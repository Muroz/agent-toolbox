"""Reporting for branch-token-tracker.

    btt report [--format markdown|csv|json] [--since S] [--until U]
               [--by day|week|month] [--project P]
    btt report <TICKET-ID>            # drilldown: the sessions behind one ticket

Time bounds accept a relative window (`30d`, `12h`) or an absolute LOCAL date
(`2026-08-01`) or local datetime (`2026-08-01T09:30`). Buckets for `--by` are
local calendar periods too. The store is UTC; a person asking for "August 1st"
means their own calendar day, and the two are not the same day.

Everything is a GROUP BY at read time over `turns` — nothing is pre-aggregated,
so a new report shape needs no migration and no re-ingest.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import cost  # noqa: E402
import db  # noqa: E402
import transcript  # noqa: E402

FORMATS = ("markdown", "csv", "json")


# ----- helpers --------------------------------------------------------------

_REL_RE = re.compile(r"(\d+)\s*([dh])$")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})$")
_DATETIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})$")

# Timestamps are stored exactly as the transcript writes them —
# `2026-09-01T00:39:50.362Z`. Bounds are emitted in that same shape so the
# comparison the query does is like-for-like rather than relying on `Z` and
# `+00:00` happening to sort the same way.
def _utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_bound(value: str | None, *, end: bool = False) -> tuple:
    """A `--since`/`--until` value -> (UTC cutoff, ok).

    Accepts a relative window (`30d`, `12h`), an absolute local date
    (`2026-08-01`), or a local datetime (`2026-08-01T09:30` / with a space).

    Absolute values are read in LOCAL time. The store is UTC, and the two
    disagree about which day a late-evening session belongs to — filtering a
    bare date as UTC would quietly pull in the previous evening's work.

    `--until 2026-08-15` covers the whole of the 15th: this returns the start of
    the 16th and the query is exclusive on the upper end. A bound that looked
    inclusive but dropped the last day's work would be worse than no flag.

    No bound asked for is not an error -> (None, True). Anything unparsable ->
    (None, False), so the caller says the bound was ignored rather than quietly
    widening to all time under a header that claims otherwise.
    """
    if not value:
        return None, True
    v = value.strip()

    m = _REL_RE.match(v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
        return _utc_z(datetime.now(timezone.utc) - delta), True

    m = _DATETIME_RE.match(v)
    if m:
        y, mo, d, hh, mm = (int(g) for g in m.groups())
        try:
            local = datetime(y, mo, d, hh, mm).astimezone()
        except ValueError:
            return None, False
        return _utc_z(local), True

    m = _DATE_RE.match(v)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            local = datetime(y, mo, d).astimezone()
        except ValueError:
            return None, False
        if end:
            # Make the named day inclusive by moving to the start of the next.
            local = (local + timedelta(days=1))
        return _utc_z(local), True

    return None, False


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


def _table(headers: list, rows: list) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _filters(since: str | None, until: str | None = None,
             project: str | None = None) -> tuple:
    where, params = ["1=1"], []
    if since:
        where.append("started_at >= ?")
        params.append(since)
    if until:
        # Exclusive: parse_bound already moved a bare end date to the next day.
        where.append("started_at < ?")
        params.append(until)
    if project:
        where.append("project = ?")
        params.append(project)
    return " AND ".join(where), tuple(params)


# Buckets are LOCAL calendar periods — the point of the flag is to line spend up
# with the days you remember working. Weeks start Monday.
PERIODS = {
    "day": "date(started_at,'localtime')",
    "week": "date(started_at,'localtime','weekday 0','-6 days')",
    "month": "strftime('%Y-%m', started_at,'localtime')",
}


# ----- data -----------------------------------------------------------------

_SUMS = """COUNT(*) AS turns,
           COUNT(DISTINCT session_id) AS sessions,
           COALESCE(SUM(input_tokens),0) AS input_tokens,
           COALESCE(SUM(output_tokens),0) AS output_tokens,
           COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
           COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
           COALESCE(SUM(total_tokens_agg),0) AS total_tokens_agg,
           COALESCE(SUM({W}),0) AS weighted_tokens,
           COALESCE(SUM(num_tool_calls),0) AS tool_calls,
           COALESCE(SUM(CASE WHEN query_source = 'main'
                             THEN active_ms END),0) AS active_ms,
           MIN(started_at) AS first_seen,
           MAX(ended_at) AS last_seen,
           SUM({USD}) AS est_usd,
           COALESCE(SUM(CASE WHEN {USD} IS NULL THEN {W} END),0)
               AS unpriced_weighted,
           COUNT(api_calls) AS detail_turns,
           SUM(api_calls) AS api_calls,
           SUM(mean_context_tokens * api_calls) AS context_sum,
           MAX(peak_context_tokens) AS peak_context_tokens,
           SUM(tool_result_chars) AS tool_result_chars,
           SUM(idle_gaps) AS idle_gaps,
           SUM(idle_ms) AS idle_ms,
           SUM(cold_cache_write_tokens) AS cold_cache_write_tokens,
           GROUP_CONCAT(DISTINCT repo) AS repos""".replace(
               "{W}", cost.WEIGHTED_SQL).replace("{USD}", cost.USD_SQL)


def _row(r: sqlite3.Row, **extra) -> dict:
    # Identity columns (ticket / session / branch) lead, so a CSV opens with the
    # thing being measured in column A rather than trailing after the numbers.
    d = dict(extra)
    d.update({
        "turns": r["turns"], "sessions": r["sessions"],
        "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
        "cache_read_tokens": r["cache_read_tokens"],
        "cache_creation_tokens": r["cache_creation_tokens"],
        "total_tokens_agg": r["total_tokens_agg"],
        "weighted_tokens": round(r["weighted_tokens"]),
        "tool_calls": r["tool_calls"],
        # Working time: each turn's idle gaps are capped at capture. Subagents
        # are left out because they run inside a main turn's span, so adding
        # theirs would count the same minutes twice.
        "active_ms": r["active_ms"],
        "first_seen": r["first_seen"], "last_seen": r["last_seen"],
        # Dollars price every turn at its own model's rates. Spend on a model
        # with no known price is left out of it and counted here instead, so a
        # partial figure never passes for a complete one.
        "est_usd": None if r["est_usd"] is None else round(r["est_usd"], 4),
        "unpriced_weighted": round(r["unpriced_weighted"] or 0),
        # Cost shape. Rows captured before these were recorded hold NULL, so
        # `detail_turns` says how many turns the figures below cover.
        "detail_turns": r["detail_turns"],
        "api_calls": r["api_calls"],
        "calls_per_turn": (round(r["api_calls"] / r["detail_turns"], 1)
                           if r["detail_turns"] and r["api_calls"] is not None
                           else None),
        "context_per_call": (round(r["context_sum"] / r["api_calls"])
                             if r["api_calls"] else None),
        "peak_context_tokens": r["peak_context_tokens"],
        "tool_result_chars": r["tool_result_chars"],
        "idle_gaps": r["idle_gaps"],
        "idle_ms": r["idle_ms"],
        "cold_cache_write_tokens": r["cold_cache_write_tokens"],
        "repos": sorted((r["repos"] or "").split(",")) if r["repos"] else [],
    })
    d["total_tokens"] = (d["input_tokens"] + d["output_tokens"]
                         + d["cache_read_tokens"] + d["cache_creation_tokens"]
                         + d["total_tokens_agg"])
    d["wall_clock_ms"] = transcript.duration_ms(r["first_seen"], r["last_seen"])
    return d


def _model_mix(conn, key: str, where: str, params: tuple) -> dict:
    """Each group's models by share of weighted tokens: `opus-5-5 70%, ...`.

    The old report named one dominant model per row, which hid that a ticket
    had been worked on two models priced differently.
    """
    totals: dict = {}
    for r in conn.execute(
            f"""SELECT {key} AS k, COALESCE(model, '?') AS model,
                       SUM({cost.WEIGHTED_SQL}) AS w
                FROM turns WHERE {where}
                GROUP BY k, model ORDER BY k, w DESC""", params):
        totals.setdefault(r["k"], []).append((r["model"], r["w"] or 0))
    out = {}
    for k, parts in totals.items():
        whole = sum(w for _, w in parts) or 1
        out[k] = ", ".join(
            f"{m.replace('claude-', '')} {round(100 * w / whole)}%"
            for m, w in parts if w / whole >= 0.01)
    return out


def _prs_for(conn, branches: list) -> list:
    """PRs `btt enrich` found for these branches. Empty if never enriched."""
    if not branches:
        return []
    marks = ",".join("?" * len(branches))
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM prs WHERE branch IN ({marks}) ORDER BY created_at",
            tuple(branches))]
    except sqlite3.Error:
        return []


def _pr_fields(prs: list) -> dict:
    """Outcome columns for a group of PRs. None when nothing was enriched."""
    if not prs:
        return {"prs": 0, "prs_merged": 0, "pr_additions": None,
                "pr_deletions": None, "pr_reviews": None,
                "pr_changes_requested": None, "pr_reopens": None}
    def total(k):
        vals = [p[k] for p in prs if p.get(k) is not None]
        return sum(vals) if vals else None
    return {"prs": len(prs),
            "prs_merged": sum(1 for p in prs if p.get("merged_at")),
            "pr_additions": total("additions"),
            "pr_deletions": total("deletions"),
            "pr_reviews": total("reviews"),
            "pr_changes_requested": total("changes_requested"),
            "pr_reopens": total("reopens")}


def by_ticket(conn, since: str | None = None, until: str | None = None,
              project: str | None = None) -> list:
    where, params = _filters(since, until, project)
    rows = conn.execute(
        f"""SELECT ticket, GROUP_CONCAT(DISTINCT branch) AS branches,
                   (SELECT model FROM turns m WHERE m.ticket = turns.ticket
                     AND m.model IS NOT NULL
                     GROUP BY m.model ORDER BY SUM(m.output_tokens) DESC
                     LIMIT 1) AS model,
                   {_SUMS}
            FROM turns WHERE {where}
            GROUP BY ticket""", params).fetchall()
    mix = _model_mix(conn, "ticket", where, params)
    out = []
    for r in rows:
        branches = (sorted((r["branches"] or "").split(","))
                    if r["branches"] else [])
        d = _row(r, ticket=r["ticket"], model=r["model"], branches=branches)
        d["models"] = mix.get(r["ticket"], "")
        d.update(_pr_fields(_prs_for(conn, branches)))
        out.append(d)
    # Ranked on weighted (input-equivalent) tokens: a raw sum is ~95% cache
    # reads, which bill at a tenth of input, so it ranks tickets by how long
    # their sessions were rather than by what they cost.
    out.sort(key=lambda d: d["weighted_tokens"], reverse=True)
    return out


def _session_facts(conn, session_id: str) -> dict:
    """What the `sessions` table knows about one session, NULLs if nothing."""
    keys = ("title", "first_prompt", "billing", "billing_source", "cc_version",
            "entrypoint")
    try:
        r = conn.execute(f"SELECT {', '.join(keys)} FROM sessions "
                         "WHERE session_id = ?", (session_id,)).fetchone()
    except sqlite3.Error:
        r = None
    return {k: (r[k] if r else None) for k in keys}


def for_ticket(conn, ticket: str, since: str | None = None,
               until: str | None = None, project: str | None = None) -> list:
    """One row per (session, branch) that rolled up into this ticket."""
    where, params = _filters(since, until, project)
    rows = conn.execute(
        f"""SELECT session_id, branch, project, {_SUMS}
            FROM turns WHERE {where} AND ticket = ?
            GROUP BY session_id, branch""", params + (ticket,)).fetchall()
    mix = _model_mix(conn, "session_id", f"{where} AND ticket = ?",
                     params + (ticket,))
    out = []
    for r in rows:
        d = _row(r, session_id=r["session_id"], branch=r["branch"],
                 project=r["project"])
        d["models"] = mix.get(r["session_id"], "")
        d.update(_session_facts(conn, r["session_id"]))
        out.append(d)
    out.sort(key=lambda d: (d["first_seen"] or ""))
    return out


def by_period(conn, period: str, since: str | None = None,
              until: str | None = None, project: str | None = None,
              ticket: str | None = None, split: bool = False) -> list:
    """Spend per local calendar bucket, optionally for a single ticket.

    `split` gives one row per (bucket, ticket) instead of one per bucket, with
    the PR events that fell in the bucket, so a ticket's spend can be lined up
    against when its PR opened and merged.
    """
    bucket = PERIODS[period]
    where, params = _filters(since, until, project)
    if ticket:
        where += " AND ticket = ?"
        params = params + (ticket,)
    if split:
        return _by_period_split(conn, period, bucket, where, params)
    rows = conn.execute(
        f"""SELECT {bucket} AS period,
                   COUNT(DISTINCT ticket) AS tickets,
                   GROUP_CONCAT(DISTINCT ticket) AS ticket_list,
                   {_SUMS}
            FROM turns WHERE {where}
            GROUP BY period ORDER BY period""", params).fetchall()
    mix = _model_mix(conn, bucket, where, params)
    return [_row(r, period=r["period"], tickets=r["tickets"],
                 models=mix.get(r["period"], ""),
                 ticket_list=sorted((r["ticket_list"] or "").split(","))
                 if r["ticket_list"] else [])
            for r in rows]


def _pr_events(conn, branches: list, period: str) -> dict:
    """{bucket: ["#12 opened", "#12 merged"]} for these branches' PRs."""
    events: dict = {}
    fmt = PERIODS[period].replace("started_at", "?")
    for pr in _prs_for(conn, branches):
        for label, ts in (("opened", pr.get("created_at")),
                          ("merged", pr.get("merged_at"))):
            if not ts:
                continue
            b = conn.execute(f"SELECT {fmt}", (ts,)).fetchone()[0]
            events.setdefault(b, []).append(f"#{pr['number']} {label}")
    return events


