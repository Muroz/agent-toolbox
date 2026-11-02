"""CLI behind the /evaluate-run skill.

Splits the qualitative-scoring flow into deterministic, testable pieces; the
actual judgement is done by the usage-evaluator subagent (model-driven), which
the skill dispatches between `context` and `persist`.

  cpt eval list-unjudged [--limit N]      -> runs needing a verdict (JSON)
  cpt eval context --run-id R             -> transcripts + per-turn prompts + rubric (JSON)
  cpt eval persist --run-id R [< verdict.json]
                                          -> write judge_verdicts + EAV scores

Scores are stored long-form (subject_type run|prompt), so adding a rubric
dimension never touches the schema.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import db
import rubric


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_unjudged(conn, limit: int) -> list:
    rows = conn.execute(
        """SELECT r.run_id, r.capture_mode, COALESCE(r.task_label, ''),
                  r.started_at
           FROM runs r
           WHERE EXISTS (SELECT 1 FROM turns t WHERE t.run_id = r.run_id)
             AND NOT EXISTS (SELECT 1 FROM judge_verdicts v WHERE v.run_id = r.run_id)
           ORDER BY r.started_at DESC LIMIT ?""", (limit,)).fetchall()
    return [{"run_id": r[0], "capture_mode": r[1], "task_label": r[2],
             "started_at": r[3]} for r in rows]


def context(conn, run_id: str) -> dict:
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM turns WHERE run_id = ?", (run_id,))]
    transcripts = []
    for sid in sessions:
        row = conn.execute(
            "SELECT transcript_path FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        if row and row[0]:
            transcripts.append(row[0])
    turns = [{"turn_id": r[0], "prompt": r[1]} for r in conn.execute(
        # is_prompt filters out Claude Code's injected records — otherwise the
        # judge scores a multi-thousand-word `<task-notification>` payload as if
        # it were the user's prompt writing.
        "SELECT turn_id, prompt_text FROM turns "
        "WHERE run_id = ? AND query_source = 'main' AND is_prompt = 1 "
        "ORDER BY seq", (run_id,))]
    return {"run_id": run_id, "rubric_path": str(rubric.RUBRIC_PATH),
            "rubric_version": rubric.version(), "transcripts": transcripts,
            "turns": turns}


def persist(conn, run_id: str, verdict: dict) -> dict:
    rv = verdict.get("rubric_version") or rubric.version()
    now = _now()
    conn.execute(
        """INSERT INTO judge_verdicts
           (run_id, model, rubric_version, overall_grade, notes, created_at)
           VALUES (?,?,?,?,?,?)""",
        (run_id, verdict.get("model"), rv, verdict.get("overall_grade"),
         verdict.get("notes"), now))
    n = 0
    for s in verdict.get("run_scores", []):
        conn.execute(
            """INSERT INTO scores
               (subject_type, subject_id, dimension, score, rationale,
                rubric_version, created_at)
               VALUES ('run', ?, ?, ?, ?, ?, ?)""",
            (run_id, s["dimension"], s.get("score"), s.get("rationale"), rv, now))
        n += 1
    for s in verdict.get("prompt_scores", []):
        conn.execute(
            """INSERT INTO scores
               (subject_type, subject_id, dimension, score, rationale,
                rubric_version, created_at)
               VALUES ('prompt', ?, ?, ?, ?, ?, ?)""",
            (s["turn_id"], s["dimension"], s.get("score"), s.get("rationale"),
             rv, now))
        n += 1
    conn.commit()
    return {"run_id": run_id, "rubric_version": rv, "scores_written": n}


def reconcile(conn, run_id: str, rubric_version: str | None = None) -> dict:
    """Compare scores across judge passes for a run (the second-opinion check).
    A dimension scored by more than one pass whose scores differ by >1 point is
    flagged — that's where the judges genuinely disagree.

    Scoped to one rubric version, defaulting to the newest verdict's. Rubric v2
    deliberately recalibrates ("prefer 1 over an unsupported 0 or 2"), so
    comparing a v1 score against a v2 one reports the rubric bump as judge
    disagreement.
    """
    n_verdicts = conn.execute(
        "SELECT COUNT(*) FROM judge_verdicts WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    if rubric_version is None:
        row = conn.execute(
            "SELECT rubric_version FROM judge_verdicts WHERE run_id=? "
            "ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        rubric_version = row[0] if row else None
    n_passes = conn.execute(
        "SELECT COUNT(*) FROM judge_verdicts WHERE run_id=? AND rubric_version IS ?",
        (run_id, rubric_version)).fetchone()[0]

    groups: dict = {}
    for dim, score in conn.execute(
            """SELECT dimension, score FROM scores
               WHERE subject_type='run' AND subject_id=? AND score IS NOT NULL
                 AND rubric_version IS ?""",
            (run_id, rubric_version)):
        groups.setdefault(("run", "", dim), []).append(score)
    for sid, dim, score in conn.execute(
            """SELECT s.subject_id, s.dimension, s.score FROM scores s
               JOIN turns t ON t.turn_id = s.subject_id
               WHERE s.subject_type='prompt' AND t.run_id=? AND s.score IS NOT NULL
                 AND s.rubric_version IS ?""",
            (run_id, rubric_version)):
        groups.setdefault(("prompt", sid, dim), []).append(score)

    disagreements = []
    for (stype, sid, dim), scores in groups.items():
        if len(scores) < 2:
            continue
        spread = max(scores) - min(scores)
        if spread > 1:
            disagreements.append({
                "subject_type": stype, "subject_id": sid, "dimension": dim,
                "scores": sorted(scores), "spread": spread})
    disagreements.sort(key=lambda d: d["spread"], reverse=True)
    return {"run_id": run_id, "verdicts": n_verdicts,
            "rubric_version": rubric_version, "passes_compared": n_passes,
            "disagreements": disagreements}


def main() -> int:
    p = argparse.ArgumentParser(description="Qualitative scoring backbone.")
    sub = p.add_subparsers(dest="cmd", required=True)

    lu = sub.add_parser("list-unjudged")
    lu.add_argument("--limit", type=int, default=10)
    lu.add_argument("--data-dir", default=None)

    ctx = sub.add_parser("context")
    ctx.add_argument("--run-id", required=True)
    ctx.add_argument("--data-dir", default=None)

    pe = sub.add_parser("persist")
    pe.add_argument("--run-id", required=True)
    pe.add_argument("--json-file", default=None,
                    help="verdict JSON file; reads stdin if omitted")
    pe.add_argument("--data-dir", default=None)

    rc = sub.add_parser("reconcile")
    rc.add_argument("--run-id", required=True)
    rc.add_argument("--rubric-version", default=None,
                    help="compare passes under this rubric only "
                         "(default: the newest verdict's)")
    rc.add_argument("--data-dir", default=None)

    args = p.parse_args()
    db.init_db(args.data_dir)
    conn = db.connect(args.data_dir)
    try:
        if args.cmd == "list-unjudged":
            print(json.dumps(list_unjudged(conn, args.limit), indent=2))
        elif args.cmd == "context":
            print(json.dumps(context(conn, args.run_id), indent=2))
        elif args.cmd == "reconcile":
            print(json.dumps(
                reconcile(conn, args.run_id, args.rubric_version), indent=2))
        else:
            raw = (open(args.json_file).read() if args.json_file
                   else sys.stdin.read())
            print(json.dumps(persist(conn, args.run_id, json.loads(raw))))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
