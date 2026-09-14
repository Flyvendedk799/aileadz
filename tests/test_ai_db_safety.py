"""Data-safety guards for the AI profile/conversation store.

- ensure_tables must never DROP a table (a failed probe used to wipe
  user_conversations — every active transcript and rolling summary).
- column/index migrations consult information_schema, so a transient error
  can't be mistaken for a missing column.
- vendor/agreement rows are read by key (the app connection is a DictCursor).
- tool failures never hand raw exception text to the model.

Offline: no MySQL, no OpenAI.
"""
import json
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

_SAFE_ENV = {
    "SANDBOX": "1",
    "AI_WARMUP_ON_IMPORT": "0",
    "SCHEDULER_OPPORTUNISTIC": "0",
    "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": "3306",
    "MYSQL_USER": "none", "MYSQL_PASSWORD": "none", "MYSQL_DB": "none",
    "OPENAI_API_KEY": "sk-test",
}
for _k, _v in _SAFE_ENV.items():
    os.environ.setdefault(_k, _v)

from app1 import user_profile_db as db  # noqa: E402


class _FakeCursor:
    def __init__(self, fetchone_results=None):
        self.executed = []
        self._fetchone = list(fetchone_results or [])
        self.rowcount = 1
        self.lastrowid = 42

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        return []

    def close(self):
        pass


class SchemaSafetyTests(unittest.TestCase):
    def test_profile_store_never_drops_tables(self):
        with open(os.path.join(_REPO_ROOT, "app1", "user_profile_db.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("DROP TABLE", source.upper())

    def test_conversation_state_tables_are_declared(self):
        declared = "\n".join(db._TABLES_SQL)
        for table in ("user_active_sessions", "user_conversation_summaries", "user_knowledge"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", declared)

    def test_ensure_column_skips_alter_when_catalog_has_it(self):
        cur = _FakeCursor(fetchone_results=[{"1": 1}])
        with mock.patch.object(db, "current_app", new=mock.MagicMock()) as app:
            db._ensure_column(cur, "conversation_history", "rev", "rev INT NOT NULL DEFAULT 0")
        self.assertEqual(len(cur.executed), 1)
        self.assertIn("information_schema.COLUMNS", cur.executed[0][0])
        app.mysql.connection.commit.assert_not_called()

    def test_ensure_column_alters_only_when_missing(self):
        cur = _FakeCursor(fetchone_results=[None])
        with mock.patch.object(db, "current_app", new=mock.MagicMock()):
            db._ensure_column(cur, "conversation_history", "rev", "rev INT NOT NULL DEFAULT 0")
        self.assertTrue(cur.executed[-1][0].startswith("ALTER TABLE conversation_history ADD COLUMN rev"))

    def test_catalog_error_never_triggers_alter(self):
        class Boom(_FakeCursor):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                raise RuntimeError("lock wait timeout")

        cur = Boom()
        with mock.patch.object(db, "current_app", new=mock.MagicMock()):
            db._ensure_column(cur, "conversation_history", "rev", "rev INT")
        self.assertFalse(any(sql.startswith("ALTER") for sql, _ in cur.executed))

    def test_backfill_is_insert_ignore_and_mode_mapped(self):
        cur = _FakeCursor()
        with mock.patch.object(db, "current_app", new=mock.MagicMock()):
            db._backfill_conversation_state(cur)
        sqls = [sql for sql, _ in cur.executed]
        self.assertEqual(len(sqls), 2)
        for sql in sqls:
            self.assertTrue(sql.startswith("INSERT IGNORE"))
            self.assertIn("'profiler'", sql)


class YearCoercionTests(unittest.TestCase):
    def test_add_education_coerces_blank_year_to_null(self):
        cur = _FakeCursor()
        with mock.patch.object(db, "current_app", new=mock.MagicMock()) as app:
            app.mysql.connection.cursor.return_value = cur
            db.add_education("eva", "Cand.merc", "CBS", year_completed="")
        self.assertIsNone(cur.executed[0][1][3])

    def test_update_education_coerces_year(self):
        cur = _FakeCursor()
        with mock.patch.object(db, "current_app", new=mock.MagicMock()) as app:
            app.mysql.connection.cursor.return_value = cur
            db.update_education("eva", 3, year_completed="2019")
        self.assertIn(2019, cur.executed[0][1])


class MemoryCategoryTests(unittest.TestCase):
    def test_wishlist_and_reminder_are_kept(self):
        self.assertIn("wishlist", db._MEMORY_CATEGORIES)
        self.assertIn("reminder", db._MEMORY_CATEGORIES)


class RowValueTests(unittest.TestCase):
    def test_reads_dict_and_tuple_rows(self):
        from app1.agent import _row_val
        self.assertEqual(_row_val({"vendor_name": "AMU"}, "vendor_name", 0), "AMU")
        self.assertEqual(_row_val(("AMU", "pct"), "vendor_name", 0), "AMU")
        self.assertIsNone(_row_val({}, "vendor_name", 0))
        self.assertIsNone(_row_val((), "vendor_name", 3))


class ToolErrorLeakTests(unittest.TestCase):
    def test_internal_error_hides_exception_text(self):
        from app1 import tools
        out = json.loads(tools._internal_tool_error(
            "get_user_profile", RuntimeError("Unknown column 'secret_col' in users")
        ))
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["error_code"], "get_user_profile_failed")
        self.assertNotIn("secret_col", json.dumps(out))

    def test_execute_tool_hides_exception_text(self):
        from app1 import tools
        call = mock.Mock()
        call.function.name = "get_my_agenda"
        call.function.arguments = "{}"
        with mock.patch.object(tools, "_execute_get_my_agenda",
                               side_effect=RuntimeError("SELECT * FROM leaked_table")):
            out = tools.execute_tool(call, session_id="s1", username="eva")
        self.assertNotIn("leaked_table", out)


if __name__ == "__main__":
    unittest.main()
