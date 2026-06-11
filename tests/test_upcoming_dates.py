"""TL-02: Stop surfacing expired course dates.

Variant dates that parse strictly before today must never be shown as
bookable/upcoming. Unparseable strings are kept (ukendt er ikke overstået).
When every parseable date is past: dates == [] + no_upcoming_dates == True so
the model can say 'ingen kommende datoer — kontakt udbyderen'.

Offline: no OpenAI, no MySQL. Fake products injected at the module seams
(load_augmented_products / catalog.get_product), same pattern as
tests/test_price_consistency.py.
"""
import datetime
import json
import unittest
from unittest.mock import patch

import app1.tools as tools
from app1 import parse_danish_date
from app1.tools import (
    _filter_upcoming_date_strings,
    _upcoming_dates,
    _extract_compact_fields,
    _catalog_compact_fields,
    _execute_catalog_compare_products,
    _execute_compare_courses,
    _execute_get_course_details,
    _execute_add_to_calendar,
    set_search_context,
)

_MONTH_NAMES = {
    1: "januar", 2: "februar", 3: "marts", 4: "april", 5: "maj", 6: "juni",
    7: "juli", 8: "august", 9: "september", 10: "oktober", 11: "november", 12: "december",
}


def _dk(d):
    """Format a date the way the catalog does: '12. august 2026'."""
    return f"{d.day}. {_MONTH_NAMES[d.month]} {d.year}"


TODAY = datetime.date.today()
PAST = _dk(TODAY - datetime.timedelta(days=40))
PAST_2 = _dk(TODAY - datetime.timedelta(days=400))
FUTURE = _dk(TODAY + datetime.timedelta(days=40))
TODAY_STR = _dk(TODAY)
UNPARSEABLE = "Løbende opstart"


def _raw_product(handle="ledelse-i-praksis", title="Ledelse i praksis", dates=None):
    """Legacy/raw product shape (augmented products, Shopify-style variants)."""
    if dates is None:
        dates = [PAST, FUTURE, UNPARSEABLE]
    return {
        "handle": handle,
        "title": title,
        "vendor": "Mannaz",
        "product_type": "Kursus",
        "ai_summary": "Et kursus i praktisk ledelse for nye ledere.",
        "tags": [],
        "variants": [
            {"price": "10000.00", "option1": "København", "option2": d}
            for d in dates
        ],
    }


def _catalog_product(handle="ledelse-i-praksis", title="Ledelse i praksis", dates=None):
    """Normalized catalog_service product shape."""
    if dates is None:
        dates = [PAST, FUTURE, UNPARSEABLE]
    return {
        "handle": handle,
        "title": title,
        "vendor": "Mannaz",
        "vendor_slug": "mannaz",
        "price_label": "10.000 kr",
        "price_min": 10000.0,
        "price_max": 10000.0,
        "format": "Fysisk",
        "summary": "Et kursus i praktisk ledelse for nye ledere.",
        "locations": ["København"],
        "dates": list(dates),
        "categories": ["Ledelse"],
        "category_slugs": ["ledelse"],
        "image_url": None,
        "source": "catalog",
    }


