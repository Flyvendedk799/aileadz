"""N-6.1: vendors receive and act on their own orders; forgot password tokens."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import account_tokens  # noqa: E402
import order_service as svc  # noqa: E402
from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402


class VendorBase(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.addCleanup(self._ctx.pop)
        self.events, self.emails = [], []
        for p in (
            mock.patch.object(svc, "_emit_event_safe", side_effect=lambda c, t, pl: self.events.append(t)),
            mock.patch.object(svc, "_send_email_safe", side_effect=lambda to, s, tpl, c, **kw: self.emails.append((to, tpl))),
            mock.patch.object(svc, "_manager_recipient_emails", return_value=[]),
            render_patches(),
        ):
            p.start()
            self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id, company_name) VALUES (7, 'Firma A/S')")
        d.execute("INSERT INTO users (id, username, email) VALUES (1, 'ada', 'ada@f.dk'), (2, 'hr', 'hr@f.dk')")
        d.execute("INSERT INTO company_users (company_id, user_id, username, role, department, manager_user_id) "
                  "VALUES (7, 1, 'ada', 'employee', 'Salg', NULL), (7, 2, 'hr', 'hr_manager', 'HR', NULL)")
        d.execute("INSERT INTO vendors (id, vendor_name, slug, contact_email, status) VALUES "
                  "(11, 'Kursus ApS', 'kursus-aps', 'v@k.dk', 'active'), (12, 'Anden', 'anden', 'a@u.dk', 'active'), "
                  "(13, 'Suspenderet', 'sus', 's@u.dk', 'suspended')")
        self.o_mine = self._order(11)
        self.o_other = self._order(12, handle="andet")
        d.execute("UPDATE course_orders SET status='approved'")

    def _order(self, vendor_id, handle="prince2"):
        ctx = svc.OrderContext(company_id=7, user_id=2, username="hr", company_role="hr_manager", department="HR")
        with mock.patch.object(svc, "_vendor_id_for_handle", return_value=vendor_id):
            r = svc.create_order(ctx, product_handle=handle, product_title="Kursus " + handle, price=1000,
                                 variant_date="2026-12-01", user_email="ada@f.dk", user_name="Ada")
        self.db.execute("UPDATE course_orders SET user_name='Ada', user_email='ada@f.dk' WHERE order_id=%s", (r["order_id"],))
        return r["order_id"]

    def vendor_client(self, vid=11, name="Kursus ApS"):
        return client_as(self.app, user_type="vendor", vendor_id=vid, vendor_name=name)

    def status(self, oid):
        return self.db.one("SELECT status FROM course_orders WHERE order_id=%s", (oid,))["status"]


class VendorOrdersTests(VendorBase):
    def test_orders_page_lists_only_own_orders(self):
        html = self.vendor_client().get("/vendor/orders").get_data(as_text=True)
        self.assertIn("Kursus prince2", html)
        self.assertNotIn("Kursus andet", html)
        self.assertIn("Bekræft plads", html)

    def test_vendor_books_own_order_and_learner_and_hr_are_told(self):
        r = self.vendor_client().post("/vendor/orders/%s/book" % self.o_mine, data={})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.status(self.o_mine), "booked")
        self.assertIn("order.booked", self.events)
        self.assertTrue(any(tpl == "order_booked" for _, tpl in self.emails))
        self.assertEqual(self.db.one("SELECT booked_by FROM course_orders WHERE order_id=%s", (self.o_mine,))["booked_by"], "Kursus ApS")

    def test_vendor_cannot_act_on_another_vendors_order(self):
        for action in ("book", "decline", "complete"):
            self.vendor_client().post("/vendor/orders/%s/%s" % (self.o_other, action), data={"reason": "x"})
            self.assertEqual(self.status(self.o_other), "approved", action)

    def test_decline_requires_reason_then_cancels_and_notifies_hr(self):
        c = self.vendor_client()
        c.post("/vendor/orders/%s/decline" % self.o_mine, data={"reason": ""})
        self.assertEqual(self.status(self.o_mine), "approved")
        c.post("/vendor/orders/%s/decline" % self.o_mine, data={"reason": "Holdet er aflyst"})
        self.assertEqual(self.status(self.o_mine), "cancelled")
        hr = [n["title"] for n in self.db.query("SELECT title FROM notifications WHERE user_id='hr'")]
        self.assertIn("Bestilling annulleret", hr)
        self.assertEqual(self.db.one("SELECT cancel_reason FROM course_orders WHERE order_id=%s", (self.o_mine,))["cancel_reason"],
                         "Holdet er aflyst")

    def test_vendor_marks_attended_after_booking(self):
        c = self.vendor_client()
        c.post("/vendor/orders/%s/book" % self.o_mine, data={})
        c.post("/vendor/orders/%s/complete" % self.o_mine, data={})
        self.assertEqual(self.status(self.o_mine), "completed")
        self.assertIn("course.completed", self.events)

    def test_suspended_vendor_is_locked_out_even_with_a_live_session(self):
        r = self.vendor_client(13, "Suspenderet").get("/vendor/orders")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/vendor/login", r.headers["Location"])

    def test_anonymous_and_employee_sessions_cannot_reach_vendor_orders(self):
        self.assertEqual(self.app.test_client().get("/vendor/orders").status_code, 302)
        emp = client_as(self.app, user="ada", user_id=1, company_id=7, company_role="employee")
        self.assertEqual(emp.get("/vendor/orders").status_code, 302)
        self.assertEqual(emp.post("/vendor/orders/%s/book" % self.o_mine).status_code, 302)
        self.assertEqual(self.status(self.o_mine), "approved")

    def test_badge_counts_unconfirmed_orders_only_for_this_vendor(self):
        from vendor_portal import awaiting_booking_count
        with self.app.app_context():
            self.assertEqual(awaiting_booking_count(11), 1)
            self.assertEqual(awaiting_booking_count(12), 1)
        self.vendor_client().post("/vendor/orders/%s/book" % self.o_mine, data={})
        with self.app.app_context():
            self.assertEqual(awaiting_booking_count(11), 0)

    def test_empty_state_has_a_next_step(self):
        self.db.execute("UPDATE course_orders SET vendor_id = 99")
        html = self.vendor_client().get("/vendor/orders").get_data(as_text=True)
        self.assertIn("Ingen bestillinger", html)
        self.assertIn("Indsend flere kurser", html)


class AccountTokenTests(VendorBase):
    def test_token_is_single_use_and_stored_hashed(self):
        with self.app.app_context():
            raw = account_tokens.create_token("vendor_reset", 11)
            self.assertNotIn(raw, str(self.db.query("SELECT * FROM account_tokens")))
            self.assertEqual(account_tokens.consume_token("vendor_reset", raw), 11)
            self.assertIsNone(account_tokens.consume_token("vendor_reset", raw))
            self.assertIsNone(account_tokens.consume_token("other_kind", raw))

    def test_expired_token_is_rejected(self):
        with self.app.app_context():
            raw = account_tokens.create_token("vendor_reset", 11)
            self.db.execute("UPDATE account_tokens SET expires_at = datetime('now', '-1 minutes')")
            self.assertIsNone(account_tokens.consume_token("vendor_reset", raw))

    def test_new_token_revokes_the_old_one(self):
        with self.app.app_context():
            old = account_tokens.create_token("vendor_reset", 11)
            account_tokens.create_token("vendor_reset", 11)
            self.assertIsNone(account_tokens.consume_token("vendor_reset", old))


class VendorResetLinkTests(VendorBase):
    """The forgot/reset SCREENS are Part A's (S-2.4); the admin button mints the link."""

    def test_send_vendor_reset_link_mints_a_token_and_mails_the_link(self):
        from vendor_portal import send_vendor_reset_link
        with mock.patch("email_service.send_branded_email", return_value=True) as mail, \
                self.app.test_request_context("/"):
            ok = send_vendor_reset_link({"id": 11, "contact_email": "v@k.dk"})
        self.assertTrue(ok)
        self.assertEqual(mail.call_args.args[2], "password_reset")
        self.assertIn("/vendor/reset-password/", mail.call_args.kwargs["reset_url"])
        token = mail.call_args.kwargs["reset_url"].rsplit("/", 1)[1]
        self.assertEqual(account_tokens.peek_token("vendor_reset", token), 11)


if __name__ == "__main__":
    unittest.main()
