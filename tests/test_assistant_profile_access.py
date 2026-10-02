"""The assistant has the same access to the profile as the profile page, and knows when
what it sees is only an excerpt."""
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app1.tools as tools  # noqa: E402
from app1 import user_profile_db as db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _profile(skills=3, experience=2, courses=2):
    return {
        "headline": "Teamleder", "bio": "B" * 600, "target_role": "Afdelingsleder",
        "skills": [{"id": i, "name": f"Skill{i}", "level": "mellem"} for i in range(skills)],
        "experience": [{"id": i, "title": f"Rolle{i}", "company": "Novo", "start_year": 2010 + i, "end_year": None,
                        "is_current": i == 0, "description": "D" * 300} for i in range(experience)],
        "education": [], "certifications": [], "languages": [], "portfolio_links": [],
        "completed_courses": [{"id": i, "title": f"Kursus{i}", "vendor": "AMU", "completed_date": "2024"}
                              for i in range(courses)],
        "learning_goals": [], "learning_paths": [],
    }


class FormatterTests(unittest.TestCase):
    def test_a_small_profile_is_unchanged_and_has_no_cut_marker(self):
        text = db.format_profile_for_ai(_profile(), include_ids=True)
        self.assertNotIn("flere", text)
        self.assertIn("Skill0 (mellem)", text)

    def test_layer_summary_says_how_much_it_left_out(self):
        text = db.format_profile_for_ai(_profile(skills=30, experience=11, courses=20), include_ids=True)
        self.assertIn("(+5 flere, hent alle med get_user_profile)", text)           # skills 30 vs cap 25
        self.assertIn("(+3 flere, hent alle med get_user_profile)", text)           # experience 11 vs cap 8
        self.assertIn("(+5 flere, hent alle med get_user_profile)", text.split("Gennemførte kurser:")[1])
        self.assertNotIn("Skill29", text)

    def test_full_read_lifts_caps_and_cuts(self):
        profile = _profile(skills=30, experience=11, courses=20)
        text = db.format_profile_for_ai(profile, include_ids=True, full=True)
        self.assertNotIn("flere", text)
        self.assertIn("Skill29", text)
        self.assertIn("Rolle10", text)
        self.assertIn("B" * 600, text)            # bio is not cut at 400
        self.assertIn("D" * 300, text)            # nor the experience description at 120
        self.assertIn("Kursus19", text)
        self.assertIn("2024", text)               # completion date is part of the full read

    def test_course_rows_carry_ids_so_they_can_be_edited(self):
        self.assertIn("[#1] Kursus1", db.format_profile_for_ai(_profile(), include_ids=True))
        self.assertNotIn("[#", db.format_profile_for_ai(_profile(), include_ids=False))


class GetUserProfileToolTests(unittest.TestCase):
    def _call(self, args):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_full_profile", return_value=_profile(skills=40)):
            return json.loads(tools._execute_get_user_profile(args, "eva"))

    def test_default_is_the_compact_excerpt(self):
        out = self._call({})
        self.assertIn("(+15 flere", out["profile_text"])
        self.assertEqual(len(out["items"]["skills"]), 25)

    def test_full_returns_everything(self):
        out = self._call({"full": True})
        self.assertNotIn("flere", out["profile_text"])
        self.assertEqual(len(out["items"]["skills"]), 40)

    def test_schema_offers_full_and_the_prompt_explains_when(self):
        schema = next(t for t in tools.PROFILE_TOOLS if t["function"]["name"] == "get_user_profile")
        self.assertIn("full", schema["function"]["parameters"]["properties"])
        import app1.agent as agent
        self.assertIn("get_user_profile(full=true)", agent.SYSTEM_PLAYBOOK_ASSISTANT)


class NavigationTests(unittest.TestCase):
    def _open(self, **args):
        return json.loads(tools._execute_open_in_app(args, "ada"))

    def test_open_my_cv_is_a_known_action_end_to_end(self):
        from app1 import sse_events
        self.assertIn("open_my_cv", sse_events.UI_ACTIONS)
        schema = next(t for t in tools.OPENAI_TOOLS if t["function"]["name"] == "open_in_app")
        self.assertIn("open_my_cv", schema["function"]["parameters"]["properties"]["action"]["enum"])
        out = self._open(action="open_my_cv")
        self.assertEqual(out["target"], "/profil/cv")
        self.assertIn("open_my_cv", _read("static/futurematch/assets/chat.js"))

    def test_open_profile_only_anchors_to_sections_that_exist(self):
        self.assertEqual(self._open(action="open_profile", section="skills")["target"], "/profile#skills")
        self.assertEqual(self._open(action="open_profile", section="cv")["target"], "/profile#cv")
        self.assertEqual(self._open(action="open_profile", section="nonsense<script>")["target"], "/profile")
        self.assertEqual(self._open(action="open_profile")["target"], "/profile")

    def test_whitelist_is_exactly_the_anchors_on_the_profile_page(self):
        page = _read("templates/fm/my_profile.html")
        anchors = set(re.findall(r'\bid="([a-z\-]+)"', page)) | {"cv"}
        self.assertTrue(set(tools._PROFILE_PAGE_SECTIONS) <= anchors, set(tools._PROFILE_PAGE_SECTIONS) - anchors)
        self.assertTrue({"skills", "experience", "education", "languages", "portfolio", "certifications",
                         "courses", "goals", "learning-paths", "cv"} <= set(tools._PROFILE_PAGE_SECTIONS))


class ProfilePageParityTests(unittest.TestCase):
    """Whatever the person can edit on the profile page, the assistant can edit too."""

    def _actions(self):
        schema = next(t for t in tools.PROFILE_TOOLS if t["function"]["name"] == "update_user_profile")
        return set(schema["function"]["parameters"]["properties"]["action"]["enum"])

    def test_every_editable_page_section_has_add_and_remove_actions(self):
        page = _read("templates/fm/my_profile.html")
        families = set(re.findall(r"/api/profile/([a-z\-]+)", page))
        entity = {"skills": "skill", "experience": "experience", "education": "education",
                  "certifications": "certification", "languages": "language", "links": "link"}
        actions = self._actions()
        for family, name in entity.items():
            self.assertIn(family, families, f"profile page no longer edits {family}?")
            self.assertIn(f"add_{name}", actions, family)
            self.assertIn(f"remove_{name}", actions, family)
        self.assertIn("summary", families)
        self.assertTrue({"update_summary", "set_target_role"} <= actions)

    def test_read_only_page_panels_have_a_matching_assistant_tool(self):
        from ai_tool_registry import get_employee_tool_selection
        _, meta = get_employee_tool_selection(
            logged_in=True, company_id=7, intent="discovery", user_query="hvad ved du om mig?", mode="assistant")
        menu = set(meta["tool_names"])
        # gaps, learning paths, goals, CV card, mind-map, full profile read, open the page
        for name in ("show_skill_gaps", "get_learning_path", "get_learning_goals", "show_cv_summary",
                     "show_mindmap_preview", "get_user_profile", "open_in_app"):
            self.assertIn(name, menu, name)


if __name__ == "__main__":
    unittest.main()
