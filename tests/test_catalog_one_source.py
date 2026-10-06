"""N-3.1: one catalog feeding pages, search index and chat ordering."""

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import catalog_service as cs  # noqa: E402
import shopify_sync  # noqa: E402
from app1 import rag  # noqa: E402


def _raw(handle, title, vendor="Udbyder A", price="1000", embedding=None, variants=None):
    p = {"id": abs(hash(handle)) % 10000, "handle": handle, "title": title, "vendor": vendor,
         "product_type": "Kursus", "tags": "Ledelse, Projekt", "body_html": "<p>Beskrivelse</p>",
         "variants": variants or [{"id": 1, "price": price, "option1": "København", "option2": "1. juni 2099"}]}
    if embedding:
        p["embedding"] = embedding
    return p


class TempCatalog:
    """Point catalog_service at a throwaway source file + instance dir."""

    def __init__(self, products):
        self.dir = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.dir.name, "source.json")
        with open(self.source, "w", encoding="utf-8") as f:
            json.dump(products, f)
        self.instance = os.path.join(self.dir.name, "instance")
        os.makedirs(self.instance)

    def __enter__(self):
        self.patches = [
            mock.patch.dict(os.environ, {"CATALOG_SOURCE_FILE": self.source}),
            mock.patch.object(cs, "_instance_path", lambda *parts: os.path.join(self.instance, *parts)),
        ]
        for p in self.patches:
            p.start()
        cs._SIG_CACHE["value"] = None
        cs.clear_catalog_cache()
        rag._augmented_cache = None
        rag._index_meta["signature"] = None
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()
        cs._SIG_CACHE["value"] = None
        cs.clear_catalog_cache()
        rag._augmented_cache = None
        rag._index_meta["signature"] = None
        self.dir.cleanup()


class SourceAndOverlayTests(unittest.TestCase):
    def test_source_file_can_live_outside_git(self):
        with TempCatalog([_raw("a", "Kursus A"), _raw("b", "Kursus B")]):
            self.assertEqual(len(cs.get_products()), 2)
            self.assertEqual(cs.source_file_path(), os.environ["CATALOG_SOURCE_FILE"])

    def test_hidden_and_archived_products_leave_every_reader(self):
        with TempCatalog([_raw("a", "Kursus A"), _raw("b", "Kursus B")]):
            self.assertTrue(cs.set_product_status("a", "hidden", actor="admin"))
            self.assertEqual([p["handle"] for p in cs.get_products()], ["b"])
            self.assertIsNone(cs.get_product("a"))                       # product page / ordering 404
            self.assertEqual(len(cs.get_all_products()), 2)              # admin still sees it
            self.assertEqual(cs.get_product_any("a")["status"], "hidden")
            self.assertEqual([p["handle"] for p in rag.load_augmented_products()], ["b"])   # AI search too
            cs.set_product_status("a", "active")
            self.assertEqual(len(cs.get_products()), 2)

    def test_unknown_handle_or_status_is_refused(self):
        with TempCatalog([_raw("a", "Kursus A")]):
            self.assertIsNone(cs.set_product_status("nope", "hidden"))
            self.assertIsNone(cs.set_product_status("a", "deleted"))

    def test_admin_edits_overlay_the_source_and_can_be_reset(self):
        with TempCatalog([_raw("a", "Gammel titel")]):
            cs.update_product("a", {"title": "Ny titel", "summary": "Kort tekst", "tags": "AI, Ledelse"}, actor="admin")
            p = cs.get_product("a")
            self.assertEqual((p["title"], p["summary"], p["tags"]), ("Ny titel", "Kort tekst", ["AI", "Ledelse"]))
            self.assertTrue(p["edited"])
            cs.reset_product_edits("a")
            self.assertEqual(cs.get_product("a")["title"], "Gammel titel")

    def test_admin_list_search_and_filter(self):
        with TempCatalog([_raw("a", "Projektledelse"), _raw("b", "Excel", vendor="Anden")]):
            cs.set_product_status("b", "archived")
            res = cs.admin_list_products(q="projekt")
            self.assertEqual([p["handle"] for p in res["products"]], ["a"])
            self.assertEqual(cs.admin_list_products(status="archived")["total"], 1)
            self.assertEqual(res["counts"], {"active": 1, "hidden": 0, "archived": 1})

    def test_stale_courses_are_excluded_from_recommendation_lists(self):
        past = [{"id": 1, "price": "1", "option1": "København", "option2": "1. januar 2020"}]
        with TempCatalog([_raw("old", "Gammelt", variants=past), _raw("new", "Nyt")]):
            self.assertIn("old", cs.stale_handles())
            kept = cs.exclude_stale(cs.get_products())
            self.assertEqual([p["handle"] for p in kept], ["new"])


