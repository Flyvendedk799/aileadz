"""Attendance and completion can never be confirmed before the course has taken place.

Decision: "completed" means the course is done. One guard in the services
(`order_fulfillment.report_completion`, `order_service.complete_order`) covers
every UI, AI tool and API path; there is no early-completion override.
"""
import datetime
from unittest import mock

import order_fulfillment as fulfillment
import order_service as svc
import order_timing
from tests.test_order_lifecycle import OrderFlowBase

COURSE_DAY = "12. november 2026"


def clock(*args):
    when = datetime.datetime(*args, tzinfo=order_timing.TZ)
    return mock.patch.object(order_timing, "now", lambda: when)


class CompletionGuardTests(OrderFlowBase):
    def booked(self, variant_date=COURSE_DAY, start_at="2026-11-12T09:00"):
        oid = self.create(variant_date=variant_date)["order_id"]
        self.assertTrue(svc.set_status(self.hr(), oid, "approved")["success"])
        self.assertTrue(svc.book_order(self.hr(), oid, booking={
            "reference": "TI-1", "start_at": start_at, "location": "Kontoret"})["success"])
        return oid

    def actors(self):
        return {
            "hr": self.hr(),
            "department_head": self.learner(uid=3, name="chef", role="department_head"),
            "vendor": svc.OrderContext.for_vendor(11, "Kursus ApS"),
            "admin": svc.OrderContext(user_id=5, username="admin", is_platform_admin=True),
        }

    def state(self, oid):
        return self.db.one("SELECT completion_state FROM course_order_details WHERE order_id=%s", (oid,))

    def test_nobody_can_confirm_or_report_before_the_course(self):
        with clock(2026, 10, 6, 12):
            oid = self.booked()
            owner = fulfillment.report_completion(self.learner(), oid, note="Jeg var der")
            self.assertEqual((owner["success"], owner["error"]), (False, "not_yet_held"))
            self.assertIn("Kurset er ikke afholdt endnu. Du kan registrere deltagelse fra 13. november 2026.", owner["message"])
            via_service = svc.complete_order(self.learner(), oid)
            self.assertEqual(via_service["error"], "not_yet_held")
            for name, ctx in self.actors().items():
                with self.subTest(actor=name):
                    result = svc.complete_order(ctx, oid)
                    self.assertFalse(result["success"])
                    self.assertEqual(result["error"], "not_yet_held")
                    self.assertIn("13. november 2026", result["message"])
                    again = svc.set_status(ctx, oid, "completed")
                    self.assertEqual(again["error"], "not_yet_held")
            self.assertEqual(self.order(oid)["status"], "booked")
            self.assertNotEqual((self.state(oid) or {}).get("completion_state"), "reported")

    def test_the_course_day_itself_is_not_over_yet(self):
        with clock(2026, 11, 12, 23, 30):
            oid = self.booked()
            self.assertEqual(fulfillment.report_completion(self.learner(), oid, note="x")["error"], "not_yet_held")
            self.assertEqual(svc.complete_order(self.hr(), oid)["error"], "not_yet_held")

    def test_the_day_after_everyone_can(self):
        for name in ("owner", "hr", "department_head", "vendor", "admin"):
            with self.subTest(actor=name), clock(2026, 11, 13, 8):
                oid = self.create(variant_date=COURSE_DAY, handle="itil" if name != "owner" else "prince2")["order_id"]
                self.assertTrue(svc.set_status(self.hr(), oid, "approved")["success"])
                self.assertTrue(svc.book_order(self.hr(), oid, booking={
                    "reference": "R", "start_at": "2026-11-12T09:00", "location": "X"})["success"])
                if name == "owner":
                    result = fulfillment.report_completion(self.learner(), oid, note="Jeg var der")
                    self.assertTrue(result["success"] and result["reported"])
                    self.assertEqual(self.order(oid)["status"], "booked")
                    self.assertEqual(self.state(oid)["completion_state"], "reported")
                else:
                    with mock.patch("completion_service.completion_moment", return_value={}):
                        result = svc.complete_order(self.actors()[name], oid)
                    self.assertTrue(result["success"], result)
                    self.assertEqual(self.order(oid)["status"], "completed")

    def test_a_multi_day_course_is_over_after_its_last_day(self):
        with clock(2026, 11, 13, 12):
            oid = self.booked(start_at="2026-11-12")
            self.db.execute("UPDATE course_order_details SET booking_json=%s WHERE order_id=%s",
                            ('{"start_at": "2026-11-12", "end_at": "2026-11-14", "location": "X"}', oid))
            self.assertEqual(svc.complete_order(self.hr(), oid)["error"], "not_yet_held")
        with clock(2026, 11, 15, 0, 5), mock.patch("completion_service.completion_moment", return_value={}):
            self.assertTrue(svc.complete_order(self.hr(), oid)["success"])

    def test_an_order_without_a_known_date_cannot_be_completed(self):
        with clock(2030, 1, 1):
            oid = self.booked()
            self.db.execute("UPDATE course_orders SET variant_date='' WHERE order_id=%s", (oid,))
            self.db.execute("UPDATE course_order_details SET booking_json='{}' WHERE order_id=%s", (oid,))
            result = svc.complete_order(self.hr(), oid)
            self.assertEqual(result["error"], "not_yet_held")
            self.assertIn("ingen bekræftet dato", result["message"])
            self.assertEqual(fulfillment.report_completion(self.learner(), oid, note="x")["error"], "not_yet_held")
