import unittest

import db_compat  # noqa: F401
from ai_tool_registry import (
    get_employee_tool_selection,
    get_hr_tool_selection,
    tool_name,
)


def _assert_strict_object(testcase, schema):
    if schema.get("type") == "object" or "properties" in schema:
        props = schema.get("properties", {})
        testcase.assertFalse(schema.get("additionalProperties", True))
        testcase.assertEqual(set(schema.get("required", [])), set(props.keys()))
        for prop in props.values():
            if isinstance(prop, dict):
                _assert_strict_object(testcase, prop)
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        _assert_strict_object(testcase, schema["items"])


# Tools that intentionally opt out of strict mode so the model can send
# flexible/empty/polymorphic payloads (a deliberate fix). New strict-False
# tools must be added here consciously, otherwise the schema check below fails.
NON_STRICT_EMPLOYEE_TOOLS = {
    "update_user_profile",
    "request_user_input",
    "set_learning_goal",
    "update_learning_goal",
}


class AIToolRegistryTests(unittest.TestCase):
    def test_employee_tool_schemas_are_strict(self):
        tools, meta = get_employee_tool_selection(
            logged_in=True,
            company_id=1,
            intent="profile_and_search",
            user_query="Jeg er projektleder og vil finde ledelseskurser under 5000 kr",
            shown_count=0,
        )
        self.assertTrue(meta["tool_names"])
        for tool in tools:
            # Every tool must be a well-formed function schema.
            self.assertEqual(tool.get("type"), "function")
            fn = tool["function"]
            name = fn.get("name")
            self.assertTrue(name, "tool is missing a function name")
            params = fn.get("parameters")
            self.assertIsInstance(params, dict)
            self.assertEqual(params.get("type"), "object")
            self.assertIsInstance(fn.get("strict"), bool)

            if name in NON_STRICT_EMPLOYEE_TOOLS:
                # Intentionally non-strict: schema is allowed to be flexible
                # (no additionalProperties:false / full required-set guarantee).
                self.assertFalse(
                    fn["strict"],
                    f"{name} is allowlisted as non-strict but reports strict=True",
                )
            else:
                # All other tools must remain fully strict.
                self.assertTrue(
                    fn["strict"],
                    f"{name} unexpectedly reports strict=False; allowlist it if intentional",
                )
                _assert_strict_object(self, params)

    def test_employee_router_selects_platform_specific_tools(self):
        tools, meta = get_employee_tool_selection(
            logged_in=True,
            company_id=1,
            intent="detail",
            user_query='[VEDHÆFTET KURSUS: "PRINCE2" (handle: prince2-foundation)]\nFortæl mere',
            shown_count=0,
        )
        names = {tool_name(tool) for tool in tools}
        self.assertIn("catalog_get_product", names)
        self.assertEqual(meta["forced_tool"], "catalog_get_product")

    def test_mutating_order_tool_requires_explicit_confirmation(self):
        tools, _ = get_employee_tool_selection(
            logged_in=True,
            company_id=1,
            intent="buying",
            user_query="Jeg vil gerne tilmeldes det kursus",
            shown_count=1,
        )
        names = {tool_name(tool) for tool in tools}
        self.assertIn("prepare_course_order", names)
        self.assertNotIn("create_course_order", names)

        confirmed, _ = get_employee_tool_selection(
            logged_in=True,
            company_id=1,
            intent="buying",
            user_query="Bekræft, opret ordre og tilmeld mig",
            shown_count=1,
        )
        self.assertIn("create_course_order", {tool_name(tool) for tool in confirmed})

    def test_company_tools_are_not_selected_without_company(self):
        tools, _ = get_employee_tool_selection(
            logged_in=True,
            company_id=None,
            intent="discovery",
            user_query="Har min afdeling budget til et kursus?",
            shown_count=0,
        )
        names = {tool_name(tool) for tool in tools}
        self.assertNotIn("get_department_budget", names)
        self.assertNotIn("check_order_approval_status", names)

    def test_chitchat_does_not_expose_tools(self):
        tools, meta = get_employee_tool_selection(
            logged_in=False,
            company_id=None,
            intent="chit_chat",
            user_query="Hej",
            shown_count=0,
        )
        self.assertEqual(tools, [])
        self.assertEqual(meta["tool_names"], [])

    def test_approval_status_does_not_expose_order_creation_tools(self):
        tools, meta = get_employee_tool_selection(
            logged_in=True,
            company_id=1,
            intent="buying",
            user_query="Er min ordre godkendt?",
            shown_count=1,
        )
        names = {tool_name(tool) for tool in tools}
        self.assertEqual(meta["forced_tool"], "check_order_approval_status")
        self.assertIn("check_order_approval_status", names)
        self.assertNotIn("create_course_order", names)
        self.assertNotIn("prepare_course_order", names)

    def test_hr_router_selects_futurematch_hr_tools(self):
        tools, meta = get_hr_tool_selection(
            company_id=1,
            user_query="Lav en træningsplan ud fra kompetencegab",
        )
        names = {tool_name(tool) for tool in tools}
        self.assertIn("hr_recommend_training_plan", names)
        self.assertIn("get_company_skill_gaps", names)
        self.assertEqual(meta["forced_tool"], "get_company_skill_gaps")


if __name__ == "__main__":
    unittest.main()
