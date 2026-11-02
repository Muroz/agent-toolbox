"""Why a ticket cost what it did, and who and what an export covers.

Model calls, context size, tool output, idle gaps and the cache rewrites after
them; session facts; per-model pricing; PR outcomes; and the export rules that
keep prompt text on the machine unless asked for.

    python3 -m unittest discover -s tests
"""

import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cost  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402
import report  # noqa: E402
import transcript  # noqa: E402


def _usage(inp=10, out=100, cread=1000, ccreate=50):
    return {"input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cread,
            "cache_creation_input_tokens": ccreate}


def _prompt(uid, ts, text="do the thing"):
    return {"type": "user", "uuid": uid, "timestamp": ts, "version": "2.1.316",
            "cwd": "/nowhere", "entrypoint": "cli",
            "message": {"role": "user", "content": text}}


def _reply(uid, mid, ts, usage=None, model="claude-opus-5-5"):
    return {"type": "assistant", "uuid": uid, "timestamp": ts,
            "version": "2.1.316",
            "message": {"role": "assistant", "id": mid, "model": model,
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": usage or _usage()}}


def _tool_result(uid, ts, text):
    return {"type": "user", "uuid": uid, "timestamp": ts,
            "toolUseResult": {"stdout": text},
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x",
                 "content": [{"type": "text", "text": text}]}]}}


def _write(path, records):
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


