"""cv_service: one CV assembled from the whole platform, analysed and exported (offline)."""
import datetime
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv_service  # noqa: E402

TODAY = datetime.date(2026, 11, 12)

PROFILE = {
    "headline": "Teamleder", "bio": "Kort.", "target_role": "Projektleder", "preferred_location": "Aarhus",
    "skills": [
        {"name": "Ledelse", "level": "ekspert", "source": "course_completion"},
        {"name": "Excel", "level": "mellem", "source": "cv_upload"},
        {"name": "excel", "level": "begynder"},  # duplicate (case) is dropped
    ],
    "experience": [
        {"id": 1, "title": "Konsulent", "company": "X", "start_year": 2010, "end_year": 2013, "description": ""},
        {"id": 2, "title": "Teamleder", "company": "Novo", "start_year": 2016, "is_current": True,
         "description": "• Leder otte konsulenter i kundeteamet\n• Øgede salget 20 % på to år"},
    ],
    "education": [{"degree": "Cand.merc.", "institution": "CBS", "year_completed": 2009}],
    "certifications": [
        {"name": "PMP", "issuer": "PMI", "issue_date": "2019", "expiry_date": "2022"},
        {"name": "Førstehjælp", "expiry_date": "2027-01"},
        {"name": "Truck", "expiry_date": "2030", "credential_url": "javascript:alert(1)"},
    ],
    "completed_courses": [
        {"title": "Scrum", "vendor": "AMU", "completed_date": "2026-02-01", "handle": "scrum"},
        {"title": "Eget kursus", "completed_date": "2020"},
    ],
    "languages": [{"language": "Engelsk", "proficiency": "flydende"}, {"language": "Dansk", "proficiency": "modersmaal"}],
    "portfolio_links": [{"label": "LinkedIn", "url": "linkedin.com/in/x"}, {"label": "Bad", "url": "javascript:alert(1)"}],
    "learning_goals": [{"title": "Blive PL", "status": "aktiv"}, {"title": "Gammelt", "status": "fuldfoert"}],
    "learning_paths": [{"title": "Sti", "status": "aktiv", "steps": [{"done": True}, {"done": False}]},
                       {"title": "Færdig", "status": "aktiv", "steps": [{"done": True}]}],
}
HR = {"matrix": [{"skill_name": "Kommunikation", "current_level": 4}, {"skill_name": "Ledelse", "current_level": 5}],
      "assignments": [{"title": "GDPR", "progress": 40}]}
GAPS = [{"skill": "Projektledelse", "current_label": "ingen", "target_label": "avanceret", "source": "role", "priority": "medium"}]
ORDERS = [{"title": "PRINCE2", "state": "booket"}, {"title": "Scrum", "state": "godkendt"}, {"title": "Afvist", "state": "afvist"}]


def build(**kw):
    args = dict(name="Eva Hansen", email="eva@example.dk", hr=HR, gaps=GAPS, orders=ORDERS, today=TODAY)
    args.update(kw)
    return cv_service.assemble(PROFILE, **args)


