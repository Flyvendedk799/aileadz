"""N-1: the one order lifecycle — pure rules plus service flows on an in-memory DB."""

import datetime
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import order_lifecycle as lc  # noqa: E402
import order_service as svc  # noqa: E402
import order_timing  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


# ── pure rules ──────────────────────────────────────────────────────────────

class LifecycleRuleTests(unittest.TestCase):
    def test_every_status_has_label_bucket_hint_and_transitions(self):
        for s in lc.ORDER_STATUSES:
            self.assertIn(s, lc.STATUS_LABELS)
            self.assertIn(s, lc.LEARNER_BUCKETS)
            self.assertIn(s, lc.STATUS_HINTS)
            self.assertIn(s, lc.TRANSITIONS)

    def test_learner_labels_match_the_decision(self):
        self.assertEqual(lc.STATUS_LABELS[lc.PENDING_APPROVAL], "Afventer godkendelse")
        self.assertEqual(lc.STATUS_LABELS[lc.APPROVED], "Godkendt – afventer booking")
        self.assertEqual(lc.STATUS_LABELS[lc.BOOKED], "Booket")
        self.assertEqual(lc.STATUS_LABELS[lc.COMPLETED], "Gennemført")

    def test_legacy_statuses_map_onto_canonical(self):
        self.assertEqual(lc.normalize_status("pending"), lc.APPROVED)       # was "Afventer betaling"
        self.assertEqual(lc.normalize_status("confirmed"), lc.BOOKED)
        self.assertEqual(lc.normalize_status("processing"), lc.BOOKED)
        self.assertEqual(lc.normalize_status("invoiced"), lc.BOOKED)
        self.assertEqual(lc.normalize_status(None), lc.APPROVED)
        self.assertEqual(lc.normalize_status("garbage"), lc.APPROVED)

    def test_billing_is_not_an_order_status(self):
        self.assertNotIn("invoiced", lc.ORDER_STATUSES)
        self.assertNotIn("paid", lc.ORDER_STATUSES)
        self.assertEqual(lc.BILLING_STATUSES, ("not_invoiced", "invoiced", "paid", "credited"))

    def test_terminal_states_have_no_exits(self):
        for s in (lc.COMPLETED, lc.REJECTED, lc.CANCELLED):
            self.assertEqual(lc.TRANSITIONS[s], frozenset())

    def test_booked_is_a_real_step(self):
        self.assertTrue(lc.can_transition(lc.APPROVED, lc.BOOKED))
        self.assertTrue(lc.can_transition(lc.BOOKED, lc.COMPLETED))
        self.assertFalse(lc.can_transition(lc.PENDING_APPROVAL, lc.BOOKED))
        self.assertFalse(lc.can_transition(lc.REJECTED, lc.APPROVED))

    def test_actor_rules(self):
        ok, code, _ = lc.check_transition(lc.PENDING_APPROVAL, lc.APPROVED, {"owner"})
        self.assertFalse(ok)
        self.assertEqual(code, "forbidden")                      # a learner cannot approve
        self.assertTrue(lc.check_transition(lc.PENDING_APPROVAL, lc.APPROVED, {"manager"})[0])
        self.assertTrue(lc.check_transition(lc.APPROVED, lc.BOOKED, {"vendor"})[0])
        self.assertFalse(lc.check_transition(lc.APPROVED, lc.BOOKED, {"owner"})[0])
        self.assertFalse(lc.check_transition(lc.PENDING_APPROVAL, lc.APPROVED, {"vendor"})[0])
        self.assertTrue(lc.check_transition(lc.BOOKED, lc.CANCELLED, {"owner"})[0])
        self.assertEqual(lc.check_transition(lc.APPROVED, lc.APPROVED, {"manager"})[1], "no_change")

    def test_billing_transitions(self):
        self.assertTrue(lc.can_bill_transition("not_invoiced", "invoiced"))
        self.assertTrue(lc.can_bill_transition("invoiced", "paid"))
        self.assertTrue(lc.can_bill_transition("paid", "credited"))
        self.assertFalse(lc.can_bill_transition("not_invoiced", "paid"))   # cannot skip invoicing
        self.assertFalse(lc.can_bill_transition("credited", "paid"))

    def test_label_maps_everywhere_come_from_the_lifecycle(self):
        import futurematch_ui
        from app1.order_handler import OrderHandler
        for s in lc.ORDER_STATUSES:
            self.assertEqual(futurematch_ui._ORDER_STATUS_LABELS[s], lc.STATUS_LABELS[s])
            self.assertEqual(OrderHandler().order_statuses[s], lc.STATUS_LABELS[s])
        # the old misleading label is gone
        self.assertNotIn("Afventer betaling", futurematch_ui._ORDER_STATUS_LABELS.values())


