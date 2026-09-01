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

import cost
import db
import evaluate
import insights

# below this, comparison reports "insufficient data" rather than ranking.
MIN_SAMPLES = insights.MIN_SAMPLES

# Approach dimensions the compare/recommend views can group by -> runs column.
COMPARE_DIMENSIONS = insights.APPROACH_DIMENSIONS


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


def _usd(amount) -> str:
    """Dollar estimate, or a dash when the model's price is not known."""
    if amount is None:
        return "—"
    return f"${amount:,.2f}"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# ----- views ----------------------------------------------------------------

def render_overview(conn: sqlite3.Connection) -> str:
    tot = conn.execute(
        f"""SELECT COUNT(*),
                  COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                  COALESCE(SUM(cache_read_tokens),0),
                  COALESCE(SUM(cache_creation_tokens),0),
                  COALESCE(SUM(num_tool_calls),0),
                  MIN(started_at), MAX(ended_at),
                  COALESCE(SUM({cost.WEIGHTED_SQL}),0),
                  COALESCE(SUM(active_ms),0),
                  COALESCE(SUM(CASE WHEN is_prompt = 1 AND query_source = 'main'
                                    THEN 1 ELSE 0 END),0),
                  COALESCE(SUM(total_tokens_agg),0)
           FROM turns"""
    ).fetchone()
    n_turns = tot[0]
    if not n_turns:
        return "No usage captured yet. Run some sessions and check back."

    n_runs = conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM turns").fetchone()[0]
    elapsed = conn.execute(
        "SELECT COALESCE(SUM(wall_clock_ms),0) FROM runs").fetchone()[0]
    weighted, active, n_prompts, agg = tot[8], tot[9], tot[10], tot[11]
    day0 = (tot[6] or "")[:10]
    day1 = (tot[7] or "")[:10]

    rows = [
        ["input tokens", _n(tot[1])],
        ["output tokens", _n(tot[2])],
        ["cache read", _n(tot[3])],
        ["cache creation", _n(tot[4])],
        ["**weighted tokens**", f"**{_n(int(weighted))}**"],
        ["tool calls", _n(tot[5])],
        ["active time", _ms(active)],
        ["elapsed span", _ms(elapsed)],
    ]
    if agg:
        rows.insert(4, ["subagent tokens (unsplit)", _n(agg)])

    parts = [
        "# Usage overview",
        "",
        f"**{_n(n_runs)} runs · {_n(n_prompts)} prompts · {_n(n_turns)} turns · "
        f"{day0} → {day1}**",
        "",
        _table(["metric", "total"], rows),
        "",
        "_Weighted tokens are input-equivalent units (output 5x, cache write "
        "1.25-2x, cache read 0.1x) — the only figure comparable across "
        "approaches. Active time caps idle gaps; elapsed span is calendar time "
        "and includes them._",
    ]
    if agg:
        parts.append(
            "_Unsplit subagent tokens come from backgrounded agents that only "
            "reported a total, so they are excluded from the weighted figure._")

    by_model = conn.execute(
        f"""SELECT COALESCE(model,'(unknown)'), COUNT(*),
                  SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),
                  SUM({cost.WEIGHTED_SQL})
           FROM turns GROUP BY model ORDER BY SUM(output_tokens) DESC"""
    ).fetchall()
    parts += ["", "## By model",
              _table(["model", "turns", "input", "output", "cache read",
                      "weighted", "est. USD"],
                     [[m, _n(c), _n(i), _n(o), _n(cr), _n(int(w or 0)),
                       _usd(cost.usd(w or 0, m))]
                      for m, c, i, o, cr, w in by_model])]

    by_proj = conn.execute(
        """SELECT COALESCE(r.project,'(none)'), COUNT(DISTINCT r.run_id),
                  COUNT(t.turn_id), SUM(t.input_tokens), SUM(t.output_tokens)
           FROM runs r JOIN turns t ON t.run_id = r.run_id
           GROUP BY r.project ORDER BY SUM(t.output_tokens) DESC"""
    ).fetchall()
    parts += ["", "## By project",
              _table(["project", "runs", "prompts", "input", "output"],
                     [[p, _n(rn), _n(pr), _n(i), _n(o)] for p, rn, pr, i, o in by_proj])]

    by_source = conn.execute(
        f"""SELECT query_source, COUNT(*), SUM(input_tokens), SUM(output_tokens),
                   SUM({cost.WEIGHTED_SQL}) + SUM(total_tokens_agg)
            FROM turns GROUP BY query_source ORDER BY query_source"""
    ).fetchall()
    if any(r[0] == "subagent" for r in by_source):
        parts += ["", "## By query source",
                  _table(["source", "turns", "input", "output", "weighted"],
                         [[q, _n(c), _n(i), _n(o), _n(int(w or 0))]
                          for q, c, i, o, w in by_source])]
        by_agent = conn.execute(
            f"""SELECT COALESCE(agent_type,'(unknown)'), COUNT(*),
                       SUM(output_tokens), SUM({cost.WEIGHTED_SQL})
                FROM turns WHERE query_source = 'subagent'
                GROUP BY agent_type ORDER BY 4 DESC"""
        ).fetchall()
        if by_agent:
            parts += ["", "## By subagent",
                      _table(["agent", "runs", "output", "weighted"],
                             [[a, _n(c), _n(o), _n(int(w or 0))]
                              for a, c, o, w in by_agent])]

    by_day = conn.execute(
        f"""SELECT substr(started_at,1,10) AS day,
                   SUM(CASE WHEN is_prompt = 1 AND query_source = 'main'
                            THEN 1 ELSE 0 END),
                   SUM(input_tokens), SUM(output_tokens),
                   SUM({cost.WEIGHTED_SQL})
            FROM turns WHERE started_at IS NOT NULL
            GROUP BY day ORDER BY day"""
    ).fetchall()
    parts += ["", "## By day",
              _table(["day", "prompts", "input", "output", "weighted"],
                     [[d, _n(c), _n(i), _n(o), _n(int(w or 0))]
                      for d, c, i, o, w in by_day])]

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
    if by not in COMPARE_DIMENSIONS:
        return (f"Unknown comparison dimension '{by}'. "
                f"Choose one of: {', '.join(sorted(COMPARE_DIMENSIONS))}.")

    buckets = insights.bucket_winners(conn, by, min_samples)
    inferred = conn.execute(
        """SELECT COUNT(*) FROM runs
           WHERE outcome = 'success' AND outcome_source = 'inferred'"""
    ).fetchone()[0]

    if not buckets:
        msg = ("No self-reported successful tracked runs yet. "
               "Use /track and /track-done to record comparable runs.")
        if inferred:
            msg += f"\n\n({inferred} inferred-success run(s) exist but are not ranked.)"
        return msg

    parts = [f"# Approach comparison (by {by})", "",
             "Ranked on median **weighted tokens per successful run** (lower is "
             "better) — input-equivalent units, so cache reuse is not punished. "
             "Only self-reported successes count."]
    if inferred:
        parts.append(f"_{inferred} inferred-success run(s) excluded from ranking._")

    for b in buckets:
        title = f"## {b['task_type']} · {b['size']}"
        if not b["confident"]:
            parts += ["", f"{title} — insufficient data: {b['n_success']} successful "
                          f"run(s) (need ≥{min_samples} to compare)."]
            continue
        table = _table(
            [by, "n", "med weighted", "med output tok", "med prompts", "med active"],
            [[a["approach"], a["n"], _n(a["median_weighted_tokens"]),
              _n(a["median_output_tokens"]), _n(a["median_prompts"]),
              (_ms(a["median_active_ms"]) + (" ⚠n=1" if a["n"] < 2 else ""))]
             for a in b["ranked"]])
        parts += ["", f"{title}  ({b['n_success']} successful runs)", table]

    return "\n".join(parts)


