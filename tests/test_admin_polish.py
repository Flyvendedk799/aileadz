"""N-6.2 / N-6.5: submission flow, admin lists (search + pagination), user actions."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402


class AdminBase(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.addCleanup(self._ctx.pop)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO users (id, username, email, password, role) VALUES "
                  "(1, 'root', 'root@x.dk', 'pw', 'admin'), (2, 'ada', 'ada@x.dk', 'pw', 'user')")
        d.execute("INSERT INTO companies (id, company_name, company_slug) VALUES (7, 'Alfa A/S', 'alfa'), (8, 'Beta ApS', 'beta')")
        d.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES (7, 2, 'ada', 'employee', 'active')")
        d.execute("INSERT INTO vendors (id, vendor_name, slug, contact_email, status) VALUES "
                  "(11, 'Kursus ApS', 'kursus', 'v@k.dk', 'active'), (12, 'Ventende', 'vent', 'p@k.dk', 'pending'), "
                  "(13, 'Sus', 'sus', 's@k.dk', 'suspended')")
        self.admin = client_as(self.app, user="root", user_id=1, role="admin")

    def mails(self):
        return mock.patch("email_service.send_branded_email", return_value=True)


class SubmissionFlowTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO vendor_submissions (id, vendor_id, job_id, filename, row_count) "
                        "VALUES (1, 11, 'job1', 'k.csv', 3)")

    def test_approve_goes_through_the_preview_and_does_not_mark_approved_yet(self):
        with mock.patch("catalog_service.get_import_draft", return_value={"job_id": "job1", "products": []}):
            r = self.admin.post("/admin/vendors/submission/1/approved")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/catalog/import/job1", r.headers["Location"])
        self.assertEqual(self.db.one("SELECT status FROM vendor_submissions WHERE id=1")["status"], "pending")

    def test_confirming_the_import_approves_and_mails_the_vendor(self):
        with mock.patch("catalog_service.confirm_import_draft", return_value={"job_id": "job1"}), self.mails() as mail:
            self.admin.post("/admin/catalog/import/job1/confirm")
        self.assertEqual(self.db.one("SELECT status FROM vendor_submissions WHERE id=1")["status"], "approved")
        self.assertEqual(mail.call_args.args[2], "vendor_submission_result")
        self.assertTrue(mail.call_args.kwargs["approved"])

    def test_reject_marks_discards_draft_and_mails_with_note(self):
        with mock.patch("catalog_service.get_import_draft", return_value={"job_id": "job1"}), \
                mock.patch("catalog_service.delete_import_draft") as drop, self.mails() as mail:
            self.admin.post("/admin/vendors/submission/1/rejected", data={"note": "Priser mangler"})
        self.assertEqual(self.db.one("SELECT status FROM vendor_submissions WHERE id=1")["status"], "rejected")
        drop.assert_called_once_with("job1")
        self.assertFalse(mail.call_args.kwargs["approved"])
        self.assertEqual(mail.call_args.kwargs["note"], "Priser mangler")

    def test_label_is_godkend_og_importer(self):
        html = self.admin.get("/admin/vendors").get_data(as_text=True)
        self.assertIn("Godkend &amp; importér", html)

    def test_non_admin_cannot_decide_submissions(self):
        emp = client_as(self.app, user="ada", user_id=2, company_id=7, company_role="company_admin")
        emp.post("/admin/vendors/submission/1/rejected")
        self.assertEqual(self.db.one("SELECT status FROM vendor_submissions WHERE id=1")["status"], "pending")


class ResendInviteTests(AdminBase):
    def test_pending_vendor_gets_a_fresh_invite_token(self):
        with self.mails() as mail:
            self.admin.post("/admin/vendors/12/resend-invite")
        tok = self.db.one("SELECT invite_token FROM vendors WHERE id=12")["invite_token"]
        self.assertTrue(tok)
        self.assertEqual(mail.call_args.args[2], "vendor_invite")

    def test_active_vendor_gets_a_reset_link_and_suspended_is_refused(self):
        with self.mails() as mail, mock.patch("password_tokens.issue_token", return_value="tok123"):
            self.admin.post("/admin/vendors/11/resend-invite")
            self.assertEqual(mail.call_args.args[2], "password_reset")
            mail.reset_mock()
            self.admin.post("/admin/vendors/13/resend-invite")
            mail.assert_not_called()


class ListTests(AdminBase):
    def test_vendor_search_and_pager(self):
        for i in range(40):
            self.db.execute("INSERT INTO vendors (vendor_name, slug, contact_email, status) VALUES (%s,%s,%s,'active')",
                            ("Udbyder %02d" % i, "u%d" % i, "u%d@x.dk" % i))
        html = self.admin.get("/admin/vendors").get_data(as_text=True)
        self.assertIn("Side 1 af", html)
        page2 = self.admin.get("/admin/vendors?page=2").get_data(as_text=True)
        self.assertIn("Side 2 af", page2)
        found = self.admin.get("/admin/vendors?q=Udbyder 07").get_data(as_text=True)
        self.assertIn("Udbyder 07", found)
        self.assertNotIn("Udbyder 08", found)

    def test_company_search(self):
        html = self.admin.get("/companies/admin?q=beta").get_data(as_text=True)
        self.assertIn("Beta ApS", html)
        self.assertNotIn("Alfa A/S", html)
        none = self.admin.get("/companies/admin?q=zzz").get_data(as_text=True)
        self.assertIn("Ingen virksomheder matcher", none)

    def test_audit_log_search_and_paging(self):
        for i in range(120):
            self.db.execute("INSERT INTO audit_log (company_id, user_id, action, action_type, description) "
                            "VALUES (7, 1, 'x', 'test.event', %s)", ("hændelse nr %d" % i,))
        self.assertIn("Side 1 af 3", self.admin.get("/admin/log").get_data(as_text=True))
        found = self.admin.get("/admin/log?q=nr 119").get_data(as_text=True)
        self.assertIn("hændelse nr 119", found)
        self.assertNotIn("hændelse nr 118", found)

    def test_lists_are_admin_only(self):
        emp = client_as(self.app, user="ada", user_id=2, company_id=7, company_role="hr_manager")
        for url in ("/admin/vendors", "/admin/log", "/admin/agreements", "/companies/admin", "/admin/users"):
            self.assertIn(emp.get(url).status_code, (302, 401, 403), url)


class UserActionTests(AdminBase):
    def test_send_reset_creates_single_use_token_and_mails(self):
        with self.mails() as mail, mock.patch("password_tokens.issue_token", return_value="tok123") as issue:
            r = self.admin.post("/admin/users/2/send-reset")
        self.assertTrue(r.get_json()["success"])
        self.assertEqual(mail.call_args.args[2], "password_reset")
        self.assertIn("reset_url", mail.call_args.kwargs)
        self.assertEqual(issue.call_args.args[1:3], ("user", 2))           # Part A token store (S-2.4)
        self.assertEqual(issue.call_args.kwargs["purpose"], "reset")

    def test_deactivate_blocks_login_and_reactivate_restores(self):
        r = self.admin.post("/admin/users/2/deactivate")
        self.assertTrue(r.get_json()["deactivated"])
        self.assertEqual(self.db.one("SELECT status FROM company_users WHERE user_id=2")["status"], "inactive")
        c = self.app.test_client()
        login = c.post("/login", data={"username": "ada", "password": "pw"})
        self.assertEqual(login.status_code, 302)
        with c.session_transaction() as s:
            self.assertNotIn("user", s)
        r2 = self.admin.post("/admin/users/2/deactivate")
        self.assertFalse(r2.get_json()["deactivated"])
        self.assertEqual(self.db.one("SELECT status FROM company_users WHERE user_id=2")["status"], "active")

    def test_admin_cannot_deactivate_self_and_others_cannot_at_all(self):
        self.assertEqual(self.admin.post("/admin/users/1/deactivate").status_code, 400)
        emp = client_as(self.app, user="ada", user_id=2, company_id=7, company_role="company_admin")
        emp.post("/admin/users/1/deactivate")
        self.assertEqual(self.db.one("SELECT status FROM users WHERE id=1")["status"], "active")


if __name__ == "__main__":
    unittest.main()


class CrossTenantViewTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO course_orders (order_id, company_id, user_id, username, product_title, price, status) "
                        "VALUES ('FM-1', 8, 2, 'ada', 'Excel kursus', 1500, 'approved')")
        self.db.execute("UPDATE course_orders SET created_at = NULL, updated_at = NULL")   # sqlite returns str, MySQL datetime
        self.user = client_as(self.app, user="ada", user_id=2, company_id=7, company_role="employee")

    def test_admin_sees_any_tenants_order(self):
        r = self.admin.get("/admin/orders/FM-1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Excel kursus", r.get_data(as_text=True))

    def test_unknown_order_redirects_with_danish_flash(self):
        r = self.admin.get("/admin/orders/NOPE")
        self.assertEqual(r.status_code, 302)

    def test_order_detail_and_ai_quality_are_admin_only(self):
        for url in ("/admin/orders/FM-1", "/admin/ai-quality"):
            r = self.user.get(url)
            self.assertIn(r.status_code, (302, 401, 403), url)
            self.assertNotIn("Excel kursus", r.get_data(as_text=True))

    def test_ai_quality_renders_for_admin_even_with_no_data(self):
        self.assertEqual(self.admin.get("/admin/ai-quality?days=7").status_code, 200)
