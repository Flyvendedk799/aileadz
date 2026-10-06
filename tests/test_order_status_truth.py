"""Order status, date and completion truth (regression tests for review E1-E5, H3).

* every order page shows exactly one status badge that matches the order's real state;
* no page shows an ISO timestamp or a price outside the ``dkmoney`` format;
* a cancel request on a booked order reads as a request;
* status/approval labels, including legacy aliases, come from ``order_lifecycle``;
* attendance and completion are refused before the course has taken place, for every
  actor, date position and entry point (service, route, AI tool).

Offline: SQLite harness (``tests/sqlite_platform.py``), the clock is
``order_timing.now`` patched per call.
"""
import datetime
import json
import os
import re
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import order_fulfillment as fulfillment  # noqa: E402
import order_lifecycle as lc  # noqa: E402
import order_service as svc  # noqa: E402
import order_timing  # noqa: E402
from tests import test_learner_orders as lo  # noqa: E402
from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402

ISO = re.compile(r"\d{4}-\d{2}-\d{2}T")
BARE_KR = re.compile(r"\d kr\b(?!\.)")        # a price that is not rendered by dkmoney ("12.500 kr.")
BADGE = re.compile(r'<span class="fm-badge[^"]*"[^>]*>(?:<i[^>]*></i>)?\s*([^<]+?)\s*</span>')
STATUS_TEXTS = set(lc.STATUS_LABELS.values()) | set(lc.STATUS_LABELS_SHORT.values())


def at(*args):
    return mock.patch.object(order_timing, "now", lambda: datetime.datetime(*args, tzinfo=order_timing.TZ))


def status_badges(html):
    """Texts of the badges that name an order status (not billing, change or other chips)."""
    return [text for text in BADGE.findall(html) if text in STATUS_TEXTS]