class TestTurnShape(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(tempfile.mkdtemp()) / "s.jsonl")

    def test_counts_calls_and_context_per_call(self):
        _write(self.path, [
            _prompt("p1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z", _usage(10, 50, 1000, 0)),
            # the same message streamed again: still one call
            _reply("a2", "m1", "2026-08-01T10:00:06Z", _usage(10, 50, 1000, 0)),
            _reply("a3", "m2", "2026-08-01T10:00:10Z", _usage(10, 50, 3000, 0)),
        ])
        t = transcript.parse_turns(self.path)[0]
        self.assertEqual(t.api_calls, 2)
        self.assertEqual(t.peak_context_tokens, 3010)
        self.assertEqual(t.mean_context_tokens, 2010)

    def test_measures_tool_output_fed_back(self):
        _write(self.path, [
            _prompt("p1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
            _tool_result("r1", "2026-08-01T10:00:06Z", "x" * 5000),
            _reply("a2", "m2", "2026-08-01T10:00:10Z"),
        ])
        self.assertEqual(transcript.parse_turns(self.path)[0].tool_result_chars,
                         5000)

    def test_a_long_gap_and_the_cache_rewrite_after_it(self):
        # Turn 1 ends at 10:00:05. The user comes back 20 minutes later; the
        # first call of turn 2 rewrites 80k of cache, the second only 1k.
        _write(self.path, [
            _prompt("p1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
            _prompt("p2", "2026-08-01T10:20:05Z"),
            _reply("a2", "m2", "2026-08-01T10:20:10Z", _usage(ccreate=80000)),
            _reply("a3", "m3", "2026-08-01T10:20:20Z", _usage(ccreate=1000)),
        ])
        first, second = transcript.parse_turns(self.path)
        self.assertEqual((first.idle_gaps, first.cold_cache_write_tokens), (0, 0))
        self.assertEqual(second.idle_gaps, 1)
        self.assertEqual(second.idle_ms, 20 * 60 * 1000 + 5000)
        self.assertEqual(second.cold_cache_write_tokens, 80000)

    def test_session_title_first_prompt_and_version(self):
        _write(self.path, [
            _prompt("p1", "2026-08-01T10:00:00Z", "fix the login bug " * 30),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
            {"type": "ai-title", "aiTitle": "Old title"},
            {"type": "ai-title", "aiTitle": "Fix login"},
            _prompt("p2", "2026-08-01T10:01:00Z", "second"),
            _reply("a2", "m2", "2026-08-01T10:01:05Z"),
        ])
        turns, meta = transcript.parse_session(self.path)
        self.assertEqual(meta.title, "Fix login")
        self.assertTrue(meta.first_prompt.startswith("fix the login bug"))
        self.assertEqual(len(meta.first_prompt), transcript.FIRST_PROMPT_CHARS)
        self.assertEqual((meta.cc_version, meta.entrypoint), ("2.1.316", "cli"))
        self.assertEqual(turns[0].cc_version, "2.1.316")


class TestRepoAndBilling(unittest.TestCase):
    def test_remote_forms_normalize_and_drop_credentials(self):
        for url, want in [
            ("git@github.com:o/r.git", "github.com/o/r"),
            ("https://github.com/o/r.git\n", "github.com/o/r"),
            ("https://x:ghp_secret@github.com/o/r", "github.com/o/r"),
            ("ssh://git@gitlab.example.com:2222/g/sub/r.git",
             "gitlab.example.com/g/sub/r"),
            ("", None),
        ]:
            self.assertEqual(ingest.normalize_remote(url), want, url)

    def test_billing_config_beats_the_env_hint(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            self.assertEqual(ingest.billing_mode({}), ("api", "hint"))
            self.assertEqual(ingest.billing_mode({"billing": "subscription"}),
                             ("subscription", "config"))
        env = {k: "" for k in ingest.API_BILLING_ENV}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(ingest.billing_mode({}), ("subscription", "hint"))


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)

    def tearDown(self):
        self.conn.close()


class TestSessionsTable(_DB):
    def test_a_stated_billing_is_never_overwritten_by_a_guess(self):
        meta = transcript.SessionMeta(title="t")
        db.upsert_session(self.conn, "s1", meta, project="p", repo=None,
                          billing="subscription", billing_source="config",
                          now="1")
        db.upsert_session(self.conn, "s1", meta, project="p", repo=None,
                          billing="api", billing_source="hint", now="2")
        row = self.conn.execute(
            "SELECT billing, billing_source FROM sessions").fetchone()
        self.assertEqual(tuple(row), ("subscription", "config"))


class TestCapture(_DB):
    def test_the_hook_stores_turn_detail_and_session_facts(self):
        path = str(Path(self.tmp) / "s.jsonl")
        _write(path, [
            _prompt("p1", "2026-08-01T10:00:00Z", "first ask"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
            {"type": "ai-title", "aiTitle": "Some work"},
        ])
        ingest.capture(self.conn, "s1", path, project="r", branch="main",
                       ticket="unassigned", repo="github.com/o/r",
                       billing=("api", "hint"))
        # A second Stop after the turn grew: detail follows the latest parse.
        with open(path, "a") as fh:
            fh.write(json.dumps(_reply("a2", "m2", "2026-08-01T10:00:09Z")) + "\n")
        ingest.capture(self.conn, "s1", path, project="r", branch="main",
                       ticket="unassigned", repo="github.com/o/r",
                       billing=("api", "hint"))
        t = self.conn.execute(
            "SELECT api_calls, repo, cc_version FROM turns").fetchone()
        self.assertEqual(tuple(t), (2, "github.com/o/r", "2.1.316"))
        s = self.conn.execute(
            "SELECT title, first_prompt, repo, billing FROM sessions").fetchone()
        self.assertEqual(tuple(s), ("Some work", "first ask", "github.com/o/r",
                                    "api"))


class TestMigration(unittest.TestCase):
    def test_old_rows_read_as_unknown_not_zero(self):
        tmp = tempfile.mkdtemp()
        conn = sqlite3.connect(Path(tmp) / db.DB_NAME)
        conn.execute("""CREATE TABLE turns (turn_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL, project TEXT, branch TEXT,
                        ticket TEXT NOT NULL, model TEXT, started_at TEXT,
                        ended_at TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                        num_tool_calls INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("INSERT INTO turns (turn_id, session_id, ticket, model, "
                     "output_tokens) VALUES ('t', 's', 'P-1', "
                     "'claude-opus-5', 10)")
        conn.commit()
        conn.close()
        db.init_db(tmp)
        conn = db.connect(tmp)
        r = report.by_ticket(conn)[0]
        self.assertEqual(r["detail_turns"], 0)
        self.assertIsNone(r["api_calls"])
        self.assertIsNone(r["calls_per_turn"])
        self.assertEqual(report._shape(r), "—")
        conn.close()


def _turn(conn, tid, *, ticket="P-1", branch="feat/P-1", model="claude-opus-5-5",
          started="2026-08-01T10:00:00Z", **cols):
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0}
    base.update(cols)
    names = ", ".join(base)
    conn.execute(
        f"""INSERT INTO turns (turn_id, session_id, branch, ticket, model,
                               started_at, ended_at, {names})
            VALUES (?, 's1', ?, ?, ?, ?, ?, {', '.join('?' * len(base))})""",
        (tid, branch, ticket, model, started, started, *base.values()))
    conn.commit()


class TestPricing(_DB):
    def test_each_turn_is_priced_at_its_own_models_rates(self):
        # 1M cache reads: $0.20 on Opus 5.5, $0.50 on Opus 5. A single
        # weighted-token rate would price both at 0.1x input.
        _turn(self.conn, "a", model="claude-opus-5-5",
              cache_read_tokens=1_000_000)
        _turn(self.conn, "b", model="claude-opus-5[1m]",
              cache_read_tokens=1_000_000)
        r = report.by_ticket(self.conn)[0]
        self.assertAlmostEqual(r["est_usd"], 0.70)
        self.assertEqual(r["unpriced_weighted"], 0)
        self.assertIn("opus-5-5 50%", r["models"])

    def test_unpriced_spend_is_flagged_not_hidden(self):
        _turn(self.conn, "a", output_tokens=1000)
        _turn(self.conn, "b", model="claude-unknown-9", output_tokens=1000)
        r = report.by_ticket(self.conn)[0]
        self.assertEqual(r["unpriced_weighted"], 5000)
        self.assertTrue(report._usd(r).endswith("*"))

    def test_sql_and_python_pricing_agree(self):
        split = dict(input_tokens=1000, output_tokens=2000,
                     cache_read_tokens=1_000_000, cache_creation_tokens=10_000,
                     cache_creation_1h_tokens=4000)
        _turn(self.conn, "a", **split)
        sql = self.conn.execute(f"SELECT {cost.USD_SQL} FROM turns").fetchone()[0]
        self.assertAlmostEqual(sql, cost.usd_split("claude-opus-5-5", **split))
        self.assertAlmostEqual(sql, 0.306)


class TestPRsAndExports(_DB):
    def setUp(self):
        super().setUp()
        _turn(self.conn, "a", output_tokens=100,
              started="2026-08-01T10:00:00Z")
        _turn(self.conn, "b", output_tokens=100,
              started="2026-08-03T10:00:00Z")
        self.conn.execute(
            """INSERT INTO prs (repo, number, branch, state, created_at,
                   merged_at, additions, deletions, reviews, changes_requested,
                   reopens, fetched_at)
               VALUES ('github.com/o/r', 7, 'feat/P-1', 'MERGED',
                       '2026-08-01T12:00:00Z', '2026-08-03T12:00:00Z',
                       120, 30, 3, 1, 0, 'now')""")
        db.upsert_session(self.conn, "s1",
                          transcript.SessionMeta(title="Secret plan",
                                                 first_prompt="token=abc"),
                          project="r", repo=None, billing=None,
                          billing_source=None, now="1")
        self.conn.commit()

    def test_ticket_rows_carry_their_pr_outcomes(self):
        r = report.by_ticket(self.conn)[0]
        self.assertEqual((r["prs"], r["prs_merged"], r["pr_additions"],
                          r["pr_changes_requested"]), (1, 1, 120, 1))

    def test_split_by_ticket_marks_pr_events_on_their_day(self):
        rows = report.by_period(self.conn, "day", split=True)
        self.assertEqual([(r["ticket"], r["pr_events"]) for r in rows],
                         [("P-1", "#7 opened"), ("P-1", "#7 merged")])

    def test_exports_leave_prompt_text_out_unless_asked(self):
        rows = report.for_ticket(self.conn, "P-1")
        meta = report.export_meta("alias-a", None, None)
        out = report.render(rows, "csv", ticket="P-1", window="", notes="",
                            project=None, meta=meta)
        row = next(csv.DictReader(io.StringIO(out)))
        self.assertNotIn("title", row)
        self.assertNotIn("Secret plan", out)
        self.assertEqual(row["person"], "alias-a")
        self.assertEqual(out.splitlines()[0].split(",")[0], "session_id")

        out = report.render(rows, "json", ticket="P-1", window="", notes="",
                            project=None, meta=meta, include_text=True)
        self.assertEqual(json.loads(out)[0]["first_prompt"], "token=abc")

    def test_on_screen_drilldown_shows_the_title(self):
        rows = report.for_ticket(self.conn, "P-1")
        out = report.render(rows, "markdown", ticket="P-1", window="",
                            notes="", project=None)
        self.assertIn("Secret plan", out)


if __name__ == "__main__":
    unittest.main()
