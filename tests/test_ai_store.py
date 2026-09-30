"""N-3.3: one AI analytics store in MySQL, one feedback scale, chat->order attribution."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import feedback_scale  # noqa: E402
from app1 import memory_store as ms  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402

EXTRA = """
CREATE TABLE ai_sessions (session_id TEXT PRIMARY KEY, user_profile TEXT, conversation_summary TEXT,
  shown_products TEXT, last_active REAL DEFAULT 0);
CREATE TABLE ai_analytics_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp REAL,
  event_type TEXT, query_text TEXT, tool_used TEXT, results_count INTEGER DEFAULT 0,
  feedback_rating INTEGER DEFAULT 0, message_index INTEGER DEFAULT 0, company_id INTEGER, username TEXT, extra TEXT);
CREATE TABLE ai_debug_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp REAL, step TEXT, data TEXT);
CREATE TABLE ai_anonymous_profiles (browser_token TEXT PRIMARY KEY, interests TEXT, budget_range TEXT,
  preferred_location TEXT, preferred_format TEXT, last_viewed TEXT, last_searches TEXT,
  conversation_summary TEXT, created_at REAL DEFAULT 0, last_active REAL DEFAULT 0);
CREATE TABLE ai_latency_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp REAL,
  operation TEXT, latency_ms REAL, prompt_version TEXT, extra TEXT);
CREATE TABLE chatbot_interactions (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, session_id TEXT,
  username TEXT, query_text TEXT, feedback_rating INTEGER DEFAULT 0, message_index INTEGER);