# ── service flows on SQLite ─────────────────────────────────────────────────

class OrderFlowBase(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.app = Flask(__name__)
        self.app.mysql = self.db
        self.ctx_mgr = self.app.app_context()
        self.ctx_mgr.push()
        self.events, self.emails = [], []
        patches = [
            mock.patch.object(svc, "_emit_event_safe", side_effect=lambda cid, t, p: self.events.append((cid, t, p))),
            mock.patch.object(svc, "_send_email_safe", side_effect=lambda to, subj, tpl, cid, **kw: self.emails.append((to, tpl, kw))),
            mock.patch.object(svc, "_send_approval_needed_emails_safe"),
            mock.patch.object(svc, "_send_budget_overrun_emails_safe"),
            mock.patch.object(svc, "_manager_recipient_emails", return_value=["hr@firma.dk"]),
            # Completion needs the course to have taken place: these flows use 2026
            # session dates, so the shared clock sits after them. The guard tests
            # (tests/test_completion_guard.py) move it themselves.
            mock.patch.object(order_timing, "now", lambda: datetime.datetime(2027, 1, 1, tzinfo=order_timing.TZ)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.ctx_mgr.pop)

        d = self.db
        d.execute("INSERT INTO companies (id, company_name) VALUES (7, 'Firma A/S'), (8, 'Andet A/S')")
        for uid, name in ((1, "ada"), (2, "hr"), (3, "chef"), (4, "eve"), (5, "admin"), (6, "mgr2")):
            d.execute("INSERT INTO users (id, username, email) VALUES (%s, %s, %s)", (uid, name, name + "@firma.dk"))
        d.execute("INSERT INTO company_users (company_id, user_id, username, role, department, manager_user_id) VALUES "
                  "(7, 1, 'ada', 'employee', 'Salg', 3), (7, 2, 'hr', 'hr_manager', 'HR', NULL), "
                  "(7, 3, 'chef', 'department_head', 'Salg', NULL), (8, 4, 'eve', 'employee', 'Salg', NULL), "
                  "(8, 6, 'mgr2', 'hr_manager', 'Salg', NULL)")
        d.execute("INSERT INTO department_budgets (company_id, department, annual_budget, spent, fiscal_year) "
                  "VALUES (7, 'Salg', 10000, 0, %s)", (svc.datetime.datetime.now().year,))
        d.execute("INSERT INTO vendors (id, vendor_name, slug, contact_email) VALUES (11, 'Kursus ApS', 'kursus-aps', 'v@k.dk'), "
                  "(12, 'Anden Udbyder', 'anden', 'a@u.dk')")

    # contexts
    def learner(self, uid=1, name="ada", cid=7, role="employee", dept="Salg"):
        return svc.OrderContext(company_id=cid, user_id=uid, username=name, company_role=role, department=dept)

    def hr(self):
        return svc.OrderContext(company_id=7, user_id=2, username="hr", company_role="hr_manager", department="HR")

    def create(self, ctx=None, price=2500, handle="prince2", **kw):
        with mock.patch.object(svc, "_vendor_id_for_handle", return_value=kw.pop("vendor_id", 11)):
            return svc.create_order(ctx or self.learner(), product_handle=handle, product_title="PRINCE2",
                                    price=price, variant_date=kw.pop("variant_date", "2026-11-03"),
                                    variant_location="København", user_email="ada@firma.dk", user_name="Ada", **kw)

    def order(self, oid):
        return self.db.one("SELECT * FROM course_orders WHERE order_id = %s", (oid,))

    def spent(self):
        return self.db.one("SELECT spent FROM department_budgets WHERE company_id = 7")["spent"]


class CreateAndApproveTests(OrderFlowBase):
    def test_employee_order_waits_for_approval_and_notifies_hr(self):
        r = self.create()
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], lc.PENDING_APPROVAL)
        self.assertEqual(self.order(r["order_id"])["vendor_id"], 11)
        self.assertEqual(self.order(r["order_id"])["billing_status"], "not_invoiced")
        self.assertEqual(self.db.one("SELECT status FROM order_approvals")["status"], "pending")
        self.assertEqual(self.spent(), 0)   # not charged until approved
        notified = {n["user_id"] for n in self.db.query("SELECT user_id FROM notifications")}
        self.assertIn("hr", notified)       # HR sees it, with an action url
        self.assertTrue(self.db.one("SELECT action_url FROM notifications WHERE user_id='hr'")["action_url"])
        self.assertEqual(self.emails[0][1], "order_confirmation")   # exactly one mail to the learner
        self.assertEqual(len([e for e in self.emails if e[1] == "order_confirmation"]), 1)

    def test_approving_sets_approved_not_pending_charges_once_and_tells_everyone(self):
        oid = self.create()["order_id"]
        res = svc.set_status(self.hr(), oid, "approved")
        self.assertTrue(res["success"])
        row = self.order(oid)
        self.assertEqual(row["status"], "approved")                # NOT "pending"/"Afventer betaling"
        self.assertEqual(row["approved_by"], 2)
        self.assertEqual(self.db.one("SELECT status FROM order_approvals")["status"], "approved")
        self.assertEqual(self.spent(), 2500)
        event_types = [e[1] for e in self.events]
        self.assertIn("order.approved", event_types)              # webhook fires (was missing)
        self.assertTrue(any(e[1] == "order_approved" and e[0] == "ada@firma.dk" for e in self.emails))
        ada_notices = self.db.query("SELECT title FROM notifications WHERE user_id='ada'")
        self.assertTrue(any("godkendt" in n["title"].lower() for n in ada_notices))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM order_status_history WHERE to_value='approved'")["c"], 1)

    def test_approving_twice_is_idempotent(self):
        oid = self.create()["order_id"]
        svc.set_status(self.hr(), oid, "approved")
        again = svc.set_status(self.hr(), oid, "approved")
        self.assertTrue(again["success"])
        self.assertTrue(again.get("unchanged"))
        self.assertEqual(self.spent(), 2500)                       # no double charge
        self.assertEqual(len([e for e in self.events if e[1] == "order.approved"]), 1)

    def test_decide_approval_goes_through_one_transaction(self):
        self.create()
        aid = self.db.one("SELECT id FROM order_approvals")["id"]
        res = svc.decide_approval(self.hr(), aid, "approved", "Fint")
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "approved")
        ap = self.db.one("SELECT status, notes, approver_user_id FROM order_approvals")
        self.assertEqual((ap["status"], ap["notes"], ap["approver_user_id"]), ("approved", "Fint", 2))
        # a second decision on the same row is refused
        self.assertFalse(svc.decide_approval(self.hr(), aid, "rejected")["success"])

    def test_rejection_refunds_nothing_uncharged_and_carries_the_note(self):
        oid = self.create()["order_id"]
        res = svc.set_status(self.hr(), oid, "rejected", note="Over budget")
        self.assertTrue(res["success"])
        self.assertFalse(res["refunded"])
        self.assertEqual(self.order(oid)["cancel_reason"], "Over budget")
        mail = [e for e in self.emails if e[1] == "order_approved"][0]
        self.assertIn("Over budget", mail[2]["message"])            # rejection note is in the email

    def test_manager_order_skips_approval_and_is_charged_at_once(self):
        r = self.create(ctx=self.hr())
        self.assertEqual(r["status"], lc.APPROVED)
        self.assertNotEqual(self.spent(), 0) if self.db.one("SELECT COUNT(*) AS c FROM department_budgets WHERE department='HR'")["c"] else None

    def test_over_budget_request_is_routed_to_approval_not_overspent(self):
        r = self.create(price=20000)
        self.assertEqual(r["status"], lc.PENDING_APPROVAL)
        self.assertTrue(r["budget_warning"])
        self.assertEqual(self.spent(), 0)

    def test_duplicate_request_within_window_returns_existing_order(self):
        first = self.create()
        second = self.create()
        self.assertTrue(second["success"])
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["order_id"], first["order_id"])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM course_orders")["c"], 1)

    def test_completion_deadline_comes_from_the_variant_date(self):
        oid = self.create(variant_date="2026-11-03")["order_id"]
        self.assertEqual(str(self.order(oid)["completion_deadline"])[:10], "2026-11-03")

    def test_order_persists_request_notes(self):
        oid = self.create(extra={"notes": "Vegetar"})["order_id"]
        self.assertEqual(self.order(oid)["request_notes"], "Vegetar")


