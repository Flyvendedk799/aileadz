"""Real lifecycle integration: requested changes, evidence and money stay aligned."""

import datetime
import json
from unittest import mock
from tests.test_order_lifecycle import OrderFlowBase
import order_service as orders
import order_fulfillment as fulfillment
import enrollment_service


class FulfillmentLaunchTests(OrderFlowBase):
    def booked(self):
        result = self.create(variant_date="2020-01-01")
        oid = result["order_id"]
        self.assertTrue(orders.set_status(self.hr(), oid, "approved")["success"])
        self.assertTrue(
            orders.book_order(self.hr(), oid, booking={"reference": "SUP-42", "date": "2020-01-01", "location": "Kontoret"})["success"]
        )
        return oid

    def test_requested_cancellation_does_not_change_booking_or_budget(self):
        oid = self.booked()
        result = orders.set_status(self.learner(), oid, "cancelled", note="Kan ikke deltage")
        self.assertTrue(result["pending"])
        self.assertEqual(self.order(oid)["status"], "booked")
        self.assertEqual(self.spent(), 2500)
        second = fulfillment.request_change(self.learner(), oid, "cancel")
        self.assertEqual(result["change_id"], second["change_id"])
        self.assertEqual(len(self.db.query("SELECT * FROM course_order_changes")), 1)

    def test_accepted_cancellation_retains_only_agreed_fee_and_is_idempotent(self):
        oid = self.booked()
        change = fulfillment.request_change(self.learner(), oid, "cancel", {"note": "Afbestilling"})
        result = fulfillment.resolve_change(self.hr(), oid, change["change_id"], True, note="Udbyderen bekræftede pr. mail", fee=300)
        self.assertTrue(result["success"])
        self.assertEqual(self.order(oid)["status"], "cancelled")
        self.assertEqual(self.spent(), 300)
        again = fulfillment.resolve_change(self.hr(), oid, change["change_id"], True, note="Gentaget", fee=300)
        self.assertTrue(again["unchanged"])
        self.assertEqual(self.spent(), 300)

    def test_rejected_cancellation_preserves_booking_and_charge(self):
        oid = self.booked()
        change = fulfillment.request_change(self.learner(), oid, "cancel")
        self.assertTrue(fulfillment.resolve_change(self.hr(), oid, change["change_id"], False, note="Fristen er overskredet")["success"])
        self.assertEqual(self.spent(), 2500)
        self.assertEqual(self.order(oid)["status"], "booked")

    def test_participant_cannot_accept_their_own_request_or_set_excessive_fee(self):
        oid = self.booked()
        change = fulfillment.request_change(self.learner(), oid, "cancel")
        self.assertFalse(fulfillment.resolve_change(self.learner(), oid, change["change_id"], True, note="ja")["success"])
        self.assertFalse(fulfillment.resolve_change(self.hr(), oid, change["change_id"], True, note="ja", fee=3000)["success"])
        self.assertEqual(self.spent(), 2500)

    def test_self_report_is_not_verified_completion_or_skill_gain(self):
        oid = self.booked()
        result = fulfillment.report_completion(self.learner(), oid, note="Jeg deltog", evidence_url="https://example.invalid/certificate")
        self.assertTrue(result["reported"])
        self.assertEqual(self.order(oid)["status"], "booked")
        self.assertEqual(self.db.query("SELECT * FROM user_completed_courses"), [])
        self.assertEqual(self.db.one("SELECT completion_state FROM course_order_details")["completion_state"], "reported")
        self.assertTrue(orders.complete_order(self.hr(), oid, note="Deltagerliste kontrolleret")["success"])
        self.assertEqual(self.db.one("SELECT completion_state FROM course_order_details")["completion_state"], "verified")
        result = fulfillment.outcome_review(self.hr(), oid, [{"name": "Projektledelse", "level": 4}], note="Anvender metoden i teamet")
        self.assertTrue(result["success"])
        snapshot = self.db.one("SELECT * FROM employee_skill_history WHERE source='post_course'")
        self.assertEqual(snapshot["order_id"], self.order(oid)["id"])
        self.assertTrue(fulfillment.outcome_review(self.hr(), oid, [{"name": "Projektledelse", "level": 5}], note="Gentaget")["unchanged"])

    def test_substitution_moves_budget_owner_and_baseline(self):
        oid = self.booked()
        self.db.execute("INSERT INTO users(id,username,email) VALUES(9,'new','new@example.invalid')")
        self.db.execute(
            "INSERT INTO company_users(company_id,user_id,username,role,department,status) VALUES(7,9,'new','employee','Drift','active')"
        )
        self.db.execute(
            "INSERT INTO department_budgets(company_id,department,annual_budget,spent,fiscal_year) VALUES(7,%s,10000,0,%s)",
            ("Drift", datetime.date.today().year),
        )
        change = fulfillment.request_change(self.hr(), oid, "substitute", {"user_id": 9})
        self.assertTrue(
            fulfillment.resolve_change(self.hr(), oid, change["change_id"], True, note="Udbyderen har bekræftet ny deltager")["success"]
        )
        self.assertEqual(self.order(oid)["user_id"], 9)
        self.assertEqual(self.db.one("SELECT user_id FROM course_order_details")["user_id"], 9)
        self.assertEqual(self.db.one("SELECT user_id FROM learning_outcome_reviews")["user_id"], 9)
        self.assertEqual(self.db.one("SELECT spent FROM department_budgets WHERE department='Salg'")["spent"], 0)
        self.assertEqual(self.db.one("SELECT spent FROM department_budgets WHERE department='Drift'")["spent"], 2500)

    def test_reschedule_rechecks_price_before_accepting(self):
        oid = self.booked()
        quote = {"price": 3000, "session_id": "s2", "variant_date": "2099-01-01", "variant_location": "Odense"}
        with mock.patch.object(enrollment_service, "quote_course", return_value=quote):
            change = fulfillment.request_change(self.learner(), oid, "reschedule", {"session_id": "s2"})
        with mock.patch.object(enrollment_service, "quote_course", return_value={**quote, "price": 3500}):
            self.assertFalse(fulfillment.resolve_change(self.hr(), oid, change["change_id"], True, note="Bekræftet")["success"])
        self.assertEqual(self.spent(), 2500)
        with mock.patch.object(enrollment_service, "quote_course", return_value=quote):
            self.assertTrue(fulfillment.resolve_change(self.hr(), oid, change["change_id"], True, note="Bekræftet")["success"])
        self.assertEqual(self.spent(), 3000)
        self.assertEqual(self.order(oid)["variant_date"], "2099-01-01")

    def test_verified_course_finishes_matching_personal_plan_step(self):
        self.db.execute(
            "INSERT INTO user_learning_paths(username,title,steps) VALUES(%s,%s,%s)",
            ("ada", "Min plan", json.dumps([{"courses": [{"handle": "prince2"}]}, {"topic": "Praktisk øvelse", "done": False}])),
        )
        oid = self.booked()
        self.assertTrue(orders.complete_order(self.hr(), oid)["success"])
        plan = self.db.one("SELECT * FROM user_learning_paths")
        steps = json.loads(plan["steps"])
        self.assertEqual(steps[0]["completed_order_id"], oid)
        self.assertFalse(steps[1]["done"])
        self.assertEqual(plan["status"], "aktiv")

    def test_booking_time_is_danish_and_end_must_be_later(self):
        values = fulfillment.booking_values({}, {"start_at": "2099-01-01T09:00", "end_at": "2099-01-01T12:00", "location": "København"})
        self.assertTrue(values["start_at"].endswith("+01:00"))
        with self.assertRaises(ValueError):
            fulfillment.booking_values({}, {"start_at": "2099-01-01T12:00", "end_at": "2099-01-01T09:00", "location": "København"})

    def test_deadlock_replays_only_after_full_rollback_and_creates_one_order(self):
        import pymysql

        original = orders._record_history
        calls = 0

        def history(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise pymysql.err.OperationalError(1213, "simulated transaction deadlock")
            return original(*args, **kwargs)

        with mock.patch.object(orders, "_record_history", side_effect=history), mock.patch("time.sleep"):
            result = self.create()
        self.assertTrue(result["success"], result)
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.db.query("SELECT * FROM course_orders")), 1)
        self.assertEqual(len(self.db.query("SELECT * FROM order_approvals")), 1)
        self.assertEqual(len([e for e in self.emails if e[1] == "order_confirmation"]), 1)

    def test_deadlock_does_not_replay_one_member_of_an_atomic_batch(self):
        import pymysql

        with mock.patch.object(orders, "_record_history", side_effect=pymysql.err.OperationalError(1213, "deadlock")) as record:
            result = self.create(deferred_events=[])
        self.assertFalse(result["success"])
        self.assertEqual(record.call_count, 1)
        self.assertEqual(self.db.query("SELECT * FROM course_orders"), [])

    def test_best_effort_notification_does_not_hide_transaction_abort(self):
        import pymysql
        import notification_service

        cur = mock.Mock()
        cur.execute.side_effect = pymysql.err.OperationalError(1213, "deadlock")
        with self.assertRaises(pymysql.err.OperationalError):
            notification_service.notify_user(cur, user_id=1, username="ada", title="Order")
