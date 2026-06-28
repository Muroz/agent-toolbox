"""Reporting for claude-performance-tracker.

All numbers are computed here, at read time, from the raw `runs` / `turns` /
`scores` tables — nothing is pre-aggregated in storage. That keeps the data
reusable for any future report shape or exporter (JSON/CSV/HTML/dashboard).

Views (tracked as issues):
  * overview      — totals, per-project, per-model, time-series
  * compare       — bucketed {task_type x size} cost-per-SUCCESS, with a
                    small-sample guard ("insufficient data, n=N") instead of a
                    false winner
  * degradation   — efficiency/quality trend over time, split by model
  * run <id>      — full scorecard + judge verdict for one run

This is a SCAFFOLD.
"""

from __future__ import annotations

import argparse

import db

MIN_SAMPLES = 5  # below this, comparison reports "insufficient data" rather than ranking.


def overview(args) -> None:
    raise NotImplementedError  # TODO(issue)


def compare(args) -> None:
    raise NotImplementedError  # TODO(issue)


def degradation(args) -> None:
    raise NotImplementedError  # TODO(issue)


def run_scorecard(args) -> None:
    raise NotImplementedError  # TODO(issue)


def main() -> int:
    parser = argparse.ArgumentParser(description="Report on tracked usage.")
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="view", required=True)
    sub.add_parser("overview")
    sub.add_parser("compare")
    sub.add_parser("degradation")
    run_p = sub.add_parser("run")
    run_p.add_argument("run_id")

    args = parser.parse_args()
    {"overview": overview, "compare": compare,
     "degradation": degradation, "run": run_scorecard}[args.view](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