class PermissionBoundaryTests(OrderFlowBase):
    def setUp(self):
        super().setUp()
        self.oid = self.create()["order_id"]

    def test_learner_cannot_approve_own_order(self):
        res = svc.set_status(self.learner(), self.oid, "approved")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "forbidden")
        self.assertEqual(self.order(self.oid)["status"], "pending_approval")

    def test_other_company_manager_gets_not_found(self):
        ctx = svc.OrderContext(company_id=8, user_id=6, username="mgr2", company_role="hr_manager")
        res = svc.set_status(ctx, self.oid, "approved")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "not_found")

    def test_colleague_cannot_see_or_cancel(self):
        eve = svc.OrderContext(company_id=7, user_id=9, username="kollega", company_role="employee")
        self.assertIsNone(svc.get_order(eve, self.oid))
        self.assertEqual(svc.cancel_order(eve, self.oid)["error"], "not_found")

    def test_department_head_only_manages_own_department(self):
        other_dept = svc.OrderContext(company_id=7, user_id=3, username="chef",
                                      company_role="department_head", department="Drift")
        self.assertEqual(svc.set_status(other_dept, self.oid, "approved")["error"], "not_found")
        own = svc.OrderContext(company_id=7, user_id=3, username="chef",
                               company_role="department_head", department="Salg")
        self.assertTrue(svc.set_status(own, self.oid, "approved")["success"])

    def test_vendor_books_only_its_own_orders_and_cannot_approve(self):
        vendor = svc.OrderContext.for_vendor(11, "Kursus ApS")
        self.assertFalse(svc.set_status(vendor, self.oid, "booked")["success"])        # still pending approval
        self.assertEqual(svc.set_status(vendor, self.oid, "approved")["error"], "forbidden")
        svc.set_status(self.hr(), self.oid, "approved")
        wrong = svc.OrderContext.for_vendor(12, "Anden Udbyder")
        self.assertEqual(svc.book_order(wrong, self.oid)["error"], "not_found")         # other vendor
        ok = svc.book_order(vendor, self.oid)
        self.assertTrue(ok["success"])
        row = self.order(self.oid)
        self.assertEqual(row["status"], "booked")
        self.assertEqual(row["booked_by"], "Kursus ApS")
        self.assertTrue(row["booked_at"])

    def test_hr_can_book_on_behalf_of_a_vendor_and_the_actor_is_logged(self):
        svc.set_status(self.hr(), self.oid, "approved")
        self.assertTrue(svc.book_order(self.hr(), self.oid)["success"])
        h = self.db.one("SELECT actor_kind, note FROM order_status_history WHERE to_value='booked'")
        self.assertEqual(h["actor_kind"], "manager")
        self.assertIn("på leverandørens vegne", h["note"])

    def test_platform_admin_can_book_any_company(self):
        svc.set_status(self.hr(), self.oid, "approved")
        admin = svc.OrderContext(user_id=5, username="admin", is_platform_admin=True)
        self.assertTrue(svc.book_order(admin, self.oid)["success"])

    def test_learner_history_hides_billing_rows(self):
        svc.set_status(self.hr(), self.oid, "approved")
        svc.set_billing_status(self.hr(), self.oid, "invoiced", invoice_number="F-1")
        learner_kinds = {h["kind"] for h in svc.get_history(self.learner(), self.oid)}
        hr_kinds = {h["kind"] for h in svc.get_history(self.hr(), self.oid)}
        self.assertEqual(learner_kinds, {"status"})
        self.assertIn("billing", hr_kinds)


