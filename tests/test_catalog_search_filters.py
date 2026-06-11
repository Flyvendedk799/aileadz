"""TL-03: Carry hard filters into catalog_search's RAG fallback + honest
progressive relaxation in filter_courses.

The semantic RAG fallback inside _execute_catalog_search ignored price/location/
format constraints — 'ledelseskursus under 5.000 kr i Aarhus' could return
over-budget courses from the wrong city. Now the RAG results are post-filtered
with the same predicates filter_courses uses; an emptied set falls back to the
unfiltered results with filters_relaxed: true + relaxed_filters so the model
narrates what was loosened instead of dead-ending. _execute_filter_courses
relaxes progressively (date range → price +25% → location) and reports
relaxed_filters the same way.

Offline: no OpenAI, no MySQL. Fake products are injected at the module seams
(semantic_search_courses_detailed / catalog.search_products /
load_augmented_products), same pattern as tests/test_price_consistency.py.
Env-gated by AI_SEARCH_HARD_FILTERS (default on); =0 restores the old behavior.
"""
import datetime
import json
import os
import unittest
from unittest.mock import patch

import app1.tools as tools
from app1.tools import (
    _apply_hard_filters,
    _product_passes_hard_filters,
    _execute_catalog_search,
    _execute_filter_courses,
    set_search_context,
)

_MONTH_NAMES = {
    1: "januar", 2: "februar", 3: "marts", 4: "april", 5: "maj", 6: "juni",
    7: "juli", 8: "august", 9: "september", 10: "oktober", 11: "november", 12: "december",
}


def _dk(d):
    """Format a date the way the catalog does: '12. august 2026'."""
    return f"{d.day}. {_MONTH_NAMES[d.month]} {d.year}"


FUTURE = _dk(datetime.date.today() + datetime.timedelta(days=60))


def _raw_product(handle, title, price="4000.00", location="Kongsvang Alle 29, 8000 Aarhus C",
                 product_type="Kursus", date=None, tags=None):
    """Legacy/raw product shape (augmented products, Shopify-style variants)."""
    return {
        "handle": handle,
        "title": title,
        "vendor": "Mannaz",
        "product_type": product_type,
        "ai_summary": f"Et kursus: {title}.",
        "tags": tags or [],
        "variants": [
            {"price": price, "option1": location, "option2": date or FUTURE},
        ],
    }


def _catalog_product(handle, title):
    """Normalized catalog_service product shape (for _catalog_compact_fields)."""
    return {
        "handle": handle,
        "title": title,
        "vendor": "Mannaz",
        "vendor_slug": "mannaz",
        "price_label": "10.000 kr",
        "price_min": 10000.0,
        "price_max": 10000.0,
        "format": "Fysisk",
        "summary": f"Et kursus: {title}.",
        "locations": ["København"],
        "dates": [FUTURE],
        "categories": ["Ledelse"],
        "category_slugs": ["ledelse"],
        "image_url": None,
        "source": "catalog",
    }


def _detailed(products):
    """Fake semantic_search_courses_detailed payload."""
    return {"products": products, "confidence": "medium", "debug": {"embedding_skipped": True}}


EMPTY_CATALOG = {"products": [], "total": 0}


def _run_catalog_search(args, rag_products, catalog_result=None):
    with patch.object(tools.catalog, "search_products", return_value=catalog_result or dict(EMPTY_CATALOG)), \
         patch.object(tools, "semantic_search_courses_detailed", return_value=_detailed(rag_products)):
        return json.loads(_execute_catalog_search(args))


