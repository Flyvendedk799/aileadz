"""N-2.1: forgot password, reset, invite, login by e-mail, tenant join."""

import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import run  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class FakeTokens(types.ModuleType):
    """In-memory stand-in for account_tokens (same public API)."""

    def __init__(self):
        super().__init__("account_tokens")
        self.store = {}
        self.n = 0

    def create_token(self, kind, subject_id, ttl_minutes=60):
        self.n += 1
        raw = "tok-%s-%d" % (kind, self.n)
        self.store[raw] = (kind, subject_id)
        return raw

    def consume_token(self, kind, raw):
        item = self.store.get(raw)
        if not item or item[0] != kind:
            return None
        del self.store[raw]
        return item[1]

    def peek_token(self, kind, raw):
        item = self.store.get(raw)
        return item[1] if item and item[0] == kind else None


class Base(unittest.TestCase):
    def setUp(self):
        self.tokens = FakeTokens()
        patcher = mock.patch.dict(sys.modules, {"account_tokens": self.tokens})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = SqliteMysql()
        self.db.execute("INSERT INTO users (id, username, password, email) VALUES (1, 'ada', %s, 'ada@firma.dk')",
                        (generate_password_hash("gammelt-kodeord"),))
        app = run.create_app()
        app.config["TESTING"] = True
        app.mysql = self.db
        self.app = app
        self.client = app.test_client()
        self.mails = []
        p = mock.patch("email_service.send_branded_email",
                       side_effect=lambda to, subj, tpl, branding=None, **kw: self.mails.append((to, tpl, kw)) or True)
        p.start()
        self.addCleanup(p.stop)


class ForgotPasswordTests(Base):
    def test_known_address_gets_a_link(self):
        resp = self.client.post("/glemt-adgangskode", data={"email": "ada@firma.dk"})
        self.assertIn("har vi sendt et link", resp.get_data(as_text=True))
        self.assertEqual(len(self.mails), 1)
        to, tpl, kw = self.mails[0]
        self.assertEqual((to, tpl), ("ada@firma.dk", "password_reset"))
        self.assertIn("/nulstil-adgangskode/tok-password_reset-1", kw["reset_url"])

    def test_unknown_address_gets_the_same_answer_and_no_mail(self):
        resp = self.client.post("/glemt-adgangskode", data={"email": "ingen@firma.dk"})
        self.assertIn("har vi sendt et link", resp.get_data(as_text=True))
        self.assertEqual(self.mails, [])

    def test_bad_input_is_rejected_politely(self):
        resp = self.client.post("/glemt-adgangskode", data={"email": "ikke-en-mail"})
        self.assertIn("e-mailadresse", resp.get_data(as_text=True))
        self.assertNotIn("har vi sendt", resp.get_data(as_text=True))

    def test_login_page_links_to_the_flow(self):
        self.assertIn("/glemt-adgangskode", self.client.get("/login").get_data(as_text=True))


