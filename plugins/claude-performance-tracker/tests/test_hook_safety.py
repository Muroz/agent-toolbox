"""The one guarantee the hooks owe the user: they never block the session.

Dependency-free (stdlib unittest):

    python3 -m unittest discover -s tests

Background — the failure this file exists to prevent. Claude Code snapshots
hooks.json when a session *starts*, but the plugin's scripts are read live off
disk (this marketplace is a local directory, so an edit is instantly live for
every running session). Retiring an event therefore leaves already-open sessions
firing a hook this build no longer knows. argparse answered that with
`error: invalid choice` and exit 2 — and on UserPromptSubmit exit 2 is not an
error report, it is the documented signal to *block the prompt* and show the
user our stderr. Every prompt in that session was refused until it was killed.

So: an unknown event is a stale-config no-op, never an error, and no input
reaches an exit code the session can act on.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "cpt"
INGEST = ROOT / "scripts" / "ingest.py"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"

sys.path.insert(0, str(ROOT / "scripts"))
import ingest  # noqa: E402  (after sys.path injection)

# Every event name this plugin has ever registered in hooks.json, including the
# ones since retired. A session predating a release can be holding any of them.
HISTORICAL_EVENTS = (
    "SessionStart", "Stop", "SessionEnd", "UserPromptSubmit", "SubagentStop",
)

# Things a stale, corrupt, or future config could plausibly hand us.
HOSTILE_EVENTS = (
    "PreToolUse", "PostToolUse", "Notification", "PreCompact", "SessionResume",
    "", "--data-dir", "Stop; rm -rf /", "ß†∆",
)

PAYLOAD = json.dumps({"session_id": "hook-safety", "cwd": "/tmp"})


class HookExitCodeTest(unittest.TestCase):
    """Nothing we can be invoked with may produce a nonzero exit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def run_hook(self, *args, via="launcher", stdin=PAYLOAD, env=None):
        cmd = (["bash", str(LAUNCHER), "ingest"] if via == "launcher"
               else [sys.executable, str(INGEST)]) + list(args)
        environ = dict(os.environ)
        environ.pop("CPT_DEBUG", None)  # stderr on a blocking event is user-facing
        environ.update(env or {})
        return subprocess.run(cmd, input=stdin, env=environ,
                              capture_output=True, text=True, timeout=60)

    def assert_never_blocks(self, proc, label):
        # Exit 2 is the block signal specifically; any nonzero is a defect.
        self.assertEqual(proc.returncode, 0,
                         f"{label} exited {proc.returncode} — this blocks the "
                         f"user's session.\nstderr: {proc.stderr}")

    def test_retired_and_unknown_events_never_block(self):
        """The exact shape of the incident: a retired event, still being fired."""
        for via in ("launcher", "direct"):  # pre-0.3 hooks.json called python3 directly
            for event in HISTORICAL_EVENTS + HOSTILE_EVENTS:
                with self.subTest(via=via, event=event):
                    proc = self.run_hook("--event", event,
                                         "--data-dir", self.data_dir, via=via)
                    self.assert_never_blocks(proc, f"--event {event!r} via {via}")

    def test_retired_events_are_silent(self):
        """A retired event must not chatter onto a user-facing stderr either."""
        for event in ("UserPromptSubmit", "SubagentStop"):
            with self.subTest(event=event):
                proc = self.run_hook("--event", event, "--data-dir", self.data_dir)
                self.assertEqual(proc.stderr, "", f"{event} wrote to stderr")

    def test_malformed_invocations_never_block(self):
        for args in ([], ["--event"], ["--unknown-flag", "x"],
                     ["--event", "Stop", "--flag-from-a-future-build", "1"],
                     ["--event", "Stop", "--data-dir"], ["--help"]):
            with self.subTest(args=args):
                proc = self.run_hook(*args)
                self.assert_never_blocks(proc, f"args {args!r}")

    def test_malformed_stdin_never_blocks(self):
        for stdin in ("", "   ", "not json", "null", "[]", '{"session_id": null}'):
            with self.subTest(stdin=stdin):
                proc = self.run_hook("--event", "Stop", "--data-dir", self.data_dir,
                                     stdin=stdin)
                self.assert_never_blocks(proc, f"stdin {stdin!r}")

    def test_unusable_data_dir_never_blocks(self):
        for data_dir in ("/nonexistent/nope", "/dev/null/nope", str(INGEST)):
            with self.subTest(data_dir=data_dir):
                proc = self.run_hook("--event", "Stop", "--data-dir", data_dir)
                self.assert_never_blocks(proc, f"--data-dir {data_dir!r}")

    def test_launcher_survives_a_broken_interpreter(self):
        """A pyenv shim pointing at an uninstalled version must not block either."""
        proc = self.run_hook("--event", "Stop", "--data-dir", self.data_dir,
                             env={"CPT_PYTHON": "/nonexistent/python3"})
        self.assert_never_blocks(proc, "CPT_PYTHON=/nonexistent/python3")


class HookConfigTest(unittest.TestCase):
    """hooks.json and the script it dispatches to must agree, in both directions."""

    def setUp(self):
        self.hooks = json.loads(HOOKS_JSON.read_text())["hooks"]

    def test_every_registered_event_has_a_handler(self):
        """The inverse mistake: shipping a hook whose handler does not exist."""
        for event in self.hooks:
            self.assertIn(event, ingest.HANDLERS,
                          f"hooks.json registers {event} with no handler")

    def test_retired_events_are_not_registered(self):
        for event in ingest.RETIRED:
            self.assertNotIn(event, self.hooks,
                             f"{event} is marked RETIRED but still registered")

    def test_retired_names_are_never_reused_as_handlers(self):
        self.assertEqual(set(ingest.RETIRED) & set(ingest.HANDLERS), set())

    def test_every_historical_event_is_known_or_retired(self):
        """Guards the rule: retire an event, never delete it."""
        accounted = set(ingest.HANDLERS) | set(ingest.RETIRED)
        for event in HISTORICAL_EVENTS:
            self.assertIn(event, accounted,
                          f"{event} shipped once and is now unaccounted for — "
                          f"add it to RETIRED rather than dropping it")

    def test_hooks_only_invoke_the_ingest_subcommand(self):
        """Only `ingest` is hardened to exit 0; hooks must not call anything else."""
        for event, groups in self.hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertIn('/bin/cpt" ingest ', hook["command"],
                                  f"{event} invokes something other than "
                                  f"`cpt ingest`: {hook['command']}")


if __name__ == "__main__":
    unittest.main()
