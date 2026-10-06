"""The booking console as state-aware sections: which partial shows for which order state and actor."""

import json
import re

from tests.sqlite_platform import PlatformDB
from tests.test_learner_orders import Base
import order_timing

BOOKING = {"start_at": "2026-11-03T09:00:00+01:00", "location": "Kontoret", "reference": "TI-1"}


class SectionBase(Base):
    DB_CLASS = PlatformDB

    def set_state(self, status, *, booking=True, change=None, review=False):
        self.db.execute("UPDATE course_orders SET status=%s WHERE order_id='ord-1'", (status,))
        if booking:
            self.db.execute(
                "INSERT INTO course_order_details (order_id, company_id, user_id, booking_json) VALUES ('ord-1', 7, 1, %s)",
                (json.dumps(BOOKING),),
            )
        if change:
            self.db.execute(
                "INSERT INTO course_order_changes (order_id, company_id, user_id, kind, requested_by, requested_kind, status, payload_json) "
                "VALUES ('ord-1', 7, 1, %s, 1, 'customer', 'pending', %s)",
                (change, json.dumps({"note": "Kan ikke deltage"})),
            )
        if review:
            self.db.execute(
                "INSERT INTO learning_outcome_reviews (order_id, company_id, user_id, status, baseline_json) "
                "VALUES ('ord-1', 7, 1, 'awaiting_completion', '{}')"
            )

    def page(self, client):
        resp = client.get("/ordre/ord-1/booking")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def shown(self, client):
        return re.findall(r'data-section="([a-z_]+)"', self.page(client))

    def hr(self):
        return self.client_as("hr", 3, "hr_manager")

    def learner(self):
        return self.client_as("ada", 1)


class SectionTableTests(SectionBase):
    def test_pending_order_explains_that_approval_comes_first(self):
        self.set_state("pending_approval", booking=False)
        html = self.page(self.hr())
        self.assertEqual(self.shown(self.hr()), ["pending"])
        self.assertIn("booking kan først ske, når bestillingen er godkendt", html)
        self.assertIn("/hr/approvals", html)
        self.assertNotIn("Ikke rapporteret", html)

    def test_pending_order_has_no_approval_link_for_the_learner(self):
        self.set_state("pending_approval", booking=False)
        self.assertEqual(self.shown(self.learner()), ["pending"])
        self.assertNotIn("/hr/approvals", self.page(self.learner()))

    def test_approved_order_offers_only_the_book_form_to_hr(self):
        self.set_state("approved", booking=False)
        self.assertEqual(self.shown(self.hr()), ["book_form"])

    def test_approved_order_offers_the_learner_nothing_to_do(self):
        self.set_state("approved", booking=False)
        self.assertEqual(self.shown(self.learner()), [])

    def test_booked_order(self):
        self.set_state("booked")
        self.assertEqual(self.shown(self.hr()), ["booking_card", "change_request", "attendance"])
        self.assertEqual(self.shown(self.learner()), ["booking_card", "change_request", "attendance"])

    def test_booked_order_with_a_pending_change_shows_the_change_instead_of_a_new_request(self):
        self.set_state("booked", change="cancel")
        self.assertEqual(self.shown(self.hr()), ["booking_card", "changes_list", "attendance"])
        self.assertIn("Accepter ændring", self.page(self.hr()))
        # The requester (the learner) cannot resolve their own request.
        learner_html = self.page(self.learner())
        self.assertIn("Afbestilling", learner_html)
        self.assertNotIn("Accepter ændring", learner_html)

    def test_completed_order_shows_the_outcome_review_to_hr_only(self):
        self.set_state("completed", review=True)
        self.assertEqual(self.shown(self.hr()), ["booking_card", "attendance", "outcome_review"])
        self.assertEqual(self.shown(self.learner()), ["booking_card", "attendance"])

    def test_cancelled_order_shows_no_booking_or_outcome_sections(self):
        self.set_state("cancelled", review=True)
        html = self.page(self.hr())
        self.assertEqual(self.shown(self.hr()), [])
        self.assertNotIn("Bekræftet booking", html)
        self.assertNotIn("Vurder kompetenceudbyttet", html)
        self.assertNotIn("Bekræft deltagelsen først", html)

    def test_rejected_order_shows_nothing(self):
        self.set_state("rejected", booking=False)
        self.assertEqual(self.shown(self.hr()), [])


