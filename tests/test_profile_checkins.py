"""Weekly heartbeat check-ins (profile_checkins.py) and the tools around them.

Offline: no MySQL, no network. The DB layer is replaced by a tiny in-memory fake.
"""
import datetime as dt
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import profile_checkins as pc  # noqa: E402

NOW = dt.datetime(2026, 10, 2, 12, 0)


def _ago(days):
    return NOW - dt.timedelta(days=days)


class BuildCandidatesTests(unittest.TestCase):
    def test_recent_course_asks_about_outcome_not_a_form(self):
        out = pc.build_candidates(
            completed_courses=[{"course_title": "Konflikthåndtering", "added_at": _ago(20)}],
            target_role="Teamleder", now=NOW)
        item = next(c for c in out if c["kind"] == "progress")
        self.assertEqual(item["ref"], "Konflikthåndtering")
        self.assertIn("Konflikthåndtering", item["topic"])
        # a reason for the model, not wording to read out
        self.assertIn("naturligt", item["reason"])

    def test_too_fresh_or_too_old_courses_are_skipped(self):
        out = pc.build_candidates(
            completed_courses=[{"course_title": "Ny", "added_at": _ago(2)},
                               {"course_title": "Gammel", "added_at": _ago(400)}],
            target_role="X", now=NOW)
        self.assertFalse([c for c in out if c["kind"] == "progress"])

    def test_only_two_newest_courses(self):
        rows = [{"course_title": f"K{i}", "added_at": _ago(10 + i)} for i in range(5)]
        out = [c for c in pc.build_candidates(completed_courses=rows, target_role="X", now=NOW)
               if c["kind"] == "progress"]
        self.assertEqual([c["ref"] for c in out], ["K0", "K1"])

    def test_stale_active_goal_only(self):
        out = pc.build_candidates(goals=[
            {"id": 1, "title": "Lær Power BI", "status": "aktiv", "updated_at": _ago(30)},
            {"id": 2, "title": "Færdigt", "status": "fuldfoert", "updated_at": _ago(90)},
            {"id": 3, "title": "Frisk", "status": "aktiv", "updated_at": _ago(2)},
        ], target_role="X", now=NOW)
        goals = [c for c in out if c["kind"] == "goal"]
        self.assertEqual([g["ref"] for g in goals], ["goal:1"])

    def test_direction_needs_something_to_build_on(self):
        self.assertTrue([c for c in pc.build_candidates(has_profile_depth=True, now=NOW) if c["kind"] == "direction"])
        self.assertFalse(pc.build_candidates(has_profile_depth=False, now=NOW))
        self.assertFalse([c for c in pc.build_candidates(target_role="Controller", has_profile_depth=True, now=NOW)
                          if c["kind"] == "direction"])

    def test_weakest_section_becomes_one_gap_item(self):
        completeness = {"sections": [
            {"key": "skills", "label": "Kompetencer", "strength": 0.9},
            {"key": "education", "label": "Uddannelse", "strength": 0.1},
            {"key": "languages", "label": "Sprog", "strength": 0.2},
        ]}
        gaps = [c for c in pc.build_candidates(target_role="X", completeness=completeness, now=NOW)
                if c["kind"] == "gap"]
        self.assertEqual([g["ref"] for g in gaps], ["education"])

    def test_wording_is_need_driven(self):
        out = pc.build_candidates(
            goals=[{"id": 1, "title": "Mål", "status": "aktiv", "updated_at": _ago(30)}],
            completed_courses=[{"course_title": "K", "added_at": _ago(20)}],
            has_profile_depth=True, now=NOW)
        for c in out:
            self.assertNotIn("Fortæl om min", c["topic"] + c["reason"])
            self.assertNotIn("Udfyld", c["topic"] + c["reason"])


