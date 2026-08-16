"""Reporting for branch-token-tracker.

    btt report [--format markdown|csv|json] [--since 30d] [--project P]
    btt report <TICKET-ID>            # drilldown: the sessions behind one ticket

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

import db  # noqa: E402
import transcript  # noqa: E402

FORMATS = ("markdown", "csv", "json")


# ----- helpers --------------------------------------------------------------

def parse_window(since: str | None) -> tuple:
    """'30d' / '12h' -> (ISO cutoff, True).

    No window asked for is not an error -> (None, True). Anything unparsable ->
    (None, False), so the caller reports the window was ignored rather than
    quietly widening to all time under a header that says otherwise.
    """
    if not since:
        return None, True
    m = re.match(r"(\d+)\s*([dh])$", since.strip())
    if not m:
        return None, False
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
    return (datetime.now(timezone.utc) - delta).isoformat(), True


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


def _filters(cutoff: str | None, project: str | None) -> tuple:
    where, params = ["1=1"], []
    if cutoff:
        where.append("started_at >= ?")
        params.append(cutoff)
    if project:
        where.append("project = ?")
        params.append(project)
    return " AND ".join(where), tuple(params)


# ----- data -----------------------------------------------------------------

_SUMS = """COUNT(*) AS turns,
           COUNT(DISTINCT session_id) AS sessions,
           COALESCE(SUM(input_tokens),0) AS input_tokens,
           COALESCE(SUM(output_tokens),0) AS output_tokens,
           COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
           COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
           COALESCE(SUM(num_tool_calls),0) AS tool_calls,
           MIN(started_at) AS first_seen,
           MAX(ended_at) AS last_seen"""


def _row(r: sqlite3.Row, **extra) -> dict:
    # Identity columns (ticket / session / branch) lead, so a CSV opens with the
    # thing being measured in column A rather than trailing after the numbers.
    d = dict(extra)
    d.update({
        "turns": r["turns"], "sessions": r["sessions"],
        "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
        "cache_read_tokens": r["cache_read_tokens"],
        "cache_creation_tokens": r["cache_creation_tokens"],
        "tool_calls": r["tool_calls"],
        "first_seen": r["first_seen"], "last_seen": r["last_seen"],
    })
    d["total_tokens"] = (d["input_tokens"] + d["output_tokens"]
                         + d["cache_read_tokens"] + d["cache_creation_tokens"])
    d["wall_clock_ms"] = transcript.duration_ms(r["first_seen"], r["last_seen"])
    return d


def by_ticket(conn, cutoff: str | None = None, project: str | None = None) -> list:
    where, params = _filters(cutoff, project)
    rows = conn.execute(
        f"""SELECT ticket, GROUP_CONCAT(DISTINCT branch) AS branches, {_SUMS}
            FROM turns WHERE {where}
            GROUP BY ticket""", params).fetchall()
    out = [_row(r, ticket=r["ticket"],
                branches=sorted((r["branches"] or "").split(","))
                if r["branches"] else [])
           for r in rows]
    out.sort(key=lambda d: d["total_tokens"], reverse=True)
    return out


def for_ticket(conn, ticket: str, cutoff: str | None = None,
               project: str | None = None) -> list:
    """One row per (session, branch) that rolled up into this ticket."""
    where, params = _filters(cutoff, project)
    rows = conn.execute(
        f"""SELECT session_id, branch, project, {_SUMS}
            FROM turns WHERE {where} AND ticket = ?
            GROUP BY session_id, branch""", params + (ticket,)).fetchall()
    out = [_row(r, session_id=r["session_id"], branch=r["branch"],
                project=r["project"]) for r in rows]
    out.sort(key=lambda d: (d["first_seen"] or ""))
    return out


# ----- rendering ------------------------------------------------------------

def _note(since: str | None, ok: bool) -> str:
    if ok:
        return ""
    return (f"_Ignoring `--since {since}`: expected a window like `30d` or "
            "`12h`. Showing all time._\n\n")


def render_tickets(rows: list, since: str | None, ok: bool,
                   project: str | None) -> str:
    window = f" (last {since})" if since and ok else ""
    scope = f" · project {project}" if project else ""
    head = f"# Tokens by ticket{window}{scope}"
    if not rows:
        return (f"{head}\n\n{_note(since, ok)}No turns captured yet. The hooks "
                "record them as you work — check back after a session.")
    total = sum(r["total_tokens"] for r in rows)
    body = _table(
        ["ticket", "turns", "sessions", "input", "output", "cache read",
         "cache create", "total", "wall-clock", "last seen"],
        [[r["ticket"], _n(r["turns"]), _n(r["sessions"]), _n(r["input_tokens"]),
          _n(r["output_tokens"]), _n(r["cache_read_tokens"]),
          _n(r["cache_creation_tokens"]), _n(r["total_tokens"]),
          _ms(r["wall_clock_ms"]), (r["last_seen"] or "—")[:16]]
         for r in rows])
    parts = [head, ""]
    note = _note(since, ok).rstrip("\n")
    if note:
        parts += [note, ""]
    parts += [f"**{len(rows)} ticket(s) · {_n(total)} tokens total**", "", body]
    return "\n".join(parts)


def render_drilldown(ticket: str, rows: list, since: str | None, ok: bool) -> str:
    window = f" (last {since})" if since and ok else ""
    head = f"# {ticket}{window}"
    if not rows:
        return (f"{head}\n\n{_note(since, ok)}No turns recorded for this ticket. "
                "Ticket ids are matched exactly as extracted from the branch — "
                "run `btt report` to see the ones that exist.")
    total = sum(r["total_tokens"] for r in rows)
    branches = sorted({r["branch"] for r in rows if r["branch"]})
    body = _table(
        ["session", "branch", "project", "turns", "input", "output",
         "cache read", "total", "started"],
        [[(r["session_id"] or "—")[:8], r["branch"] or "—", r["project"] or "—",
          _n(r["turns"]), _n(r["input_tokens"]), _n(r["output_tokens"]),
          _n(r["cache_read_tokens"]), _n(r["total_tokens"]),
          (r["first_seen"] or "—")[:16]] for r in rows])
    lead = (f"**{len(rows)} session(s) · {_n(total)} tokens** · "
            f"branch(es): {', '.join(branches) or '—'}")
    return "\n".join([head, "", lead, "", body])


def render_csv(rows: list) -> str:
    if not rows:
        return ""
    cols = [k for k in rows[0] if k != "branches"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().rstrip("\n")


def render(rows: list, fmt: str, *, ticket: str | None, since: str | None,
           ok: bool, project: str | None) -> str:
    if fmt == "json":
        return json.dumps(rows, indent=2)
    if fmt == "csv":
        return render_csv(rows)
    if ticket:
        return render_drilldown(ticket, rows, since, ok)
    return render_tickets(rows, since, ok, project)


# ----- CLI ------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Token spend per branch ticket.")
    p.add_argument("ticket", nargs="?", default=None,
                   help="drill into one ticket id")
    p.add_argument("--format", default="markdown", choices=FORMATS)
    p.add_argument("--since", default=None, help="window, e.g. 30d / 12h")
    p.add_argument("--project", default=None, help="limit to one project")
    p.add_argument("--data-dir", default=None)
    args = p.parse_args()

    cutoff, ok = parse_window(args.since)
    db.init_db(args.data_dir)
    conn = db.connect(args.data_dir)
    try:
        rows = (for_ticket(conn, args.ticket, cutoff, args.project)
                if args.ticket else by_ticket(conn, cutoff, args.project))
        print(render(rows, args.format, ticket=args.ticket, since=args.since,
                     ok=ok, project=args.project))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