class ChangeFormTests(SectionBase):
    def test_form_offers_only_the_kinds_that_have_what_they_need(self):
        self.set_state("booked")
        html = self.page(self.learner())
        self.assertIn('<option value="cancel">', html)
        self.assertNotIn('value="substitute"', html)
        self.assertNotIn('name="user_id"', html)

    def test_hr_can_pick_a_substitute_participant(self):
        self.set_state("booked")
        html = self.page(self.hr())
        self.assertIn('value="substitute"', html)
        self.assertIn('data-kind-field="substitute"', html)

    def test_server_ignores_fields_that_do_not_belong_to_the_chosen_kind(self):
        self.set_state("booked")
        resp = self.learner().post(
            "/ordre/ord-1/booking",
            data={"action": "change", "kind": "cancel", "note": "Syg", "session_id": "s9", "user_id": "2", "extra": "x"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("#aendring"))
        change = self.db.one("SELECT * FROM course_order_changes")
        self.assertEqual(json.loads(change["payload_json"]), {"note": "Syg"})

    def test_reschedule_without_a_chosen_session_is_refused(self):
        import order_fulfillment

        self.set_state("booked")
        with self.app.app_context():
            result = order_fulfillment.request_change(
                __import__("order_service").OrderContext(
                    user_id=1, username="ada", company_id=7, company_role="employee", source="test"
                ),
                "ord-1",
                "reschedule",
                {"note": "x"},
            )
        self.assertFalse(result["success"])
        self.assertIn("Vælg det nye hold", result["message"])


class BookFormTests(SectionBase):
    def test_book_form_is_prefilled_from_the_ordered_session(self):
        self.set_state("approved", booking=False)
        html = self.page(self.hr())
        self.assertIn('value="2026-11-03T00:00"', html)
        self.assertIn('value="København"', html)

    def _book(self, **extra):
        data = {"action": "book", "start_at": "2026-11-10T09:00", "location": "Odense", "reference": "R-1"}
        data.update(extra)
        return self.hr().post("/ordre/ord-1/booking", data=data)

    def test_a_different_date_without_the_confirmation_is_refused_in_danish(self):
        self.set_state("approved", booking=False)
        with self.client_as("hr", 3, "hr_manager") as client:
            resp = client.post(
                "/ordre/ord-1/booking",
                data={"action": "book", "start_at": "2026-11-10T09:00", "location": "Odense", "reference": "R-1"},
            )
            self.assertEqual(resp.status_code, 302)
            with client.session_transaction() as s:
                flashed = [m for _, m in s.get("_flashes", [])]
        self.assertTrue(any("Datoen afviger fra den bestilte session" in m for m in flashed), flashed)
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "approved")

    def test_a_confirmed_different_date_books_and_is_recorded_in_history(self):
        self.set_state("approved", booking=False)
        self._book(confirm_date_change="1")
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "booked")
        notes = " ".join(str(r.get("note") or "") for r in self.db.query("SELECT note FROM order_status_history"))
        self.assertIn("Datoen afviger fra den bestilte session", notes)

    def test_the_ordered_date_books_without_any_confirmation(self):
        self.set_state("approved", booking=False)
        self._book(start_at="2026-11-03T10:00")
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "booked")

    def test_differs_from_ordered_session_helper(self):
        row = {"variant_date": "3.-4. december 2026"}
        self.assertIsNone(order_timing.differs_from_ordered_session(row, "2026-12-04T09:00"))
        self.assertEqual(order_timing.differs_from_ordered_session(row, "2026-12-05"), "3.-4. december 2026")
        self.assertIsNone(order_timing.differs_from_ordered_session({"variant_date": ""}, "2026-12-05"))


class OutcomeReviewFormTests(SectionBase):
    def test_levels_have_no_default_and_the_first_skill_is_required(self):
        self.set_state("completed", review=True)
        html = self.page(self.hr())
        self.assertIn("Vælg niveau", html)
        self.assertNotRegex(html, r'<option value="1" selected')
        self.assertIn('name="level" required', html)


