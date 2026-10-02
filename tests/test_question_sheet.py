"""Several questions at once: the assistant shows a sheet (one field per question, answered in one
message) instead of a numbered list the person has to answer with "1: ... 2: ...".

The browser part (question-sheet.js) is exercised in jsdom by hand; these tests pin the server side
and the wiring so it cannot silently fall out.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app1.agent as agent  # noqa: E402
import app1.tools as tools  # noqa: E402
from ai_tool_registry import _EMPLOYEE_META, _TOOL_LABELS, get_employee_tool_selection  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _run(args):
    return json.loads(tools._execute_ask_user_questions(args, "eva"))


class _Call:
    def __init__(self, name, args):
        self.id = ""
        self.function = type("_Fn", (), {"name": name, "arguments": json.dumps(args)})()


class ExecutorTests(unittest.TestCase):
    def test_builds_a_questions_card(self):
        out = _run({"intro": "Så rammer jeg dit niveau.", "questions": [
            {"label": "Nuværende rolle", "question": "Hvad arbejder du som i dag?", "placeholder": "fx Teamleder"},
            {"label": "Retning", "question": "Hvor vil du hen?", "choices": ["Op i ledelse", "Specialist", "Op i ledelse"]},
        ]})
        self.assertEqual(out["status"], "ui_card")
        self.assertEqual(out["ui_type"], "questions")
        self.assertEqual(out["message"], "Så rammer jeg dit niveau.")
        self.assertEqual([f["label"] for f in out["fields"]], ["Nuværende rolle", "Retning"])
        self.assertEqual(out["fields"][1]["choices"], ["Op i ledelse", "Specialist"])   # de-duplicated
        self.assertTrue(out["note"])                                                      # tells the model not to repeat them

    def test_one_question_belongs_in_the_text(self):
        out = _run({"questions": [{"label": "A", "question": "Hvad?"}]})
        self.assertEqual(out["status"], "error")
        self.assertIn("ét spørgsmål", out["message_da"])

    def test_junk_is_dropped_and_sizes_are_clamped(self):
        out = _run({"questions": [
            {"label": "x" * 100, "question": "q" * 500, "choices": ["c" * 100] * 9},
            "not a dict", {"label": "tom"}, {"question": "Hvem?", "label": ""},
            {"question": "Hvor?"}, {"question": "Hvornår?"}, {"question": "Hvorfor?"},
        ]})
        self.assertEqual(out["status"], "ui_card")
        self.assertEqual(len(out["fields"]), 4)                         # at most four
        first = out["fields"][0]
        self.assertLessEqual(len(first["label"]), 40)
        self.assertLessEqual(len(first["question"]), 240)
        self.assertLessEqual(len(first["choices"]), 5)
        names = [f["name"] for f in out["fields"]]
        self.assertEqual(len(names), len(set(names)))                   # unique ids
        self.assertEqual(out["fields"][1]["label"], "Spørgsmål 2")      # blank label gets a number

    def test_questions_arriving_as_a_json_string_still_work(self):
        out = _run({"questions": json.dumps([{"label": "A", "question": "Hvad?"}, {"label": "B", "question": "Hvem?"}])})
        self.assertEqual(out["status"], "ui_card")

    def test_garbage_args_never_raise(self):
        for args in ({}, {"questions": None}, {"questions": "{"}, {"questions": 5}):
            self.assertEqual(_run(args)["status"], "error")

    def test_dispatch_and_anonymous_use(self):
        out = json.loads(tools.execute_tool(_Call("ask_user_questions", {"questions": [
            {"label": "A", "question": "Hvad?"}, {"label": "B", "question": "Hvem?"}]}), username=None))
        self.assertEqual(out["ui_type"], "questions")        # writes nothing, so visitors may use it


class RegistryTests(unittest.TestCase):
    def test_schema_meta_label_and_js_label(self):
        schema = next(t for t in tools.OPENAI_TOOLS if t["function"]["name"] == "ask_user_questions")
        props = schema["function"]["parameters"]["properties"]["questions"]
        self.assertEqual((props["minItems"], props["maxItems"]), (2, 4))
        self.assertIn("ask_user_questions", _EMPLOYEE_META)
        self.assertFalse(_EMPLOYEE_META["ask_user_questions"].side_effect)
        self.assertTrue(_TOOL_LABELS.get("ask_user_questions"))
        self.assertIn("ask_user_questions:", _read("static/futurematch/assets/chat.js"))

    def test_on_the_menu_for_everyone_in_every_mode(self):
        for kw in ({"logged_in": True, "mode": "assistant"}, {"logged_in": True, "mode": "default"},
                   {"logged_in": False, "mode": "default"}):
            _, meta = get_employee_tool_selection(company_id=None, intent="discovery",
                                                  user_query="Hjælp mig med at opbygge min profil fra bunden", **kw)
            self.assertIn("ask_user_questions", meta["tool_names"], kw)

    def test_agent_forwards_the_card_to_the_browser(self):
        src = _read("app1/agent.py")
        self.assertIn('elif fn in ("request_user_input", "ask_user_questions"):', src)


class PromptTests(unittest.TestCase):
    def test_assistant_asks_one_thing_at_a_time_and_never_lists_fields(self):
        text = agent.SYSTEM_PLAYBOOK_ASSISTANT
        self.assertIn("ÉT spørgsmål ad gangen", text)
        self.assertIn("aldrig en liste over profilfelter", text)
        self.assertIn('"1: ... 2: ..."', text)
        self.assertIn("ask_user_questions", text)
        self.assertIn("fra bunden", text)
        self.assertIn("open_cv_upload", text)

    def test_no_checklist_directives_crept_in(self):
        for banned in ("DIT NÆSTE SPØRGSMÅL", "SKAL VÆRE OM", "SPØRG ALDRIG", "ALLEREDE AFDÆKKET"):
            self.assertNotIn(banned, agent.SYSTEM_PLAYBOOK_ASSISTANT)


class FrontendWiringTests(unittest.TestCase):
    def test_chat_page_loads_the_sheet_before_chat_js_with_content_versioning(self):
        html = _read("templates/fm/chat.html")
        sheet = "?v={{ asset_version('futurematch/assets/question-sheet.js') }}"
        self.assertIn(sheet, html)
        self.assertLess(html.index("question-sheet.js"), html.index("futurematch/assets/chat.js"))

    def test_chat_js_renders_the_card_and_falls_back_for_prose_lists(self):
        js = _read("static/futurematch/assets/chat.js")
        self.assertIn('data.ui_type === "questions"', js)
        self.assertIn("window.FmQuestionSheet.render(", js)
        self.assertIn("window.FmQuestionSheet.fromList(", js)
        self.assertIn("questionsSeen", js)
        # the fallback must not double up on a turn that already showed the sheet
        self.assertRegex(js, r"if \(!questionsSeen && textEl && window\.FmQuestionSheet\)")

    def test_sheet_is_accessible_and_escapes_model_text(self):
        js = _read("static/futurematch/assets/question-sheet.js")
        for needle in ('class="qs-q" for="', 'aria-live="polite"', "aria-pressed", "esc(f.question)", "esc(f.label)",
                       "esc(spec.message", "esc(c)"):
            self.assertIn(needle, js)

    def test_one_composed_message_and_a_skip_path(self):
        js = _read("static/futurematch/assets/question-sheet.js")
        self.assertIn("Springer over: ", js)
        self.assertIn("Lad os springe de spørgsmål over lige nu.", js)
        css = _read("static/futurematch/assets/chat.css")
        self.assertIn(".pcard.qsheet", css)
        self.assertIn(".msg.user .bubble { white-space: pre-wrap; }", css)

    def test_ui_card_is_a_known_event_type(self):
        from app1 import sse_events
        self.assertIn("ui_card", sse_events.KNOWN_EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()
