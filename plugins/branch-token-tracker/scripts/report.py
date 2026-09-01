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
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cost
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
           MIN(started_at) AS first_seen,
           MAX(ended_at) AS last_seen""".replace("{W}", cost.WEIGHTED_SQL)


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
        "first_seen": r["first_seen"], "last_seen": r["last_seen"],
    })
    d["total_tokens"] = (d["input_tokens"] + d["output_tokens"]
                         + d["cache_read_tokens"] + d["cache_creation_tokens"]
                         + d["total_tokens_agg"])
    d["wall_clock_ms"] = transcript.duration_ms(r["first_seen"], r["last_seen"])
    return d


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
    out = [_row(r, ticket=r["ticket"], model=r["model"],
                branches=sorted((r["branches"] or "").split(","))
                if r["branches"] else [])
           for r in rows]
    # Ranked on weighted (input-equivalent) tokens: a raw sum is ~95% cache
    # reads, which bill at a tenth of input, so it ranks tickets by how long
    # their sessions were rather than by what they cost.
    out.sort(key=lambda d: d["weighted_tokens"], reverse=True)
    return out


def for_ticket(conn, ticket: str, since: str | None = None,
               until: str | None = None, project: str | None = None) -> list:
    """One row per (session, branch) that rolled up into this ticket."""
    where, params = _filters(since, until, project)
    rows = conn.execute(
        f"""SELECT session_id, branch, project, {_SUMS}
            FROM turns WHERE {where} AND ticket = ?
            GROUP BY session_id, branch""", params + (ticket,)).fetchall()
    out = [_row(r, session_id=r["session_id"], branch=r["branch"],
                project=r["project"]) for r in rows]
    out.sort(key=lambda d: (d["first_seen"] or ""))
    return out


def _dominant_models(conn, bucket: str, where: str, params: tuple) -> dict:
    """The model that produced the most output in each bucket.

    Only used for the dollar estimate, which needs a per-model input price. A
    bucket that mixed models is priced on the one that did most of the talking
    and is approximate by nature — which is why the raw and weighted columns,
    both exact, sit next to it.
    """
    out: dict = {}
    for r in conn.execute(
            f"""SELECT {bucket} AS period, model, SUM(output_tokens) AS produced
                FROM turns WHERE {where} AND model IS NOT NULL
                GROUP BY period, model
                ORDER BY period, produced DESC""", params):
        out.setdefault(r["period"], r["model"])
    return out


def by_period(conn, period: str, since: str | None = None,
              until: str | None = None, project: str | None = None,
              ticket: str | None = None) -> list:
    """Spend per local calendar bucket, optionally for a single ticket."""
    bucket = PERIODS[period]
    where, params = _filters(since, until, project)
    if ticket:
        where += " AND ticket = ?"
        params = params + (ticket,)
    rows = conn.execute(
        f"""SELECT {bucket} AS period,
                   COUNT(DISTINCT ticket) AS tickets,
                   GROUP_CONCAT(DISTINCT ticket) AS ticket_list,
                   {_SUMS}
            FROM turns WHERE {where}
            GROUP BY period ORDER BY period""", params).fetchall()
    models = _dominant_models(conn, bucket, where, params)
    return [_row(r, period=r["period"], tickets=r["tickets"],
                 model=models.get(r["period"]),
                 ticket_list=sorted((r["ticket_list"] or "").split(","))
                 if r["ticket_list"] else [])
            for r in rows]


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
    """Dollar estimate for a row, when its model is priced and known."""
    amount = cost.usd(row["weighted_tokens"], row.get("model"))
    return "—" if amount is None else f"${amount:,.2f}"


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
        ["ticket", "turns", "sessions", "input", "output", "cache read",
         "cache create", "weighted", "est. USD", "raw total", "elapsed",
         "last seen"],
        [[r["ticket"], _n(r["turns"]), _n(r["sessions"]), _n(r["input_tokens"]),
          _n(r["output_tokens"]), _n(r["cache_read_tokens"]),
          _n(r["cache_creation_tokens"]), _n(r["weighted_tokens"]),
          _usd(r), _n(r["total_tokens"]),
          _ms(r["wall_clock_ms"]), (r["last_seen"] or "—")[:16]]
         for r in rows])
    parts = [head, ""]
    note = notes.rstrip("\n")
    if note:
        parts += [note, ""]
    parts += [f"**{len(rows)} ticket(s) · {_n(weighted)} weighted tokens "
              f"({_n(total)} raw)**", "", body, "",
              "_Weighted tokens are input-equivalent units (output 5x, cache "
              "write 1.25-2x, cache read 0.1x) — the raw total is dominated by "
              "cache reads and is not a cost. Elapsed is calendar span, not "
              "working time._"]
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
        ["session", "branch", "project", "turns", "input", "output",
         "cache read", "weighted", "raw total", "started"],
        [[(r["session_id"] or "—")[:8], r["branch"] or "—", r["project"] or "—",
          _n(r["turns"]), _n(r["input_tokens"]), _n(r["output_tokens"]),
          _n(r["cache_read_tokens"]), _n(r["weighted_tokens"]),
          _n(r["total_tokens"]), (r["first_seen"] or "—")[:16]] for r in rows])
    lead = (f"**{len(rows)} session(s) · {_n(weighted)} weighted tokens "
            f"({_n(total)} raw)** · branch(es): {', '.join(branches) or '—'}")
    return "\n".join([head, "", lead, "", body])


def render_periods(rows: list, period: str, window: str, notes: str,
                   ticket: str | None, project: str | None) -> str:
    scope = "".join([f" · {ticket}" if ticket else "",
                     f" · project {project}" if project else ""])
    head = f"# Tokens by {period}{scope}{window}"
    if not rows:
        return (f"{head}\n\n{notes}No turns in this range.")
    total = sum(r["total_tokens"] for r in rows)
    weighted = sum(r["weighted_tokens"] for r in rows)
    cols = [period, "turns", "sessions"]
    if not ticket:
        cols.append("tickets")
    cols += ["output", "cache read", "weighted", "est. USD", "raw total",
             "active"]
    body = _table(cols, [
        [r["period"] or "—", _n(r["turns"]), _n(r["sessions"])]
        + ([] if ticket else [_n(r["tickets"])])
        + [_n(r["output_tokens"]), _n(r["cache_read_tokens"]),
           _n(r["weighted_tokens"]), _usd(r), _n(r["total_tokens"]),
           _ms(r["wall_clock_ms"])]
        for r in rows])
    parts = [head, ""]
    note = notes.rstrip("\n")
    if note:
        parts += [note, ""]
    parts += [f"**{len(rows)} {period}(s) · {_n(weighted)} weighted tokens "
              f"({_n(total)} raw)**", "", body, "",
              f"_Buckets are local calendar {period}s"
              + (" (weeks start Monday)" if period == "week" else "")
              + "; stored timestamps are UTC. Weighted tokens are "
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


def render(rows: list, fmt: str, *, ticket: str | None, window: str,
           notes: str, project: str | None, period: str | None = None) -> str:
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
                             args.ticket)
        elif args.ticket:
            rows = for_ticket(conn, args.ticket, since, until, args.project)
        else:
            rows = by_ticket(conn, since, until, args.project)
        print(render(rows, args.format, ticket=args.ticket, window=window,
                     notes=notes, project=args.project, period=args.by))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
