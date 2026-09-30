"""N-3.5 / S-4.4 Part B side: per-goal sharing. Unshared goals never reach the learner."""

import os
import unittest

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import goal_sharing_ui as gs  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class GoalSharingTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        d = self.db
        d.execute("INSERT INTO users (id, username) VALUES (1, 'ada'), (2, 'hr'), (3, 'bo')")
        d.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES "
                  "(7, 1, 'ada', 'employee', 'active'), (7, 2, 'hr', 'hr_manager', 'active'), "
                  "(8, 3, 'bo', 'employee', 'active')")
        self.cur = d.connection.cursor()

    def test_new_goals_are_private_by_default(self):
        res = gs.add_goal(self.cur, company_id=7, employee_id=1, title="Lederuddannelse", actor_user_id=2)
        self.assertTrue(res["success"])
        self.assertFalse(res["shared"])
        shared, private = gs.list_goals_for_hr(self.cur, 7, 1)
        self.assertEqual((len(shared), len(private)), (0, 1))
        self.assertEqual(gs.shared_goals_for_learner(self.cur, 1, 7), [])      # learner sees nothing

    def test_sharing_moves_goal_notifies_learner_and_is_audited(self):
        gid = gs.add_goal(self.cur, company_id=7, employee_id=1, title="PRINCE2", actor_user_id=2)["goal_id"]
        res = gs.set_shared(self.cur, company_id=7, goal_id=gid, shared=True, actor_user_id=2, note="Fint mål til efteråret")
        self.assertTrue(res["shared"])
        learner = gs.shared_goals_for_learner(self.cur, 1, 7)
        self.assertEqual([g["goal_title"] for g in learner], ["PRINCE2"])
        self.assertEqual(learner[0]["share_note"], "Fint mål til efteråret")
        n = self.db.one("SELECT user_id, action_url, title FROM notifications")
        self.assertEqual((n["user_id"], n["action_url"]), ("ada", "/mine-maal"))
        acts = [r["action_type"] for r in self.db.query("SELECT action_type FROM audit_log")]
        self.assertIn("goal.shared", acts)

    def test_unsharing_removes_it_from_the_learner_again(self):
        gid = gs.add_goal(self.cur, company_id=7, employee_id=1, title="X", share=True, actor_user_id=2)["goal_id"]
        self.assertEqual(len(gs.shared_goals_for_learner(self.cur, 1, 7)), 1)
        gs.set_shared(self.cur, company_id=7, goal_id=gid, shared=False, actor_user_id=2)
        self.assertEqual(gs.shared_goals_for_learner(self.cur, 1, 7), [])
        self.assertIn("goal.unshared", [r["action_type"] for r in self.db.query("SELECT action_type FROM audit_log")])

    def test_cross_tenant_goal_cannot_be_shared(self):
        gid = gs.add_goal(self.cur, company_id=8, employee_id=3, title="Andet firma")["goal_id"]
        res = gs.set_shared(self.cur, company_id=7, goal_id=gid, shared=True, actor_user_id=2)
        self.assertEqual(res["error"], "not_found")
        self.assertEqual(gs.shared_goals_for_learner(self.cur, 3, 8), [])

    def test_employee_lookup_is_company_scoped(self):
        self.assertIsNotNone(gs.employee_in_company(self.cur, 7, 1))
        self.assertIsNone(gs.employee_in_company(self.cur, 7, 3))

    def test_learner_ai_context_only_reads_shared_goals(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "learner_context.py"), encoding="utf-8").read()
        self.assertIn("shared_with_employee = 1", src)


class GoalRoutePermissionTests(unittest.TestCase):
    def test_employee_cannot_open_hr_goal_pages(self):
        import run
        client = run.create_app().test_client()
        with client.session_transaction() as s:
            s["user"] = "ada"; s["company_id"] = 7; s["company_role"] = "employee"
        page = {"Accept": "text/html"}          # browsers get a redirect, JSON callers a 403 (S-1.6)
        self.assertEqual(client.get("/hr/employee/1/goals", headers=page).status_code, 302)
        self.assertEqual(client.post("/hr/goals/1/share", data={"shared": "1"}, headers=page).status_code, 302)


if __name__ == "__main__":
    unittest.main()
