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


class PublicFormTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.db.raw.close)
        self.client = self.app.test_client()

    def test_invalid_demo_post_keeps_typed_values_with_field_errors(self):
        r = self.client.post("/for-virksomheder", data={
            "name": "Mette Krogh", "email": "ikke-en-mail", "company_name": "Krogh A/S", "message": "Vi vil gerne have en demo",
            "contact_consent": "yes"})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn('value="Mette Krogh"', html)
        self.assertIn('value="Krogh A/S"', html)
        self.assertIn('value="ikke-en-mail"', html)
        self.assertIn("Vi vil gerne have en demo", html)
        self.assertIn("gyldig e-mailadresse", html)
        self.assertEqual(self.db.query("SELECT * FROM sales_enquiries"), [])

    def test_valid_demo_post_shows_one_confirmation(self):
        r = self.client.post("/for-virksomheder", data={
            "name": "Mette", "email": "m@krogh.dk", "company_name": "Krogh", "contact_consent": "yes"}, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertEqual(html.count("Din forespørgsel er registreret"), 1)
        self.assertNotIn("fm-flash", html)

    def test_register_error_keeps_username_and_email_but_never_the_password(self):
        r = self.client.post("/register", data={"username": "newbie", "email": "n@x.dk", "password": "kort-pw9"})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn('value="newbie"', html)
        self.assertIn('value="n@x.dk"', html)
        self.assertNotIn("kort-pw9", html)
        self.assertIn("mindst 10 tegn", html)