def _by_period_split(conn, period: str, bucket: str, where: str,
                     params: tuple) -> list:
    rows = conn.execute(
        f"""SELECT {bucket} AS period, ticket,
                   GROUP_CONCAT(DISTINCT branch) AS branches, {_SUMS}
            FROM turns WHERE {where}
            GROUP BY period, ticket ORDER BY period, ticket""",
        params).fetchall()
    events_by_ticket: dict = {}
    out = []
    for r in rows:
        if r["ticket"] not in events_by_ticket:
            branches = [b[0] for b in conn.execute(
                "SELECT DISTINCT branch FROM turns WHERE ticket = ? "
                "AND branch IS NOT NULL", (r["ticket"],))]
            events_by_ticket[r["ticket"]] = _pr_events(conn, branches, period)
        d = _row(r, period=r["period"], ticket=r["ticket"])
        d["pr_events"] = "; ".join(
            events_by_ticket[r["ticket"]].get(r["period"], []))
        out.append(d)
    return out


# ----- rendering ------------------------------------------------------------

BOUND_HELP = ("expected `30d`, `12h`, a local date `2026-08-01`, or a local "
              "datetime `2026-08-01T09:30`")


def _notes(bad: list) -> str:
    """One line per ignored bound. Silence would be the dangerous option: a
    header that says a window is applied while the numbers cover all time."""
    if not bad:
        return ""
    return "".join(
        f"_Ignoring `--{flag} {value}`: {BOUND_HELP}. That bound is not "
        f"applied._\n\n" for flag, value in bad)


