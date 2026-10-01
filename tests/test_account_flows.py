"""N-2.1: forgot password, reset, invite, login by e-mail, tenant join."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import run  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.issued = []
        patcher = mock.patch("password_tokens.issue_token",
                             side_effect=lambda conn, typ, aid, purpose="reset", ttl_minutes=None:
                             self.issued.append((typ, aid, purpose)) or "tok-%s-%s" % (purpose, aid))
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


class LinkHelperTests(Base):
    """Reset/invite links are sent through Part A's token store (S-2.4)."""

    def test_reset_link_uses_part_a_token_and_route(self):
        import account_flows
        with self.app.test_request_context("/"):
            ok = account_flows.send_reset_link(1, actor="hr")
        self.assertTrue(ok)
        self.assertEqual(self.issued, [("user", 1, "reset")])
        to, tpl, kw = self.mails[0]
        self.assertEqual((to, tpl), ("ada@firma.dk", "password_reset"))
        self.assertIn("/reset-password/tok-reset-1", kw["reset_url"])

    def test_reset_link_for_unknown_user_or_missing_email_sends_nothing(self):
        import account_flows
        self.db.execute("INSERT INTO users (id, username, password, email) VALUES (5, 'noemail', 'x', '')")
        with self.app.test_request_context("/"):
            self.assertFalse(account_flows.send_reset_link(999))
            self.assertFalse(account_flows.send_reset_link(5))
        self.assertEqual(self.mails, [])

    def test_send_invite_emails_a_set_password_link(self):
        import account_flows
        with self.app.test_request_context("/"):
            ok = account_flows.send_invite(1, email="ada@firma.dk", name="Ada", username="ada",
                                           company={"id": 7, "company_name": "Firma"})
        self.assertTrue(ok)
        self.assertEqual(self.issued, [("user", 1, "invite")])
        to, tpl, kw = self.mails[-1]
        self.assertEqual((to, tpl), ("ada@firma.dk", "password_invite"))
        self.assertIn("/set-password/tok-invite-1", kw["set_password_url"])

    def test_login_page_links_to_forgot_password(self):
        self.assertIn("/forgot-password", self.client.get("/login").get_data(as_text=True))


class HrResetLinkTests(Base):
    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO companies (id, company_name, status) VALUES (7, 'Firma', 'active')")
        self.db.execute("INSERT INTO users (id, username, password, email) VALUES (2, 'hr', 'x', 'hr@firma.dk')")
        self.db.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES "
                        "(7, 1, 'ada', 'employee', 'active'), (7, 2, 'hr', 'hr_manager', 'active')")
        self.db.raw.executescript("CREATE TABLE IF NOT EXISTS audit_log2 (id INTEGER)")

    def _hr(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s.update(user="hr", user_id=2, company_id=7, company_role="hr_manager")
        return c

    def test_hr_sends_a_link_instead_of_reading_out_a_password(self):
        with mock.patch("white_label_global_integration.get_template_context", return_value={}):
            resp = self._hr().post("/hr/employee/1/reset-password", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.mails[-1][:2], ("ada@firma.dk", "password_reset"))
        # the stored password is untouched: no plaintext ever handled by a human
        self.assertTrue(check_password_hash(self.db.one("SELECT password FROM users WHERE id=1")["password"], "gammelt-kodeord"))

    def test_employee_cannot_trigger_it_for_a_colleague(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s.update(user="ada", user_id=1, company_id=7, company_role="employee")
        resp = c.post("/hr/employee/2/reset-password")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.mails, [])


class LoginAndTenantTests(Base):
    def test_login_accepts_email_as_the_form_promises(self):
        with mock.patch("two_factor.is_enabled", return_value=False):
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
