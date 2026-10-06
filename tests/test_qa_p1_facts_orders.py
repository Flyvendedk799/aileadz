"""Regression tests for QA P1 half: L03/L04/L09/L10/S01 (codey2)."""
import json
import time
import unittest
from unittest import mock

import catalog_service as catalog
from app1 import course_facts, confirm_store


class AvailabilityFactTests(unittest.TestCase):
    def test_five_stock_cases_agree(self):
        cases = [
            # tracked positive
            ({"inventory_management": "shopify", "inventory_quantity": 5}, "known", 5),
            # tracked zero sold out
            ({"inventory_management": "shopify", "inventory_quantity": 0, "inventory_policy": "deny"}, "sold_out", 0),
            # untracked raw 0 -> unknown
            ({"inventory_management": None, "inventory_quantity": 0}, "unknown", None),
            # missing stock
            ({}, "unknown", None),
            # invalid stock
            ({"inventory_management": "shopify", "inventory_quantity": "nope"}, "unknown", None),
        ]
        for variant, status, count in cases:
            avail = course_facts.variant_availability(variant)
            self.assertEqual(avail["status"], status, variant)
            self.assertEqual(avail["count"], count, variant)
            if status == "unknown":
                self.assertEqual(avail["label_da"], "Tilgængelighed ikke oplyst")
                self.assertNotIn(avail["label_da"], ("Ledig", "0 pladser"))

    def test_card_payload_never_defaults_to_99(self):
        payload = course_facts.card_variant_payload(
            {"inventory_management": None, "inventory_quantity": 0, "option1": "Aarhus", "option2": "1. maj"}
        )
        self.assertIsNone(payload["seats"])
        self.assertEqual(payload["availability"], "unknown")

    def test_normalize_variant_still_unknown_for_untracked_zero(self):
        nv = catalog.normalize_variant(
            {"price": "100", "inventory_management": None, "inventory_quantity": 0})
        self.assertIsNone(nv["seats"])


class SessionLocationFactTests(unittest.TestCase):
    def test_exact_city_does_not_mix_aarhus_date_with_copenhagen(self):
        product = {
            "title": "Agil projektledelse",
            "handle": "agil",
            "vendor": "Test",
            "variants": [
                {"price": "8900", "option1": "Aarhus", "option2": "13. marts 2026",
                 "inventory_management": "shopify", "inventory_quantity": 4},
                {"price": "8900", "option1": "København", "option2": "20. april 2026",
                 "inventory_management": "shopify", "inventory_quantity": 4},
            ],
        }
        bundle = course_facts.course_fact_bundle(product, location_filter="København", exact_city=True)
        self.assertTrue(bundle["has_exact_city_match"])
        cities = {s["city"] for s in bundle["matching_sessions"]}
        self.assertEqual(cities, {"København"})
        dates = {s["date"] for s in bundle["matching_sessions"]}
        self.assertIn("20. april 2026", dates)
        self.assertNotIn("13. marts 2026", dates)

    def test_no_copenhagen_session_is_honest(self):
        product = {
            "title": "X", "handle": "x", "vendor": "V",
            "variants": [
                {"price": "1", "option1": "Aarhus", "option2": "1. maj"},
            ],
        }
        bundle = course_facts.course_fact_bundle(product, location_filter="København", exact_city=True)
        self.assertFalse(bundle["has_exact_city_match"])
        self.assertEqual(bundle["matching_sessions"], [])


class StrictProductResolveTests(unittest.TestCase):
    def test_strict_title_no_fallback(self):
        from app1 import tools as T
        with mock.patch.object(T.catalog, "get_product", return_value=None), \
             mock.patch.object(T.catalog, "get_products", return_value=[
                 {"title": "AB 18 for ikke-jurister", "handle": "ab18", "vendor": "Nohrcon"},
             ]), \
             mock.patch.object(T.catalog, "search_products", return_value={
                 "products": [{"title": "AB 18 for ikke-jurister", "handle": "ab18"}],
             }):
            self.assertIsNone(T._find_catalog_product(
                title="QA-IKKE-EKSISTERER-20261006",
                strict_match=True,
            ))
            # Non-strict may still fall back
            hit = T._find_catalog_product(title="QA-IKKE-EKSISTERER-20261006", allow_alternatives=True)
            self.assertIsNotNone(hit)