class FilterHelperTests(unittest.TestCase):
    def test_mixed_keeps_only_future_and_unparseable(self):
        kept, no_upcoming = _filter_upcoming_date_strings([PAST, FUTURE, UNPARSEABLE])
        self.assertEqual(kept, [FUTURE, UNPARSEABLE])
        self.assertFalse(no_upcoming)

    def test_all_past_sets_no_upcoming_flag(self):
        kept, no_upcoming = _filter_upcoming_date_strings([PAST, PAST_2])
        self.assertEqual(kept, [])
        self.assertTrue(no_upcoming)

    def test_today_is_kept(self):
        kept, no_upcoming = _filter_upcoming_date_strings([TODAY_STR])
        self.assertEqual(kept, [TODAY_STR])
        self.assertFalse(no_upcoming)

    def test_unparseable_only_is_kept_without_flag(self):
        kept, no_upcoming = _filter_upcoming_date_strings([UNPARSEABLE, "Efter aftale"])
        self.assertEqual(kept, [UNPARSEABLE, "Efter aftale"])
        self.assertFalse(no_upcoming)

    def test_empty_input_has_no_flag(self):
        kept, no_upcoming = _filter_upcoming_date_strings([])
        self.assertEqual(kept, [])
        self.assertFalse(no_upcoming)
        kept, no_upcoming = _filter_upcoming_date_strings(None)
        self.assertEqual(kept, [])
        self.assertFalse(no_upcoming)

    def test_upcoming_dates_reads_variant_option2(self):
        variants = [
            {"option2": PAST}, {"option2": FUTURE}, {"option2": ""}, {"option1": "København"},
        ]
        kept, no_upcoming = _upcoming_dates(variants)
        self.assertEqual(kept, [FUTURE])
        self.assertFalse(no_upcoming)

    def test_env_gate_restores_old_unfiltered_behavior(self):
        with patch.dict(tools._os.environ, {"AI_FILTER_PAST_DATES": "0"}):
            kept, no_upcoming = _filter_upcoming_date_strings([PAST, FUTURE])
            self.assertEqual(kept, [PAST, FUTURE])
            self.assertFalse(no_upcoming)


class CompactFieldsTests(unittest.TestCase):
    def tearDown(self):
        set_search_context()

    def test_extract_compact_fields_drops_past_dates(self):
        fields = _extract_compact_fields(_raw_product())
        self.assertEqual(fields["dates"], [FUTURE, UNPARSEABLE])
        self.assertNotIn("no_upcoming_dates", fields)

    def test_extract_compact_fields_all_past(self):
        fields = _extract_compact_fields(_raw_product(dates=[PAST, PAST_2]))
        self.assertEqual(fields["dates"], [])
        self.assertTrue(fields["no_upcoming_dates"])

    def test_catalog_compact_fields_drops_past_dates(self):
        fields = _catalog_compact_fields(_catalog_product())
        self.assertEqual(fields["dates"], [FUTURE, UNPARSEABLE])
        self.assertNotIn("no_upcoming_dates", fields)

    def test_catalog_compact_fields_all_past(self):
        fields = _catalog_compact_fields(_catalog_product(dates=[PAST, PAST_2]))
        self.assertEqual(fields["dates"], [])
        self.assertTrue(fields["no_upcoming_dates"])