def _window(since: str | None, until: str | None, bad: list) -> str:
    """How the applied range reads in the header."""
    bad_flags = {flag for flag, _ in bad}
    lo = since if since and "since" not in bad_flags else None
    hi = until if until and "until" not in bad_flags else None
    if lo and hi:
        return f" ({lo} → {hi})"
    if lo:
        return f" (since {lo})" if not _REL_RE.match(lo.strip()) else f" (last {lo})"
    if hi:
        return f" (until {hi})"
    return ""


def _usd(row: dict) -> str:
    """Dollar estimate, starred when part of the row's spend is unpriced."""
    amount = row.get("est_usd")
    if amount is None:
        return "—"
    return f"${amount:,.2f}" + ("*" if row.get("unpriced_weighted") else "")


def _k(n) -> str:
    """Compact token count for a narrow column: 182k, 1.4M."""
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1000)}k"
    return str(n)


def _shape(r: dict) -> str:
    """`3.2 calls/turn · 182k ctx` — why the turns cost what they did."""
    if not r.get("detail_turns"):
        return "—"
    return (f"{r['calls_per_turn']} calls/turn · "
            f"{_k(r['context_per_call'])} ctx")


def _idle(r: dict) -> str:
    """`4 gaps · 310k rewritten` — idle gaps and the cache writes after them."""
    if not r.get("detail_turns"):
        return "—"
    if not r.get("idle_gaps"):
        return "none"
    gaps = r["idle_gaps"]
    return (f"{gaps} gap{'' if gaps == 1 else 's'} · "
            f"{_k(r['cold_cache_write_tokens'])} rewritten")


