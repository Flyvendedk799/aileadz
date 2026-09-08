"""The AI write paths must survive the argument shapes models actually emit.

Regression cover for one recurring bug class: an executor assumed the ONE shape
the schema describes, so a tool call carrying every value it needed was refused
(or silently mangled) because the container around those values differed.

Seen in production: three AMU courses in one profiler turn all failed with
"course_title mangler." because the model flattened the fields next to `action`
instead of nesting them under `data`; a `handles` string was sliced into
one-character product lookups; and every confirm-card "Bekraeft" click raised
AttributeError because the confirm route re-dispatched a flat tool call into an
executor that only read `tool_call.function.*`.
"""
import json
import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import types  # noqa: E402

import cv_ingest  # noqa: E402
import hr_tools  # noqa: E402
from app1.tools import (  # noqa: E402
    _as_list, _as_object, _as_object_list, _multi_value_fields,
    _normalize_profile_args, execute_tool,
)
from tool_confirm import tool_call_parts  # noqa: E402


class NormalizeProfileArgs(unittest.TestCase):
    def test_nested_data_is_passed_through(self):
        action, data = _normalize_profile_args(
            {"action": "add_skill", "data": {"skill_name": "Excel", "skill_level": "avanceret"}})
        self.assertEqual(action, "add_skill")
        self.assertEqual(data, {"skill_name": "Excel", "skill_level": "avanceret"})

    def test_flat_args_are_lifted_into_data(self):
        # The exact shape that failed in production (AI Profiler, AMU courses).
        action, data = _normalize_profile_args(
            {"action": "add_course", "course_title": "Salgsledelse",
             "vendor": "AMU", "completed_date": None})
        self.assertEqual(action, "add_course")
        self.assertEqual(data["course_title"], "Salgsledelse")
        self.assertEqual(data["vendor"], "AMU")
        self.assertNotIn("completed_date", data)

    def test_data_as_json_string(self):
        _, data = _normalize_profile_args(
            {"action": "add_skill", "data": '{"skill_name": "SQL", "skill_level": "mellem"}'})
        self.assertEqual(data["skill_name"], "SQL")

    def test_unparseable_data_string_falls_back_to_flat_fields(self):
        _, data = _normalize_profile_args(
            {"action": "add_skill", "data": "Excel", "skill_name": "Excel"})
        self.assertEqual(data["skill_name"], "Excel")

    def test_nested_value_wins_over_flat_duplicate(self):
        _, data = _normalize_profile_args(
            {"action": "add_skill", "skill_level": "begynder",
             "data": {"skill_name": "Excel", "skill_level": "ekspert"}})
        self.assertEqual(data["skill_level"], "ekspert")

    def test_aliases_are_action_scoped(self):
        # "name" means the skill on add_skill, the course on add_course, and the
        # certification itself on add_certification.
        _, skill = _normalize_profile_args({"action": "add_skill", "name": "Excel", "level": "ekspert"})
        self.assertEqual(skill, {"skill_name": "Excel", "skill_level": "ekspert"})

        _, course = _normalize_profile_args(
            {"action": "add_course", "data": {"title": "Konflikthåndtering", "provider": "AMU"}})
        self.assertEqual(course, {"course_title": "Konflikthåndtering", "vendor": "AMU"})

        _, cert = _normalize_profile_args(
            {"action": "add_certification", "name": "PRINCE2", "issuer": "AXELOS"})
        self.assertEqual(cert, {"name": "PRINCE2", "issuer": "AXELOS"})

    def test_unknown_keys_are_dropped(self):
        _, data = _normalize_profile_args(
            {"action": "add_skill", "skill_name": "Excel", "confidence": 0.9})
        self.assertEqual(data, {"skill_name": "Excel"})

    def test_missing_or_garbage_args(self):
        self.assertEqual(_normalize_profile_args(None), ("", {}))
        self.assertEqual(_normalize_profile_args({}), ("", {}))
        self.assertEqual(_normalize_profile_args({"action": "add_skill"}), ("add_skill", {}))


class MultiValueGuard(unittest.TestCase):
    def test_list_value_is_reported_not_crashed(self):
        self.assertEqual(
            _multi_value_fields({"skill_name": ["Excel", "SQL", "Word"]}), ["skill_name"])

    def test_scalar_values_pass(self):
        self.assertEqual(
            _multi_value_fields({"course_title": "Salgsledelse", "id": 3, "is_current": True}), [])


class ListArguments(unittest.TestCase):
    """`handles` and friends: a string is iterable, so a bad shape used to be
    walked character by character instead of rejected."""

    def test_real_list_passes_through(self):
        self.assertEqual(_as_list(["a-handle", "b-handle"]), ["a-handle", "b-handle"])

    def test_comma_string_is_split_not_sliced(self):
        self.assertEqual(_as_list("a-handle, b-handle"), ["a-handle", "b-handle"])

    def test_json_string_array(self):
        self.assertEqual(_as_list('["a-handle", "b-handle"]'), ["a-handle", "b-handle"])

    def test_single_value_becomes_one_item(self):
        self.assertEqual(_as_list("a-handle"), ["a-handle"])

    def test_objects_are_reduced_to_their_handle(self):
        self.assertEqual(_as_list([{"handle": "a"}, {"handle": "b"}]), ["a", "b"])

    def test_duplicates_and_blanks_drop(self):
        self.assertEqual(_as_list(["a", "a", "", None, " b "]), ["a", "b"])

    def test_empty_shapes(self):
        for value in (None, "", [], True, False):
            self.assertEqual(_as_list(value), [])


