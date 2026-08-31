"""Ordering flow: contact details come from the record, and the AI places the order.

Two reported failures are pinned here:

1. The assistant asked a logged-in user for name, email and phone even though the
   platform already held all three — ``_company_employee_contact`` was the only
   source, and it returns nothing outside a company workspace.
2. After that it told the user to go and order on the course page, because
   ``create_course_order`` only reached the tool menu on a handful of exact Danish
   confirmation phrases and was absent on the turn the user actually confirmed.

No live DB or mail: the two contact lookups are patched.
"""
import json
import unittest
from unittest import mock

import app1.tools as tools
from ai_tool_registry import get_employee_tool_selection, get_tool_meta, tool_name


def _patched_contact(company_row=None, account_row=None):
    return mock.patch.multiple(
        tools,
        _company_employee_contact=mock.Mock(return_value=company_row or {}),
        _user_account_contact=mock.Mock(return_value=account_row or {}),
    )


class ResolveUserContactTests(unittest.TestCase):
    def test_company_profile_supplies_all_three_fields(self):
        with _patched_contact({"full_name": "Tobias P", "email": "t@firma.dk",
                               "phone": "12345678", "department": "Salg"}):
            contact = tools.resolve_user_contact("tobias")
        self.assertEqual(contact["name"], "Tobias P")
        self.assertEqual(contact["email"], "t@firma.dk")
        self.assertEqual(contact["phone"], "12345678")
        self.assertEqual(contact["missing_required"], [])
        self.assertEqual(contact["missing_optional"], [])
        self.assertEqual(contact["sources"]["email"], "company_profile")

    def test_account_email_fills_in_without_a_company_record(self):
        with _patched_contact({}, {"username": "tobias", "email": "t@gmail.com"}):
            contact = tools.resolve_user_contact("tobias")
        self.assertEqual(contact["email"], "t@gmail.com")
        self.assertEqual(contact["sources"]["email"], "account")
        # The login handle stands in for the name, but is flagged for confirmation
        # rather than counted as missing — one question instead of three.
        self.assertEqual(contact["name"], "tobias")
        self.assertEqual(contact["confirm_fields"], ["name"])
        self.assertEqual(contact["missing_required"], [])

    def test_a_missing_phone_never_blocks(self):
        with _patched_contact({"full_name": "Tobias P", "email": "t@firma.dk"}):
            contact = tools.resolve_user_contact("tobias")
        self.assertEqual(contact["missing_required"], [])
        self.assertEqual(contact["missing_optional"], ["phone"])

    def test_explicit_arguments_win_over_the_record(self):
        with _patched_contact({"full_name": "Gammelt Navn", "email": "gammel@firma.dk"}):
            contact = tools.resolve_user_contact("tobias", overrides={"email": "ny@firma.dk"})
        self.assertEqual(contact["email"], "ny@firma.dk")
        self.assertEqual(contact["sources"]["email"], "argument")
        self.assertEqual(contact["name"], "Gammelt Navn")

    def test_anonymous_user_still_reports_what_is_missing(self):
        with _patched_contact({}, {}):
            contact = tools.resolve_user_contact(None)
        self.assertEqual(contact["missing_required"], ["name", "email"])


class ReadinessContactTests(unittest.TestCase):
    def _readiness(self, company_row):
        product = {"handle": "distanceledelse", "title": "Distanceledelse",
                   "vendor": "Udbyder A", "variants": []}
        with _patched_contact(company_row), \
                mock.patch.object(tools.catalog, "get_product", return_value=product), \
                mock.patch.object(tools, "_supplier_state_for_vendor", return_value={"is_active": True}), \
                mock.patch.object(tools, "_catalog_compact_fields", return_value=product), \
                mock.patch.object(tools, "mark_order_flow_open"):
            return json.loads(tools._execute_check_course_readiness(
                {"product_handle": "distanceledelse"}, "tobias"))

    def test_a_complete_profile_is_ready_and_asks_for_nothing(self):
        out = self._readiness({"full_name": "Tobias P", "email": "t@firma.dk", "phone": "12345678"})
        self.assertEqual(out["readiness"], "ready")
        self.assertEqual(out["missing_fields"], [])
        self.assertEqual(out["employee"]["email"], "t@firma.dk")
        self.assertIn("spørg IKKE om dem igen", out["message"])

    def test_a_missing_phone_leaves_the_user_ready_to_order(self):
        out = self._readiness({"full_name": "Tobias P", "email": "t@firma.dk"})
        self.assertEqual(out["readiness"], "ready")
        self.assertEqual(out["missing_fields"], [])
        self.assertEqual(out["optional_missing"], ["phone"])


