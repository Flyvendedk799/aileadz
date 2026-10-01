"""N-0: error pages, mail configuration, honest email probe, worker status."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

import run  # noqa: E402
import email_service  # noqa: E402
import feature_status  # noqa: E402
import scheduler  # noqa: E402


def _app():
    app = run.create_app()
    app.config["TESTING"] = True
    return app


class NotFoundPageTests(unittest.TestCase):
    def setUp(self):
        self.client = _app().test_client()

    def test_browser_gets_danish_404_page_not_redirect(self):
        resp = self.client.get("/den-findes-ikke", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("Location", resp.headers)
        body = resp.get_data(as_text=True)
        self.assertIn("Vi kunne ikke finde den side", body)

    def test_api_callers_get_json_404(self):
        resp = self.client.get("/api/ikke-en-rute")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["status"], 404)

    def test_json_accept_header_gets_json(self):
        resp = self.client.get("/nope", headers={"Accept": "application/json"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.get_json())


class MailConfigTests(unittest.TestCase):
    ENV = {
        "MAIL_SERVER": "smtp.example.dk",
        "MAIL_PORT": "465",
        "MAIL_USE_SSL": "1",
        "MAIL_USERNAME": "noreply@example.dk",
        "MAIL_PASSWORD": "hemmelig",
    }

    def test_env_is_copied_into_app_config(self):
        app = _app()
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            status = email_service.load_mail_config(app)
        self.assertEqual(app.config["MAIL_SERVER"], "smtp.example.dk")
        self.assertEqual(app.config["MAIL_PORT"], 465)
        self.assertTrue(app.config["MAIL_USE_SSL"])
        self.assertFalse(app.config["MAIL_USE_TLS"])
        # sender falls back to the username
        self.assertEqual(app.config["MAIL_DEFAULT_SENDER"], "noreply@example.dk")
        self.assertTrue(status["configured"])
        self.assertNotIn("hemmelig", repr(status))

    def test_status_reports_what_is_missing(self):
        app = _app()
        env = {k: "" for k in ("MAIL_SERVER", "SMTP_HOST", "SMTP_SERVER", "MAIL_USERNAME",
                               "SMTP_USER", "MAIL_DEFAULT_SENDER")}
        with mock.patch.dict(os.environ, env, clear=False):
            status = email_service.load_mail_config(app)
        self.assertFalse(status["configured"])
        self.assertIn("MAIL_SERVER", status["missing"])
        self.assertIn("MAIL_DEFAULT_SENDER", status["missing"])

    def test_probe_is_not_green_without_config(self):
        env = {k: "" for k in ("MAIL_SERVER", "SMTP_HOST", "SMTP_SERVER", "MAIL_USERNAME",
                               "MAIL_DEFAULT_SENDER")}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertFalse(feature_status._probe_email()["available"])

    def test_probe_green_with_config(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            self.assertTrue(feature_status._probe_email()["available"])

    def test_test_email_refuses_honestly_when_unconfigured(self):
        app = _app()
        env = {k: "" for k in ("MAIL_SERVER", "SMTP_HOST", "SMTP_SERVER", "MAIL_USERNAME",
                               "MAIL_DEFAULT_SENDER")}
        with mock.patch.dict(os.environ, env, clear=False):
            email_service.load_mail_config(app)
            with app.app_context():
                res = email_service.send_test_email("a@b.dk")
        self.assertFalse(res["ok"])
        self.assertIn("MAIL_SERVER", res["error"])


class TestEmailRouteAuth(unittest.TestCase):
    def test_non_admin_cannot_send_test_email(self):
        client = _app().test_client()
        with client.session_transaction() as s:
            s["user"] = "emp"
            s["role"] = "employee"
        resp = client.post("/admin/system-health/test-email", data={"to_email": "a@b.dk"})
        self.assertIn(resp.status_code, (302, 401, 403))
        self.assertNotIn("system-health", resp.headers.get("Location", ""))


class WorkerStatusTests(unittest.TestCase):
    def test_in_request_runner_toggle(self):
        with mock.patch.dict(os.environ, {"SCHEDULER_OPPORTUNISTIC": "0"}):
            self.assertFalse(scheduler.in_request_runner_enabled())
        with mock.patch.dict(os.environ, {"SCHEDULER_OPPORTUNISTIC": "1"}):
            self.assertTrue(scheduler.in_request_runner_enabled())

    def test_job_status_rows_flags_never_run_jobs_overdue(self):
        class Cur:
            def execute(self, *a, **k): pass
            def fetchall(self): return []
            def close(self): pass

        class Conn:
            def cursor(self): return Cur()
            def commit(self): pass

        rows = scheduler.job_status_rows(Conn())
        self.assertTrue(rows)
        self.assertTrue(all(r["overdue"] for r in rows))
        self.assertEqual(rows[-1]["name"], scheduler.HEARTBEAT_JOB)


if __name__ == "__main__":
    unittest.main()
