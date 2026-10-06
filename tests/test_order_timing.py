"""order_timing (course start/end/has_taken_place) and the Danish date/price filters. Offline."""
import datetime as dt
import os
import unittest

os.environ.setdefault("SANDBOX", "1")

import order_timing as ot  # noqa: E402
from dashboard import dkdate, dkmoney, dknum  # noqa: E402

TZ = ot.TZ


def at(y, m, d, hh=12, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=TZ)


class CourseStartTests(unittest.TestCase):
    def test_booking_start_wins_over_variant_date(self):
        row = {"variant_date": "3. december 2026"}
        booking = {"start_at": "2026-12-10T09:00:00+01:00"}
        self.assertEqual(ot.course_start(row, booking), at(2026, 12, 10, 9))

    def test_variant_date_is_parsed_when_unbooked(self):
        self.assertEqual(ot.course_start({"variant_date": "3. december 2026"}), at(2026, 12, 3, 0))

    def test_booking_json_on_the_row_is_used(self):
        row = {"variant_date": "1. januar 2026", "booking_json": '{"start_at": "2026-12-03T09:00:00+01:00"}'}
        self.assertEqual(ot.course_start(row), at(2026, 12, 3, 9))
        row["booking_json"] = {"start_at": "2026-12-04"}
        self.assertEqual(ot.course_start(row), at(2026, 12, 4, 0))

    def test_utc_offset_is_shown_in_copenhagen_time(self):
        self.assertEqual(ot.course_start({}, {"start_at": "2026-07-01T07:00:00+00:00"}), at(2026, 7, 1, 9))

    def test_naive_timestamp_means_copenhagen_wall_time(self):
        self.assertEqual(ot.course_start({}, {"start_at": "2026-12-03T09:00"}), at(2026, 12, 3, 9))

    def test_missing_or_unreadable_dates(self):
        self.assertIsNone(ot.course_start({}))
        self.assertIsNone(ot.course_start({"variant_date": ""}))
        self.assertIsNone(ot.course_start({"variant_date": "efter aftale"}))
        self.assertIsNone(ot.course_start(None))
        # an unreadable booking date falls back to the label
        self.assertEqual(ot.course_start({"variant_date": "3. december 2026"}, {"start_at": "snart"}),
                         at(2026, 12, 3, 0))


class CourseEndTests(unittest.TestCase):
    def test_end_with_time(self):
        self.assertEqual(ot.course_end({}, {"start_at": "2026-12-03T09:00:00+01:00",
                                            "end_at": "2026-12-03T16:00:00+01:00"}), at(2026, 12, 3, 16))

    def test_date_only_end_is_end_of_that_day(self):
        end = ot.course_end({}, {"start_at": "2026-12-03", "end_at": "2026-12-04"})
        self.assertEqual(end.date(), dt.date(2026, 12, 4))
        self.assertEqual((end.hour, end.minute), (23, 59))

    def test_no_end_known(self):
        self.assertIsNone(ot.course_end({"variant_date": "3. december 2026"}))
        self.assertIsNone(ot.course_end({}, {"start_at": "2026-12-03T09:00:00+01:00"}))

    def test_label_range_ends_on_its_last_day(self):
        end = ot.course_end({"variant_date": "3.-4. december 2026"})
        self.assertEqual(end.date(), dt.date(2026, 12, 4))


