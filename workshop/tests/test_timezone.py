"""
Regression tests for the UTC/local boundary.

MM5 stores Played.PlayDate in UTC; recap windows mean local calendar dates.
Mixing the two silently shifts plays across month edges and across days, which
is exactly the kind of bug that reappears the next time someone touches a date
path. These tests pin the invariants.

Written against stdlib unittest so they run with no extra dependencies:

    python -m unittest discover -s workshop/tests -t .

Every assertion here is timezone-independent (it derives its expectation from
the running machine's own rules) except test_dst_offset_differs, which needs a
zone that observes DST and skips itself elsewhere.
"""
from __future__ import annotations

import datetime
import unittest

from workshop.recap.compose import RecapWindow
from workshop.recap.db import (dt_to_ole, local_dt_to_ole, local_to_utc,
                               ole_to_dt, ole_to_local_dt, utc_to_local)
from workshop.recap.lastfm import _uts_to_naive_utc


def _utc_offset(dt_local: datetime.datetime) -> datetime.timedelta:
    """The machine's own UTC offset at a given local instant."""
    return dt_local.astimezone().utcoffset()


class RoundTrip(unittest.TestCase):

    def test_local_ole_local_is_exact(self):
        for dt in (datetime.datetime(2026, 1, 15, 3, 7, 42),
                   datetime.datetime(2026, 8, 15, 13, 45),
                   datetime.datetime(2026, 12, 31, 23, 59, 59)):
            with self.subTest(dt=dt):
                self.assertEqual(ole_to_local_dt(local_dt_to_ole(dt)), dt)

    def test_local_utc_helpers_are_inverses(self):
        dt = datetime.datetime(2026, 8, 15, 13, 45)
        self.assertEqual(utc_to_local(local_to_utc(dt)), dt)


class WindowBoundaries(unittest.TestCase):

    def test_window_bound_is_shifted_by_the_local_offset(self):
        """A local window boundary must not be compared to PlayDate as-is."""
        w = RecapWindow.from_month(2026, 8)
        shift_days = local_dt_to_ole(w.start) - dt_to_ole(w.start)
        expected = -_utc_offset(w.start).total_seconds() / 86400.0
        self.assertAlmostEqual(shift_days, expected, places=9)

    def test_mm5_and_lastfm_halves_agree(self):
        """
        The original bug: the MM5 half filtered the naive window value as UTC
        while the Last.fm half passed the same value through .timestamp(),
        which Python reads as local. The two sources filtered on instants
        hours apart. They must now denote the same instant.
        """
        for year, month in ((2026, 1), (2026, 8), (2026, 12)):
            with self.subTest(month=month):
                w = RecapWindow.from_month(year, month)
                for bound in (w.start, w.end):
                    mm5 = local_dt_to_ole(bound)
                    lastfm = dt_to_ole(
                        datetime.datetime.fromtimestamp(
                            bound.timestamp(), datetime.timezone.utc
                        ).replace(tzinfo=None)
                    )
                    self.assertAlmostEqual(mm5, lastfm, places=9)

    def test_window_is_half_open_and_contiguous(self):
        """One month's end must be the next month's start, with no gap or overlap."""
        aug_end = local_dt_to_ole(RecapWindow.from_month(2026, 8).end)
        sep_start = local_dt_to_ole(RecapWindow.from_month(2026, 9).start)
        self.assertAlmostEqual(aug_end, sep_start, places=9)

    @unittest.skipUnless(
        _utc_offset(datetime.datetime(2026, 1, 1))
        != _utc_offset(datetime.datetime(2026, 8, 1)),
        "local zone does not observe DST",
    )
    def test_dst_offset_differs(self):
        """The shift must be computed per instant, not as a fixed constant."""
        jan = local_dt_to_ole(datetime.datetime(2026, 1, 1)) - dt_to_ole(datetime.datetime(2026, 1, 1))
        aug = local_dt_to_ole(datetime.datetime(2026, 8, 1)) - dt_to_ole(datetime.datetime(2026, 8, 1))
        self.assertNotAlmostEqual(jan, aug, places=6)


class ScrobblePathStaysUTC(unittest.TestCase):
    """
    Last.fm scrobbles arrive as UTC unix timestamps and are stored alongside
    PlayDate, which is also UTC. That path must use the raw primitives —
    localizing it would double-shift the scrobble half of the merged stream.
    """

    def test_scrobble_ole_matches_the_utc_instant(self):
        uts = 1_772_000_000
        expected = datetime.datetime.fromtimestamp(
            uts, datetime.timezone.utc
        ).replace(tzinfo=None)
        self.assertEqual(_uts_to_naive_utc(uts), expected)
        self.assertAlmostEqual(dt_to_ole(_uts_to_naive_utc(uts)),
                               dt_to_ole(expected), places=9)

    def test_scrobble_and_window_land_on_one_axis(self):
        """
        A scrobble at a known local wall-clock time must fall inside the
        window for the month containing that local time.
        """
        local_moment = datetime.datetime(2026, 8, 15, 13, 45)
        uts = int(local_moment.timestamp())
        scrobble_ole = dt_to_ole(_uts_to_naive_utc(uts))
        w = RecapWindow.from_month(2026, 8)
        self.assertGreaterEqual(scrobble_ole, local_dt_to_ole(w.start))
        self.assertLess(scrobble_ole, local_dt_to_ole(w.end))

    def test_scrobble_just_before_local_month_end_stays_in_that_month(self):
        """The edge case the bug actually corrupted: late-evening local plays."""
        local_moment = datetime.datetime(2026, 8, 31, 23, 30)
        scrobble_ole = dt_to_ole(_uts_to_naive_utc(int(local_moment.timestamp())))
        aug = RecapWindow.from_month(2026, 8)
        sep = RecapWindow.from_month(2026, 9)
        self.assertLess(scrobble_ole, local_dt_to_ole(aug.end))
        self.assertLess(scrobble_ole, local_dt_to_ole(sep.start))


class DisplayConversion(unittest.TestCase):

    def test_playdate_displays_as_local_wall_clock(self):
        utc_moment = datetime.datetime(2026, 9, 3, 20, 37, 46)
        expected = utc_to_local(utc_moment)
        self.assertEqual(ole_to_local_dt(dt_to_ole(utc_moment)), expected)

    def test_raw_primitive_is_not_localized(self):
        """ole_to_dt must stay timezone-blind; callers choose."""
        utc_moment = datetime.datetime(2026, 9, 3, 20, 37, 46)
        self.assertEqual(ole_to_dt(dt_to_ole(utc_moment)), utc_moment)


if __name__ == "__main__":
    unittest.main()
