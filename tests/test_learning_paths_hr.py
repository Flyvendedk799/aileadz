"""N-4.4: paths create real work (orders via the lifecycle), versioned saves,
learner sees HR assignments. Plus delegation of goal sharing to Part A when present."""

import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import goal_sharing_ui as gsu  # noqa: E402
import learning_path_service as lps  # noqa: E402
import order_service as svc  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class PathTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.app = Flask(__name__)
        self.app.mysql = self.db
        cm = self.app.app_context()
        cm.push()
        self.addCleanup(cm.pop)
        d = self.db
        d.raw.executescript("""
          CREATE TABLE learning_paths (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, path_name TEXT, path_category TEXT,
            difficulty_level TEXT, is_active INTEGER DEFAULT 1, version INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE learning_path_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, path_id INTEGER, company_id INTEGER, position INTEGER,
            step_type TEXT, course_handle TEXT, title TEXT);
          CREATE TABLE learning_path_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, path_id INTEGER, company_id INTEGER, version INTEGER,
            steps_json TEXT, saved_by INTEGER, note TEXT, saved_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        d.execute("INSERT INTO users (id, username, email) VALUES (1,'ada','a@f.dk'),(2,'bo','b@f.dk'),(3,'hr','h@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,email,role,department,status) VALUES "
                  "(7,1,'ada','a@f.dk','employee','Drift','active'),(7,2,'bo','b@f.dk','employee','Drift','active'),"
                  "(8,9,'x','x@f.dk','employee','Drift','active'),(7,3,'hr','h@f.dk','hr_manager','HR','active')")
        d.execute("INSERT INTO learning_paths (id, company_id, path_name) VALUES (1,7,'Ledelse 1'),(2,8,'Andet')")
        for p in [mock.patch.object(svc, "_emit_event_safe"), mock.patch.object(svc, "_send_email_safe"),
                  mock.patch.object(svc, "_send_approval_needed_emails_safe"),
                  mock.patch.object(svc, "_manager_recipient_emails", return_value=[]),
                  mock.patch.object(svc, "_vendor_id_for_handle", return_value=None),
                  mock.patch("catalog_service.get_product", side_effect=lambda h: None if h == "findes-ikke" else {"title": "Kursus " + h, "price_min": 1200})]:
            p.start()
            self.addCleanup(p.stop)
        self.cur = d.connection.cursor()
        self.hr = svc.OrderContext(company_id=7, user_id=3, username="hr", company_role="hr_manager", department="HR")

    def test_unknown_course_handle_is_rejected(self):
        res = lps.save_steps(self.cur, 7, 1, [{"course_handle": "findes-ikke"}])
        self.assertFalse(res["success"])
        self.assertIn("findes ikke", res["message"])

    def test_saves_are_versioned_and_keep_the_previous_state(self):
        lps.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"title": "Tal med din leder"}])
        res = lps.save_steps(self.cur, 7, 1, [{"course_handle": "b"}], actor_user_id=3, note="Skiftet kursus")
        self.assertEqual(res["version"], 3)
        v = self.db.one("SELECT version, steps_json FROM learning_path_versions")
        self.assertEqual(v["version"], 2)
        self.assertIn("Tal med din leder", v["steps_json"])       # old state is preserved
        self.assertEqual([s["course_handle"] for s in lps.get_steps(self.cur, 7, 1)], ["b"])

    def test_cannot_edit_another_companys_path(self):
        self.assertFalse(lps.save_steps(self.cur, 7, 2, [{"title": "x"}])["success"])

    def test_assigning_a_path_orders_its_paid_steps_approved_and_charges_the_budget(self):
        lps.save_steps(self.cur, 7, 1, [{"course_handle": "prince2"}, {"title": "Guidance"}])
        out = lps.assign_path(self.cur, self.hr, 7, 1, [1, 2, 9], due_date="2026-12-01", sender_id=3)
        self.assertEqual((out["assigned"], out["orders"], out["skipped"]), (2, 2, 1))      # user 9 is another company
        orders = self.db.query("SELECT user_id, status, request_notes FROM course_orders ORDER BY user_id")
        self.assertEqual([o["status"] for o in orders], ["approved", "approved"])       # assigned by HR = approved
        self.assertIn("Tildelt af", orders[0]["request_notes"])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM order_approvals WHERE status='pending'")["c"], 0)
        self.assertEqual(self.db.one("SELECT completion_deadline FROM course_orders WHERE user_id=1")["completion_deadline"].strftime("%Y-%m-%d"), "2026-12-01")
        note = self.db.one("SELECT action_url FROM notifications WHERE user_id='ada' AND kind='assignment'")
        self.assertTrue(note["action_url"].startswith("/min-laering/forloeb/"))

    def test_reassigning_skips_enrolled_and_does_not_reorder(self):
        lps.save_steps(self.cur, 7, 1, [{"course_handle": "prince2"}])
        lps.assign_path(self.cur, self.hr, 7, 1, [1])
        again = lps.assign_path(self.cur, self.hr, 7, 1, [1])
        self.assertEqual((again["assigned"], again["orders"]), (0, 0))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM course_orders")["c"], 1)

    def test_learner_sees_hr_assignments_with_due_date(self):
        lps.save_steps(self.cur, 7, 1, [{"course_handle": "prince2"}])
        lps.assign_path(self.cur, self.hr, 7, 1, [1], due_date="2026-12-01")
        mine = lps.assignments_for_learner(self.cur, 1, 7)
        self.assertEqual([(a["path_name"], a["due_date"]) for a in mine], [("Ledelse 1", "2026-12-01")])
        self.assertEqual(lps.assignments_for_learner(self.cur, 2, 8), [])      # other tenant/user


class GoalDelegationTests(unittest.TestCase):
    def test_delegates_to_part_a_when_present_and_notifies(self):
        fake = types.SimpleNamespace(
            list_goals_for_hr=mock.Mock(return_value={"shared": ["s"], "hr_only": ["p"]}),
            set_shared=mock.Mock(return_value={"id": 5, "employee_id": 1, "goal_title": "Mål"}),
            list_shared_goals_for_learner=mock.Mock(return_value=["g"]),
            create_goal=mock.Mock(return_value=9),
        )
        with mock.patch.dict(sys.modules, {"goal_sharing": fake}), mock.patch.object(gsu, "_notify_shared") as notify:
            self.assertEqual(gsu.list_goals_for_hr(None, 7, 1, conn=object()), (["s"], ["p"]))
            self.assertEqual(gsu.shared_goals_for_learner(None, 1, 7, conn=object()), ["g"])
            res = gsu.set_shared(None, company_id=7, goal_id=5, shared=True, actor_user_id=2, note="n", conn=object())
            self.assertTrue(res["shared"])
            notify.assert_called_once()
            self.assertEqual(gsu.add_goal(None, company_id=7, employee_id=1, title="T", share=True, conn=object())["goal_id"], 9)


if __name__ == "__main__":
    unittest.main()