# -- 1. rendering per state ----------------------------------------------------
class RenderingPerStateTests(lo.Base):
    DB_CLASS = PlatformDB

    STATES = (
        # (name, status, approval, pending change, expected label)
        ("pending_approval", "pending_approval", "pending", False, "Afventer godkendelse"),
        ("approved", "approved", "approved", False, "Godkendt – afventer booking"),
        ("booked", "booked", "approved", False, "Booket"),
        ("booked_with_change", "booked", "approved", True, "Booket"),
        ("cancelled", "cancelled", "approved", False, "Annulleret"),
        ("completed", "completed", "approved", False, "Gennemført"),
    )

    def setUp(self):
        super().setUp()
        self.db.execute("UPDATE course_orders SET variant_date='3. december 2026', price=12500 WHERE order_id='ord-1'")
        self.db.execute("INSERT INTO course_order_details (order_id, company_id, user_id, booking_json) VALUES "
                        "('ord-1', 7, 1, %s)", ('{"start_at": "2026-12-03T09:00:00+01:00", "location": "Kontoret", '
                                                '"reference": "TI-48213"}',))
        patcher = mock.patch("completion_service.completion_moment",
                             return_value={"skill_proposals": [], "next_steps": [], "review_url": None,
                                           "course_title": "PRINCE2"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def put_in_state(self, status, approval, change):
        self.db.execute("UPDATE course_orders SET status=%s WHERE order_id='ord-1'", (status,))
        self.db.execute("DELETE FROM order_approvals")
        self.db.execute("INSERT INTO order_approvals (order_id, company_id, status) VALUES ('ord-1', 7, %s)", (approval,))
        self.db.execute("DELETE FROM course_order_changes")
        if change:
            self.db.execute("INSERT INTO course_order_changes (order_id, company_id, user_id, kind, status) "
                            "VALUES ('ord-1', 7, 1, 'cancel', 'pending')")

    def pages(self):
        return {
            "tidslinje": self.client_as("ada", 1).get("/min-tidslinje"),
            "min-ordre": self.client_as("ada", 1).get("/min-ordre/ord-1"),
            "hr": self.client_as("hr", 3, "hr_manager").get("/hr/order/ord-1/details"),
        }

    def test_every_state_shows_one_status_badge_danish_dates_and_one_price_format(self):
        for name, status, approval, change, label in self.STATES:
            with self.subTest(state=name):
                self.put_in_state(status, approval, change)
                for page, resp in self.pages().items():
                    self.assertEqual(resp.status_code, 200, (name, page))
                    html = resp.get_data(as_text=True)
                    self.assertEqual(status_badges(html), [label], (name, page))
                    self.assertIsNone(ISO.search(html), (name, page))
                    self.assertIsNone(BARE_KR.search(html), (name, page))
                    self.assertIn("12.500 kr.", html, (name, page))
                    # the pending-change chip appears exactly when a change is open
                    self.assertEqual("Ændring afventer svar" in html, change and page != "hr", (name, page))

    def test_the_booked_date_reads_as_a_danish_date_with_time(self):
        self.put_in_state("booked", "approved", False)
        for page in ("min-ordre", "hr"):
            self.assertIn("3. december 2026 kl. 09.00", self.pages()[page].get_data(as_text=True), page)

    def test_cancel_request_says_request_and_keeps_the_order_booked(self):
        self.put_in_state("booked", "approved", False)
        c = self.client_as("ada", 1)
        html = c.post("/min-ordre/ord-1/annuller", data={"reason": "Syg"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Afbestillingen er sendt til udbyderen", html)
        self.assertNotIn("Ordren er annulleret", html)
        self.assertEqual(status_badges(html), ["Booket"])
        self.assertIn("Din afbestilling afventer svar fra udbyderen", html)
        self.assertEqual(self.db.one("SELECT status FROM course_orders WHERE order_id='ord-1'")["status"], "booked")
        timeline = c.get("/min-tidslinje").get_data(as_text=True)
        self.assertEqual(status_badges(timeline), ["Booket"])
        self.assertIn("Ændring afventer svar", timeline)


# -- 2. labels -------------------------------------------------------------------
class LabelTableTests(unittest.TestCase):
    ORDER = (
        ("pending_approval", "Afventer godkendelse", "Afventer godkendelse"),
        ("approved", "Godkendt – afventer booking", "Godkendt"),
        ("booked", "Booket", "Booket"),
        ("completed", "Gennemført", "Gennemført"),
        ("rejected", "Afvist", "Afvist"),
        ("cancelled", "Annulleret", "Annulleret"),
        # legacy aliases
        ("pending", "Godkendt – afventer booking", "Godkendt"),
        ("processing", "Booket", "Booket"),
        ("confirmed", "Booket", "Booket"),
        ("invoiced", "Booket", "Booket"),
        ("paid", "Booket", "Booket"),
        ("canceled", "Annulleret", "Annulleret"),
        (" BOOKED ", "Booket", "Booket"),
        # unknown / empty fall back to approved
        (None, "Godkendt – afventer booking", "Godkendt"),
        ("", "Godkendt – afventer booking", "Godkendt"),
        ("noget-andet", "Godkendt – afventer booking", "Godkendt"),
    )
    APPROVAL = (
        ("pending", "Afventer godkendelse"),
        ("approved", "Godkendt"),
        ("rejected", "Afvist"),
        (" Pending ", "Afventer godkendelse"),
        ("APPROVED", "Godkendt"),
        (None, ""),
        ("", ""),
        ("noget", ""),
    )

    def test_status_label_covers_canonical_statuses_and_legacy_aliases(self):
        for raw, long_label, short_label in self.ORDER:
            with self.subTest(raw=raw):
                self.assertEqual(lc.status_label(raw), long_label)
                self.assertEqual(lc.status_label(raw, short=True), short_label)

    def test_approval_label_has_its_own_vocabulary(self):
        for raw, label in self.APPROVAL:
            with self.subTest(raw=raw):
                self.assertEqual(lc.approval_label(raw), label)
        # the legacy order alias "pending" means approved for an order, awaiting for an approval
        self.assertNotEqual(lc.status_label("pending"), lc.approval_label("pending"))

    def test_change_labels(self):
        self.assertEqual(lc.change_label("reschedule", "accepted"), "Ombooking accepteret")
        self.assertEqual(lc.change_label("cancel", "requested"), "Afbestilling anmodet")
        self.assertEqual(lc.change_label("substitute", "rejected"), "Deltagerskift afvist")
        self.assertEqual(lc.change_status_label("pending"), "Afventer svar")


# -- 3. guard matrix -------------------------------------------------------------
class GuardMatrixTests(unittest.TestCase):
    """actor x date x entry point for report_completion and complete_order."""

    COURSE = "12. november 2026"
    DATES = {
        "before": (2026, 10, 6, 12),
        "on_the_day": (2026, 11, 12, 14),
        "after": (2026, 11, 13, 8),
    }
    # (actor, entry point, finishes the order (True) or only reports attendance (False))
    CASES = (
        ("owner", "service", False),
        ("owner", "route", False),
        ("owner", "ai", False),
        ("hr", "service", True),
        ("hr", "route", True),
        ("hr", "status_api", True),
        ("department_head", "service", True),
        ("department_head", "route", True),
        ("admin", "service", True),
        ("admin", "route", True),
        ("vendor", "service", True),
        ("vendor", "route", True),
    )

    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        for p in (
            mock.patch.object(svc, "_emit_event_safe"),
            mock.patch.object(svc, "_send_email_safe"),
            mock.patch.object(svc, "_manager_recipient_emails", return_value=[]),
            mock.patch("completion_service.completion_moment", return_value={}),
            render_patches(),
        ):
            p.start()
            self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id, company_name) VALUES (7, 'Firma A/S')")
        for uid, name in ((1, "ada"), (2, "hr"), (3, "chef"), (5, "root")):
            d.execute("INSERT INTO users (id, username, email) VALUES (%s, %s, %s)", (uid, name, name + "@f.dk"))
        d.execute("INSERT INTO company_users (company_id, user_id, username, role, department, manager_user_id) VALUES "
                  "(7, 1, 'ada', 'employee', 'Salg', 3), (7, 2, 'hr', 'hr_manager', 'HR', NULL), "
                  "(7, 3, 'chef', 'department_head', 'Salg', NULL)")
        d.execute("INSERT INTO department_budgets (company_id, department, annual_budget, spent, fiscal_year) "
                  "VALUES (7, 'Salg', 100000, 0, %s)", (datetime.datetime.now().year,))
        d.execute("INSERT INTO vendors (id, vendor_name, slug, contact_email, status) VALUES "
                  "(11, 'Kursus ApS', 'kursus-aps', 'v@k.dk', 'active')")
        self.counter = 0

    # fixtures
    def booked_order(self):
        self.counter += 1
        learner = svc.OrderContext(company_id=7, user_id=1, username="ada", company_role="employee", department="Salg")
        hr = svc.OrderContext(company_id=7, user_id=2, username="hr", company_role="hr_manager", department="HR")
        with mock.patch.object(svc, "_vendor_id_for_handle", return_value=11):
            oid = svc.create_order(learner, product_handle="kursus-%d" % self.counter, product_title="Kursus",
                                   price=12500, variant_date=self.COURSE, variant_location="Kontoret",
                                   user_email="ada@f.dk", user_name="Ada")["order_id"]
        self.assertTrue(svc.set_status(hr, oid, "approved")["success"])
        self.assertTrue(svc.book_order(hr, oid, booking={"reference": "TI-1", "start_at": "2026-11-12T09:00",
                                                         "location": "Kontoret"})["success"])
        return oid

    def row(self, oid):
        return self.db.one("SELECT co.status, d.completion_state FROM course_orders co "
                           "LEFT JOIN course_order_details d ON d.order_id = co.order_id WHERE co.order_id=%s", (oid,))

    # entry points
    SERVICE_CTX = {
        "owner": dict(company_id=7, user_id=1, username="ada", company_role="employee", department="Salg"),
        "hr": dict(company_id=7, user_id=2, username="hr", company_role="hr_manager", department="HR"),
        "department_head": dict(company_id=7, user_id=3, username="chef", company_role="department_head",
                                department="Salg"),
        "admin": dict(user_id=5, username="root", is_platform_admin=True),
    }
    ROUTE_SESSION = {
        "hr": dict(user="hr", user_id=2, company_id=7, company_role="hr_manager"),
        "department_head": dict(user="chef", user_id=3, company_id=7, company_role="department_head"),
        "admin": dict(user="root", user_id=5, role="admin"),
    }

    def via_service(self, actor, oid):
        if actor == "vendor":
            return svc.complete_order(svc.OrderContext.for_vendor(11, "Kursus ApS"), oid)
        ctx = svc.OrderContext(**self.SERVICE_CTX[actor])
        if actor == "owner":
            return fulfillment.report_completion(ctx, oid, note="Jeg deltog")
        return svc.complete_order(ctx, oid)

    def via_route(self, actor, oid):
        if actor == "owner":
            client = client_as(self.app, user="ada", user_id=1, company_id=7, company_role="employee")
            resp = client.post("/min-ordre/%s/gennemfoert" % oid, data={"evidence_note": "Jeg deltog"},
                               follow_redirects=True)
        elif actor == "vendor":
            client = client_as(self.app, user_type="vendor", vendor_id=11, vendor_name="Kursus ApS")
            resp = client.post("/vendor/orders/%s/complete" % oid, follow_redirects=True)
        else:
            resp = client_as(self.app, **self.ROUTE_SESSION[actor]).post(
                "/ordre/%s/booking" % oid, data={"action": "verify", "note": "Bekræftet"}, follow_redirects=True)
        return resp.get_data(as_text=True)

    def via_status_api(self, oid):
        client = client_as(self.app, **self.ROUTE_SESSION["hr"])
        return client.post("/hr/order/%s/update" % oid, data={"status": "completed"}).get_json()["message"]

    def via_ai_tool(self, oid):
        import app1.tools as tools
        from flask import session
        with self.app.test_request_context():
            session.update(user="ada", user_id=1, company_id=7, company_role="employee")
            return json.loads(tools._execute_mark_course_complete({"confirm": "ja", "order_id": oid}, "ada"))

    def invoke(self, actor, entry, oid):
        if entry == "service":
            return self.via_service(actor, oid)
        if entry == "route":
            return self.via_route(actor, oid)
        if entry == "status_api":
            return self.via_status_api(oid)
        return self.via_ai_tool(oid)

    def test_matrix(self):
        for actor, entry, finishes in self.CASES:
            for position, clock in self.DATES.items():
                with self.subTest(actor=actor, entry=entry, date=position), at(*clock):
                    oid = self.booked_order()
                    outcome = self.invoke(actor, entry, oid)
                    state = self.row(oid)
                    if position != "after":
                        self.assertEqual(state["status"], "booked")
                        self.assertNotIn(state["completion_state"], ("reported", "verified"))
                        if entry == "service":
                            self.assertFalse(outcome["success"])
                            self.assertEqual(outcome["error"], "not_yet_held")
                        text = outcome if isinstance(outcome, str) else json.dumps(outcome, ensure_ascii=False)
                        self.assertIn("Kurset er ikke afholdt endnu", text)
                        self.assertIn("13. november 2026", text)
                    elif finishes:
                        self.assertEqual(state["status"], "completed")
                    else:
                        self.assertEqual(state["status"], "booked")
                        self.assertEqual(state["completion_state"], "reported")


if __name__ == "__main__":
    unittest.main()
