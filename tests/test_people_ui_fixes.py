"""HR people screens: edit-employee must not silently change roles, add-employee must default to invite."""

import os
import re
import unittest

os.environ.setdefault("SANDBOX", "1")

from tests.sqlite_platform import PlatformDB, client_as, make_app  # noqa: E402


class EditEmployeeRoleTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.addCleanup(self._ctx.pop)
        d = self.db
        for col in ("employee_id TEXT", "employment_type TEXT", "hire_date TEXT", "updated_at TEXT"):
            try:
                d.execute("ALTER TABLE company_users ADD COLUMN " + col)
            except Exception:
                pass  # the shared sqlite schema may already have it
        d.execute("INSERT INTO users (id, username, email, password, role) VALUES "
                  "(2, 'hr', 'hr@x.dk', 'pw', 'user'), (3, 'boss', 'boss@x.dk', 'pw', 'user'), (4, 'ada', 'ada@x.dk', 'pw', 'user')")
        d.execute("INSERT INTO companies (id, company_name, company_slug) VALUES (7, 'Alfa A/S', 'alfa')")
        d.execute("INSERT INTO company_users (company_id, user_id, username, role, department, status) VALUES "
                  "(7, 2, 'hr', 'hr_manager', 'HR', 'active'), (7, 3, 'boss', 'company_admin', 'Ledelse', 'active'), "
                  "(7, 4, 'ada', 'employee', 'Salg', 'active')")

    def _role(self, uid):
        return self.db.one("SELECT role FROM company_users WHERE company_id=7 AND user_id=%s" % uid)["role"]

    def test_hr_manager_cannot_demote_or_promote_admins(self):
        hr = client_as(self.app, user="hr", user_id=2, role="user", company_id=7, company_role="hr_manager")
        hr.post("/companies/employees/3/edit", data={"role": "employee", "department": "Ledelse", "status": "active"})
        self.assertEqual(self._role(3), "company_admin")
        hr.post("/companies/employees/4/edit", data={"role": "company_admin", "department": "Salg", "status": "active"})
        self.assertEqual(self._role(4), "employee")

    def test_company_admin_can_change_roles(self):
        ca = client_as(self.app, user="boss", user_id=3, role="user", company_id=7, company_role="company_admin")
        ca.post("/companies/employees/4/edit", data={"role": "hr_manager", "department": "Salg", "status": "active"})
        self.assertEqual(self._role(4), "hr_manager")

    def test_edit_form_keeps_admin_role_and_unlisted_department_selected_for_hr(self):
        hr = client_as(self.app, user="hr", user_id=2, role="user", company_id=7, company_role="hr_manager")
        html = hr.get("/companies/employees/3/edit").get_data(as_text=True)
        self.assertRegex(html, r'<option value="company_admin"\s+selected>')
        # 'Ledelse' has no company_departments row; it must still be a selected option.
        self.assertRegex(html, r'<option value="Ledelse"\s+selected>')

    def test_add_form_does_not_prefill_a_password(self):
        hr = client_as(self.app, user="hr", user_id=2, role="user", company_id=7, company_role="hr_manager")
        html = hr.get("/companies/employees/add").get_data(as_text=True)
        self.assertNotIn("generatePassword();\n", re.sub(r"function generatePassword\(\) \{.*?\n\}", "", html, flags=re.S))

    def test_only_company_admins_may_create_company_admins(self):
        # The form only offers "Admin" to holders of company.admins, and the route
        # enforces the same rule (a hand-crafted POST is downgraded to employee).
        hr = client_as(self.app, user="hr", user_id=2, role="user", company_id=7, company_role="hr_manager")
        self.assertNotIn('value="company_admin"', hr.get("/companies/employees/add").get_data(as_text=True))
        ca = client_as(self.app, user="boss", user_id=3, role="user", company_id=7, company_role="company_admin")
        self.assertIn('value="company_admin"', ca.get("/companies/employees/add").get_data(as_text=True))
        hr.post("/companies/employees/add", data={
            "full_name": "Bo Ny", "username": "bo", "email": "bo@x.dk", "role": "company_admin",
            "department": "Salg", "job_title": "Sælger"})
        row = self.db.one("SELECT cu.role FROM company_users cu JOIN users u ON u.id = cu.user_id "
                          "WHERE cu.company_id = 7 AND u.username = 'bo'")
        self.assertIsNotNone(row, "the employee should have been created")
        self.assertEqual(row["role"], "employee")


if __name__ == "__main__":
    unittest.main()
