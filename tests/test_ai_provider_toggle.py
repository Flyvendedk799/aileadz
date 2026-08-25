"""Tests for the OpenAI <-> Claude provider toggle.

No live API: the Anthropic client is replaced with a fake that records the exact
kwargs sent, which is how the API-contract assertions (no temperature, system
split out, tool_result blocks merged) are made.

Coverage:
  (a) the toggle is inert by default — every accessor returns its legacy value,
  (b) the message/tool/usage converters produce a well-formed Anthropic request,
  (c) the Claude agent loop honours the same guardrails as run_chat_agent,
  (d) dispatch + the Claude->OpenAI fallback pick the right models,
  (e) subsystems pinned to OpenAI stay on OpenAI when the toggle flips.
"""
import json
import os
import unittest
from unittest.mock import patch

import ai_provider
import ai_provider_anthropic as apa
import ai_runtime
from ai_tool_registry import anthropic_tool_choice, to_anthropic_tool


# --- fakes ---------------------------------------------------------------------

class _FakeHeaders(dict):
    pass


class _FakeStatusError(Exception):
    """Shaped like anthropic.APIStatusError: .body + .response.headers."""

    def __init__(self, message, body, headers=None):
        super().__init__(message)
        self.status_code = 429
        self.body = body
        self.response = type("R", (), {"headers": _FakeHeaders(headers or {})})()


def _spend_cap_error():
    return _FakeStatusError(
        "Error code: 429 - you have reached your API usage limits",
        {"type": "error", "error": {
            "type": "rate_limit_error",
            "message": ("You have reached your API usage limits: your organization "
                        "has crossed its monthly API usage threshold."),
            "details": {"error_code": "enforced_spend_limit_reached"},
        }},
    )


def _rate_limit_error():
    return _FakeStatusError(
        "Error code: 429 - rate limit",
        {"type": "error", "error": {
            "type": "rate_limit_error",
            "message": "This request would exceed your organization's output tokens per minute rate limit.",
        }},
        headers={"retry-after": "27"},
    )


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _text_block(text):
    return _Block(type="text", text=text)


def _tool_block(call_id, name, payload):
    return _Block(type="tool_use", id=call_id, name=name, input=payload)


def _usage(input_tokens=10, output_tokens=5, cache_read=0, cache_write=0):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


def _response(content, *, stop_reason="end_turn", usage=None, resp_id="msg_1"):
    return _Block(
        id=resp_id,
        content=list(content),
        stop_reason=stop_reason,
        usage=usage if usage is not None else _usage(),
    )


