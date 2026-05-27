import json
import unittest

from app1.tools import execute_tool


class _ToolCall:
    def __init__(self, name, args):
        self.id = "call_test"
        self.function = type("Function", (), {"name": name, "arguments": json.dumps(args)})()


class FuturematchAIToolTests(unittest.TestCase):
    def test_catalog_search_returns_internal_urls(self):
        payload = json.loads(execute_tool(
            _ToolCall("catalog_search", {"query": "ledelse", "limit": 2}),
            username=None,
            session_id="test",
        ))
        self.assertIn(payload["status"], {"success", "no_results"})
        for result in payload.get("results", []):
            self.assertTrue(result["product_url"].startswith("/products/"))
            self.assertTrue(result["vendor_url"].startswith("/vendors/"))
            self.assertNotIn("futurematch.dk/products", json.dumps(result))

    def test_prepare_order_does_not_create_order(self):
        search = json.loads(execute_tool(
            _ToolCall("catalog_search", {"query": "ledelse", "limit": 1}),
            username="demo",
            session_id="test",
        ))
        if not search.get("results"):
            self.skipTest("No catalog products available")
        handle = search["results"][0]["handle"]
        payload = json.loads(execute_tool(
            _ToolCall("prepare_course_order", {"product_handle": handle}),
            username="demo",
            session_id="test",
        ))
        self.assertFalse(payload["creates_order"])
        self.assertIn(payload["status"], {"ready_for_confirmation", "needs_info"})
        self.assertEqual(payload["confirmation_payload"]["product_handle"], handle)


if __name__ == "__main__":
    unittest.main()