class ConfirmStoreS01Tests(unittest.TestCase):
    def setUp(self):
        confirm_store.clear_all()

    def tearDown(self):
        confirm_store.clear_all()

    def test_reject_then_confirm_is_not_success(self):
        token = confirm_store.store_pending("sid-a", "employee", "create_course_order", {"x": 1})
        self.assertEqual(confirm_store.reject_pending("sid-a", token), "rejected")
        self.assertEqual(confirm_store.reject_pending("sid-a", token), "already_rejected")
        entry, status = confirm_store.peek_pending("sid-a", token)
        self.assertIsNone(entry)
        self.assertEqual(status, "rejected")
        # pop after reject yields nothing; outcome stays rejected
        self.assertIsNone(confirm_store.pop_pending("sid-a", token))
        self.assertEqual(confirm_store.get_outcome(token)["status"], "rejected")

    def test_expired_distinct_from_already_confirmed(self):
        token = confirm_store.store_pending("sid-a", "employee", "create_course_order", {"x": 1})
        with confirm_store._LOCK:
            confirm_store._STORE[token]["expires_at"] = time.time() - 1
        entry, status = confirm_store.peek_pending("sid-a", token)
        self.assertIsNone(entry)
        self.assertEqual(status, "expired")
        self.assertNotEqual(status, "consumed")

    def test_failed_execute_not_described_as_confirmed(self):
        token = confirm_store.store_pending("sid-a", "employee", "create_course_order", {"x": 1})
        entry = confirm_store.pop_pending("sid-a", token)
        self.assertIsNotNone(entry)
        confirm_store.mark_consumed(token, ok=False, tool_name="create_course_order")
        self.assertEqual(confirm_store.get_outcome(token)["status"], "failed")


class SerializeCardStockTests(unittest.TestCase):
    def test_serialize_uses_canonical_availability(self):
        import importlib
        app1_init = importlib.import_module("app1")
        product = {
            "title": "Test",
            "vendor": "V",
            "handle": "test-course",
            "variants": [
                {
                    "price": "1000",
                    "option1": "København",
                    "option2": "1. juni 2026",
                    "inventory_management": None,
                    "inventory_quantity": 0,
                }
            ],
        }
        # serialize_course_card already falls back safely; call through the
        # public helper after stubbing discount/description lookups.
        with mock.patch("app1.get_short_description", return_value="", create=True), \
             mock.patch("catalog_service.get_short_description", return_value="", create=True):
            # Prefer calling course_facts directly if serialize imports heavy deps fail;
            # still assert the card path when available.
            if hasattr(app1_init, "serialize_course_card"):
                with mock.patch.object(app1_init, "_apply_product_discount", side_effect=lambda p: p, create=True), \
                     mock.patch.object(app1_init, "_extract_product_location", return_value="København", create=True), \
                     mock.patch.object(app1_init, "_product_image_src", return_value="", create=True), \
                     mock.patch.object(app1_init, "_course_card_icon", return_value="fa-graduation-cap", create=True), \
                     mock.patch.object(app1_init, "_course_card_price", return_value="1.000 kr", create=True), \
                     mock.patch.object(app1_init, "_clean_variant_opt", side_effect=lambda x: x or "", create=True), \
                     mock.patch.object(app1_init, "_dkprice_filter", side_effect=lambda x: str(x), create=True), \
                     mock.patch.object(app1_init, "get_short_description", return_value="", create=True):
                    card = app1_init.serialize_course_card(product)
                self.assertEqual(card["variants"][0]["availability"], "unknown")
                self.assertIsNone(card["variants"][0]["seats"])
            else:
                payload = course_facts.card_variant_payload(product["variants"][0], product=product)
                self.assertEqual(payload["availability"], "unknown")
                self.assertIsNone(payload["seats"])


if __name__ == "__main__":
    unittest.main()