class IndexTests(unittest.TestCase):
    def test_search_index_is_a_view_over_the_catalog_including_csv_imports(self):
        with TempCatalog([_raw("a", "Kursus A")]) as t:
            cs._write_json(os.path.join(t.instance, cs.IMPORT_PRODUCTS_FILE),
                           {"products": [_raw("csv-kursus", "CSV kursus")]})
            cs.clear_catalog_cache()
            handles = {p["handle"] for p in rag.load_augmented_products()}
            self.assertEqual(handles, {"a", "csv-kursus"})       # vendor/CSV courses reach AI search

    def test_embed_missing_is_incremental_and_persisted(self):
        dim = rag.embedding_dimensions()
        with TempCatalog([_raw("a", "Har vektor", embedding=[0.1] * dim), _raw("b", "Mangler vektor")]):
            calls = []

            def fake_embed(texts):
                calls.append(list(texts))
                return [[0.2] * dim for _ in texts]

            res = rag.embed_missing(client_embed=fake_embed)
            self.assertEqual(res["embedded"], 1)
            self.assertEqual(len(calls[0]), 1)                   # only the missing one was sent
            by_handle = {p["handle"]: p for p in rag.load_augmented_products()}
            self.assertTrue(by_handle["b"].get("embedding"))
            self.assertEqual(rag.embed_missing(client_embed=fake_embed)["embedded"], 0)   # nothing left
            st = rag.index_status()
            self.assertEqual((st["products"], st["with_embeddings"], st["missing_embeddings"]), (2, 2, 0))

    def test_embedding_is_skipped_cleanly_without_an_api_key(self):
        with TempCatalog([_raw("a", "Kursus A")]), mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(rag.embed_missing().get("reason"), "no_api_key")

    def test_rebuild_reports_status(self):
        with TempCatalog([_raw("a", "Kursus A")]), mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            st = rag.rebuild_index(embed=True)
            self.assertEqual(st["products"], 1)
            self.assertTrue(st["built_at"])


