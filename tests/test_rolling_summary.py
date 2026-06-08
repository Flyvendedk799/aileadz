"""Unit tests for the cross-session rolling summary persistence (Item #3).

Covers app1.user_profile_db.save_conversation_summary / load_conversation_summary
against a fake MySQL connection (no live DB). The module imports flask.current_app
and MySQLdb at import time, so we stub both before importing it.
"""
import importlib.util
import os
import sys
import types
import unittest


# ── Stub MySQLdb.cursors so importing user_profile_db succeeds without the driver.
if "MySQLdb" not in sys.modules:
    _mysqldb = types.ModuleType("MySQLdb")
    _cursors = types.ModuleType("MySQLdb.cursors")
    _cursors.DictCursor = object  # only referenced as a cursor-type flag
    _mysqldb.cursors = _cursors
    sys.modules["MySQLdb"] = _mysqldb
    sys.modules["MySQLdb.cursors"] = _cursors

# ── Stub db_compat (no real connection to heal in unit tests).
if "db_compat" not in sys.modules:
    _db_compat = types.ModuleType("db_compat")
    _db_compat.refresh_flask_mysql_connection = lambda *a, **k: None
    sys.modules["db_compat"] = _db_compat


def _load_user_profile_db():
    """Load app1/user_profile_db.py by file path WITHOUT importing the heavy
    app1 package __init__ (which boots the whole Flask app). We register a
    minimal namespace `app1` package first so the module's package context
    resolves, then load the single module from its file."""
    if "app1" not in sys.modules:
        pkg = types.ModuleType("app1")
        pkg.__path__ = [os.path.join(os.path.dirname(__file__), "..", "app1")]
        sys.modules["app1"] = pkg
    mod_name = "app1.user_profile_db"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    path = os.path.join(os.path.dirname(__file__), "..", "app1", "user_profile_db.py")
    spec = importlib.util.spec_from_file_location(mod_name, os.path.abspath(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeCursor:
    def __init__(self, store, dict_cursor=False):
        self._store = store
        self._dict = dict_cursor
        self._result = None

    def execute(self, sql, params=()):
        self._store["sql"].append((sql, params))
        s = " ".join(sql.split()).upper()
        if s.startswith("INSERT INTO USER_CONVERSATIONS"):
            # (username, session_id, summary) upsert
            username, session_id, summary = params
            self._store["rows"][username] = {
                "session_id": session_id, "summary": summary,
            }
        elif s.startswith("SELECT SUMMARY FROM USER_CONVERSATIONS"):
            (username,) = params
            row = self._store["rows"].get(username)
            self._result = {"summary": row["summary"]} if row else None

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, store):
        self._store = store

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self._store, dict_cursor=bool(args))

    def commit(self):
        self._store["commits"] += 1

    def rollback(self):
        pass


class _FakeMySQL:
    def __init__(self, store):
        self.connection = _FakeConnection(store)


class RollingSummaryTests(unittest.TestCase):
    def setUp(self):
        self.store = {"sql": [], "rows": {}, "commits": 0}
        upd = _load_user_profile_db()
        self.upd = upd

        class _AppProxy:
            mysql = _FakeMySQL(self.store)

        self._orig_current_app = upd.current_app
        upd.current_app = _AppProxy()

        # Force the connection-refresh to a no-op in the module under test,
        # regardless of whether the real db_compat was already imported by an
        # earlier test in the suite (the module-level stub above only applies
        # when db_compat isn't already in sys.modules). This keeps the test
        # deterministic in isolation AND in the full suite.
        self._orig_refresh = getattr(upd, "refresh_flask_mysql_connection", None)
        upd.refresh_flask_mysql_connection = lambda *a, **k: None

    def tearDown(self):
        self.upd.current_app = self._orig_current_app
        if self._orig_refresh is not None:
            self.upd.refresh_flask_mysql_connection = self._orig_refresh

    def test_save_then_load_roundtrip(self):
        ok = self.upd.save_conversation_summary(
            "alice", "sess-1", "SAMTALEOVERSIGT: vil have ITIL-kursus i København"
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(self.store["commits"], 1)
        loaded = self.upd.load_conversation_summary("alice")
        self.assertIn("ITIL", loaded)
        self.assertIn("SAMTALEOVERSIGT", loaded)

    def test_empty_summary_is_not_saved(self):
        self.assertFalse(self.upd.save_conversation_summary("bob", "s", "   "))
        self.assertFalse(self.upd.save_conversation_summary("bob", "s", None))
        # Nothing persisted, load returns "".
        self.assertEqual(self.upd.load_conversation_summary("bob"), "")

    def test_load_missing_user_returns_empty_string(self):
        self.assertEqual(self.upd.load_conversation_summary("nobody"), "")

    def test_summary_is_capped(self):
        big = "x" * 9000
        self.upd.save_conversation_summary("carol", "s", big)
        loaded = self.upd.load_conversation_summary("carol")
        self.assertLessEqual(len(loaded), 4000)

    def test_save_upserts_on_same_user(self):
        self.upd.save_conversation_summary("dave", "s1", "første")
        self.upd.save_conversation_summary("dave", "s2", "anden")
        self.assertEqual(self.upd.load_conversation_summary("dave"), "anden")
        self.assertEqual(self.store["rows"]["dave"]["session_id"], "s2")


if __name__ == "__main__":
    unittest.main()