"""


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.raw.executescript(EXTRA)
        self.app = Flask(__name__)
        self.app.secret_key = "x"
        self.app.mysql = self.db
        env = mock.patch.dict(os.environ, {"AI_MEMORY_BACKEND": "mysql"})
        env.start()
        self.addCleanup(env.stop)
        ms._ready_ids.clear()
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.addCleanup(self.ctx.pop)


class StoreTests(StoreBase):
    def test_data_lands_in_mysql(self):
        ms.log_event("s1", "user_query", query_text="hej")
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM ai_analytics_events")["c"], 1)

    def test_feedback_uses_one_scale_and_message_index(self):
        ms.log_event("s1", "feedback", query_text="q", feedback_rating=5, message_index=2)
        ms.log_event("s1", "feedback", query_text="q", feedback_rating=-3, message_index=3)
        rows = self.db.query("SELECT feedback_rating, message_index FROM ai_analytics_events ORDER BY id")
        self.assertEqual([(r["feedback_rating"], r["message_index"]) for r in rows], [(1, 2), (-1, 3)])
        dash = ms.get_observability_dashboard()
        self.assertEqual((dash["feedback_positive"], dash["feedback_negative"]), (1, 1))

    def test_top_rated_is_tenant_scoped(self):
        from flask import session
        session["company_id"] = 7
        ms.log_event("a", "feedback", query_text="for 7", feedback_rating=1)
        session["company_id"] = 8
        ms.log_event("b", "feedback", query_text="for 8", feedback_rating=1)
        self.assertEqual([r["query"] for r in ms.get_top_rated_interactions(company_id=7)], ["for 7"])

    def test_debug_and_latency_round_trip(self):
        ms.log_debug("s1", "tool_call", {"tool": "catalog_search", "q": "æøå"})
        ms.log_latency("s1", "ai_runtime_turn", 120, prompt_version="v2")
        entries = ms.get_debug_logs_for_session("s1")
        self.assertEqual(entries[0]["data"]["q"], "æøå")
        self.assertEqual(ms.get_debug_sessions()[0]["session_id"], "s1")
        self.assertEqual(ms.get_latency_stats()[0]["operation"], "ai_runtime_turn")

    def test_session_and_anonymous_profile_upsert(self):
        ms.save_session("s1", user_profile="p", shown_products=["a"])
        ms.save_session("s1", user_profile="p2", shown_products=["a", "b"])
        self.assertEqual(ms.load_session("s1")["shown_products"], ["a", "b"])
        ms.update_anonymous_interests("tok", new_interests=["ledelse"], new_search="lederkursus")
        ms.update_anonymous_interests("tok", new_interests=["ledelse", "agile"])
        prof = ms.load_anonymous_profile("tok")
        self.assertEqual(prof["interests"], ["ledelse", "agile"])
        self.assertEqual(prof["last_searches"], ["lederkursus"])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM ai_anonymous_profiles")["c"], 1)

    def test_erase_subject_removes_only_that_subject(self):
        ms.save_anonymous_profile("tok", interests=["x"])
        ms.save_anonymous_profile("other", interests=["y"])
        ms.log_event("s1", "user_query")
        ms.log_event("s2", "user_query")
        removed = ms.erase_subject(browser_token="tok", session_id="s1")
        self.assertEqual(removed, 2)
        self.assertIsNone(ms.load_anonymous_profile("tok"))
        self.assertIsNotNone(ms.load_anonymous_profile("other"))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM ai_analytics_events")["c"], 1)

    def test_database_failure_never_raises_into_the_chat_turn(self):
        self.db.raw.execute("DROP TABLE ai_debug_logs")
        ms.log_debug("s1", "x", {})            # must not raise


class FeedbackRouteTests(StoreBase):
    def test_rating_goes_to_the_answer_it_belongs_to(self):
        import run
        app = run.create_app()
        app.mysql = self.db
        d = self.db
        d.execute("INSERT INTO chatbot_interactions (company_id, session_id, username, query_text, message_index) "
                  "VALUES (7,'sid1','ada','første',1),(7,'sid1','ada','anden',2),(7,'sid1','bo','andens',1)")
        c = app.test_client()
        with c.session_transaction() as s:
            s.update({"user": "ada", "session_id": "sid1", "session_ids": {"chat": "sid1"}})
        r = c.post("/app1/feedback", json={"rating": 1, "message_index": 1, "query_text": "første"})
        self.assertEqual(r.status_code, 200)
        rows = {(x["username"], x["message_index"]): x["feedback_rating"]
                for x in d.query("SELECT username, message_index, feedback_rating FROM chatbot_interactions")}
        self.assertEqual(rows[("ada", 1)], 1)
        self.assertEqual(rows[("ada", 2)], 0)        # other answers untouched
        self.assertEqual(rows[("bo", 1)], 0)         # other users untouched
        c.post("/app1/feedback", json={"rating": -9, "message_index": 2})
        self.assertEqual(d.one("SELECT feedback_rating AS f FROM chatbot_interactions WHERE message_index=2 "
                               "AND username='ada'")["f"], -1)


class ScaleTests(unittest.TestCase):
    def test_to_five_maps_thumbs_onto_the_display_scale(self):
        self.assertEqual(feedback_scale.to_five(1), 5.0)
        self.assertEqual(feedback_scale.to_five(-1), 1.0)
        self.assertEqual(feedback_scale.to_five(0), 3.0)
        self.assertEqual(feedback_scale.to_five(None), 0)
        self.assertEqual(feedback_scale.approval_pct(3, 1), 75)
        self.assertIsNone(feedback_scale.approval_pct(0, 0))


class AttributionTests(unittest.TestCase):
    def test_chat_attribution_carries_session_depth_and_tool(self):
        from app1 import tools
        app = Flask(__name__)
        app.secret_key = "x"
        with app.test_request_context("/"):
            from flask import session
            session.update({"session_ids": {"chat": "sid9"}, "_chatbot_query_count": 4,
                            "_last_recommending_tool": "catalog_search"})
            self.assertEqual(tools.chat_attribution(), {
                "chatbot_session_id": "sid9", "chatbot_queries_before_order": 4,
                "recommended_by_tool": "catalog_search"})
            session.pop("_last_recommending_tool")
            self.assertEqual(tools.chat_attribution("create_course_order")["recommended_by_tool"],
                             "create_course_order")


if __name__ == "__main__":
    unittest.main()
