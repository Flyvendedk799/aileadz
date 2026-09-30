"""N-6.4: AI usage metering through the credit ledger."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import credit_service as cs  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402

EXTRA = """
CREATE TABLE company_credit_accounts (company_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0,
  low_threshold INTEGER DEFAULT 100, limit_mode TEXT DEFAULT 'soft', low_notified INTEGER DEFAULT 0,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def make_db():
    db = SqliteMysql()
    db.raw.executescript(EXTRA)
    db.execute("INSERT INTO companies (id, company_name) VALUES (7, 'Firma A/S'), (8, 'Andet A/S')")
    for uid, name in ((1, "ada"), (2, "hr"), (3, "solo")):
        db.execute("INSERT INTO users (id, username, credits) VALUES (%s, %s, 50)", (uid, name))
    db.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES "
               "(7, 1, 'ada', 'employee', 'active'), (7, 2, 'hr', 'hr_manager', 'active')")
    return db


def charge(db, n_calls=1, **kw):
    kw.setdefault("username", "ada")
    kw.setdefault("company_id", 7)
    kw.setdefault("model", "gpt-4o")
    kw.setdefault("tokens_in", 4000)
    kw.setdefault("tokens_out", 800)
    total = 0
    for _ in range(n_calls):
        total += cs.charge_turn(db.connection, **kw)
    return total


class MeteringTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def test_turn_cost_comes_from_the_cost_model_with_a_minimum_of_one(self):
        self.assertEqual(cs.turn_credits("gpt-4o-mini", 10, 10), 1)
        self.assertGreater(cs.turn_credits("gpt-4o", 100000, 20000), 10)
        self.assertEqual(cs.turn_credits("ukendt-model", 1000, 1000), 1)

    def test_each_turn_deducts_from_the_company_and_writes_a_ledger_row(self):
        n = charge(self.db)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self.db.one("SELECT balance FROM company_credit_accounts WHERE company_id=7")["balance"], -n)
        row = self.db.one("SELECT username, assistant, kind, credits_used, model FROM credit_usage")
        self.assertEqual((row["username"], row["assistant"], row["kind"], row["credits_used"]), ("ada", "advisor", "usage", n))

    def test_ledger_integrity_balance_equals_sum_of_ledger(self):
        charge(self.db, 5)
        cs.grant(self.db.connection, amount=500, reason="Faktura 1", actor="admin", company_id=7)
        charge(self.db, 3, assistant="hr", username="hr")
        cur = self.db.connection.cursor()
        ok, bal, ledger = cs.verify_ledger(cur, 7)
        self.assertTrue(ok, (bal, ledger))

    def test_grant_records_reason_and_actor(self):
        res = cs.grant(self.db.connection, amount=250, reason="Faktura 2026-114", actor="tobias", company_id=7)
        self.assertTrue(res["success"])
        self.assertEqual(res["balance"], 250)
        g = self.db.one("SELECT credits_used, actor, description, kind FROM credit_usage")
        self.assertEqual((g["credits_used"], g["actor"], g["kind"]), (-250, "tobias", "grant"))
        self.assertIn("Faktura 2026-114", g["description"])

    def test_solo_user_uses_personal_balance(self):
        n = charge(self.db, username="solo", company_id=None)
        self.assertEqual(self.db.one("SELECT credits FROM users WHERE username='solo'")["credits"], 50 - n)
        self.assertIsNone(self.db.one("SELECT * FROM company_credit_accounts"))

    def test_vendor_turns_are_logged_but_not_billed(self):
        n = cs.charge_from_usage(self.db, username="vendor:4", company_id=None, agent_scope="vendor",
                                 model="gpt-4o", usage={"input_tokens": 3000, "output_tokens": 500})
        self.assertEqual(n, 0)
        row = self.db.one("SELECT assistant, credits_used FROM credit_usage")
        self.assertEqual((row["assistant"], row["credits_used"]), ("vendor", 0))

    def test_shadow_runs_are_not_metered(self):
        self.assertEqual(cs.charge_from_usage(self.db, username="ada", company_id=7, agent_scope="employee",
                                              model="claude-sonnet-5", usage={"input_tokens": 9}, runtime="anthropic-shadow"), 0)
        self.assertIsNone(self.db.one("SELECT * FROM credit_usage"))

    def test_usage_hook_reads_both_usage_shapes(self):
        a = cs.charge_from_usage(self.db, username="ada", company_id=7, agent_scope="employee", model="gpt-4o",
                                 usage={"input_tokens": 2000, "output_tokens": 300})
        b = cs.charge_from_usage(self.db, username="ada", company_id=7, agent_scope="employee", model="gpt-4o",
                                 usage={"prompt_tokens": 2000, "completion_tokens": 300})
        self.assertEqual(a, b)


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.app_patch = mock.patch("flask.current_app", mock.Mock(mysql=self.db))
        self.app_patch.start()
        self.addCleanup(self.app_patch.stop)

    def test_soft_limit_keeps_working_and_flags_negative_balance(self):
        charge(self.db, 3)
        self.assertIsNone(cs.guard(self.db, company_id=7, username="ada"))
        self.assertLess(self.db.one("SELECT balance FROM company_credit_accounts")["balance"], 0)

    def test_hard_limit_pauses_at_zero_with_a_friendly_message(self):
        cs.update_settings(self.db.connection, 7, limit_mode="hard")
        msg = cs.guard(self.db, company_id=7, username="ada")          # balance 0 -> paused
        self.assertIn("kreditter", msg)
        cs.grant(self.db.connection, amount=100, reason="x", actor="a", company_id=7)
        self.assertIsNone(cs.guard(self.db, company_id=7, username="ada"))

    def test_guard_fails_open_on_errors(self):
        class Boom:
            @property
            def connection(self):
                raise RuntimeError("db down")
        self.assertIsNone(cs.guard(Boom(), company_id=7))

    def test_hr_is_warned_once_when_balance_crosses_the_threshold(self):
        cs.grant(self.db.connection, amount=120, reason="start", actor="a", company_id=7)
        cs.update_settings(self.db.connection, 7, low_threshold=115)
        charge(self.db, 6)   # crosses 100 at some point and then keeps going down
        titles = [r["title"] for r in self.db.query("SELECT title FROM notifications WHERE user_id='hr'")]
        self.assertTrue(any("ved at løbe tør" in t or "brugt op" in t for t in titles))
        self.assertLessEqual(len([t for t in titles if "ved at løbe tør" in t]), 1)   # deduped
        self.assertEqual(self.db.query("SELECT 1 FROM notifications WHERE user_id='ada'"), [])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        charge(self.db, 2, username="ada")
        charge(self.db, 1, username="hr", assistant="hr")
        charge(self.db, 1, username="outsider", company_id=8)

    def test_company_summary_is_scoped_per_user_and_assistant(self):
        cur = self.db.connection.cursor()
        s = cs.usage_summary(cur, company_id=7, days=30)
        self.assertEqual(s["turns"], 3)
        self.assertEqual({u["username"] for u in s["by_user"]}, {"ada", "hr"})     # other tenant excluded
        self.assertEqual({a["assistant"] for a in s["by_assistant"]}, {"advisor", "hr"})

    def test_personal_summary_only_shows_own_usage(self):
        cur = self.db.connection.cursor()
        s = cs.usage_summary(cur, username="ada", days=30)
        self.assertEqual(s["turns"], 2)
        self.assertEqual(s["by_user"], [])                                        # no colleague breakdown

    def test_admin_overview_has_burn_rate_and_runway(self):
        cs.grant(self.db.connection, amount=1000, reason="top-up", actor="a", company_id=7)
        cur = self.db.connection.cursor()
        ov = cs.admin_overview(cur, days=30)
        row = next(c for c in ov["companies"] if c["id"] == 7)
        self.assertGreater(row["burn_per_day"], 0)
        self.assertIsNotNone(row["runway_days"])
        self.assertTrue(ov["recent_grants"])


class RouteBoundaryTests(unittest.TestCase):
    def _client(self, **sess):
        import run
        app = run.create_app()
        c = app.test_client()
        with c.session_transaction() as s:
            s.update(sess)
        return c

    def test_employee_cannot_open_company_credits(self):
        c = self._client(user="ada", user_id=1, company_id=7, company_role="employee")
        r = c.get("/hr/credits")
        self.assertEqual(r.status_code, 302)
        self.assertIn("min-laering", r.headers["Location"])

    def test_employee_cannot_change_settings(self):
        c = self._client(user="ada", user_id=1, company_id=7, company_role="employee")
        r = c.post("/hr/credits/settings", data={"limit_mode": "hard"})
        self.assertEqual(r.status_code, 403)

    def test_non_admin_cannot_grant(self):
        c = self._client(user="hr", user_id=2, company_id=7, company_role="hr_manager")
        r = c.post("/admin/credits/grant", data={"company_id": 7, "amount": 99999})
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertNotIn("credits/companies", r.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
