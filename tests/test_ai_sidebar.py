"""Shared AI-workspace sidebar: conversation titles, mode tagging, and the
three connected surfaces (/chat, /ai-profiler, /mind-map) all mounting the
same panel.

Offline — stubs MySQLdb like tests/test_rolling_summary.py, and renders the
Jinja templates with a stubbed url_for (no Flask app, no DB).
"""
import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime

import jinja2


TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "templates")


try:
    import flask  # noqa: F401
except ImportError:
    _flask = types.ModuleType("flask")
    _flask.current_app = None
    sys.modules["flask"] = _flask
if "MySQLdb" not in sys.modules:
    _mysqldb = types.ModuleType("MySQLdb")
    _cursors = types.ModuleType("MySQLdb.cursors")
    _cursors.DictCursor = object
    _mysqldb.cursors = _cursors
    sys.modules["MySQLdb"] = _mysqldb
    sys.modules["MySQLdb.cursors"] = _cursors

if "db_compat" not in sys.modules:
    _db_compat = types.ModuleType("db_compat")
    _db_compat.refresh_flask_mysql_connection = lambda *a, **k: None
    sys.modules["db_compat"] = _db_compat


def _load_user_profile_db():
    if "app1" not in sys.modules:
        pkg = types.ModuleType("app1")
        pkg.__path__ = [os.path.join(os.path.dirname(__file__), "..", "app1")]
        sys.modules["app1"] = pkg
    mod_name = "app1.user_profile_db"
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, "_extract_title", None):
        return existing
    sys.modules.pop(mod_name, None)
    path = os.path.join(os.path.dirname(__file__), "..", "app1", "user_profile_db.py")
    spec = importlib.util.spec_from_file_location(mod_name, os.path.abspath(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module


class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self._result = None
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self._store["sql"].append((sql, params))
        s = " ".join(sql.split()).upper()
        if s.startswith("SELECT ID FROM CONVERSATION_HISTORY"):
            username, session_id = params
            row = self._store["by_session"].get((username, session_id))
            self._result = {"id": row["id"]} if row else None
            self._rows = [self._result] if self._result else []
        elif s.startswith("SELECT ID, SESSION_ID, TITLE, MODE"):
            if "AND SESSION_ID" in s:
                username, session_id = params
                row = self._store["by_session"].get((username, session_id))
                self._result = dict(row) if row else None
                self._rows = [self._result] if self._result else []
            else:
                username, limit = params
                rows = [r for r in self._store["rows"] if r["username"] == username]
                rows.sort(key=lambda r: r["updated_at"], reverse=True)
                self._rows = rows[:limit]
                self._result = self._rows[0] if self._rows else None
        elif s.startswith("INSERT INTO CONVERSATION_HISTORY"):
            username, session_id, title = params[0], params[1], params[2]
            if "MODE" in s:
                mode, messages = params[3], params[4]
            else:
                mode, messages = "chat", params[3]
            rid = self._store["next_id"]
            self._store["next_id"] += 1
            row = {
                "id": rid, "username": username, "session_id": session_id,
                "title": title, "mode": mode, "messages": messages,
                "updated_at": datetime(2026, 9, 6, 12, 0, 0),
            }
            self._store["rows"].append(row)
            self._store["by_session"][(username, session_id)] = row
            self.rowcount = 1
        elif s.startswith("UPDATE CONVERSATION_HISTORY"):
            if "MODE" in s:
                messages, title, mode, rid = params
            else:
                messages, title, rid = params
                mode = None
            for row in self._store["rows"]:
                if row["id"] == rid:
                    row["messages"] = messages
                    row["title"] = title
                    if mode:
                        row["mode"] = mode
                    row["updated_at"] = datetime(2026, 9, 6, 13, 0, 0)
                    self._store["by_session"][(row["username"], row["session_id"])] = row
                    self.rowcount = 1
                    break

    def fetchone(self):
        return self._result

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, store):
        self._store = store

    def ping(self, *a, **k):
        return True

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self._store)

    def commit(self):
        self._store["commits"] += 1

    def rollback(self):
        self._store["rollbacks"] += 1


class _FakeMySQL:
    def __init__(self, store):
        self.connection = _FakeConnection(store)


def _env():
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        undefined=jinja2.ChainableUndefined,
    )
    env.globals["url_for"] = lambda ep, **kw: (
        "/" + kw["filename"] if ep == "static" and kw.get("filename") else "/" + ep
    )
    env.globals["csrf_token"] = lambda: ""
    env.globals["session"] = {}
    env.globals["request"] = jinja2.ChainableUndefined()
    env.globals["get_flashed_messages"] = lambda **kw: []
    env.globals["white_label_active"] = False
    env.globals["company_branding"] = {}
    env.globals["hide_platform_branding"] = False
    return env


