"""Phase 10: /app1/memory GET + DELETE route tests.

Boots the real Flask app under SANDBOX (no MySQL) and drives the two new
memory-management routes. user_profile_db is mocked so no live DB is needed.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

_SAFE_ENV = {
    "SANDBOX": "1",
    "AI_WARMUP_ON_IMPORT": "0",
    "SCHEDULER_OPPORTUNISTIC": "0",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "none",
    "MYSQL_PASSWORD": "none",
    "MYSQL_DB": "none",
    "OPENAI_API_KEY": "sk-test",
}
for _k, _v in _SAFE_ENV.items():
    os.environ.setdefault(_k, _v)

_APP = None
_TMPDIR = None
_ORIG_DB_PATH = None


def setUpModule():
    global _APP, _TMPDIR, _ORIG_DB_PATH
    from app1 import memory_store
    _TMPDIR = tempfile.TemporaryDirectory()
    _ORIG_DB_PATH = memory_store.DB_PATH
    conn = getattr(memory_store._local, "conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
    memory_store._local.conn = None
    memory_store.DB_PATH = os.path.join(_TMPDIR.name, "ai_memory_test.db")
    memory_store.init_db()

    from run import create_app
    _APP = create_app()
    _APP.config["TESTING"] = True
    for flag in ("_ai_subsystems_warmed", "_branding_schema_ensured",
                 "_enterprise_tables_created", "_perf_indexes_ensured"):
        setattr(_APP, flag, True)


def tearDownModule():
    from app1 import memory_store
    conn = getattr(memory_store._local, "conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
    memory_store._local.conn = None
    if _ORIG_DB_PATH:
        memory_store.DB_PATH = _ORIG_DB_PATH
    if _TMPDIR is not None:
        _TMPDIR.cleanup()


_FAKE_MEMORIES = [
    {"id": 1, "category": "wishlist", "label": "Python Basis", "detail": "Gemt til senere"},
    {"id": 2, "category": "reminder", "label": "GDPR kursus", "detail": "Påmind mandag"},
]


class MemoryListRouteTests(unittest.TestCase):
    def _client_with_session(self):
        client = _APP.test_client()
        with client.session_transaction() as sess:
            sess["user"] = "eva"
        return client

    def test_unauthenticated_returns_401(self):
        client = _APP.test_client()
        resp = client.get("/app1/memory")
        self.assertEqual(resp.status_code, 401)

    def test_returns_memory_list(self):
        client = self._client_with_session()
        with patch("app1.user_profile_db.get_memories", return_value=_FAKE_MEMORIES), \
                patch("app1.user_profile_db.ensure_tables"):
            resp = client.get("/app1/memory")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(len(body["memories"]), 2)
        self.assertEqual(body["memories"][0]["id"], 1)
        self.assertEqual(body["memories"][0]["label"], "Python Basis")
        self.assertEqual(body["memories"][0]["category"], "wishlist")

    def test_returns_empty_list_gracefully(self):
        client = self._client_with_session()
        with patch("app1.user_profile_db.get_memories", return_value=[]), \
                patch("app1.user_profile_db.ensure_tables"):
            resp = client.get("/app1/memory")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["memories"], [])


class MemoryDeleteRouteTests(unittest.TestCase):
    def _client_with_session(self):
        client = _APP.test_client()
        with client.session_transaction() as sess:
            sess["user"] = "eva"
        return client

    def test_unauthenticated_returns_401(self):
        client = _APP.test_client()
        resp = client.delete("/app1/memory/1")
        self.assertEqual(resp.status_code, 401)

    def test_delete_existing_returns_ok(self):
        client = self._client_with_session()
        with patch("app1.user_profile_db.remove_memory", return_value=True) as rm, \
                patch("app1.user_profile_db.ensure_tables"):
            resp = client.delete("/app1/memory/42")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        rm.assert_called_once_with("eva", 42)

    def test_delete_nonexistent_returns_not_found(self):
        client = self._client_with_session()
        with patch("app1.user_profile_db.remove_memory", return_value=False), \
                patch("app1.user_profile_db.ensure_tables"):
            resp = client.delete("/app1/memory/99")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "not_found")

    def test_other_user_cannot_delete(self):
        """remove_memory enforces ownership via AND username=? in the SQL."""
        client = _APP.test_client()
        with client.session_transaction() as sess:
            sess["user"] = "attacker"
        with patch("app1.user_profile_db.remove_memory", return_value=False) as rm, \
                patch("app1.user_profile_db.ensure_tables"):
            resp = client.delete("/app1/memory/1")
        self.assertEqual(resp.get_json()["status"], "not_found")
        rm.assert_called_once_with("attacker", 1)


if __name__ == "__main__":
    unittest.main()