class HrBooksByDefaultTests(SectionBase):
    """HR books without inventing a reference; the vendor may add it later."""

    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO users (id, username, email) VALUES (4, 'hr2', 'hr2@f.dk')")
        self.db.execute("INSERT INTO company_users (company_id, user_id, username, role, department, manager_user_id) "
                        "VALUES (7, 4, 'hr2', 'hr_manager', 'HR', NULL)")
        self.set_state("approved", booking=False)

    def book_as_hr(self, **extra):
        data = {"action": "book", "start_at": "2026-11-03T09:00", "location": "Kontoret", "reference": ""}
        data.update(extra)
        return self.hr().post("/ordre/ord-1/booking", data=data)

    def booking(self):
        return json.loads(self.db.one("SELECT booking_json FROM course_order_details")["booking_json"])

    def test_hr_books_with_an_empty_reference_and_nothing_is_invented(self):
        html = self.page(self.hr())
        self.assertIn("Leverandørens bookingreference (hvis I har den)", html)
        self.assertNotRegex(html, r'name="reference"[^>]*required')
        self.book_as_hr()
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "booked")
        booking = self.booking()
        self.assertIsNone(booking["reference"])
        self.assertEqual(booking["confirmed_by_kind"], "hr")
        page = self.page(self.hr())
        self.assertIn("Bekræftet af HR · ingen reference fra udbyderen endnu", page)
        self.assertNotIn("Bekræftet i portalen", page)
        note = self.db.one("SELECT note FROM order_status_history WHERE to_value='booked'")["note"]
        self.assertEqual(note, "Booket af HR")

    def test_a_reference_added_later_keeps_the_order_booked_and_is_audited(self):
        self.book_as_hr()
        resp = self.hr().post("/ordre/ord-1/booking", data={"action": "reference", "reference": "TI-77"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "booked")
        self.assertEqual(self.booking()["reference"], "TI-77")
        self.assertEqual(self.booking()["confirmed_by_kind"], "hr")
        history = self.db.one("SELECT * FROM order_status_history WHERE kind='change' AND from_value='reference'")
        self.assertEqual(history["note"], "TI-77")
        self.assertIn("Reference: TI-77 · bekræftet af HR", self.page(self.hr()))

    def test_the_learner_cannot_add_a_reference(self):
        self.book_as_hr()
        resp = self.learner().post("/ordre/ord-1/booking", data={"action": "reference", "reference": "x"})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self.booking()["reference"])
        self.assertNotIn("data-reference-form", self.page(self.learner()))

    def hr_notifications(self):
        return {r["user_id"] for r in self.db.query("SELECT user_id FROM notifications WHERE title='Plads bekræftet'")}

    def test_hr_is_not_notified_about_their_own_booking_but_colleagues_are(self):
        self.book_as_hr()
        self.assertEqual(self.hr_notifications(), {"hr2"})
        titles = {r["title"] for r in self.db.query("SELECT title FROM notifications WHERE user_id='ada'")}
        self.assertIn("Din plads er booket", titles)

    def test_a_vendor_booking_notifies_the_learner_and_all_of_hr(self):
        from tests.sqlite_platform import client_as

        self.db.execute("INSERT INTO vendors (id, vendor_name, slug, contact_email, status) VALUES (11, 'Kursus ApS', 'kursus-aps', 'v@k.dk', 'active')")
        self.db.execute("UPDATE course_orders SET vendor_id=11")
        vendor = client_as(self.app, user_type="vendor", vendor_id=11, vendor_name="Kursus ApS")
        resp = vendor.post("/vendor/orders/ord-1/booking", data={"action": "book", "start_at": "2026-11-03T09:00", "location": "Kontoret"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "booked")
        self.assertEqual(self.booking()["confirmed_by_kind"], "vendor")
        self.assertEqual(self.hr_notifications(), {"hr", "hr2"})
        titles = {r["title"] for r in self.db.query("SELECT title FROM notifications WHERE user_id='ada'")}
        self.assertIn("Din plads er booket", titles)
        self.assertEqual(self.db.one("SELECT note FROM order_status_history WHERE to_value='booked'")["note"], "Booket af udbyderen")
        self.assertIn("Bekræftet af udbyderen", self.page(self.hr()))

    def test_approved_orders_are_marked_ready_for_booking_for_hr(self):
        self.assertIn("Klar til booking", self.page(self.hr()))
        resp = self.hr().get("/hr/order/ord-1/details")
        self.assertIn("Klar til booking", resp.get_data(as_text=True))


class LearnerPageTests(SectionBase):
    """The learner never needs /ordre/<id>/booking: everything is on /min-ordre/<id>."""

    def order_page(self, client=None):
        return (client or self.learner()).get("/min-ordre/ord-1").get_data(as_text=True)

    def test_page_links_nowhere_near_the_old_console(self):
        for status in ("pending_approval", "approved", "booked", "completed", "cancelled"):
            self.set_state(status, booking=status in ("booked", "completed"))
            html = self.order_page()
            self.assertNotIn("/ordre/ord-1/booking", html, status)
            self.assertNotIn("Booking, ændringer og kursusudbytte", html, status)
            self.db.execute("DELETE FROM course_order_details")

    def test_booked_page_shows_booking_change_and_attendance_in_one_place(self):
        self.set_state("booked")
        html = self.order_page()
        self.assertEqual(re.findall(r'data-section="([a-z_]+)"', html), ["booking_card", "change_request", "attendance"])
        self.assertIn("Kontoret", html)
        self.assertIn('action="/min-ordre/ord-1/handling"', html)
        self.assertNotIn("/gennemfoert", html)

    def test_pending_page_explains_the_wait(self):
        self.set_state("pending_approval", booking=False)
        self.assertIn("booking kan først ske, når bestillingen er godkendt", self.order_page())

    def test_change_request_posts_back_to_the_page_at_the_change_section(self):
        self.set_state("booked")
        resp = self.learner().post("/min-ordre/ord-1/handling", data={"action": "change", "kind": "cancel", "note": "Syg"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/min-ordre/ord-1#aendring"), resp.headers["Location"])
        self.assertEqual(self.db.one("SELECT status FROM course_order_changes")["status"], "pending")
        html = self.order_page()
        self.assertEqual(re.findall(r'data-section="([a-z_]+)"', html), ["booking_card", "changes_list", "attendance"])
        self.assertIn("Din afbestilling afventer svar fra udbyderen", html)

    def test_attendance_report_posts_back_and_shows_the_waiting_state(self):
        self.set_state("booked")
        resp = self.learner().post(
            "/min-ordre/ord-1/handling", data={"action": "report", "evidence_note": "Jeg deltog"}
        )
        self.assertTrue(resp.headers["Location"].endswith("/min-ordre/ord-1#deltagelse"))
        self.assertEqual(self.db.one("SELECT completion_state FROM course_order_details")["completion_state"], "reported")
        self.assertIn("Deltagelse indsendt – afventer bekræftelse", self.order_page())

    def test_only_the_owner_may_use_the_endpoint(self):
        self.set_state("booked")
        self.assertEqual(self.client_as("bo", 2).post("/min-ordre/ord-1/handling", data={"action": "report", "evidence_note": "x"}).status_code, 404)
        self.assertEqual(self.learner().post("/min-ordre/ord-1/handling", data={"action": "verify", "note": "x"}).status_code, 403)
        self.assertEqual(self.learner().post("/min-ordre/ord-1/handling", data={"action": "review"}).status_code, 403)

    def test_completed_page_shows_the_confirmed_outcome_to_the_learner(self):
        self.set_state("completed", review=True)
        self.assertNotIn("Kursusudbytte", self.order_page())
        self.db.execute("UPDATE learning_outcome_reviews SET status='completed', review_note='Bruger metoden dagligt'")
        html = self.order_page()
        self.assertIn("Bruger metoden dagligt", html)
        self.assertEqual(re.findall(r'data-section="([a-z_]+)"', html), ["booking_card", "attendance", "outcome_summary"])
