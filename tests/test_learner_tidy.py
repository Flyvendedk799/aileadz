"""Learner pages: the ordered session is preselected, one welcome, no credits chip for
employees, and format/location tags are never proposed as skills."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

import completion_service  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402
from tests.test_first_run import _client, SCHEMA_EXTRA  # noqa: E402,F401


class SkillProposalTests(unittest.TestCase):
    def test_format_location_and_level_tags_are_not_skills(self):
        product = {"handle": "h", "metadata": {"primary_topic": "Klassekursus", "difficulty": "mellem"},
                   "categories": ["Projektledelse", "Online"],
                   "tags": ["klassekursus", "e-learning", "aarhus", "avanceret", "scrum", "region:midtjylland"]}
        names = [p["name"] for p in completion_service.skills_for_product(product)]
        self.assertIn("Projektledelse", names)
        self.assertIn("Scrum", names)
        for bad in ("Klassekursus", "Online", "E-Learning", "Aarhus", "Avanceret"):
            self.assertNotIn(bad, names)


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.execute("INSERT INTO users (id, username, email) VALUES (1, 'ada', 'ada@x.dk')")
        self.client = _client(self.db, user="ada", user_id=1, company_id=7, company_role="employee")
        for p in (mock.patch("white_label_global_integration.get_template_context", return_value={}),
                  mock.patch("futurematch_ui._home_recommendations", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def test_one_welcome_block_with_each_action_once(self):
        html = self.client.get("/min-laering").get_data(as_text=True)
        self.assertIn("Vælg, hvor du vil starte", html)
        self.assertNotIn("Velkommen til", html)
        self.assertEqual(html.count("Upload dit CV") + html.count("Upload CV"), 1)

    def test_dismissed_welcome_leaves_the_regular_hero(self):
        self.client.post("/min-laering/velkommen/luk")
        html = self.client.get("/min-laering").get_data(as_text=True)
        self.assertNotIn("Vælg, hvor du vil starte", html)
        self.assertIn("Velkommen til", html)

    def test_employee_header_has_no_credits_chip(self):
        self.assertNotIn("fm-chip credits", self.client.get("/min-laering").get_data(as_text=True))

    def test_credit_managers_keep_the_chip(self):
        hr = _client(self.db, user="hr", user_id=2, company_id=7, company_role="hr_manager")
        self.assertIn("fm-chip credits", hr.get("/min-laering").get_data(as_text=True))


class OrderedSessionTests(unittest.TestCase):
    def setUp(self):
        import catalog_routes
        self.routes = catalog_routes
        self.db = SqliteMysql()
        self.db.execute("INSERT INTO course_orders (order_id, user_id, username, product_handle, product_title, status, "
                        "variant_date, variant_location) VALUES ('o-1', 1, 'ada', 'h', 'Kursus', 'pending_approval', "
                        "'3. december 2026', 'Aarhus')")
        self.product = {"handle": "h", "variants": [
            {"session_id": "s-nov", "date": "12. november 2026", "location": "København"},
            {"session_id": "s-dec", "date": "3. december 2026", "location": "Aarhus"}]}

    def lookup(self, order_id, **sess):
        from flask import Flask
        app = Flask(__name__)
        app.secret_key = "x"
        app.mysql = self.db
        with app.test_request_context("/"):
            from flask import session
            session.update(sess)
            return self.routes._ordered_session_id(self.product, order_id)

    def test_the_ordered_session_is_matched_on_date_and_place(self):
        self.assertEqual(self.lookup("o-1", user="ada", user_id=1), "s-dec")

    def test_the_stored_session_id_wins(self):
        self.db.execute("INSERT INTO course_order_details (order_id, session_id) VALUES ('o-1', 's-nov')")
        self.assertEqual(self.lookup("o-1", user="ada", user_id=1), "s-nov")

    def test_someone_elses_order_is_ignored(self):
        self.assertIsNone(self.lookup("o-1", user="eve", user_id=9))
        self.assertIsNone(self.lookup("o-1"))


if __name__ == "__main__":
    unittest.main()
