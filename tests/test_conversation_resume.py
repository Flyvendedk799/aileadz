"""
Offline test for POST /app1/conversations/<id>/resume — the "open a past
conversation" backend that fixes the sidebar click doing nothing.

Boots the REAL app via run.create_app() under the SANDBOX no-MySQL env and
drives the route through Flask's test client, with the user_profile_db seam
mocked at the module boundary (mirrors tests/test_ask_sse_offline.py). No
OPENAI_API_KEY, no MySQL, no network.

Contract:
  * anonymous (no session 'user')           -> 401,
  * a logged-in resume of an existing conv  -> status 'ok', returns the stored
    messages, promotes it to the active conversation (save_conversation), points
    session['session_id'] at the conversation's session_id, and drops any stale
    in-memory CHAT_MEMORY for the previous session id,
  * a missing conversation                  -> 404.
"""

import json
import os
import sys
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

from run import create_app  # noqa: E402
import app1.agent as agent  # noqa: E402

_CONV = {
    "id": 7,
    "session_id": "stored-sid-7",
    "title": "Ledelseskurser til mit team",
    "messages": [
        {"role": "user", "content": "Jeg leder et team på 6."},
        {"role": "assistant", "content": "Godt — jeg foreslår PRINCE2 og Scrum."},
    ],
}


class ConversationResumeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def _client(self, user=None):
        client = self.app.test_client()
        if user is not None:
            with client.session_transaction() as sess:
                sess["user"] = user
        return client

    def test_anonymous_is_rejected(self):
        resp = self._client().post("/app1/conversations/7/resume")
        self.assertEqual(resp.status_code, 401)

    def test_resume_loads_and_activates_conversation(self):
        saved = {}

        def fake_save(username, session_id, messages):
            saved["args"] = (username, session_id, messages)

        with patch("app1.user_profile_db.ensure_tables", lambda: None), \
             patch("app1.user_profile_db.load_conversation_by_id", return_value=dict(_CONV)), \
             patch("app1.user_profile_db.save_conversation", side_effect=fake_save):
            client = self._client(user="alice")
            # Seed a stale in-memory session that resume must clear.
            with client.session_transaction() as sess:
                sess["session_id"] = "old-sid"
            agent.CHAT_MEMORY["old-sid"] = [{"role": "system", "content": "x"}]

            resp = client.post("/app1/conversations/7/resume")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["session_id"], "stored-sid-7")
            self.assertEqual(len(data["messages"]), 2)

            # Promoted to active conversation with the stored session id + messages.
            self.assertIn("args", saved)
            self.assertEqual(saved["args"][0], "alice")
            self.assertEqual(saved["args"][1], "stored-sid-7")

            # Session now points at the restored conversation; stale memory dropped.
            with client.session_transaction() as sess:
                self.assertEqual(sess["session_id"], "stored-sid-7")
            self.assertNotIn("old-sid", agent.CHAT_MEMORY)
            self.assertNotIn("stored-sid-7", agent.CHAT_MEMORY)

    def test_missing_conversation_is_404(self):
        with patch("app1.user_profile_db.ensure_tables", lambda: None), \
             patch("app1.user_profile_db.load_conversation_by_id", return_value=None):
            resp = self._client(user="alice").post("/app1/conversations/999/resume")
            self.assertEqual(resp.status_code, 404)

    def test_resume_returns_stored_ui_artifacts(self):
        """Cards/tool chips persisted on an assistant turn survive the round-trip
        so the frontend can replay the rich UI (the bug this change fixes)."""
        conv = dict(_CONV)
        conv["messages"] = [
            {"role": "user", "content": "Find projektledelseskurser."},
            {"role": "assistant", "content": "Her er et match.",
             "_cards": [{"title": "PRINCE2 Foundation", "meta": []}],
             "_tools": [{"type": "tool_call", "name": "catalog_search",
                         "label": "Katalogsøgning", "results_count": 3}]},
        ]
        agent.SHOWN_ARTIFACTS.pop("stored-sid-7", None)
        with patch("app1.user_profile_db.ensure_tables", lambda: None), \
             patch("app1.user_profile_db.load_conversation_by_id", return_value=conv), \
             patch("app1.user_profile_db.save_conversation", lambda *a, **k: None):
            resp = self._client(user="alice").post("/app1/conversations/7/resume")
            self.assertEqual(resp.status_code, 200)
            asst = [m for m in resp.get_json()["messages"] if m["role"] == "assistant"][0]
            self.assertEqual(asst["_cards"][0]["title"], "PRINCE2 Foundation")
            self.assertEqual(asst["_tools"][0]["name"], "catalog_search")
            # The artifact cache is re-seeded so a follow-up turn's save preserves
            # these earlier cards/chips instead of dropping them.
            seeded = agent.SHOWN_ARTIFACTS.get("stored-sid-7", {})
            self.assertIn("Her er et match.", seeded)
            self.assertEqual(seeded["Her er et match."]["cards"][0]["title"], "PRINCE2 Foundation")
        agent.SHOWN_ARTIFACTS.pop("stored-sid-7", None)