def render_recommend(conn: sqlite3.Connection, task_type: str | None = None,
                     size: str | None = None, by: str = "model",
                     min_samples: int = MIN_SAMPLES) -> str:
    """Answer 'for this kind of task, use approach Z' directly. With task_type +
    size, drills into one bucket; otherwise lists the best approach per bucket.
    Same honesty guard as compare — an under-sampled bucket is flagged low."""
    if by not in COMPARE_DIMENSIONS:
        return (f"Unknown dimension '{by}'. "
                f"Choose one of: {', '.join(sorted(COMPARE_DIMENSIONS))}.")

    partial = ""
    if bool(task_type) != bool(size):
        given, missing = ("--type", "--size") if task_type else ("--size", "--type")
        partial = (f"_Ignoring `{given}`: drilling into one bucket needs both "
                   f"`--type` and `--size`; `{missing}` is missing. "
                   "Showing every bucket instead._\n\n")

    if task_type and size:
        b = insights.recommend(conn, task_type, size, by, min_samples)
        title = f"# Recommended approach — {task_type} · {size} (by {by})"
        if not b["ranked"]:
            return (f"{title}\n\nNo self-reported successful runs for this bucket "
                    "yet. Track a few with /track … /track-done to build a "
                    "recommendation.")
        best = b["ranked"][0]
        if b["confident"]:
            lead = (f"→ **{best['approach']}** — median "
                    f"{_n(best['median_weighted_tokens'])} weighted tokens / success "
                    f"(n={best['n']}).")
        else:
            lead = (f"→ **{best['approach']}** _(low confidence: only "
                    f"{b['n_success']} successful run(s); need ≥{min_samples})._")
        lines = [title, "", lead]
        if len(b["ranked"]) > 1:
            lines += ["", _table(
                [by, "n", "med total tok", "med prompts"],
                [[a["approach"], a["n"], _n(a["median_weighted_tokens"]),
                  _n(a["median_prompts"])] for a in b["ranked"]])]
        return "\n".join(lines)

    buckets = insights.bucket_winners(conn, by, min_samples)
    if not buckets:
        return (partial + "No self-reported successful tracked runs yet. "
                "Use /track and /track-done to record comparable runs.")
    rows = []
    for b in buckets:
        best = b["ranked"][0]
        conf = "" if b["confident"] else " ⚠low"
        rows.append([f"{b['task_type']} · {b['size']}", best["approach"] + conf,
                     b["n_success"], _n(best["median_weighted_tokens"])])
    return partial + "\n".join([
        f"# Recommended approach per bucket (by {by})", "",
        "Cheapest-per-success approach in each {task_type × size} bucket. "
        "⚠low = under the confidence threshold.", "",
        _table(["bucket", f"best {by}", "n success", "med total tok"], rows)])


