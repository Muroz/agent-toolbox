"""`--since` / `--until` bounds and `--by` period buckets.

The store is UTC; a person asking for "August 1st" means their own calendar
day. Those are not the same day — in the timezone this was written in they are
three hours apart, so a late-evening session lands on the following UTC date.
Every absolute bound and every bucket is therefore interpreted in LOCAL time,
and these tests are written to hold in whatever timezone they run in.

    python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import report  # noqa: E402

_N = [0]


def _local(y, m, d, hh=0, mm=0) -> datetime:
    """A local wall-clock moment, timezone-aware."""
    return datetime(y, m, d, hh, mm).astimezone()


def _stamp(dt: datetime) -> str:
    """Stored exactly as the transcript writes it: UTC, milliseconds, `Z`."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _turn(conn, *, ticket, at: datetime, session="s1", out=100):
    _N[0] += 1
    conn.execute(
        """INSERT INTO turns
           (turn_id, session_id, project, branch, ticket, model, started_at,
            ended_at, input_tokens, output_tokens, cache_read_tokens,
            cache_creation_tokens, num_tool_calls)
           VALUES (?,?,'repo','b',?,'claude-opus-5',?,?,10,?,1000,50,3)""",
        (f"t{_N[0]}", session, ticket, _stamp(at),
         _stamp(at + timedelta(minutes=5)), out))
    conn.commit()


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)
        self.addCleanup(self.conn.close)


