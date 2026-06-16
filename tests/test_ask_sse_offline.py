"""
EV-01: Offline mocked-LLM end-to-end test of the /app1/ask SSE pipeline.

Boots the REAL app via run.create_app() under the SANDBOX no-MySQL env and
drives POST /app1/ask through Flask's test client, with every LLM/tool seam
mocked at the module boundary (mirrors tests/test_ai_runtime.py):

  * ai_runtime.OpenAI            -> scripted _FakeClient (Responses runtime),
  * ai_runtime.iter_completion_stream -> scripted, call-counting chunk stream,
  * app1.agent.execute_tool      -> canned catalog_search JSON payload,
  * app1.tools.resolve_products_for_ui -> canned raw products (real
    serialize_course_cards / render_multi_course_media run on top of them),
  * app1.memory_store.DB_PATH    -> temp SQLite so no repo/db pollution.

Asserted SSE contract (the most regression-prone ~500 lines in the repo):
  1. the literal <suggestions> tag never leaks into 'chunk' events and a
     separate {'type': 'suggestions'} event carries the parsed items,
  2. a scripted catalog_search tool result produces a 'tool_call' event and a
     'course_cards' event with the canonical handles,
  3. a 'meta' event with message_index arrives before 'data: [DONE]',
  4. a fabricated price in the streamed answer triggers the grounding
     disclaimer chunk (and a grounded answer does NOT),
  5. exactly ONE final-answer completion per tool turn — FINAL CONTRACT per
     RT-02 (AI_CAPTURE_FINAL=1): the already-generated answer is captured, not
     discarded + regenerated. Until RT-02 lands this test documents the bug.
  6. with AI_LIVE_TOOL_EVENTS, a tool_call phase:'start' frame precedes the
     finish frame — skipped until AG-02's wiring exists in app1/agent.py.

Fully offline + deterministic: no OPENAI_API_KEY, no MySQL, no network.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Make the project root importable when run from anywhere.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

# Safe offline env BEFORE importing the app factory (the SANDBOX recipe).
# setdefault so an explicit CI/pytest env prefix always wins.
_SAFE_ENV = {
    "SANDBOX": "1",
    "AI_WARMUP_ON_IMPORT": "0",
    "SCHEDULER_OPPORTUNISTIC": "0",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "none",
    "MYSQL_PASSWORD": "none",
    "MYSQL_DB": "none",
    "OPENAI_API_KEY": "sk-test",
}
for _k, _v in _SAFE_ENV.items():
    os.environ.setdefault(_k, _v)

import grounding  # noqa: E402

# AG-02 (live tool events) lands in a parallel work item; activate test 6
# automatically once its env gate shows up in the agent source.
with open(os.path.join(_REPO_ROOT, "app1", "agent.py"), encoding="utf-8") as _f:
    _LIVE_TOOL_EVENTS_WIRED = "AI_LIVE_TOOL_EVENTS" in _f.read()


# ── Scripted fakes (pattern from tests/test_ai_runtime.py:23-69) ────────────

class _ScriptedResponses:
    """client.responses stand-in: pops scripted outputs, records kwargs."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError(
                "responses.create called more times than scripted — the agent "
                "loop issued an unexpected extra completion"
            )
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _RefusingChatCompletions:
    """Any chat.completions call is unscripted in these tests -> hard failure."""

    def create(self, **kwargs):
        raise AssertionError(
            "Unexpected chat.completions.create — this offline test only "
            "scripts the Responses runtime + iter_completion_stream"
        )


def _make_fake_client_cls(fake_responses):
    class _FakeClient:
        # Class attribute so ai_runtime._openai_client's cache key
        # (id(OpenAI), id(OpenAI.responses), ...) changes per scripted run.
        responses = fake_responses

        def __init__(self, *args, **kwargs):
            self.responses = type(self).responses
            self.chat = type("Chat", (), {"completions": _RefusingChatCompletions()})()

    return _FakeClient