class HardFilterPredicateTests(unittest.TestCase):
    def test_price_max_excludes_over_budget(self):
        p = _raw_product("dyr", "Dyrt kursus", price="12000.00")
        self.assertFalse(_product_passes_hard_filters(p, price_max=5000))
        self.assertTrue(_product_passes_hard_filters(p, price_max=15000))

    def test_location_uses_location_matches(self):
        p = _raw_product("aarhus", "Aarhus-kursus", location="Kongsvang Alle 29, 8000 Aarhus C")
        self.assertTrue(_product_passes_hard_filters(p, location="Aarhus"))
        self.assertTrue(_product_passes_hard_filters(p, location="århus"))  # alias
        self.assertFalse(_product_passes_hard_filters(p, location="Odense"))

    def test_format_substring_against_product_type(self):
        p = _raw_product("el", "E-learning kursus", product_type="E-learning")
        self.assertTrue(_product_passes_hard_filters(p, fmt="e-learning"))
        self.assertFalse(_product_passes_hard_filters(p, fmt="Konference"))

    def test_no_active_filters_is_passthrough(self):
        products = [_raw_product("a", "A"), _raw_product("b", "B")]
        filtered, active = _apply_hard_filters(products)
        self.assertEqual(filtered, products)
        self.assertEqual(active, [])

    def test_active_filters_named(self):
        products = [_raw_product("a", "A", price="12000.00")]
        filtered, active = _apply_hard_filters(products, price_max=5000, location="Aarhus")
        self.assertEqual(filtered, [])
        self.assertEqual(set(active), {"price_max", "location"})


class CatalogSearchRagFallbackTests(unittest.TestCase):
    """The RAG fallback must honor catalog_search's hard constraints."""

    def setUp(self):
        set_search_context()

    def tearDown(self):
        set_search_context()

    def test_over_budget_rag_result_filtered_out(self):
        rag = [
            _raw_product("dyr", "Dyrt lederkursus", price="12000.00"),
            _raw_product("billig", "Billigt lederkursus", price="4000.00"),
        ]
        payload = _run_catalog_search({"query": "ledelse", "price_max": 5000}, rag)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["search_mode"], "rag")
        handles = [r["handle"] for r in payload["results"]]
        self.assertEqual(handles, ["billig"])
        self.assertNotIn("filters_relaxed", payload)

    def test_wrong_city_rag_result_filtered_out(self):
        rag = [
            _raw_product("kbh", "Kursus i København", location="Vesterbrogade 1, 1620 København V"),
            _raw_product("aarhus", "Kursus i Aarhus", location="Kongsvang Alle 29, 8000 Aarhus C"),
        ]
        payload = _run_catalog_search({"query": "ledelse", "location": "Aarhus"}, rag)
        handles = [r["handle"] for r in payload["results"]]
        self.assertEqual(handles, ["aarhus"])

    def test_emptied_set_relaxes_honestly_never_unexplained_empty(self):
        rag = [
            _raw_product("a", "Kursus A", price="12000.00"),
            _raw_product("b", "Kursus B", price="9000.00"),
        ]
        payload = _run_catalog_search({"query": "ledelse", "price_max": 5000}, rag)
        # Never a dead-end: the unfiltered results come back, honestly flagged.
        self.assertEqual(payload["status"], "success")
        self.assertGreater(payload["count"], 0)
        self.assertTrue(payload["filters_relaxed"])
        self.assertIn("price_max_dropped", payload["relaxed_filters"])

    def test_relaxed_filters_name_every_dropped_constraint(self):
        rag = [_raw_product("a", "Kursus A", price="12000.00", location="Odense")]
        payload = _run_catalog_search(
            {"query": "ledelse", "price_max": 5000, "location": "Aarhus"}, rag)
        self.assertTrue(payload["filters_relaxed"])
        self.assertEqual(
            set(payload["relaxed_filters"]), {"price_max_dropped", "location_dropped"})

    def test_env_gate_off_restores_old_unfiltered_behavior(self):
        rag = [
            _raw_product("dyr", "Dyrt lederkursus", price="12000.00"),
            _raw_product("billig", "Billigt lederkursus", price="4000.00"),
        ]
        with patch.dict(os.environ, {"AI_SEARCH_HARD_FILTERS": "0"}):
            payload = _run_catalog_search({"query": "ledelse", "price_max": 5000}, rag)
        handles = [r["handle"] for r in payload["results"]]
        self.assertIn("dyr", handles)  # old behavior: constraint ignored
        self.assertNotIn("filters_relaxed", payload)

    def test_previously_shown_marked_on_reincluded_catalog_fallback(self):
        # All catalog hits were already shown -> the re-include path kicks in
        # and the results must carry previously_shown so the model can say so.
        set_search_context(shown_handles={"ledelse-i-praksis"})
        catalog_result = {"products": [_catalog_product("ledelse-i-praksis", "Ledelse i praksis")], "total": 1}
        with patch.object(tools.catalog, "search_products", return_value=catalog_result), \
             patch.object(tools, "semantic_search_courses_detailed", return_value=_detailed([])):
            payload = json.loads(_execute_catalog_search({"query": "ledelse"}))
        self.assertEqual(payload["search_mode"], "catalog")
        self.assertTrue(payload["results"])
        self.assertTrue(all(r.get("previously_shown") for r in payload["results"]))


