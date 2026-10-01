"""HR money pages (approvals, budgets, billing, suppliers, agreements): rendering and input guards.

Real route table + fake MySQL (tests/secapp.py). No network, no MySQL.
"""

import datetime as dt
import unittest
from unittest import mock

from tests.secapp import client_as, get_app, patch_mysql

import auth_decorators as ad

COMPANY = {"id": 7, "company_name": "Acme", "name": "Acme", "user_role": "hr_manager",
           "department": None, "permissions": None, "status": "active", "plan": "pro"}


def _client(role="hr_manager", responder=None):
    app = get_app()

    def resp(sql, params):
        s = " ".join(str(sql).split()).lower()
        if "from companies c" in s and "company_users cu" in s:
            return dict(COMPANY, user_role=role)
        return responder(s, params) if responder else None

    ad.invalidate_session_cache()
    fake, p = patch_mysql(app, resp)
    return client_as(app, role), p


class MoneyUiTests(unittest.TestCase):
    def tearDown(self):
        ad.invalidate_session_cache()

    def test_hr_billing_page_renders_with_script_urls(self):
        # Regression: `set url_update` lived in the content block, so the scripts block saw an
        # undefined value and the whole page 500'd back to the dashboard.
        c, p = _client()
        with p:
            r = c.get("/hr/billing")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("const UPDATE_TMPL = \"/hr/order/ORDER_ID/billing\"", html)
        self.assertIn("const BULK_URL = \"/hr/billing/bulk\"", html)

    def test_save_budget_rejects_garbage_with_json_400(self):
        c, p = _client()
        with p:
            for body in ({"department": "IT", "annual_budget": "abc"},
                         {"department": "IT", "annual_budget": 10, "fiscal_year": "x"},
                         {"department": "IT", "annual_budget": "nan"},
                         {"department": None, "annual_budget": 10}):
                r = c.post("/hr/budgets/save", json=body)
                self.assertEqual(r.status_code, 400, body)
                self.assertFalse(r.get_json()["success"])

    def test_pending_sum_is_shown_on_approvals(self):
        # Regression: the sum was built with `set` inside a for loop and always rendered 0 kr.
        now = dt.datetime(2026, 9, 1, 10, 0)

        def responder(s, params):
            if "from order_approvals oa" in s and "join course_orders" in s and "group by" not in s:
                return [dict(id=i, order_id="o%d" % i, company_id=7, status="pending", requested_at=now,
                             product_title="Kursus", price=price, product_handle="x", variant_date=None,
                             variant_location=None, user_email="a@b.dk", user_name="Ada", requester_username="ada",
                             requester_user_id=1, department="Salg", job_title=None)
                        for i, price in ((1, 1500), (2, 2501))]
            return None

        c, p = _client(responder=responder)
        with p:
            r = c.get("/hr/approvals")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("samlet 4.001 kr", html)
        self.assertIn('id="bulkReject"', html)

    def test_dept_head_does_not_get_edit_controls(self):
        def responder(s, params):
            if "from department_budgets db" in s:
                return [dict(id=1, company_id=7, department="Salg", annual_budget=1000, spent=10,
                             fiscal_year=2026, employee_count=2)]
            return None

        c, p = _client("dept_head", responder)
        with p:
            r = c.get("/hr/budgets")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertNotIn("openBudgetModal()\"", html)
        self.assertNotIn("Rediger budget", html)

    def test_hr_manager_gets_edit_controls_on_budgets(self):
        def responder(s, params):
            if "from department_budgets db" in s:
                return [dict(id=1, company_id=7, department="Salg", annual_budget=1000, spent=10,
                             fiscal_year=2026, employee_count=2)]
            return None

        c, p = _client("hr_manager", responder)
        with p:
            html = c.get("/hr/budgets").get_data(as_text=True)
        self.assertIn("openBudgetModal()", html)
        self.assertIn("Rediger budget", html)

    def test_procurement_formats_discounts_in_danish(self):
        import json
        import hr_ext

        cov = json.dumps({"vendors": [{"name": "V", "course_count": 1, "categories": [], "is_active": True,
                                       "agreement": {"name": "A", "type": "fixed_price", "value": 9000.0,
                                                     "valid_until": None}}],
                          "active_suppliers": 1, "inactive_suppliers": 0, "agreement_count": 1})
        exp = json.dumps({"within_days": 30, "total": 0, "expired": 0, "agreements": []})
        c, p = _client()
        with p, mock.patch.object(hr_ext, "_tool", side_effect=lambda name, args=None: json.loads(
                cov if "coverage" in name else exp)):
            html = c.get("/hr/procurement").get_data(as_text=True)
        self.assertIn("Fastpris 9000 kr", html)
        self.assertNotIn("fixed_price", html)


if __name__ == "__main__":
    unittest.main()