class TurnArtifactHelpersTest(unittest.TestCase):
    """Unit tests for the agent helpers that capture/reattach UI artifacts."""

    def setUp(self):
        self._sid = "art-sid"
        agent.SHOWN_ARTIFACTS.pop(self._sid, None)

    def tearDown(self):
        agent.SHOWN_ARTIFACTS.pop(self._sid, None)

    def test_reattach_keeps_live_messages_clean(self):
        agent._record_turn_artifacts(
            self._sid, "Her er et match.",
            [{"title": "X", "meta": []}],
            [{"type": "tool_call", "name": "catalog_search"}],
        )
        live = [
            {"role": "user", "content": "find kurser"},
            {"role": "assistant", "content": "Her er et match."},
        ]
        persisted = agent._messages_with_artifacts(self._sid, live)
        # Persisted copy carries artifacts...
        pa = [m for m in persisted if m["role"] == "assistant"][0]
        self.assertIn("_cards", pa)
        self.assertIn("_tools", pa)
        # ...but the live message dict is untouched (no keys reach the API).
        self.assertNotIn("_cards", live[1])
        self.assertNotIn("_tools", live[1])

    def test_reattach_matches_by_answer_text_across_turns(self):
        agent._record_turn_artifacts(self._sid, "svar A", [{"title": "A", "meta": []}], [])
        agent._record_turn_artifacts(self._sid, "svar B", [{"title": "B", "meta": []}], [])
        live = [
            {"role": "assistant", "content": "svar A"},
            {"role": "user", "content": "mere"},
            {"role": "assistant", "content": "svar B"},
        ]
        persisted = agent._messages_with_artifacts(self._sid, live)
        asst = [m for m in persisted if m["role"] == "assistant"]
        self.assertEqual(asst[0]["_cards"][0]["title"], "A")
        self.assertEqual(asst[1]["_cards"][0]["title"], "B")

    def test_seed_then_save_preserves_across_workers(self):
        """The cards-don't-reappear regression: a worker that never handled an
        earlier turn must still preserve that turn's cards when it saves the full
        transcript after a new turn. Seeding from the persisted transcript fixes
        it. Simulates worker hand-off without any in-process carry-over."""
        sid = self._sid
        # --- Worker 1: turn 1 produces a card, persisted with _cards. ---
        agent._record_turn_artifacts(sid, "svar 1", [{"title": "Kursus 1", "meta": []}],
                                     [{"type": "tool_call", "name": "catalog_search"}])
        persisted_t1 = agent._messages_with_artifacts(
            sid, [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "svar 1"}])
        # JSON round-trip, exactly like save_conversation_history -> load.
        stored = json.loads(json.dumps(persisted_t1, ensure_ascii=False))

        # --- Worker 2: fresh process, no in-memory artifacts/memory. ---
        agent.SHOWN_ARTIFACTS.pop(sid, None)
        agent.seed_artifacts_from_messages(sid, stored)
        self.assertIn("svar 1", agent.SHOWN_ARTIFACTS.get(sid, {}))

        # Worker 2 handles turn 2; CHAT_MEMORY is the clean (stripped) restore.
        agent._record_turn_artifacts(sid, "svar 2", [{"title": "Kursus 2", "meta": []}], [])
        clean = [{"role": m["role"], "content": m["content"]} for m in stored]
        clean += [{"role": "user", "content": "q2"}, {"role": "assistant", "content": "svar 2"}]
        persisted_t2 = agent._messages_with_artifacts(sid, clean)
        asst = [m for m in persisted_t2 if m["role"] == "assistant"]
        # BOTH turns keep their cards — turn 1's are not lost by the overwrite.
        self.assertEqual(asst[0]["_cards"][0]["title"], "Kursus 1")
        self.assertEqual(asst[1]["_cards"][0]["title"], "Kursus 2")


if __name__ == "__main__":
    unittest.main()