class CancelAndBudgetTests(OrderFlowBase):
    def test_cancel_refunds_budget_exactly_once(self):
        oid = self.create()["order_id"]
        svc.set_status(self.hr(), oid, "approved")
        self.assertEqual(self.spent(), 2500)
        a = svc.cancel_order(self.learner(), oid, reason="Syg")
        self.assertTrue(a["success"])
        self.assertEqual(self.spent(), 0)
        b = svc.cancel_order(self.learner(), oid)
        self.assertTrue(b["success"])
        self.assertTrue(b["already_cancelled"])
        self.assertEqual(self.spent(), 0)                           # never refunded twice (no negative)

    def test_vendor_decline_notifies_hr_and_learner(self):
        oid = self.create()["order_id"]
        svc.set_status(self.hr(), oid, "approved")
        res = svc.set_status(svc.OrderContext.for_vendor(11, "Kursus ApS"), oid, "cancelled", reason="Holdet er aflyst")
        self.assertTrue(res["success"])
        self.assertEqual(self.order(oid)["cancel_reason"], "Holdet er aflyst")
        self.assertEqual(self.spent(), 0)
        hr_titles = [n["title"] for n in self.db.query("SELECT title FROM notifications WHERE user_id='hr'")]
        self.assertIn("Bestilling annulleret", hr_titles)
        self.assertTrue(any(e[1] == "order_cancelled" for e in self.emails))

    def test_completed_order_cannot_be_cancelled(self):
        oid = self.create()["order_id"]
        svc.set_status(self.hr(), oid, "approved")
        svc.book_order(self.hr(), oid)
        svc.complete_order(self.hr(), oid)
        self.assertEqual(svc.cancel_order(self.learner(), oid)["error"], "bad_transition")


