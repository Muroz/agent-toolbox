"""Minimal accessors for the evaluation rubric.

The rubric lives in rubric.yaml (human-editable). We deliberately avoid a YAML
dependency at runtime — the subagent reads the YAML as text, and the only things
code needs are the version (to stamp scores) and the declared dimension keys
(for optional validation). Both are extracted with simple line parsing, so
adding a dimension never requires code or schema changes.
"""

from __future__ import annotations

import re
from pathlib import Path

RUBRIC_PATH = Path(__file__).with_name("rubric.yaml")


def version(path: Path | None = None) -> str:
    for line in (path or RUBRIC_PATH).read_text().splitlines():
        m = re.match(r"""\s*version:\s*["']?([^"'#\s]+)""", line)
        if m:
            return m.group(1).strip()
    return "0"


def dimension_keys(path: Path | None = None) -> set:
    keys = set()
    for line in (path or RUBRIC_PATH).read_text().splitlines():
        m = re.match(r"\s*-\s*key:\s*(\S+)", line)
        if m:
            keys.add(m.group(1))
    return keys


def anchors(path: Path | None = None) -> dict:
    """{dimension_key: {"0": text, "1": text, "2": text}} from the YAML.

    Only the subagent consumes anchors at runtime, so a typo (`anchor:`, wrong
    indent) would otherwise be invisible until a judge call silently ran without
    a calibrated scale. Parsing them here gives the test suite something to
    assert on. Same line-wise approach as the rest of this module — no YAML dep.
    """
    out: dict = {}
    key = None
    in_anchors = False
    for line in (path or RUBRIC_PATH).read_text().splitlines():
        m = re.match(r"\s*-\s*key:\s*(\S+)", line)
        if m:
            key, in_anchors = m.group(1), False
            out[key] = {}
            continue
        if key is None:
            continue
        if re.match(r"\s*anchors:\s*$", line):
            in_anchors = True
            continue
        if in_anchors:
            a = re.match(r"""\s*["']?(\d+)["']?:\s*(.+?)\s*$""", line)
            if a:
                out[key][a.group(1)] = a.group(2).strip("\"'")
            elif line.strip() and not line.startswith(" " * 6):
                in_anchors = False
    return out
