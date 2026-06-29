"""Reporting for claude-performance-tracker.

All numbers are computed here, at read time, from the raw `runs` / `turns` /
`scores` tables — nothing is pre-aggregated in storage. That keeps the data
reusable for any future report shape or exporter (JSON/CSV/HTML/dashboard).

Token totals are computed from `turns` (the source of truth), so they reconcile
with `runs` aggregates and are correct even for runs that are still open.

Views:
  * overview      — totals, per-project, per-model, per-day            (this slice)
  * compare       — bucketed {task_type x size} cost-per-SUCCESS       (later slice)
  * degradation   — efficiency/quality trend over time, per model      (later slice)
  * run <id>      — full scorecard + judge verdict for one run         (later slice)
"""

from __future__ import annotations

import argparse
import sqlite3
from statistics import median

import db

MIN_SAMPLES = 5  # below this, comparison reports "insufficient data" rather than ranking.

# Approach dimensions the compare view can group by -> runs column.
COMPARE_DIMENSIONS = {
    "model": "models",
    "mode": "permission_mode",
    "subagent": "subagents_used",
    "skill": "skills_used",
    "effort": "effort",
}


# ----- formatting helpers ---------------------------------------------------

def _n(x) -> str:
    return f"{int(x or 0):,}"


def _ms(ms) -> str:
    if not ms:
        return "—"
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# ----- views ----------------------------------------------------------------

def render_overview(conn: sqlite3.Connection) -> str:
    tot = conn.execute(
        """SELECT COUNT(*),
                  COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                  COALESCE(SUM(cache_read_tokens),0),
                  COALESCE(SUM(cache_creation_tokens),0),
                  COALESCE(SUM(num_tool_calls),0),
                  MIN(started_at), MAX(ended_at)
           FROM turns"""
    ).fetchone()
    n_turns = tot[0]
    if not n_turns:
        return "No usage captured yet. Run some sessions and check back."

    n_runs = conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM turns").fetchone()[0]
    wall = conn.execute(
        "SELECT COALESCE(SUM(wall_clock_ms),0) FROM runs").fetchone()[0]
    day0 = (tot[6] or "")[:10]
    day1 = (tot[7] or "")[:10]

    parts = [
        "# Usage overview",
        "",
        f"**{_n(n_runs)} runs · {_n(n_turns)} prompts · {day0} → {day1}**",
        "",
        _table(["metric", "total"], [
            ["input tokens", _n(tot[1])],
            ["output tokens", _n(tot[2])],
            ["cache read", _n(tot[3])],
            ["cache creation", _n(tot[4])],
            ["tool calls", _n(tot[5])],
            ["wall-clock", _ms(wall)],
        ]),
    ]

    by_model = conn.execute(
        """SELECT COALESCE(model,'(unknown)'), COUNT(*),
                  SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens)
           FROM turns GROUP BY model ORDER BY SUM(output_tokens) DESC"""
    ).fetchall()
    parts += ["", "## By model",
              _table(["model", "prompts", "input", "output", "cache read"],
                     [[m, _n(c), _n(i), _n(o), _n(cr)] for m, c, i, o, cr in by_model])]

    by_proj = conn.execute(
        """SELECT COALESCE(r.project,'(none)'), COUNT(DISTINCT r.run_id),
                  COUNT(t.turn_id), SUM(t.input_tokens), SUM(t.output_tokens)
           FROM runs r LEFT JOIN turns t ON t.run_id = r.run_id
           GROUP BY r.project ORDER BY SUM(t.output_tokens) DESC"""
    ).fetchall()
    parts += ["", "## By project",
              _table(["project", "runs", "prompts", "input", "output"],
                     [[p, _n(rn), _n(pr), _n(i), _n(o)] for p, rn, pr, i, o in by_proj])]

    by_source = conn.execute(
        """SELECT query_source, COUNT(*), SUM(input_tokens), SUM(output_tokens)
           FROM turns GROUP BY query_source ORDER BY query_source"""
    ).fetchall()
    if any(r[0] == "subagent" for r in by_source):
        parts += ["", "## By query source",
                  _table(["source", "turns", "input", "output"],
                         [[s, _n(c), _n(i), _n(o)] for s, c, i, o in by_source])]

    by_day = conn.execute(
        """SELECT substr(started_at,1,10) AS day, COUNT(*),
                  SUM(input_tokens), SUM(output_tokens)
           FROM turns WHERE started_at IS NOT NULL
           GROUP BY day ORDER BY day"""
    ).fetchall()
    parts += ["", "## By day",
              _table(["day", "prompts", "input", "output"],
                     [[d, _n(c), _n(i), _n(o)] for d, c, i, o in by_day])]

    return "\n".join(parts)


