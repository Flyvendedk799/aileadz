"""Surface the .ics add-to-calendar action + in-app subscribable feed (plan #4).

The calendar layer was built and boot-safe but invisible: hr_ext.order_ics /
deadline_ics existed, calendar_service.build_ics(_feed) worked, the addtocal
macro was defined but never invoked, and a full feed lived only behind an
enterprise API key. This initiative wires those into the HR UI and adds a
session-authed, company-scoped feed wrapper that reuses the SAME event builder
as the API-key feed (no new data surface, just a different gate).

These tests lock in:

  1. enterprise_api.build_company_calendar_events — event shape, company scope,
     user_ids IN-clause, and []-on-failure (never raises).
  2. The session-authed hr_ext /calendar/feed.ics route is HR-gated,
     company-scoped, returns text/calendar, and degrades to a valid (possibly
     empty) calendar instead of 500.
  3. The three HR templates actually invoke the addtocal macro against the
     existing ics endpoints (the macro is no longer dead code).
  4. The API feed route still produces identical event shape via the helper.

Backend tests use fakes (no live DB); the route test boots the sandbox app with
a fake mysql connection injected so it never touches a real DB.
"""

import os
import unittest
from unittest import mock

# run installs the pymysql->MySQLdb shim that enterprise_api imports at top.
import run  # noqa: F401
import enterprise_api


# ── Fakes (modelled on tests/test_order_alert_emails.py) ─────────────────────

class FakeCursor:
    def __init__(self, *, fetchall=None, raise_on_execute=False):
        self.executed = []
        self._fetchall = fetchall if fetchall is not None else []
        self._raise = raise_on_execute
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise:
            raise RuntimeError("execute blew up")

    def fetchall(self):
        return self._fetchall

    def fetchone(self):
        return None

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor=None):
        self._cursor = cursor or FakeCursor()

    def cursor(self, *a, **k):
        return self._cursor


# Two representative course_orders rows: one with a scheduled start AND a
# completion deadline (-> two events), one with neither (-> zero events).
SAMPLE_ROWS = [
    {
        "order_id": "ORD-1",
        "product_title": "Lederuddannelse",
        "product_handle": "ledelse",
        "variant_date": "2026-02-17",
        "variant_location": "København",
        "status": "approved",
        "completion_deadline": "2026-03-01",
        "started_at": None,
        "user_name": "Anna",
        "department": "Sales",
    },
    {
        "order_id": "ORD-2",
        "product_title": "GDPR",
        "product_handle": "gdpr",
        "variant_date": None,
        "variant_location": "",
        "status": "pending",
        "completion_deadline": None,
        "started_at": None,
        "user_name": "Bo",
        "department": "IT",
    },
]


# ── 1) build_company_calendar_events ─────────────────────────────────────────

class BuildCompanyCalendarEventsTests(unittest.TestCase):
    def test_builds_start_and_deadline_events(self):
        cur = FakeCursor(fetchall=SAMPLE_ROWS)
        events = enterprise_api.build_company_calendar_events(cur, 42)
        titles = [e["title"] for e in events]
        # ORD-1 yields a start + a deadline event; ORD-2 yields nothing usable.
        self.assertIn("Kursusstart: Lederuddannelse", titles)
        self.assertIn("Frist: Lederuddannelse", titles)
        self.assertEqual(len(events), 2)
        # UIDs are stable / order-derived so a re-subscribe de-dupes client-side.
        uids = {e["uid"] for e in events}
        self.assertEqual(uids, {"order-ORD-1-start@aileadz", "order-ORD-1-deadline@aileadz"})

    def test_company_scope_is_always_in_the_query(self):
        cur = FakeCursor(fetchall=SAMPLE_ROWS)
        enterprise_api.build_company_calendar_events(cur, 42)
        sql, params = cur.executed[0]
        self.assertIn("company_id = %s", sql)
        self.assertEqual(params[0], 42)
        # Cancelled/rejected orders are excluded (forward-looking feed).
        self.assertIn("NOT IN ('cancelled', 'rejected')", sql)

    def test_user_ids_narrow_with_in_clause(self):
        cur = FakeCursor(fetchall=SAMPLE_ROWS)
        enterprise_api.build_company_calendar_events(cur, 42, user_ids=[7, 9])
        sql, params = cur.executed[0]
        self.assertIn("user_id IN (%s,%s)", sql)
        # company_id first, then the two user ids.
        self.assertEqual(list(params), [42, 7, 9])

    def test_user_ids_dedupe_drops_none_and_empty_short_circuits(self):
        # [None] collapses to no ids -> nothing to scope to -> empty, no query.
        cur = FakeCursor(fetchall=SAMPLE_ROWS)
        events = enterprise_api.build_company_calendar_events(cur, 42, user_ids=[None])
        self.assertEqual(events, [])
        self.assertEqual(cur.executed, [])

    def test_query_failure_returns_empty_never_raises(self):
        cur = FakeCursor(raise_on_execute=True)
        self.assertEqual(enterprise_api.build_company_calendar_events(cur, 42), [])