def _prs(r: dict) -> str:
    if not r.get("prs"):
        return "—"
    return (f"{r['prs']} ({r['prs_merged']} merged) "
            f"+{r['pr_additions'] or 0}/-{r['pr_deletions'] or 0}")


_FOOTNOTE = (
    "_Weighted tokens are input-equivalent units (output 5x, cache write "
    "1.25-2x, cache read 0.1x) and rank work across models. Est. USD prices "
    "each turn at its own model's rates; a * means some spend was on a model "
    "with no known price. Cost shape is model calls per turn and context per "
    "call. Idle counts gaps of over 5 minutes between calls and the cache "
    "writes the next call made. Active caps each idle gap at 5 minutes. "
    "PRs come from `btt enrich`. A dash means not recorded for these turns._")


def render_tickets(rows: list, window: str, notes: str,
                   project: str | None) -> str:
    scope = f" · project {project}" if project else ""
    head = f"# Tokens by ticket{window}{scope}"
    if not rows:
        return (f"{head}\n\n{notes}No turns captured yet. The hooks "
                "record them as you work — check back after a session.")
    total = sum(r["total_tokens"] for r in rows)
    weighted = sum(r["weighted_tokens"] for r in rows)
    body = _table(
        ["ticket", "turns", "sessions", "output", "cache read",
         "cache create", "weighted", "est. USD", "models", "cost shape",
         "idle", "active", "PRs", "last seen"],
        [[r["ticket"], _n(r["turns"]), _n(r["sessions"]),
          _n(r["output_tokens"]), _n(r["cache_read_tokens"]),
          _n(r["cache_creation_tokens"]), _n(r["weighted_tokens"]),
          _usd(r), r.get("models") or "—", _shape(r), _idle(r),
          _ms(r["active_ms"]), _prs(r), (r["last_seen"] or "—")[:16]]
         for r in rows])
    parts = [head, ""]
    note = notes.rstrip("\n")
    if note:
        parts += [note, ""]
    parts += [f"**{len(rows)} ticket(s) · {_n(weighted)} weighted tokens "
              f"({_n(total)} raw)**", "", body, "",
              _FOOTNOTE]
    return "\n".join(parts)


