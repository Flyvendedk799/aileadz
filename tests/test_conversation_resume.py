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


if __name__ == "__main__":
    unittest.main()
