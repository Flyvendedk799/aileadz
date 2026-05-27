import json
import os
import unittest
from unittest.mock import patch

from ai_runtime import run_agent_with_fallback, run_responses_agent


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
            )

        self.assertEqual(result.runtime, "chat")
        self.assertEqual(result.text, "fallback text")
        self.assertIn("responses unavailable", result.fallback_reason)
        self.assertNotEqual(_FakeChatCompletions.calls[0]["tool_choice"], "auto")
        self.assertEqual(_FakeChatCompletions.calls[1]["tool_choice"], "auto")


if __name__ == "__main__":
    unittest.main()