class _FakeMessages:
    """Records every create() kwargs; returns queued responses (or raises them)."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeAnthropic:
    def __init__(self, outputs):
        self.messages = _FakeMessages(outputs)

    def with_options(self, **_kwargs):
        return self


def _tool(name="catalog_search", *, strict=True, closed=True):
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    if closed:
        schema["additionalProperties"] = False
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Search the catalog",
            "parameters": schema,
            "strict": strict,
        },
    }


class _ProviderTestCase(unittest.TestCase):
    """Resets process-global AI state so tests never leak into each other.

    ai_runtime keeps the 429 cooldown in a module global; a rate-limit test that
    left it set would force every later test in the run onto the fast model.
    """

    def setUp(self):
        ai_provider.invalidate_settings_cache()
        ai_provider._SNAPSHOT = {}
        ai_runtime._RATE_LIMIT_COOLDOWN_UNTIL = 0.0
        self.addCleanup(ai_provider.invalidate_settings_cache)
        self.addCleanup(setattr, ai_runtime, "_RATE_LIMIT_COOLDOWN_UNTIL", 0.0)


def _as_anthropic(env=None):
    """patch.dict context switching the toggle to Claude."""
    base = {"AI_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "test-key"}
    base.update(env or {})
    return patch.dict(os.environ, base)


# --- (a) inert by default ------------------------------------------------------

class DefaultsAreLegacyTests(_ProviderTestCase):
    def test_default_provider_is_openai(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_PROVIDER", None)
            self.assertEqual(ai_provider.provider(), "openai")
            self.assertFalse(ai_provider.uses_anthropic())

    def test_default_models_match_legacy_env_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ("AI_PROVIDER", "AI_MAIN_MODEL", "AI_FAST_MODEL"):
                os.environ.pop(key, None)
            self.assertEqual(ai_runtime.main_model(), "gpt-4o")
            self.assertEqual(ai_runtime.fast_model(), "gpt-4o-mini")

    def test_unknown_provider_falls_back_to_openai(self):
        with patch.dict(os.environ, {"AI_PROVIDER": "bogus"}):
            self.assertEqual(ai_provider.provider(), "openai")

    def test_toggle_switches_the_tier_accessors(self):
        with _as_anthropic():
            self.assertTrue(ai_provider.uses_anthropic())
            self.assertEqual(ai_runtime.main_model(), "claude-opus-5")
            self.assertEqual(ai_runtime.fast_model(), "claude-haiku-4-5")

    def test_only_managed_keys_are_writable(self):
        # A secret must never be persistable through the settings API.
        self.assertNotIn("ANTHROPIC_API_KEY", ai_provider.MANAGED_KEYS)
        self.assertNotIn("OPENAI_API_KEY", ai_provider.MANAGED_KEYS)
        self.assertFalse(ai_provider.set_setting(None, "ANTHROPIC_API_KEY", "leak"))


# --- (e) pinned subsystems -----------------------------------------------------

class PinnedSubsystemTests(_ProviderTestCase):
    def test_openai_accessors_ignore_the_toggle(self):
        """RAG rerank / CV extraction call the OpenAI SDK directly and must not
        be handed a Claude model id when the conversational toggle flips."""
        with _as_anthropic():
            self.assertEqual(ai_provider.openai_main_model(), "gpt-4o")
            self.assertEqual(ai_provider.openai_fast_model(), "gpt-4o-mini")

    def test_openai_key_is_required_in_every_configuration(self):
        with _as_anthropic({"OPENAI_API_KEY": ""}):
            readiness = ai_provider.provider_readiness()
            self.assertFalse(readiness["ready"])
            self.assertTrue(any("OPENAI_API_KEY" in p for p in readiness["problems"]))


# --- (b) converters ------------------------------------------------------------

class ToolConversionTests(unittest.TestCase):
    def test_tool_uses_input_schema_and_flat_shape(self):
        converted = to_anthropic_tool(_tool())
        self.assertEqual(converted["name"], "catalog_search")
        self.assertIn("input_schema", converted)
        self.assertNotIn("parameters", converted)
        self.assertNotIn("function", converted)
        # `strict` is never forwarded to Anthropic — see
        # test_strict_is_never_forwarded_to_anthropic for the five reasons.
        self.assertNotIn("strict", converted)

    def test_strict_is_dropped_on_an_open_schema(self):
        converted = to_anthropic_tool(_tool(strict=True, closed=False))
        self.assertNotIn("strict", converted)

    def test_tool_choice_mapping(self):
        self.assertEqual(anthropic_tool_choice("auto"), {"type": "auto"})
        self.assertEqual(anthropic_tool_choice(None), {"type": "auto"})
        self.assertEqual(anthropic_tool_choice("required"), {"type": "any"})
        self.assertEqual(
            anthropic_tool_choice({"name": "catalog_search"}),
            {"type": "tool", "name": "catalog_search"},
        )


class MessageConversionTests(unittest.TestCase):
    def test_system_layers_split_out_with_one_cache_breakpoint(self):
        system, rest = apa.split_system([
            {"role": "system", "content": "STATIC PROMPT"},
            {"role": "system", "content": "[SESSION KONTEKST] volatile"},
            {"role": "user", "content": "hej"},
        ])
        self.assertEqual(len(system), 2)
        self.assertEqual(system[0]["text"], "STATIC PROMPT")
        # Only the byte-stable prefix carries the breakpoint.
        self.assertEqual(system[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", system[1])
        self.assertEqual([m["role"] for m in rest], ["user"])

    def test_tool_results_merge_into_one_user_message(self):
        converted = apa.to_anthropic_messages([
            {"role": "user", "content": "find kurser"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "a", "arguments": json.dumps({"q": "x"})}},
                {"id": "c2", "type": "function",
                 "function": {"name": "b", "arguments": json.dumps({"q": "y"})}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "a", "content": "res1"},
            {"role": "tool", "tool_call_id": "c2", "name": "b", "content": "res2"},
        ])
        self.assertEqual([m["role"] for m in converted], ["user", "assistant", "user"])
        results = converted[2]["content"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(b["type"] == "tool_result" for b in results))
        self.assertEqual([b["tool_use_id"] for b in results], ["c1", "c2"])

    def test_tool_use_input_is_an_object_not_a_json_string(self):
        converted = apa.to_anthropic_messages([
            {"role": "user", "content": "hej"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "a", "arguments": json.dumps({"q": "x"})}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "a", "content": "res"},
        ])
        block = converted[1]["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["input"], {"q": "x"})

    def test_empty_tool_output_becomes_a_non_empty_block(self):
        converted = apa.to_anthropic_messages([
            {"role": "user", "content": "hej"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "a", "content": ""},
        ])
        self.assertTrue(converted[2]["content"][0]["content"])

    def test_orphaned_tool_result_is_dropped(self):
        converted = apa.to_anthropic_messages([
            {"role": "user", "content": "hej"},
            {"role": "tool", "tool_call_id": "ghost", "name": "a", "content": "res"},
        ])
        blocks = [b for m in converted if isinstance(m["content"], list) for b in m["content"]]
        self.assertFalse([b for b in blocks if b.get("type") == "tool_result"])

    def test_unanswered_tool_use_is_dropped(self):
        converted = apa.to_anthropic_messages([
            {"role": "user", "content": "hej"},
            {"role": "assistant", "content": "tekst", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            ]},
        ])
        blocks = [b for m in converted if isinstance(m["content"], list) for b in m["content"]]
        self.assertFalse([b for b in blocks if b.get("type") == "tool_use"])
        self.assertTrue([b for b in blocks if b.get("type") == "text"])

    def test_conversation_always_opens_on_a_user_turn(self):
        converted = apa.to_anthropic_messages([
            {"role": "assistant", "content": "jeg starter"},
            {"role": "user", "content": "hej"},
        ])
        self.assertEqual(converted[0]["role"], "user")


class UsageNormalisationTests(unittest.TestCase):
    def test_cache_tokens_map_onto_the_shared_shape(self):
        usage = apa.normalize_usage(_usage(input_tokens=100, output_tokens=20,
                                           cache_read=300, cache_write=50))
        # Anthropic reports cache tokens OUTSIDE input_tokens; we fold them in so
        # a Claude row and an OpenAI row in ai_agent_runs mean the same thing.
        self.assertEqual(usage["input_tokens"], 450)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["cached_tokens"], 300)
        self.assertEqual(usage["input_tokens_details"]["cached_tokens"], 300)
        # The accessor ai_runtime uses for telemetry must find it.
        self.assertEqual(ai_runtime._cached_tokens_from_usage(usage), 300)

    def test_empty_usage_is_safe(self):
        self.assertEqual(apa.normalize_usage(None), {})


# --- API contract --------------------------------------------------------------

class RequestContractTests(_ProviderTestCase):
    def _kwargs(self, **over):
        params = dict(
            model="claude-opus-5",
            prepared=[
                {"role": "system", "content": "S"},
                {"role": "user", "content": "hej"},
            ],
            tools=None,
            tool_choice="auto",
            max_tokens=None,
        )
        params.update(over)
        return apa._build_kwargs(**params)

    def test_openai_nullable_encoding_is_undone_for_anthropic(self):
        """Regression: Anthropic 400s on `enum` + `type: [T, "null"]`.

            tools.0.custom: Invalid schema: Enum value 'beginner' does not
            match declared type '['string', 'null']'

        OpenAI strict mode needs every property in `required`, so an optional
        enum is encoded as a nullable union. Anthropic has no such rule, so the
        union must be unwound and the property moved back out of `required` —
        otherwise every tool-carrying turn fails and falls back to OpenAI.
        """
        from ai_tool_registry import _normalize_chat_tool

        raw = {"type": "function", "function": {
            "name": "catalog_search",
            "description": "Search the catalog.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "difficulty": {"type": "string",
                                   "enum": ["beginner", "intermediate", "advanced"]},
                },
                "required": [],
            },
        }}
        normalized = _normalize_chat_tool(raw)
        # The OpenAI path keeps the nullable encoding it needs...
        openai_prop = normalized["function"]["parameters"]["properties"]["difficulty"]
        self.assertEqual(openai_prop["type"], ["string", "null"])
        self.assertIn(None, openai_prop["enum"])

        # ...and the Anthropic conversion undoes exactly that.
        schema = to_anthropic_tool(normalized)["input_schema"]
        prop = schema["properties"]["difficulty"]
        self.assertEqual(prop["type"], "string")
        self.assertNotIn(None, prop["enum"])
        self.assertEqual(prop["enum"], ["beginner", "intermediate", "advanced"])
        self.assertNotIn("difficulty", schema["required"])
        # Still a closed schema, so `strict` stays forwardable.
        self.assertIs(schema["additionalProperties"], False)

    def test_strict_is_never_forwarded_to_anthropic(self):
        """Anthropic's strict validator enforces a JSON-Schema subset this
        toolset violates five ways, two of them unsatisfiable:

            Too many strict tools (31). The maximum ... is 20.
            Schemas contains too many optional parameters (50) ... limit: 24.

        Each is a 400 on EVERY tool-carrying turn, which the provider fallback
        then hides as OpenAI traffic. The OpenAI path keeps strict; Anthropic
        does not get it.
        """
        from ai_tool_registry import _normalize_chat_tool

        raw = {"type": "function", "function": {
            "name": "compare_courses", "description": "Compare 2-4 courses.",
            "parameters": {"type": "object", "properties": {
                "handles": {"type": "array", "items": {"type": "string"},
                            "description": "List of 2-4 product handles.",
                            "minItems": 2, "maxItems": 4}},
                "required": ["handles"]},
        }}
        normalized = _normalize_chat_tool(raw)
        self.assertIs(normalized["function"]["strict"], True)   # OpenAI keeps it

        tool = to_anthropic_tool(normalized)
        self.assertNotIn("strict", tool)
        # The schema itself is untouched — constraints still steer the model.
        handles = tool["input_schema"]["properties"]["handles"]
        self.assertEqual(handles["minItems"], 2)
        self.assertEqual(handles["maxItems"], 4)
        self.assertIn("handles", tool["input_schema"]["required"])

    def test_required_is_truthful_for_anthropic(self):
        """OpenAI strict mode marks EVERY property required. Forwarding that to
        Claude would advertise optional filters as mandatory."""
        from ai_tool_registry import _normalize_chat_tool

        raw = {"type": "function", "function": {
            "name": "catalog_search", "description": "Search.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "difficulty": {"type": "string", "enum": ["beginner", "advanced"]},
                "limit": {"type": "integer"}},
                "required": ["query"]},
        }}
        normalized = _normalize_chat_tool(raw)
        self.assertEqual(sorted(normalized["function"]["parameters"]["required"]),
                         ["difficulty", "limit", "query"])
        self.assertEqual(to_anthropic_tool(normalized)["input_schema"]["required"],
                         ["query"])

    def test_nested_nullable_enums_are_unwound_too(self):
        from ai_tool_registry import _normalize_chat_tool

        raw = {"type": "function", "function": {
            "name": "bulk", "description": "d",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"kind": {"type": "string", "enum": ["a", "b"]}},
                        "required": [],
                    }},
                },
                "required": [],
            },
        }}
        schema = to_anthropic_tool(_normalize_chat_tool(raw))["input_schema"]
        kind = schema["properties"]["items"]["items"]["properties"]["kind"]
        self.assertEqual(kind["type"], "string")
        self.assertNotIn(None, kind["enum"])

    def test_sampling_params_are_never_sent(self):
        """temperature/top_p/top_k are removed on current Claude models (400)."""
        kwargs = self._kwargs(tools=[_tool()])
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, kwargs)

    def test_volatile_layer_moves_next_to_the_answer_turn(self):
        """Per-turn steering must reach the model where it is attending.

        consolidate_system_layers parks the volatile "[SESSION KONTEKST]" layer
        at system[1] to protect the cached prefix, which also puts it before the
        entire conversation. On a model that supports mid-conversation system
        messages it is delivered as a trailing role:"system" entry instead —
        next to the turn it shapes, cached prefix still byte-identical.
        """
        kwargs = apa._build_kwargs(
            model="claude-opus-5",
            prepared=[
                {"role": "system", "content": "STATIC"},
                {"role": "system", "content": "[SESSION KONTEKST]\nspoerg om erfaring"},
                {"role": "user", "content": "hej"},
            ],
            tools=None, tool_choice="auto", max_tokens=None,
        )
        # Cached prefix keeps ONLY the static block.
        self.assertEqual([b["text"] for b in kwargs["system"]], ["STATIC"])
        self.assertEqual(kwargs["system"][0]["cache_control"], {"type": "ephemeral"})
        # Volatile layer is the last thing before the model answers.
        self.assertEqual(kwargs["messages"][-1]["role"], "system")
        self.assertIn("spoerg om erfaring", kwargs["messages"][-1]["content"])

    def test_volatile_layer_stays_in_system_on_models_without_support(self):
        """Sonnet 5 and the Haiku fast tier 400 on role:"system" in messages."""
        for model in ("claude-haiku-4-5", "claude-sonnet-5"):
            kwargs = apa._build_kwargs(
                model=model,
                prepared=[
                    {"role": "system", "content": "STATIC"},
                    {"role": "system", "content": "VOLATILE"},
                    {"role": "user", "content": "hej"},
                ],
                tools=None, tool_choice="auto", max_tokens=None,
            )
            self.assertEqual([b["text"] for b in kwargs["system"]], ["STATIC", "VOLATILE"], model)
            self.assertFalse([m for m in kwargs["messages"] if m["role"] == "system"], model)

    def test_no_trailing_system_message_after_an_assistant_turn(self):
        """A mid-conversation system message must follow a user-role turn."""
        kwargs = apa._build_kwargs(
            model="claude-opus-5",
            prepared=[
                {"role": "system", "content": "STATIC"},
                {"role": "system", "content": "VOLATILE"},
                {"role": "user", "content": "hej"},
                {"role": "assistant", "content": "hej selv"},
            ],
            tools=None, tool_choice="auto", max_tokens=None,
        )
        self.assertEqual(kwargs["messages"][-1]["role"], "assistant")
        self.assertEqual(len(kwargs["system"]), 2)

    def test_system_is_top_level_not_a_message(self):
        kwargs = self._kwargs()
        self.assertEqual(kwargs["system"][0]["text"], "S")
        self.assertFalse([m for m in kwargs["messages"] if m["role"] == "system"])

    def test_max_tokens_floor_protects_the_tool_turn(self):
        # The OpenAI tool-turn cap would be spent on thinking tokens alone.
        tight = ai_runtime.max_output_tokens_for_turn(True)
        self.assertLess(tight, apa._min_max_tokens())
        self.assertGreaterEqual(self._kwargs(tools=[_tool()])["max_tokens"],
                                apa._min_max_tokens())

    def test_answer_turn_gets_a_higher_floor_than_the_tool_turn(self):
        """Thinking over long tool output must not eat the whole answer budget:
        the tool-turn floor truncates a grounded answer mid-sentence."""
        self.assertGreater(apa._answer_max_tokens(), apa._min_max_tokens())
        # A tool-less request can only be an answer turn.
        self.assertGreaterEqual(self._kwargs()["max_tokens"], apa._answer_max_tokens())
        # ...and so is a tool-carrying turn the agent loop marks as one (RT-02).
        self.assertGreaterEqual(
            self._kwargs(tools=[_tool()], max_tokens=ai_runtime.max_output_tokens(),
                         answer_turn=True)["max_tokens"],
            apa._answer_max_tokens(),
        )

    def test_tool_blocks_are_flattened_when_no_tools_are_declared(self):
        """Anthropic 400s on tool blocks without a `tools` definition, and the
        deferred final-answer stream is exactly that: full agent history, no
        tools. The tool OUTPUT must survive as text so the answer can use it."""
        history = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "find kurser"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "catalog_search", "arguments": json.dumps({"q": "python"})}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "catalog_search",
             "content": '{"results": ["Kursus A"]}'},
        ]
        kwargs = self._kwargs(prepared=history, tools=None)
        blocks = [b for m in kwargs["messages"] if isinstance(m["content"], list)
                  for b in m["content"]]
        self.assertFalse(blocks, "no structured tool blocks may survive")
        joined = "\n".join(m["content"] for m in kwargs["messages"])
        self.assertIn("Kursus A", joined)          # tool output preserved
        self.assertIn("catalog_search", joined)    # which tool ran preserved

    def test_tool_blocks_are_kept_when_tools_are_declared(self):
        history = [
            {"role": "user", "content": "find kurser"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "catalog_search", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "catalog_search", "content": "res"},
        ]
        kwargs = self._kwargs(prepared=history, tools=[_tool()])
        blocks = [b.get("type") for m in kwargs["messages"]
                  if isinstance(m["content"], list) for b in m["content"]]
        self.assertIn("tool_use", blocks)
        self.assertIn("tool_result", blocks)

    def test_effort_is_low_on_tool_turns_and_absent_on_haiku(self):
        with patch.dict(os.environ, {"ANTHROPIC_EFFORT": ""}):
            self.assertEqual(self._kwargs(tools=[_tool()])["output_config"],
                             {"effort": "low"})
            self.assertEqual(self._kwargs()["output_config"], {"effort": "high"})
            self.assertNotIn("output_config", self._kwargs(model="claude-haiku-4-5"))


# --- (c) agent loop ------------------------------------------------------------

class AnthropicAgentLoopTests(_ProviderTestCase):
    def _run(self, outputs, **over):
        fake = _FakeAnthropic(outputs)
        params = dict(
            messages=[
                {"role": "system", "content": "S"},
                {"role": "user", "content": "find kurser"},
            ],
            tools=[_tool()],
            tool_executor=lambda *a, **k: json.dumps({"status": "success", "results": [1]}),
            username="u",
            session_id="s",
            max_iterations=3,
            defer_final_stream=False,
        )
        params.update(over)
        with _as_anthropic(), patch.object(apa, "client", return_value=fake):
            return apa.run_anthropic_agent(**params), fake

    def test_tool_call_then_final_answer(self):
        result, fake = self._run([
            _response([_tool_block("c1", "catalog_search", {"q": "python"})],
                      stop_reason="tool_use"),
            _response([_text_block("Her er tre kurser.")]),
        ])
        self.assertEqual(result.text, "Her er tre kurser.")
        self.assertEqual(result.runtime, "anthropic")
        self.assertEqual(len(result.tool_results), 1)
        self.assertEqual(result.tool_results[0].name, "catalog_search")
        # Second request carries the tool_result back.
        second = fake.messages.calls[1]["messages"]
        blocks = [b for m in second if isinstance(m["content"], list) for b in m["content"]]
        self.assertTrue([b for b in blocks if b.get("type") == "tool_result"])

    def test_history_stays_chat_shaped_for_the_rest_of_the_app(self):
        result, _ = self._run([
            _response([_tool_block("c1", "catalog_search", {"q": "python"})],
                      stop_reason="tool_use"),
            _response([_text_block("svar")]),
        ])
        assistant = [m for m in result.messages if m.get("role") == "assistant"][0]
        self.assertEqual(assistant["tool_calls"][0]["type"], "function")
        self.assertIsInstance(assistant["tool_calls"][0]["function"]["arguments"], str)
        self.assertTrue([m for m in result.messages if m.get("role") == "tool"])

    def test_repeated_identical_call_trips_the_circuit_breaker(self):
        result, _ = self._run([
            _response([_tool_block("c1", "catalog_search", {"q": "x"})], stop_reason="tool_use"),
            _response([_tool_block("c2", "catalog_search", {"q": "x"})], stop_reason="tool_use"),
            _response([_text_block("opsummering")]),
        ])
        self.assertEqual(result.runtime_path, "anthropic-forced-final")
        self.assertEqual(result.text, "opsummering")

    def test_refusal_returns_a_user_facing_message(self):
        result, _ = self._run([_response([], stop_reason="refusal")])
        self.assertEqual(result.runtime_path, "anthropic-refusal")
        self.assertTrue(result.text)

    def test_deferred_stream_captures_the_final_answer(self):
        result, _ = self._run(
            [_response([_text_block("færdigt svar")])],
            defer_final_stream=True,
        )
        self.assertFalse(result.needs_final_stream)
        self.assertEqual(result.text, "færdigt svar")

    def test_truncated_answer_is_not_captured(self):
        result, _ = self._run(
            [_response([_text_block("halvt sva")], stop_reason="max_tokens")],
            defer_final_stream=True,
        )
        self.assertTrue(result.needs_final_stream)
        self.assertEqual(result.text, "")

    def test_truncated_answer_is_regenerated_not_served_half(self):
        """Without deferral there is no caller to re-stream, so the loop must
        regenerate through the forced-final path instead of serving the
        sentence the model stopped inside."""
        result, _ = self._run([
            _response([_text_block("halvt sva")], stop_reason="max_tokens"),
            _response([_text_block("helt svar")]),
        ])
        self.assertEqual(result.runtime_path, "anthropic-forced-final")
        self.assertEqual(result.text, "helt svar")

    def test_malformed_request_is_classified_as_our_bug_not_an_outage(self):
        """A 400 invalid_request_error must not look like "Claude is down".

        It is the schema bug that silently rerouted every tool-carrying turn to
        OpenAI: the toggle said Anthropic, the bill said OpenAI, and nothing in
        the logs or telemetry distinguished the two.
        """
        bad = _FakeStatusError(
            "Error code: 400 - invalid schema",
            {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": ("tools.0.custom: Invalid schema: Enum value 'beginner' "
                            "does not match declared type '['string', 'null']'"),
            }, "request_id": "req_011CePkNh56g4weMGvS92Crs"},
        )
        bad.status_code = 400
        self.assertTrue(apa.is_permanent_request_error(bad))
        self.assertEqual(apa.request_id(bad), "req_011CePkNh56g4weMGvS92Crs")

    def test_transient_failures_are_not_flagged_as_our_bug(self):
        for exc in (_rate_limit_error(), _spend_cap_error(), RuntimeError("connection reset")):
            self.assertFalse(apa.is_permanent_request_error(exc), repr(exc))
        overloaded = _FakeStatusError("Error code: 529", {"type": "error", "error": {"type": "overloaded_error"}})
        overloaded.status_code = 529
        self.assertFalse(apa.is_permanent_request_error(overloaded))

    def test_spend_cap_is_not_retried_as_a_rate_limit(self):
        """The monthly spend cap is a 429 with the same rate_limit_error type,
        but it is not transient: retrying burns RPM and the user is told to
        shorten a question that was never the problem."""
        capped = _spend_cap_error()
        self.assertTrue(apa.is_spend_limit(capped))
        self.assertFalse(apa._is_rate_limit(capped))
        self.assertIn("forbrugsgrænse", ai_runtime.user_facing_error_message(capped))

    def test_a_real_rate_limit_is_still_retried(self):
        plain = _rate_limit_error()
        self.assertFalse(apa.is_spend_limit(plain))
        self.assertTrue(apa._is_rate_limit(plain))
        self.assertEqual(apa.retry_after_seconds(plain), 27.0)

    def test_rate_limit_retries_on_the_fast_model(self):
        # Backoff is patched out: this asserts the downgrade path, not sleeping.
        with patch.object(ai_runtime, "_backoff_wait_seconds", return_value=0):
            result, fake = self._run([
                RuntimeError("Error code: 429 - rate limit"),
                RuntimeError("Error code: 429 - rate limit"),
                _response([_text_block("ok")]),
            ], tools=None)
        self.assertEqual(result.text, "ok")
        self.assertEqual(fake.messages.calls[-1]["model"], "claude-haiku-4-5")


# --- (d) dispatch + fallback ---------------------------------------------------

class DispatchTests(_ProviderTestCase):
    def test_run_agent_with_fallback_routes_to_claude(self):
        fake = _FakeAnthropic([_response([_text_block("claude svarer")])])
        with _as_anthropic(), patch.object(apa, "client", return_value=fake):
            result = ai_runtime.run_agent_with_fallback(
                messages=[{"role": "system", "content": "S"},
                          {"role": "user", "content": "hej"}],
                tools=[],
                tool_executor=lambda *a, **k: "{}",
                username="u",
                session_id="s",
                defer_final_stream=False,
            )
        self.assertEqual(result.runtime, "anthropic")
        self.assertEqual(result.text, "claude svarer")

    def test_claude_failure_falls_back_to_openai_with_an_openai_model(self):
        captured = {}

        def _fake_chat_agent(**kwargs):
            captured.update(kwargs)
            return ai_runtime.AgentRunResult(text="openai svarer", messages=[], runtime="chat")

        with _as_anthropic(), \
                patch.object(apa, "client", side_effect=RuntimeError("no key")), \
                patch.object(ai_runtime, "run_chat_agent", _fake_chat_agent):
            result = ai_runtime.run_agent_with_fallback(
                messages=[{"role": "user", "content": "hej"}],
                tools=[],
                tool_executor=lambda *a, **k: "{}",
                username="u",
                session_id="s",
                # choose_turn_model() would have handed us a Claude id here.
                model="claude-opus-5",
            )
        self.assertEqual(result.runtime_path, "anthropic-openai-fallback")
        self.assertTrue(result.fallback_reason)
        # The OpenAI client must never be handed a Claude model id.
        self.assertEqual(captured["model"], "gpt-4o")

    def test_direct_completion_and_stream_route_to_claude(self):
        fake = _FakeAnthropic([_response([_text_block("kort svar")])])
        with _as_anthropic(), patch.object(apa, "client", return_value=fake):
            self.assertEqual(
                ai_runtime.run_direct_completion([{"role": "user", "content": "hej"}]),
                "kort svar",
            )

    def test_final_answer_stream_sends_no_tool_blocks(self):
        """Regression guard for the deferred-stream path in hr_agent /
        vendor_portal, which streams runtime_result.stream_messages verbatim."""
        history = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "find kurser"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "catalog_search", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "catalog_search",
             "content": '{"results": ["Kursus A"]}'},
        ]

        class _FakeStream:
            text_stream = ["Her ", "er ", "svaret."]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        captured = {}

        class _StreamingMessages:
            def stream(self, **kwargs):
                captured.update(kwargs)
                return _FakeStream()

        fake = _Block(messages=_StreamingMessages())
        with _as_anthropic(), patch.object(apa, "client", return_value=fake):
            text = "".join(ai_runtime.iter_completion_stream(history))

        self.assertEqual(text, "Her er svaret.")
        self.assertNotIn("tools", captured)
        blocks = [b for m in captured["messages"] if isinstance(m["content"], list)
                  for b in m["content"]]
        self.assertFalse(blocks)
        self.assertIn("Kursus A", "\n".join(m["content"] for m in captured["messages"]))

    def test_forced_final_sends_no_tool_blocks(self):
        """The circuit-breaker summary is also tool-less and history-carrying."""
        fake = _FakeAnthropic([
            _response([_tool_block("c1", "catalog_search", {"q": "x"})], stop_reason="tool_use"),
            _response([_tool_block("c2", "catalog_search", {"q": "x"})], stop_reason="tool_use"),
            _response([_text_block("opsummering")]),
        ])
        with _as_anthropic(), patch.object(apa, "client", return_value=fake):
            result = apa.run_anthropic_agent(
                messages=[{"role": "system", "content": "S"},
                          {"role": "user", "content": "find kurser"}],
                tools=[_tool()],
                tool_executor=lambda *a, **k: json.dumps({"results": [1]}),
                username="u",
                session_id="s",
                max_iterations=3,
                defer_final_stream=False,
            )
        self.assertEqual(result.text, "opsummering")
        forced = fake.messages.calls[-1]
        self.assertNotIn("tools", forced)
        blocks = [b for m in forced["messages"] if isinstance(m["content"], list)
                  for b in m["content"]]
        self.assertFalse(blocks, "forced-final must not carry tool blocks")

    def test_shadow_mode_serves_openai(self):
        with patch.dict(os.environ, {"AI_PROVIDER": "anthropic_shadow",
                                     "ANTHROPIC_API_KEY": "k",
                                     "AI_SHADOW_SAMPLE_RATE": "0"}):
            self.assertFalse(ai_provider.uses_anthropic())
            self.assertTrue(ai_provider.shadow_enabled())
            # Shadow keeps the request path on OpenAI models.
            self.assertEqual(ai_runtime.main_model(), "gpt-4o")


# --- cost model ----------------------------------------------------------------

class ClaudeCostTests(unittest.TestCase):
    def test_claude_models_are_priced(self):
        from ai_cost_model import estimate_cost, price_for

        for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
            self.assertIsNotNone(price_for(model), model)
        self.assertTrue(estimate_cost("claude-opus-5", 1_000_000, 0, 0)["known"])

    def test_anthropic_cache_reads_are_cheaper_than_openai_cached_input(self):
        from ai_cost_model import cached_input_discount

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_CACHED_INPUT_DISCOUNT", None)
            self.assertEqual(cached_input_discount("gpt-4o"), 0.50)
            self.assertEqual(cached_input_discount("claude-opus-5"), 0.10)
            # Zero-arg call keeps the historical OpenAI default.
            self.assertEqual(cached_input_discount(), 0.50)

    def test_run_cost_ceiling_recognises_claude_usage(self):
        usage = apa.normalize_usage(_usage(input_tokens=1_000_000, output_tokens=0))
        total, cost = ai_runtime._usage_run_cost(usage, model="claude-opus-5")
        self.assertEqual(total, 1_000_000)
        self.assertAlmostEqual(cost, 5.0, places=3)


if __name__ == "__main__":
    unittest.main()