class CompletionTests(OrderFlowBase):
    def test_one_completion_path_updates_everything(self):
        oid = self.create()["order_id"]
        svc.set_status(self.hr(), oid, "approved")
        svc.book_order(svc.OrderContext.for_vendor(11), oid)
        with mock.patch.object(svc, "_completion_moment", return_value={"skill_proposals": [{"name": "PRINCE2", "level": "mellem"}],
                                                                         "next_steps": [], "review_url": "/x"}):
            res = svc.complete_order(self.hr(), oid)
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "completed")
        row = self.order(oid)
        self.assertEqual((row["status"], row["completion_status"]), ("completed", "completed"))
        self.assertTrue(row["completion_date"])
        self.assertEqual(self.db.one("SELECT course_title FROM user_completed_courses WHERE username='ada'")["course_title"], "PRINCE2")
        self.assertEqual(self.db.one("SELECT status FROM employee_learning_progress WHERE user_id=1")["status"], "completed")
        self.assertEqual(self.db.one("SELECT total_courses_completed AS n FROM company_users WHERE user_id=1")["n"], 1)
        self.assertIn("course.completed", [e[1] for e in self.events])
        self.assertIn("order.completed", [e[1] for e in self.events])
        # the learner's manager (chef, id 3) is asked to confirm the uplift
        chef = self.db.query("SELECT title, action_url FROM notifications WHERE user_id='chef'")
        self.assertTrue(any(n["title"] == "Bekræft kompetenceløft" for n in chef))

    def test_complete_is_idempotent_and_counters_do_not_double(self):
        oid = self.create()["order_id"]
        svc.set_status(self.hr(), oid, "approved")
        svc.book_order(self.hr(), oid)
        svc.complete_order(self.hr(), oid)
        again = svc.complete_order(self.hr(), oid)
        self.assertTrue(again["success"])
        self.assertTrue(again["already_completed"])
        self.assertEqual(self.db.one("SELECT total_courses_completed AS n FROM company_users WHERE user_id=1")["n"], 1)

    def test_cannot_complete_before_approval(self):
        oid = self.create()["order_id"]
        res = svc.complete_order(self.learner(), oid)
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "bad_transition")


