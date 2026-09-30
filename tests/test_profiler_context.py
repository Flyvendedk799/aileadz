"""Profiler rework: need-driven context, mode-aware playbooks, knowledge wiring.

North star (product owner): the AI must read as a fluent consultant using its
tools as a toolbox — never a form-filler working through fields. These tests
pin that down structurally. Offline: no MySQL, no OpenAI.
"""
import json
import os
import re
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

import app1.agent as agent  # noqa: E402

_IMPERATIVES = re.compile(r"DIT NÆSTE SPØRGSMÅL|SKAL VÆRE OM|SPØRG ALDRIG|ALLEREDE AFDÆKKET")


def _completeness(sections, *, target_role="", weighted=40, pct=50):
    return {"sections": sections, "target_role": target_role, "weighted_pct": weighted, "pct": pct}


class AntiChecklistTests(unittest.TestCase):
    def test_no_form_filler_directives_in_profiler_prompts(self):
        for text in (agent.SYSTEM_PLAYBOOK_PROFILER, agent._PROFILER_FEW_SHOT,
                     agent.SYSTEM_PLAYBOOK_PROFILE_SAVE, agent.SYSTEM_PLAYBOOK_CV_ONBOARDING):
            self.assertIsNone(_IMPERATIVES.search(text), text[:80])

    def test_profiler_state_is_background_not_a_directive(self):
        state = agent._build_profiler_state(_completeness([
            {"key": "experience", "label": "Erfaring", "strength": 0.0},
            {"key": "skills", "label": "Kompetencer", "strength": 0.2},
        ]))
        self.assertIsNone(_IMPERATIVES.search(state))
        self.assertIn("Brug det som baggrund", state)

    def test_sections_with_depth_are_never_listed_as_unknown(self):
        # One experience entry = strength 0.5: it exists, so it is not a gap.
        state = agent._build_profiler_state(
            _completeness([
                {"key": "experience", "label": "Erfaring", "strength": 0.5},
                {"key": "education", "label": "Uddannelse", "strength": 0.0},
            ]),
            profile={"experience": [{"id": 1, "title": "Teamleder"}]},
        )
        self.assertIn("erfaring (1)", state)
        self.assertNotIn(agent._SECTION_WHY["experience"], state)
        self.assertIn(agent._SECTION_WHY["education"], state)

    def test_direction_first_then_gaps_once_known(self):
        no_role = agent._build_profiler_state(_completeness([]))
        self.assertIn("ønsket rolle", no_role)
        with_role = agent._build_profiler_state(
            _completeness([], target_role="Data Analyst"),
            gaps=[{"skill": "SQL"}, {"skill": "Power BI"}, {"skill": "Python"}],
        )
        self.assertIn("Ønsket retning: Data Analyst", with_role)
        self.assertIn("SQL og Power BI", with_role)
        self.assertNotIn("Python", with_role)


class ModePlaybookTests(unittest.TestCase):
    def _text(self, layers):
        return "\n".join(l["content"] for l in layers)

    def test_profiler_seed_gets_save_rules_but_no_cv_onboarding_or_search(self):
        text = self._text(agent._build_playbook_messages("greeting", "profiler_resume", "profiler",
                                                         "Start profilsamtalen"))
        self.assertIn("NÅR BRUGEREN FORTÆLLER OM SIG SELV", text)
        # N-5.1: no form-first wording survives in the playbook.
        self.assertNotIn("FORETRUKKEN METODE", text)
        self.assertNotIn("ui_type=form", text)
        self.assertNotIn("CV-ONBOARDING", text)
        self.assertNotIn("SØGE-INTELLIGENS", text)

    def test_profiler_profile_update_never_pulls_cv_onboarding(self):
        text = self._text(agent._build_playbook_messages("profile_update", "profile_update", "profiler",
                                                         "Jeg har erhvervserfaring fra Nordea"))
        self.assertNotIn("CV-ONBOARDING", text)

    def test_profiler_search_playbook_only_on_explicit_course_request(self):
        self.assertNotIn("SØGE-INTELLIGENS", self._text(agent._build_playbook_messages(
            "searching", "discovery", "profiler", "jeg vil gerne blive projektleder")))
        self.assertIn("SØGE-INTELLIGENS", self._text(agent._build_playbook_messages(
            "searching", "discovery", "profiler", "find et kursus i projektledelse")))

    def test_advisor_keeps_cv_onboarding_on_profile_turns(self):
        text = self._text(agent._build_playbook_messages("profile_update", "profile_update", "default", ""))
        self.assertIn("CV-ONBOARDING", text)

    def test_playbooks_are_tagged_context_layers(self):
        layers = agent._build_playbook_messages("searching", "discovery", "default", "")
        self.assertEqual(layers[0]["_layer"], "flow_playbooks")

    def test_mode_profiles(self):
        self.assertIs(agent.mode_profile("nope"), agent.MODE_PROFILES["default"])
        self.assertTrue(agent.MODE_PROFILES["profiler"]["prefer_quality"])
        self.assertEqual(agent.MODE_PROFILES["profiler"]["core_playbook"], agent.SYSTEM_PLAYBOOK_PROFILER)


