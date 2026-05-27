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


if __name__ == "__main__":
    unittest.main()
