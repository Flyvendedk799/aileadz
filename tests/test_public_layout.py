"""Logged-out pages render the public header, not the account sidebar."""
import unittest

from tests.sqlite_platform import PlatformDB, make_app, client_as, render_patches

ACCOUNT_ONLY = ("Mine bestillinger", "Notifikationer", "Profil &amp; CV", "data-notif-dot", "unread_count")


class PublicLayoutTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.db.raw.close)
        self.anon = self.app.test_client()

    def test_public_pages_have_public_header_and_no_account_navigation(self):
        for path in ("/catalog", "/chat", "/for-virksomheder", "/about"):
            with self.subTest(path=path):
                r = self.anon.get(path, follow_redirects=False)
                self.assertEqual(r.status_code, 200, path)
                html = r.get_data(as_text=True)
                self.assertIn('data-auth="0"', html)
                self.assertIn("fm-pubnav", html)
                self.assertIn("Opret konto", html)
                self.assertIn("favicon.ico", html)
                for needle in ACCOUNT_ONLY:
                    self.assertNotIn(needle, html, f"{needle} on {path}")

    def test_logged_in_pages_keep_the_account_sidebar(self):
        c = client_as(self.app, user="learner", user_id=1)
        html = c.get("/for-virksomheder").get_data(as_text=True)
        self.assertIn('data-auth="1"', html)
        self.assertIn("Mine bestillinger", html)
        self.assertNotIn("fm-pubnav", html)
