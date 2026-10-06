"""Assigning a course or a learning path is reviewed first: orders, cost, budget before and after."""

import datetime
import unittest
from unittest import mock

import learning_path_service as paths
from tests.sqlite_platform import PlatformDB, make_app, client_as, render_patches
from tests.test_learning_path_editor import SCHEMA

CATALOG = {
    "a": {"handle": "a", "title": "PRINCE2 Foundation", "vendor": "V", "price_min": 12500.0, "variants": []},
    "b": {"handle": "b", "title": "Ledelse for nye ledere", "vendor": "V", "price_min": 25300.0, "variants": []},
    "multi": {"handle": "multi", "title": "Flere hold", "vendor": "V", "price_min": 900.0,
              "variants": [{"date": "1. marts 2099", "city": "Aarhus", "location": "Aarhus", "price": 900.0},
                           {"date": "1. april 2099", "city": "Odense", "location": "Odense", "price": 900.0}]},
}
YEAR = datetime.datetime.now().year


class ReviewBase(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.db.raw.executescript(SCHEMA)
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
        d.execute("INSERT INTO users (id,username,email) VALUES (1,'ada','a@f.dk'),(2,'hr','h@f.dk'),(4,'bo','b@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,full_name,email,role,status,department) VALUES "
                  "(7,1,'ada','Ada Hansen','a@f.dk','employee','active','Salg'),"
                  "(7,4,'bo','Bo Jensen','b@f.dk','employee','active','Salg'),"
                  "(7,2,'hr','Hanne HR','h@f.dk','hr_manager','active','HR')")
        d.execute("INSERT INTO learning_paths (id,company_id,path_name,version) VALUES (1,7,'Ledelse 1',3)")
        d.execute("INSERT INTO learning_path_steps (path_id,company_id,position,step_type,course_handle,title) VALUES "
                  "(1,7,1,'catalog','a','PRINCE2'),(1,7,2,'catalog','b','Ledelse'),(1,7,3,'info',NULL,'Tal med din leder')")
        d.execute("INSERT INTO department_budgets (company_id,department,annual_budget,spent,fiscal_year) VALUES (7,'Salg',100000,12500,%s)", (YEAR,))
        d.execute("INSERT INTO company_courses(id,company_id,title,price,location) VALUES(5,7,'Intern dyr',18900,'Kontoret')")
        for patcher in (mock.patch("catalog_service.get_product", side_effect=lambda h: CATALOG.get(h)),):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hr = client_as(self.app, user="hr", user_id=2, company_id=7, company_role="hr_manager")
        self.addCleanup(d.raw.close)

    def counts(self):
        return (self.db.one("SELECT COUNT(*) AS c FROM course_orders")["c"],
                self.db.one("SELECT COUNT(*) AS c FROM employee_learning_progress")["c"],
                self.db.one("SELECT COUNT(*) AS c FROM learning_assignment_steps")["c"])

    def review(self, **q):
        q.setdefault("assign_type", "individual")
        q.setdefault("user_id", "1")
        resp = self.hr.get("/hr/learning-paths/1/tildel/gennemse", query_string=q)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def confirm(self, **over):
        data = {"assign_type": "individual", "user_id": "1", "expected_version": "3", "expected_total": "37800.0", **over}
        return self.hr.post("/hr/learning-paths/1/assign", data=data)


class PathReviewTests(ReviewBase):
    def test_the_review_writes_nothing_and_shows_orders_cost_and_budget_before_and_after(self):
        before = self.counts()
        html = self.review()
        self.assertEqual(self.counts(), before)
        self.assertIn("PRINCE2 Foundation", html)
        self.assertIn("Ledelse for nye ledere", html)
        self.assertIn("37.800 kr.", html)
        self.assertIn("Salg: 87.500 kr. tilbage → <strong class=\"\">49.700 kr.</strong> efter tildelingen", html)
        self.assertNotIn("37800.0 kr", html)

    def test_a_sequential_path_separates_now_from_later(self):
        self.db.execute("UPDATE learning_paths SET ordering_mode = 'sequential'")
        html = self.review()
        self.assertIn("Bestilles nu", html)
        self.assertIn("Bestilles senere (anslået)", html)
        self.assertRegex(html, r'data-total="now">12\.500 kr\.')
        self.assertRegex(html, r'data-total="later">≈ 25\.300 kr\.')
        self.assertIn("75.000 kr.", html)               # 87.500 - 12.500: only the course ordered now is charged

    def test_confirm_with_the_reviewed_version_and_total_assigns(self):
        resp = self.confirm()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/hr/learning-paths"))
        self.assertEqual(self.counts()[0], 2)
        self.assertEqual(float(self.db.one("SELECT spent FROM department_budgets")["spent"]), 50300)

    def test_confirm_without_a_review_is_sent_to_the_review_and_creates_nothing(self):
        resp = self.hr.post("/hr/learning-paths/1/assign", data={"assign_type": "individual", "user_id": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/tildel/gennemse", resp.headers["Location"])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_a_changed_path_version_or_total_forces_a_new_review(self):
        for over in ({"expected_version": "2"}, {"expected_total": "30000"}):
            resp = self.confirm(**over)
            self.assertIn("/tildel/gennemse", resp.headers["Location"], over)
            self.assertEqual(self.counts(), (0, 0, 0), over)

    def test_a_course_that_no_longer_exists_blocks_the_assignment(self):
        self.db.execute("INSERT INTO learning_path_steps (path_id,company_id,position,step_type,course_handle,title) VALUES (1,7,4,'catalog','vaek','Væk')")
        html = self.review()
        self.assertIn("Kurset findes ikke længere i kataloget.", html)
        self.assertRegex(html, r"<button[^>]*disabled")
        resp = self.confirm(expected_total="37800.0")
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertIn("/tildel/gennemse", resp.headers["Location"])

    def test_a_department_over_budget_blocks_until_it_is_acknowledged(self):
        self.db.execute("UPDATE department_budgets SET annual_budget = 40000")        # 27.500 left, the path costs 37.800
        html = self.review()
        self.assertIn("Overskrider budgettet med 10.300 kr.", html)
        self.assertIn('name="accept_over_budget"', html)
        refused = self.confirm()
        self.assertIn("/tildel/gennemse", refused.headers["Location"])
        self.assertEqual(self.counts(), (0, 0, 0))
        accepted = self.confirm(accept_over_budget="1")
        self.assertTrue(accepted.headers["Location"].endswith("/hr/learning-paths"))
        statuses = sorted(r["status"] for r in self.db.query("SELECT status FROM course_orders"))
        self.assertEqual(statuses, ["approved", "pending_approval"])    # the one that no longer fits goes to approval

    def test_two_people_in_the_same_department_add_up(self):
        html = self.review(assign_type="selected", user_id="", employee_ids=["1", "4"])
        self.assertIn("75.600 kr.", html)
        self.assertIn("11.900 kr.", html)                              # 87.500 - 75.600 still fits
        self.assertNotIn("Overskrider budgettet med", html)

    def test_the_bulk_assign_form_goes_through_the_review_too(self):
        resp = self.hr.post("/hr/assign-path", data={"path_id": "1", "employee_ids": ["1"], "due_date": "2099-01-01"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bekræft og tildel", html)
        self.assertIn('name="expected_total"', html)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_the_assign_modal_opens_the_review_instead_of_creating_orders(self):
        html = self.hr.get("/hr/learning-paths").get_data(as_text=True)
        self.assertIn("/hr/learning-paths/1/tildel/gennemse", html)
        self.assertNotIn('action="/hr/learning-paths/1/assign"', html)

    def test_a_course_with_several_sessions_is_priced_and_says_the_participant_chooses(self):
        self.db.execute("INSERT INTO learning_path_steps (path_id,company_id,position,step_type,course_handle,title) VALUES (1,7,4,'catalog','multi','Hold')")
        html = self.review()
        self.assertIn("Hold vælges af deltageren", html)


class CourseReviewTests(ReviewBase):
    def test_the_course_confirm_page_uses_the_component_money_format_and_hr_subnav(self):
        html = self.hr.post("/hr/assign-course", data={"handle": "internal:5", "employee_ids": ["1"]}).get_data(as_text=True)
        self.assertIn("18.900 kr.", html)
        self.assertNotIn("18900.0 kr", html)
        self.assertIn('class="pg-subnav-wrap"', html)
        self.assertIn('data-review="budget"', html)
        self.assertIn("Salg: 87.500 kr. tilbage → <strong class=\"\">68.600 kr.</strong> efter tildelingen", html)
        self.assertEqual(self.counts()[0], 0)

    def test_an_over_budget_course_assignment_needs_an_acknowledgement(self):
        self.db.execute("UPDATE department_budgets SET annual_budget = 20000")        # 7.500 left
        data = {"handle": "internal:5", "employee_ids": ["1"], "confirm": "yes", "expected_price": "18900"}
        again = self.hr.post("/hr/assign-course", data=data).get_data(as_text=True)
        self.assertIn("Bekræft, at bestillinger ud over budgettet sendes til godkendelse", again)
        self.assertEqual(self.counts()[0], 0)
        self.hr.post("/hr/assign-course", data={**data, "accept_over_budget": "1"})
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "pending_approval")


class PreviewFunctionTests(ReviewBase):
    def test_preview_is_read_only_and_company_scoped(self):
        with self.app.app_context():
            cur = self.db.connection.cursor()
            review = paths.preview_path_assignment(cur, 7, 1, [1, 99])
            self.assertEqual([p["user_id"] for p in review["people"]], [1])          # 99 is nobody here
            self.assertEqual((review["total_now"], review["total_later"]), (37800.0, 0.0))
            self.assertIsNone(paths.preview_path_assignment(cur, 8, 1, [1]))          # another company's path id
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_people_already_in_the_path_get_nothing_and_cost_nothing(self):
        self.confirm()
        with self.app.app_context():
            cur = self.db.connection.cursor()
            review = paths.preview_path_assignment(cur, 7, 1, [1, 4])
        self.assertEqual([p["name"] for p in review["people"]], ["Bo Jensen"])
        self.assertEqual(review["already"], ["Ada Hansen"])
        self.assertEqual(review["total_now"], 37800.0)