class ObjectArguments(unittest.TestCase):
    def test_object_list_from_json_string(self):
        self.assertEqual(
            _as_object_list('[{"name": "title", "label": "Titel", "type": "text"}]'),
            [{"name": "title", "label": "Titel", "type": "text"}])

    def test_single_object_is_wrapped(self):
        self.assertEqual(_as_object_list({"name": "a"}), [{"name": "a"}])

    def test_non_objects_are_dropped(self):
        self.assertEqual(_as_object_list(["a", {"name": "b"}, 3]), [{"name": "b"}])

    def test_garbage_yields_empty_not_crash(self):
        for value in (None, "", "not json", 7):
            self.assertEqual(_as_object_list(value), [])
            self.assertEqual(_as_object(value), {})

    def test_object_from_json_string(self):
        self.assertEqual(_as_object('{"title": "Salgsleder"}'), {"title": "Salgsleder"})


class ToolCallShapes(unittest.TestCase):
    """Both callers are legitimate: the provider shape and the confirm route's
    internal re-dispatch. Reading only the first one broke every Bekraeft."""

    def test_provider_shape(self):
        call = types.SimpleNamespace(
            function=types.SimpleNamespace(name="catalog_search", arguments='{"query": "ledelse"}'))
        self.assertEqual(tool_call_parts(call), ("catalog_search", {"query": "ledelse"}))

    def test_flat_redispatch_shape(self):
        call = types.SimpleNamespace(name="manage_my_order", arguments={"confirm": True})
        self.assertEqual(tool_call_parts(call), ("manage_my_order", {"confirm": True}))

    def test_missing_arguments_are_an_empty_dict(self):
        self.assertEqual(
            tool_call_parts(types.SimpleNamespace(name="get_user_profile", arguments=None)),
            ("get_user_profile", {}))

    def test_unparseable_arguments_raise(self):
        call = types.SimpleNamespace(name="x", arguments="{not json")
        with self.assertRaises(ValueError):
            tool_call_parts(call)

    def test_executor_reaches_the_tool_through_the_flat_shape(self):
        # Before the fix this raised AttributeError on .function and the confirm
        # route answered "Fejl ved bekraeftelse" for every held mutation.
        call = types.SimpleNamespace(name="manage_my_order", arguments={"confirm": True})
        payload = json.loads(execute_tool(call, username="tester", session_id="s"))
        self.assertEqual(payload["status"], "error")          # the TOOL's own validation
        self.assertIn("order_id", payload["message"])         # ...not an arg-shape crash


class HrArgumentShapes(unittest.TestCase):
    def test_employee_ids_from_every_shape(self):
        for value in ([12, 13], ["12", "13"], "12,13", "[12, 13]", "12 13"):
            self.assertEqual(hr_tools._as_id_list(value), [12, 13], value)

    def test_employee_ids_drop_junk_and_dedupe(self):
        self.assertEqual(hr_tools._as_id_list([12, "12", "abc", None, 13]), [12, 13])
        self.assertEqual(hr_tools._as_id_list(""), [])
        self.assertEqual(hr_tools._as_id_list(True), [])

    def test_cohort_dict_passes_through(self):
        self.assertEqual(hr_tools._as_cohort({"department": "Salg"}), {"department": "Salg"})

    def test_bare_department_string_becomes_a_selector(self):
        self.assertEqual(hr_tools._as_cohort("Salg"), {"department": "Salg"})

    def test_bare_role_string_becomes_a_role_selector(self):
        self.assertEqual(hr_tools._as_cohort("ledere"), {"role": "manager"})

    def test_json_string_cohort(self):
        self.assertEqual(hr_tools._as_cohort('{"role": "employee"}'), {"role": "employee"})

    def test_unusable_cohort_is_none_so_the_tool_still_refuses(self):
        self.assertIsNone(hr_tools._as_cohort(None))
        self.assertIsNone(hr_tools._as_cohort(7))


class CvCoursesBucket(unittest.TestCase):
    """A CV's AMU/kursus section had nowhere to land but skills."""

    def test_courses_are_extracted_into_their_own_bucket(self):
        out = cv_ingest._normalise_profile({
            "courses": [
                {"title": "Salgsledelse", "vendor": "AMU", "completed_date": "2024"},
                {"kursus": "Konflikthaandtering", "udbyder": "AMU", "aar": "2023"},
            ],
        })
        self.assertEqual(out["courses"][0],
                         {"title": "Salgsledelse", "vendor": "AMU", "completed_date": "2024"})
        self.assertEqual(out["courses"][1]["title"], "Konflikthaandtering")
        self.assertEqual(out["skills"], [])

    def test_danish_key_and_bare_strings(self):
        out = cv_ingest._normalise_profile({"kurser": ["Den svaere samtale", ""]})
        self.assertEqual([c["title"] for c in out["courses"]], ["Den svaere samtale"])

    def test_courses_key_always_present(self):
        self.assertEqual(cv_ingest._normalise_profile({})["courses"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
