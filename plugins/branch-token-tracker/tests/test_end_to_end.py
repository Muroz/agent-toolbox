"""The hook path as Claude Code actually invokes it: `bin/btt ingest` with a
JSON payload on stdin, against a real git repo on a real branch.

Skipped when git is unavailable. Everything else in the suite exercises the
Python directly; this is the only test that proves the launcher, the argv, the
stdin contract and the git call line up.

    python3 -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BTT = ROOT / "bin" / "btt"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _transcript(path, turns):
    with open(path, "w") as fh:
        for i, uid in enumerate(turns):
            fh.write(json.dumps({
                "type": "user", "uuid": uid,
                "timestamp": f"2026-08-0{i + 1}T10:00:00Z",
                "message": {"role": "user", "content": "go"}}) + "\n")
            fh.write(json.dumps({
                "type": "assistant", "uuid": f"a-{uid}",
                "timestamp": f"2026-08-0{i + 1}T10:05:00Z",
                "message": {"role": "assistant", "id": f"m-{uid}",
                            "model": "claude-opus-5",
                            "content": [{"type": "text", "text": "done"}],
                            "usage": {"input_tokens": 10, "output_tokens": 100,
                                      "cache_read_input_tokens": 1000,
                                      "cache_creation_input_tokens": 50}}}) + "\n")


@unittest.skipUnless(HAS_GIT, "git not available")
class TestHookPath(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "f.txt").write_text("x")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "commit", "-qm", "init")
        self.transcript = self.tmp / "session.jsonl"
        # keep the developer's own config out of it
        self.env = {**os.environ, "PYENV_VERSION": "system"}
        self.env.pop("BTT_CONFIG", None)
        self.env.pop("BTT_PATTERN", None)
        self.env["BTT_PYTHON"] = sys.executable

    def _hook(self, event):
        payload = json.dumps({
            "session_id": "sess-1", "cwd": str(self.repo),
            "transcript_path": str(self.transcript)})
        p = subprocess.run(
            ["bash", str(BTT), "ingest", "--event", event,
             "--data-dir", str(self.data)],
            input=payload, text=True, capture_output=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def _report(self, *args):
        p = subprocess.run(
            ["bash", str(BTT), "report", "--data-dir", str(self.data), *args],
            text=True, capture_output=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_captures_against_the_branch_ticket_and_follows_a_switch(self):
        _git(self.repo, "checkout", "-qb", "feature/PROJ-412-add-login")
        _transcript(self.transcript, ["t1"])
        self._hook("SessionStart")
        self._hook("Stop")

        # switch branch mid-session; the next turn belongs to the new ticket
        _git(self.repo, "checkout", "-qb", "fix/ENG-77-null-deref")
        _transcript(self.transcript, ["t1", "t2"])
        self._hook("Stop")

        rows = json.loads(self._report("--format", "json"))
        by_ticket = {r["ticket"]: r for r in rows}
        self.assertEqual(set(by_ticket), {"PROJ-412", "ENG-77"})
        self.assertEqual(by_ticket["PROJ-412"]["turns"], 1)
        self.assertEqual(by_ticket["ENG-77"]["turns"], 1)
        self.assertEqual(by_ticket["PROJ-412"]["total_tokens"],
                         10 + 100 + 1000 + 50)
        self.assertEqual(by_ticket["ENG-77"]["branches"],
                         ["fix/ENG-77-null-deref"])

    def test_session_end_writes_current_json_and_a_summary(self):
        _git(self.repo, "checkout", "-qb", "feature/PROJ-9-x")
        _transcript(self.transcript, ["t1"])
        out = self._hook("SessionEnd")
        self.assertIn("PROJ-9", out)
        current = json.loads((db.data_dir(str(self.data)) / "current.json")
                             .read_text())
        self.assertEqual(current["ticket"], "PROJ-9")
        self.assertEqual(current["branch"], "feature/PROJ-9-x")
        self.assertEqual(current["session_tokens"], 10 + 100 + 1000 + 50)
        self.assertEqual(current["ticket_sessions"], 1)

    def test_session_start_greets_with_the_running_total(self):
        _git(self.repo, "checkout", "-qb", "feature/PROJ-5-x")
        _transcript(self.transcript, ["t1"])
        self.assertEqual(self._hook("SessionStart"), "")   # nothing yet
        self._hook("Stop")
        self.assertIn("PROJ-5 so far", self._hook("SessionStart"))

    def test_work_on_main_lands_under_the_fallback(self):
        _transcript(self.transcript, ["t1"])
        self._hook("Stop")
        rows = json.loads(self._report("--format", "json"))
        self.assertEqual([r["ticket"] for r in rows], ["unassigned"])

    def test_project_config_overrides_the_default_pattern(self):
        (self.repo / ".branch-tokens.json").write_text(json.dumps(
            {"patterns": [r"(?P<id>task_\d+)"], "fallback": "none",
             "uppercase": False}))
        _git(self.repo, "checkout", "-qb", "wip/task_77")
        _transcript(self.transcript, ["t1"])
        self._hook("Stop")
        rows = json.loads(self._report("--format", "json"))
        self.assertEqual([r["ticket"] for r in rows], ["task_77"])

    def test_drilldown_names_the_branch(self):
        _git(self.repo, "checkout", "-qb", "feature/PROJ-3-x")
        _transcript(self.transcript, ["t1"])
        self._hook("Stop")
        out = self._report("PROJ-3")
        self.assertIn("feature/PROJ-3-x", out)
        self.assertIn("# PROJ-3", out)

    def test_a_broken_transcript_path_does_not_fail_the_hook(self):
        self.transcript = self.tmp / "does-not-exist.jsonl"
        self._hook("Stop")   # asserts returncode 0


if __name__ == "__main__":
    unittest.main()
