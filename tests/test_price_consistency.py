"""TL-01: One price-resolution helper — search, details and comparison must
quote the same negotiated (supplier-agreement) price.

Offline: no OpenAI, no MySQL. Fake products are injected at the module seams
(load_augmented_products / catalog.get_product); the supplier agreement is set
via the same set_search_context call agent.py uses before tool execution.
"""
import json
import unittest
from unittest.mock import patch

import app1.tools as tools
from app1.tools import (
    price_view,
    set_search_context,
    _extract_compact_fields,
    _catalog_compact_fields,
    _execute_catalog_compare_products,
    _execute_compare_courses,
    _execute_get_course_details,
)

AGREEMENT = {
    "Mannaz": {
        "discount_type": "percentage",
        "discount_value": 10,
        "agreement_name": "Rammeaftale 2026",
    }
}

DISCOUNTED_LABEL = "kr 9.000"
ORIGINAL_LABEL = "kr 10.000"
SAVINGS_LABEL = "kr 1.000"


def _raw_product(handle="ledelse-i-praksis", title="Ledelse i praksis", price="10000.00"):
    """Legacy/raw product shape (augmented products, Shopify-style variants)."""
    return {
        "handle": handle,
        "title": title,
        "vendor": "Mannaz",
        "product_type": "Kursus",
        "ai_summary": "Et kursus i praktisk ledelse for nye ledere.",
        "tags": [],
        "variants": [
            {"price": price, "option1": "København", "option2": "12. august 2099"},
        ],
    }


def _catalog_product(handle="ledelse-i-praksis", title="Ledelse i praksis", price_min=10000.0):
    """Normalized catalog_service product shape."""
    return {
        "handle": handle,
        "title": title,
        "vendor": "Mannaz",
        "vendor_slug": "mannaz",
        "price_label": "10.000 kr",
        "price_min": price_min,
        "price_max": price_min,
        "format": "Fysisk",
        "summary": "Et kursus i praktisk ledelse for nye ledere.",
        "locations": ["København"],
        "dates": ["12. august 2099"],
        "categories": ["Ledelse"],
        "category_slugs": ["ledelse"],
        "image_url": None,
        "source": "catalog",
    }


class PriceViewHelperTests(unittest.TestCase):
    def tearDown(self):
        set_search_context()

    def test_no_agreement_returns_raw_price_without_discount(self):
        set_search_context()
        pv = price_view("10000.00", "Mannaz")
        self.assertEqual(pv["price"], ORIGINAL_LABEL)
        self.assertEqual(pv["amount"], 10000.0)
        self.assertIsNone(pv["discount"])

    def test_percentage_agreement_discounts_price(self):
        set_search_context(supplier_agreements=AGREEMENT)
        pv = price_view("10000.00", "Mannaz")
        self.assertEqual(pv["price"], DISCOUNTED_LABEL)
        self.assertEqual(pv["amount"], 9000.0)
        self.assertEqual(pv["discount"]["original_price"], ORIGINAL_LABEL)
        self.assertEqual(pv["discount"]["discounted_price"], DISCOUNTED_LABEL)
        self.assertEqual(pv["discount"]["savings"], SAVINGS_LABEL)
        self.assertEqual(pv["discount"]["agreement_name"], "Rammeaftale 2026")

    def test_agreement_for_other_vendor_does_not_apply(self):
        set_search_context(supplier_agreements=AGREEMENT)
        pv = price_view("10000.00", "Anden Udbyder")
        self.assertEqual(pv["price"], ORIGINAL_LABEL)
        self.assertIsNone(pv["discount"])

    def test_fixed_amount_agreement(self):
        set_search_context(supplier_agreements={
            "Mannaz": {"discount_type": "fixed_amount", "discount_value": 2500, "agreement_name": "Fast rabat"},
        })
        pv = price_view("10000.00", "Mannaz")
        self.assertEqual(pv["amount"], 7500.0)
        self.assertEqual(pv["price"], "kr 7.500")

    def test_unparseable_price_is_passed_through(self):
        set_search_context(supplier_agreements=AGREEMENT)
        pv = price_view(None, "Mannaz")
        self.assertEqual(pv["price"], "Pris på forespørgsel")
        self.assertIsNone(pv["amount"])
        self.assertIsNone(pv["discount"])