class AssembleTests(unittest.TestCase):
    def test_experience_is_ordered_current_first_with_bullets_and_periods(self):
        cv = build()
        self.assertEqual([e["title"] for e in cv["experience"]], ["Teamleder", "Konsulent"])
        self.assertEqual(cv["experience"][0]["period"], "2016 – nu")
        self.assertEqual(cv["experience"][0]["bullets"], ["Leder otte konsulenter i kundeteamet", "Øgede salget 20 % på to år"])
        self.assertEqual(cv["stats"]["years"], 13)  # 2010-2013 + 2016-2026

    def test_skills_carry_platform_evidence_and_hr_rated_skills_join(self):
        cv = build()
        by = {s["name"]: s for s in cv["skills"]}
        self.assertEqual(len([s for s in cv["skills"] if s["name"].lower() == "excel"]), 1)
        kinds = {e["kind"] for e in by["Ledelse"]["evidence"]}
        self.assertEqual(kinds, {"kursus", "leder"})
        self.assertEqual(by["Kommunikation"]["evidence"][0]["kind"], "leder")
        self.assertEqual(by["Kommunikation"]["level"], "avanceret")
        self.assertEqual(by["Excel"]["evidence"], [])  # a CV import is a self-report, not evidence
        self.assertEqual(cv["skill_groups"][0]["level"], "ekspert")

    def test_certification_state_and_unsafe_credential_url(self):
        certs = {c["name"]: c for c in build()["certifications"]}
        self.assertEqual(certs["PMP"]["state"], "udloebet")
        self.assertEqual(certs["Førstehjælp"]["state"], "udloeber")
        self.assertEqual(certs["Truck"]["state"], "gyldig")
        self.assertEqual(certs["Truck"]["credential_url"], "")

    def test_platform_courses_are_verified_and_work_in_progress_is_collected(self):
        cv = build()
        self.assertEqual([c["verified"] for c in cv["courses"]], [True, False])
        titles = [i["title"] for i in cv["in_progress"]]
        self.assertEqual(titles, ["Sti", "GDPR", "PRINCE2"])  # finished path, done course, refused order left out

    def test_links_drop_unsafe_schemes_and_fix_bare_domains(self):
        links = build()["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["url"], "https://linkedin.com/in/x")
        self.assertEqual(links[0]["display"], "linkedin.com/in/x")

    def test_target_role_fit_and_targeted_ordering(self):
        cv = build(prefs={"targeted": True})
        fit = cv["fit"]
        self.assertEqual(fit["role"], "Projektleder")
        self.assertEqual(fit["gaps"][0]["skill"], "Projektledelse")
        self.assertEqual(fit["gaps"][0]["url"], "/catalog?q=Projektledelse")
        self.assertTrue(0 <= fit["pct"] <= 100)
        relevant = [s["relevant"] for s in cv["skills"]]
        self.assertEqual(relevant, sorted(relevant, reverse=True))

    def test_hidden_and_empty_sections(self):
        cv = build(prefs={"hidden": ["languages"]})
        keys = cv_service.visible_sections(cv)
        self.assertNotIn("languages", keys)
        self.assertIn("direction", keys)  # an explicit list replaces the default
        self.assertNotIn("direction", cv_service.visible_sections(build()))  # hidden by default
        self.assertIn("in_progress", keys)
        empty = cv_service.assemble({}, today=TODAY)
        self.assertFalse(empty["has_content"])
        self.assertEqual(cv_service.visible_sections(empty), [])

    def test_never_raises_on_junk_rows(self):
        junk = {"skills": [None, "x", {"name": ""}], "experience": [{"title": None}, 3], "certifications": [{}],
                "portfolio_links": [{"url": None}], "learning_paths": [{"steps": None}]}
        cv = cv_service.assemble(junk, hr={"matrix": [{"skill_name": "A", "current_level": "x"}]}, today=TODAY)
        self.assertEqual(cv["skills"], [])
        self.assertEqual(cv["experience"], [])


class AnalyseTests(unittest.TestCase):
    def test_score_and_actions_point_at_the_right_platform_feature(self):
        q = cv_service.analyse(build())
        checks = {c["key"]: c for c in q["checks"]}
        self.assertFalse(checks["summary"]["ok"])
        self.assertEqual(checks["summary"]["action"]["kind"], "coach")
        self.assertEqual(checks["experience_detail"]["action"], {"kind": "coach", "section": "experience", "id": 1})
        self.assertEqual(checks["certs"]["action"], {"kind": "catalog", "query": "PMP"})
        self.assertEqual(checks["fit"]["action"]["query"], "Projektledelse")
        self.assertFalse(checks["timeline"]["ok"])  # 2013 -> 2016
        self.assertTrue(checks["evidence"]["ok"])
        self.assertTrue(0 < q["score"] < 100)
        self.assertLessEqual(len(q["top"]), 3)
        self.assertEqual(q["top"][0]["severity"], "vigtig")

    def test_empty_cv_starts_with_import(self):
        q = cv_service.analyse(cv_service.assemble({}, today=TODAY))
        self.assertEqual(q["top"][0]["action"], {"kind": "import"})
        self.assertLess(q["score"], 20)

    def test_strong_cv_scores_high(self):
        bio = "Jeg leder projekter i life science og har leveret 12 lanceringer til tiden. " * 4
        profile = dict(PROFILE, bio=bio, headline="Projektleder i life science",
                       experience=[dict(PROFILE["experience"][1]), dict(PROFILE["experience"][0],
                                   end_year=2016, description="Ledte 4 projekter og reducerede omkostninger 10 %")],
                       skills=PROFILE["skills"] + [{"name": n, "level": "avanceret"} for n in "ABCDE"],
                       certifications=[])
        q = cv_service.analyse(cv_service.assemble(profile, gaps=[], today=TODAY))
        self.assertGreaterEqual(q["score"], 85)
        self.assertEqual(q["grade"], "Stærkt")


class ExportTests(unittest.TestCase):
    def test_markdown_and_text(self):
        cv = build()
        md = cv_service.to_markdown(cv)
        self.assertTrue(md.startswith("# Eva Hansen"))
        self.assertIn("### Teamleder, Novo (2016 – nu)", md)
        self.assertIn("- PMP, PMI, 2019 (udløbet)", md)
        self.assertNotIn("javascript", md)
        txt = cv_service.to_text(cv)
        self.assertIn("ERFARING\n--------", txt)
        self.assertIn("• Leder otte konsulenter", txt)
        self.assertNotIn("**", txt)
        self.assertNotIn("\n\n\n", txt)

    def test_chat_card(self):
        cv = build()
        card = cv_service.chat_card(cv, cv_service.analyse(cv))
        self.assertTrue(card["has_cv"])
        self.assertEqual(card["sections"]["skills"][0]["name"], "Ledelse")
        self.assertTrue(card["sections"]["skills"][0]["verified"])
        self.assertEqual(card["fit"]["gaps"], ["Projektledelse"])
        self.assertTrue(card["next_steps"])
        json.dumps(card)


class PrefsTests(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(cv_service.normalise_prefs(None), cv_service.DEFAULT_PREFS)
        p = cv_service.normalise_prefs('{"template": "moderne", "hidden": ["links", "nope"], "targeted": 1}')
        self.assertEqual((p["template"], p["hidden"], p["targeted"]), ("moderne", ["links"], True))
        self.assertEqual(cv_service.normalise_prefs({"template": "<x>"})["template"], "klassisk")
        self.assertEqual(cv_service.normalise_prefs("not json")["template"], "klassisk")

    def test_load_and_save_never_raise(self):
        with mock.patch("app1.user_profile_db.get_cv_prefs", side_effect=RuntimeError):
            self.assertEqual(cv_service.load_prefs("u")["template"], "klassisk")
        with mock.patch("app1.user_profile_db.set_cv_prefs", side_effect=RuntimeError):
            self.assertEqual(cv_service.save_prefs("u", {"template": "kompakt"}), (dict(cv_service.DEFAULT_PREFS, template="kompakt"), False))


class ShowCvSummaryToolTests(unittest.TestCase):
    def test_tool_returns_the_studio_card_with_skills(self):
        import app1.tools as tools
        cv = build()
        with mock.patch("cv_service.build_cv", return_value=(cv, cv_service.analyse(cv))):
            out = json.loads(tools._execute_show_cv_summary({}, "eva"))
        self.assertEqual(out["status"], "cv_summary")
        # regression: the old tool read skill_name/skill_level and always showed no skills
        self.assertEqual(out["sections"]["skills"][0]["name"], "Ledelse")
        self.assertIn("score", out)


if __name__ == "__main__":
    unittest.main()