class ConversationTitleTests(unittest.TestCase):
    def setUp(self):
        self.upd = _load_user_profile_db()

    def test_first_user_message_becomes_title(self):
        msgs = [
            {"role": "user", "content": "Find PRINCE2 i København"},
            {"role": "assistant", "content": "Her er tre forslag."},
        ]
        self.assertEqual(self.upd._extract_title(msgs), "Find PRINCE2 i København")

    def test_canned_profiler_prompt_is_skipped(self):
        msgs = [
            {"role": "user", "content": "Hjælp mig med at gøre min profil komplet. Stil mig det første spørgsmål."},
            {"role": "assistant", "content": "Hvad er din nuværende rolle?"},
            {"role": "user", "content": "Jeg er projektleder i en kommune."},
        ]
        self.assertEqual(self.upd._extract_title(msgs, mode="profiler"), "Jeg er projektleder i en kommune.")

    def test_only_canned_profiler_prompt_gets_friendly_title(self):
        msgs = [
            {"role": "user", "content": "Hjælp mig med at gøre min profil komplet. Start med min erfaring, og stil mig ét spørgsmål ad gangen."},
        ]
        self.assertEqual(self.upd._extract_title(msgs, mode="profiler"), "Profilsamtale")

    def test_empty_messages_default_title(self):
        self.assertEqual(self.upd._extract_title([]), "Ny samtale")

    def test_normalize_mode(self):
        self.assertEqual(self.upd.normalize_conversation_mode("profiler"), "profiler")
        self.assertEqual(self.upd.normalize_conversation_mode("default"), "chat")
        self.assertEqual(self.upd.normalize_conversation_mode(None), "chat")

    def test_schema_keeps_history_and_goals_as_separate_tables(self):
        hist = [s for s in self.upd._TABLES_SQL if "CREATE TABLE IF NOT EXISTS conversation_history" in s]
        goals = [s for s in self.upd._TABLES_SQL if "CREATE TABLE IF NOT EXISTS user_learning_goals" in s]
        self.assertEqual(len(hist), 1)
        self.assertEqual(len(goals), 1)
        self.assertIn("mode VARCHAR(20)", hist[0])


class SaveConversationModeTests(unittest.TestCase):
    def setUp(self):
        self.store = {
            "sql": [], "rows": [], "by_session": {}, "next_id": 1,
            "commits": 0, "rollbacks": 0,
        }
        self.upd = _load_user_profile_db()

        class _AppProxy:
            mysql = _FakeMySQL(self.store)

        self._orig = self.upd.current_app
        self.upd.current_app = _AppProxy()
        self._orig_refresh = getattr(self.upd, "refresh_flask_mysql_connection", None)
        self.upd.refresh_flask_mysql_connection = lambda *a, **k: None

    def tearDown(self):
        self.upd.current_app = self._orig
        if self._orig_refresh is not None:
            self.upd.refresh_flask_mysql_connection = self._orig_refresh

    def test_insert_stores_profiler_mode_and_friendly_title(self):
        self.upd.save_conversation_history(
            "alice", "sid-1",
            [{"role": "user", "content": "Hjælp mig med at gøre min profil komplet. Stil mig det første spørgsmål."}],
            mode="profiler",
        )
        self.assertEqual(len(self.store["rows"]), 1)
        row = self.store["rows"][0]
        self.assertEqual(row["mode"], "profiler")
        self.assertEqual(row["title"], "Profilsamtale")

    def test_list_returns_mode(self):
        self.upd.save_conversation_history(
            "alice", "sid-1",
            [{"role": "user", "content": "Find ITIL-kurser"}],
            mode="chat",
        )
        rows = self.upd.list_conversations("alice")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "chat")
        self.assertEqual(rows[0]["title"], "Find ITIL-kurser")

    def test_find_by_session(self):
        self.upd.save_conversation_history(
            "alice", "sid-9",
            [{"role": "user", "content": "Ledelseskurser"}],
            mode="chat",
        )
        found = self.upd.find_conversation_by_session("alice", "sid-9")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], 1)
        self.assertEqual(found["mode"], "chat")


class AiSidebarTemplateTests(unittest.TestCase):
    def test_all_three_ai_pages_mount_the_shared_panel(self):
        env = _env()
        for tmpl, page in (
            ("fm/chat.html", "chat"),
            ("fm/ai_profiler.html", "profiler"),
            ("fm/mind_map.html", "mindmap"),
        ):
            html = env.get_template(tmpl).render()
            self.assertIn('id="aiConvPanel"', html, tmpl)
            self.assertIn('id="aiConvList"', html, tmpl)
            self.assertIn("ai-sidebar.js", html, tmpl)
            self.assertIn('data-page="%s"' % page, html, tmpl)
            self.assertIn("fm-ai-cluster", html, tmpl)
            self.assertIn('class="fm-ai-cluster in"', html, tmpl)

    def test_non_ai_page_does_not_mount_conversation_panel(self):
        with open(os.path.join(TEMPLATES, "fm_base.html"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("fm/_ai_sidebar.html", src)
        self.assertIn("ai-sidebar.js", src)
        self.assertLess(src.index("fm-ai-cluster"), src.index("Kursuskatalog"))

    def test_shared_js_has_search_filter_and_mode_badges(self):
        path = os.path.join(os.path.dirname(__file__), "..", "static/futurematch/assets/ai-sidebar.js")
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        for needle in ("aiConvSearch", "data-ai-filter", "profiler", "fmOpenConversation", "/ai-profiler?c="):
            self.assertIn(needle, js)

    def test_chat_js_restores_active_thread_instead_of_always_welcoming(self):
        path = os.path.join(os.path.dirname(__file__), "..", "static/futurematch/assets/chat.js")
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("restoreActiveConversation", js)
        self.assertIn("/app1/load_conversation", js)
        self.assertIn("fmOpenConversation", js)
        self.assertNotIn("function refreshConv", js)
        self.assertNotIn("function renderConv", js)


if __name__ == "__main__":
    unittest.main()