class CreateOrderConfirmGateTests(unittest.TestCase):
    PRODUCT = {"handle": "distanceledelse", "title": "Distanceledelse",
               "vendor": "Udbyder A", "product_type": "Kursus",
               "variants": [{"price": "9995"}]}

    def _create(self, args):
        with _patched_contact({"full_name": "Tobias P", "email": "t@firma.dk", "phone": "12345678"}), \
                mock.patch("app1.rag.load_augmented_products", return_value=[self.PRODUCT]), \
                mock.patch.object(tools, "apply_discount", return_value=(None, None, None)), \
                mock.patch.object(tools, "mark_order_flow_open"), \
                mock.patch.object(tools, "clear_order_flow"), \
                mock.patch("app1.order_handler.store_user_info_for_order"), \
                mock.patch("app1.order_handler.create_order_from_chatbot",
                           return_value={"success": True, "order_id": "abc123",
                                         "message": "Ordre oprettet!"}) as create:
            return json.loads(tools._execute_create_order(args, "tobias")), create

    def test_an_unconfirmed_call_previews_and_books_nothing(self):
        out, create = self._create({"product_handle": "distanceledelse"})
        self.assertTrue(out.get("needs_confirmation"))
        self.assertEqual(out["action"], "create_course_order")
        self.assertEqual(out["details"]["user_email"], "t@firma.dk")
        create.assert_not_called()

    def test_a_confirmed_call_creates_the_order_without_asking_for_contact_details(self):
        out, create = self._create({"product_handle": "distanceledelse", "confirm": True})
        self.assertEqual(out["status"], "order_created")
        self.assertEqual(out["order_id"], "abc123")
        create.assert_called_once()

    def test_it_reports_what_is_missing_for_an_anonymous_user(self):
        with _patched_contact({}, {}):
            out = json.loads(tools._execute_create_order(
                {"product_handle": "distanceledelse", "confirm": True}, None))
        self.assertEqual(out["status"], "needs_info")
        self.assertEqual(out["missing_fields"], ["name", "email"])


class OrderToolReachabilityTests(unittest.TestCase):
    def test_the_order_tool_is_available_when_the_user_wants_to_book(self):
        tools_, _ = get_employee_tool_selection(
            logged_in=True, company_id=1, intent="buying",
            user_query="Jeg vil bestille kurset til mit team", shown_count=1,
        )
        self.assertIn("create_course_order", {tool_name(t) for t in tools_})

    def test_the_schema_only_requires_the_product_handle(self):
        schema = next(t for t in tools.OPENAI_TOOLS
                      if tool_name(t) == "create_course_order")["function"]["parameters"]
        self.assertEqual(schema["required"], ["product_handle"])
        self.assertIn("confirm", schema["properties"])

    def test_the_tool_is_marked_confirm_gated(self):
        meta = get_tool_meta("create_course_order")
        self.assertTrue(meta.confirm_required)
        self.assertTrue(meta.side_effect)
        self.assertFalse(meta.parallel_safe)


class OrderFlowFlagTests(unittest.TestCase):
    """The session flag that keeps ordering tools reachable across turns."""

    def _app(self):
        from flask import Flask
        app = Flask(__name__)
        app.secret_key = "test"
        return app

    def test_it_survives_the_turn_that_set_it(self):
        app = self._app()
        with app.test_request_context("/"):
            self.assertFalse(tools.order_flow_open())
            tools.mark_order_flow_open("distanceledelse", stage="prepared")
            self.assertTrue(tools.order_flow_open())
            self.assertEqual(tools.order_flow_state()["handle"], "distanceledelse")
            tools.clear_order_flow()
            self.assertFalse(tools.order_flow_open())

    def test_an_abandoned_flow_expires(self):
        import datetime as _datetime
        from flask import session as flask_session
        app = self._app()
        with app.test_request_context("/"):
            stale = _datetime.datetime.now() - _datetime.timedelta(
                minutes=tools._ORDER_FLOW_TTL_MINUTES + 1)
            flask_session[tools._ORDER_FLOW_SESSION_KEY] = {
                "handle": "distanceledelse", "updated_at": stale.isoformat()}
            self.assertEqual(tools.order_flow_state(), {})

    def test_it_is_inert_outside_a_request(self):
        tools.mark_order_flow_open("distanceledelse")
        self.assertEqual(tools.order_flow_state(), {})
        self.assertFalse(tools.order_flow_open())


if __name__ == "__main__":
    unittest.main()