class ShopifySyncTests(unittest.TestCase):
    class Resp:
        def __init__(self, products, link=None, status=200):
            self._p, self.status_code, self.headers = products, status, ({"Link": link} if link else {})

        def json(self):
            return {"products": self._p}

    class Http:
        def __init__(self, pages):
            self.pages, self.calls = list(pages), []

        def get(self, url, headers=None, timeout=None):
            self.calls.append((url, headers))
            return self.pages.pop(0)

    ENV = {"SHOPIFY_STORE": "demo.myshopify.com", "SHOPIFY_ADMIN_TOKEN": "shpat_test"}

    def test_skips_cleanly_without_credentials(self):
        with mock.patch.dict(os.environ, {"SHOPIFY_STORE": "", "SHOPIFY_ADMIN_TOKEN": ""}):
            self.assertIn("skipped", shopify_sync.sync())

    def test_paginates_and_replaces_the_source_file_atomically(self):
        with TempCatalog([_raw("old", "Gammel")]), mock.patch.dict(os.environ, self.ENV):
            http = self.Http([
                self.Resp([_raw("a", "A"), _raw("b", "B")], link='<https://demo.myshopify.com/next>; rel="next"'),
                self.Resp([_raw("c", "C")]),
            ])
            out = shopify_sync.sync(http=http)
            self.assertEqual(out, {"synced": 3})
            self.assertEqual(len(http.calls), 2)
            self.assertEqual(http.calls[0][1]["X-Shopify-Access-Token"], "shpat_test")   # token only from env
            self.assertEqual({p["handle"] for p in cs.get_products()}, {"a", "b", "c"})

    def test_a_suspiciously_small_response_never_wipes_the_catalog(self):
        with TempCatalog([_raw(f"p{i}", f"K{i}") for i in range(10)]), mock.patch.dict(os.environ, self.ENV):
            out = shopify_sync.sync(http=self.Http([self.Resp([_raw("only", "Kun én")])]))
            self.assertIn("error", out)
            self.assertEqual(len(cs.get_products()), 10)

    def test_api_error_is_reported_not_raised(self):
        with TempCatalog([_raw("a", "A")]), mock.patch.dict(os.environ, self.ENV):
            out = shopify_sync.sync(http=self.Http([self.Resp([], status=401)]))
            self.assertIn("error", out)

    def test_no_secret_is_committed(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        text = open(os.path.join(root, "shopify_sync.py"), encoding="utf-8").read()
        self.assertNotIn("shpat_", text.replace("shpat_...", ""))


class VariantPricingTests(unittest.TestCase):
    def _product(self, variants):
        return {"variants": variants}

    def test_chosen_session_sets_the_price_not_the_first_variant(self):
        from app1 import tools
        product = self._product([
            {"date": "1. juni", "location": "København", "city": "København", "price": 5000.0},
            {"date": "8. august", "location": "Aarhus", "city": "Aarhus", "price": 7500.0}])
        v, amb = tools._pick_variant(product, "8. august", "")
        self.assertEqual((v["price"], amb), (7500.0, False))
        v, amb = tools._pick_variant(product, "", "aarhus")
        self.assertEqual(v["price"], 7500.0)

    def test_unspecified_session_with_different_prices_asks_instead_of_guessing(self):
        from app1 import tools
        product = self._product([{"date": "a", "location": "x", "price": 1.0}, {"date": "b", "location": "y", "price": 2.0}])
        self.assertEqual(tools._pick_variant(product, "", ""), (None, True))
        self.assertEqual(tools._pick_variant(product, "c", "")[1], True)    # asked for a session that does not exist

    def test_equal_prices_still_require_the_actual_date_and_venue(self):
        from app1 import tools
        product = self._product([{"date": "a", "price": 3.0}, {"date": "b", "price": 3.0}])
        v, amb = tools._pick_variant(product, "", "")
        self.assertEqual((v, amb), (None, True))


class VendorProfileTests(unittest.TestCase):
    def test_db_profiles_override_the_bundled_seed(self):
        seed = {"Udbyder A": {"reputation": "Gammel tekst", "best_for": "Alt"}}
        with mock.patch.object(cs, "_json_seed_profiles", return_value=seed), \
                mock.patch.object(cs, "_db_vendor_profiles", return_value={"Udbyder A": {"reputation": "Ny tekst fra DB"}}):
            merged = cs._load_vendor_profiles()
        self.assertEqual(merged["Udbyder A"]["reputation"], "Ny tekst fra DB")
        self.assertEqual(merged["Udbyder A"]["best_for"], "Alt")          # seed fills the gaps

    def test_seed_is_used_when_no_database(self):
        with mock.patch.object(cs, "_json_seed_profiles", return_value={"X": {"reputation": "seed"}}), \
                mock.patch.object(cs, "_db_vendor_profiles", return_value=None):
            self.assertEqual(cs._load_vendor_profiles()["X"]["reputation"], "seed")


class CategorisationProviderTests(unittest.TestCase):
    def test_ai_categorisation_uses_the_provider_toggle(self):
        import ai_runtime
        batch = [{"handle": "a", "title": "A", "vendor": "V", "tags": [], "categories": [], "summary": "s"}]
        with mock.patch.object(ai_runtime, "run_direct_completion",
                               return_value='[{"handle":"a","categories":["Ledelse"],"reason":"x"}]') as run:
            out = cs._call_openai_category_batch(batch, ["Ledelse"])
        run.assert_called_once()
        self.assertEqual(out[0]["categories"], ["Ledelse"])


if __name__ == "__main__":
    unittest.main()
