"""Branch name -> ticket id, and the config precedence around it.

    python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import config  # noqa: E402


class _EnvSandbox(unittest.TestCase):
    """Isolate from the developer's own $BTT_* and ~/.claude config."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("BTT_CONFIG", "BTT_PATTERN")}
        self.tmp = tempfile.mkdtemp()
        # Point the user-level lookup at a path that does not exist, so a real
        # ~/.claude/branch-tokens.json on this machine can't sway the tests.
        self._user_cfg = config.USER_CONFIG
        config.USER_CONFIG = Path(self.tmp) / "no-such-user-config.json"

    def tearDown(self):
        config.USER_CONFIG = self._user_cfg
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestDefaultExtraction(_EnvSandbox):
    def test_default_patterns(self):
        cases = {
            "feature/PROJ-412-add-login": "PROJ-412",
            "ENG-1234": "ENG-1234",
            "bugfix/SC-7": "SC-7",
            "fix/#883-null-deref": "#883",
            "main": "unassigned",
            "": "unassigned",
        }
        for branch, expected in cases.items():
            with self.subTest(branch=branch):
                self.assertEqual(
                    config.ticket_for(branch, cwd=self.tmp), expected)

    def test_none_branch_is_not_an_error(self):
        # detached HEAD / not a repo — the work still gets counted
        self.assertEqual(config.ticket_for(None, cwd=self.tmp), "unassigned")

    def test_first_matching_pattern_wins(self):
        # a branch carrying both forms resolves to the first pattern's kind
        self.assertEqual(
            config.ticket_for("PROJ-412-closes-#883", cwd=self.tmp), "PROJ-412")

    def test_uppercase_normalizes_case(self):
        cfg = {"patterns": [r"(?P<id>[a-zA-Z]+-\d+)"], "fallback": "none",
               "uppercase": True}
        self.assertEqual(config.ticket_for("feature/proj-412", cfg), "PROJ-412")
        cfg["uppercase"] = False
        self.assertEqual(config.ticket_for("feature/proj-412", cfg), "proj-412")

    def test_pattern_without_named_group_uses_whole_match(self):
        cfg = {"patterns": [r"[A-Z]+-\d+"], "fallback": "none",
               "uppercase": True}
        self.assertEqual(config.ticket_for("feature/ABC-9", cfg), "ABC-9")


class TestConfigPrecedence(_EnvSandbox):
    def _write(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_project_config_beats_user_config(self):
        user = Path(self.tmp) / "user.json"
        self._write(user, {"fallback": "from-user"})
        config.USER_CONFIG = user
        proj = Path(self.tmp) / "repo"
        self._write(proj / config.CONFIG_NAME, {"fallback": "from-project"})
        self.assertEqual(config.load(str(proj))["fallback"], "from-project")

    def test_project_config_is_found_from_a_subdirectory(self):
        proj = Path(self.tmp) / "repo"
        self._write(proj / config.CONFIG_NAME, {"fallback": "from-project"})
        deep = proj / "src" / "nested"
        deep.mkdir(parents=True)
        self.assertEqual(config.load(str(deep))["fallback"], "from-project")

    def test_btt_config_env_beats_project_config(self):
        proj = Path(self.tmp) / "repo"
        self._write(proj / config.CONFIG_NAME, {"fallback": "from-project"})
        explicit = Path(self.tmp) / "explicit.json"
        self._write(explicit, {"fallback": "from-env"})
        os.environ["BTT_CONFIG"] = str(explicit)
        self.assertEqual(config.load(str(proj))["fallback"], "from-env")

    def test_btt_pattern_env_beats_everything(self):
        proj = Path(self.tmp) / "repo"
        self._write(proj / config.CONFIG_NAME,
                    {"patterns": [r"(?P<id>[A-Z]+-\d+)"]})
        os.environ["BTT_PATTERN"] = r"(?P<id>zzz\d+)"
        self.assertEqual(
            config.ticket_for("PROJ-412-zzz9", cwd=str(proj)), "ZZZ9")

    def test_pattern_string_is_a_synonym_for_patterns(self):
        proj = Path(self.tmp) / "repo"
        self._write(proj / config.CONFIG_NAME, {"pattern": r"(?P<id>T\d+)"})
        self.assertEqual(config.ticket_for("feature/T42", cwd=str(proj)), "T42")


class TestDegradesRatherThanRaises(_EnvSandbox):
    """This code runs inside a session hook — nothing here may throw."""

    def test_malformed_regex_falls_through_to_the_next_pattern(self):
        cfg = {"patterns": ["(unclosed", r"(?P<id>[A-Z]+-\d+)"],
               "fallback": "none", "uppercase": True}
        self.assertEqual(config.ticket_for("feature/ABC-1", cfg), "ABC-1")

    def test_all_patterns_malformed_yields_the_fallback(self):
        cfg = {"patterns": ["(unclosed", "[bad"], "fallback": "none",
               "uppercase": True}
        self.assertEqual(config.ticket_for("feature/ABC-1", cfg), "none")

    def test_unreadable_config_falls_back_to_defaults(self):
        bad = Path(self.tmp) / "repo" / config.CONFIG_NAME
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not json at all")
        cfg = config.load(str(bad.parent))
        self.assertEqual(cfg["patterns"], config.DEFAULTS["patterns"])

    def test_non_list_patterns_falls_back_to_defaults(self):
        proj = Path(self.tmp) / "repo"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / config.CONFIG_NAME).write_text(json.dumps({"patterns": "nope"}))
        self.assertEqual(
            config.load(str(proj))["patterns"], config.DEFAULTS["patterns"])

    def test_empty_fallback_is_replaced(self):
        proj = Path(self.tmp) / "repo"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / config.CONFIG_NAME).write_text(json.dumps({"fallback": ""}))
        self.assertEqual(config.load(str(proj))["fallback"], "unassigned")


if __name__ == "__main__":
    unittest.main()