def render_overview_for(data_dir: str | None) -> str:
    conn = db.connect(data_dir)
    try:
        return render_overview(conn)
    finally:
        conn.close()


def render_compare(conn: sqlite3.Connection, by: str = "model",
                   min_samples: int = MIN_SAMPLES) -> str:
    """Rank approaches by median cost per successful run, within each
    {task_type x size} bucket. Only self-reported successful tracked runs are
    ranked — inferred outcomes are never blended into the ranking, just flagged.
    """
    col = COMPARE_DIMENSIONS.get(by)
    if col is None:
        return (f"Unknown comparison dimension '{by}'. "
                f"Choose one of: {', '.join(sorted(COMPARE_DIMENSIONS))}.")

    rows = conn.execute(
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

    inferred = conn.execute(
        """SELECT COUNT(*) FROM runs
           WHERE outcome = 'success' AND outcome_source = 'inferred'"""
    ).fetchone()[0]

    if not rows:
        msg = ("No self-reported successful tracked runs yet. "
               "Use /track and /track-done to record comparable runs.")
        if inferred:
            msg += f"\n\n({inferred} inferred-success run(s) exist but are not ranked.)"
        return msg

    # bucket -> approach -> list of run dicts
    buckets: dict = {}
    for r in rows:
        b = (r[0], r[1])
        buckets.setdefault(b, {}).setdefault(r[2], []).append(r)

    parts = [f"# Approach comparison (by {by})", "",
             "Ranked on median **total tokens per successful run** "
             "(lower is better). Only self-reported successes count."]
    if inferred:
        parts.append(f"_{inferred} inferred-success run(s) excluded from ranking._")

    for bucket in sorted(buckets, key=lambda b: (b[0] or "", b[1] or "")):
        approaches = buckets[bucket]
        n_success = sum(len(v) for v in approaches.values())
        title = f"## {bucket[0]} · {bucket[1]}"
        if n_success < min_samples:
            parts += ["", f"{title} — insufficient data: {n_success} successful "
                          f"run(s) (need ≥{min_samples} to compare)."]
            continue

        ranked = []
        for approach, runs in approaches.items():
            ranked.append((
                approach, len(runs),
                int(median([r[4] for r in runs])),   # total_tokens
                int(median([r[3] for r in runs])),   # output_tokens
                int(median([r[5] for r in runs])),   # num_prompts
                median([r[6] for r in runs if r[6] is not None] or [0]),  # wall_ms
            ))
        ranked.sort(key=lambda x: x[2])  # by median total tokens
        table = _table(
            [by, "n", "med total tok", "med output tok", "med prompts", "med wall"],
            [[a, n, _n(tt), _n(ot), _n(p),
              (_ms(w) + (" ⚠n=1" if n < 2 else ""))]
             for a, n, tt, ot, p, w in ranked])
        parts += ["", f"{title}  ({n_success} successful runs)", table]

    return "\n".join(parts)


def render_compare_for(data_dir: str | None, by: str, min_samples: int) -> str:
    conn = db.connect(data_dir)
    try:
        return render_compare(conn, by, min_samples)
    finally:
        conn.close()


def render_degradation(conn: sqlite3.Connection, period: str = "month") -> str:
    """Per-model trend of efficiency/quality metrics over time. Rising friction
    or a falling judge score across periods is the drift signal."""
    plen = 7 if period == "month" else 10
    rows = conn.execute(
        f"""SELECT COALESCE(models,'(unknown)') AS model,
                   substr(started_at,1,{plen}) AS period,
                   COUNT(*) AS n,
                   AVG(CASE WHEN num_prompts>0
                            THEN CAST(output_tokens AS REAL)/num_prompts
                            ELSE output_tokens END) AS out_per_prompt,
                   AVG(interrupts) AS interrupts,
                   AVG(edits_without_read) AS ewr,
                   AVG(reasoning_loops) AS loops,
                   AVG(peak_context_pct) AS ctx
            FROM runs
            WHERE ended_at IS NOT NULL AND started_at IS NOT NULL
            GROUP BY model, period ORDER BY model, period"""
    ).fetchall()
    if not rows:
        return "No finalized runs yet."

    judge = {}
    for jr in conn.execute(
        f"""SELECT COALESCE(r.models,'(unknown)') AS model,
                   substr(r.started_at,1,{plen}) AS period, AVG(s.score)
            FROM runs r JOIN scores s
              ON s.subject_type='run' AND s.subject_id=r.run_id
            WHERE r.started_at IS NOT NULL GROUP BY model, period"""):
        judge[(jr[0], jr[1])] = jr[2]

    by_model: dict = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)

    parts = [f"# Degradation watch (by {period})", "",
             "Per-model trend. Rising friction or a falling judge score over "
             "time signals drift."]
    for model, mrows in by_model.items():
        body = []
        for r in mrows:
            jv = judge.get((model, r["period"]))
            body.append([r["period"], _n(r["n"]), _n(r["out_per_prompt"]),
                         round(r["interrupts"], 2), round(r["ewr"], 2),
                         round(r["loops"], 2), round(r["ctx"] or 0, 1),
                         (round(jv, 2) if jv is not None else "—")])
        parts += ["", f"## {model}",
                  _table(["period", "runs", "out/prompt", "interrupts",
                          "edits w/o read", "loops", "ctx%", "judge"], body)]
    return "\n".join(parts)


