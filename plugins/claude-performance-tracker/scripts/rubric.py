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
