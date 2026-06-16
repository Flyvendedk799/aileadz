import json
import os
import unittest
from unittest.mock import patch

from ai_runtime import (
    AgentRunResult,
    ToolCallResult,
    build_tool_call_event,
    choose_turn_model,
    check_turn_token_budget,
    compact_messages_for_api,
    consolidate_system_layers,
    estimate_messages_tokens,
    fast_model,
    iter_agent_with_live_tool_events,
    iter_buffered_text_chunks,
    main_model,
    max_output_tokens,
    max_output_tokens_for_turn,
    prepare_messages_for_turn,
    run_agent_with_fallback,
    run_chat_agent,
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


class LiveToolEventRunnerTests(unittest.TestCase):
    def test_shared_live_runner_streams_events_before_result(self):
        def fake_run_agent_with_fallback(on_tool_event=None, **kwargs):
            on_tool_event({"type": "tool_call", "id": "call_1", "name": "catalog_search", "phase": "start"})
            on_tool_event({"type": "tool_call", "id": "call_1", "name": "catalog_search", "phase": "finish"})
            return AgentRunResult(text="done", messages=[])

        with patch("ai_runtime.run_agent_with_fallback", side_effect=fake_run_agent_with_fallback):
            items = list(iter_agent_with_live_tool_events({}, timeout_seconds=2, thread_name="test-agent-live"))

        event_items = [payload for kind, payload in items if kind == "tool_event"]
        result_items = [payload for kind, payload in items if kind == "result"]
        self.assertEqual([e["phase"] for e in event_items], ["start", "finish"])
        self.assertEqual(result_items[0].text, "done")
        self.assertLess(
            items.index(("tool_event", event_items[0])),
            items.index(("result", result_items[0])),
        )

    def test_shared_live_runner_reraises_worker_errors(self):
        def fake_run_agent_with_fallback(on_tool_event=None, **kwargs):
            raise RuntimeError("boom")

        with patch("ai_runtime.run_agent_with_fallback", side_effect=fake_run_agent_with_fallback):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                list(iter_agent_with_live_tool_events({}, timeout_seconds=2, thread_name="test-agent-live"))


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

    def test_responses_stream_messages_preserve_tool_evidence(self):
        """RT-01: the deferred final stream must see this turn's tool results.

        The responses loop appends role:"tool" messages to source_messages; without
        a parent assistant tool_calls message, _sanitize_tool_sequence (via
        prepare_messages_for_turn) drops them as orphans and the streamed final
        answer is generated blind to the tool evidence.
        """
        tool_payload = json.dumps({
            "status": "success",
            "count": 1,
            "results": [{"title": "Kursus Evidens A", "handle": "kursus-evidens-a", "price": "12.500 kr"}],
        }, ensure_ascii=False)
        first = type("Resp", (), {
            "id": "resp_1",
            "output": [{
                "type": "function_call",
                "name": "catalog_search",
                "call_id": "call_ev_1",
                "arguments": json.dumps({"query": "evidens kursus"}),
            }],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })()
        second = type("Resp", (), {
            "id": "resp_2",
            "output": [{"type": "message", "content": [{"text": "endeligt svar"}]}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        })()
        _FakeClient.responses = _FakeResponses([first, second])

        with patch("ai_runtime.OpenAI", _FakeClient):
            result = run_responses_agent(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "find kursus"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "catalog_search",
                        "description": "Søg i kataloget",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
                        "strict": True,
                    },
                }],
                tool_executor=lambda *a, **k: tool_payload,
                username="u",
                session_id="s",
                defer_final_stream=True,
            )

        stream_messages = result.stream_messages or result.messages
        prepared = prepare_messages_for_turn(stream_messages)

        # Tool evidence survives sanitization: the tool output content is intact.
        tool_msgs = [m for m in prepared if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0].get("tool_call_id"), "call_ev_1")
        self.assertIn("Kursus Evidens A", tool_msgs[0].get("content") or "")
        self.assertIn("12.500 kr", tool_msgs[0].get("content") or "")

        # And a parent assistant message carries the matching tool_calls ids.
        assistant_call_ids = []
        for m in prepared:
            if m.get("role") == "assistant":
                for call in m.get("tool_calls") or []:
                    assistant_call_ids.append(call.get("id"))
                    self.assertEqual(call.get("type"), "function")
                    self.assertEqual((call.get("function") or {}).get("name"), "catalog_search")
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                    self.assertEqual(args, {"query": "evidens kursus"})
        self.assertEqual(assistant_call_ids, ["call_ev_1"])

    def test_messages_to_responses_input_emits_function_call_pairs(self):
        """A rebuilt Responses request must never carry orphaned function_call_output."""
        from ai_runtime import _messages_to_responses_input

        converted = _messages_to_responses_input([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hej"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "catalog_search", "arguments": json.dumps({"query": "x"})},
                }],
            },
            {"role": "tool", "tool_call_id": "call_x", "name": "catalog_search", "content": "{\"status\": \"success\"}"},
        ])

        types = [item.get("type") for item in converted]
        self.assertIn("function_call", types)
        self.assertIn("function_call_output", types)
        fc = next(item for item in converted if item.get("type") == "function_call")
        fco = next(item for item in converted if item.get("type") == "function_call_output")
        self.assertEqual(fc["call_id"], "call_x")
        self.assertEqual(fc["name"], "catalog_search")
        self.assertEqual(fco["call_id"], "call_x")
        # The empty assistant shell is not emitted as a contentless message.
        self.assertNotIn({"role": "assistant", "content": ""}, converted)

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

    def test_tooler2_event_fields_and_progress_builder(self):
        from ai_runtime import build_tool_start_event, build_tool_progress_event

        # side-effect tool start event carries the new confirm_required flag
        start = build_tool_start_event("create_course_order", "call_x", agent_scope="employee")
        self.assertIn("confirm_required", start)
        self.assertIn("progress_label", start)
        self.assertEqual(start["status"], "running")

        # partial_failure + redaction-safe error string surface on a failed call
        event = build_tool_call_event(
            ToolCallResult(
                call_id="call_e",
                name="catalog_search",
                arguments={"query": "x"},
                output=json.dumps({"status": "error", "message_da": "Kunne ikke hente data", "partial_failure": True}),
                latency_ms=5,
            ),
            agent_scope="employee",
        )
        self.assertEqual(event["status"], "error")
        self.assertTrue(event.get("partial_failure"))
        self.assertEqual(event.get("safe_error"), "Kunne ikke hente data")

        # progress event is browser-safe and clamps percent to 0..100
        prog = build_tool_progress_event("catalog_search", "call_p", percent=150, note="Analyserer…")
        self.assertEqual(prog["type"], "tool_progress")
        self.assertEqual(prog["percent"], 100)
        self.assertEqual(prog["message"], "Analyserer…")
        self.assertNotIn("output", prog)

    def test_tooler2_master_flag(self):
        from ai_runtime import ai_tooler2_enabled

        with patch.dict(os.environ, {"AI_TOOLER2": "off"}):
            self.assertFalse(ai_tooler2_enabled())
        with patch.dict(os.environ, {"AI_TOOLER2": "on"}):
            self.assertTrue(ai_tooler2_enabled())

    def test_tool_turn_temperature_env(self):
        from ai_runtime import tool_turn_temperature

        with patch.dict(os.environ, {"AI_TOOL_TURN_TEMPERATURE": "0.2"}):
            self.assertEqual(tool_turn_temperature(), 0.2)
        with patch.dict(os.environ, {"AI_TOOL_TURN_TEMPERATURE": ""}):
            self.assertIsNone(tool_turn_temperature())  # empty => API default
        with patch.dict(os.environ, {"AI_TOOL_TURN_TEMPERATURE": "9"}):
            self.assertEqual(tool_turn_temperature(), 2.0)  # clamped

    def test_tool_turn_sets_low_temperature(self):
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
        with patch.dict(os.environ, {"AI_TOOL_TURN_TEMPERATURE": "0.2"}):
            with patch("ai_runtime.OpenAI", _FakeClient):
                run_responses_agent(
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
                )
        # The tool-deciding turn must carry the low temperature.
        first_call = _FakeClient.responses.calls[0]
        self.assertIn("tools", first_call)
        self.assertEqual(first_call.get("temperature"), 0.2)

    def test_run_cost_uses_cost_model_when_model_known(self):
        from ai_runtime import _approx_cost_usd

        usage_in, usage_out = 100000, 50000
        # Known model resolves through ai_cost_model's per-model table.
        priced = _approx_cost_usd(usage_in, usage_out, model="gpt-4o")
        self.assertGreater(priced, 0.0)
        # Unknown model falls back to the legacy flat env rate (still monotonic).
        fallback = _approx_cost_usd(usage_in, usage_out, model="totally-unknown-model")
        self.assertGreater(fallback, 0.0)

    def test_token_divisor_env_configurable(self):
        from ai_runtime import _estimate_text_tokens

        text = "x" * 40
        with patch.dict(os.environ, {"AI_TOKEN_CHARS_PER_TOKEN": "4.0"}):
            self.assertEqual(_estimate_text_tokens(text), 10)
        with patch.dict(os.environ, {"AI_TOKEN_CHARS_PER_TOKEN": "2.0"}):
            self.assertEqual(_estimate_text_tokens(text), 20)

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

    def test_responses_defer_final_stream_captures_answer(self):
        """RT-02: the already-generated final answer is captured, not discarded.

        With AI_CAPTURE_FINAL on (default), a no-tool-calls iteration under
        defer_final_stream=True returns the produced text with
        needs_final_stream=False so the caller streams the buffered answer
        instead of paying a second completion.
        """
        first = type("Resp", (), {
            "id": "resp_1",
            "output": [{"type": "message", "content": [{"text": "hello stream"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })()
        fake_responses = _FakeResponses([first])
        _FakeClient.responses = fake_responses

        with patch("ai_runtime.OpenAI", _FakeClient), patch.dict(os.environ, {"AI_CAPTURE_FINAL": "1"}):
            result = run_responses_agent(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
                tools=[],
                tool_executor=lambda *a, **k: "{}",
                username=None,
                session_id="s",
                defer_final_stream=True,
            )

        self.assertFalse(result.needs_final_stream)
        self.assertEqual(result.text, "hello stream")
        # Exactly one completion was issued for the whole turn.
        self.assertEqual(len(fake_responses.calls), 1)
        self.assertTrue(result.stream_messages)

    def test_responses_capture_final_disabled_restores_discard_path(self):
        """AI_CAPTURE_FINAL=0 is the rollback: old discard+regenerate contract."""
        first = type("Resp", (), {
            "id": "resp_1",
            "output": [{"type": "message", "content": [{"text": "hello stream"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })()
        _FakeClient.responses = _FakeResponses([first])

        with patch("ai_runtime.OpenAI", _FakeClient), patch.dict(os.environ, {"AI_CAPTURE_FINAL": "0"}):
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

    def test_responses_capture_skipped_when_output_truncated(self):
        """A status='incomplete' (max_output_tokens) answer must not be captured."""
        first = type("Resp", (), {
            "id": "resp_1",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [{"text": "afkortet sv"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })()
        _FakeClient.responses = _FakeResponses([first])

        with patch("ai_runtime.OpenAI", _FakeClient), patch.dict(os.environ, {"AI_CAPTURE_FINAL": "1"}):
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

    def test_responses_output_cap_lifted_after_tool_execution(self):
        """RT-02: after >=1 executed tool the next iteration gets the full
        answer budget instead of the tight tool-turn cap, so the captured
        final answer is never truncated at the tool cap."""
        first = type("Resp", (), {
            "id": "resp_1",
            "output": [{
                "type": "function_call",
                "name": "catalog_get_product",
                "call_id": "call_1",
                # Distinct handle: the module-global tool cache keys on
                # (tool, args), so reusing another test's args would leak
                # its cached output into that test (and vice versa).
                "arguments": json.dumps({"handle": "course-cap-lift"}),
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

        with patch("ai_runtime.OpenAI", _FakeClient), patch.dict(os.environ, {"AI_CAPTURE_FINAL": "1"}):
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
                tool_executor=lambda *a, **k: json.dumps({"status": "success"}),
                username="u",
                session_id="s",
                defer_final_stream=True,
            )

        self.assertEqual(result.text, "done")
        self.assertFalse(result.needs_final_stream)
        # First iteration (tool-deciding) uses the tight tool-turn cap …
        self.assertEqual(fake_responses.calls[0]["max_output_tokens"], max_output_tokens_for_turn(True))
        # … the post-tool iteration is lifted to the full answer budget.
        self.assertEqual(fake_responses.calls[1]["max_output_tokens"], max_output_tokens())

    def test_chat_defer_final_stream_captures_answer(self):
        """RT-02 chat path: msg.content captured under defer_final_stream."""
        # The fake emits a tool call on the first non-"auto" create; skip that
        # so this run is a pure no-tool final-answer iteration.
        _FakeChatCompletions.forced_once = True
        with patch("ai_runtime.OpenAI", _FakeClient), patch.dict(os.environ, {"AI_CAPTURE_FINAL": "1"}):
            result = run_chat_agent(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
                tools=[],
                tool_executor=lambda *a, **k: "{}",
                username=None,
                session_id="s",
                tool_choice="auto",
                defer_final_stream=True,
            )

        self.assertFalse(result.needs_final_stream)
        self.assertEqual(result.text, "fallback text")
        self.assertEqual(len(_FakeChatCompletions.calls), 1)

    def test_chat_capture_skipped_on_length_finish_reason(self):
        """finish_reason='length' means truncation: fall back to regeneration."""

        class _TruncCompletions:
            def create(self, **kwargs):
                message = type("Msg", (), {"content": "afkortet svar", "tool_calls": None})()
                choice = type("Choice", (), {"message": message, "finish_reason": "length"})()
                return type("ChatResp", (), {"choices": [choice], "usage": {}})()

        class _TruncClient:
            def __init__(self, *args, **kwargs):
                self.chat = type("Chat", (), {"completions": _TruncCompletions()})()

        with patch("ai_runtime.OpenAI", _TruncClient), patch.dict(os.environ, {"AI_CAPTURE_FINAL": "1"}):
            result = run_chat_agent(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
                tools=[],
                tool_executor=lambda *a, **k: "{}",
                username=None,
                session_id="s",
                tool_choice="auto",
                defer_final_stream=True,
            )

        self.assertTrue(result.needs_final_stream)
        self.assertEqual(result.text, "")

    def test_iter_buffered_text_chunks_preserves_text_exactly(self):
        text = "Her er tre gode kurser til dit team — skal jeg sammenligne priser?\n\nSig til."
        chunks = list(iter_buffered_text_chunks(text))
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        # ~3 words per chunk (last chunk may be shorter).
        for chunk in chunks[:-1]:
            self.assertLessEqual(len(chunk.split()), 3)
        self.assertEqual(list(iter_buffered_text_chunks("")), [])

    def test_check_turn_token_budget_refuses_over_limit(self):
        with patch("ai_runtime.compaction_level_for_messages", return_value="over_budget"):
            allowed, message, level = check_turn_token_budget([{"role": "user", "content": "test"}])
        self.assertFalse(allowed)
        self.assertIn("ny chat", message.lower())
        self.assertEqual(level, "over_budget")

    def test_live_tool_events_env_gate(self):
        """AG-02 gate: default ON; AI_LIVE_TOOL_EVENTS=0/false/off is rollback."""
        from ai_runtime import live_tool_events_enabled

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_LIVE_TOOL_EVENTS", None)
            self.assertTrue(live_tool_events_enabled())
        for off_value in ("0", "false", "no", "off", "FALSE"):
            with patch.dict(os.environ, {"AI_LIVE_TOOL_EVENTS": off_value}):
                self.assertFalse(live_tool_events_enabled())
        with patch.dict(os.environ, {"AI_LIVE_TOOL_EVENTS": "1"}):
            self.assertTrue(live_tool_events_enabled())

    def test_tool_cache_is_lock_protected_under_concurrent_writes(self):
        """AG-02 prerequisite: _TOOL_CACHE is now also touched from the live-
        events worker thread; concurrent writes past _TOOL_CACHE_MAX exercise
        the eviction path (min() over a mutating dict raises RuntimeError
        without the lock)."""
        import threading
        import ai_runtime

        with ai_runtime._TOOL_CACHE_LOCK:
            ai_runtime._TOOL_CACHE.clear()
        errors = []

        def _hammer(worker_id):
            try:
                for i in range(300):
                    key = f"lock-smoke-{worker_id}-{i}"
                    ai_runtime._cache_tool_result(key, 60, "{}")
                    with ai_runtime._TOOL_CACHE_LOCK:
                        ai_runtime._TOOL_CACHE.get(key)
            except Exception as exc:  # pragma: no cover - kun ved race
                errors.append(exc)

        threads = [threading.Thread(target=_hammer, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        with ai_runtime._TOOL_CACHE_LOCK:
            size = len(ai_runtime._TOOL_CACHE)
            ai_runtime._TOOL_CACHE.clear()
        self.assertLessEqual(size, ai_runtime._TOOL_CACHE_MAX)

    def test_router_cache_is_lock_protected_under_concurrent_writes(self):
        """Same smoke for _ROUTER_CACHE via classify_intent_llm's bounded-cache
        write path (eviction + insert under _ROUTER_CACHE_LOCK)."""
        import threading
        import ai_runtime
        from ai_runtime import classify_intent_llm

        with ai_runtime._ROUTER_CACHE_LOCK:
            ai_runtime._ROUTER_CACHE.clear()
        errors = []

        def _hammer(worker_id):
            try:
                for i in range(150):
                    classify_intent_llm(f"unik forespørgsel {worker_id}-{i}", fallback="discovery")
            except Exception as exc:  # pragma: no cover - kun ved race
                errors.append(exc)

        with patch("ai_runtime.OpenAI", _FakeClient), patch.dict(os.environ, {"AI_LLM_ROUTER": "1"}):
            threads = [threading.Thread(target=_hammer, args=(t,)) for t in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        self.assertEqual(errors, [])
        with ai_runtime._ROUTER_CACHE_LOCK:
            size = len(ai_runtime._ROUTER_CACHE)
            ai_runtime._ROUTER_CACHE.clear()
        self.assertLessEqual(size, ai_runtime._ROUTER_CACHE_MAX)

    def test_company_scope_param_keys_tool_cache_without_request_context(self):
        """AG-02: when the agent loop runs in a worker thread (no Flask request
        context) the caller resolves the tenant scope in the request thread and
        threads it in via company_scope — the tool-cache key must carry it."""
        import ai_runtime
        from ai_runtime import _execute_tool_calls_parallel

        with ai_runtime._TOOL_CACHE_LOCK:
            ai_runtime._TOOL_CACHE.clear()

        results = _execute_tool_calls_parallel(
            calls=[{"id": "call_scope_1", "name": "catalog_get_product",
                    "arguments": {"handle": "scope-test"}}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "catalog_get_product",
                    "description": "Get product",
                    "parameters": {"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"], "additionalProperties": False},
                    "strict": True,
                },
            }],
            tool_executor=lambda *a, **k: json.dumps({"status": "success", "product": {"handle": "scope-test"}}),
            username="u",
            session_id="s",
            company_scope="tenant-42",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "ok")
        with ai_runtime._TOOL_CACHE_LOCK:
            keys = list(ai_runtime._TOOL_CACHE.keys())
            ai_runtime._TOOL_CACHE.clear()
        self.assertTrue(any("tenant-42" in key for key in keys), keys)

    def test_run_responses_agent_threads_company_scope_to_tool_cache(self):
        """company_scope flows run_agent_with_fallback -> run_responses_agent ->
        _execute_tool_calls_parallel intact (the end-to-end live-events ordering
        itself is asserted in tests/test_ask_sse_offline.py)."""
        import ai_runtime

        first = type("Resp", (), {
            "id": "resp_1",
            "output": [{
                "type": "function_call",
                "name": "catalog_get_product",
                "call_id": "call_scope_e2e",
                "arguments": json.dumps({"handle": "scope-e2e"}),
            }],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })()
        second = type("Resp", (), {
            "id": "resp_2",
            "output": [{"type": "message", "content": [{"text": "done"}]}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        })()
        _FakeClient.responses = _FakeResponses([first, second])

        with ai_runtime._TOOL_CACHE_LOCK:
            ai_runtime._TOOL_CACHE.clear()

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
                username="u",
                session_id="s",
                defer_final_stream=False,
                company_scope="tenant-77",
            )

        self.assertEqual(result.text, "done")
        with ai_runtime._TOOL_CACHE_LOCK:
            keys = list(ai_runtime._TOOL_CACHE.keys())
            ai_runtime._TOOL_CACHE.clear()
        self.assertTrue(any("tenant-77" in key for key in keys), keys)


if __name__ == "__main__":
    unittest.main()
