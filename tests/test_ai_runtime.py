import json
import os
import unittest
from unittest.mock import patch

from ai_runtime import (
    ToolCallResult,
    build_tool_call_event,
    choose_turn_model,
    check_turn_token_budget,
    compact_messages_for_api,
    consolidate_system_layers,
    estimate_messages_tokens,
    fast_model,
    main_model,
    prepare_messages_for_turn,
    run_agent_with_fallback,
    run_responses_agent,
    _strip_heavy_tool_payload,
)


class _FakeResponses:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeChatCompletions:
    calls = []
    forced_once = False

    def create(self, **kwargs):
        self.__class__.calls.append(kwargs)
        if kwargs.get("tool_choice") != "auto" and not self.__class__.forced_once:
            self.__class__.forced_once = True
            tc = type("ToolCall", (), {
                "id": "chat_call_1",
                "function": type("Function", (), {"name": "catalog_get_product", "arguments": json.dumps({"handle": "x"})})(),
            })()
            message = type("Msg", (), {
                "content": "",
                "tool_calls": [tc],
                "model_dump": lambda self: {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "chat_call_1", "type": "function", "function": {"name": "catalog_get_product", "arguments": json.dumps({"handle": "x"})}}],
                },
            })()
        else:
            message = type("Msg", (), {"content": "fallback text", "tool_calls": None})()
        choice = type("Choice", (), {"message": message})()
        return type("ChatResp", (), {"choices": [choice], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})()


class _FakeClient:
    responses = None

    def __init__(self, *args, **kwargs):
        self.responses = self.__class__.responses
        self.chat = type("Chat", (), {"completions": _FakeChatCompletions()})()


class AIRuntimeTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("OPENAI_API_KEY", "sk-test")
        _FakeChatCompletions.calls = []
        _FakeChatCompletions.forced_once = False

    def test_responses_tool_loop_preserves_function_outputs(self):
        first = type("Resp", (), {
            "id": "resp_1",
            "output": [{
                "type": "function_call",
                "name": "catalog_get_product",
                "call_id": "call_1",
                "arguments": json.dumps({"handle": "course-a"}),
            }],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })()
        second = type("Resp", (), {
            "id": "resp_2",
            "output": [{"type": "message", "content": [{"text": "done"}]}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        })()
        fake_responses = _FakeResponses([first, second])
        _FakeClient.responses = fake_responses

        def executor(tool_call, username=None, session_id=None):
            self.assertEqual(tool_call.function.name, "catalog_get_product")
            return json.dumps({"status": "success", "count": 1})

        with patch("ai_runtime.OpenAI", _FakeClient):
            result = run_responses_agent(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "catalog_get_product",
                        "description": "Get product",
                        "parameters": {"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"], "additionalProperties": False},
                        "strict": True,
                    },
                }],
                tool_executor=executor,
                username="u",
                session_id="s",
            )

        self.assertEqual(result.text, "done")
        self.assertEqual(len(result.tool_results), 1)
        self.assertEqual(fake_responses.calls[1]["previous_response_id"], "resp_1")
        self.assertEqual(fake_responses.calls[1]["input"][0]["type"], "function_call_output")

    def test_chat_fallback_activates_when_responses_fails(self):
        _FakeClient.responses = _FakeResponses([RuntimeError("responses unavailable")])

        with patch("ai_runtime.OpenAI", _FakeClient), patch.dict(os.environ, {"AI_RUNTIME": "responses"}):
            result = run_agent_with_fallback(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "catalog_get_product",
                        "description": "Get product",
                        "parameters": {"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"], "additionalProperties": False},
                        "strict": True,
                    },
                }],
                tool_executor=lambda *a, **k: json.dumps({"status": "success"}),
                username=None,
                session_id="s",
                tool_choice={"name": "catalog_get_product"},
                defer_final_stream=False,
            )

        self.assertEqual(result.runtime, "chat")
        self.assertEqual(result.text, "fallback text")
        self.assertIn("responses unavailable", result.fallback_reason)
        self.assertNotEqual(_FakeChatCompletions.calls[0]["tool_choice"], "auto")
        self.assertEqual(_FakeChatCompletions.calls[1]["tool_choice"], "auto")

    def test_strip_heavy_tool_payload_removes_raw_products(self):
        payload = json.dumps({
            "status": "success",
            "results": [{"title": "Kursus A", "handle": "a"}],
            "raw_products": [{"title": "Huge", "body_html": "x" * 5000}],
        }, ensure_ascii=False)
        stripped = _strip_heavy_tool_payload(payload)
        data = json.loads(stripped)
        self.assertNotIn("raw_products", data)
        self.assertEqual(data["results"][0]["handle"], "a")

    def test_compact_messages_for_api_reduces_token_estimate(self):
        huge_tool = json.dumps({"raw_products": [{"body_html": "x" * 20000}]})
        messages = [{"role": "system", "content": "sys"}]
        for i in range(30):
            messages.append({"role": "user", "content": f"question {i} " + ("data " * 200)})
            messages.append({"role": "tool", "content": huge_tool, "tool_call_id": f"c{i}"})
        before = estimate_messages_tokens(messages)
        compact = compact_messages_for_api(messages, max_tokens=12000, aggressive=True)
        after = estimate_messages_tokens(compact)
        self.assertLess(after, before)
        self.assertLess(after, 12000)

    def test_consolidate_system_layers_merges_dynamic_context(self):
        messages = [
            {"role": "system", "content": "STATIC PROMPT"},
            {"role": "system", "content": "profil info"},
            {"role": "system", "content": "shown products"},
            {"role": "user", "content": "hej"},
        ]
        merged = consolidate_system_layers(messages)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["content"], "STATIC PROMPT")
        self.assertIn("SESSION KONTEKST", merged[1]["content"])
        self.assertIn("profil info", merged[1]["content"])

    def test_strip_heavy_tool_payload_removes_search_debug(self):
        payload = json.dumps({"status": "success", "search_debug": {"tokens": list(range(100))}, "results": []})
        stripped = json.loads(_strip_heavy_tool_payload(payload))
        self.assertNotIn("search_debug", stripped)

    def test_choose_turn_model_routes_simple_intents_to_fast_model(self):
        self.assertEqual(choose_turn_model(intent="chit_chat", tool_count=0, token_estimate=1000), fast_model())
        self.assertEqual(
            choose_turn_model(intent="comparison", tool_count=3, token_estimate=1000, prefer_quality=True),
            main_model(),
        )

    def test_tool_call_event_is_sanitized_and_display_ready(self):
        event = build_tool_call_event(
            ToolCallResult(
                call_id="call_1",
                name="catalog_search",
                arguments={"query": "person@example.com"},
                output=json.dumps({"status": "success", "count": 2, "results": [{"title": "A"}, {"title": "B"}]}),
                latency_ms=42,
                cache_hit=True,
            ),
            agent_scope="employee",
        )

        self.assertEqual(event["type"], "tool_call")
        self.assertEqual(event["name"], "catalog_search")
        self.assertEqual(event["label"], "Søg katalog")
        self.assertEqual(event["category"], "Katalog")
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["results_count"], 2)
        self.assertTrue(event["cache_hit"])
        self.assertNotIn("arguments", event)
        self.assertNotIn("output", event)

    def test_tool_lifecycle_callback_emits_start_and_finish(self):
        first = type("Resp", (), {
            "id": "resp_1",
            "output": [{
                "type": "function_call",
                "name": "catalog_get_product",
                "call_id": "call_1",
                "arguments": json.dumps({"handle": "course-a"}),
            }],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })()
        second = type("Resp", (), {
            "id": "resp_2",
            "output": [{"type": "message", "content": [{"text": "done"}]}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        })()
        _FakeClient.responses = _FakeResponses([first, second])
        events = []

        with patch("ai_runtime.OpenAI", _FakeClient):
            result = run_responses_agent(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "catalog_get_product",
                        "description": "Get product",
                        "parameters": {"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"], "additionalProperties": False},
                        "strict": True,
                    },
                }],
                tool_executor=lambda *a, **k: json.dumps({"status": "success", "product": {"handle": "course-a"}}),
                username="u",
                session_id="s",
                on_tool_event=events.append,
            )

        self.assertEqual(result.text, "done")
        self.assertEqual([event["phase"] for event in events], ["start", "finish"])
        self.assertEqual(events[0]["status"], "running")
        self.assertEqual(events[1]["label"], "Hent kursus")
        self.assertEqual(events[1]["results_count"], 1)

    def test_responses_defer_final_stream_when_requested(self):
        first = type("Resp", (), {
            "id": "resp_1",
            "output": [{"type": "message", "content": [{"text": "hello stream"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })()
        fake_responses = _FakeResponses([first])
        _FakeClient.responses = fake_responses

        with patch("ai_runtime.OpenAI", _FakeClient):
            result = run_responses_agent(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
                tools=[],
                tool_executor=lambda *a, **k: "{}",
                username=None,
                session_id="s",
                defer_final_stream=True,
            )

        self.assertTrue(result.needs_final_stream)
        self.assertEqual(result.text, "")

    def test_check_turn_token_budget_refuses_over_limit(self):
        with patch("ai_runtime.compaction_level_for_messages", return_value="over_budget"):
            allowed, message, level = check_turn_token_budget([{"role": "user", "content": "test"}])
        self.assertFalse(allowed)
        self.assertIn("ny chat", message.lower())
        self.assertEqual(level, "over_budget")


if __name__ == "__main__":
    unittest.main()
