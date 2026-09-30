"""N-7.2 SCIM groups <-> departments and N-7.3 webhooks (per-subscriber delivery, log, events)."""

import json
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import event_bus  # noqa: E402
from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.db.raw.executescript(
            "ALTER TABLE company_users ADD COLUMN employment_type TEXT;"
            "ALTER TABLE company_users ADD COLUMN updated_at TEXT;")
        self.app = make_app(self.db)
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.addCleanup(self._ctx.pop)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id, company_name, company_slug) VALUES (7,'A','a'), (8,'B','b')")
        d.execute("INSERT INTO users (id, username, email) VALUES (1,'hr','hr@a.dk'),(2,'ada','ada@a.dk'),(3,'bo','bo@a.dk'),(4,'eve','eve@b.dk')")
        d.execute("INSERT INTO company_users (id, company_id, user_id, username, email, role, department, status) VALUES "
                  "(11,7,1,'hr','hr@a.dk','hr_manager','HR','active'),(12,7,2,'ada','ada@a.dk','employee','Salg','active'),"
                  "(13,7,3,'bo','bo@a.dk','employee','Salg','active'),(21,8,4,'eve','eve@b.dk','employee','Salg','active')")
        d.execute("INSERT INTO company_departments (id, company_id, department_name) VALUES (1,7,'Salg'),(2,7,'HR'),(3,8,'Salg')")


def raw(fn):
    return getattr(fn, "__wrapped__", fn)


class ScimGroupTests(Base):
    def call(self, fn, company=7, method="GET", json_body=None, query=None, **kw):
        import scim_groups
        with self.app.test_request_context("/scim/v2/Groups", method=method, json=json_body, query_string=query):
            from flask import g
            g.company_id = company
            return raw(getattr(scim_groups, fn))(**kw)

    def body(self, resp):
        return json.loads(resp.get_data(as_text=True))

    def test_list_returns_departments_with_members_of_own_tenant_only(self):
        res = self.body(self.call("list_groups"))
        names = {r["displayName"]: r for r in res["Resources"]}
        self.assertEqual(set(names), {"Salg", "HR"})
        self.assertEqual({m["value"] for m in names["Salg"]["members"]}, {"12", "13"})

    def test_get_other_tenants_group_is_404(self):
        self.assertEqual(self.call("get_group", group_id="3").status_code, 404)

    def test_create_makes_a_department_and_assigns_members(self):
        r = self.call("create_group", method="POST", json_body={"displayName": "Drift", "members": [{"value": "12"}]})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.db.one("SELECT department FROM company_users WHERE id=12")["department"], "Drift")
        self.assertIsNotNone(self.db.one("SELECT id FROM company_departments WHERE company_id=7 AND department_name='Drift'"))

    def test_create_duplicate_is_409_and_cannot_steal_other_tenants_users(self):
        self.assertEqual(self.call("create_group", method="POST", json_body={"displayName": "salg"}).status_code, 409)
        self.call("create_group", method="POST", json_body={"displayName": "Ny", "members": [{"value": "21"}]})
        self.assertEqual(self.db.one("SELECT department FROM company_users WHERE id=21")["department"], "Salg")

    def test_patch_add_remove_members_and_rename(self):
        self.call("update_group", method="PATCH", group_id="2",
                  json_body={"Operations": [{"op": "add", "path": "members", "value": [{"value": "13"}]}]})
        self.assertEqual(self.db.one("SELECT department FROM company_users WHERE id=13")["department"], "HR")
        self.call("update_group", method="PATCH", group_id="2",
                  json_body={"Operations": [{"op": "remove", "path": 'members[value eq "13"]'}]})
        self.assertIsNone(self.db.one("SELECT department FROM company_users WHERE id=13")["department"])
        self.call("update_group", method="PATCH", group_id="1",
                  json_body={"Operations": [{"op": "replace", "path": "displayName", "value": "Salg og marketing"}]})
        self.assertEqual(self.db.one("SELECT department FROM company_users WHERE id=12")["department"], "Salg og marketing")

    def test_delete_clears_members_and_department(self):
        r = self.call("delete_group", method="DELETE", group_id="1")
        self.assertEqual(r.status_code, 204)
        self.assertIsNone(self.db.one("SELECT department FROM company_users WHERE id=12")["department"])
        self.assertIsNone(self.db.one("SELECT id FROM company_departments WHERE id=1"))
        self.assertIsNotNone(self.db.one("SELECT id FROM company_departments WHERE id=3"))   # other tenant untouched

    def test_routes_are_registered_behind_the_api_key_decorator(self):
        rules = {r.rule: r.endpoint for r in self.app.url_map.iter_rules() if "Groups" in r.rule}
        self.assertIn("/scim/v2/Groups", rules)
        self.assertIn("/scim/v2/Groups/<group_id>", rules)
        r = self.app.test_client().get("/scim/v2/Groups")          # no key
        self.assertIn(r.status_code, (401, 403, 503))


