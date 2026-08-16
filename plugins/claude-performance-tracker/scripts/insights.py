"""Read-time insight aggregations over the raw runs / turns / scores tables.

Sits between raw storage and `report.py`'s formatting: `report.py` (and the
lessons-synthesizer subagent) call these pure `conn -> data` helpers, so all
derivation stays at read time — nothing is pre-aggregated in storage. Same design
rule as `report.py`.

  cpt insights context   -> JSON evidence pack for the lessons-synthesizer subagent
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median

import db

# Below this many successful runs a bucket is not "confident" enough to rank —
# mirrors report.MIN_SAMPLES (which aliases this).
MIN_SAMPLES = 5

# Approach dimensions the compare/recommend views group by -> runs column.
APPROACH_DIMENSIONS = {
    "model": "models",
    "mode": "permission_mode",
    "subagent": "subagents_used",
    "skill": "skills_used",
    "effort": "effort",
}

# The run-level friction signals we watch (columns on `runs`).
FRICTION_SIGNALS = [
    "interrupts", "re_prompts", "edits_without_read",
    "reasoning_loops", "premature_stops",
]

# friction column -> the rubric dimension that already covers it (None = gap,
# i.e. a candidate to add to rubric.yaml).
FRICTION_TO_RUBRIC = {
    "reasoning_loops": "reasoning_loops",
    "edits_without_read": "blind_edits",
    "premature_stops": "premature_stopping",
    "interrupts": None,
    "re_prompts": None,
}


# ----- approach comparison / recommendation --------------------------------

def _success_rows(conn: sqlite3.Connection, col: str) -> list:
    """Self-reported successful tracked runs, tagged with bucket + cost.

    Inferred successes are deliberately excluded — only ground-truth outcomes
    are ranked, so a cheap-but-guessed run can never crown a false winner.
    """
    return conn.execute(
        f"""SELECT task_type, size_class, COALESCE({col}, '(none)') AS approach,
                   output_tokens,
                   input_tokens + output_tokens + cache_read_tokens
                       + cache_creation_tokens AS total_tokens,
                   num_prompts, wall_clock_ms
            FROM runs
            WHERE capture_mode = 'tracked' AND outcome = 'success'
              AND outcome_source = 'self_report'
              AND task_type IS NOT NULL AND size_class IS NOT NULL"""
    ).fetchall()


def bucket_winners(conn: sqlite3.Connection, by: str = "model",
                   min_samples: int = MIN_SAMPLES) -> list:
    """Per {task_type x size} bucket, approaches ranked by median total tokens
    (lower is better). Each bucket carries `n_success` and a `confident` flag."""
    col = APPROACH_DIMENSIONS.get(by)
    if col is None:
        raise ValueError(f"unknown approach dimension: {by}")

    buckets: dict = {}
    for r in _success_rows(conn, col):
        buckets.setdefault((r[0], r[1]), {}).setdefault(r[2], []).append(r)

    out = []
    for (ttype, size), approaches in buckets.items():
        n_success = sum(len(v) for v in approaches.values())
        ranked = []
        for approach, runs in approaches.items():
            wall = [r[6] for r in runs if r[6] is not None] or [0]
            ranked.append({
                "approach": approach,
                "n": len(runs),
                "median_total_tokens": int(median([r[4] for r in runs])),
                "median_output_tokens": int(median([r[3] for r in runs])),
                "median_prompts": int(median([r[5] for r in runs])),
                "median_wall_ms": int(median(wall)),
            })
        ranked.sort(key=lambda x: x["median_total_tokens"])
        out.append({
            "task_type": ttype, "size": size, "by": by,
            "n_success": n_success, "confident": n_success >= min_samples,
            "ranked": ranked,
        })
    out.sort(key=lambda b: (b["task_type"] or "", b["size"] or ""))
    return out


def recommend(conn: sqlite3.Connection, task_type: str, size: str,
              by: str = "model", min_samples: int = MIN_SAMPLES) -> dict:
    """The one bucket matching {task_type x size}, or an empty (unconfident)
    bucket when nothing comparable has been recorded yet."""
    for b in bucket_winners(conn, by, min_samples):
        if b["task_type"] == task_type and b["size"] == size:
            return b
    return {"task_type": task_type, "size": size, "by": by,
            "n_success": 0, "confident": False, "ranked": []}


# ----- friction / anti-patterns --------------------------------------------

def parse_window(since: str | None) -> tuple[str | None, bool]:
    """'30d' / '12h' -> (ISO cutoff, True).

    No window asked for is not an error -> (None, True). Anything we can't parse
    -> (None, False), so the caller can say the window was ignored rather than
    silently reporting all time under a header that claims otherwise.
    """
    if not since:
        return None, True
    m = re.match(r"(\d+)\s*([dh])$", since.strip())
    if not m:
        return None, False
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
    return (datetime.now(timezone.utc) - delta).isoformat(), True


def recurring_friction(conn: sqlite3.Connection, since: str | None = None) -> list:
    """Each friction signal that fired, ranked by how many runs it hit, with the
    share landing in a bad outcome and where it clusters (task types)."""
    cutoff, _ = parse_window(since)
    base = "started_at IS NOT NULL"
    params: list = []
    if cutoff:
        base += " AND started_at >= ?"
        params.append(cutoff)

    out = []
    for sig in FRICTION_SIGNALS:
        agg = conn.execute(
            f"""SELECT COUNT(*),
                       SUM(CASE WHEN outcome IN ('failed','partial') THEN 1 ELSE 0 END),
                       SUM({sig})
                FROM runs WHERE {base} AND {sig} > 0""", params).fetchone()
        runs_hit = agg[0] or 0
        if runs_hit == 0:
            continue
        tt = conn.execute(
            f"""SELECT COALESCE(task_type,'(untagged)'), COUNT(*)
                FROM runs WHERE {base} AND {sig} > 0
                GROUP BY 1 ORDER BY 2 DESC LIMIT 3""", params).fetchall()
        out.append({
            "signal": sig,
            "runs_hit": runs_hit,
            "bad_outcome_runs": agg[1] or 0,
            "total_occurrences": agg[2] or 0,
            "top_task_types": [{"task_type": r[0], "runs": r[1]} for r in tt],
            "rubric_dimension": FRICTION_TO_RUBRIC.get(sig),
        })
    out.sort(key=lambda x: x["runs_hit"], reverse=True)
    return out


def weak_prompt_dimensions(conn: sqlite3.Connection) -> list:
    """Average judge score per prompt-quality dimension, weakest first.

    `rubric_versions` rides along because a rubric bump recalibrates the scale:
    an average pooled across versions is not a like-for-like comparison, and the
    caller should say so rather than present it as one trend.
    """
    rows = conn.execute(
        """SELECT dimension, AVG(score), COUNT(*),
                  GROUP_CONCAT(DISTINCT rubric_version)
           FROM scores WHERE subject_type='prompt' AND score IS NOT NULL
           GROUP BY dimension ORDER BY AVG(score) ASC""").fetchall()
    return [{"dimension": r[0], "avg_score": round(r[1], 2), "n": r[2],
             "rubric_versions": sorted((r[3] or "").split(",")) if r[3] else []}
            for r in rows]


# ----- degradation ----------------------------------------------------------

def _period_len(period: str) -> int:
    return 7 if period == "month" else 10


def period_rows(conn: sqlite3.Connection, period: str = "month") -> list:
    """Per (model, period) trend row.

    The shared basis for both the degradation table and the first-vs-last
    deltas, so the two views can never drift apart.
    """
    plen = _period_len(period)
    friction_sum = "+".join(FRICTION_SIGNALS)
    rows = conn.execute(
        f"""SELECT COALESCE(models,'(unknown)'),
                   substr(started_at,1,{plen}),
                   COUNT(*),
                   AVG(CASE WHEN num_prompts>0
                            THEN CAST(output_tokens AS REAL)/num_prompts
                            ELSE output_tokens END),
                   AVG(interrupts), AVG(edits_without_read),
                   AVG(reasoning_loops), AVG(peak_context_pct),
                   AVG({friction_sum})
            FROM runs
            WHERE ended_at IS NOT NULL AND started_at IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    return [{"model": r[0], "period": r[1], "n": r[2], "out_per_prompt": r[3],
             "interrupts": r[4], "edits_without_read": r[5],
             "reasoning_loops": r[6], "peak_context_pct": r[7],
             "friction": r[8]} for r in rows]