class PriceConsistencyAcrossToolsTests(unittest.TestCase):
    """The same course must show the same negotiated price in every tool payload."""

    def setUp(self):
        set_search_context(supplier_agreements=AGREEMENT)

    def tearDown(self):
        set_search_context()

    def test_extract_compact_fields_discounts(self):
        fields = _extract_compact_fields(_raw_product())
        self.assertEqual(fields["price"], DISCOUNTED_LABEL)
        self.assertEqual(fields["discount"]["original_price"], ORIGINAL_LABEL)
        self.assertEqual(fields["discount"]["savings"], SAVINGS_LABEL)

    def test_catalog_compact_fields_discounts(self):
        fields = _catalog_compact_fields(_catalog_product())
        self.assertEqual(fields["price"], DISCOUNTED_LABEL)
        self.assertEqual(fields["price_min"], 9000.0)
        self.assertEqual(fields["discount"]["original_price"], ORIGINAL_LABEL)
        self.assertEqual(fields["discount"]["agreement_name"], "Rammeaftale 2026")

    def test_catalog_compare_products_discounts(self):
        products = {
            "a": _catalog_product(handle="a", title="Kursus A"),
            "b": _catalog_product(handle="b", title="Kursus B"),
        }
        with patch.object(tools.catalog, "get_product", side_effect=lambda h: products.get(h)):
            payload = json.loads(_execute_catalog_compare_products({"handles": ["a", "b"]}))
        self.assertEqual(payload["status"], "success")
        for entry in payload["comparison"]:
            self.assertEqual(entry["price"], DISCOUNTED_LABEL)
            self.assertEqual(entry["discount"]["original_price"], ORIGINAL_LABEL)

    def test_get_course_details_discounts(self):
        with patch.object(tools, "load_augmented_products", return_value=[_raw_product()]):
            payload = json.loads(_execute_get_course_details({"handle": "ledelse-i-praksis"}))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["price"], DISCOUNTED_LABEL)
        self.assertEqual(payload["discount"]["original_price"], ORIGINAL_LABEL)

    def test_compare_courses_discounts(self):
        fakes = [
            _raw_product(handle="a", title="Kursus A"),
            _raw_product(handle="b", title="Kursus B"),
        ]
        with patch.object(tools, "load_augmented_products", return_value=fakes):
            payload = json.loads(_execute_compare_courses({"handles": ["a", "b"]}))
        self.assertEqual(payload["status"], "success")
        for comp in payload["comparison"]:
            self.assertEqual(comp["price"], DISCOUNTED_LABEL)
            self.assertEqual(comp["discount"]["original_price"], ORIGINAL_LABEL)

    def test_all_surfaces_quote_identical_price(self):
        prices = set()
        prices.add(_extract_compact_fields(_raw_product())["price"])
        prices.add(_catalog_compact_fields(_catalog_product())["price"])
        products = {
            "a": _catalog_product(handle="a", title="Kursus A"),
            "ledelse-i-praksis": _catalog_product(),
        }
        with patch.object(tools.catalog, "get_product", side_effect=lambda h: products.get(h)):
            payload = json.loads(_execute_catalog_compare_products({"handles": ["ledelse-i-praksis", "a"]}))
        prices.add(payload["comparison"][0]["price"])
        with patch.object(tools, "load_augmented_products", return_value=[_raw_product(), _raw_product(handle="a", title="Kursus A")]):
            details = json.loads(_execute_get_course_details({"handle": "ledelse-i-praksis"}))
            compare = json.loads(_execute_compare_courses({"handles": ["ledelse-i-praksis", "a"]}))
        prices.add(details["price"])
        prices.add(compare["comparison"][0]["price"])
        self.assertEqual(prices, {DISCOUNTED_LABEL})


class PriceConsistencyNoAgreementTests(unittest.TestCase):
    """Without an agreement, nothing changes: raw price, no discount block."""

    def setUp(self):
        set_search_context()

    def tearDown(self):
        set_search_context()

    def test_extract_compact_fields_raw_price(self):
        fields = _extract_compact_fields(_raw_product())
        self.assertEqual(fields["price"], ORIGINAL_LABEL)
        self.assertNotIn("discount", fields)

    def test_catalog_compact_fields_keeps_price_label(self):
        fields = _catalog_compact_fields(_catalog_product())
        self.assertEqual(fields["price"], "10.000 kr")
        self.assertEqual(fields["price_min"], 10000.0)
        self.assertNotIn("discount", fields)

    def test_get_course_details_raw_price(self):
        with patch.object(tools, "load_augmented_products", return_value=[_raw_product()]):
            payload = json.loads(_execute_get_course_details({"handle": "ledelse-i-praksis"}))
        self.assertEqual(payload["price"], ORIGINAL_LABEL)
        self.assertNotIn("discount", payload)

    def test_compare_executors_raw_price(self):
        fakes = [_raw_product(handle="a", title="Kursus A"), _raw_product(handle="b", title="Kursus B")]
        with patch.object(tools, "load_augmented_products", return_value=fakes):
            payload = json.loads(_execute_compare_courses({"handles": ["a", "b"]}))
        for comp in payload["comparison"]:
            self.assertEqual(comp["price"], ORIGINAL_LABEL)
            self.assertNotIn("discount", comp)
        products = {"a": _catalog_product(handle="a"), "b": _catalog_product(handle="b")}
        with patch.object(tools.catalog, "get_product", side_effect=lambda h: products.get(h)):
            payload = json.loads(_execute_catalog_compare_products({"handles": ["a", "b"]}))
        for entry in payload["comparison"]:
            self.assertEqual(entry["price"], "10.000 kr")
            self.assertNotIn("discount", entry)


if __name__ == "__main__":
    unittest.main()
