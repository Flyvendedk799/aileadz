"""N-2.3: navigation per role, read from the capability helper."""

import os
import unittest

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

from unittest import mock  # noqa: E402

import capabilities  # noqa: E402
import run  # noqa: E402


def _html(role=None, company_role=None, admin=False, company=True):
    app = run.create_app()
    client = app.test_client()
    with client.session_transaction() as s:
        if role or company_role or admin:
            s["user"] = "tester"
            s["user_id"] = 1
        if company:
            s["company_id"] = 7
            s["company_name"] = "Firma A/S"
        if company_role:
            s["company_role"] = company_role
        if admin:
            s["role"] = "admin"
            s["twofa_ok"] = True
    # The white-label context processor reads the tenant's branding from MySQL;
    # there is no database in a unit test, so feed it "no custom branding".
    with mock.patch("white_label_global_integration.get_template_context", return_value={}):
        return client.get("/privacy").get_data(as_text=True)   # public page, renders the shared shell


def _s(**kw):
    """A signed-in session dict (the capability matrix needs a user and a tenant)."""
    base = {"user": "tester", "user_id": 1, "company_id": 7}
    base.update(kw)
    return base


class CapabilityHelperTests(unittest.TestCase):
    def test_role_hierarchy(self):
        self.assertFalse(capabilities.can("company.workspace", _s(company_role="employee")))
        self.assertTrue(capabilities.can("company.workspace", _s(company_role="department_head")))
        self.assertFalse(capabilities.can("company.billing", _s(company_role="department_head")))
        self.assertTrue(capabilities.can("company.billing", _s(company_role="hr_manager")))
        self.assertTrue(capabilities.can("company.sso", _s(company_role="company_admin")))
        self.assertFalse(capabilities.can("company.sso", _s(company_role="hr_manager")))
        self.assertTrue(capabilities.can("company.sso", _s(role="admin")))

    def test_unknown_capability_and_roles_fail_closed(self):
        self.assertFalse(capabilities.can("company.nonsense", _s(company_role="company_admin")))
        self.assertEqual(capabilities.effective_role({"company_role": "intern"}), "employee")
        self.assertFalse(capabilities.can("platform.admin", _s(company_role="company_admin")))


class SidebarTests(unittest.TestCase):
    def test_employee_sees_learning_nav_and_no_company_block(self):
        html = _html(company_role="employee")
        self.assertIn("Min læring", html)
        self.assertIn("Mine bestillinger", html)
        self.assertIn("Udviklingsmål", html)
        self.assertNotIn(">Virksomhed<", html)
        self.assertNotIn('href="/hr/"', html)
        self.assertNotIn("Virksomhedsindstillinger", html)
        self.assertNotIn(">Admin<", html)

    def test_hr_manager_sees_company_block_and_still_gets_min_laering(self):
        html = _html(company_role="hr_manager")
        self.assertIn("Min læring", html)            # every learner, managers included
        self.assertIn(">Virksomhed<", html)
        self.assertIn("Godkendelser", html)
        self.assertIn("HR-assistent", html)
        self.assertIn("/virksomhed/indstillinger", html)
        self.assertNotIn(">Admin<", html)

    def test_department_head_has_no_settings_link(self):
        html = _html(company_role="department_head")
        self.assertIn(">Virksomhed<", html)
        self.assertNotIn("/virksomhed/indstillinger", html)

    def test_solo_user_has_learning_nav_without_company_block(self):
        html = _html(role="user", company=False)
        self.assertIn("Min læring", html)
        self.assertNotIn('href="/hr/"', html)

    def test_admin_block_lists_the_previously_orphaned_pages(self):
        html = _html(admin=True, company_role="company_admin")
        for label in ("Aftaler", "Send notifikationer", "AI-udbyder", "Kreditter", "Systemstatus", "Designgalleri"):
            self.assertIn(label, html)

    def test_cv_portal_is_not_listed_twice(self):
        html = _html(company_role="employee")
        self.assertEqual(html.count("CV-portal"), 1)
        self.assertNotIn("Upload CV", html)

    def test_server_brand_marker_is_set(self):
        self.assertIn('data-brand-server="1"', _html(company_role="employee"))

    def test_stale_brand_is_cleared_when_not_white_label(self):
        self.assertIn("removeItem('fm-brand')", _html(company_role="employee"))

    def test_footer_links_the_vendor_portal(self):
        self.assertIn("Leverandørportal", _html(company_role="employee"))


if __name__ == "__main__":
    unittest.main()