def judge_by_period(conn: sqlite3.Connection, period: str = "month") -> dict:
    """(model, period) -> {'avg': mean run-level judge score, 'versions': [...]}.

    Versions are carried, not filtered: dropping all but the newest rubric would
    blank out older periods, which is the opposite of what a trend view needs.
    The caller flags a mixed set instead.
    """
    plen = _period_len(period)
    out = {}
    for model, per, avg, versions in conn.execute(
            f"""SELECT COALESCE(r.models,'(unknown)'),
                       substr(r.started_at,1,{plen}), AVG(s.score),
                       GROUP_CONCAT(DISTINCT s.rubric_version)
                FROM runs r JOIN scores s
                  ON s.subject_type='run' AND s.subject_id=r.run_id
                WHERE r.started_at IS NOT NULL AND s.score IS NOT NULL
                GROUP BY 1, 2"""):
        out[(model, per)] = {
            "avg": avg,
            "versions": sorted((versions or "").split(",")) if versions else [],
        }
    return out


def degradation_deltas(conn: sqlite3.Connection, period: str = "month") -> list:
    """First-vs-last-period drift per model: rising friction / output-per-prompt
    or a falling judge score is the drift signal. Models with <2 periods skip."""
    by_model: dict = {}
    for r in period_rows(conn, period):
        by_model.setdefault(r["model"], []).append(r)
    judge = judge_by_period(conn, period)

    out = []
    for model, mrows in by_model.items():
        if len(mrows) < 2:
            continue
        first, last = mrows[0], mrows[-1]
        jf = judge.get((model, first["period"]))
        jl = judge.get((model, last["period"]))
        jd = round(jl["avg"] - jf["avg"], 2) if jf and jl else None
        out.append({
            "model": model, "periods": len(mrows),
            "first_period": first["period"], "last_period": last["period"],
            "out_per_prompt_delta": round(
                (last["out_per_prompt"] or 0) - (first["out_per_prompt"] or 0), 1),
            "friction_delta": round(
                (last["friction"] or 0) - (first["friction"] or 0), 2),
            "judge_score_delta": jd,
        })
    return out


