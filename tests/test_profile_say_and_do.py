"""N-5.1: the profiler says it AND does it (additions saved at once with undo)."""

import json
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from app1 import agent, tools, user_profile_db  # noqa: E402


def _proposal(action, data, message="Tilføj kompetence: Excel (mellem)", section="skills"):
    return json.dumps({"status": "proposed", "section": section, "message": message,
                       "confirm": {"action": action, "data": data}})


class AutosaveTests(unittest.TestCase):
    def test_addition_is_saved_at_once_with_an_undo_payload(self):
        with mock.patch.object(tools, "_propose_user_profile_update",
                               return_value=_proposal("add_skill", {"skill_name": "Excel", "skill_level": "mellem"})), \
                mock.patch.object(tools, "_autosave_profile_add",
                                  return_value=("Excel (mellem)", {"action": "remove_skill", "data": {"skill_name": "Excel"}})) as save:
            res = json.loads(tools._execute_update_user_profile({"action": "add_skill"}, "ada"))
        save.assert_called_once()
        self.assertEqual(res["status"], "saved")
        self.assertIn("Noteret på din profil", res["message"])
        self.assertEqual(res["undo"]["action"], "remove_skill")

    def test_removals_and_edits_still_need_confirmation(self):
        for action in ("remove_skill", "update_experience", "update_summary"):
            raw = _proposal(action, {"skill_name": "x"}, message="Fjern x?")
            with mock.patch.object(tools, "_propose_user_profile_update", return_value=raw), \
                    mock.patch.object(tools, "_autosave_profile_add") as save:
                res = json.loads(tools._execute_update_user_profile({}, "ada"))
            save.assert_not_called()
            self.assertEqual(res["status"], "proposed", action)

    def test_level_upgrade_is_an_edit_and_stays_a_proposal(self):
        raw = _proposal("add_skill", {"skill_name": "Excel", "skill_level": "avanceret"},
                        message='Opgradér "Excel" fra mellem til avanceret?')
        with mock.patch.object(tools, "_propose_user_profile_update", return_value=raw), \
                mock.patch.object(tools, "_autosave_profile_add") as save:
            res = json.loads(tools._execute_update_user_profile({}, "ada"))
        save.assert_not_called()
        self.assertEqual(res["status"], "proposed")

    def test_incomplete_data_and_duplicates_pass_through(self):
        for raw in (json.dumps({"status": "ui_card", "ui_type": "form"}),
                    json.dumps({"status": "already_exists", "message": "findes"}),
                    json.dumps({"status": "error", "message": "x"})):
            with mock.patch.object(tools, "_propose_user_profile_update", return_value=raw):
                self.assertEqual(tools._execute_update_user_profile({}, "ada"), raw)

    def test_failed_save_falls_back_to_the_confirm_card(self):
        raw = _proposal("add_skill", {"skill_name": "Excel"})
        with mock.patch.object(tools, "_propose_user_profile_update", return_value=raw), \
                mock.patch.object(tools, "_autosave_profile_add", side_effect=RuntimeError("db")):
            res = json.loads(tools._execute_update_user_profile({}, "ada"))
        self.assertEqual(res["status"], "proposed")

    def test_switch_off_restores_the_old_behaviour(self):
        raw = _proposal("add_skill", {"skill_name": "Excel"})
        with mock.patch.dict(os.environ, {"AI_PROFILE_AUTOSAVE": "0"}), \
                mock.patch.object(tools, "_propose_user_profile_update", return_value=raw), \
                mock.patch.object(tools, "_autosave_profile_add") as save:
            self.assertEqual(tools._execute_update_user_profile({}, "ada"), raw)
        save.assert_not_called()

    def test_anonymous_user_cannot_save(self):
        res = json.loads(tools._execute_update_user_profile({"action": "add_skill"}, None))
        self.assertEqual(res["status"], "error")