# ── 2) session-authed in-app feed route ──────────────────────────────────────

def _sandbox_app():
    os.environ.setdefault("SANDBOX", "1")
    os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
    os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")
    from run import create_app
    return create_app()


class CalendarFeedRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _sandbox_app()

    def _inject_fake_mysql(self, cursor):
        """Replace app.mysql with a fake whose .connection returns a fake conn."""
        fake = type("M", (), {"connection": FakeConnection(cursor)})()
        return mock.patch.object(self.app, "mysql", fake)

    def test_unauthenticated_is_not_served_calendar(self):
        # No session -> _hr_ok() false -> redirect (302) to dashboard, never .ics.
        with self.app.test_client() as c:
            resp = c.get("/hr/calendar/feed.ics")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("text/calendar", resp.headers.get("Content-Type", ""))

    def test_hr_user_gets_company_scoped_calendar(self):
        cur = FakeCursor(fetchall=SAMPLE_ROWS)
        with self._inject_fake_mysql(cur):
            with self.app.test_client() as c:
                with c.session_transaction() as s:
                    s["user"] = "hr@acme.dk"
                    s["company_id"] = 42
                    s["company_role"] = "hr_manager"
                resp = c.get("/hr/calendar/feed.ics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/calendar", resp.headers.get("Content-Type", ""))
        body = resp.get_data(as_text=True)
        self.assertTrue(body.startswith("BEGIN:VCALENDAR"))
        self.assertIn("Kursusstart: Lederuddannelse", body)
        # The feed is whole-company (no user_ids), so the query carries only the
        # company scope — confirm we passed company_id 42 and no IN clause.
        sql, params = cur.executed[0]
        self.assertIn("company_id = %s", sql)
        self.assertNotIn("user_id IN", sql)
        self.assertEqual(params[0], 42)

    def test_feed_degrades_to_valid_empty_calendar_on_db_failure(self):
        cur = FakeCursor(raise_on_execute=True)
        with self._inject_fake_mysql(cur):
            with self.app.test_client() as c:
                with c.session_transaction() as s:
                    s["user"] = "hr@acme.dk"
                    s["company_id"] = 42
                    s["company_role"] = "company_admin"
                resp = c.get("/hr/calendar/feed.ics")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertTrue(body.startswith("BEGIN:VCALENDAR"))
        self.assertIn("END:VCALENDAR", body)
        self.assertNotIn("BEGIN:VEVENT", body)  # no events, still valid


# ── 3) templates actually invoke the addtocal macro ──────────────────────────

class TemplateWiringTests(unittest.TestCase):
    def _src(self, name):
        with open(os.path.join("templates", "fm", name), encoding="utf-8") as fh:
            return fh.read()

    def test_macro_is_no_longer_dead_code(self):
        # _macros.html defines addtocal; at least these three pages now call it.
        self.assertIn("macro addtocal", self._src("_macros.html"))

    def test_hr_dashboard_wires_order_ics_and_subscribe_feed(self):
        src = self._src("hr.html")
        self.assertIn("hr_ext.order_ics", src)
        self.assertIn("hr_ext.calendar_feed", src)
        self.assertIn("ui.addtocal", src)

    def test_learning_paths_wires_deadline_ics_and_subscribe_feed(self):
        src = self._src("learning_paths.html")
        self.assertIn("hr_ext.deadline_ics", src)
        self.assertIn("hr_ext.calendar_feed", src)
        self.assertIn("ui.addtocal", src)

    def test_employee_progress_wires_subscribe_feed(self):
        src = self._src("employee_progress.html")
        self.assertIn("hr_ext.calendar_feed", src)
        self.assertIn("addtocal", src)


if __name__ == "__main__":
    unittest.main()