class ProfileIdTests(unittest.TestCase):
    def test_profile_text_carries_row_ids_for_edits(self):
        from app1.user_profile_db import format_profile_for_ai
        text = format_profile_for_ai({
            "experience": [{"id": 12, "title": "Teamleder", "company": "Novo", "start_year": 2019,
                            "end_year": None, "is_current": True}],
            "certifications": [{"id": 4, "name": "PRINCE2"}],
        }, include_ids=True)
        self.assertIn("[#12] Teamleder @ Novo", text)
        self.assertIn("[#4] PRINCE2", text)
        self.assertNotIn("[#", format_profile_for_ai({"certifications": [{"id": 4, "name": "PRINCE2"}]}))

    def test_update_summary_aliases_survive_the_proposal(self):
        from app1.tools import _execute_update_user_profile
        with mock.patch("app1.user_profile_db.ensure_tables", lambda: None):
            out = json.loads(_execute_update_user_profile(
                {"action": "update_summary", "data": {"role": "Data Analyst", "summary": "Erfaren"}}, "eva"))
        self.assertEqual(out["status"], "proposed")
        self.assertEqual(out["confirm"]["data"], {"target_role": "Data Analyst", "bio": "Erfaren"})


class MemorySelectionTests(unittest.TestCase):
    _MEMS = [
        {"id": 1, "label": "Foretrækker online kurser", "category": "praeference", "confidence": 0.9},
        {"id": 2, "label": "Har to børn", "category": "kontekst", "confidence": 0.9},
        {"id": 3, "label": "Usikker gæt", "category": "andet", "confidence": 0.2},
    ]

    def _run(self, mode, hits):
        with mock.patch("app1.user_profile_db.get_memories", return_value=list(self._MEMS)), \
                mock.patch("app1.user_knowledge.knowledge_enabled", return_value=True), \
                mock.patch("app1.user_knowledge.sync_user"), \
                mock.patch("app1.user_knowledge.search", return_value=hits):
            return agent._select_memories_for_turn("eva", "online kursus", mode)

    def test_only_matched_memories_count_as_used(self):
        out = self._run("profiler", [{"source_id": "1", "score": 0.8}, {"source_id": "2", "score": 0.1}])
        self.assertEqual([m["id"] for m in out], [1, 2])
        self.assertEqual([m["_relevant"] for m in out], [True, False])
        self.assertNotIn(3, [m["id"] for m in out])  # low confidence never injected

    def test_advisor_injects_only_relevant_memories(self):
        out = self._run("default", [{"source_id": "1", "score": 0.8}, {"source_id": "2", "score": 0.1}])
        self.assertEqual([m["id"] for m in out], [1])


class KnowledgeToolReachabilityTests(unittest.TestCase):
    def _names(self, query, *, logged_in=True, mode="default", intent="discovery"):
        from ai_tool_registry import get_employee_tool_selection
        _, meta = get_employee_tool_selection(logged_in=logged_in, company_id=None, intent=intent,
                                              user_query=query, mode=mode)
        return set(meta["tool_names"])

    def test_recall_reachable_on_past_conversation_questions(self):
        self.assertIn("recall_about_user", self._names("hvad talte vi om sidste gang?"))
        self.assertIn("recall_about_user", self._names("what did we talk about last time?"))
        self.assertIn("recall_about_user", self._names("hej", mode="profiler", intent="chit_chat"))
        self.assertNotIn("recall_about_user", self._names("sidste gang", logged_in=False))

    def test_platform_help_reachable_for_how_to_questions_only(self):
        self.assertIn("search_platform_help", self._names("hvordan får jeg godkendt et kursus?"))
        self.assertIn("search_platform_help", self._names("hvor uploader jeg mit cv?", logged_in=False))
        self.assertNotIn("search_platform_help", self._names("hvordan bliver jeg projektleder?"))

    def test_new_tools_are_defined_and_dispatched(self):
        from app1 import tools
        names = {t["function"]["name"] for t in tools.OPENAI_TOOLS + tools.PROFILE_TOOLS}
        self.assertIn("recall_about_user", names)
        self.assertIn("search_platform_help", names)
        call = mock.Mock()
        call.function.name = "search_platform_help"
        call.function.arguments = json.dumps({"query": "hvordan uploader jeg mit cv"})
        out = json.loads(tools.execute_tool(call, username=None, session_id="s"))
        self.assertIn(out["status"], ("success", "no_results"))


class RuntimeContextTests(unittest.TestCase):
    def test_profiler_turn_context_reaches_the_model(self):
        """End-to-end over the real layer builders: a profiler turn with a
        realistic profile keeps the playbook, the profile and the state."""
        import ai_runtime
        profile = {
            "experience": [{"id": 1, "title": "Lager Team Lead", "company": "Nemlig.com",
                            "start_year": 2019, "end_year": None, "is_current": True,
                            "description": "Ansvar for 12 medarbejdere"}],
            "skills": [{"id": i, "name": f"Kompetence {i}", "level": "mellem"} for i in range(20)],
        }
        from app1.user_profile_db import format_profile_for_ai
        messages = [
            {"role": "system", "content": agent.SYSTEM_CORE},
            agent._ctx.layer("mode_core_playbook", agent.SYSTEM_PLAYBOOK_PROFILER),
            agent._ctx.layer("few_shot", agent._PROFILER_FEW_SHOT),
            *agent._build_playbook_messages("greeting", "profiler_resume", "profiler", "Start profilsamtalen"),
            agent._ctx.layer("profile", format_profile_for_ai(profile, include_ids=True),
                             header="BRUGERPROFIL:", fence="BRUGERPROFIL"),
            agent._ctx.layer("profiler_state", agent._build_profiler_state(_completeness([]), profile)),
            {"role": "user", "content": "Start profilsamtalen"},
        ]
        prepared = ai_runtime.prepare_messages_for_turn(messages)
        joined = "\n".join(m["content"] for m in prepared)
        self.assertIn("PROFILER-MODE", joined)
        self.assertIn("[#1] Lager Team Lead @ Nemlig.com", joined)
        self.assertIn("HVOR SAMTALEN STÅR", joined)
        self.assertNotIn("CV-ONBOARDING", joined)


if __name__ == "__main__":
    unittest.main()
