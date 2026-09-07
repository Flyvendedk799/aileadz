import unittest

import catalog_service as catalog


class _Upload:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


class CatalogServiceTest(unittest.TestCase):
    def test_slugify_danish_names(self):
        self.assertEqual(catalog.slugify("Ledelse og organisation"), "ledelse-og-organisation")
        self.assertEqual(catalog.slugify("København & Århus"), "kobenhavn-aarhus")

    def test_loads_source_catalog(self):
        products = catalog.get_products()
        self.assertGreater(len(products), 1000)
        first = products[0]
        self.assertIn("handle", first)
        self.assertIn("price_label", first)
        self.assertIsInstance(first["categories"], list)

    def test_categories_ignore_operational_tags(self):
        categories = {category["name"].lower() for category in catalog.get_categories()}
        self.assertNotIn("efter aftale", categories)
        self.assertNotIn("kontakt for pris", categories)
        self.assertIn("it-professionel", categories)

    def test_csv_preview_normalizes_products(self):
        parsed = catalog.parse_catalog_csv(_Upload(
            b"title;vendor;category;tags;price;location;date\n"
            b"AI Ledelse;Test Vendor;Ledelse;HR|AI;1200;Kobenhavn;1. juni\n"
        ))
        self.assertEqual(parsed["summary"]["created"], 1)
        self.assertEqual(parsed["summary"]["skipped"], 0)
        product = catalog.normalize_product(parsed["products"][0], overrides={})
        self.assertEqual(product["handle"], "ai-ledelse")
        self.assertEqual(product["vendor"], "Test Vendor")
        self.assertIn("Ledelse", product["categories"])
        self.assertEqual(product["price_label"], "1.200 kr")

    def test_variant_seats_only_count_when_inventory_is_tracked(self):
        """The live catalog ships untracked Shopify rows with inventory_quantity 0.
        Reading those as sold out would disable booking for the whole catalog, so a
        seat count is only trusted when inventory_management is set."""
        untracked = catalog.normalize_variant(
            {"price": "100", "inventory_management": None, "inventory_quantity": 0})
        self.assertIsNone(untracked["seats"])

        tracked = catalog.normalize_variant(
            {"price": "100", "inventory_management": "shopify", "inventory_quantity": 3})
        self.assertEqual(tracked["seats"], 3)

        sold_out = catalog.normalize_variant(
            {"price": "100", "inventory_management": "shopify",
             "inventory_quantity": 0, "inventory_policy": "deny"})
        self.assertEqual(sold_out["seats"], 0)

        # "continue" means the vendor allows overselling -> not a stop sign.
        oversell = catalog.normalize_variant(
            {"price": "100", "inventory_management": "shopify",
             "inventory_quantity": 0, "inventory_policy": "continue"})
        self.assertIsNone(oversell["seats"])

        self.assertEqual(catalog.normalize_variant({"price": "100", "available": False})["seats"], 0)
        self.assertEqual(catalog.normalize_variant({"price": "100", "seats": 7})["seats"], 7)

    def test_live_catalog_has_no_falsely_sold_out_variants(self):
        sold_out = [v for product in catalog.get_products() for v in product["variants"]
                    if v["seats"] is not None and v["seats"] <= 0]
        self.assertEqual(sold_out, [])

    def test_discount_decoration_preserves_seats(self):
        product = catalog.normalize_product({
            "title": "Test", "vendor": "V", "handle": "test",
            "variants": [{"price": "1000", "inventory_management": "shopify",
                          "inventory_quantity": 4, "option1": "Aarhus", "option2": "1. maj"}],
        }, overrides={})
        decorated = catalog.decorate_product_with_discount(
            product, {"discount_type": "percentage", "discount_value": 10})
        self.assertEqual(decorated["variants"][0]["seats"], 4)
        self.assertEqual(decorated["variants"][0]["discounted_price_label"], "900 kr")


if __name__ == "__main__":
    unittest.main()
