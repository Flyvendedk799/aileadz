"""When the AI provider fails, anonymous chat answers with catalogue results."""
import json
import unittest
from unittest import mock

from tests.sqlite_platform import PlatformDB, make_app, render_patches

PRODUCT = {
    "id": 1, "handle": "projektledelse-grundkursus", "title": "Projektledelse grundkursus",
    "body_html": "<p>Lær at lede projekter.</p>", "vendor": "Test", "tags": "", "product_type": "",
    "variants": [{"id": 11, "price": "4500", "title": "Hold 1"}], "images": [],
    "category_slugs": [], "vendor_slug": "test",
}


def _events(sse):
    out = []
    for part in sse.split("\n\n"):
        if part.startswith("data: ") and part[6:].strip() != "[DONE]":
            out.append(json.loads(part[6:]))
    return out


class ProviderFallbackTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.db.raw.close)

    def test_fallback_has_course_cards_for_the_query_and_is_flagged(self):
        import app1.agent as agent
        with self.app.test_request_context("/"), mock.patch(
            "catalog_service.search_products", return_value={"products": [PRODUCT]}
        ) as search:
            events = _events("".join(agent.provider_fallback_events("projektledelse", RuntimeError("401 no key"))))
        self.assertEqual(search.call_args[0][0], {"q": "projektledelse"})
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["chunk", "course_cards", "fallback"])
        self.assertIn("ikke tilgængelig", events[0]["content"])
        self.assertEqual(len(events[1]["items"]), 1)
        self.assertTrue(events[1]["fallback"])

    def test_no_matches_degrades_to_the_plain_error(self):
        import app1.agent as agent
        with self.app.test_request_context("/"), mock.patch(
            "catalog_service.search_products", return_value={"products": []}
        ):
            events = _events("".join(agent.provider_fallback_events("xyzzy", RuntimeError("boom"))))
        self.assertEqual([e["type"] for e in events], ["chunk", "fallback"])
        self.assertIn("teknisk fejl", events[0]["content"])

    def test_agent_turn_failure_streams_the_fallback(self):
        import app1.agent as agent
        client = self.app.test_client()
        with mock.patch("ai_runtime.run_agent_with_fallback", side_effect=RuntimeError("401 invalid key")), \
                mock.patch("ai_runtime.iter_completion_stream", side_effect=RuntimeError("401 invalid key"), create=True), \
                mock.patch("catalog_service.search_products", return_value={"products": [PRODUCT]}):
            resp = client.post("/app1/ask", json={"query": "projektledelse"})
            body = resp.get_data(as_text=True)
        kinds = [e["type"] for e in _events(body)]
        self.assertIn("fallback", kinds, body[:500])
        self.assertIn("course_cards", kinds)
        self.assertTrue(hasattr(agent, "provider_fallback_events"))

    def test_anonymous_chat_config_has_no_profile_chips(self):
        html = self.app.test_client().get("/chat").get_data(as_text=True)
        self.assertIn('"loggedIn": false', html.replace('"loggedIn":false', '"loggedIn": false'))
