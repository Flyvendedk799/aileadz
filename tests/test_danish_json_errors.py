"""R-7: session-authenticated JSON shown in toasts is Danish (DECISIONS #8).

The machine API (enterprise_api/) is exempt by decision and not checked here.
"""

import os
import re
import unittest

os.environ.setdefault("SANDBOX", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FILES = ("app1/order_routes.py", "enterprise_company_settings.py", "multitenant_reports.py")
ENGLISH = ("Missing required fields", "Product handle is required", "Product not found", "Service unavailable",
           "Order cancelled successfully", "Order not found", "Failed to cancel order", "System error occurred",
           "Company not found", "Failed to process", "Settings updated successfully", "Failed to update settings",
           "Internal server error", "Theme applied successfully", "Failed to apply theme", "Preview failed",
           "Insufficient permissions", "No file provided", "File size exceeds", "Invalid file type",
           "Invalid MIME type")


class NoEnglishUserMessagesTests(unittest.TestCase):
    def test_json_and_validation_messages_are_danish(self):
        for rel in FILES:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                src = f.read()
            # user-visible channels only; log lines may stay English
            visible = "\n".join(line for line in src.splitlines()
                                if re.search(r"'(error|message)'|\"(error|message)\"|'errors'|return False, ", line))
            for phrase in ENGLISH:
                self.assertNotIn(phrase, visible, (rel, phrase))

    def test_raw_exceptions_are_not_returned_to_the_learner(self):
        with open(os.path.join(ROOT, "app1/order_routes.py"), encoding="utf-8") as f:
            self.assertNotRegex(f.read(), r"'error':\s*str\(e\)")


class StoreUserInfoTests(unittest.TestCase):
    def test_missing_fields_are_named_in_danish(self):
        from tests.sqlite_platform import PlatformDB, make_app
        app = make_app(PlatformDB())
        r = app.test_client().post("/app1/orders/store_user_info", json={"name": "Ane"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "Udfyld venligst: e-mail, telefon.")


if __name__ == "__main__":
    unittest.main()
