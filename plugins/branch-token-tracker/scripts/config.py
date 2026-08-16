"""Branch name -> task-tracker id, and the config that governs it.

The whole customization surface of this plugin is here. Everything else takes a
ticket string and counts tokens against it.

Config lookup, first hit wins:

  1. $BTT_CONFIG                     — explicit file path
  2. .branch-tokens.json             — walking up from cwd to the filesystem root
  3. ~/.claude/branch-tokens.json    — user-level default
  4. DEFAULTS below

A single pattern can also be forced with $BTT_PATTERN, which beats all of the
above — that is the one-off escape hatch, not the thing to configure per repo.

    {
      "patterns": ["(?P<id>[A-Z][A-Z0-9]+-\\\\d+)", "(?P<id>#\\\\d+)"],
      "fallback": "unassigned",
      "uppercase": true
    }

Patterns are tried in order against the full branch name; the first that matches
wins, taking the `id` named group when present and the whole match otherwise.
Nothing here may raise: a malformed regex or an unreadable config degrades to the
fallback, because this code runs inside a session hook.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

CONFIG_NAME = ".branch-tokens.json"
USER_CONFIG = Path.home() / ".claude" / "branch-tokens.json"

DEFAULTS = {
    # JIRA / Linear / Shortcut style: PROJ-412, ENG-1234, SC-7.
    # Then GitHub / GitLab issue refs: #883 — the '#' is kept in the id so a
    # ticket never collides with a bare number from some other scheme.
    "patterns": [r"(?P<id>[A-Z][A-Z0-9]+-\d+)", r"(?P<id>#\d+)"],
    "fallback": "unassigned",
    "uppercase": True,
}


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _find_project_config(start: str | None) -> dict | None:
    """Nearest `.branch-tokens.json` at or above `start`."""
    if not start:
        return None
    try:
        here = Path(start).resolve()
    except OSError:
        return None
    for d in [here, *here.parents]:
        found = _read_json(d / CONFIG_NAME)
        if found is not None:
            return found
    return None


def load(cwd: str | None = None) -> dict:
    """Resolve the effective config. Never raises."""
    cfg = dict(DEFAULTS)

    explicit = os.environ.get("BTT_CONFIG")
    layered = None
    if explicit:
        layered = _read_json(Path(explicit))
    if layered is None:
        layered = _find_project_config(cwd or os.getcwd())
    if layered is None:
        layered = _read_json(USER_CONFIG)
    if layered:
        cfg.update(layered)

    # `pattern` (string) is accepted as a synonym for a one-element `patterns`.
    if isinstance(cfg.get("pattern"), str):
        cfg["patterns"] = [cfg["pattern"]]
    env_pattern = os.environ.get("BTT_PATTERN")
    if env_pattern:
        cfg["patterns"] = [env_pattern]

    if not isinstance(cfg.get("patterns"), list):
        cfg["patterns"] = list(DEFAULTS["patterns"])
    cfg["patterns"] = [p for p in cfg["patterns"] if isinstance(p, str)]
    if not isinstance(cfg.get("fallback"), str) or not cfg["fallback"]:
        cfg["fallback"] = DEFAULTS["fallback"]
    cfg["uppercase"] = bool(cfg.get("uppercase", True))
    return cfg


def ticket_for(branch: str | None, cfg: dict | None = None,
               cwd: str | None = None) -> str:
    """Extract the task-tracker id from a branch name.

    A branch that matches nothing is not an error — plenty of real work happens
    on `main` — so it lands under the configured fallback and still gets counted.
    """
    cfg = cfg if cfg is not None else load(cwd)
    if not branch:
        return cfg["fallback"]
    for pattern in cfg["patterns"]:
        try:
            m = re.search(pattern, branch)
        except re.error:
            continue  # a bad regex must not take the session down
        if not m:
            continue
        found = m.groupdict().get("id") or m.group(0)
        if found:
            return found.upper() if cfg["uppercase"] else found
    return cfg["fallback"]
