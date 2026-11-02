"""Attach pull-request outcomes to the branches in the store.

    btt enrich [--since 30d] [--data-dir DIR]

For every (repo, branch) the hooks recorded, asks `gh` for the PRs opened from
that branch and stores their state, size, reviews and reopens in `prs`. Reports
then show cost next to what it shipped.

This is a command you run, never a hook. It needs the network and a logged-in
`gh`, and takes a second or more per branch; a hook must stay fast and offline.
Re-running it refreshes every PR it finds, so a PR merged since the last run
picks up its `merged_at`.

Only GitHub remotes are queried. A branch with no repo (captured before repos
were recorded, or outside any git remote) is skipped and counted as such.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import report  # noqa: E402

# Long-lived branches carry many people's PRs; none of them is "the" PR for the
# work done on it.
SKIP_BRANCHES = {"main", "master", "develop", "trunk"}

PR_FIELDS = ("number,state,createdAt,mergedAt,closedAt,additions,deletions,"
             "changedFiles,reviews,url")

GH_TIMEOUT_S = 30


def _gh(args: list) -> tuple:
    """(parsed JSON, error text). Never raises."""
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True,
                           timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip().splitlines()[-1:] or ["?"]
    try:
        return json.loads(p.stdout or "null"), None
    except json.JSONDecodeError as e:
        return None, f"unreadable gh output: {e}"


def _reopens(repo: str, number: int) -> int | None:
    """How many times the PR was reopened, from its issue events."""
    owner_name = repo.split("/", 1)[1]
    data, err = _gh(["api", "--paginate",
                     f"repos/{owner_name}/issues/{number}/events"])
    if err or not isinstance(data, list):
        return None
    return sum(1 for e in data
               if isinstance(e, dict) and e.get("event") == "reopened")


def branches(conn, since: str | None) -> list:
    where, params = report._filters(since)
    return [(r[0], r[1]) for r in conn.execute(
        f"""SELECT DISTINCT repo, branch FROM turns
            WHERE {where} AND branch IS NOT NULL AND repo IS NOT NULL
            ORDER BY repo, branch""", params)
            if r[1] not in SKIP_BRANCHES]


def enrich(conn, since: str | None = None, verbose: bool = True) -> dict:
    stats = {"branches": 0, "prs": 0, "skipped_not_github": 0, "errors": 0}
    unknown_repo = conn.execute(
        "SELECT COUNT(DISTINCT branch) FROM turns "
        "WHERE repo IS NULL AND branch IS NOT NULL").fetchone()[0]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for repo, branch in branches(conn, since):
        if not repo.startswith("github.com/"):
            stats["skipped_not_github"] += 1
            continue
        stats["branches"] += 1
        prs, err = _gh(["pr", "list", "--repo", repo, "--head", branch,
                        "--state", "all", "--limit", "20",
                        "--json", PR_FIELDS])
        if err:
            stats["errors"] += 1
            if verbose:
                print(f"  {repo} {branch}: {err}", file=sys.stderr)
            continue
        for pr in prs or []:
            reviews = pr.get("reviews") or []
            conn.execute(
                """INSERT INTO prs (repo, number, branch, state, created_at,
                       merged_at, closed_at, additions, deletions,
                       changed_files, reviews, changes_requested, reopens,
                       url, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(repo, number) DO UPDATE SET
                       branch = excluded.branch, state = excluded.state,
                       merged_at = excluded.merged_at,
                       closed_at = excluded.closed_at,
                       additions = excluded.additions,
                       deletions = excluded.deletions,
                       changed_files = excluded.changed_files,
                       reviews = excluded.reviews,
                       changes_requested = excluded.changes_requested,
                       reopens = COALESCE(excluded.reopens, reopens),
                       url = excluded.url, fetched_at = excluded.fetched_at""",
                (repo, pr["number"], branch, pr.get("state"),
                 pr.get("createdAt"), pr.get("mergedAt"), pr.get("closedAt"),
                 pr.get("additions"), pr.get("deletions"),
                 pr.get("changedFiles"), len(reviews),
                 sum(1 for r in reviews
                     if r.get("state") == "CHANGES_REQUESTED"),
                 _reopens(repo, pr["number"]), pr.get("url"), now))
            stats["prs"] += 1
            if verbose:
                print(f"  {repo} {branch}: #{pr['number']} {pr.get('state')}")
        conn.commit()
    stats["unknown_repo_branches"] = unknown_repo
    return stats


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description="Attach PR outcomes to branches.")
    p.add_argument("--since", default=None,
                   help="only branches with turns in this window (30d, a date)")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if not shutil.which("gh"):
        print("btt enrich: needs the GitHub CLI (`gh`) on PATH, logged in "
              "with `gh auth login`.", file=sys.stderr)
        return 1
    since, ok = report.parse_bound(args.since)
    if not ok:
        print(f"btt enrich: ignoring --since {args.since!r}: "
              f"{report.BOUND_HELP}", file=sys.stderr)

    db.init_db(args.data_dir)
    conn = db.connect(args.data_dir)
    try:
        s = enrich(conn, since, verbose=not args.quiet)
    finally:
        conn.close()
    print(f"[branch-token-tracker] enrich: {s['branches']} branch(es) "
          f"queried, {s['prs']} PR(s) stored, {s['errors']} error(s), "
          f"{s['skipped_not_github']} skipped (not GitHub), "
          f"{s['unknown_repo_branches']} branch(es) with no recorded repo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
