"""N-5.6: the embeddable widget enforces the PARENT page's origin and keeps memory
in an iframe-held session token."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from flask import Flask, Response  # noqa: E402

import app1  # noqa: E402


class _Cur:
    def __init__(self, row):
        self.row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _Conn:
    def __init__(self, row):
        self.row = row

    def cursor(self, *a, **k):
        return _Cur(self.row)

    def commit(self):
        pass


class _Mysql:
    def __init__(self, row):
        self.connection = _Conn(row)


ROW = {"widget_token": "tok1", "company_id": 7, "cid": 7, "company_name": "ACME", "is_active": 1,
       "allowed_domains": "kunde.dk", "widget_title": "Rådgiver", "theme_primary_color": "#0f766e"}


class WidgetOriginTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "..", "app1", "templates"))
        self.app.secret_key = "t"
        self.app.mysql = _Mysql(dict(ROW))
        self.app.register_blueprint(app1.app1_bp, url_prefix="/app1")
        self.c = self.app.test_client()
        p = mock.patch("branding_service.get_branding", return_value={})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(app1, "_widget_rate_exceeded", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.seen = {}

        def fake_agent(q, sess, sid_override=None, company_override=None):
            self.seen["sid"] = sid_override
            return Response("data: [DONE]\n\n", mimetype="text/event-stream")
        p = mock.patch("app1.agent.handle_agentic_ask", side_effect=fake_agent)
        p.start()
        self.addCleanup(p.stop)

    def _token(self, referer="https://www.kunde.dk/kurser"):
        r = self.c.get("/app1/widget/tok1", headers={"Referer": referer})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        marker = "WIDGET_SESSION = \""
        i = html.index(marker) + len(marker)
        return html[i:html.index("\"", i)], r

    def test_embed_on_foreign_parent_is_refused(self):
        r = self.c.get("/app1/widget/tok1", headers={"Referer": "https://ond.example/side"})
        self.assertEqual(r.status_code, 403)

    def test_embed_without_referer_is_refused_when_allowlisted(self):
        self.assertEqual(self.c.get("/app1/widget/tok1").status_code, 403)

    def test_allowed_parent_gets_token_and_frame_ancestors(self):
        tok, r = self._token()
        self.assertTrue(tok)
        self.assertIn("frame-ancestors", r.headers.get("Content-Security-Policy", ""))
        self.assertIn("kunde.dk", r.headers["Content-Security-Policy"])

    def test_ask_with_token_works_and_keeps_one_conversation(self):
        tok, _ = self._token()
        for _i in range(2):
            r = self.c.post("/app1/widget/tok1/ask", json={"query": "hej"}, headers={"X-Widget-Session": tok})
            self.assertEqual(r.status_code, 200)
        first = self.seen["sid"]
        self.c.post("/app1/widget/tok1/ask", json={"query": "igen"}, headers={"X-Widget-Session": tok})
        self.assertEqual(self.seen["sid"], first)
        self.assertTrue(first.startswith("widget_"))

    def test_ask_without_token_or_with_forged_token_is_403(self):
        self.assertEqual(self.c.post("/app1/widget/tok1/ask", json={"query": "hej"}).status_code, 403)
        r = self.c.post("/app1/widget/tok1/ask", json={"query": "hej"}, headers={"X-Widget-Session": "forged"})
        self.assertEqual(r.status_code, 403)

    def test_token_from_another_widget_is_rejected(self):
        with self.app.app_context():
            other = app1._widget_mint_session("tok2", "kunde.dk")
        r = self.c.post("/app1/widget/tok1/ask", json={"query": "hej"}, headers={"X-Widget-Session": other})
        self.assertEqual(r.status_code, 403)

    def test_open_widget_without_allowlist_still_works_without_token(self):
        self.app.mysql = _Mysql(dict(ROW, allowed_domains=""))
        r = self.c.post("/app1/widget/tok1/ask", json={"query": "hej"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