class FilterCoursesRelaxationTests(unittest.TestCase):
    """_execute_filter_courses: progressive relaxation instead of a dead-end."""

    def setUp(self):
        set_search_context()

    def tearDown(self):
        set_search_context()

    def _run(self, args, products):
        with patch.object(tools, "load_augmented_products", return_value=products):
            return json.loads(_execute_filter_courses(args))

    def test_matching_filters_unchanged_no_relaxation_flags(self):
        products = [_raw_product("a", "Kursus A", price="4000.00")]
        payload = self._run({"price_max": 5000}, products)
        self.assertEqual(payload["status"], "success")
        self.assertNotIn("filters_relaxed", payload)
        self.assertNotIn("relaxed_filters", payload)

    def test_date_range_dropped_first(self):
        far_future_start = (datetime.date.today() + datetime.timedelta(days=3650)).isoformat()
        products = [_raw_product("a", "Kursus A", date=FUTURE)]
        payload = self._run({"start_after": far_future_start}, products)
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["filters_relaxed"])
        self.assertEqual(payload["relaxed_filters"], ["date_range_dropped"])

    def test_price_max_widened_25pct(self):
        # 4500 * 1.25 = 5625 >= 5500 -> widening alone rescues the result.
        products = [_raw_product("a", "Kursus A", price="5500.00")]
        payload = self._run({"price_max": 4500}, products)
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["filters_relaxed"])
        self.assertEqual(payload["relaxed_filters"], ["price_max_widened_25pct"])

    def test_location_dropped_last_with_elearning_hint(self):
        products = [_raw_product("kbh", "Kursus i København", location="Vesterbrogade 1, 1620 København V")]
        payload = self._run({"location": "Aarhus"}, products)
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["filters_relaxed"])
        self.assertEqual(payload["relaxed_filters"], ["location_dropped"])
        self.assertIn("e-learning", payload["relaxation_hint"].lower())

    def test_relaxation_is_progressive_and_cumulative(self):
        # Both over budget (even widened) AND in the wrong city: price widening
        # alone cannot rescue it, so location is dropped too.
        products = [_raw_product("kbh", "Kursus i København", price="5000.00",
                                 location="Vesterbrogade 1, 1620 København V")]
        payload = self._run({"price_max": 4500, "location": "Aarhus"}, products)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["relaxed_filters"], ["price_max_widened_25pct", "location_dropped"])

    def test_still_empty_after_relaxation_is_explained(self):
        # Tag filter excludes everything; relaxation (location) cannot help —
        # the no_results answer says relaxation was attempted, never an
        # unexplained empty list.
        products = [_raw_product("a", "Kursus A", tags=["ledelse"])]
        payload = self._run({"location": "Odense", "tag": "regnskab"}, products)
        self.assertEqual(payload["status"], "no_results")
        self.assertTrue(payload["relaxation_attempted"])
        self.assertEqual(payload["relaxed_filters"], ["location_dropped"])

    def test_env_gate_off_restores_plain_no_results(self):
        products = [_raw_product("a", "Kursus A", price="12000.00")]
        with patch.dict(os.environ, {"AI_SEARCH_HARD_FILTERS": "0"}):
            payload = self._run({"price_max": 5000}, products)
        self.assertEqual(payload["status"], "no_results")
        self.assertNotIn("relaxation_attempted", payload)
        self.assertNotIn("relaxed_filters", payload)

    def test_previously_shown_marker_on_reshown_results(self):
        set_search_context(shown_handles={"a"})
        products = [_raw_product("a", "Kursus A"), _raw_product("b", "Kursus B")]
        payload = self._run({"price_max": 50000}, products)
        by_handle = {r["handle"]: r for r in payload["results"]}
        self.assertTrue(by_handle["a"].get("previously_shown"))
        self.assertNotIn("previously_shown", by_handle["b"])
        # Re-shown results are deprioritized to the end of the list.
        self.assertEqual([r["handle"] for r in payload["results"]], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