class _FakeDb:
    """The slice of user_profile_db the heartbeat touches."""

    def __init__(self, muted=False, open_count=0, profile=None, goals=(), courses=(), completeness=None):
        self.muted, self.open_count = muted, open_count
        self._profile = profile or {}
        self._goals, self._courses = list(goals), list(courses)
        self._completeness = completeness or {}
        self.added = []

    def checkins_muted(self, _u):
        return self.muted

    def open_checkin_count(self, _u):
        return self.open_count + len(self.added)

    def get_full_profile(self, _u):
        return self._profile

    def profile_completeness(self, _u, profile=None):
        return self._completeness

    def get_learning_goals(self, _u):
        return self._goals

    def get_completed_courses(self, _u):
        return self._courses

    def add_checkin(self, _u, kind, ref, topic, reason):
        self.added.append((kind, ref))
        return True


class QueueForUserTests(unittest.TestCase):
    def test_muted_user_is_skipped(self):
        summary = {"muted": 0, "full": 0}
        db = _FakeDb(muted=True, profile={"skills": [1]})
        self.assertEqual(pc.queue_for_user(db, "eva", summary), 0)
        self.assertEqual(summary["muted"], 1)
        self.assertFalse(db.added)

    def test_full_queue_is_left_alone(self):
        summary = {"muted": 0, "full": 0}
        db = _FakeDb(open_count=pc.MAX_OPEN, profile={"skills": [1]})
        self.assertEqual(pc.queue_for_user(db, "eva", summary), 0)
        self.assertEqual(summary["full"], 1)

    def test_never_queues_more_than_the_room_left(self):
        db = _FakeDb(
            open_count=1, profile={"skills": [1], "target_role": ""},
            goals=[{"id": 1, "title": "G", "status": "aktiv", "updated_at": dt.datetime.now() - dt.timedelta(days=40)}],
            courses=[{"course_title": f"K{i}", "added_at": dt.datetime.now() - dt.timedelta(days=20 + i)} for i in range(3)])
        added = pc.queue_for_user(db, "eva")
        self.assertEqual(added, pc.MAX_OPEN - 1)
        self.assertEqual(len(db.added), pc.MAX_OPEN - 1)

    def test_heartbeat_respects_the_kill_switch(self):
        with mock.patch.dict(os.environ, {"AI_PROFILE_CHECKINS": "0"}):
            self.assertEqual(pc.run_heartbeat(mock.Mock()), {"skipped": "disabled"})

    def test_one_failing_user_does_not_stop_the_rest(self):
        app = mock.MagicMock()
        calls = []

        def fake_queue(_db, username, summary=None):
            calls.append(username)
            if username == "bad":
                raise RuntimeError("boom")
            return 1

        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.active_assistant_users", return_value=["a", "bad", "b"]), \
                mock.patch.object(pc, "queue_for_user", side_effect=fake_queue):
            summary = pc.run_heartbeat(app)
        self.assertEqual(calls, ["a", "bad", "b"])
        self.assertEqual((summary["users"], summary["queued"], summary["errors"]), (3, 2, 1))


class LayerTests(unittest.TestCase):
    def test_layer_is_background_with_ids_and_a_cap(self):
        rows = [{"id": i, "topic": f"Emne {i}", "reason": "Fordi."} for i in (4, 5, 6)]
        body = pc.checkin_layer(rows)
        self.assertEqual(body.count("[#c"), pc.MAX_SHOWN)
        self.assertIn("[#c4]", body)
        self.assertIn("kun", body.split("\n")[0])  # optional, not a directive
        self.assertEqual(pc.checkin_layer([]), "")

    def test_agent_layer_marks_items_asked_once(self):
        import app1.agent as agent
        rows = [{"id": 7, "topic": "Status på målet", "reason": "Gammelt."}]
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_pending_checkins", return_value=rows), \
                mock.patch("app1.user_profile_db.mark_checkins_asked") as asked:
            layers = agent._checkin_layers("eva")
        self.assertEqual(len(layers), 1)
        asked.assert_called_once_with("eva", [7])

    def test_agent_layer_is_empty_when_nothing_queued_or_switched_off(self):
        import app1.agent as agent
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_pending_checkins", return_value=[]), \
                mock.patch("app1.user_profile_db.mark_checkins_asked") as asked:
            self.assertEqual(agent._checkin_layers("eva"), [])
        asked.assert_not_called()
        with mock.patch.dict(os.environ, {"AI_PROFILE_CHECKINS": "0"}):
            self.assertEqual(agent._checkin_layers("eva"), [])

    def test_layer_spec_exists_and_is_dropped_before_playbooks(self):
        from ai_context_layers import LAYER_SPECS
        self.assertGreater(LAYER_SPECS["checkins"].priority, LAYER_SPECS["flow_playbooks"].priority - 1)
        self.assertEqual(LAYER_SPECS["checkins"].floor, 0)

    def test_checkins_only_offered_at_the_start_of_a_plain_assistant_conversation(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "app1", "agent.py"), encoding="utf-8").read()
        self.assertIn('if mode == "assistant" and user_turns <= 1 and not surface_context and not _cv_note:', src)