class ExecutorTests(unittest.TestCase):
    """No tool payload may contain a parseable date earlier than today."""

    def tearDown(self):
        set_search_context()

    def _assert_no_past_dates(self, date_strings):
        for raw in date_strings:
            dt = parse_danish_date(str(raw))
            if dt is not None:
                self.assertGreaterEqual(
                    dt, TODAY, f"Udløbet dato '{raw}' blev vist som kommende"
                )

    def test_get_course_details_filters_past_dates(self):
        with patch.object(tools, "load_augmented_products", return_value=[_raw_product()]):
            payload = json.loads(_execute_get_course_details({"handle": "ledelse-i-praksis"}))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["upcoming_dates"], [FUTURE, UNPARSEABLE])
        self._assert_no_past_dates(payload["upcoming_dates"])
        self.assertNotIn("no_upcoming_dates", payload)

    def test_get_course_details_all_past(self):
        with patch.object(tools, "load_augmented_products", return_value=[_raw_product(dates=[PAST, PAST_2])]):
            payload = json.loads(_execute_get_course_details({"handle": "ledelse-i-praksis"}))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["upcoming_dates"], [])
        self.assertTrue(payload["no_upcoming_dates"])

    def test_get_course_details_stale_catalog_data_is_flagged(self):
        old_ts = (TODAY - datetime.timedelta(days=200)).isoformat() + "T12:00:00+01:00"
        product = _raw_product()
        product["updated_at"] = old_ts
        with patch.object(tools, "load_augmented_products", return_value=[product]):
            payload = json.loads(_execute_get_course_details({"handle": "ledelse-i-praksis"}))
        self.assertTrue(payload["date_data_stale"])

    def test_get_course_details_fresh_catalog_data_not_flagged(self):
        fresh_ts = (TODAY - datetime.timedelta(days=5)).isoformat() + "T12:00:00+01:00"
        product = _raw_product()
        product["updated_at"] = fresh_ts
        with patch.object(tools, "load_augmented_products", return_value=[product]):
            payload = json.loads(_execute_get_course_details({"handle": "ledelse-i-praksis"}))
        self.assertNotIn("date_data_stale", payload)

    def test_compare_courses_filters_past_dates(self):
        fakes = [
            _raw_product(handle="a", title="Kursus A"),
            _raw_product(handle="b", title="Kursus B", dates=[PAST, PAST_2]),
        ]
        with patch.object(tools, "load_augmented_products", return_value=fakes):
            payload = json.loads(_execute_compare_courses({"handles": ["a", "b"]}))
        self.assertEqual(payload["status"], "success")
        by_handle = {c["handle"]: c for c in payload["comparison"]}
        self.assertEqual(by_handle["a"]["upcoming_dates"], [FUTURE, UNPARSEABLE])
        self.assertNotIn("no_upcoming_dates", by_handle["a"])
        self.assertEqual(by_handle["b"]["upcoming_dates"], [])
        self.assertTrue(by_handle["b"]["no_upcoming_dates"])
        for comp in payload["comparison"]:
            self._assert_no_past_dates(comp["upcoming_dates"])

    def test_catalog_compare_products_filters_past_dates(self):
        products = {
            "a": _catalog_product(handle="a", title="Kursus A"),
            "b": _catalog_product(handle="b", title="Kursus B", dates=[PAST, PAST_2]),
        }
        with patch.object(tools.catalog, "get_product", side_effect=lambda h: products.get(h)):
            payload = json.loads(_execute_catalog_compare_products({"handles": ["a", "b"]}))
        self.assertEqual(payload["status"], "success")
        by_handle = {c["handle"]: c for c in payload["comparison"]}
        self.assertEqual(by_handle["a"]["dates"], [FUTURE, UNPARSEABLE])
        self.assertNotIn("no_upcoming_dates", by_handle["a"])
        self.assertEqual(by_handle["b"]["dates"], [])
        self.assertTrue(by_handle["b"]["no_upcoming_dates"])
        for comp in payload["comparison"]:
            self._assert_no_past_dates(comp["dates"])


class AddToCalendarFallbackTests(unittest.TestCase):
    """The variant-date fallback must suggest a coming date — never an expired one."""

    def test_fallback_picks_first_upcoming_date(self):
        with patch.object(tools, "load_augmented_products", return_value=[_raw_product()]):
            payload = json.loads(_execute_add_to_calendar({"handle": "ledelse-i-praksis"}, ""))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["event"]["date"], FUTURE)

    def test_fallback_all_past_asks_for_date_instead(self):
        with patch.object(tools, "load_augmented_products", return_value=[_raw_product(dates=[PAST, PAST_2])]):
            payload = json.loads(_execute_add_to_calendar({"handle": "ledelse-i-praksis"}, ""))
        self.assertEqual(payload["status"], "needs_info")

    def test_explicit_date_argument_is_respected(self):
        with patch.object(tools, "load_augmented_products", return_value=[_raw_product()]):
            payload = json.loads(
                _execute_add_to_calendar({"handle": "ledelse-i-praksis", "date": PAST}, "")
            )
        # Eksplicit brugervalgt dato røres ikke — kun fallback'en filtreres.
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["event"]["date"], PAST)


if __name__ == "__main__":
    unittest.main()