class ResetAndInviteTests(Base):
    def _token(self, kind="password_reset"):
        return self.tokens.create_token(kind, 1)

    def test_valid_reset_sets_a_hashed_password_and_is_single_use(self):
        t = self._token()
        self.assertEqual(self.client.get("/nulstil-adgangskode/" + t).status_code, 200)
        resp = self.client.post("/nulstil-adgangskode/" + t, data={"password": "et-langt-nyt-kodeord", "password2": "et-langt-nyt-kodeord"})
        self.assertEqual(resp.status_code, 302)
        stored = self.db.one("SELECT password FROM users WHERE id=1")["password"]
        self.assertTrue(check_password_hash(stored, "et-langt-nyt-kodeord"))
        again = self.client.post("/nulstil-adgangskode/" + t, data={"password": "endnu-et-langt-et", "password2": "endnu-et-langt-et"})
        self.assertEqual(again.status_code, 400)
        self.assertIn("Linket virker ikke", again.get_data(as_text=True))

    def test_expired_or_unknown_link_shows_a_friendly_page(self):
        resp = self.client.get("/nulstil-adgangskode/findes-ikke")
        self.assertIn("Linket virker ikke", resp.get_data(as_text=True))
        self.assertIn("Få et nyt link", resp.get_data(as_text=True))

    def test_policy_and_mismatch_are_enforced_without_consuming_the_token(self):
        t = self._token()
        short = self.client.post("/nulstil-adgangskode/" + t, data={"password": "kort", "password2": "kort"})
        self.assertEqual(short.status_code, 400)
        self.assertIn("mindst 10 tegn", short.get_data(as_text=True))
        diff = self.client.post("/nulstil-adgangskode/" + t, data={"password": "et-langt-kodeord", "password2": "et-andet-kodeord"})
        self.assertIn("ikke ens", diff.get_data(as_text=True))
        ok = self.client.post("/nulstil-adgangskode/" + t, data={"password": "et-langt-kodeord", "password2": "et-langt-kodeord"})
        self.assertEqual(ok.status_code, 302)       # the token survived the two failed attempts

    def test_a_reset_token_cannot_be_used_as_an_invite_and_vice_versa(self):
        t = self._token("password_reset")
        resp = self.client.post("/invitation/" + t, data={"password": "et-langt-kodeord", "password2": "et-langt-kodeord"})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(check_password_hash(self.db.one("SELECT password FROM users WHERE id=1")["password"], "gammelt-kodeord"))

    def test_invite_lets_the_employee_choose_their_own_password(self):
        t = self._token("invite")
        page = self.client.get("/invitation/" + t).get_data(as_text=True)
        self.assertIn("Vælg din adgangskode", page)
        resp = self.client.post("/invitation/" + t, data={"password": "min-egen-adgangskode", "password2": "min-egen-adgangskode"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(check_password_hash(self.db.one("SELECT password FROM users WHERE id=1")["password"], "min-egen-adgangskode"))

    def test_send_invite_emails_a_set_password_link(self):
        import account_flows
        with self.app.test_request_context("/"):
            ok = account_flows.send_invite(1, email="ny@firma.dk", name="Ny", company={"id": 7, "company_name": "Firma"})
        self.assertTrue(ok)
        self.assertEqual([k for k in self.tokens.store.values()][-1][0], "invite")
        to, tpl, kw = self.mails[-1]
        self.assertEqual((to, tpl), ("ny@firma.dk", "welcome"))
        self.assertIn("/invitation/tok-invite-", kw["set_password_url"])


class PasswordPolicyTests(unittest.TestCase):
    def test_policy(self):
        import account_flows as af
        self.assertIsNotNone(af.password_problem("kort"))
        self.assertIsNotNone(af.password_problem("aaaaaaaaaaaa"))
        self.assertIsNotNone(af.password_problem("ada@firma.dk", email="ada@firma.dk"))
        self.assertIsNone(af.password_problem("et-helt-fint-kodeord"))


class LoginAndTenantTests(Base):
    def test_login_accepts_email_as_the_form_promises(self):
        resp = self.client.post("/login", data={"username": "ADA@firma.dk", "password": "gammelt-kodeord"})
        self.assertEqual(resp.status_code, 302)
        with self.client.session_transaction() as s:
            self.assertEqual(s.get("user"), "ada")

    def test_register_joins_the_company_only_for_its_verified_domain(self):
        self.db.execute("INSERT INTO companies (id, company_name, company_slug, status) VALUES (7, 'Firma A/S', 'firma', 'active')")
        self.db.raw.execute("ALTER TABLE companies ADD COLUMN company_domain TEXT")
        self.db.execute("UPDATE companies SET company_domain = 'firma.dk' WHERE id = 7")
        good = self.client.post("/register?tenant=firma", data={"username": "bo", "password": "et-langt-kodeord", "email": "bo@firma.dk"})
        self.assertEqual(good.status_code, 302)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM company_users WHERE username='bo'")["c"], 1)
        bad = self.client.post("/register?tenant=firma", data={"username": "cy", "password": "et-langt-kodeord", "email": "cy@gmail.com"})
        self.assertEqual(bad.status_code, 302)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM company_users WHERE username='cy'")["c"], 0)


if __name__ == "__main__":
    unittest.main()