class ParseBoundTest(unittest.TestCase):

    def test_relative_windows_still_work(self):
        for v in ("30d", "12h", "1 d"):
            with self.subTest(v=v):
                iso, ok = report.parse_bound(v)
                self.assertTrue(ok)
                self.assertTrue(iso.endswith("Z"))

    def test_absolute_date_is_read_as_a_local_day(self):
        iso, ok = report.parse_bound("2026-08-01")
        self.assertTrue(ok)
        self.assertEqual(iso, _stamp(_local(2026, 8, 1)))

    def test_absolute_datetime_is_read_as_local(self):
        for v in ("2026-08-01T09:30", "2026-08-01 09:30"):
            with self.subTest(v=v):
                iso, ok = report.parse_bound(v)
                self.assertTrue(ok)
                self.assertEqual(iso, _stamp(_local(2026, 8, 1, 9, 30)))

    def test_bare_end_date_covers_that_whole_day(self):
        """`--until 2026-08-15` must include the 15th, not stop at its start."""
        iso, ok = report.parse_bound("2026-08-15", end=True)
        self.assertTrue(ok)
        self.assertEqual(iso, _stamp(_local(2026, 8, 16)))

    def test_end_datetime_is_exact_not_rounded_up(self):
        iso, _ = report.parse_bound("2026-08-15T17:00", end=True)
        self.assertEqual(iso, _stamp(_local(2026, 8, 15, 17, 0)))

    def test_bounds_are_emitted_in_the_stored_format(self):
        """Comparison is a string compare, so the shapes must match."""
        iso, _ = report.parse_bound("2026-08-01")
        self.assertRegex(iso, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_garbage_is_reported_not_guessed(self):
        for v in ("2w", "nonsense", "2026-13-01", "2026-02-30", "01/08/2026"):
            with self.subTest(v=v):
                self.assertEqual(report.parse_bound(v), (None, False))

    def test_absent_bound_is_not_an_error(self):
        self.assertEqual(report.parse_bound(None), (None, True))


class RangeFilterTest(_DB):

    def setUp(self):
        super().setUp()
        # one turn per local day, 1st..5th of August, at midday local
        for day in range(1, 6):
            _turn(self.conn, ticket=f"D-{day}", at=_local(2026, 8, day, 12))

    def _tickets(self, since=None, until=None):
        lo, ok1 = report.parse_bound(since)
        hi, ok2 = report.parse_bound(until, end=True)
        self.assertTrue(ok1 and ok2)
        return sorted(r["ticket"] for r in report.by_ticket(self.conn, lo, hi))

    def test_since_is_inclusive_of_its_day(self):
        self.assertEqual(self._tickets(since="2026-08-03"),
                         ["D-3", "D-4", "D-5"])

    def test_until_is_inclusive_of_its_day(self):
        self.assertEqual(self._tickets(until="2026-08-03"),
                         ["D-1", "D-2", "D-3"])

    def test_closed_range(self):
        self.assertEqual(self._tickets(since="2026-08-02", until="2026-08-04"),
                         ["D-2", "D-3", "D-4"])

    def test_single_day_range(self):
        self.assertEqual(self._tickets(since="2026-08-03", until="2026-08-03"),
                         ["D-3"])

    def test_the_two_halves_partition_the_whole(self):
        """No turn may fall in both halves, and none may fall in neither."""
        before = self._tickets(until="2026-08-02")
        after = self._tickets(since="2026-08-03")
        self.assertEqual(set(before) & set(after), set())
        self.assertEqual(sorted(before + after), self._tickets())

    def test_a_late_evening_turn_belongs_to_its_local_day(self):
        """The UTC date rolls over before the local one does (or after it).

        This is the case a naive UTC filter gets wrong.
        """
        _turn(self.conn, ticket="LATE", at=_local(2026, 8, 6, 23, 30))
        self.assertIn("LATE", self._tickets(since="2026-08-06",
                                            until="2026-08-06"))
        self.assertNotIn("LATE", self._tickets(since="2026-08-07"))


class PeriodBucketTest(_DB):

    def test_day_buckets_are_local_days(self):
        _turn(self.conn, ticket="A", at=_local(2026, 8, 1, 23, 30))
        _turn(self.conn, ticket="B", at=_local(2026, 8, 2, 0, 30))
        rows = report.by_period(self.conn, "day")
        self.assertEqual([r["period"] for r in rows], ["2026-08-01", "2026-08-02"])

    def test_week_buckets_start_on_monday(self):
        # 2026-08-31 is a Monday; 2026-09-06 Sunday; 2026-09-07 the next Monday.
        for d in (_local(2026, 8, 31, 12), _local(2026, 9, 6, 12)):
            _turn(self.conn, ticket="W", at=d)
        _turn(self.conn, ticket="W", at=_local(2026, 9, 7, 12))
        rows = report.by_period(self.conn, "week")
        self.assertEqual([r["period"] for r in rows],
                         ["2026-08-31", "2026-09-07"])
        self.assertEqual(rows[0]["turns"], 2)
        self.assertEqual(rows[1]["turns"], 1)

    def test_month_buckets(self):
        _turn(self.conn, ticket="M", at=_local(2026, 8, 31, 12))
        _turn(self.conn, ticket="M", at=_local(2026, 9, 1, 12))
        rows = report.by_period(self.conn, "month")
        self.assertEqual([r["period"] for r in rows], ["2026-08", "2026-09"])

    def test_period_counts_distinct_tickets(self):
        _turn(self.conn, ticket="A", at=_local(2026, 8, 1, 9))
        _turn(self.conn, ticket="B", at=_local(2026, 8, 1, 10))
        _turn(self.conn, ticket="A", at=_local(2026, 8, 1, 11))
        row = report.by_period(self.conn, "day")[0]
        self.assertEqual(row["turns"], 3)
        self.assertEqual(row["tickets"], 2)
        self.assertEqual(row["ticket_list"], ["A", "B"])

    def test_period_can_be_scoped_to_one_ticket(self):
        _turn(self.conn, ticket="A", at=_local(2026, 8, 1, 9))
        _turn(self.conn, ticket="B", at=_local(2026, 8, 1, 10))
        rows = report.by_period(self.conn, "day", ticket="A")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turns"], 1)

    def test_period_respects_the_range(self):
        for day in (1, 2, 3):
            _turn(self.conn, ticket="X", at=_local(2026, 8, day, 12))
        lo, _ = report.parse_bound("2026-08-02")
        hi, _ = report.parse_bound("2026-08-02", end=True)
        rows = report.by_period(self.conn, "day", lo, hi)
        self.assertEqual([r["period"] for r in rows], ["2026-08-02"])

    def test_every_period_is_a_valid_choice(self):
        for period in report.PERIODS:
            with self.subTest(period=period):
                self.assertEqual(report.by_period(self.conn, period), [])


class RenderingTest(_DB):

    def setUp(self):
        super().setUp()
        _turn(self.conn, ticket="A", at=_local(2026, 8, 1, 12))

    def test_header_states_the_applied_range(self):
        out = report.render_periods(
            report.by_period(self.conn, "day"), "day",
            report._window("2026-08-01", "2026-08-02", []), "", None, None)
        self.assertIn("2026-08-01 → 2026-08-02", out)

    def test_an_ignored_bound_is_never_shown_as_applied(self):
        bad = [("until", "nope")]
        window = report._window("2026-08-01", "nope", bad)
        self.assertIn("since 2026-08-01", window)
        self.assertNotIn("nope", window)
        self.assertIn("Ignoring `--until nope`", report._notes(bad))

    def test_both_bounds_bad_leaves_no_window_claim(self):
        bad = [("since", "x"), ("until", "y")]
        self.assertEqual(report._window("x", "y", bad), "")

    def test_period_table_names_the_bucket_column(self):
        out = report.render_periods(report.by_period(self.conn, "week"),
                                    "week", "", "", None, None)
        self.assertIn("| week |", out)
        self.assertIn("weeks start Monday", out)


if __name__ == "__main__":
    unittest.main()
