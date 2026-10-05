"""Chat ordering and search regressions from a reported transcript.

The user asked for communication courses in Nordsjælland, then "ikke online,
fysisk", then ordered one course and picked "16 september". Four things went wrong:

1. The session picker compared dates as strings, so "16 september" never matched
   the "15.-16. september 2099" session; the order stalled and the assistant
   handed out a course link instead of a confirm card.
2. prepare_course_order returned confirmation data but no confirm card, so the
   model said the order was "started" while nothing was booked.
3. catalog_search had no way to exclude online courses, and knew no regions.
4. The same course could be listed twice (same title and vendor, two handles).

Offline: catalog, contact lookups and RAG are patched at the module seams.
"""
import json
import unittest
from unittest import mock

import app1.tools as tools
from app1.tools import _execute_catalog_search, _pick_variant, set_search_context

FORTUNA = {
    "handle": "kommunikation-og-samarbejde",
    "title": "Kursus i kommunikation og samarbejde",
    "vendor": "Fortuna Kurser",
    "variants": [
        {"date": "10.-11. marts 2098", "location": "Vesterbrogade 1, 1620 København V", "price": 6500},
        {"date": "15.-16. september 2099", "location": "Vesterbrogade 1, 1620 København V", "price": 7500},
    ],
}


class SessionDateMatchingTests(unittest.TestCase):
    def _date(self, wanted):
        variant, ambiguous = _pick_variant(FORTUNA, wanted)
        return (variant or {}).get("date"), ambiguous

    def test_a_day_inside_a_multi_day_session_picks_it(self):
        self.assertEqual(self._date("16 september"), ("15.-16. september 2099", False))

    def test_iso_and_full_danish_dates_pick_the_session(self):
        self.assertEqual(self._date("2099-09-15")[0], "15.-16. september 2099")
        self.assertEqual(self._date("15. september 2099")[0], "15.-16. september 2099")

    def test_a_month_alone_picks_the_only_session_that_month(self):
        self.assertEqual(self._date("marts")[0], "10.-11. marts 2098")

    def test_a_date_with_no_session_asks_again(self):
        self.assertEqual(self._date("1 december"), (None, True))

    def test_month_fallback_needs_a_single_session_that_month(self):
        product = {"variants": [{"date": "3. september 2099", "price": 1},
                                {"date": "24. september 2099", "price": 2}]}
        self.assertEqual(_pick_variant(product, "16 september"), (None, True))


class PreparedOrderPreviewTests(unittest.TestCase):
    def _prepare(self, args):
        contact = mock.patch.multiple(
            tools,
            _company_employee_contact=mock.Mock(return_value={"full_name": "Tobias P", "email": "t@firma.dk"}),
            _user_account_contact=mock.Mock(return_value={}),
        )
        with contact, \
                mock.patch.object(tools.catalog, "get_product", return_value=FORTUNA), \
                mock.patch.object(tools, "_catalog_compact_fields", return_value={}), \
                mock.patch.object(tools, "apply_discount", return_value=(None, None, None)), \
                mock.patch.object(tools, "mark_order_flow_open"):
            return json.loads(tools._execute_prepare_course_order(args, "tobias"))

    def test_ready_order_carries_a_confirm_card_for_create_course_order(self):
        out = self._prepare({"product_handle": FORTUNA["handle"], "variant_date": "16 september"})
        self.assertFalse(out["creates_order"])
        self.assertEqual(out["status"], "ready_for_confirmation")
        self.assertTrue(out["needs_confirmation"])
        self.assertEqual(out["confirm_tool"], "create_course_order")
        self.assertEqual(out["details"]["variant"]["date"], "15.-16. september 2099")
        self.assertEqual(float(out["price"]), 7500)
        self.assertIn("15.-16. september 2099", out["message_da"])

    def test_unresolved_session_gets_no_confirm_card(self):
        out = self._prepare({"product_handle": FORTUNA["handle"]})
        self.assertEqual(out["status"], "needs_info")
        self.assertNotIn("needs_confirmation", out)
        self.assertTrue(out["variant_options"])


def _catalog_product(handle, title, locations, vendor="Fortuna Kurser", fmt="Kursus"):
    return {
        "handle": handle, "title": title, "vendor": vendor, "vendor_slug": "v",
        "price_label": "5.000 kr", "price_min": 5000.0, "format": fmt,
        "summary": title, "locations": locations, "dates": [], "category_slugs": [],
        "source": "catalog",
    }


class CatalogSearchDeliveryTests(unittest.TestCase):
    def setUp(self):
        set_search_context()

    def tearDown(self):
        set_search_context()

    def _search(self, args, products):
        result = {"products": products, "total": len(products)}
        with mock.patch.object(tools.catalog, "search_products", return_value=result) as sp, \
                mock.patch.object(tools, "semantic_search_courses_detailed",
                                  return_value={"products": [], "confidence": "medium"}):
            payload = json.loads(_execute_catalog_search(args))
        return payload, sp.call_args.kwargs["filters"]

    PRODUCTS = [
        _catalog_product("online-a", "Kommunikation online", ["Online"]),
        _catalog_product("kbh", "Kommunikation i praksis", ["København"]),
        _catalog_product("hybrid", "Kommunikation hybrid", ["Online", "Aarhus"]),
        _catalog_product("el", "Kommunikation e-learning", [], fmt="E-learning"),
    ]

    def test_fysisk_keeps_only_courses_with_an_in_person_session(self):
        payload, _ = self._search({"query": "kommunikation", "delivery": "fysisk"}, self.PRODUCTS)
        self.assertEqual([r["handle"] for r in payload["results"]], ["kbh", "hybrid"])

    def test_online_keeps_only_online_courses(self):
        payload, _ = self._search({"query": "kommunikation", "delivery": "online"}, self.PRODUCTS)
        self.assertEqual([r["handle"] for r in payload["results"]], ["online-a", "hybrid", "el"])

    def test_fysisk_filed_as_location_or_format_is_treated_as_delivery(self):
        for args in ({"location": "fysisk"}, {"format": "Fysisk"}):
            payload, filters = self._search({"query": "kommunikation", **args}, self.PRODUCTS)
            self.assertEqual([r["handle"] for r in payload["results"]], ["kbh", "hybrid"], args)
            self.assertEqual(filters["location"], "")
            self.assertEqual(filters["format"], "")

    def test_region_matches_the_towns_inside_it(self):
        products = self.PRODUCTS + [_catalog_product("hil", "Kommunikation i Hillerød", ["Hillerød"])]
        payload, filters = self._search({"query": "kommunikation", "location": "Nordsjælland"}, products)
        self.assertEqual(filters["location"], "")
        self.assertEqual([r["handle"] for r in payload["results"]], ["hil"])

    def test_the_same_course_listed_twice_is_shown_once(self):
        products = [
            _catalog_product("kom-1", "Kommunikation for ledere", ["Online"]),
            _catalog_product("kom-2", "Kommunikation for ledere", ["Online"]),
            _catalog_product("kom-3", "Kommunikation for ledere", ["Online"], vendor="Anden udbyder"),
        ]
        payload, _ = self._search({"query": "kommunikation", "limit": 2}, products)
        self.assertEqual([r["handle"] for r in payload["results"]], ["kom-1", "kom-3"])


if __name__ == "__main__":
    unittest.main()