class ToolTests(unittest.TestCase):
    def test_resolve_requires_login_and_valid_outcome(self):
        self.assertEqual(pc.execute_resolve_checkin({"checkin_id": 1}, None)["status"], "error")
        with mock.patch("app1.user_profile_db.ensure_tables"):
            self.assertEqual(pc.execute_resolve_checkin({"checkin_id": 1, "outcome": "bogus"}, "eva")["status"], "error")

    def test_resolve_accepts_the_layer_notation(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.resolve_checkin", return_value=True) as res:
            out = pc.execute_resolve_checkin({"checkin_id": "#c12", "outcome": "dismissed"}, "eva")
        self.assertEqual(out["status"], "ok")
        res.assert_called_once_with("eva", 12, "dismissed")

    def test_resolving_someone_elses_or_closed_item_is_not_found(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.resolve_checkin", return_value=False):
            self.assertEqual(pc.execute_resolve_checkin({"checkin_id": 9}, "eva")["status"], "not_found")

    def test_stop_all_mutes(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.set_checkins_muted") as mute:
            out = pc.execute_resolve_checkin({"outcome": "stop_all"}, "eva")
        self.assertTrue(out["muted"])
        mute.assert_called_once_with("eva", True)

    def test_record_outcome_saves_one_memory_and_closes_the_checkin(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.add_memory", return_value=5) as add, \
                mock.patch("app1.user_profile_db.resolve_checkins_for", return_value=1) as close:
            out = pc.execute_record_learning_outcome(
                {"course_title": "Konflikthåndtering", "learned": "at lytte først", "applied": True,
                 "taught_others": False}, "eva")
        self.assertEqual(out["status"], "memory_saved")
        self.assertEqual(out["closed_checkins"], 1)
        label = add.call_args.args[1]
        detail = add.call_args.kwargs["detail"]
        self.assertEqual(label, "Udbytte af Konflikthåndtering")
        self.assertIn("at lytte først", detail)
        self.assertIn("brugt det i praksis", detail)
        close.assert_called_once_with("eva", "progress", "Konflikthåndtering", "answered")

    def test_record_outcome_needs_a_course_and_something_to_record(self):
        self.assertEqual(pc.execute_record_learning_outcome({"course_title": "X"}, "eva")["status"], "error")
        self.assertEqual(pc.execute_record_learning_outcome({"learned": "x"}, "eva")["status"], "error")
        self.assertEqual(pc.execute_record_learning_outcome({"course_title": "X", "learned": "y"}, None)["status"], "error")

    def test_dispatch_reaches_the_executors(self):
        import app1.tools as tools
        with mock.patch("profile_checkins.execute_resolve_checkin", return_value={"status": "ok"}) as res:
            out = json.loads(tools.execute_tool(_Call("resolve_checkin", {"checkin_id": 3}), username="eva"))
        self.assertEqual(out["status"], "ok")
        res.assert_called_once()


class _Call:
    def __init__(self, name, args):
        self.id = ""
        self.function = type("_Fn", (), {"name": name, "arguments": json.dumps(args)})()


class RegistryTests(unittest.TestCase):
    NEW = ("resolve_checkin", "record_learning_outcome")

    def test_schemas_meta_labels_and_menu(self):
        from ai_tool_registry import _EMPLOYEE_META, _TOOL_LABELS, get_employee_tool_selection
        from app1.tools import PROFILE_TOOLS
        defined = {t["function"]["name"] for t in PROFILE_TOOLS}
        for name in self.NEW:
            self.assertIn(name, defined)
            self.assertIn(name, _EMPLOYEE_META)
            self.assertTrue(_TOOL_LABELS.get(name), name)
        _, meta = get_employee_tool_selection(
            logged_in=True, company_id=None, intent="discovery", user_query="hvad ved du om mig?", mode="assistant")
        for name in self.NEW:
            self.assertIn(name, meta["tool_names"])
        # anonymous visitors never get them
        _, anon = get_employee_tool_selection(
            logged_in=False, company_id=None, intent="discovery", user_query="hej", mode="default")
        for name in self.NEW:
            self.assertNotIn(name, anon["tool_names"])

    def test_outcome_phrases_reach_the_tool_outside_the_assistant_too(self):
        from ai_tool_registry import get_employee_tool_selection
        _, meta = get_employee_tool_selection(
            logged_in=True, company_id=None, intent="discovery",
            user_query="Det lærte jeg af kurset, og jeg har brugt det i praksis", mode="default")
        self.assertIn("record_learning_outcome", meta["tool_names"])

    def test_every_employee_tool_has_a_label_in_chat_js(self):
        """A tool the browser cannot name falls back to a humanised identifier."""
        from app1.tools import OPENAI_TOOLS, PROFILE_TOOLS
        js = open(os.path.join(os.path.dirname(__file__), "..", "static", "futurematch", "assets", "chat.js"),
                  encoding="utf-8").read()
        block = re.search(r"const TOOL_LABELS = \{(.*?)\n  \};", js, re.S).group(1)
        labelled = set(re.findall(r"^\s*(\w+):", block, re.M))
        missing = sorted({(t.get("function") or t)["name"] for t in OPENAI_TOOLS + PROFILE_TOOLS} - labelled)
        self.assertEqual(missing, [])

    def test_mutating_tool_set_includes_the_outcome_tool(self):
        import app1.agent as agent
        self.assertIn("record_learning_outcome", agent._PROFILE_MUTATING_TOOLS)


class SchedulerAndTableTests(unittest.TestCase):
    def test_job_is_registered_weekly(self):
        import scheduler
        job = {j["name"]: j for j in scheduler.JOBS}["profile_checkin_heartbeat"]
        self.assertEqual(job["interval_seconds"], 7 * 86400)
        self.assertTrue(job["enabled"])

    def test_table_is_erased_and_exported_with_the_profile(self):
        import gdpr_service
        self.assertIn("user_profile_checkins", {t for t, _ in gdpr_service._EXPORT_QUERIES})
        self.assertIn("user_profile_checkins", {t for t, _ in gdpr_service._DELETE_TABLES})
        self.assertEqual(gdpr_service.COVERAGE["user_profile_checkins"][0], "delete")


class ChipEventTests(unittest.TestCase):
    def test_chip_ui_explains_statuses_and_pluralises(self):
        js = open(os.path.join(os.path.dirname(__file__), "..", "static", "futurematch", "assets", "chat.js"),
                  encoding="utf-8").read()
        for needle in ("TOOL_STATUS_NOTES", '"1 resultat"', "chip.title = note"):
            self.assertIn(needle, js)

    def test_event_status_vocabulary_matches_the_chip_notes(self):
        from ai_runtime import build_tool_call_event
        res = mock.Mock(output=json.dumps({"status": "memory_saved"}), name="x", status="ok",
                        call_id="c1", latency_ms=5, cache_hit=False)
        res.name = "record_learning_outcome"
        event = build_tool_call_event(res)
        self.assertEqual(event["status"], "memory_saved")
        self.assertEqual(event["message"], "Hukommelse gemt.")
        self.assertTrue(event["side_effect"])


if __name__ == "__main__":
    unittest.main()