# ----- lessons evidence pack (Feature 3) -----------------------------------

def synthesis_context(conn: sqlite3.Connection) -> dict:
    """Compact, deterministic evidence pack the lessons-synthesizer turns into a
    playbook. Numbers only — the subagent adds no facts of its own."""
    failure_notes = [
        {"task": r[0], "outcome": r[1], "satisfaction": r[2], "note": r[3]}
        for r in conn.execute(
            """SELECT COALESCE(task_label,'(untitled)'), outcome, satisfaction, note
               FROM runs
               WHERE note IS NOT NULL AND note != ''
                 AND (outcome IN ('failed','partial') OR satisfaction <= 2)
               ORDER BY started_at DESC LIMIT 10""")]
    return {
        "generated_for": "lessons-synthesizer",
        "bucket_winners": bucket_winners(conn),
        "recurring_friction": recurring_friction(conn),
        "weak_prompt_dimensions": weak_prompt_dimensions(conn),
        "degradation_deltas": degradation_deltas(conn),
        "failure_notes": failure_notes,
    }


# ----- CLI ------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Read-time insight aggregations.")
    sub = p.add_subparsers(dest="cmd", required=True)
    ctx = sub.add_parser("context")
    ctx.add_argument("--data-dir", default=None)
    lp = sub.add_parser("lessons-path")
    lp.add_argument("--data-dir", default=None)
    args = p.parse_args()

    # lessons-path needs no DB connection — just resolve the writable dir.
    if args.cmd == "lessons-path":
        print(db.data_dir(args.data_dir) / "lessons.md")
        return 0

    db.init_db(args.data_dir)
    conn = db.connect(args.data_dir)
    try:
        if args.cmd == "context":
            print(json.dumps(synthesis_context(conn), indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