class HasTakenPlaceTests(unittest.TestCase):
    ROW = {"variant_date": "12. november 2026"}

    def test_future_course_has_not_taken_place(self):
        self.assertFalse(ot.has_taken_place(self.ROW, now=at(2026, 10, 6)))

    def test_day_of_the_course_is_not_yet_over(self):
        self.assertFalse(ot.has_taken_place(self.ROW, now=at(2026, 11, 12, 23, 30)))

    def test_day_after_it_is_over(self):
        self.assertTrue(ot.has_taken_place(self.ROW, now=at(2026, 11, 13, 0, 1)))

    def test_booked_start_time_does_not_end_the_day_early(self):
        booking = {"start_at": "2026-11-12T09:00:00+01:00"}
        self.assertFalse(ot.has_taken_place({}, booking, now=at(2026, 11, 12, 18)))
        self.assertTrue(ot.has_taken_place({}, booking, now=at(2026, 11, 13, 0, 1)))

    def test_explicit_end_time_decides(self):
        booking = {"start_at": "2026-11-12T09:00:00+01:00", "end_at": "2026-11-12T16:00:00+01:00"}
        self.assertFalse(ot.has_taken_place({}, booking, now=at(2026, 11, 12, 15)))
        self.assertTrue(ot.has_taken_place({}, booking, now=at(2026, 11, 12, 16, 1)))

    def test_multi_day_course_runs_until_its_last_day(self):
        booking = {"start_at": "2026-11-12", "end_at": "2026-11-14"}
        self.assertFalse(ot.has_taken_place({}, booking, now=at(2026, 11, 13, 12)))
        self.assertTrue(ot.has_taken_place({}, booking, now=at(2026, 11, 15, 0, 1)))

    def test_no_date_means_not_taken_place(self):
        self.assertFalse(ot.has_taken_place({}, now=at(2030, 1, 1)))

    def test_dst_boundary_uses_copenhagen_midnight(self):
        # 25 Oct 2026 is the CEST -> CET switch. 24 Oct 22:30 UTC = 25 Oct 00:30 CEST.
        row = {"variant_date": "24. oktober 2026"}
        before = dt.datetime(2026, 10, 24, 21, 30, tzinfo=dt.timezone.utc)   # 23:30 CEST on the 24th
        after = dt.datetime(2026, 10, 24, 22, 30, tzinfo=dt.timezone.utc)    # 00:30 CEST on the 25th
        self.assertFalse(ot.has_taken_place(row, now=before))
        self.assertTrue(ot.has_taken_place(row, now=after))
        # the day of the switch itself still ends at local midnight (CET, UTC+1)
        row = {"variant_date": "25. oktober 2026"}
        self.assertFalse(ot.has_taken_place(row, now=dt.datetime(2026, 10, 25, 22, 30, tzinfo=dt.timezone.utc)))
        self.assertTrue(ot.has_taken_place(row, now=dt.datetime(2026, 10, 25, 23, 30, tzinfo=dt.timezone.utc)))

    def test_default_now_is_patchable(self):
        original = ot.now
        try:
            ot.now = lambda: at(2026, 10, 6)
            self.assertFalse(ot.has_taken_place(self.ROW))
            ot.now = lambda: at(2026, 11, 13, 8)
            self.assertTrue(ot.has_taken_place(self.ROW))
        finally:
            ot.now = original


class CourseLabelTests(unittest.TestCase):
    def test_label_variants(self):
        self.assertEqual(ot.course_label({}, {"start_at": "2026-12-03T09:00:00+01:00"}), "3. december 2026 kl. 09.00")
        self.assertEqual(ot.course_label({"variant_date": "3. december 2026"}), "3. december 2026")
        self.assertEqual(ot.course_label({"variant_date": "efter aftale"}), "efter aftale")
        self.assertEqual(ot.course_label({}), "")


class DanishFormatTests(unittest.TestCase):
    def test_dkdate_legacy_output_is_unchanged(self):
        self.assertEqual(dkdate("2026-10-01T08:30:00.123456"), "01.10.2026")
        self.assertEqual(dkdate("2026-10-01T08:30:00", True), "01.10.2026 08:30")
        self.assertEqual(dkdate(dt.date(2026, 3, 4)), "04.03.2026")
        self.assertEqual(dkdate(None), "")
        self.assertEqual(dkdate("ikke en dato"), "ikke en dato")

    def test_dkdate_styles(self):
        iso = "2026-12-03T09:00:00+01:00"
        self.assertEqual(dkdate(iso, style="long", with_time=True), "3. december 2026 kl. 09.00")
        self.assertEqual(dkdate(iso, style="long"), "3. december 2026")
        self.assertEqual(dkdate(iso, style="short"), "03.12.2026")
        self.assertEqual(dkdate(iso, style="short", with_time=True), "03.12.2026 kl. 09.00")
        self.assertEqual(dkdate("3. december 2026", style="short"), "03.12.2026")
        self.assertEqual(dkdate("2026-12-03", style="long", with_time=True), "3. december 2026")
        self.assertEqual(dkdate(dt.date(2026, 3, 4), style="long"), "4. marts 2026")
        self.assertEqual(dkdate(None, style="long"), "")
        self.assertEqual(dkdate("snart", style="long"), "snart")

    def test_dkmoney(self):
        self.assertEqual(dkmoney(12500), "12.500 kr.")
        self.assertEqual(dkmoney(12500.0), "12.500 kr.")
        self.assertEqual(dkmoney("12500.00"), "12.500 kr.")
        self.assertEqual(dkmoney(12500.5), "12.500,50 kr.")
        self.assertEqual(dkmoney(0), "0 kr.")
        self.assertEqual(dkmoney("999"), "999 kr.")
        self.assertEqual(dkmoney(None), "")
        self.assertEqual(dkmoney(""), "")
        self.assertEqual(dkmoney("på forespørgsel"), "på forespørgsel")
        self.assertEqual(dknum(1234567), "1.234.567")


if __name__ == "__main__":
    unittest.main()