class BatchingTests(unittest.TestCase):
    def test_several_saved_items_share_one_card(self):
        evs = [json.dumps({"type": "profile_saved", "items": [{"label": a, "section": "skills", "undo": None}]})
               for a in ("Excel", "Python")]
        evs.insert(1, json.dumps({"type": "profile_update", "message": "x"}))
        out = [json.loads(e) for e in agent.merge_profile_events(evs)]
        saved = [e for e in out if e["type"] == "profile_saved"]
        self.assertEqual(len(saved), 1)
        self.assertEqual([i["label"] for i in saved[0]["items"]], ["Excel", "Python"])
        self.assertEqual(len(out), 2)

    def test_several_proposals_become_one_batch_but_a_single_one_is_untouched(self):
        one = json.dumps({"type": "profile_confirm_request", "message": "Fjern A?", "confirm": {"action": "remove_skill"}})
        self.assertEqual(agent.merge_profile_events([one]), [one])
        two = [one, json.dumps({"type": "profile_confirm_request", "message": "Fjern B?", "confirm": {"action": "remove_skill"}})]
        out = [json.loads(e) for e in agent.merge_profile_events(two)]
        self.assertEqual(out[0]["type"], "profile_confirm_batch")
        self.assertEqual(len(out[0]["items"]), 2)

    def test_events_are_known_to_the_sse_contract(self):
        from app1 import sse_events
        self.assertIn("profile_saved", sse_events.KNOWN_EVENT_TYPES)
        self.assertIn("profile_confirm_batch", sse_events.KNOWN_EVENT_TYPES)
        js = open(os.path.join(os.path.dirname(__file__), "..", "static/futurematch/assets/chat.js"), encoding="utf-8").read()
        self.assertIn('"profile_saved"', js)
        self.assertIn('"profile_confirm_batch"', js)


class NeedDrivenFramingTests(unittest.TestCase):
    def test_no_checklist_residue_in_prompts_or_ui(self):
        text = agent.SYSTEM_PLAYBOOK_PROFILE_SAVE
        for banned in ("FORETRUKKEN METODE", "ui_type=form", "Fortæl om min"):
            self.assertNotIn(banned, text)
        root = os.path.join(os.path.dirname(__file__), "..")
        banner = open(os.path.join(root, "templates/fm/ai_profiler.html"), encoding="utf-8").read()
        self.assertNotIn("Profilen er komplet", banner)
        self.assertNotIn("Mangler:", banner)
        self.assertNotIn(">felter<", banner)

    def test_completeness_reports_what_the_ai_can_do_now(self):
        c = user_profile_db.profile_completeness(None, profile={
            "skills": [{"name": "Excel", "level": "mellem"}], "target_role": "Controller"})
        self.assertTrue(any("kompetencer" in u for u in c["unlocked"]))
        self.assertTrue(any("Controller" in u for u in c["unlocked"]))
        self.assertTrue(c["next_help"])
        self.assertNotIn("Mangler", c["next_help"])
        empty = user_profile_db.profile_completeness(None, profile={})
        self.assertIn("generelle anbefalinger", empty["unlocked"][0])
        self.assertIn("hvor du gerne vil hen", empty["next_help"])


class CvAwarenessTests(unittest.TestCase):
    def test_note_appears_within_the_hour_and_is_quiet_otherwise(self):
        sess = {"cv_applied": {"t": 1000, "counts": {"skills": 5, "experience": 2, "courses": 0}}}
        note = agent.cv_applied_note(sess, now=1500)
        self.assertIn("5 kompetencer", note)
        self.assertIn("2 erfaringer", note)
        self.assertIn("Spørg ikke", note)
        self.assertEqual(agent.cv_applied_note(sess, now=1000 + 4000), "")
        self.assertEqual(agent.cv_applied_note({}, now=1), "")
        self.assertEqual(agent.cv_applied_note({"cv_applied": {"t": 1, "counts": {}}}, now=2), "")


if __name__ == "__main__":
    unittest.main()