def render_run(conn: sqlite3.Connection, run_id: str) -> str:
    r = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if r is None:
        return f"No run found with id '{run_id}'."

    def g(k):
        return r[k] if r[k] is not None else "—"

    parts = [f"# Run scorecard — {run_id}", "",
             _table(["field", "value"], [
                 ["capture mode", g("capture_mode")],
                 ["project", g("project")],
                 ["task", f'{g("task_label")} [{g("task_type")}/{g("size_class")}]'],
                 ["started", g("started_at")],
                 ["ended", g("ended_at")],
                 ["closed by", g("closed_by")],
             ]),
             "", "## Approach",
             _table(["field", "value"], [
                 ["models", g("models")],
                 ["effort", g("effort")],
                 ["permission mode", g("permission_mode")],
                 ["subagents", g("subagents_used")],
                 ["skills", g("skills_used")],
                 ["mcp", g("mcp_tools_used")],
                 ["intended approach", g("intended_approach")],
             ]),
             "", "## Cost & output",
             _table(["metric", "value"], [
                 ["prompts", _n(r["num_prompts"])],
                 ["tool calls", _n(r["num_tool_calls"])],
                 ["input tokens", _n(r["input_tokens"])],
                 ["output tokens", _n(r["output_tokens"])],
                 ["cache read", _n(r["cache_read_tokens"])],
                 ["cache creation", _n(r["cache_creation_tokens"])],
                 ["wall-clock", _ms(r["wall_clock_ms"])],
                 ["lines +/-", f'+{_n(r["lines_added"])} / -{_n(r["lines_removed"])}'],
                 ["files touched", _n(r["files_touched"])],
                 ["doc words", _n(r["doc_words"])],
             ]),
             "", "## Friction & context",
             _table(["signal", "value"], [
                 ["interrupts", _n(r["interrupts"])],
                 ["re-prompts", _n(r["re_prompts"])],
                 ["edits without read", _n(r["edits_without_read"])],
                 ["reasoning loops", _n(r["reasoning_loops"])],
                 ["premature stops", _n(r["premature_stops"])],
                 ["peak context %", g("peak_context_pct")],
                 ["compactions", _n(r["compact_count"])],
                 ["clears", _n(r["clear_count"])],
             ])]

    qs = conn.execute(
        """SELECT query_source, SUM(input_tokens), SUM(output_tokens)
           FROM turns WHERE run_id=? GROUP BY query_source""", (run_id,)).fetchall()
    if any(row[0] == "subagent" for row in qs):
        parts += ["", "## By query source",
                  _table(["source", "input", "output"],
                         [[row[0], _n(row[1]), _n(row[2])] for row in qs])]

    outcome = f'{g("outcome")} ({g("outcome_source")})'
    if r["satisfaction"] is not None:
        outcome += f' · satisfaction {r["satisfaction"]}/5'
    parts += ["", "## Outcome", outcome]
    if r["note"]:
        parts += [f'_note:_ {r["note"]}']
    if r["outcome_source"] == "inferred" and r["inferred_signals"]:
        parts += [f'_inferred from:_ `{r["inferred_signals"]}`']

    verdict = conn.execute(
        """SELECT overall_grade, notes, rubric_version, created_at
           FROM judge_verdicts WHERE run_id=? ORDER BY created_at DESC LIMIT 1""",
        (run_id,)).fetchone()
    if verdict:
        parts += ["", "## Judge verdict",
                  f'**{g_verdict(verdict, "overall_grade")}** '
                  f'(rubric v{verdict["rubric_version"]})']
        if verdict["notes"]:
            parts += [verdict["notes"]]
        run_scores = conn.execute(
            "SELECT dimension, score, rationale FROM scores "
            "WHERE subject_type='run' AND subject_id=? ORDER BY dimension",
            (run_id,)).fetchall()
        if run_scores:
            parts += ["", "### Agent behavior",
                      _table(["dimension", "score", "why"],
                             [[s[0], s[1], (s[2] or "")[:80]] for s in run_scores])]
        prompt_scores = conn.execute(
            """SELECT t.seq, s.dimension, s.score, substr(t.prompt_text,1,40)
               FROM scores s JOIN turns t ON t.turn_id = s.subject_id
               WHERE s.subject_type='prompt' AND t.run_id=?
               ORDER BY t.seq, s.dimension""", (run_id,)).fetchall()
        if prompt_scores:
            parts += ["", "### Prompt quality",
                      _table(["turn", "dimension", "score", "prompt"],
                             [[p[0], p[1], p[2], (p[3] or "") + "…"]
                              for p in prompt_scores])]
    return "\n".join(parts)


def g_verdict(row, key):
    return row[key] if row[key] is not None else "—"


def _not_implemented(name: str) -> str:
    return f"`{name}` view is not implemented yet."


# ----- CLI ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Report on tracked usage.")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument(
        "view", nargs="?", default="overview",
        choices=["overview", "compare", "degradation", "run"])
    parser.add_argument("run_id", nargs="?", default=None)
    parser.add_argument("--by", default="model",
                        choices=sorted(COMPARE_DIMENSIONS))
    parser.add_argument("--min", type=int, default=MIN_SAMPLES,
                        help="min successful runs per bucket to rank")
    parser.add_argument("--period", default="month", choices=["month", "day"])
    args = parser.parse_args()

    conn = db.connect(args.data_dir)
    try:
        if args.view == "overview":
            print(render_overview(conn))
        elif args.view == "compare":
            print(render_compare(conn, args.by, args.min))
        elif args.view == "degradation":
            print(render_degradation(conn, args.period))
        elif args.view == "run":
            if not args.run_id:
                print("Usage: report.py run <run_id>")
                return 2
            print(render_run(conn, args.run_id))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