def render_drilldown(ticket: str, rows: list, window: str, notes: str) -> str:
    head = f"# {ticket}{window}"
    if not rows:
        return (f"{head}\n\n{notes}No turns recorded for this ticket. "
                "Ticket ids are matched exactly as extracted from the branch — "
                "run `btt report` to see the ones that exist.")
    total = sum(r["total_tokens"] for r in rows)
    weighted = sum(r["weighted_tokens"] for r in rows)
    branches = sorted({r["branch"] for r in rows if r["branch"]})
    body = _table(
        ["session", "title", "branch", "repo", "turns", "output",
         "weighted", "est. USD", "models", "cost shape", "idle", "active",
         "started"],
        [[(r["session_id"] or "—")[:8], _cell(r.get("title")),
          r["branch"] or "—", ", ".join(r["repos"]) or r["project"] or "—",
          _n(r["turns"]), _n(r["output_tokens"]), _n(r["weighted_tokens"]),
          _usd(r), r.get("models") or "—", _shape(r), _idle(r),
          _ms(r["active_ms"]), (r["first_seen"] or "—")[:16]] for r in rows])
    lead = (f"**{len(rows)} session(s) · {_n(weighted)} weighted tokens "
            f"({_n(total)} raw)** · branch(es): {', '.join(branches) or '—'}")
    return "\n".join([head, "", lead, "", body, "", _FOOTNOTE])


def _cell(text) -> str:
    """Free text made safe for one markdown table cell."""
    if not text:
        return "—"
    return " ".join(str(text).split()).replace("|", "/")[:80]


def render_periods(rows: list, period: str, window: str, notes: str,
                   ticket: str | None, project: str | None) -> str:
    scope = "".join([f" · {ticket}" if ticket else "",
                     f" · project {project}" if project else ""])
    head = f"# Tokens by {period}{scope}{window}"
    if not rows:
        return (f"{head}\n\n{notes}No turns in this range.")
    total = sum(r["total_tokens"] for r in rows)
    weighted = sum(r["weighted_tokens"] for r in rows)
    split = bool(rows) and "pr_events" in rows[0]
    cols = [period] + (["ticket"] if split else []) + ["turns", "sessions"]
    if not ticket and not split:
        cols.append("tickets")
    cols += ["output", "cache read", "weighted", "est. USD", "raw total",
             "active"] + (["PR events"] if split else [])
    body = _table(cols, [
        [r["period"] or "—"] + ([r["ticket"]] if split else [])
        + [_n(r["turns"]), _n(r["sessions"])]
        + ([] if ticket or split else [_n(r["tickets"])])
        + [_n(r["output_tokens"]), _n(r["cache_read_tokens"]),
           _n(r["weighted_tokens"]), _usd(r), _n(r["total_tokens"]),
           _ms(r["active_ms"])]
        + ([r["pr_events"] or "—"] if split else [])
        for r in rows])
    parts = [head, ""]
    note = notes.rstrip("\n")
    if note:
        parts += [note, ""]
    parts += [f"**{len(rows)} {period}(s) · {_n(weighted)} weighted tokens "
              f"({_n(total)} raw)**", "", body, "",
              f"_Buckets are local calendar {period}s"
              + (" (weeks start Monday)" if period == "week" else "")
              + "; stored timestamps are UTC. Active is working time, with "
              "each idle gap capped at 5 minutes; subagent time is not "
              "added on top. Weighted tokens are "
              "input-equivalent units (output 5x, cache write 1.25-2x, cache "
              "read 0.1x) — the raw total is dominated by cache reads and is "
              "not a cost._"]
    return "\n".join(parts)


