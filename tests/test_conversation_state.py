"""DB-authoritative conversation state (app1/conversation_state.py) + routes.

Guards the bugs this replaces:
- "Ny samtale" DELETEd the single active row (and the rolling summary) and the
  next /ask silently reloaded the previous transcript into the new session;
- chat and profiler shared one session id and one active row;
- a stale gunicorn worker overwrote newer turns;
- confirm tokens minted on one surface could not be resolved from the other.

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

from app1 import conversation_state as cs  # noqa: E402


class _Cursor:
    """Scripted cursor: fetchone results are consumed in order."""

    def __init__(self, fetchone=None, rowcounts=None):
        self.executed = []
        self._fetchone = list(fetchone or [])
        self._rowcounts = list(rowcounts or [])
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        if sql.lstrip().upper().startswith("UPDATE") and self._rowcounts:
            self.rowcount = self._rowcounts.pop(0)

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def close(self):
        pass


def _app_with(cursor):
    app = mock.MagicMock()
    app.mysql.connection.cursor.return_value = cursor
    return app


class SessionIdTests(unittest.TestCase):
    def test_surfaces_get_independent_session_ids(self):
        sess = {}
        chat_sid = cs.resolve_sid(sess, "default")
        prof_sid = cs.resolve_sid(sess, "profiler")
        self.assertNotEqual(chat_sid, prof_sid)
        self.assertEqual(cs.resolve_sid(sess, "chat"), chat_sid)
        self.assertEqual(sess["session_ids"], {"chat": chat_sid, "profiler": prof_sid})

    def test_new_session_only_resets_its_own_surface(self):
        sess = {}
        chat_sid = cs.resolve_sid(sess, "default")
        prof_sid = cs.resolve_sid(sess, "profiler")
        fresh = cs.start_new_session(sess, "profiler")
        self.assertNotEqual(fresh, prof_sid)
        self.assertEqual(sess["session_ids"]["chat"], chat_sid)
        self.assertEqual(sess["session_id"], fresh)

    def test_legacy_session_id_is_not_adopted_by_the_wrong_surface(self):
        sess = {"session_id": "legacy-chat"}
        with mock.patch.object(cs, "load", return_value={"mode": "chat", "messages": []}):
            sid = cs.resolve_sid(sess, "profiler", username="eva")
        self.assertNotEqual(sid, "legacy-chat")
        with mock.patch.object(cs, "load", return_value={"mode": "chat", "messages": []}):
            self.assertEqual(cs.resolve_sid({"session_id": "legacy-chat"}, "default", username="eva"),
                             "legacy-chat")

    def test_all_session_ids_lists_every_surface(self):
        sess = {"session_ids": {"chat": "a", "profiler": "b"}, "session_id": "b"}
        self.assertEqual(cs.all_session_ids(sess), ["a", "b"])


class LoadTests(unittest.TestCase):
    def test_load_never_falls_back_to_another_conversation(self):
        cur = _Cursor(fetchone=[None])
        with mock.patch.object(cs, "current_app", new=_app_with(cur)):
            self.assertIsNone(cs.load("eva", "brand-new-sid"))
        self.assertEqual(len(cur.executed), 1)
        sql, params = cur.executed[0]
        self.assertIn("WHERE username = %s AND session_id = %s", sql)
        self.assertEqual(params, ("eva", "brand-new-sid"))

    def test_load_decodes_row(self):
        row = {"id": 5, "session_id": "s1", "title": "t", "mode": "profiler",
               "messages": json.dumps([{"role": "user", "content": "hej"}]),
               "rev": 3, "summary": "sum", "summary_msg_count": 2,
               "state_json": json.dumps({"handoff": {"attempts": 1}}), "updated_at": None}
        with mock.patch.object(cs, "current_app", new=_app_with(_Cursor(fetchone=[row]))):
            conv = cs.load("eva", "s1")
        self.assertEqual(conv["mode"], "profiler")
        self.assertEqual(conv["rev"], 3)
        self.assertEqual(conv["state"], {"handoff": {"attempts": 1}})
        self.assertEqual(conv["messages"][0]["content"], "hej")


class SaveTurnTests(unittest.TestCase):
    _MSGS = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]

    def test_first_save_inserts_rev_one_and_sets_pointer(self):
        cur = _Cursor(fetchone=[None])
        with mock.patch.object(cs, "current_app", new=_app_with(cur)), \
                mock.patch.object(cs, "set_active") as set_active:
            result = cs.save_turn("eva", "s1", "profiler", self._MSGS)
        self.assertEqual(result["rev"], 1)
        self.assertTrue(any(sql.startswith("INSERT INTO conversation_history") for sql, _ in cur.executed))
        set_active.assert_called_once_with("eva", "profiler", "s1")
        stored = json.loads(next(p for s, p in cur.executed if s.startswith("INSERT"))[4])
        self.assertEqual([m["role"] for m in stored], ["user", "assistant"])

    def test_update_is_revision_checked(self):
        cur = _Cursor(fetchone=[{"id": 9, "rev": 4, "messages": "[]"}], rowcounts=[1])
        with mock.patch.object(cs, "current_app", new=_app_with(cur)), \
                mock.patch.object(cs, "set_active"):
            result = cs.save_turn("eva", "s1", "chat", self._MSGS, expected_rev=4)
        self.assertEqual(result["rev"], 5)
        self.assertFalse(result["conflict"])
        update_sql, params = next((s, p) for s, p in cur.executed if s.startswith("UPDATE"))
        self.assertIn("WHERE id = %s AND rev = %s", update_sql)
        self.assertEqual(params[-2:], (9, 4))

    def test_stale_worker_merges_instead_of_overwriting(self):
        stored = [
            {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2 (other worker)"}, {"role": "assistant", "content": "a2"},
        ]
        ours = self._MSGS + [{"role": "user", "content": "q3"}, {"role": "assistant", "content": "a3"}]
        cur = _Cursor(fetchone=[{"id": 9, "rev": 7, "messages": json.dumps(stored)}], rowcounts=[1])
        with mock.patch.object(cs, "current_app", new=_app_with(cur)), \
                mock.patch.object(cs, "set_active"):
            result = cs.save_turn("eva", "s1", "chat", ours, expected_rev=5)
        self.assertTrue(result["conflict"])
        contents = [m["content"] for m in result["messages"]]
        self.assertEqual(contents, ["q1", "a1", "q2 (other worker)", "a2", "q3", "a3"])

    def test_merge_does_not_duplicate_an_already_stored_turn(self):
        stored = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
        merged = cs.merge_transcripts(stored, list(stored))
        self.assertEqual(merged, stored)

    def test_save_failure_returns_no_rev(self):
        class Boom(_Cursor):
            def execute(self, sql, params=None):
                raise RuntimeError("db down")
        with mock.patch.object(cs, "current_app", new=_app_with(Boom())):
            self.assertIsNone(cs.save_turn("eva", "s1", "chat", self._MSGS)["rev"])


class DigestTests(unittest.TestCase):
    def test_digest_merges_into_surface_summary_and_indexes(self):
        msgs = [{"role": "user", "content": "Jeg vil være teamleder"},
                {"role": "assistant", "content": "Godt mål"}]
        with mock.patch("ai_context.summarize_session", side_effect=["sessionsum", "merged digest"]), \
                mock.patch.object(cs, "load_mode_summary", return_value="gammel digest"), \
                mock.patch.object(cs, "save_session_summary") as save_session, \
                mock.patch.object(cs, "save_mode_summary") as save_mode, \
                mock.patch("app1.user_knowledge.index_conversation_summary") as index:
            out = cs.digest_session("eva", "s1", "profiler", msgs)
        self.assertEqual(out, "sessionsum")
        save_session.assert_called_once_with("eva", "s1", "sessionsum", 2)
        save_mode.assert_called_once_with("eva", "profiler", "merged digest", source_session_id="s1")
        index.assert_called_once_with("eva", "s1", "profiler", "sessionsum")

    def test_rule_based_session_summary_offline(self):
        from ai_context import summarize_session
        with mock.patch.dict(os.environ, {"AI_SESSION_SUMMARY_MODE": "rules"}):
            text = summarize_session([{"role": "user", "content": "Jeg vil lære SQL"},
                                      {"role": "assistant", "content": "Så starter vi der"}], "chat")
            merged = summarize_session([], "chat", previous_digest="Tidligere: Python",
                                       session_summary=text)
        self.assertIn("SQL", text)
        self.assertIn("Python", merged)
        self.assertIn("SQL", merged)


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from run import create_app
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def _client(self, **sess_values):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            for k, v in sess_values.items():
                sess[k] = v
        return client

    def test_new_session_never_deletes_and_keeps_other_surface(self):
        import app1.agent as agent
        agent.CHAT_MEMORY["prof-old"] = [{"role": "system", "content": "s"},
                                         {"role": "user", "content": "hej"},
                                         {"role": "assistant", "content": "hej selv"}]
        client = self._client(user="eva", session_ids={"chat": "chat-open", "profiler": "prof-old"},
                              session_id="prof-old", session_surface="profiler")
        with mock.patch("app1.user_profile_db.ensure_tables", lambda: None), \
                mock.patch("app1.user_profile_db.clear_conversation") as clear, \
                mock.patch.object(cs, "save_turn", return_value={"rev": 2}) as save_turn, \
                mock.patch.object(cs, "digest_session_async") as digest, \
                mock.patch.object(cs, "set_active") as set_active:
            resp = client.post("/app1/new_session", json={"mode": "profiler"})
        data = resp.get_json()
        self.assertEqual(data["mode"], "profiler")
        clear.assert_not_called()
        save_turn.assert_called_once()
        digest.assert_called_once()
        self.assertNotIn("prof-old", agent.CHAT_MEMORY)
        set_active.assert_called_once_with("eva", "profiler", data["session_id"])
        with client.session_transaction() as sess:
            self.assertEqual(sess["session_ids"]["chat"], "chat-open")
            self.assertEqual(sess["session_ids"]["profiler"], data["session_id"])
            self.assertNotEqual(data["session_id"], "prof-old")

    def test_load_conversation_after_new_chat_is_empty(self):
        client = self._client(user="eva")
        with mock.patch("app1.user_profile_db.ensure_tables", lambda: None), \
                mock.patch.object(cs, "get_active", return_value="fresh-sid"), \
                mock.patch.object(cs, "load", return_value=None), \
                mock.patch("app1.user_profile_db.load_latest_conversation_by_mode") as latest:
            data = client.get("/app1/load_conversation?mode=profiler").get_json()
        self.assertEqual(data["status"], "empty")
        latest.assert_not_called()
        with client.session_transaction() as sess:
            self.assertEqual(sess["session_ids"]["profiler"], "fresh-sid")

    def test_load_conversation_restores_only_its_surface(self):
        conv = {"id": 3, "session_id": "chat-sid", "title": "t", "mode": "chat",
                "messages": [{"role": "user", "content": "hej"}]}
        client = self._client(user="eva")
        with mock.patch("app1.user_profile_db.ensure_tables", lambda: None), \
                mock.patch.object(cs, "get_active", return_value="chat-sid"), \
                mock.patch.object(cs, "load", return_value=conv):
            data = client.get("/app1/load_conversation?mode=profiler").get_json()
        self.assertEqual(data["status"], "empty")

    def test_confirm_resolves_tokens_from_either_surface(self):
        client = self._client(user="eva", session_ids={"chat": "chat-sid", "profiler": "prof-sid"},
                              session_id="chat-sid")
        calls = []

        def fake_pop(sid, token):
            calls.append(sid)
            return {"scope": "employee", "tool_name": "manage_my_order", "args": {}} if sid == "prof-sid" else None

        with mock.patch("app1.confirm_store.pop_pending", side_effect=fake_pop), \
                mock.patch("app1.tools.execute_tool", return_value=json.dumps({"status": "success"})) as run_tool:
            data = client.post("/app1/confirm_tool_action", json={"token": "tok"}).get_json()
        self.assertEqual(data["status"], "success")
        self.assertIn("prof-sid", calls)
        self.assertEqual(run_tool.call_args.kwargs["session_id"], "prof-sid")


if __name__ == "__main__":
    unittest.main()
