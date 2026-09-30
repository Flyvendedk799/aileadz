"""N-5.3: the HR assistant is durable, renders the full event vocabulary and is permission-bound."""

import json
import os
import types
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import ai_reply  # noqa: E402
import hr_agent  # noqa: E402
import hr_conversations as hc  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402

EXTRA = """
CREATE TABLE conversation_history (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, session_id TEXT,
  title TEXT, mode TEXT DEFAULT 'chat', messages TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE user_active_sessions (username TEXT, mode TEXT, session_id TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (username, mode));
CREATE TABLE chatbot_interactions (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, session_id TEXT,
  username TEXT, query_text TEXT, response_text TEXT, query_type TEXT, category TEXT, response_time_ms INTEGER,
  tools_used TEXT, conversation_depth INTEGER, is_logged_in INTEGER, feedback_rating INTEGER DEFAULT 0,
  message_index INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.raw.executescript(EXTRA)
        self.app = Flask(__name__)
        self.app.secret_key = "x"
        self.app.mysql = self.db
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.addCleanup(self.ctx.pop)


class ReplyHelperTests(unittest.TestCase):
    def test_extract_and_strip(self):
        text = 'Svar her.\n<suggestions>["Et", "To", "Tre", "Fire"]</suggestions>'
        self.assertEqual(ai_reply.extract_suggestions(text), ["Et", "To", "Tre"])
        self.assertEqual(ai_reply.strip_suggestions(text), "Svar her.")
        self.assertEqual(ai_reply.extract_suggestions("ingen"), [])
        self.assertEqual(ai_reply.extract_suggestions("<suggestions>ikke json</suggestions>"), [])

    def test_stream_filter_never_leaks_a_split_tag(self):
        flt = ai_reply.SuggestionFilter()
        shown = ""
        for tok in ["Hej ", "der. <sugg", "estions>[\"a\"", "]</suggestions>"]:
            shown += flt.feed(tok)
        shown += flt.flush()
        self.assertEqual(shown.strip(), "Hej der.")

    def test_stream_filter_releases_lookalike_text(self):
        flt = ai_reply.SuggestionFilter()
        out = flt.feed("a < b ") + flt.feed("og <sug") + flt.flush()
        self.assertEqual(out, "a < b og <sug")


class ConversationTests(Base):
    def test_round_trip_is_scoped_to_the_user(self):
        hc.save("hr1", "hr_a", [{"role": "user", "content": "Hvad er budgettet?"},
                                {"role": "assistant", "content": "Det er 100.000 kr."}])
        self.assertEqual(len(hc.load("hr1", "hr_a")), 2)
        self.assertEqual(hc.load("hr2", "hr_a"), [])                 # another user never sees it
        self.assertIsNone(hc.open_session("hr2", "hr_a"))
        self.assertEqual([s["session_id"] for s in hc.list_sessions("hr1")], ["hr_a"])
        self.assertEqual(hc.list_sessions("hr2"), [])

    def test_resume_after_losing_the_cookie_uses_the_last_active_conversation(self):
        hc.save("hr1", "hr_a", [{"role": "user", "content": "hej"}, {"role": "assistant", "content": "hej"}])
        from flask import session
        self.assertEqual(hc.resolve_sid(session, "hr1"), "hr_a")
        new = hc.start_new(session, "hr1")
        self.assertNotEqual(new, "hr_a")
        self.assertEqual(hc.active_sid("hr1"), new)
        self.assertEqual(len(hc.load("hr1", "hr_a")), 2)             # old conversation kept

    def test_employee_chat_sessions_are_untouched(self):
        self.db.execute("INSERT INTO user_active_sessions (username, mode, session_id) VALUES ('hr1','chat','emp1')")
        from flask import session
        hc.start_new(session, "hr1")
        self.assertEqual(self.db.one("SELECT session_id FROM user_active_sessions "
                                     "WHERE username='hr1' AND mode='chat'")["session_id"], "emp1")


def _result(text, tool_results=()):
    return types.SimpleNamespace(
        text=text, runtime="openai", response_id="r1", fallback_reason="", latency_ms=5, usage={},
        compaction_level="normal", runtime_path="openai", tool_results=list(tool_results),
        needs_final_stream=False, stream_messages=None, messages=[])


class AskTests(Base):
    def _ask(self, result, query="Hvad bruger vi?", page=None):
        from flask import session
        session.update({"user": "hr1", "company_id": 7, "company_role": "hr_manager", "company_name": "Acme"})
        patches = [
            mock.patch.object(hr_agent, "close_flask_mysql_connection"),
            mock.patch("ai_runtime.run_agent_with_fallback",
                       **({"side_effect": result} if callable(result) else {"return_value": result})),
            mock.patch("ai_runtime.live_tool_events_enabled", return_value=False),
            mock.patch("ai_runtime.log_agent_run"),
            mock.patch("ai_runtime.log_tool_run"),
            mock.patch("ai_runtime.update_agent_run_quality"),
            mock.patch("ai_tool_registry.toolset_enabled", return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = hr_agent.handle_hr_ask(query, session, page=page)
        events = []
        for line in resp.get_data(as_text=True).split("\n"):
            if line.startswith("data: ") and line.strip() != "data: [DONE]":
                events.append(json.loads(line[6:]))
        return events

    def test_answer_has_no_raw_tag_and_chips_arrive_as_an_event(self):
        ev = self._ask(_result('Vi har brugt **40 %**.\n<suggestions>["Vis pr. afdeling", "Se budget"]</suggestions>'))
        text = "".join(e["content"] for e in ev if e["type"] == "text")
        self.assertIn("40 %", text)
        self.assertNotIn("suggestions", text)
        sug = [e for e in ev if e["type"] == "suggestions"]
        self.assertEqual(sug[0]["items"], ["Vis pr. afdeling", "Se budget"])
        self.assertEqual(ev[-1]["type"], "done")

    def test_missing_tag_gets_fallback_chips(self):
        ev = self._ask(_result("Et svar uden forslag."), page="budgets")
        sug = [e for e in ev if e["type"] == "suggestions"][0]["items"]
        self.assertTrue(sug)

    def test_conversation_is_saved_and_continues_in_the_next_turn(self):
        self._ask(_result("Første svar."), query="Første spørgsmål")
        stored = hc.load("hr1", hc.active_sid("hr1"))
        self.assertEqual([m["content"] for m in stored], ["Første spørgsmål", "Første svar."])
        seen = {}

        def fake(**kw):
            seen["messages"] = kw["messages"]
            return _result("Andet svar.")

        ev = self._ask(fake, query="Andet spørgsmål")
        contents = [m.get("content") for m in seen["messages"]]
        self.assertIn("Første spørgsmål", contents)                   # memory survived "the process"
        self.assertEqual(len(hc.load("hr1", hc.active_sid("hr1"))), 4)
        self.assertEqual([e for e in ev if e["type"] == "meta"][0]["message_index"], 2)

    def test_turn_is_logged_for_feedback(self):
        self._ask(_result("Svar."), query="Hvad bruger vi?")
        row = self.db.one("SELECT company_id, username, category, message_index FROM chatbot_interactions")
        self.assertEqual((row["company_id"], row["username"], row["category"], row["message_index"]),
                         (7, "hr1", "hr_assistant", 1))

    def test_side_effect_tool_produces_a_confirm_card(self):
        tr = types.SimpleNamespace(
            name="hr_send_reminder", call_id="c1", arguments={"who": "alle"},
            output=json.dumps({"needs_confirmation": True, "action": "send_reminder",
                               "message_da": "Send påmindelse til 4 personer?", "recipient_count": 4}))
        with mock.patch("ai_runtime.build_tool_call_event", return_value={"type": "tool_call", "name": tr.name}):
            ev = self._ask(_result("Jeg har forberedt det.", [tr]))
        card = [e for e in ev if e["type"] == "confirm_card"][0]
        self.assertEqual((card["summary_da"], card["recipient_count"]), ("Send påmindelse til 4 personer?", 4))
        self.assertTrue(card["token"])

    def test_prompt_is_proper_danish(self):
        for ascii_form in ("hjaelper", "raadgiver", "traeningsrapporter", " paa "):
            self.assertNotIn(ascii_form, hr_agent.HR_SYSTEM_PROMPT)
        self.assertNotIn("Assistent: [", hr_agent.HR_FEW_SHOT)


class RouteBoundaryTests(Base):
    def _client(self, **sess):
        import run
        app = run.create_app()
        app.mysql = self.db
        c = app.test_client()
        with c.session_transaction() as s:
            s.update(sess)
        return c

    def test_employee_cannot_use_hr_history_or_open(self):
        c = self._client(user="emp", company_id=7, company_role="employee")
        self.assertEqual(c.get("/hr/chatbot/history").status_code, 401)
        self.assertEqual(c.post("/hr/chatbot/open", json={"session_id": "hr_a"}).status_code, 401)

    def test_hr_user_cannot_open_someone_elses_conversation(self):
        hc.save("other", "hr_x", [{"role": "user", "content": "hemmeligt"}, {"role": "assistant", "content": "ok"}])
        c = self._client(user="hr1", company_id=7, company_role="hr_manager")
        self.assertEqual(c.post("/hr/chatbot/open", json={"session_id": "hr_x"}).status_code, 404)

    def test_hr_confirmation_needs_an_hr_role(self):
        from app1 import confirm_store
        token = confirm_store.store_pending("emp-sid", "hr", "hr_send_reminder", {"who": "alle"})
        c = self._client(user="emp", company_id=7, company_role="employee", session_ids={"chat": "emp-sid"},
                         session_id="emp-sid")
        r = c.post("/app1/confirm_tool_action", json={"token": token})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