def render_csv(rows: list) -> str:
    if not rows:
        return ""
    cols = [k for k in rows[0] if k not in ("branches", "ticket_list")]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().rstrip("\n")


# Free text that can carry prompt content. Shown on screen, but an export
# leaves the machine, so it carries these only with `--include-text`.
TEXT_FIELDS = ("title", "first_prompt")


def plugin_version() -> str | None:
    try:
        manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" \
            / "plugin.json"
        return json.loads(manifest.read_text()).get("version")
    except (OSError, ValueError):
        return None


def export_meta(person: str | None, since: str | None,
                until: str | None) -> dict:
    """Columns that say whose data an export is and what it covers.

    Repeated on every row rather than put in a header, so exports from several
    people can be concatenated into one sheet and still be told apart.
    """
    return {"person": person or "", "btt_version": plugin_version() or "",
            "schema_version": db.SCHEMA_VERSION,
            "window_since": since or "", "window_until": until or "",
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")}


def export_rows(rows: list, meta: dict | None,
                include_text: bool = False) -> list:
    out = []
    for r in rows:
        d = {k: v for k, v in r.items()
             if include_text or k not in TEXT_FIELDS}
        if isinstance(d.get("repos"), list):
            d["repos"] = "; ".join(d["repos"])
        if meta:
            d.update(meta)
        out.append(d)
    return out


def render(rows: list, fmt: str, *, ticket: str | None, window: str,
           notes: str, project: str | None, period: str | None = None,
           meta: dict | None = None, include_text: bool = False) -> str:
    if fmt in ("json", "csv"):
        rows = export_rows(rows, meta, include_text)
    if fmt == "json":
        return json.dumps(rows, indent=2)
    if fmt == "csv":
        return render_csv(rows)
    if period:
        return render_periods(rows, period, window, notes, ticket, project)
    if ticket:
        return render_drilldown(ticket, rows, window, notes)
    return render_tickets(rows, window, notes, project)


# ----- CLI ------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Token spend per branch ticket.")
    p.add_argument("ticket", nargs="?", default=None,
                   help="drill into one ticket id")
    p.add_argument("--format", default="markdown", choices=FORMATS)
    p.add_argument("--since", default=None,
                   help="30d | 12h | 2026-08-01 | 2026-08-01T09:30 (local)")
    p.add_argument("--until", default=None,
                   help="same forms; a bare date includes that whole day")
    p.add_argument("--by", default=None, choices=sorted(PERIODS),
                   help="group by local calendar period instead of by ticket")
    p.add_argument("--project", default=None, help="limit to one project")
    p.add_argument("--split", default=None, choices=["ticket"],
                   help="with --by: one row per period per ticket")
    p.add_argument("--include-text", action="store_true",
                   help="put session titles and first prompts in csv/json "
                        "exports (they can hold sensitive text)")
    p.add_argument("--data-dir", default=None)
    args = p.parse_args()

    since, since_ok = parse_bound(args.since)
    until, until_ok = parse_bound(args.until, end=True)
    bad = ([("since", args.since)] if not since_ok else []) + \
          ([("until", args.until)] if not until_ok else [])
    notes = _notes(bad)
    window = _window(args.since, args.until, bad)

    db.init_db(args.data_dir)
    conn = db.connect(args.data_dir)
    try:
        if args.by:
            rows = by_period(conn, args.by, since, until, args.project,
                             args.ticket, split=args.split == "ticket")
        elif args.ticket:
            rows = for_ticket(conn, args.ticket, since, until, args.project)
        else:
            rows = by_ticket(conn, since, until, args.project)
        person = (os.environ.get("BTT_PERSON")
                  or config.load(os.getcwd()).get("person"))
        print(render(rows, args.format, ticket=args.ticket, window=window,
                     notes=notes, project=args.project, period=args.by,
                     meta=export_meta(person, since, until),
                     include_text=args.include_text))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
