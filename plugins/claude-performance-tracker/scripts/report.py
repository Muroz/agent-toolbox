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

import db

MIN_SAMPLES = 5  # below this, comparison reports "insufficient data" rather than ranking.


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

    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
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
    args = parser.parse_args()

    if args.view == "overview":
        print(render_overview_for(args.data_dir))
    else:
        print(_not_implemented(args.view))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
