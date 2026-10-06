"""Cross-surface launch regressions, against the real lifecycle and SQL harness."""

import datetime
import unittest
from unittest import mock
from tests.test_learning_paths_hr import PathTests as _PathFixture
from tests.test_compliance_actions import AssignTests as _ComplianceFixture
import learning_path_service as paths
import enrollment_service as enrollment
import order_service
import compliance_assign


class LearningLaunchTests(unittest.TestCase):
    setUp = _PathFixture.setUp

    def test_path_snapshot_survives_edits_and_rolls_up_actual_orders(self):
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}, {"title": "Tal med din leder"}])
        result = paths.assign_path(self.cur, self.hr, 7, 1, [1])
        assignment = self.db.one("SELECT id FROM employee_learning_progress WHERE learning_path_id=1")
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "b"}])
        frozen = paths.assignment_detail(self.cur, 7, 1, assignment["id"])
        self.assertEqual([s["title"] for s in frozen["steps"]], ["Kursus a", "Tal med din leder"])
        oid = result["results"][0]["order_id"]
        self.assertTrue(order_service.set_status(self.hr, oid, "approved")["success"])
        self.assertTrue(order_service.book_order(self.hr, oid, booking={"date": "2026-01-01", "location": "Kontoret"})["success"])
        self.assertTrue(order_service.complete_order(self.hr, oid)["success"])
        self.assertEqual(
            self.db.one("SELECT progress_percentage FROM employee_learning_progress WHERE id=%s", (assignment["id"],))[
                "progress_percentage"
            ],
            50,
        )
        paths.acknowledge_step(self.cur, 7, 1, frozen["steps"][1]["id"])
        self.assertEqual(paths.assignment_detail(self.cur, 7, 1, assignment["id"])["status"], "completed")

    def test_failed_step_is_visible_and_retry_is_idempotent(self):
        paths.save_steps(self.cur, 7, 1, [{"course_handle": "a"}])
        with mock.patch("catalog_service.get_product", return_value=None):
            result = paths.assign_path(self.cur, self.hr, 7, 1, [1])
        self.assertEqual(result["order_failures"], 1)
        step = self.db.one("SELECT * FROM learning_assignment_steps")
        self.assertEqual(step["status"], "failed")
        learner = order_service.OrderContext(company_id=7, user_id=1, username="ada", company_role="employee", department="Drift")
        first = paths.order_assignment_step(learner, step["id"])
        second = paths.order_assignment_step(learner, step["id"])
        self.assertTrue(first["success"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM course_orders")["n"], 1)

    def test_quote_uses_stable_sessions_and_current_prices(self):
        product = {
            "title": "Kursus",
            "vendor": "V",
            "price_min": 400,
            "variants": [
                {"id": 11, "price": 900, "date": "2099-01-01", "location": "A", "seats": 2},
                {"id": 12, "price": 400, "date": "2099-02-01", "location": "B", "seats": 5},
            ],
        }
        with (
            mock.patch("catalog_service.get_product", return_value=product),
            mock.patch(
                "catalog_service.get_company_discount_map",
                return_value={"v": {"discount_type": "percentage", "discount_value": 20, "min_participants": 2}},
            ),
        ):
            self.assertEqual(enrollment.quote_course("a", 7, session_id="11")["price"], 900)
            self.assertEqual(enrollment.quote_course("a", 7, session_id="11", participants=2)["price"], 720)
            with self.assertRaises(ValueError):
                enrollment.quote_course("a", 7, session_id="11", participants=3)
            with self.assertRaises(ValueError):
                enrollment.quote_course("a", 7, session_id="gone")
            with self.assertRaises(ValueError):
                enrollment.quote_course("a", 7)

    def test_other_learner_cannot_acknowledge_a_step(self):
        paths.save_steps(self.cur, 7, 1, [{"title": "Vejledning"}])
        paths.assign_path(self.cur, self.hr, 7, 1, [1])
        step = self.db.one("SELECT id FROM learning_assignment_steps")
        self.assertFalse(paths.acknowledge_step(self.cur, 7, 2, step["id"])["success"])


class RenewalLaunchTests(unittest.TestCase):
    setUp = _ComplianceFixture.setUp

    def test_expired_completion_can_be_renewed_but_open_renewal_is_not_duplicated(self):
        self.db.execute("UPDATE course_orders SET completion_date=%s WHERE user_id=2", (datetime.datetime(2020, 1, 1),))
        result = compliance_assign.assign_required_course(self.cur, self.hr, 7, 1)
        self.assertEqual(result["created"], 2)
        again = compliance_assign.assign_required_course(self.cur, self.hr, 7, 1)
        self.assertEqual(again["created"], 0)


class InternalAndBulkLaunchTests(LearningLaunchTests):
    def test_internal_course_is_tenant_scoped_and_uses_real_order_lifecycle(self):
        self.db.execute("INSERT INTO company_courses(id,company_id,title,price,location) VALUES(5,7,'Introduktion',250,'Kontoret')")
        learner = order_service.OrderContext(company_id=7, user_id=1, username="ada", company_role="employee", department="Drift")
        result = enrollment.create_order(learner, product_handle="internal:5")
        self.assertTrue(result["success"], result)
        oid = result["order_id"]
        row = self.db.one("SELECT * FROM course_orders WHERE order_id=%s", (oid,))
        self.assertEqual(row["internal_course_id"], 5)
        self.assertEqual(float(row["price"]), 250)
        self.assertIsNone(enrollment.get_course("internal:5", 8))
        self.assertTrue(order_service.set_status(self.hr, oid, "approved")["success"])
        self.assertTrue(order_service.book_order(self.hr, oid, booking={"date": "2020-01-01", "location": "Kontoret"})["success"])
        self.assertTrue(order_service.complete_order(learner, oid)["reported"])
        self.assertTrue(order_service.complete_order(self.hr, oid)["success"])

    def test_bulk_failure_rolls_back_all_people_and_messages(self):
        original = enrollment.create_order
        calls = []

        def create(ctx, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                return {"success": False, "message": "Test af delvis fejl"}
            return original(ctx, **kwargs)

        with mock.patch.object(enrollment, "create_order", side_effect=create):
            result = paths.assign_course_to_people(self.cur, self.hr, 7, "a", [1, 2])
        self.assertEqual(result["orders"], 0)
        self.assertEqual(self.db.query("SELECT * FROM course_orders"), [])
        self.assertEqual(self.db.query("SELECT * FROM notifications"), [])

    def test_invalid_ids_do_not_qualify_for_group_discount(self):
        product = {"handle": "a", "title": "A", "vendor": "V", "price_min": 100, "variants": []}
        with (
            mock.patch("catalog_service.get_product", return_value=product),
            mock.patch(
                "catalog_service.get_company_discount_map",
                return_value={"v": {"discount_type": "percentage", "discount_value": 20, "min_participants": 2}},
            ),
        ):
            result = paths.assign_course_to_people(self.cur, self.hr, 7, "a", [1, 99999])
        self.assertEqual(result["orders"], 1)
        self.assertEqual(float(self.db.one("SELECT price FROM course_orders")["price"]), 100)

    def test_changed_path_version_requires_new_confirmation(self):
        paths.save_steps(self.cur, 7, 1, [{"title": "Nyt trin"}])
        result = paths.assign_path(self.cur, self.hr, 7, 1, [1], expected_version=0)
        self.assertEqual(result["assigned"], 0)
        self.assertEqual(self.db.query("SELECT * FROM employee_learning_progress"), [])

    def test_inactive_supplier_is_blocked_on_shared_entrypoint(self):
        self.db.execute("INSERT INTO company_supplier_preferences(company_id,vendor_name,is_active) VALUES(7,'V',0)")
        with mock.patch("catalog_service.get_product", return_value={"title": "A", "vendor": "V", "price_min": 100}):
            result = enrollment.create_order(self.hr, product_handle="a")
        self.assertEqual(result["error"], "supplier_inactive")