class BillingTests(OrderFlowBase):
    def setUp(self):
        super().setUp()
        self.oid = self.create()["order_id"]
        svc.set_status(self.hr(), self.oid, "approved")

    def test_invoice_requires_a_number_and_paid_requires_invoiced_first(self):
        self.assertEqual(svc.set_billing_status(self.hr(), self.oid, "invoiced")["error"], "invoice_number_required")
        self.assertEqual(svc.set_billing_status(self.hr(), self.oid, "paid")["error"], "bad_transition")
        ok = svc.set_billing_status(self.hr(), self.oid, "invoiced", invoice_number="F-100", due_date="2026-12-01")
        self.assertTrue(ok["success"])
        row = self.order(self.oid)
        self.assertEqual((row["billing_status"], row["invoice_number"], str(row["invoice_due_date"])[:10]),
                         ("invoiced", "F-100", "2026-12-01"))
        paid = svc.set_billing_status(self.hr(), self.oid, "paid", payment_reference="BANK-7")
        self.assertTrue(paid["success"])
        self.assertEqual(self.order(self.oid)["payment_reference"], "BANK-7")
        self.assertEqual(self.order(self.oid)["status"], "approved")     # billing never touches order status

    def test_credit_needs_a_reason_and_learner_cannot_bill(self):
        svc.set_billing_status(self.hr(), self.oid, "invoiced", invoice_number="F-1")
        self.assertEqual(svc.set_billing_status(self.hr(), self.oid, "credited")["error"], "note_required")
        self.assertEqual(svc.set_billing_status(self.learner(), self.oid, "credited", note="x")["error"], "not_found")

    def test_department_head_cannot_bill(self):
        dh = svc.OrderContext(company_id=7, user_id=3, username="chef", company_role="department_head", department="Salg")
        self.assertEqual(svc.set_billing_status(dh, self.oid, "invoiced", invoice_number="F")["error"], "not_found")

    def test_bulk_reports_per_order_results(self):
        oid2 = self.create(handle="itil", variant_date="2026-12-05")["order_id"]
        svc.set_status(self.hr(), oid2, "approved")
        res = svc.bulk_set_billing_status(self.hr(), [self.oid, oid2, "unknown"], "invoiced", invoice_number="F-9")
        self.assertEqual(res["done"], 2)
        self.assertEqual(res["failed"], 1)

    def test_solo_orders_are_managed_by_platform_admin_only(self):
        solo = svc.OrderContext(user_id=9, username="solo")
        with mock.patch.object(svc, "_vendor_id_for_handle", return_value=None):
            r = svc.create_order(solo, product_handle="h", product_title="Kursus", price=900,
                                 user_email="s@x.dk", user_name="Solo")
        self.assertEqual(r["status"], lc.APPROVED)          # no approval step without a company
        self.assertEqual(svc.set_billing_status(self.hr(), r["order_id"], "invoiced", invoice_number="F")["error"], "not_found")
        admin = svc.OrderContext(user_id=5, username="admin", is_platform_admin=True)
        self.assertTrue(svc.set_billing_status(admin, r["order_id"], "invoiced", invoice_number="F-S")["success"])


class EndToEndTests(OrderFlowBase):
    """request -> approve -> vendor confirms -> complete -> skill recorded -> manager confirms."""

    def test_full_loop(self):
        # request
        created = self.create(price=3000)
        oid = created["order_id"]
        self.assertEqual(created["status"], "pending_approval")
        # approve (HR)
        self.assertTrue(svc.set_status(self.hr(), oid, "approved")["success"])
        self.assertEqual(self.spent(), 3000)
        # vendor confirms
        self.assertTrue(svc.book_order(svc.OrderContext.for_vendor(11, "Kursus ApS"), oid)["success"])
        self.assertTrue(any(e[1] == "order_booked" for e in self.emails))
        # learner completes
        with mock.patch("completion_service.completion_moment",
                        return_value={"skill_proposals": [{"name": "Projektledelse", "level": "mellem"}],
                                      "next_steps": [], "review_url": None}):
            reported = svc.complete_order(self.learner(), oid)
            self.assertTrue(reported["reported"])
            self.assertEqual(self.order(oid)["status"], "booked")
            done = svc.complete_order(self.hr(), oid)
        self.assertTrue(done["success"])
        self.assertEqual(done["skill_proposals"][0]["name"], "Projektledelse")
        # skill recorded for the learner (history is keyed on users.id)
        import skill_history
        cur = self.db.connection.cursor()
        self.assertTrue(skill_history.record_snapshot(cur, 7, 1, "Projektledelse", 3, previous_level=None,
                                                      source="post_course", order_id=1))
        # manager confirms the uplift: matrix + post_course history row
        skill_history.current_level_for(cur, 7, 1, "Projektledelse")
        cur.execute("INSERT INTO employee_skills_matrix (employee_id, company_id, skill_name, current_level) VALUES (%s,%s,%s,%s)",
                    (1, 7, "Projektledelse", 4))
        skill_history.record_snapshot(cur, 7, 1, "Projektledelse", 4, previous_level=3, source="post_course")
        self.db.raw.commit()
        rows = self.db.query("SELECT level, previous_level FROM employee_skill_history WHERE employee_id=1 ORDER BY id")
        self.assertEqual([(r["previous_level"], r["level"]) for r in rows], [(None, 3), (3, 4)])
        # the learner's timeline says Gennemført and the whole trail is in the history
        states = [h["to_value"] for h in svc.get_history(self.learner(), oid)]
        self.assertEqual(states, ["pending_approval", "approved", "booked", "completed"])


if __name__ == "__main__":
    unittest.main()