def render_antipatterns(conn: sqlite3.Connection, since: str | None = None) -> str:
    """Recurring friction + weak prompt habits, with rubric-coverage gaps flagged
    as candidate rubric dimensions (incident → eval synthesis). Read-time only —
    nothing is persisted; the catalog is recomputed from raw rows each call."""
    friction = insights.recurring_friction(conn, since)
    weak = insights.weak_prompt_dimensions(conn)

    # Only claim a window in the header if we actually applied one — an
    # unparsable `--since` silently widens the query to all time, and a header
    # that says otherwise is worse than no header.
    _, ok = insights.parse_window(since)
    window = f" (last {since})" if since and ok else ""
    note = ("" if ok else
            f"_Ignoring `--since {since}`: expected a window like `30d` or "
            "`12h`. Showing all time._\n\n")
    if not friction and not weak:
        return f"# Anti-patterns{window}\n\n{note}No friction signals recorded yet."

    parts = [f"# Anti-patterns{window}", ""]
    if note:
        parts.append(note.rstrip("\n"))
    if friction:
        parts += ["Recurring friction across runs, worst first. `bad` = share "
                  "landing in a failed/partial outcome."]
        rows = []
        for f in friction:
            cover = f["rubric_dimension"] or "— (gap)"
            spots = ", ".join(f"{t['task_type']}×{t['runs']}"
                              for t in f["top_task_types"])
            rows.append([f["signal"], f["runs_hit"], f["bad_outcome_runs"],
                         f["total_occurrences"], spots, cover])
        parts += ["", _table(
            ["signal", "runs", "bad", "total", "clusters", "rubric dim"], rows)]

    candidates = [f["signal"] for f in friction if f["rubric_dimension"] is None]
    if candidates:
        parts += ["", "## Rubric candidates", "",
                  "These recurring signals have **no** matching rubric dimension — "
                  "consider adding one to `rubric.yaml` (and bump its `version`):",
                  ""] + [f"- `{c}`" for c in candidates]

    if weak:
        parts += ["", "## Weakest prompt habits",
                  "Lowest-scoring prompt-quality dimensions (judge, 0–2):", "",
                  _table(["dimension", "avg", "n"],
                         [[w["dimension"], w["avg_score"], w["n"]] for w in weak])]
        spans = sorted({v for w in weak for v in w["rubric_versions"]})
        if len(spans) > 1:
            parts += ["", f"⚠ Averages pool rubric v{', v'.join(spans)}. A rubric "
                      "bump recalibrates the scale, so these are not a like-for-"
                      "like comparison."]
    return "\n".join(parts)


