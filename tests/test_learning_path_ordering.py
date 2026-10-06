"""Ordering modes of a learning path: all at once, or one course at a time."""

import datetime
import unittest
from unittest import mock

import learning_path_service as paths
import order_service
from tests.test_learning_paths_hr import PathTests as _PathFixture


class OrderingTests(unittest.TestCase):
    setUp = _PathFixture.setUp

    def start(self):
        self.db.execute("INSERT INTO department_budgets (company_id,department,annual_budget,spent,fiscal_year) VALUES (7,'Drift',50000,0,%s)",
                        (datetime.datetime.now().year,))

    def mode(self, mode):
        self.db.execute("UPDATE learning_paths SET ordering_mode = %s WHERE id = 1", (mode,))

    def orders(self):
        return self.db.query("SELECT order_id, product_handle, status, budget_charged FROM course_orders ORDER BY created_at, id")

    def spent(self):
        return float(self.db.one("SELECT spent FROM department_budgets")["spent"])

    def finish(self, order_id):
        self.assertTrue(order_service.book_order(self.hr, order_id, booking={"date": "2026-01-01", "location": "Kontoret"})["success"])
        self.assertTrue(order_service.complete_order(self.hr, order_id)["success"])

    def steps(self):
        return self.db.query("SELECT position, step_type, course_handle, status, order_id, last_error FROM learning_assignment_steps ORDER BY position")

    def test_all_at_once_is_unchanged_and_charges_every_course(self):
        self.start()
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"course_handle": "b"}, {"course_handle": "c"}])
        out = paths.assign_path(self.cur, self.hr, 7, 1, [1])
        self.assertEqual((out["orders"], out["ordering_mode"]), (3, "all_at_once"))
        self.assertEqual(self.spent(), 3600)

    def test_sequential_assignment_orders_only_the_first_course_and_charges_only_it(self):
        self.start()
        self.mode("sequential")
        paths.save_steps(self.cur, 7, 1, [{"title": "Læs op"}, {"course_handle": "a"}, {"course_handle": "b"}, {"course_handle": "c"}])
        out = paths.assign_path(self.cur, self.hr, 7, 1, [1])
        self.assertEqual((out["orders"], out["ordering_mode"]), (1, "sequential"))       # the guidance step before it does not block
        orders = self.orders()
        self.assertEqual([(o["product_handle"], o["status"]) for o in orders], [("a", "approved")])       # pre-approved as assigned by HR
        self.assertEqual(self.spent(), 1200)
        self.assertEqual([s["status"] for s in self.steps()], ["not_started", "ordered", "not_started", "not_started"])
        progress = self.db.one("SELECT ordering_mode, assigned_by_user_id FROM employee_learning_progress")
        self.assertEqual((progress["ordering_mode"], progress["assigned_by_user_id"]), ("sequential", 3))

    def test_completing_a_course_orders_the_next_one_automatically(self):
        self.start()
        self.mode("sequential")
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"course_handle": "b"}, {"course_handle": "c"}])
        paths.assign_path(self.cur, self.hr, 7, 1, [1])
        self.finish(self.orders()[0]["order_id"])
        orders = self.orders()
        self.assertEqual([(o["product_handle"], o["status"]) for o in orders], [("a", "completed"), ("b", "approved")])
        self.assertEqual(self.spent(), 2400)                # a + b, c is not ordered yet
        note = self.db.one("SELECT title, action_url FROM notifications WHERE user_id='ada' AND dedupe_key LIKE 'path-step-ordered:%'")
        self.assertIn("Kursus b", note["title"])
        self.finish(orders[1]["order_id"])
        self.assertEqual([o["product_handle"] for o in self.orders()], ["a", "b", "c"])
        self.finish(self.orders()[2]["order_id"])
        self.assertEqual(self.db.one("SELECT status, progress_percentage FROM employee_learning_progress")["status"], "completed")

    def test_a_guidance_step_between_courses_gates_the_next_order(self):
        self.mode("sequential")
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"title": "Tal med din leder"}, {"course_handle": "b"}])
        paths.assign_path(self.cur, self.hr, 7, 1, [1])
        self.finish(self.orders()[0]["order_id"])
        self.assertEqual(len(self.orders()), 1)
        guidance = self.db.one("SELECT id FROM learning_assignment_steps WHERE step_type='info'")["id"]
        paths.acknowledge_step(self.cur, 7, 1, guidance)
        self.assertEqual([o["product_handle"] for o in self.orders()], ["a", "b"])

    def test_editing_the_path_does_not_change_a_running_assignment(self):
        self.mode("sequential")
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"course_handle": "b"}])
        paths.assign_path(self.cur, self.hr, 7, 1, [1])
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "x"}, {"course_handle": "y"}, {"course_handle": "z"}], ordering_mode="all_at_once")
        self.assertEqual(self.db.one("SELECT ordering_mode FROM learning_paths")["ordering_mode"], "all_at_once")
        self.finish(self.orders()[0]["order_id"])
        self.assertEqual([o["product_handle"] for o in self.orders()], ["a", "b"])     # frozen steps and frozen mode
        self.assertEqual(self.db.one("SELECT ordering_mode FROM employee_learning_progress")["ordering_mode"], "sequential")

    def test_a_cancelled_sequential_order_shows_failed_and_hr_can_retry_or_skip(self):
        self.mode("sequential")
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"course_handle": "b"}])
        paths.assign_path(self.cur, self.hr, 7, 1, [1])
        first = self.orders()[0]["order_id"]
        self.assertTrue(order_service.cancel_order(self.hr, first, "Kan ikke")["success"])
        paths.refresh_for_order(self.cur, first, 7)
        self.assertEqual([s["status"] for s in self.steps()], ["failed", "not_started"])
        self.assertEqual(len(self.orders()), 1)             # the chain waits for HR
        step_id = self.db.one("SELECT id FROM learning_assignment_steps WHERE position = 1")["id"]
        retry = paths.hr_retry_step(self.hr, step_id)
        self.assertTrue(retry["success"], retry)
        self.assertEqual([o["status"] for o in self.orders()], ["cancelled", "approved"])
        self.assertEqual(self.steps()[0]["status"], "ordered")
        # Skip instead: the next course is ordered.
        self.db.execute("DELETE FROM course_orders WHERE status = 'approved'")
        self.db.execute("UPDATE learning_assignment_steps SET status='failed', order_id=%s WHERE position = 1", (first,))
        skipped = paths.hr_skip_step(self.hr, step_id, "Ikke relevant")
        self.assertTrue(skipped["success"])
        self.assertEqual([s["status"] for s in self.steps()], ["skipped", "ordered"])
        self.assertEqual([o["product_handle"] for o in self.orders() if o["status"] == "approved"], ["b"])

    def test_a_course_that_cannot_be_ordered_is_recorded_and_hr_is_told(self):
        self.mode("sequential")
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"course_handle": "b"}])
        paths.assign_path(self.cur, self.hr, 7, 1, [1])
        with mock.patch("catalog_service.get_product", side_effect=lambda h: None if h == "b" else {"title": "Kursus", "price_min": 1200}):
            self.finish(self.orders()[0]["order_id"])
        self.assertEqual(self.orders()[0]["status"], "completed")           # the completion itself was not undone
        failed = self.steps()[1]
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["last_error"])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM notifications WHERE user_id='hr' AND dedupe_key LIKE 'path-step-failed:%'")["c"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM notifications WHERE user_id='ada' AND dedupe_key LIKE 'path-step-failed-learner:%'")["c"], 1)

    def test_the_learner_cannot_order_a_course_that_is_not_due_yet(self):
        self.mode("sequential")
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"course_handle": "b"}])
        paths.assign_path(self.cur, self.hr, 7, 1, [1])
        later = self.db.one("SELECT id FROM learning_assignment_steps WHERE position = 2")["id"]
        learner = order_service.OrderContext(company_id=7, user_id=1, username="ada", company_role="employee", department="Drift")
        res = paths.order_assignment_step(learner, later)
        self.assertFalse(res["success"])
        self.assertIn("bestilles automatisk", res["message"])
        self.assertEqual(len(self.orders()), 1)

    def test_unknown_modes_fall_back_to_all_at_once(self):
        self.assertEqual(paths.normalize_mode("whatever"), "all_at_once")
        self.assertEqual(paths.normalize_mode("sequential"), "sequential")
