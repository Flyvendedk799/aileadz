"""A failing order write or branding lookup must not hand the raw exception text
(SQL, driver messages, internals) to the user; it is logged instead."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

SECRET = "OperationalError: (1146) Table 'prod.course_orders' doesn't exist"


class OrderHandlerLeakTests(unittest.TestCase):
    def test_store_failure_returns_a_code_not_the_exception(self):
        from flask import Flask
        from app1.order_handler import OrderHandler
        app = Flask(__name__)
        app.secret_key = "t"
        with app.test_request_context("/"):
            with mock.patch("order_service.create_order", side_effect=RuntimeError(SECRET)), \
                    self.assertLogs("app1.order_handler", level="ERROR"):
                res = OrderHandler().create_order({"handle": "h", "title": "T", "price": "100"},
                                                  {"email": "a@b.dk", "name": "A"})
        self.assertFalse(res["success"])
        self.assertNotIn(SECRET, repr(res))
        self.assertTrue(res.get("message"))

    def test_unexpected_failure_returns_a_code_not_the_exception(self):
        from flask import Flask
        from app1.order_handler import OrderHandler
        app = Flask(__name__)
        app.secret_key = "t"
        handler = OrderHandler()
        with app.test_request_context("/"):
            with mock.patch.object(handler, "_parse_price", side_effect=RuntimeError(SECRET)), \
                    self.assertLogs("app1.order_handler", level="ERROR"):
                res = handler.create_order({"handle": "h", "price": "x"}, {"email": "a@b.dk"})
        self.assertFalse(res["success"])
        self.assertNotIn(SECRET, repr(res))


class EnterpriseBrandingLeakTests(unittest.TestCase):
    def test_branding_api_error_is_generic(self):
        from flask import Flask, g
        import enterprise_api
        view = enterprise_api.get_company_branding_api.__wrapped__  # past the API-key guard
        app = Flask(__name__)
        with app.test_request_context("/api/v1/company/branding"):
            g.company_id = 7
            with mock.patch("branding_service.get_branding", side_effect=RuntimeError(SECRET)), \
                    self.assertLogs(level="ERROR"):
                resp, status = view()
        self.assertEqual(status, 500)
        body = resp.get_data(as_text=True)
        self.assertNotIn("1146", body)
        self.assertNotIn("Failed to retrieve", body)


if __name__ == "__main__":
    unittest.main()