def render_degradation(conn: sqlite3.Connection, period: str = "month") -> str:
    """Per-model trend of efficiency/quality metrics over time. Rising friction
    or a falling judge score across periods is the drift signal."""
    rows = insights.period_rows(conn, period)
    if not rows:
        return "No finalized runs yet."
    judge = insights.judge_by_period(conn, period)

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
                         round(r["interrupts"], 2),
                         round(r["edits_without_read"], 2),
                         round(r["reasoning_loops"], 2),
                         round(r["peak_context_pct"] or 0, 1),
                         (round(jv["avg"], 2) if jv else "—")])
        parts += ["", f"## {model}",
                  _table(["period", "runs", "out/prompt", "interrupts",
                          "edits w/o read", "loops", "ctx% (assumed)", "judge"], body)]
        spans = sorted({v for r in mrows
                        for v in (judge.get((model, r["period"])) or {})
                        .get("versions", [])})
        if len(spans) > 1:
            parts += ["", f"⚠ Judge column spans rubric v{', v'.join(spans)} — a "
                      "rubric bump moves the scale, so part of any trend here is "
                      "the rubric, not the model."]
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
                 ["**weighted tokens**", f'**{_n(int(cost.weighted(r["input_tokens"], r["output_tokens"], r["cache_read_tokens"], r["cache_creation_tokens"], r["cache_creation_1h_tokens"])))}**'],
                 ["active time", _ms(r["active_ms"])],
                 ["elapsed span", _ms(r["wall_clock_ms"])],
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
                 ["peak context tokens", _n(r["peak_context_tokens"])],
                 ["peak context % (assumed window)", g("peak_context_pct")],
                 ["compactions", _n(r["compact_count"])],
                 ["clears", _n(r["clear_count"])],
             ])]

    qs = conn.execute(
        f"""SELECT query_source, COUNT(*), SUM(input_tokens), SUM(output_tokens),
                   SUM({cost.WEIGHTED_SQL}) + SUM(total_tokens_agg)
            FROM turns WHERE run_id=? GROUP BY query_source""", (run_id,)).fetchall()
    if any(row[0] == "subagent" for row in qs):
        parts += ["", "## By query source",
                  _table(["source", "turns", "input", "output", "weighted"],
                         [[row[0], _n(row[1]), _n(row[2]), _n(row[3]),
                           _n(int(row[4] or 0))] for row in qs])]

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
        rv = verdict["rubric_version"]
        parts += ["", "## Judge verdict",
                  f'**{g_verdict(verdict, "overall_grade")}** (rubric v{rv})']
        if verdict["notes"]:
            parts += [verdict["notes"]]
        # Scoped to the rendered verdict's rubric version: a re-judge under a
        # newer rubric would otherwise list every dimension twice under one grade,
        # with nothing to say which pass a row came from.
        run_scores = conn.execute(
            "SELECT dimension, score, rationale FROM scores "
            "WHERE subject_type='run' AND subject_id=? AND rubric_version IS ? "
            "ORDER BY dimension", (run_id, rv)).fetchall()
        if run_scores:
            parts += ["", "### Agent behavior",
                      _table(["dimension", "score", "why"],
                             [[s[0], s[1], (s[2] or "")[:80]] for s in run_scores])]
        prompt_scores = conn.execute(
            """SELECT t.seq, s.dimension, s.score, substr(t.prompt_text,1,40)
               FROM scores s JOIN turns t ON t.turn_id = s.subject_id
               WHERE s.subject_type='prompt' AND t.run_id=? AND s.rubric_version IS ?
               ORDER BY t.seq, s.dimension""", (run_id, rv)).fetchall()
        if prompt_scores:
            parts += ["", "### Prompt quality",
                      _table(["turn", "dimension", "score", "prompt"],
                             [[p[0], p[1], p[2], (p[3] or "") + "…"]
                              for p in prompt_scores])]

        rec = evaluate.reconcile(conn, run_id, rv)
        if rec["passes_compared"] > 1:
            dis = rec["disagreements"]
            parts += ["", f"### Judge agreement ({rec['passes_compared']} passes "
                      f"under rubric v{rv})"]
            if dis:
                parts += [f"⚠ {len(dis)} dimension(s) disagree by >1 point:",
                          _table(["subject", "dimension", "scores"],
                                 [[d["subject_type"], d["dimension"],
                                   "/".join(str(s) for s in d["scores"])]
                                  for d in dis])]
            else:
                parts += ["✓ passes agree within 1 point on every dimension."]
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
        choices=["overview", "compare", "recommend", "antipatterns",
                 "degradation", "run"])
    parser.add_argument("run_id", nargs="?", default=None)
    parser.add_argument("--by", default="model",
                        choices=sorted(COMPARE_DIMENSIONS))
    parser.add_argument("--min", type=int, default=MIN_SAMPLES,
                        help="min successful runs per bucket to rank")
    parser.add_argument("--type", default=None, help="task_type for recommend")
    parser.add_argument("--size", default=None, help="size class for recommend")
    parser.add_argument("--since", default=None,
                        help="window for antipatterns, e.g. 30d / 12h")
    parser.add_argument("--period", default="month", choices=["month", "day"])
    args = parser.parse_args()

    conn = db.connect(args.data_dir)
    try:
        if args.view == "overview":
            print(render_overview(conn))
        elif args.view == "compare":
            print(render_compare(conn, args.by, args.min))
        elif args.view == "recommend":
            print(render_recommend(conn, args.type, args.size, args.by, args.min))
        elif args.view == "antipatterns":
            print(render_antipatterns(conn, args.since))
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
