"""Tests for AI Profiler continuation, target_role persistence, and mode-aware restoration.

Offline: all database and network seams are mocked.
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
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


class ProfilerContinuationTests(unittest.TestCase):
    def test_execute_update_user_profile_set_target_role(self):
        """_execute_update_user_profile with set_target_role persists target_role."""
        from app1.tools import _execute_update_user_profile

        with patch("app1.user_profile_db.ensure_tables", lambda: None), \
             patch("app1.user_profile_db.update_profile_summary") as mock_update:
            mock_update.return_value = True

            # Direct data
            res_str = _execute_update_user_profile(
                {"action": "set_target_role", "data": {"target_role": "Data Analyst"}},
                "alice"
            )
            res = json.loads(res_str)
            self.assertEqual(res.get("status"), "success")
            mock_update.assert_called_with("alice", target_role="Data Analyst")

            # With alias 'role'
            mock_update.reset_mock()
            res_str = _execute_update_user_profile(
                {"action": "set_target_role", "data": {"role": "Machine Learning Engineer"}},
                "alice"
            )
            res = json.loads(res_str)
            self.assertEqual(res.get("status"), "success")
            mock_update.assert_called_with("alice", target_role="Machine Learning Engineer")

            # Missing role returns error
            mock_update.reset_mock()
            res_str = _execute_update_user_profile(
                {"action": "set_target_role", "data": {"target_role": ""}},
                "alice"
            )
            res = json.loads(res_str)
            self.assertEqual(res.get("status"), "error")
            mock_update.assert_not_called()

    def test_execute_update_user_profile_update_summary_preserves_target_role(self):
        """update_summary proposes update with target_role preserved in confirm data."""
        from app1.tools import _execute_update_user_profile

        with patch("app1.user_profile_db.ensure_tables", lambda: None):
            res_str = _execute_update_user_profile(
                {"action": "update_summary", "data": {
                    "target_role": "Senior Consultant",
                    "bio": "Erfaren rådgiver",
                    "headline": "Konsulent"
                }},
                "alice"
            )
            res = json.loads(res_str)
            self.assertEqual(res.get("status"), "proposed")
            self.assertEqual(res.get("section"), "summary")
            confirm_data = res.get("confirm", {}).get("data", {})
            self.assertEqual(confirm_data.get("target_role"), "Senior Consultant")
            self.assertEqual(confirm_data.get("bio"), "Erfaren rådgiver")
            self.assertEqual(confirm_data.get("headline"), "Konsulent")

    def test_load_conversation_by_mode_fallback(self):
        """load_conversation returns latest conversation matching requested mode."""
        from app1.user_profile_db import load_conversation

        profiler_conv = {
            "session_id": "sid-prof",
            "mode": "profiler",
            "messages": [{"role": "user", "content": "Hej fra profiler"}]
        }

        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = {
            "session_id": "sid-chat",
            "mode": "chat",
            "messages": '[{"role": "user", "content": "Hej fra chat"}]',
            "updated_at": None,
        }
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = fake_cursor
        mock_mysql = MagicMock()
        mock_mysql.connection = mock_conn

        # Case 1: active row is chat, but caller requests profiler mode
        with patch("app1.user_profile_db.ensure_tables", lambda: None), \
             patch("app1.user_profile_db.current_app", MagicMock(mysql=mock_mysql)), \
             patch("app1.user_profile_db.load_latest_conversation_by_mode", return_value=dict(profiler_conv)) as mock_by_mode:
            result = load_conversation("alice", mode="profiler")
            self.assertEqual(result["session_id"], "sid-prof")
            self.assertEqual(result["mode"], "profiler")
            mock_by_mode.assert_called_once_with("alice", "profiler")

        # Case 2: active row is profiler, caller requests profiler mode -> direct return
        fake_cursor.fetchone.return_value = {
            "session_id": "sid-prof",
            "mode": "profiler",
            "messages": '[{"role": "user", "content": "Hej fra profiler"}]',
            "updated_at": None,
        }
        with patch("app1.user_profile_db.ensure_tables", lambda: None), \
             patch("app1.user_profile_db.current_app", MagicMock(mysql=mock_mysql)), \
             patch("app1.user_profile_db.load_latest_conversation_by_mode") as mock_by_mode:
            result = load_conversation("alice", mode="profiler")
            self.assertEqual(result["session_id"], "sid-prof")
            self.assertEqual(result["mode"], "profiler")
            mock_by_mode.assert_not_called()

    def test_profiler_handoff_throttling_set(self):
        """PROFILER_HANDOFFS prevents duplicate handoff emission per session."""
        from app1.agent import PROFILER_HANDOFFS

        sid = "test-session-handoff-1"
        PROFILER_HANDOFFS.discard(sid)

        # Before handoff:
        self.assertNotIn(sid, PROFILER_HANDOFFS)

        # Simulate handoff trigger:
        PROFILER_HANDOFFS.add(sid)
        self.assertIn(sid, PROFILER_HANDOFFS)

        # Subsequent check verifies it is throttled:
        should_handoff = sid not in PROFILER_HANDOFFS
        self.assertFalse(should_handoff)

        PROFILER_HANDOFFS.discard(sid)

    def test_ai_profiler_template_contracts(self):
        """ai_profiler.html sends neutral SEED turns (no section-scripted
        sentences that match the profile-update patterns), keeps the dynamic
        Start/Fortsæt CTA and paints the banner from the shared workspace event."""
        tmpl_path = os.path.join(_REPO_ROOT, "templates", "fm", "ai_profiler.html")
        with open(tmpl_path, encoding="utf-8") as fh:
            content = fh.read()

        self.assertIn("'fm:workspace'", content)
        self.assertNotIn("/api/profile/mindmap", content)  # no second graph fetch
        self.assertIn("window.fmSendSeed", content)
        self.assertIn("'Start profilsamtalen'", content)
        self.assertIn("'Fortsæt profilsamtalen'", content)
        self.assertNotIn("formatSeedSection", content)
        self.assertNotIn("Start med min", content)
        self.assertIn("profStartLabel", content)
        self.assertIn("updateCtaState", content)
        self.assertIn("Fortsæt", content)

    def test_seed_titles_are_not_used_as_sidebar_titles(self):
        from app1.user_profile_db import _extract_title
        msgs = [{"role": "user", "content": "Start profilsamtalen"},
                {"role": "assistant", "content": "Hej"}]
        self.assertEqual(_extract_title(msgs, mode="profiler"), "Profilsamtale")

    def test_ai_sidebar_cross_surface_redirection(self):
        """ai-sidebar.js checks surfaceMatches to redirect cross-surface when modes differ."""
        js_path = os.path.join(_REPO_ROOT, "static", "futurematch", "assets", "ai-sidebar.js")
        with open(js_path, encoding="utf-8") as fh:
            js = fh.read()

        self.assertIn("surfaceMatches", js)
        self.assertIn('page === "profiler" && convMode === "profiler"', js)
        self.assertIn('page === "chat" && convMode === "chat"', js)


class ConfirmProfileUpdateEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from run import create_app
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def test_confirm_target_role_endpoint(self):
        """POST /app1/confirm_profile_update with set_target_role saves role."""
        with patch("app1.user_profile_db.update_profile_summary") as mock_update:
            mock_update.return_value = True
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["user"] = "bob"

            resp = client.post(
                "/app1/confirm_profile_update",
                data=json.dumps({"action": "set_target_role", "data": {"target_role": "Scrum Master"}}),
                content_type="application/json"
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["status"], "success")
            mock_update.assert_called_with("bob", target_role="Scrum Master")


if __name__ == "__main__":
    unittest.main()