class WebhookDeliveryTests(Base):
    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO company_webhooks (id, company_id, name, url, secret, events) VALUES "
                        "(1,7,'A','https://a.example/h','s','[\"order.approved\"]'),"
                        "(2,7,'B','https://b.example/h','s','[\"order.approved\",\"budget.overrun\"]'),"
                        "(3,8,'Andens','https://c.example/h','s','[\"order.approved\"]')")
        for p in (mock.patch.object(event_bus, "_safe_webhook_url", return_value=True),):
            p.start()
            self.addCleanup(p.stop)
        self.calls = []

    def urlopen(self, fail_hosts=()):
        def _open(req, timeout=None):
            self.calls.append(req.full_url)
            if any(h in req.full_url for h in fail_hosts):
                raise OSError("connection refused")
            return mock.Mock(status=200)
        return mock.patch("urllib.request.urlopen", side_effect=_open)

    def test_one_failing_subscriber_is_retried_alone(self):
        event_bus.emit_event(7, "order.approved", {"order_id": "o1"})
        with self.urlopen(fail_hosts=("b.example",)):
            event_bus.drain_outbox()
        self.assertEqual(sorted(self.calls), ["https://a.example/h", "https://b.example/h"])
        row = self.db.one("SELECT status, attempts FROM event_outbox")
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        states = {r["webhook_id"]: r["status"] for r in self.db.query("SELECT webhook_id, status FROM webhook_deliveries")}
        self.assertEqual(states, {1: "delivered", 2: "failed"})

        self.calls.clear()
        with self.urlopen():
            event_bus.drain_outbox()
        self.assertEqual(self.calls, ["https://b.example/h"])        # A is NOT sent the event again
        self.assertEqual(self.db.one("SELECT status FROM event_outbox")["status"], "delivered")
        self.assertEqual(self.db.one("SELECT attempts FROM webhook_deliveries WHERE webhook_id=2")["attempts"], 2)

    def test_other_tenants_webhooks_never_receive_the_event(self):
        event_bus.emit_event(7, "order.approved", {})
        with self.urlopen():
            event_bus.drain_outbox()
        self.assertNotIn("https://c.example/h", self.calls)

    def test_budget_overrun_is_subscribable_in_the_ui_choices(self):
        from enterprise_company_settings import WEBHOOK_EVENT_CHOICES
        names = {n for n, _ in WEBHOOK_EVENT_CHOICES}
        for ev in ("budget.overrun", "order.approved", "order.booked", "employee.added", "employee.updated"):
            self.assertIn(ev, names)

    def test_resend_requeues_only_that_delivery_company_scoped(self):
        event_bus.emit_event(7, "order.approved", {})
        with self.urlopen(fail_hosts=("b.example",)):
            event_bus.drain_outbox(); event_bus.drain_outbox(); event_bus.drain_outbox()
            event_bus.drain_outbox(); event_bus.drain_outbox()
        self.assertEqual(self.db.one("SELECT status FROM event_outbox")["status"], "failed")
        did = self.db.one("SELECT id FROM webhook_deliveries WHERE webhook_id=2")["id"]
        self.assertFalse(event_bus.resend_delivery(self.db.connection, 8, did))        # wrong tenant
        self.assertTrue(event_bus.resend_delivery(self.db.connection, 7, did))
        self.assertEqual(self.db.one("SELECT status FROM event_outbox")["status"], "pending")
        self.calls.clear()
        with self.urlopen():
            event_bus.drain_outbox()
        self.assertEqual(self.calls, ["https://b.example/h"])

    def test_webhooks_page_shows_delivery_log_with_gensend_and_hides_others(self):
        event_bus.emit_event(7, "order.approved", {})
        with self.urlopen(fail_hosts=("b.example",)):
            event_bus.drain_outbox()
        with mock.patch("enterprise_company_settings.get_company_context", return_value={"id": 7, "company_name": "A"}):
            c = client_as(self.app, user="hr", user_id=1, company_id=7, company_role="hr_manager")
            html = c.get("/virksomhed/indstillinger/webhooks").get_data(as_text=True)
        self.assertIn("Leveringslog", html)
        self.assertIn("Gensend", html)
        self.assertNotIn("Andens", html)

    def test_resend_route_requires_hr_and_scopes_to_company(self):
        did = 1
        self.db.execute("INSERT INTO webhook_deliveries (id, outbox_id, webhook_id, company_id, event_type, status) "
                        "VALUES (1, 1, 2, 7, 'order.approved', 'failed')")
        self.db.execute("INSERT INTO event_outbox (id, company_id, event_type, payload, status) VALUES (1,7,'order.approved','{}','failed')")
        emp = client_as(self.app, user="ada", user_id=2, company_id=7, company_role="employee")
        emp.post("/enterprise/webhooks/deliveries/1/resend")
        self.assertEqual(self.db.one("SELECT status FROM webhook_deliveries WHERE id=1")["status"], "failed")
        other = client_as(self.app, user="eve", user_id=4, company_id=8, company_role="hr_manager")
        with mock.patch("enterprise_company_settings.get_company_context", return_value={"id": 8}):
            other.post("/enterprise/webhooks/deliveries/1/resend")
        self.assertEqual(self.db.one("SELECT status FROM webhook_deliveries WHERE id=1")["status"], "failed")


class EmployeeEventTests(Base):
    def test_hr_edit_employee_emits_employee_updated(self):
        events = []
        c = client_as(self.app, user="hr", user_id=1, company_id=7, company_role="hr_manager", company_name="A")
        with mock.patch("event_bus.emit_event", side_effect=lambda cid, t, p: events.append((cid, t, p))), \
                mock.patch("companies.get_company_context" if False else "email_service.send_branded_email", return_value=True):
            c.post("/companies/employees/2/edit", data={"role": "employee", "department": "HR", "job_title": "Konsulent",
                                                        "status": "active"})
        types = [t for _c, t, _p in events]
        self.assertIn("employee.updated", types)
        self.assertEqual(events[0][0], 7)


if __name__ == "__main__":
    unittest.main()