class AnnotateSessionFactsTests(unittest.TestCase):
    """Search annotation must exist and preserve exact-city honesty (L04 crash fix)."""

    def _product(self):
        return {
            "title": "Agil projektledelse",
            "handle": "agil",
            "vendor": "Test",
            "variants": [
                {
                    "price": "8900",
                    "option1": "Aarhus",
                    "option2": "13. marts 2026",
                    "inventory_management": "shopify",
                    "inventory_quantity": 4,
                },
                {
                    "price": "8900",
                    "option1": "København",
                    "option2": "20. april 2026",
                    "inventory_management": "shopify",
                    "inventory_quantity": 4,
                },
                {
                    "price": "8900",
                    "option1": "Aarhus",
                    "option2": "1. maj 2026",
                    "inventory_management": None,
                    "inventory_quantity": 0,
                },
            ],
        }

    def test_annotate_session_facts_is_defined_and_callable(self):
        from app1 import tools as T
        self.assertTrue(callable(getattr(T, "_annotate_session_facts", None)))

    def test_exact_city_matching_sessions_stay_correct(self):
        from app1 import tools as T
        product = self._product()
        compact = [{"title": product["title"], "handle": product["handle"], "vendor": "Test"}]
        out = T._annotate_session_facts(
            compact, [product], location="København", exact_city=True,
        )
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertTrue(row.get("has_exact_city_match"))
        cities = {s["city"] for s in row["matching_sessions"]}
        self.assertEqual(cities, {"København"})
        dates = {s["date"] for s in row["matching_sessions"]}
        self.assertIn("20. april 2026", dates)
        self.assertNotIn("13. marts 2026", dates)
        # availability_sessions mirrors get_product shape and stays city-honest
        for s in row["availability_sessions"]:
            self.assertIn("date", s)
            self.assertIn("city", s)
            self.assertIn("availability", s)
            self.assertEqual(s["city"], "København")

    def test_missing_product_is_noop(self):
        from app1 import tools as T
        compact = [{"title": "Ghost", "handle": "ghost"}]
        out = T._annotate_session_facts(compact, [], location="København", exact_city=True)
        self.assertEqual(out[0], {"title": "Ghost", "handle": "ghost"})
        self.assertNotIn("matching_sessions", out[0])

    def test_unknown_vs_sold_out_preserved(self):
        from app1 import tools as T
        product = self._product()
        compact = [{"title": product["title"], "handle": product["handle"]}]
        out = T._annotate_session_facts(
            compact, [product], location="Aarhus", exact_city=True,
        )
        statuses = {
            (s.get("date"), (s.get("availability") or {}).get("status"))
            for s in out[0]["matching_sessions"]
        }
        self.assertIn(("13. marts 2026", "known"), statuses)
        self.assertIn(("1. maj 2026", "unknown"), statuses)
        self.assertNotIn("sold_out", {st for _, st in statuses})

    def test_title_fallback_mapping(self):
        from app1 import tools as T
        product = self._product()
        # Compact row without handle still maps by title
        compact = [{"title": product["title"]}]
        out = T._annotate_session_facts(
            compact, [product], location="København", exact_city=True,
        )
        self.assertTrue(out[0].get("has_exact_city_match"))
        self.assertEqual({s["city"] for s in out[0]["matching_sessions"]}, {"København"})

    def test_exact_city_miss_sets_nearby_note(self):
        from app1 import tools as T
        product = {
            "title": "X", "handle": "x", "vendor": "V",
            "variants": [
                {"price": "1", "option1": "Aarhus", "option2": "1. maj",
                 "inventory_management": "shopify", "inventory_quantity": 2},
            ],
        }
        compact = [{"title": "X", "handle": "x"}]
        out = T._annotate_session_facts(
            compact, [product], location="København", exact_city=True,
        )
        self.assertFalse(out[0].get("has_exact_city_match"))
        self.assertEqual(out[0]["matching_sessions"], [])
        self.assertIn("Ingen hold i København", out[0].get("nearby_note", ""))


class ActiveResultSetNoResultsClearTests(unittest.TestCase):
    def test_empty_replace_clears_prior_handles(self):
        """Failed search must not leave the prior active set as 'latest' (L02)."""
        from app1 import result_set as rs
        state = {}
        rs.replace_active_result_set(state, [
            {"handle": "old-physical", "title": "Old", "vendor": "V", "price": "1"},
        ])
        self.assertEqual(rs.get_active_result_set(state)["handles"], ["old-physical"])
        rs.replace_active_result_set(state, [])
        cleared = rs.get_active_result_set(state)
        self.assertEqual(cleared["handles"], [])
        self.assertEqual(cleared["products"], [])
