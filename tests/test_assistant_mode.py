"""The AI Profiler is built into the AI assistant: one persona for every logged-in
user, with the advisor's course toolbox and the profiler's profile awareness."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app1.agent as agent  # noqa: E402
from ai_tool_registry import get_employee_tool_selection  # noqa: E402

_PROFILE_TOOLS = {
    "get_user_profile", "update_user_profile", "request_user_input", "remember_about_user",
    "recommend_for_profile", "suggest_learning_path", "show_skill_gaps", "show_cv_summary",
    "set_learning_goal", "recall_about_user", "forget_about_user", "show_mindmap_preview",
}


class AskRouteModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from run import create_app
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def _mode_for(self, requested, logged_in):
        from flask import Response
        client = self.app.test_client()
        if logged_in:
            with client.session_transaction() as sess:
                sess["user"] = "ada"
        body = {"query": "hej"}
        if requested is not None:
            body["mode"] = requested
        with mock.patch("app1.handle_agentic_ask", return_value=Response("ok")) as h:
            client.post("/app1/ask", json=body)
        return h.call_args.kwargs["mode"]

    def test_every_logged_in_turn_is_the_assistant(self):
        for requested in (None, "default", "profiler", "assistant", "nonsense"):
            self.assertEqual(self._mode_for(requested, True), "assistant", requested)

    def test_anonymous_turns_stay_the_plain_advisor(self):
        for requested in (None, "default", "profiler"):
            self.assertEqual(self._mode_for(requested, False), "default", requested)


class AssistantProfileTests(unittest.TestCase):
    def test_assistant_combines_advisor_and_profile_toolbox(self):
        cfg = agent.mode_profile("assistant")
        self.assertIn("assistant", agent.PROFILING_MODES)
        self.assertEqual(cfg["core_playbook"], agent.SYSTEM_PLAYBOOK_ASSISTANT)
        # Advisor side: stage hints and every flow playbook, including course search.
        self.assertTrue(cfg["stage_hints"])
        for pb in ("buying", "profile_save", "cv_onboarding", "search", "situation"):
            self.assertIn(pb, cfg["flow_playbooks"])
        # Profiler side: the longer memory window.
        self.assertGreaterEqual(cfg["memory_limit"], 12)

    def test_assistant_playbook_is_need_driven_not_a_checklist(self):
        text = agent.SYSTEM_PLAYBOOK_ASSISTANT
        for banned in ("DIT NÆSTE SPØRGSMÅL", "SKAL VÆRE OM", "SPØRG ALDRIG", "ALLEREDE AFDÆKKET"):
            self.assertNotIn(banned, text)
        # Asks follow-ups when relevant, and answers a concrete request first.
        self.assertIn("NÅR DET ER RELEVANT, SPØRG", text)
        self.assertIn("svar på det først", text)

    def test_assistant_gets_the_profile_context_layer_and_progress(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "app1", "agent.py"), encoding="utf-8").read()
        self.assertIn("if mode in PROFILING_MODES:\n                    try:\n                        from app1.user_profile_db import profile_completeness", src)

    def test_profile_turn_chips_are_need_driven_for_the_assistant(self):
        chips = agent._fallback_suggestions(mode="assistant", completeness={"target_role": ""}, logged_in=True)
        self.assertIn("Hvor vil jeg gerne hen?", chips)
        self.assertFalse(any("Mangler" in c or "Udfyld" in c for c in chips))
        # Course cards on screen still get the advisor's follow-ups.
        with_cards = agent._fallback_suggestions(mode="assistant", had_cards=True, logged_in=True)
        self.assertIn("Sammenlign de to bedste", with_cards)


class AssistantToolMenuTests(unittest.TestCase):
    def _names(self, **kw):
        tools, meta = get_employee_tool_selection(
            logged_in=True, company_id=None, intent="discovery", user_query="hvad ved du om mig?", **kw)
        return set(meta["tool_names"])

    def test_assistant_menu_carries_the_profile_toolbox_and_course_search(self):
        names = self._names(mode="assistant")
        self.assertTrue(_PROFILE_TOOLS <= names, _PROFILE_TOOLS - names)
        self.assertIn("catalog_search", names)

    def test_plain_advisor_menu_is_not_widened(self):
        self.assertFalse(_PROFILE_TOOLS <= self._names(mode="default"))


class LegacyProfilerUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from run import create_app
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def test_old_profiler_url_redirects_to_chat_keeping_the_handoff(self):
        resp = self.app.test_client().get("/ai-profiler?from=mind_map&focus=skill%3A42")
        self.assertEqual(resp.status_code, 301)
        loc = resp.headers["Location"]
        self.assertTrue(loc.startswith("/chat?"))
        from urllib.parse import parse_qs, urlparse
        self.assertEqual(parse_qs(urlparse(loc).query), {"from": ["mind_map"], "focus": ["skill:42"]})

    def test_no_navigation_offers_a_separate_profiler(self):
        root = os.path.join(os.path.dirname(__file__), "..", "templates")
        for rel in ("fm_base.html", os.path.join("fm", "_ai_modebar.html")):
            src = open(os.path.join(root, rel), encoding="utf-8").read()
            self.assertNotIn("futurematch.ai_profiler", src, rel)


if __name__ == "__main__":
    unittest.main()
