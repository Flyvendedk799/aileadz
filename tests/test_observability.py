"""N-8.2: request ids, JSON logs, ops alerts, shared cache fallback."""

import json
import logging
import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

import observability  # noqa: E402
import perf_cache  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class RequestIdTests(unittest.TestCase):
    def _client(self):
        import run
        return run.create_app().test_client()

    def test_response_carries_a_generated_request_id(self):
        resp = self._client().get("/healthz")
        rid = resp.headers.get("X-Request-ID")
        self.assertTrue(rid and len(rid) >= 8)

    def test_incoming_request_id_is_echoed(self):
        resp = self._client().get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
        self.assertEqual(resp.headers["X-Request-ID"], "trace-abc-123")

    def test_hostile_request_id_is_replaced(self):
        resp = self._client().get("/healthz", headers={"X-Request-ID": "x" * 200})
        self.assertLessEqual(len(resp.headers["X-Request-ID"]), 64)


class JsonLogTests(unittest.TestCase):
    def test_json_formatter_emits_parseable_lines_with_request_context(self):
        rec = logging.LogRecord("t", logging.WARNING, __file__, 1, "noget gik galt: æøå", None, None)
        rec.request_id, rec.path, rec.user = "r1", "/x", "ada"
        data = json.loads(observability.JsonFormatter().format(rec))
        self.assertEqual((data["level"], data["request_id"], data["user"]), ("WARNING", "r1", "ada"))
        self.assertIn("æøå", data["msg"])

    def test_filter_outside_a_request_uses_dashes(self):
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
        observability.RequestIdFilter().filter(rec)
        self.assertEqual((rec.request_id, rec.path, rec.user), ("-", "-", "-"))


class OpsAlertTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.raw.executescript(
            "CREATE TABLE email_log (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INT, to_email TEXT, "
            "template TEXT, status TEXT, error TEXT, dedupe_key TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
            "CREATE TABLE event_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT);")
        self.db.execute("INSERT INTO users (id, username, role) VALUES (1, 'admin', 'admin'), (2, 'ada', 'user')")

    def _add_email_errors(self, n):
        for _ in range(n):
            self.db.execute("INSERT INTO email_log (to_email, template, status) VALUES ('a@b.dk', 'welcome', 'error')")

    def test_no_alert_below_thresholds(self):
        self._add_email_errors(2)
        out = observability.check_ops_alerts(self.db.connection)
        self.assertEqual(out["alerts"], 0)

    def test_failed_mail_alerts_only_platform_admins_and_dedupes(self):
        self._add_email_errors(4)
        first = observability.check_ops_alerts(self.db.connection)
        second = observability.check_ops_alerts(self.db.connection)
        self.assertEqual(first["alerts"], 1)
        self.assertEqual(second["alerts"], 0)              # same hour: no spam
        rows = self.db.query("SELECT user_id, action_url, is_urgent FROM notifications")
        self.assertEqual([r["user_id"] for r in rows], ["admin"])
        self.assertEqual(rows[0]["action_url"], "/admin/system-health")

    def test_stuck_webhooks_alert(self):
        for _ in range(6):
            self.db.execute("INSERT INTO event_outbox (status) VALUES ('failed')")
        self.assertEqual(observability.check_ops_alerts(self.db.connection)["alerts"], 1)

    def test_the_job_is_registered_with_the_worker(self):
        import scheduler
        self.assertIn("ops_alerts", [j["name"] for j in scheduler.JOBS])


class SharedCacheTests(unittest.TestCase):
    def setUp(self):
        perf_cache.cache_clear()
        perf_cache._REDIS_STATE.update(client=None, retry_at=0.0)

    def test_without_redis_url_the_in_process_cache_is_used(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": ""}):
            perf_cache.cache_set(("k", 1), {"a": 1}, 30)
            self.assertEqual(perf_cache.cache_get(("k", 1)), ({"a": 1}, True))

    def test_unreachable_redis_falls_back_quietly(self):
        fake = types.SimpleNamespace(Redis=types.SimpleNamespace(
            from_url=mock.Mock(side_effect=ConnectionError("down"))))
        with mock.patch.dict(sys.modules, {"redis": fake}), mock.patch.dict(os.environ, {"REDIS_URL": "redis://x"}):
            perf_cache.cache_set(("k", 2), 5, 30)
            self.assertEqual(perf_cache.cache_get(("k", 2)), (5, True))

    def test_shared_backend_roundtrip_and_clear(self):
        store = {}

        class FakeRedis:
            def ping(self): return True
            def get(self, k): return store.get(k)
            def set(self, k, v, px=None): store[k] = v
            def delete(self, k): store.pop(k, None)
            def scan_iter(self, match=None, count=None):
                pre = match.rstrip("*")
                return [k for k in list(store) if k.startswith(pre)]

        fake = types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda *a, **k: FakeRedis()))
        with mock.patch.dict(sys.modules, {"redis": fake}), mock.patch.dict(os.environ, {"REDIS_URL": "redis://x"}):
            perf_cache.cache_set(("mod.fn", (1,)), [1, 2], 30)
            self.assertEqual(perf_cache.cache_get(("mod.fn", (1,))), ([1, 2], True))
            self.assertEqual(len(store), 1)
            perf_cache.cache_clear("mod.fn")
            self.assertEqual(perf_cache.cache_get(("mod.fn", (1,))), (None, False))


if __name__ == "__main__":
    unittest.main()