class _ScriptedStream:
    """ai_runtime.iter_completion_stream stand-in: scripted chunks + counter."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.calls = 0
        self.last_messages = None

    def __call__(self, messages, model=None, max_tokens=None, **kwargs):
        self.calls += 1
        self.last_messages = list(messages)
        chunks = list(self.chunks)

        def _gen():
            for piece in chunks:
                yield piece

        return _gen()


def _tool_call_response(resp_id, name, call_id, arguments):
    return type("Resp", (), {
        "id": resp_id,
        "output": [{
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }],
        "usage": {"input_tokens": 10, "output_tokens": 3},
    })()


def _final_response(resp_id, text):
    return type("Resp", (), {
        "id": resp_id,
        "output": [{"type": "message", "content": [{"text": text}]}],
        "usage": {"input_tokens": 12, "output_tokens": 5},
    })()


# ── Canned catalog data ──────────────────────────────────────────────────────

_RAW_PRODUCTS = [
    {
        "title": "Projektledelse Grundkursus",
        "handle": "projektledelse-grund",
        "vendor": "Kursushuset",
        "product_type": "Kursus",
        "body_html": "<p>Lær fundamentet i projektledelse på to dage.</p>",
        "variants": [
            {"price": "12500", "option1": "København", "option2": "12. august 2026", "inventory_quantity": 8},
            {"price": "12500", "option1": "Aarhus", "option2": "2. september 2026", "inventory_quantity": 3},
        ],
    },
    {
        "title": "Agil Projektledelse",
        "handle": "agil-projektledelse",
        "vendor": "Lærhuset",
        "product_type": "Kursus",
        "body_html": "<p>Scrum og agile metoder i praksis.</p>",
        "variants": [
            {"price": "8900", "option1": "Online", "option2": "20. august 2026", "inventory_quantity": 12},
        ],
    },
]

_TOOL_PAYLOAD = json.dumps({
    "status": "success",
    "count": 2,
    "results": [
        {
            "title": "Projektledelse Grundkursus",
            "handle": "projektledelse-grund",
            "price": "12500",
            "vendor": "Kursushuset",
            "locations": ["København", "Aarhus"],
            "summary": "Lær fundamentet i projektledelse på to dage.",
        },
        {
            "title": "Agil Projektledelse",
            "handle": "agil-projektledelse",
            "price": "8900",
            "vendor": "Lærhuset",
            "locations": ["Online"],
            "summary": "Scrum og agile metoder i praksis.",
        },
    ],
}, ensure_ascii=False)


class _FakeExecuteTool:
    """app1.agent.execute_tool stand-in: canned JSON per tool name."""

    def __init__(self, payload_by_name):
        self.payload_by_name = dict(payload_by_name)
        self.calls = []

    def __call__(self, tool_call, username=None, session_id=None):
        name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except (TypeError, ValueError):
            args = {}
        self.calls.append({"name": name, "arguments": args})
        payload = self.payload_by_name.get(name)
        if payload is None:
            return json.dumps({"status": "error", "message": f"uventet værktøj: {name}"})
        return payload


# ── Scripted answers ─────────────────────────────────────────────────────────

_SUGGESTIONS = ["Vis flere kurser", "Sammenlign de to billigste"]
_SUGGESTIONS_TAG = '<suggestions>["Vis flere kurser", "Sammenlign de to billigste"]</suggestions>'

# Grounded: no concrete price/date/title claims -> never trips the breaker.
_HAPPY_VISIBLE = "Jeg fandt to kurser der matcher din søgning — se kortene herunder."
_HAPPY_FULL = _HAPPY_VISIBLE + "\n\n" + _SUGGESTIONS_TAG
# Chunks deliberately split MID-TAG to exercise the partial-tag stream buffer.
_HAPPY_CHUNKS = [
    "Jeg fandt to kurser der matcher",
    " din søgning — se kortene herunder.",
    "\n\n<sugg",
    'estions>["Vis flere kurser", ',
    '"Sammenlign de to billigste"]</suggest',
    "ions>",
]
assert "".join(_HAPPY_CHUNKS) == _HAPPY_FULL

# Fabricated: 99.999 kr appears nowhere in the tool evidence -> must disclaim.
_FABRICATED_VISIBLE = "Begge kurser koster 99.999 kr og starter 1. februar 2027."
_FABRICATED_FULL = _FABRICATED_VISIBLE + "\n\n" + _SUGGESTIONS_TAG
_FABRICATED_CHUNKS = [_FABRICATED_VISIBLE, "\n\n", _SUGGESTIONS_TAG]


# ── SSE parsing helpers ──────────────────────────────────────────────────────

_DONE = {"type": "__done__"}


def _parse_sse(raw_text):
    """Parse the SSE byte stream into a list of event dicts (+ __done__)."""
    events = []
    for line in raw_text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            events.append(dict(_DONE))
            continue
        events.append(json.loads(payload))
    return events


def _of_type(events, type_):
    return [e for e in events if e.get("type") == type_]


def _chunk_text(events):
    return "".join(e.get("content", "") for e in _of_type(events, "chunk"))


# ── Module-level app boot (real create_app, once) ────────────────────────────

_APP = None
_TMPDIR = None
_ORIG_DB_PATH = None


def _reset_store_conn(memory_store):
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    memory_store._local.conn = None


def setUpModule():
    global _APP, _TMPDIR, _ORIG_DB_PATH
    from app1 import memory_store
    _TMPDIR = tempfile.TemporaryDirectory()
    _ORIG_DB_PATH = memory_store.DB_PATH
    _reset_store_conn(memory_store)
    memory_store.DB_PATH = os.path.join(_TMPDIR.name, "ai_memory_test.db")
    memory_store.init_db()

    from run import create_app
    _APP = create_app()
    _APP.config["TESTING"] = True
    # The hooks are guarded, but skipping them keeps the test fast + silent.
    for flag in (
        "_ai_subsystems_warmed",
        "_branding_schema_ensured",
        "_enterprise_tables_created",
        "_perf_indexes_ensured",
    ):
        setattr(_APP, flag, True)


def tearDownModule():
    from app1 import memory_store
    _reset_store_conn(memory_store)
    if _ORIG_DB_PATH:
        memory_store.DB_PATH = _ORIG_DB_PATH
    if _TMPDIR is not None:
        _TMPDIR.cleanup()


class AskSSEOfflineTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        import ai_runtime
        import app1.agent as agent_mod
        # Per-test isolation: fresh in-memory agent state + fresh OpenAI client
        # cache + empty tool cache so scripted runs can never bleed together.
        for cache in (
            agent_mod.CHAT_MEMORY,
            agent_mod.SHOWN_PRODUCTS,
            agent_mod.USER_PROFILES,
            agent_mod.CONVERSATION_STAGES,
            agent_mod.ANONYMOUS_PROFILES,
            agent_mod.REJECTED_SEARCHES,
        ):
            cache.clear()
        ai_runtime._OPENAI_CLIENT = None
        ai_runtime._OPENAI_CLIENT_KEY = None
        ai_runtime._TOOL_CACHE.clear()

    # ── Drive one scripted tool turn through POST /app1/ask ──
    def _drive_turn(self, query, *, final_text, stream_chunks, extra_env=None,
                    raw_products=_RAW_PRODUCTS):
        fake_responses = _ScriptedResponses([
            _tool_call_response("resp_1", "catalog_search", "call_1", {"query": "projektledelse"}),
            _final_response("resp_2", final_text),
        ])
        fake_stream = _ScriptedStream(stream_chunks)
        fake_execute = _FakeExecuteTool({"catalog_search": _TOOL_PAYLOAD})
        env = {
            "AI_RUNTIME": "responses",
            "AI_LLM_ROUTER": "0",       # keep intent routing regex-only/deterministic
            "AI_GROUNDING_RECALL": "0",  # disclaimer path, never the re-call path
            "OPENAI_API_KEY": "sk-test",
        }
        env.update(extra_env or {})

        with patch("ai_runtime.OpenAI", _make_fake_client_cls(fake_responses)), \
                patch("ai_runtime.iter_completion_stream", fake_stream), \
                patch("app1.agent.execute_tool", fake_execute), \
                patch("app1.tools.resolve_products_for_ui",
                      lambda **kwargs: list(raw_products)), \
                patch.dict(os.environ, env):
            client = _APP.test_client()
            resp = client.post("/app1/ask", json={"query": query})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.content_type)
            raw = resp.get_data(as_text=True)

        return _parse_sse(raw), fake_responses, fake_stream, fake_execute

    # 1+2+3: suggestions never leak, tool evidence becomes events, meta before [DONE].
    def test_sse_contract_happy_tool_turn(self):
        events, fake_responses, _, fake_execute = self._drive_turn(
            "Find kurser i projektledelse",
            final_text=_HAPPY_FULL,
            stream_chunks=_HAPPY_CHUNKS,
        )
        types = [e.get("type") for e in events]

        # Stream opens with the ping and terminates with [DONE].
        self.assertEqual(types[0], "ping", events)
        self.assertEqual(types[-1], "__done__", types)

        # The scripted catalog_search executed once and surfaced as a tool_call event.
        self.assertEqual([c["name"] for c in fake_execute.calls], ["catalog_search"])
        tool_events = [e for e in _of_type(events, "tool_call") if e.get("name") == "catalog_search"]
        self.assertTrue(tool_events, f"no catalog_search tool_call event in {types}")
        finish = [e for e in tool_events if e.get("phase", "finish") == "finish"]
        self.assertTrue(finish, tool_events)
        self.assertEqual(finish[-1]["status"], "success")
        self.assertEqual(finish[-1]["results_count"], 2)

        # course_cards carry the canonical handles from the canned products.
        cards_events = _of_type(events, "course_cards")
        self.assertTrue(cards_events, f"no course_cards event in {types}")
        handles = [c.get("handle") for c in cards_events[0]["items"]]
        self.assertEqual(handles, ["projektledelse-grund", "agil-projektledelse"])

        # The <suggestions> tag NEVER leaks into visible chunks…
        for chunk in _of_type(events, "chunk"):
            self.assertNotIn("<suggestions", chunk.get("content", ""), events)
        self.assertIn(_HAPPY_VISIBLE, _chunk_text(events))
        # …and arrives as its own structured event instead.
        suggestion_events = _of_type(events, "suggestions")
        self.assertTrue(suggestion_events, f"no suggestions event in {types}")
        self.assertEqual(suggestion_events[0]["items"], _SUGGESTIONS)

        # Grounded answer -> NO disclaimer chunk.
        self.assertNotIn(grounding.GROUNDING_DISCLAIMER_DA, _chunk_text(events))

        # meta carries the feedback index and precedes [DONE].
        meta_events = _of_type(events, "meta")
        self.assertTrue(meta_events, f"no meta event in {types}")
        self.assertEqual(meta_events[0]["message_index"], 1)
        self.assertLess(events.index(meta_events[0]), events.index(_DONE))

        # RT-01 contract: the post-tool completion request actually carries this
        # turn's tool output (function_call_output), so the final answer is
        # generated WITH the evidence — not from a sanitized empty transcript.
        self.assertGreaterEqual(len(fake_responses.calls), 2)
        continuation_input = fake_responses.calls[1].get("input") or []
        outputs = [i for i in continuation_input if isinstance(i, dict) and i.get("type") == "function_call_output"]
        self.assertTrue(outputs, continuation_input)
        self.assertIn("Projektledelse Grundkursus", outputs[0].get("output", ""))

    # 4: fabricated price -> grounding disclaimer appended after the answer.
    def test_fabricated_price_triggers_grounding_disclaimer(self):
        events, _, _, _ = self._drive_turn(
            "Find kurser i projektledelse",
            final_text=_FABRICATED_FULL,
            stream_chunks=_FABRICATED_CHUNKS,
        )
        text = _chunk_text(events)
        self.assertIn(_FABRICATED_VISIBLE, text)
        self.assertIn(grounding.GROUNDING_DISCLAIMER_DA, text)
        # The disclaimer is a trailing note — it must come AFTER the answer.
        self.assertLess(
            text.index(_FABRICATED_VISIBLE),
            text.index(grounding.GROUNDING_DISCLAIMER_DA),
        )
        # Still a complete turn: suggestions + meta + [DONE] all present.
        self.assertTrue(_of_type(events, "suggestions"))
        self.assertTrue(_of_type(events, "meta"))
        self.assertEqual(events[-1], _DONE)

    # 5: FINAL CONTRACT (RT-02 / AI_CAPTURE_FINAL=1): a tool turn issues exactly
    # ONE final-answer completion. The tool-deciding call is responses.create #1;
    # everything after it (extra responses.create calls + iter_completion_stream
    # re-generations) must total exactly 1. Pre-RT-02 the produced answer is
    # discarded and regenerated (2 final completions) — this test documents that
    # bug and goes green when RT-02 lands. Deliberately NOT weakened.
    def test_tool_turn_issues_exactly_one_final_completion(self):
        events, fake_responses, fake_stream, _ = self._drive_turn(
            "Find kurser i projektledelse",
            final_text=_HAPPY_FULL,
            stream_chunks=_HAPPY_CHUNKS,
            extra_env={"AI_CAPTURE_FINAL": "1"},
        )
        # Whichever path produced it, the user saw exactly one streamed answer.
        self.assertIn(_HAPPY_VISIBLE, _chunk_text(events))
        self.assertEqual(_of_type(events, "suggestions")[0]["items"], _SUGGESTIONS)

        final_completions = (len(fake_responses.calls) - 1) + fake_stream.calls
        self.assertEqual(
            final_completions,
            1,
            "RT-02 contract: after the tool-deciding completion, exactly one "
            "final-answer completion may be issued (capture the generated text; "
            f"don't discard + regenerate). responses.create={len(fake_responses.calls)}, "
            f"iter_completion_stream={fake_stream.calls}",
        )

    # 6: AG-02 live tool progress — start frame precedes finish frame.
    @unittest.skipUnless(
        _LIVE_TOOL_EVENTS_WIRED,
        "AG-02 (AI_LIVE_TOOL_EVENTS) not wired in app1/agent.py yet",
    )
    def test_live_tool_start_event_precedes_finish(self):
        events, _, _, _ = self._drive_turn(
            "Find kurser i projektledelse",
            final_text=_HAPPY_FULL,
            stream_chunks=_HAPPY_CHUNKS,
            extra_env={"AI_LIVE_TOOL_EVENTS": "1"},
        )
        tool_events = [e for e in _of_type(events, "tool_call") if e.get("name") == "catalog_search"]
        start_idx = [i for i, e in enumerate(tool_events) if e.get("phase") == "start"]
        finish_idx = [i for i, e in enumerate(tool_events) if e.get("phase", "finish") == "finish"]
        self.assertTrue(start_idx, f"no phase:'start' tool_call frame in {tool_events}")
        self.assertTrue(finish_idx, f"no phase:'finish' tool_call frame in {tool_events}")
        self.assertLess(start_idx[0], finish_idx[-1], tool_events)
        self.assertEqual(tool_events[start_idx[0]]["status"], "running")


class ConfirmStoreUnitTests(unittest.TestCase):
    """Unit-tests for app1.confirm_store (pure in-memory, no Flask needed)."""

    def setUp(self):
        from app1 import confirm_store
        confirm_store.clear_all()

    def test_store_and_pop_round_trip(self):
        from app1 import confirm_store
        token = confirm_store.store_pending("sess-1", "employee", "manage_my_order", {"order_id": "o1"})
        self.assertTrue(token)
        entry = confirm_store.pop_pending("sess-1", token)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["tool_name"], "manage_my_order")
        self.assertEqual(entry["scope"], "employee")
        self.assertEqual(entry["args"]["order_id"], "o1")

    def test_second_pop_returns_none(self):
        from app1 import confirm_store
        token = confirm_store.store_pending("sess-1", "employee", "manage_my_order", {})
        confirm_store.pop_pending("sess-1", token)
        self.assertIsNone(confirm_store.pop_pending("sess-1", token))

    def test_wrong_session_is_rejected(self):
        from app1 import confirm_store
        token = confirm_store.store_pending("sess-A", "hr", "send_company_email", {})
        self.assertIsNone(confirm_store.pop_pending("sess-B", token))
        # Original session can still consume it
        self.assertIsNotNone(confirm_store.pop_pending("sess-A", token))

    def test_unknown_token_returns_none(self):
        from app1 import confirm_store
        self.assertIsNone(confirm_store.pop_pending("sess-1", "nonexistent-token"))


class ConfirmCardSSETests(unittest.TestCase):
    """Phase 8: a side-effect tool response emits a confirm_card SSE event."""

    _CONFIRM_TOOL_PAYLOAD = json.dumps({
        "needs_confirmation": True,
        "action": "manage_my_order",
        "message_da": "Annuller bestillingen Python Basis?",
        "details": "Bestillingen Python Basis vil blive annulleret.",
    }, ensure_ascii=False)

    def setUp(self):
        import ai_runtime
        import app1.agent as agent_mod
        from app1 import confirm_store
        for cache in (
            agent_mod.CHAT_MEMORY,
            agent_mod.SHOWN_PRODUCTS,
            agent_mod.USER_PROFILES,
            agent_mod.CONVERSATION_STAGES,
            agent_mod.ANONYMOUS_PROFILES,
            agent_mod.REJECTED_SEARCHES,
        ):
            cache.clear()
        ai_runtime._OPENAI_CLIENT = None
        ai_runtime._OPENAI_CLIENT_KEY = None
        ai_runtime._TOOL_CACHE.clear()
        confirm_store.clear_all()

    def _drive_side_effect_turn(self):
        """Drive a turn where manage_my_order returns needs_confirmation."""
        fake_responses = _ScriptedResponses([
            _tool_call_response("resp_1", "manage_my_order", "call_1", {"order_id": "o1"}),
            _final_response("resp_2", "Din bestilling afventer bekræftelse."),
        ])
        fake_stream = _ScriptedStream(["Din bestilling afventer bekræftelse."])
        fake_execute = _FakeExecuteTool({"manage_my_order": self._CONFIRM_TOOL_PAYLOAD})
        env = {
            "AI_RUNTIME": "responses",
            "AI_LLM_ROUTER": "0",
            "AI_GROUNDING_RECALL": "0",
            "OPENAI_API_KEY": "sk-test",
        }
        with patch("ai_runtime.OpenAI", _make_fake_client_cls(fake_responses)), \
                patch("ai_runtime.iter_completion_stream", fake_stream), \
                patch("app1.agent.execute_tool", fake_execute), \
                patch("app1.tools.resolve_products_for_ui", lambda **kwargs: []), \
                patch.dict(os.environ, env):
            client = _APP.test_client()
            with client.session_transaction() as sess:
                sess["user"] = "eva"
            resp = client.post("/app1/ask", json={"query": "annullér min bestilling"})
            self.assertEqual(resp.status_code, 200)
            raw = resp.get_data(as_text=True)
            session_cookie = client.get_cookie("session")
        return _parse_sse(raw), client, session_cookie

    def test_confirm_card_event_emitted(self):
        """A needs_confirmation tool result produces exactly one confirm_card event."""
        events, _, _ = self._drive_side_effect_turn()
        cards = _of_type(events, "confirm_card")
        self.assertEqual(len(cards), 1, f"expected 1 confirm_card, got {cards}")
        card = cards[0]
        self.assertEqual(card["action"], "manage_my_order")
        self.assertIn("Annuller", card["summary_da"])
        self.assertTrue(card["token"], "token must be non-empty")

    def test_confirm_route_redispatches_with_confirm_true(self):
        """POST /app1/confirm_tool_action re-executes the tool with confirm=True."""
        events, client, _ = self._drive_side_effect_turn()
        card = _of_type(events, "confirm_card")[0]
        token = card["token"]

        confirmed_payload = json.dumps({"status": "success", "message_da": "Annulleret"})

        with patch("app1.tools.execute_tool") as mock_exec, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            mock_exec.return_value = confirmed_payload
            resp2 = client.post(
                "/app1/confirm_tool_action",
                json={"token": token},
                content_type="application/json",
            )

        self.assertEqual(resp2.status_code, 200)
        body = resp2.get_json()
        self.assertIsNotNone(body, resp2.get_data(as_text=True))
        self.assertEqual(body.get("status"), "success")

        # Verify confirm=True was injected into args
        self.assertTrue(mock_exec.called)
        tc = mock_exec.call_args[0][0]  # positional tool_call arg
        self.assertTrue(tc.arguments.get("confirm"), "confirm=True must be set")
        self.assertEqual(tc.name, "manage_my_order")

    def test_double_confirm_is_idempotent(self):
        """Second POST with same token returns already_confirmed, not an error."""
        events, client, _ = self._drive_side_effect_turn()
        token = _of_type(events, "confirm_card")[0]["token"]

        confirmed_payload = json.dumps({"status": "success"})
        with patch("app1.tools.execute_tool", return_value=confirmed_payload), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            client.post("/app1/confirm_tool_action",
                        json={"token": token}, content_type="application/json")
            resp2 = client.post("/app1/confirm_tool_action",
                                json={"token": token}, content_type="application/json")

        body2 = resp2.get_json()
        self.assertEqual(body2.get("status"), "already_confirmed")


if __name__ == "__main__":
    unittest.main()
