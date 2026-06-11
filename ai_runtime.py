"""Shared AI runtime for Futurematch employee and HR agents."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from openai import OpenAI

from db_compat import refresh_flask_mysql_connection
from ai_tool_registry import (
    chat_tool_choice,
    is_parallel_safe,
    responses_tool_choice,
    sanitize_args_for_tool,
    tool_cache_ttl,
    tool_display_metadata,
    to_responses_tool,
    tool_name,
)


PROMPT_VERSION = "futurematch-ai-v3"
_TOOL_CACHE: Dict[str, Tuple[float, str]] = {}
_TOOL_CACHE_MAX = 512
# AG-02: tool-cachen læses/skrives både fra request-tråden, fra ThreadPool-
# workers (parallel-sikre tools) og nu også fra live-event worker-tråden
# (AI_LIVE_TOOL_EVENTS) — al get/set/eviction sker derfor under denne lås.
_TOOL_CACHE_LOCK = threading.Lock()
_LOG_TABLES_READY = set()
_OPENAI_CLIENT: Optional[OpenAI] = None
_OPENAI_CLIENT_KEY = None
_RATE_LIMIT_COOLDOWN_UNTIL = 0.0


@dataclass
class ToolCallResult:
    call_id: str
    name: str
    arguments: Dict[str, Any]
    output: str
    latency_ms: int
    status: str = "ok"
    error: str = ""
    cache_hit: bool = False


@dataclass
class AgentRunResult:
    text: str
    messages: List[Dict[str, Any]]
    tool_results: List[ToolCallResult] = field(default_factory=list)
    tool_messages: List[Dict[str, Any]] = field(default_factory=list)
    response_id: str = ""
    runtime: str = "chat"
    fallback_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    raw_response: Any = None
    needs_final_stream: bool = False
    stream_messages: List[Dict[str, Any]] = field(default_factory=list)
    compaction_level: str = "normal"
    runtime_path: str = ""


ToolEventCallback = Callable[[Dict[str, Any]], None]


def _parse_tool_output(output: Any) -> Dict[str, Any]:
    if isinstance(output, dict):
        return output
    try:
        parsed = json.loads(output or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _tool_result_count(parsed: Dict[str, Any]) -> int:
    if not isinstance(parsed, dict):
        return 0
    scalar_keys = (
        "count",
        "total",
        "total_count",
        "results_count",
        "order_count",
        "orders_count",
        "employee_count",
        "course_count",
    )
    for key in scalar_keys:
        try:
            value = parsed.get(key)
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            pass
    list_keys = (
        "results",
        "items",
        "courses",
        "products",
        "top_courses",
        "categories",
        "comparables",
        "employees",
        "actions",
        "alerts",
        "gaps",
        "series",
        "orders",
    )
    for key in list_keys:
        value = parsed.get(key)
        if isinstance(value, list):
            return len(value)
    if isinstance(parsed.get("product"), dict):
        return 1
    return 0


def _has_empty_collection_result(parsed: Dict[str, Any]) -> bool:
    if not isinstance(parsed, dict):
        return False
    if parsed.get("count") == 0:
        return True
    for key in ("results", "items", "courses", "products", "comparables", "employees", "actions", "gaps"):
        value = parsed.get(key)
        if isinstance(value, list) and not value:
            return True
    return False


def _normalize_tool_event_status(result: ToolCallResult, parsed: Dict[str, Any], count: int) -> str:
    raw = str(parsed.get("status") or result.status or "success").strip().lower()
    if result.status == "error" or raw in {"error", "failed", "failure"} or parsed.get("error"):
        return "error"
    if raw in {"proposed", "ui_card", "memory_saved"}:
        return raw
    if raw == "already_exists":
        return "success"
    if _has_empty_collection_result(parsed) and count == 0:
        return "empty"
    return "success"


def _tool_event_message(status: str, cache_hit: bool) -> str:
    if status == "error":
        return "Værktøjet kunne ikke gennemføres."
    if status == "empty":
        return "Ingen resultater."
    if status == "proposed":
        return "Afventer bekræftelse."
    if status == "ui_card":
        return "Mangler input."
    if status == "memory_saved":
        return "Hukommelse gemt."
    if cache_hit:
        return "Brugte cache."
    return "Færdig."


def build_tool_call_event(
    result: ToolCallResult,
    *,
    agent_scope: str = "employee",
    phase: str = "finish",
) -> Dict[str, Any]:
    """Build the browser-safe SSE payload for a completed tool call.

    The event intentionally excludes raw arguments and raw tool output. Those can
    contain private user/profile/company data and belong only in redacted server
    telemetry.
    """
    parsed = _parse_tool_output(result.output)
    count = _tool_result_count(parsed)
    status = _normalize_tool_event_status(result, parsed, count)
    display = tool_display_metadata(result.name, agent_scope)
    cache_hit = bool(result.cache_hit or result.status == "cached")
    try:
        latency_ms = max(0, int(round(float(result.latency_ms or 0))))
    except (TypeError, ValueError):
        latency_ms = 0
    return {
        "type": "tool_call",
        "id": result.call_id or "",
        "agent": agent_scope or display.get("agent") or "employee",
        "name": result.name,
        "label": display.get("label") or result.name,
        "category": display.get("category") or "",
        "category_key": display.get("category_key") or "",
        "ui_icon": display.get("ui_icon") or "fa-wand-magic-sparkles",
        "phase": phase or "finish",
        "status": status,
        "results_count": count,
        "latency_ms": latency_ms,
        "cache_hit": cache_hit,
        "side_effect": bool(display.get("side_effect")),
        "message": _tool_event_message(status, cache_hit),
    }


def build_tool_start_event(name: str, call_id: str = "", *, agent_scope: str = "employee") -> Dict[str, Any]:
    display = tool_display_metadata(name, agent_scope)
    return {
        "type": "tool_call",
        "id": call_id or "",
        "agent": agent_scope or display.get("agent") or "employee",
        "name": name,
        "label": display.get("label") or name,
        "category": display.get("category") or "",
        "category_key": display.get("category_key") or "",
        "ui_icon": display.get("ui_icon") or "fa-wand-magic-sparkles",
        "phase": "start",
        "status": "running",
        "results_count": 0,
        "latency_ms": 0,
        "cache_hit": False,
        "side_effect": bool(display.get("side_effect")),
        "message": "Kører værktøj.",
    }


def _emit_tool_event(callback: Optional[ToolEventCallback], event: Dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        pass


def main_model() -> str:
    return os.getenv("AI_MAIN_MODEL", "gpt-4o")


def fast_model() -> str:
    return os.getenv("AI_FAST_MODEL", "gpt-4o-mini")


def runtime_mode() -> str:
    return os.getenv("AI_RUNTIME", "responses").lower().strip() or "responses"


# --- Cost-aware model routing --------------------------------------------------
# AI_MODEL_ROUTING picks how aggressively turns are pushed onto the cheap
# fast_model() (gpt-4o-mini is ~15-25x cheaper than gpt-4o):
#   * "quality"  -> legacy behaviour, identical to before this change (safe fallback).
#   * "balanced" -> NEW DEFAULT. Final answers of clearly-simple intents and all
#                   tool-deciding turns run on mini; only genuine multi-course
#                   SYNTHESIS intents stay on gpt-4o.
#   * "cost"     -> mini for everything except a tiny must-be-4o allowlist.
# Reverse the whole feature with: AI_MODEL_ROUTING=quality
_VALID_ROUTING_MODES = ("quality", "balanced", "cost")


def model_routing_mode() -> str:
    raw = (os.getenv("AI_MODEL_ROUTING", "balanced") or "balanced").lower().strip()
    return raw if raw in _VALID_ROUTING_MODES else "balanced"


# Intents whose FINAL answer is a simple lookup / restate and is safe on mini.
# Names cover BOTH the employee classifier (_classify_intent_local) and the HR
# classifier (_classify_hr_intent), plus the abstract names used in specs/tests.
_SIMPLE_INTENTS = frozenset({
    # employee agent
    "chit_chat", "needs_clarification", "follow_up", "detail",
    "discovery", "correction",
    # hr agent
    "general", "budget", "catalog",
    # abstract / spec aliases
    "greeting", "search", "status", "profile_get",
    # NOTE: profile MUTATION intents (profile_update / profile_add) are deliberately
    # NOT here — they must stay on the main model so the "show a confirm UI-card before
    # changing the user's profile" UX (request_user_input -> profile_confirm_request)
    # holds; the fast model tended to mutate the profile directly without confirming.
})

# Genuine multi-course SYNTHESIS intents that benefit from gpt-4o reasoning.
_SYNTHESIS_INTENTS = frozenset({
    # employee agent
    "comparison", "team_buying", "profile_and_search", "buying",
    # hr agent
    "skills",
    # abstract / spec aliases
    "compare", "learning_path", "skill_gap", "recommend_for_profile",
})

# In "cost" mode, the only intents allowed to keep gpt-4o.
_COST_MODE_QUALITY_ALLOWLIST = frozenset({
    "comparison", "compare", "learning_path", "skill_gap",
})

# Profile MUTATION intents: keep these on the main model in every cost mode so the
# "show a confirm UI-card before changing the user's profile" UX holds (the fast
# model tended to mutate the profile directly instead of proposing a confirm card).
_PROFILE_MUTATION_INTENTS = frozenset({"profile_update", "profile_add"})


def _route_tier(
    mode: str,
    intent: str,
    tool_count: int,
    token_estimate: int,
    prefer_quality: bool,
) -> str:
    """Pure, OpenAI-free routing decision -> returns "main" or "fast".

    Callers map "main" -> main_model() (gpt-4o) and "fast" -> fast_model()
    (gpt-4o-mini). Kept side-effect-free so it can be unit-tested with no
    network / no API key. The live wrapper choose_turn_model() layers the
    rate-limit cooldown short-circuit on top of this.
    """
    intent = (intent or "").strip()
    mode = mode if mode in _VALID_ROUTING_MODES else "balanced"

    # Guards that apply in EVERY mode (mirror legacy choose_turn_model).
    # prefer_quality always wins -> 4o (callers set this for high-value flows).
    if prefer_quality:
        return "main"
    # Huge prompts are cheaper and just as reliable on mini.
    if token_estimate > int(tpm_budget() * 0.75):
        return "fast"

    # Profile mutations keep the confirm-first UX on the main model in every mode.
    if intent in _PROFILE_MUTATION_INTENTS:
        return "main"

    if mode == "quality":
        # EXACTLY the historical behaviour (regression-free fallback).
        if tool_count == 0 and intent in {"chit_chat", "needs_clarification"}:
            return "fast"
        if intent in {"follow_up", "detail"} and token_estimate < 12000:
            return "fast"
        return "main"

    if mode == "cost":
        # Mini for everything except the small must-be-4o allowlist.
        return "main" if intent in _COST_MODE_QUALITY_ALLOWLIST else "fast"

    # mode == "balanced" (default)
    # Reserve 4o for genuine multi-course synthesis; everything else -> mini.
    if intent in _SYNTHESIS_INTENTS:
        return "main"
    if intent in _SIMPLE_INTENTS:
        return "fast"
    # Unknown intent: default to mini in balanced mode (cheap + reliable for
    # tool-deciding and short answers); prefer_quality above still rescues the
    # high-value flows that callers explicitly flag.
    return "fast"


def trace_sample_rate() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("AI_TRACE_SAMPLE_RATE", "1"))))
    except ValueError:
        return 1.0


def _openai_client() -> OpenAI:
    global _OPENAI_CLIENT, _OPENAI_CLIENT_KEY
    try:
        timeout = float(os.getenv("AI_OPENAI_TIMEOUT_SECONDS", "45"))
    except ValueError:
        timeout = 45.0
    api_key = os.getenv("OPENAI_API_KEY")
    cache_key = (id(OpenAI), id(getattr(OpenAI, "responses", None)), api_key, timeout)
    if _OPENAI_CLIENT is None or _OPENAI_CLIENT_KEY != cache_key:
        _OPENAI_CLIENT = OpenAI(api_key=api_key, timeout=timeout)
        _OPENAI_CLIENT_KEY = cache_key
    return _OPENAI_CLIENT


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


# --- PII redaction for persisted telemetry only --------------------------------
# IMPORTANT: this is applied ONLY to data written into ai_agent_runs / ai_tool_runs.
# The model always receives the original, un-redacted content; redaction here must
# never affect what is sent to OpenAI or what the agent answers.
import re as _re  # local alias so we never shadow a caller's `re`

_EMAIL_RE = _re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Danish CPR (DDMMYY-XXXX, optional separator). Checked before phone numbers so a
# 10-digit CPR isn't mistaken for a phone number.
_CPR_RE = _re.compile(r"\b(\d{6})[-\s]?(\d{4})\b")
# Danish phone numbers: optional +45 / 0045 prefix, then 8 digits (spaces allowed).
_DK_PHONE_RE = _re.compile(
    r"(?<!\d)(?:\+45[\s]?|0045[\s]?)?(?:\d[\s]?){8}(?!\d)"
)
# Long opaque tokens / secrets (API keys, bearer tokens, UUID-ish blobs).
_TOKEN_RE = _re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
# Obvious personal names: 2+ capitalised words in a row (e.g. "Anders Hansen").
# Conservative on purpose — only collapses runs of TitleCase words.
_NAME_RE = _re.compile(
    r"\b([A-ZÆØÅ][a-zæøå]+)(\s+[A-ZÆØÅ][a-zæøå]+){1,3}\b"
)
_REDACT_MAX_CHARS = 8000


def _redact_pii(text: Any) -> str:
    """Mask emails, Danish phone/CPR numbers, secret-like tokens and names.

    Best-effort and fully guarded: on any failure it returns a safe string so
    telemetry logging is never broken by redaction. Order matters — emails,
    CPR and tokens are masked before the looser phone/name heuristics so the
    most specific patterns win.
    """
    try:
        if text is None:
            return ""
        if not isinstance(text, str):
            text = _safe_json(text)
        if not text:
            return text
        if len(text) > _REDACT_MAX_CHARS:
            text = text[:_REDACT_MAX_CHARS] + "…[truncated]"
        text = _EMAIL_RE.sub("[email]", text)
        text = _CPR_RE.sub("[cpr]", text)
        text = _TOKEN_RE.sub("[token]", text)
        text = _DK_PHONE_RE.sub("[phone]", text)
        text = _NAME_RE.sub("[name]", text)
        return text
    except Exception:
        # Never let redaction failure break telemetry; degrade to a marker.
        try:
            return "[redaction-error]" if text else ""
        except Exception:
            return "[redaction-error]"


def max_api_input_tokens() -> int:
    try:
        return max(8000, int(os.getenv("AI_MAX_INPUT_TOKENS", "22000")))
    except ValueError:
        return 22000


def max_output_tokens() -> int:
    try:
        return max(120, int(os.getenv("AI_MAX_OUTPUT_TOKENS", "600")))
    except ValueError:
        return 600


def capture_final_enabled() -> bool:
    """RT-02 gate: capture the final answer the model already produced on the
    no-tool-calls iteration instead of discarding it and paying a second
    full completion in the caller's deferred stream. Set AI_CAPTURE_FINAL=0
    to restore the old discard+regenerate path (rollback)."""
    return (os.getenv("AI_CAPTURE_FINAL", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}


def live_tool_events_enabled() -> bool:
    """AG-02 gate: stream tool start/finish-events LIVE fra agent-loopet (kørt
    i en worker-tråd) mens det stadig kører, i stedet for først efter loopet
    er færdigt. Sæt AI_LIVE_TOOL_EVENTS=0 for at gendanne den gamle post-hoc
    emissionssti (rollback)."""
    return (os.getenv("AI_LIVE_TOOL_EVENTS", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}


def iter_buffered_text_chunks(text: str, words_per_chunk: int = 3) -> Iterator[str]:
    """Split a buffered (already complete) answer into ~N-word pieces so the
    captured final answer (RT-02) still feels typewriter-streamed in the SSE
    UI. Concatenating the pieces yields the exact original text — whitespace
    is preserved on the chunk boundaries."""
    if not text:
        return
    tokens = _re.findall(r"\s*\S+\s*", text)
    if not tokens:
        # Pure-whitespace input: emit as-is so nothing is silently dropped.
        yield text
        return
    step = max(1, int(words_per_chunk))
    for i in range(0, len(tokens), step):
        yield "".join(tokens[i:i + step])


def max_output_tokens_for_turn(has_tools: bool) -> int:
    """Output-token cap for a single turn.

    Tool-deciding turns (the model is choosing WHICH tool to call) emit only a
    short tool-call payload, so a tight cap avoids paying for over-generation
    without ever truncating a real answer. Final-answer turns keep the full
    max_output_tokens() budget. The tool-turn cap is env-tunable and is clamped
    so it never exceeds the answer budget. Reverse by setting
    AI_MAX_TOOL_TURN_OUTPUT_TOKENS equal to AI_MAX_OUTPUT_TOKENS.
    """
    answer_budget = max_output_tokens()
    if not has_tools:
        return answer_budget
    try:
        cap = int(os.getenv("AI_MAX_TOOL_TURN_OUTPUT_TOKENS", "320"))
    except ValueError:
        cap = 320
    # Never below a safe floor, never above the answer budget.
    return max(120, min(cap, answer_budget))


def tpm_budget() -> int:
    """Soft input-token ceiling with headroom under org TPM limits."""
    try:
        return max(12000, int(os.getenv("AI_TPM_BUDGET", "28000")))
    except ValueError:
        return 28000


def rate_limit_retry_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("AI_RATE_LIMIT_RETRY_SECONDS", "2.5")))
    except ValueError:
        return 2.5


def rate_limit_backoff_cap_seconds() -> float:
    """Upper bound for the exponential backoff between transient retries."""
    try:
        return max(0.0, float(os.getenv("AI_RATE_LIMIT_BACKOFF_CAP_SECONDS", "20")))
    except ValueError:
        return 20.0


def _backoff_wait_seconds(attempt_index: int) -> float:
    """Exponential backoff (base * 2^attempt) capped, for 429/transient retries.

    ``attempt_index`` is 0-based: the first failed attempt waits ``base``, the
    next ``base*2``, etc., never exceeding the configured cap. Returns the wait
    in seconds; callers decide whether to sleep.
    """
    base = rate_limit_retry_seconds()
    if base <= 0:
        return 0.0
    try:
        idx = max(0, int(attempt_index))
    except Exception:
        idx = 0
    # Cap the exponent so very long loops can't overflow into huge floats.
    wait = base * (2 ** min(idx, 16))
    return min(wait, rate_limit_backoff_cap_seconds())


def max_tool_calls_per_run() -> int:
    """Hard ceiling on total tool calls per agent run (runaway-loop guard)."""
    try:
        return max(1, int(os.getenv("AI_MAX_TOOL_CALLS", "12")))
    except ValueError:
        return 12


def max_run_cost_usd() -> float:
    """Approximate per-run USD cost ceiling (0 disables the guard)."""
    try:
        return max(0.0, float(os.getenv("AI_MAX_RUN_COST", "0")))
    except ValueError:
        return 0.0


def max_run_tokens() -> int:
    """Approximate per-run total-token ceiling (0 disables the guard)."""
    try:
        return max(0, int(os.getenv("AI_MAX_RUN_TOKENS", "0")))
    except ValueError:
        return 0


def _approx_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Rough USD estimate from token counts using configurable per-1K rates.

    Defaults track gpt-4o pricing tiers; only used for the soft cost ceiling, so
    precision is not important — it just needs to be monotonic in token use.
    """
    try:
        in_rate = float(os.getenv("AI_COST_PER_1K_INPUT", "0.0025"))
        out_rate = float(os.getenv("AI_COST_PER_1K_OUTPUT", "0.01"))
    except ValueError:
        in_rate, out_rate = 0.0025, 0.01
    return (max(0, input_tokens) / 1000.0) * in_rate + (max(0, output_tokens) / 1000.0) * out_rate


def _usage_run_cost(usage: Dict[str, Any]) -> Tuple[int, float]:
    """Return (total_tokens, approx_usd_cost) for an accumulated usage dict."""
    try:
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    except Exception:
        return 0, 0.0
    total = input_tokens + output_tokens
    return total, _approx_cost_usd(input_tokens, output_tokens)


def _run_cost_exceeded(usage: Dict[str, Any]) -> bool:
    """True when the accumulated run usage breaches the cost/token ceiling."""
    cost_cap = max_run_cost_usd()
    token_cap = max_run_tokens()
    if cost_cap <= 0 and token_cap <= 0:
        return False
    total_tokens, cost = _usage_run_cost(usage)
    if cost_cap > 0 and cost >= cost_cap:
        return True
    if token_cap > 0 and total_tokens >= token_cap:
        return True
    return False


# Graceful Danish message reused when a run is stopped for being over budget.
_OVER_BUDGET_DA = (
    "Denne forespørgsel blev for omfattende til at fuldføre lige nu. "
    "Prøv at stille et mere afgrænset spørgsmål — så får du et hurtigt og præcist svar."
)


def _accumulate_usage(total: Dict[str, Any], latest: Dict[str, Any]) -> Dict[str, Any]:
    """Sum token counts across loop iterations for the per-run cost guard.

    The single-iteration ``usage`` dict (used for telemetry) is intentionally
    left as the last-iteration snapshot by callers; this separate accumulator is
    only consulted by the cost ceiling so it never changes logged token values.
    """
    if not latest:
        return total
    for key in (
        "input_tokens", "prompt_tokens",
        "output_tokens", "completion_tokens",
        "total_tokens",
    ):
        try:
            val = int(latest.get(key) or 0)
        except Exception:
            val = 0
        if val:
            total[key] = int(total.get(key) or 0) + val
    return total


def rate_limit_cooldown_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("AI_RATE_LIMIT_COOLDOWN_SECONDS", "90")))
    except ValueError:
        return 90.0


def _note_rate_limit_hit() -> None:
    global _RATE_LIMIT_COOLDOWN_UNTIL
    _RATE_LIMIT_COOLDOWN_UNTIL = time.time() + rate_limit_cooldown_seconds()


def in_rate_limit_cooldown() -> bool:
    return time.time() < _RATE_LIMIT_COOLDOWN_UNTIL


def _estimate_text_tokens(text: Any) -> int:
    if text is None:
        return 0
    if not isinstance(text, str):
        text = _safe_json(text)
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for msg in messages or []:
        total += 4  # per-message overhead
        content = msg.get("content")
        if isinstance(content, str):
            total += _estimate_text_tokens(content)
        elif content is not None:
            total += _estimate_text_tokens(content)
        tool_calls = msg.get("tool_calls") or []
        for call in tool_calls:
            fn = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
            if isinstance(fn, dict):
                total += _estimate_text_tokens(fn.get("name"))
                total += _estimate_text_tokens(fn.get("arguments"))
            elif fn is not None:
                total += _estimate_text_tokens(getattr(fn, "name", ""))
                total += _estimate_text_tokens(getattr(fn, "arguments", ""))
    return total


def _strip_heavy_tool_payload(output: str, max_chars: int = 6000) -> str:
    """Remove UI-only blobs from tool JSON before sending it back to the model."""
    if not output:
        return output
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            for heavy_key in (
                "raw_products", "raw_product", "search_debug", "debug",
                "description", "body_html", "variants_full",
            ):
                data.pop(heavy_key, None)
            metadata = data.get("metadata")
            if isinstance(metadata, dict) and len(_safe_json(metadata)) > 1200:
                data["metadata"] = {k: metadata[k] for k in list(metadata.keys())[:8]}
            variants = data.get("variants")
            if isinstance(variants, list) and len(variants) > 4:
                data["variants"] = variants[:4]
            comparison = data.get("comparison")
            if isinstance(comparison, list):
                data["comparison"] = comparison[:4]
            vendor = data.get("vendor")
            if isinstance(vendor, dict) and len(_safe_json(vendor)) > 1500:
                data["vendor"] = {
                    k: vendor[k]
                    for k in ("name", "slug", "url", "course_count", "price_label")
                    if k in vendor
                }
            output = _safe_json(data)
    except Exception:
        pass
    if len(output) > max_chars:
        return _safe_json({"status": "truncated", "message": "Tool output shortened for model context."})
    return output


def consolidate_system_layers(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the static system prompt isolated for cache hits; merge dynamic layers.

    Prompt-cache invariant (OpenAI auto-caches identical prompt PREFIXES >1024
    tokens for a 50% input discount):

      * messages[0] (the static system prompt: SYSTEM_CORE / HR_SYSTEM_PROMPT,
        optionally tenant-name-swapped which is STABLE per tenant) is emitted
        FIRST and verbatim, so it is byte-identical across every turn of a
        session and across sessions of the same tenant -> it forms the cacheable
        prefix.
      * ALL per-turn / dynamic system layers (session context, playbook hints,
        INTENT lines, length nudges, etc.) are merged into a SEPARATE
        "[SESSION KONTEKST]" system message placed AFTER the static prefix, so a
        changing dynamic token can never shift bytes inside the cached prefix.

    Do NOT inject timestamps / uuids / random ids into messages[0]; put any such
    dynamic content into a later (non-prefix) message instead. This function is
    what enforces that split, so callers get cache hits for free.
    """
    if not messages:
        return []
    static = messages[0] if messages[0].get("role") == "system" else None
    start = 1 if static else 0
    dynamic_parts: List[str] = []
    rest: List[Dict[str, Any]] = []
    for msg in messages[start:]:
        if msg.get("role") == "system":
            content = str(msg.get("content") or "").strip()
            if content:
                dynamic_parts.append(content)
        else:
            rest.append(msg)
    consolidated: List[Dict[str, Any]] = []
    if static:
        consolidated.append(static)
    if dynamic_parts:
        consolidated.append({
            "role": "system",
            "content": "[SESSION KONTEKST]\n" + "\n\n".join(dynamic_parts),
        })
    consolidated.extend(rest)
    return consolidated


def compaction_level_for_messages(messages: List[Dict[str, Any]]) -> str:
    """Return normal / aggressive / over_budget / cooldown for observability."""
    if in_rate_limit_cooldown():
        return "cooldown"
    merged = consolidate_system_layers(messages)
    normal = compact_messages_for_api(merged, aggressive=False)
    if estimate_messages_tokens(normal) <= tpm_budget():
        return "normal"
    aggressive = compact_messages_for_api(merged, aggressive=True)
    if estimate_messages_tokens(aggressive) <= tpm_budget():
        return "aggressive"
    return "over_budget"


def prepare_messages_for_turn(
    messages: List[Dict[str, Any]],
    *,
    aggressive: bool = False,
) -> List[Dict[str, Any]]:
    """Normalize, merge dynamic system layers, and enforce token budget."""
    merged = consolidate_system_layers(messages)
    compact = compact_messages_for_api(merged, aggressive=aggressive)
    if estimate_messages_tokens(compact) > tpm_budget():
        compact = compact_messages_for_api(merged, aggressive=True)
    # Repair any tool/tool_calls pairing broken by trimming before it hits the API.
    return _sanitize_tool_sequence(compact)


def check_turn_token_budget(messages: List[Dict[str, Any]]) -> tuple:
    """Return (allowed, user_message, compaction_level)."""
    level = compaction_level_for_messages(messages)
    if level != "over_budget":
        return True, "", level
    return (
        False,
        (
            "Denne samtale er blevet for lang til at behandle sikkert lige nu. "
            "Start venligst en ny chat — så får du bedre og hurtigere svar."
        ),
        level,
    )


def iter_completion_stream(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
):
    """Stream final assistant text tokens (no tools)."""
    client = _openai_client()
    prepared = prepare_messages_for_turn(messages, aggressive=in_rate_limit_cooldown())
    chosen = model or (fast_model() if in_rate_limit_cooldown() else main_model())
    limit = max_tokens or max_output_tokens()
    models_to_try = []
    for candidate in (chosen, fast_model(), main_model()):
        if candidate and candidate not in models_to_try:
            models_to_try.append(candidate)
    for model_name in models_to_try:
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=prepared,
                stream=True,
                max_tokens=limit,
                temperature=0.4,
            )
            for chunk in resp:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content
            return
        except Exception as exc:
            if _is_rate_limit_error(exc):
                _note_rate_limit_hit()
                prepared = prepare_messages_for_turn(messages, aggressive=True)
                continue
            raise


def choose_turn_model(
    *,
    intent: str = "",
    tool_count: int = 0,
    token_estimate: int = 0,
    prefer_quality: bool = False,
) -> str:
    """Route easy turns to the fast model without sacrificing complex flows.

    Honours AI_MODEL_ROUTING (quality|balanced|cost) via the pure, testable
    _route_tier() helper. The only runtime-only short-circuit kept here is the
    rate-limit cooldown, which forces mini regardless of mode/intent so we shed
    TPM pressure fast.
    """
    # Runtime guard (cannot live in the pure helper): under a 429 cooldown we
    # always drop to mini to recover capacity. This wins over prefer_quality.
    if in_rate_limit_cooldown():
        return fast_model()
    tier = _route_tier(
        model_routing_mode(),
        intent,
        tool_count,
        token_estimate,
        prefer_quality,
    )
    return fast_model() if tier == "fast" else main_model()


def user_facing_error_message(exc: Exception) -> str:
    text = str(exc).lower()
    if "429" in text or "rate_limit" in text or "tokens per min" in text or "tpm" in text:
        return (
            "Beklager — forespørgslen blev for stor eller ramte en midlertidig grænse. "
            "Prøv igen med et kortere spørgsmål, eller start en ny samtale."
        )
    if "timeout" in text or "timed out" in text:
        return "Beklager — svaret tog for lang tid. Prøv igen med et mere konkret spørgsmål."
    return "Beklager, der opstod en teknisk fejl. Prøv venligst igen."


def run_direct_completion(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """One-shot chat completion without tools."""
    client = _openai_client()
    prepared = prepare_messages_for_turn(messages, aggressive=in_rate_limit_cooldown())
    model = model or (fast_model() if in_rate_limit_cooldown() else main_model())
    limit = max_tokens or max_output_tokens()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=prepared,
            stream=False,
            max_tokens=limit,
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        if _is_rate_limit_error(exc):
            _note_rate_limit_hit()
            resp = client.chat.completions.create(
                model=fast_model(),
                messages=prepare_messages_for_turn(messages, aggressive=True),
                stream=False,
                max_tokens=limit,
                temperature=0.4,
            )
            return (resp.choices[0].message.content or "").strip()
        raise


# --- LLM-as-router for ambiguous intents --------------------------------------
# The rule-based classifier (_classify_intent_local in app1/agent.py) is the
# source of truth for everything it CAN classify. Its one ambiguous catch-all
# ("discovery") lumps together genuinely different needs (learning_path,
# skill_gap, comparison, profile_and_search). ONLY on that catch-all turn we do a
# single cheap gpt-4o-mini classification to pick the real intent before the main
# turn, and feed the refined intent into tool selection + model-tier routing.
#
# Cost/latency safety: this fires only on ambiguous turns (confident regex intents
# skip it entirely), is bounded (tiny prompt, low max-tokens, short timeout), and
# any error/timeout/garbage silently falls back to the regex result. A small
# bounded in-process cache avoids repeat calls for the same text.

# The fixed enum the router is allowed to return. Each label is already understood
# by both _route_tier() (model tier) and get_employee_tool_selection() (tools).
ROUTER_INTENT_ENUM = (
    "learning_path",
    "skill_gap",
    "comparison",
    "profile_and_search",
    "discovery",
)

_ROUTER_CACHE: Dict[str, str] = {}
_ROUTER_CACHE_MAX = 256
# AG-02: beskytter router-cachen mod samtidige læs/skriv når agent-loopet
# kører i en worker-tråd (AI_LIVE_TOOL_EVENTS).
_ROUTER_CACHE_LOCK = threading.Lock()

_ROUTER_SYSTEM_PROMPT = (
    "Du klassificerer en brugers besked til en dansk kursus- og kompetencerådgiver. "
    "Vælg PRÆCIST ÉT af følgende intentioner og svar KUN med selve etiketten (intet andet):\n"
    "- learning_path: brugeren vil have en læringssti/plan for at nå et mål eller en rolle "
    "(\"lav en læringssti\", \"hvordan bliver jeg X\", \"plan for at lære Y\").\n"
    "- skill_gap: brugeren spørger hvilke kompetencer de mangler "
    "(\"hvad mangler jeg for at blive X\", \"hvilke kompetencer skal jeg have\").\n"
    "- comparison: brugeren vil sammenligne kurser eller muligheder "
    "(\"hvad er forskellen\", \"hvilket er bedst\", \"X vs Y\").\n"
    "- profile_and_search: brugeren fortæller om sin egen baggrund/erfaring OG vil finde kurser.\n"
    "- discovery: almindelig kursussøgning eller noget der ikke passer ovenfor.\n"
    "Svar med ét ord fra: learning_path, skill_gap, comparison, profile_and_search, discovery."
)


def llm_router_enabled() -> bool:
    """Env gate for the LLM-as-router.

    Default is ON-for-ambiguous-only: the router only ever fires on the regex
    catch-all, and is bounded + falls back on any failure, so the cost is tiny.
    Disable completely (regex path stays the source of truth + fallback) with
    AI_LLM_ROUTER=0 / false / off / no.
    """
    return (os.getenv("AI_LLM_ROUTER", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def _router_timeout_seconds() -> float:
    try:
        return float(os.getenv("AI_LLM_ROUTER_TIMEOUT_SECONDS", "4"))
    except ValueError:
        return 4.0


def _parse_router_label(raw: Any, fallback: str) -> str:
    """Defensively map a model response to one enum label; unknown -> fallback."""
    if not isinstance(raw, str):
        return fallback
    text = raw.strip().lower()
    if not text:
        return fallback
    # Exact match first, then substring (model may add punctuation/prose).
    if text in ROUTER_INTENT_ENUM:
        return text
    for label in ROUTER_INTENT_ENUM:
        if label in text:
            return label
    return fallback


def classify_intent_llm(user_query: str, *, fallback: str = "discovery") -> str:
    """Single cheap gpt-4o-mini classification of an ambiguous turn.

    Returns one label from ROUTER_INTENT_ENUM, or `fallback` (the regex result)
    on any error/timeout/garbage. Cached per query text within the process.
    Reuses the shared OpenAI client + fast_model() (no new client config).
    """
    if not llm_router_enabled():
        return fallback
    query = (user_query or "").strip()
    if not query:
        return fallback

    cache_key = str(hash(query))
    with _ROUTER_CACHE_LOCK:
        cached = _ROUTER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    label = fallback
    try:
        client = _openai_client()
        # Short, per-request timeout so an ambiguous turn never stalls the SSE turn.
        # with_options is the SDK's per-request override; guard for resilience.
        with_options = getattr(client, "with_options", None)
        scoped = with_options(timeout=_router_timeout_seconds()) if callable(with_options) else client
        resp = scoped.chat.completions.create(
            model=fast_model(),
            messages=[
                {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": query[:2000]},
            ],
            stream=False,
            max_tokens=4,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content if resp.choices else ""
        label = _parse_router_label(raw, fallback)
    except Exception:
        # Any error/timeout/unparseable response -> silently keep the regex result.
        label = fallback

    # Bounded cache: drop oldest-ish entry when full (simple, no LRU needed).
    with _ROUTER_CACHE_LOCK:
        if len(_ROUTER_CACHE) >= _ROUTER_CACHE_MAX:
            try:
                _ROUTER_CACHE.pop(next(iter(_ROUTER_CACHE)))
            except StopIteration:
                pass
        _ROUTER_CACHE[cache_key] = label
    return label


def _truncate_text(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 16] + "\n…[truncated]"


def compact_messages_for_api(
    messages: List[Dict[str, Any]],
    *,
    max_tokens: Optional[int] = None,
    aggressive: bool = False,
) -> List[Dict[str, Any]]:
    """Keep prompt size under org TPM limits by trimming history and tool payloads."""
    limit = max_tokens or max_api_input_tokens()
    if not messages:
        return []

    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]
    core_system = system_msgs[:1]
    extra_system = system_msgs[1:]

    keep_recent = 10 if aggressive else 14
    recent = other_msgs[-keep_recent:]
    dropped = other_msgs[:-keep_recent] if len(other_msgs) > keep_recent else []

    compacted: List[Dict[str, Any]] = []
    for msg in core_system:
        compacted.append({
            **msg,
            "content": _truncate_text(str(msg.get("content") or ""), 12000 if not aggressive else 8000),
        })

    if dropped:
        user_bits = []
        for msg in dropped:
            if msg.get("role") == "user" and msg.get("content"):
                user_bits.append(str(msg["content"])[:120])
            if len(user_bits) >= 4:
                break
        if user_bits:
            compacted.append({
                "role": "system",
                "content": "TIDLIGERE SAMTALE (kort): " + " | ".join(user_bits),
            })

    per_extra = 1200 if aggressive else 1800
    for msg in extra_system:
        compacted.append({
            **msg,
            "content": _truncate_text(str(msg.get("content") or ""), per_extra),
        })

    for msg in recent:
        copy = dict(msg)
        role = copy.get("role")
        if role == "tool":
            copy["content"] = _strip_heavy_tool_payload(
                str(copy.get("content") or ""),
                max_chars=3500 if aggressive else 6000,
            )
        elif role == "assistant":
            tool_calls = copy.get("tool_calls")
            if tool_calls:
                trimmed_calls = []
                for call in tool_calls:
                    if isinstance(call, dict):
                        fn = dict(call.get("function") or {})
                        fn["arguments"] = _truncate_text(str(fn.get("arguments") or ""), 800)
                        trimmed = dict(call)
                        trimmed["function"] = fn
                        trimmed_calls.append(trimmed)
                    else:
                        trimmed_calls.append(call)
                copy["tool_calls"] = trimmed_calls
            if copy.get("content"):
                copy["content"] = _truncate_text(str(copy["content"]), 2500)
        elif role == "user" and copy.get("content"):
            copy["content"] = _truncate_text(str(copy["content"]), 2000)
        compacted.append(copy)

    while estimate_messages_tokens(compacted) > limit and len(compacted) > 2:
        # Drop oldest non-core message first.
        for idx, msg in enumerate(compacted):
            if msg.get("role") != "system" or idx > 0:
                compacted.pop(idx)
                break
        else:
            break

    if estimate_messages_tokens(compacted) > limit:
        for msg in compacted:
            if msg.get("role") == "system" and msg is not compacted[0]:
                msg["content"] = _truncate_text(str(msg.get("content") or ""), 600)

    return compacted


def _sanitize_tool_sequence(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Guarantee a valid OpenAI tool-call sequence.

    OpenAI rejects (400) any message with role ``tool`` that is not a direct
    response to an immediately preceding assistant message whose ``tool_calls``
    contains a matching ``tool_call_id``. History slicing / token-budget
    trimming can orphan a tool message from its parent assistant call (the
    classic "messages with role 'tool' must be a response to a preceding message
    with 'tool_calls'" error). This pass drops orphaned tool messages and strips
    unanswered tool_calls so the outgoing request is always well-formed.
    """
    if not messages:
        return messages
    out: List[Dict[str, Any]] = []
    n = len(messages)
    i = 0
    while i < n:
        msg = messages[i]
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            calls = [c for c in (msg.get("tool_calls") or [])
                     if isinstance(c, dict) and c.get("id")]
            call_ids = [c["id"] for c in calls]
            # Collect the consecutive tool responses that immediately follow.
            j = i + 1
            answers: Dict[str, Dict[str, Any]] = {}
            while j < n and messages[j].get("role") == "tool":
                tcid = messages[j].get("tool_call_id")
                if tcid in call_ids and tcid not in answers:
                    answers[tcid] = messages[j]
                j += 1
            kept_calls = [c for c in calls if c["id"] in answers]
            if kept_calls:
                fixed = dict(msg)
                fixed["tool_calls"] = kept_calls
                out.append(fixed)
                for cid in (c["id"] for c in kept_calls):
                    out.append(answers[cid])
            else:
                # No surviving answers: keep the assistant turn only if it still
                # carries real text content; otherwise drop the dangling call.
                content = (msg.get("content") or "").strip()
                if content:
                    fixed = dict(msg)
                    fixed.pop("tool_calls", None)
                    out.append(fixed)
            i = j
            continue
        if role == "tool":
            # Orphaned tool message (no matching preceding assistant call) — drop.
            i += 1
            continue
        out.append(msg)
        i += 1
    return out


def _is_rate_limit_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    return "rate_limit" in name or "429" in text or "tokens per min" in text or "tpm" in text


def _current_tenant_cache_scope() -> str:
    """Best-effort tenant identifier for the active request.

    Tool results for company-scoped tools (e.g. HR analytics) must never be
    shared across tenants. The HR/employee executors read ``company_id`` from
    the Flask session internally, but that id is not threaded down to the tool
    cache key — so we resolve it here from the active request context.

    Returns an empty string when there is no Flask request context (e.g. batch
    jobs or background tasks) or when Flask is unavailable, so behaviour stays
    unchanged in those paths and module import never fails on prod boot.
    """
    try:
        from flask import has_request_context, session as flask_session  # lazy, optional

        if not has_request_context():
            return ""
        company_id = flask_session.get("company_id")
        if company_id in (None, ""):
            return ""
        return str(company_id)
    except Exception:
        return ""


def _cache_tool_result(cache_key: str, ttl: int, output: str) -> None:
    if not cache_key or ttl <= 0:
        return
    with _TOOL_CACHE_LOCK:
        if len(_TOOL_CACHE) >= _TOOL_CACHE_MAX:
            oldest_key = min(_TOOL_CACHE, key=lambda k: _TOOL_CACHE[k][0])
            _TOOL_CACHE.pop(oldest_key, None)
        _TOOL_CACHE[cache_key] = (time.time() + ttl, output)


def _chat_completion_with_resilience(
    client: OpenAI,
    *,
    model: str,
    source_messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tool_choice: Any,
    output_cap: Optional[int] = None,
) -> Tuple[Any, str, List[Dict[str, Any]]]:
    attempts = [
        (model, False),
        (model, True),
        (fast_model(), True),
    ]
    last_exc: Optional[Exception] = None
    for attempt_index, (attempt_model, aggressive) in enumerate(attempts):
        working = prepare_messages_for_turn(source_messages, aggressive=aggressive)
        kwargs: Dict[str, Any] = {
            "model": attempt_model,
            "messages": working,
            "stream": False,
            # Tool-deciding turns only emit a short tool-call payload; cap them
            # tighter (env-tunable) so we don't pay for over-generation. The
            # caller lifts the cap (output_cap) once tools have executed so a
            # captured final answer (RT-02) is never truncated at the tool cap.
            "max_tokens": int(output_cap) if output_cap else max_output_tokens_for_turn(bool(tools)),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = chat_tool_choice(tool_choice)
        try:
            # Timeout is enforced by the shared client (AI_OPENAI_TIMEOUT_SECONDS).
            return client.chat.completions.create(**kwargs), attempt_model, working
        except Exception as exc:
            last_exc = exc
            if not _is_rate_limit_error(exc):
                raise
            _note_rate_limit_hit()
            # Exponential backoff (base * 2^attempt, capped) on 429/transient errors.
            wait = _backoff_wait_seconds(attempt_index)
            if wait > 0:
                time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("chat completion failed without exception")


def _truncate_responses_tool_outputs(items: List[Dict[str, Any]], max_chars: int = 3500) -> List[Dict[str, Any]]:
    trimmed = []
    for item in items or []:
        if item.get("type") != "function_call_output":
            trimmed.append(item)
            continue
        copy = dict(item)
        copy["output"] = _strip_heavy_tool_payload(str(copy.get("output") or ""), max_chars=max_chars)
        trimmed.append(copy)
    return trimmed


def _responses_create_with_resilience(
    client: OpenAI,
    *,
    model: str,
    source_messages: List[Dict[str, Any]],
    response_tools: List[Dict[str, Any]],
    tool_choice: Any,
    prompt_cache_key: str,
    previous_response_id: Optional[str],
    input_items: Optional[List[Dict[str, Any]]],
    output_cap: Optional[int] = None,
) -> Tuple[Any, str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    is_continuation = bool(previous_response_id and input_items)
    if is_continuation:
        attempts = [
            (model, input_items, previous_response_id, 6000),
            (model, _truncate_responses_tool_outputs(input_items or [], max_chars=3500), previous_response_id, 3500),
            (fast_model(), _truncate_responses_tool_outputs(input_items or [], max_chars=2500), previous_response_id, 2500),
        ]
    else:
        attempts = [
            (model, None, None, 6000),
            (model, None, None, 3500),
            (fast_model(), None, None, 2500),
        ]

    last_exc: Optional[Exception] = None
    for attempt_index, (attempt_model, payload_items, prev_id, _max_chars) in enumerate(attempts):
        if is_continuation:
            payload = payload_items or []
            working = source_messages
        else:
            aggressive = attempt_model == fast_model() or _max_chars <= 3500
            working = prepare_messages_for_turn(source_messages, aggressive=aggressive)
            payload = _messages_to_responses_input(working)
        kwargs: Dict[str, Any] = {
            "model": attempt_model,
            "input": payload,
            "store": True,
            "stream": False,
            # Tool-deciding turns only emit a short tool-call payload; cap them
            # tighter (env-tunable) so we don't pay for over-generation. The
            # caller lifts the cap (output_cap) once tools have executed so a
            # captured final answer (RT-02) is never truncated at the tool cap.
            "max_output_tokens": int(output_cap) if output_cap else max_output_tokens_for_turn(bool(response_tools)),
        }
        if response_tools:
            kwargs["tools"] = response_tools
            kwargs["tool_choice"] = responses_tool_choice(tool_choice)
            kwargs["parallel_tool_calls"] = True
        if prompt_cache_key and not is_continuation:
            kwargs["prompt_cache_key"] = prompt_cache_key
        if prev_id:
            kwargs["previous_response_id"] = prev_id
        try:
            # Timeout is enforced by the shared client (AI_OPENAI_TIMEOUT_SECONDS).
            return client.responses.create(**kwargs), attempt_model, working, payload
        except Exception as exc:
            last_exc = exc
            if not _is_rate_limit_error(exc):
                raise
            _note_rate_limit_hit()
            # Exponential backoff (base * 2^attempt, capped) on 429/transient errors.
            wait = _backoff_wait_seconds(attempt_index)
            if wait > 0:
                time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("responses create failed without exception")


def _usage_to_dict(usage: Any) -> Dict[str, Any]:
    if not usage:
        return {}
    if hasattr(usage, "model_dump"):
        try:
            return usage.model_dump()
        except Exception:
            pass
    if isinstance(usage, dict):
        return usage
    return {}


def _cached_tokens_from_usage(usage: Dict[str, Any]) -> int:
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    return int(input_details.get("cached_tokens") or 0)


def ensure_ai_log_tables(mysql) -> None:
    """Create observability tables if a MySQL connection is available."""
    if not mysql:
        return
    cache_key = id(mysql)
    if cache_key in _LOG_TABLES_READY:
        return
    refresh_flask_mysql_connection(mysql)
    cur = mysql.connection.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_agent_runs (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                run_id VARCHAR(64) NOT NULL,
                session_id VARCHAR(255),
                company_id INT,
                username VARCHAR(255),
                agent_scope VARCHAR(30),
                runtime VARCHAR(30),
                model VARCHAR(100),
                prompt_version VARCHAR(100),
                toolset_version VARCHAR(100),
                tool_names TEXT,
                response_id VARCHAR(255),
                status VARCHAR(30),
                fallback_reason TEXT,
                latency_ms INT,
                input_tokens INT DEFAULT 0,
                output_tokens INT DEFAULT 0,
                cached_tokens INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_session_created (session_id, created_at),
                INDEX idx_company_created (company_id, created_at),
                INDEX idx_scope_created (agent_scope, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_tool_runs (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                run_id VARCHAR(64) NOT NULL,
                session_id VARCHAR(255),
                company_id INT,
                username VARCHAR(255),
                agent_scope VARCHAR(30),
                tool_name VARCHAR(100),
                tool_call_id VARCHAR(255),
                arguments_json TEXT,
                status VARCHAR(30),
                latency_ms INT,
                result_count INT DEFAULT 0,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_run (run_id),
                INDEX idx_tool_created (tool_name, created_at),
                INDEX idx_company_created (company_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        mysql.connection.commit()
        for col, ddl in (
            ("compaction_level", "VARCHAR(30) DEFAULT 'normal'"),
            ("runtime_path", "VARCHAR(60) DEFAULT ''"),
            ("embedding_skipped", "TINYINT NULL"),
            # Post-turn quality signal (Item #6): heuristic, reference-free, no
            # API cost. NULL when no self-eval ran for the run.
            ("self_eval_score", "FLOAT NULL"),
            # Hallucination circuit-breaker flag (Item #1): 1 when the live
            # chain-of-custody check found an unsupported price/date/title claim.
            ("grounding_violation", "TINYINT NULL"),
        ):
            try:
                cur.execute(f"ALTER TABLE ai_agent_runs ADD COLUMN {col} {ddl}")
                mysql.connection.commit()
            except Exception:
                try:
                    mysql.connection.rollback()
                except Exception:
                    pass
        _LOG_TABLES_READY.add(cache_key)
    except Exception:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()


def log_agent_run(
    mysql,
    *,
    run_id: str,
    session_id: str,
    company_id: Optional[Any],
    username: Optional[str],
    agent_scope: str,
    runtime: str,
    model: str,
    prompt_version: str,
    toolset_version: str,
    tool_names: Iterable[str],
    response_id: str,
    status: str,
    fallback_reason: str,
    latency_ms: int,
    usage: Dict[str, Any],
    compaction_level: str = "normal",
    runtime_path: str = "",
    embedding_skipped: Optional[bool] = None,
    self_eval_score: Optional[float] = None,
    grounding_violation: Optional[bool] = None,
) -> None:
    if not mysql or trace_sample_rate() <= 0:
        return
    try:
        refresh_flask_mysql_connection(mysql)
        ensure_ai_log_tables(mysql)
        cur = mysql.connection.cursor()
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        try:
            self_eval_value = float(self_eval_score) if self_eval_score is not None else None
        except (TypeError, ValueError):
            self_eval_value = None
        cur.execute(
            """
            INSERT INTO ai_agent_runs
            (run_id, session_id, company_id, username, agent_scope, runtime, model,
             prompt_version, toolset_version, tool_names, response_id, status,
             fallback_reason, latency_ms, input_tokens, output_tokens, cached_tokens,
             compaction_level, runtime_path, embedding_skipped, self_eval_score,
             grounding_violation)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                session_id,
                company_id,
                username,
                agent_scope,
                runtime,
                model,
                prompt_version,
                toolset_version,
                _redact_pii(",".join(tool_names)),
                response_id,
                status,
                _redact_pii(fallback_reason),
                latency_ms,
                input_tokens,
                output_tokens,
                _cached_tokens_from_usage(usage),
                compaction_level,
                runtime_path,
                (1 if embedding_skipped else 0 if embedding_skipped is False else None),
                self_eval_value,
                (1 if grounding_violation else 0 if grounding_violation is False else None),
            ),
        )
        mysql.connection.commit()
        cur.close()
    except Exception:
        try:
            mysql.connection.rollback()
        except Exception:
            pass


def update_agent_run_quality(
    mysql,
    *,
    run_id: str,
    self_eval_score: Optional[float] = None,
    grounding_violation: Optional[bool] = None,
) -> None:
    """Backfill the post-turn quality signals on an already-logged run row.

    The agent logs the run row right after the tool loop, but the self-eval score
    and grounding-violation flag are only known AFTER the answer has streamed.
    Rather than duplicating the row, we UPDATE it by run_id once the final answer
    is in hand. Fully guarded + idempotent: a missing column, missing row, or any
    DB error degrades silently and never raises into the SSE path. No-op when both
    signals are None or trace sampling is off.
    """
    if not mysql or trace_sample_rate() <= 0:
        return
    if self_eval_score is None and grounding_violation is None:
        return
    try:
        refresh_flask_mysql_connection(mysql)
        ensure_ai_log_tables(mysql)
        sets = []
        params: List[Any] = []
        if self_eval_score is not None:
            try:
                sets.append("self_eval_score = %s")
                params.append(float(self_eval_score))
            except (TypeError, ValueError):
                pass
        if grounding_violation is not None:
            sets.append("grounding_violation = %s")
            params.append(1 if grounding_violation else 0)
        if not sets:
            return
        params.append(run_id)
        cur = mysql.connection.cursor()
        cur.execute(
            f"UPDATE ai_agent_runs SET {', '.join(sets)} WHERE run_id = %s",
            tuple(params),
        )
        mysql.connection.commit()
        cur.close()
    except Exception:
        try:
            mysql.connection.rollback()
        except Exception:
            pass


def log_tool_run(
    mysql,
    *,
    run_id: str,
    session_id: str,
    company_id: Optional[Any],
    username: Optional[str],
    agent_scope: str,
    result: ToolCallResult,
) -> None:
    if not mysql or trace_sample_rate() <= 0:
        return
    try:
        refresh_flask_mysql_connection(mysql)
        ensure_ai_log_tables(mysql)
        result_count = 0
        try:
            parsed = json.loads(result.output or "{}")
            if isinstance(parsed, dict):
                result_count = int(parsed.get("count") or len(parsed.get("results", []) or []))
        except Exception:
            pass
        cur = mysql.connection.cursor()
        cur.execute(
            """
            INSERT INTO ai_tool_runs
            (run_id, session_id, company_id, username, agent_scope, tool_name, tool_call_id,
             arguments_json, status, latency_ms, result_count, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                session_id,
                company_id,
                username,
                agent_scope,
                result.name,
                result.call_id,
                _redact_pii(_safe_json(result.arguments)),
                result.status,
                result.latency_ms,
                result_count,
                _redact_pii(result.error),
            ),
        )
        mysql.connection.commit()
        cur.close()
    except Exception:
        try:
            mysql.connection.rollback()
        except Exception:
            pass


class _ChatToolCall:
    def __init__(self, call_id: str, name: str, arguments: Dict[str, Any]):
        self.id = call_id
        self.function = type("Function", (), {"name": name, "arguments": _safe_json(arguments)})()


def _execute_one_tool(
    *,
    executor: Callable[..., str],
    name: str,
    call_id: str,
    arguments: Dict[str, Any],
    username: Optional[str],
    session_id: Optional[str],
    company_scope: Optional[str] = None,
) -> ToolCallResult:
    start = time.time()
    status = "ok"
    error = ""
    ttl = tool_cache_ttl(name)
    cache_key = ""
    if ttl > 0:
        # company_id keys the cache to the caller's tenant so company-scoped
        # tool results (e.g. HR analytics) can never bleed across tenants.
        # session_id (a per-login UUID) and username remain in the key so the
        # same user keeps hitting the cache as before. company_scope is resolved
        # once by the caller (in the request thread) and threaded in, because
        # Flask's request context is not visible inside ThreadPoolExecutor
        # workers; fall back to resolving it here for any direct caller.
        if company_scope is None:
            company_scope = _current_tenant_cache_scope()
        cache_key = _safe_json({
            "name": name,
            "arguments": arguments,
            "company_id": company_scope or "",
            "username": username or "",
            "session_id": session_id or "",
        })
        with _TOOL_CACHE_LOCK:
            cached = _TOOL_CACHE.get(cache_key)
        if cached and cached[0] > time.time():
            return ToolCallResult(
                call_id=call_id,
                name=name,
                arguments=arguments,
                output=cached[1],
                latency_ms=0,
                status="cached",
                cache_hit=True,
            )
    try:
        output = executor(_ChatToolCall(call_id, name, arguments), username=username, session_id=session_id)
        try:
            parsed = json.loads(output or "{}")
            if isinstance(parsed, dict) and (parsed.get("status") == "error" or parsed.get("error")):
                status = "error"
                error = str(parsed.get("message") or parsed.get("error") or "")[:1000]
        except Exception:
            pass
    except Exception as exc:
        status = "error"
        error = str(exc)
        output = _safe_json({"status": "error", "message": error})
    result = ToolCallResult(
        call_id=call_id,
        name=name,
        arguments=arguments,
        output=output,
        latency_ms=int((time.time() - start) * 1000),
        status=status,
        error=error,
    )
    if ttl > 0 and status == "ok" and cache_key:
        _cache_tool_result(cache_key, ttl, _strip_heavy_tool_payload(output))
    return result


def _execute_tool_calls_parallel(
    *,
    calls: List[Any],
    tools: List[Dict[str, Any]],
    tool_executor: Callable[..., str],
    username: Optional[str],
    session_id: Optional[str],
    agent_scope: str = "employee",
    on_tool_event: Optional[ToolEventCallback] = None,
    company_scope: Optional[str] = None,
) -> List[ToolCallResult]:
    """Execute tool calls with safe parallelization for read-only tools."""
    normalized = []
    for tc in calls:
        if hasattr(tc, "function"):
            name = tc.function.name
            call_id = tc.id
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
        elif isinstance(tc, dict):
            name = tc.get("name") or tc.get("function", {}).get("name")
            call_id = tc.get("id") or tc.get("call_id")
            args = tc.get("arguments") or tc.get("function", {}).get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {}
        else:
            continue
        args = sanitize_args_for_tool(name, args, tools)
        normalized.append({"id": call_id, "name": name, "arguments": args})

    parallel_calls = [call for call in normalized if is_parallel_safe(call["name"])]
    serial_calls = [call for call in normalized if not is_parallel_safe(call["name"])]
    result_by_id: Dict[str, ToolCallResult] = {}

    # Resolve the tenant cache scope once, here in the calling thread, because
    # Flask's request context is not visible inside ThreadPoolExecutor workers.
    # AG-02: når hele agent-loopet kører i en worker-tråd (live tool events)
    # resolver kalderen scope i request-tråden og threader det ind i stedet.
    if company_scope is None:
        company_scope = _current_tenant_cache_scope()

    if len(parallel_calls) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(parallel_calls))) as pool:
            futures = {}
            for call in parallel_calls:
                _emit_tool_event(on_tool_event, build_tool_start_event(
                    call["name"],
                    call["id"],
                    agent_scope=agent_scope,
                ))
                futures[pool.submit(
                    _execute_one_tool,
                    executor=tool_executor,
                    name=call["name"],
                    call_id=call["id"],
                    arguments=call["arguments"],
                    username=username,
                    session_id=session_id,
                    company_scope=company_scope,
                )] = call["id"]
            for future, call_id in futures.items():
                result = future.result()
                result_by_id[call_id] = result
                _emit_tool_event(on_tool_event, build_tool_call_event(
                    result,
                    agent_scope=agent_scope,
                    phase="finish",
                ))
    else:
        serial_calls = parallel_calls + serial_calls

    for call in serial_calls:
        if call["id"] in result_by_id:
            continue
        _emit_tool_event(on_tool_event, build_tool_start_event(
            call["name"],
            call["id"],
            agent_scope=agent_scope,
        ))
        result = _execute_one_tool(
            executor=tool_executor,
            name=call["name"],
            call_id=call["id"],
            arguments=call["arguments"],
            username=username,
            session_id=session_id,
            company_scope=company_scope,
        )
        result_by_id[call["id"]] = result
        _emit_tool_event(on_tool_event, build_tool_call_event(
            result,
            agent_scope=agent_scope,
            phase="finish",
        ))

    return [result_by_id[call["id"]] for call in normalized if call["id"] in result_by_id]


def _messages_to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    converted = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            converted.append({"role": "developer", "content": msg.get("content") or ""})
        elif role in {"user", "assistant"}:
            content = msg.get("content") or ""
            if content or role == "user":
                converted.append({"role": role, "content": content})
            # Assistant tool_calls survive _sanitize_tool_sequence now that the
            # responses loop appends the parent message; emit matching
            # function_call items so the function_call_output items below are
            # never orphaned in a freshly built Responses request.
            if role == "assistant":
                for call in msg.get("tool_calls") or []:
                    if not isinstance(call, dict) or not call.get("id"):
                        continue
                    fn = call.get("function") or {}
                    args = fn.get("arguments")
                    if not isinstance(args, str):
                        try:
                            args = json.dumps(args or {}, ensure_ascii=False, default=str)
                        except Exception:
                            args = "{}"
                    converted.append({
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": fn.get("name") or "",
                        "arguments": args,
                    })
        elif role == "tool":
            converted.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id") or msg.get("id") or "",
                "output": msg.get("content") or "",
            })
    return converted


def _response_text(resp: Any) -> str:
    text = getattr(resp, "output_text", None)
    if text:
        return text
    parts = []
    for item in getattr(resp, "output", []) or []:
        content = getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else None) or []
        for block in content:
            value = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
            if value:
                parts.append(value)
    return "".join(parts)


def _response_truncated(resp: Any) -> bool:
    """True when a Responses API result stopped before completing its output
    (status='incomplete', typically reason='max_output_tokens'). A truncated
    answer must NOT be captured as the final answer (RT-02) — fall back to the
    deferred regeneration path instead."""
    status = getattr(resp, "status", None)
    if status is None and isinstance(resp, dict):
        status = resp.get("status")
    # Any incomplete status (max_output_tokens, content_filter, …) means the
    # text is unreliable; treat it as truncated.
    return status == "incomplete"


def _response_tool_calls(resp: Any) -> List[Dict[str, Any]]:
    calls = []
    for item in getattr(resp, "output", []) or []:
        item_type = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
        if item_type != "function_call":
            continue
        name = getattr(item, "name", None) or item.get("name")
        call_id = getattr(item, "call_id", None) or item.get("call_id") or getattr(item, "id", None) or item.get("id")
        raw_args = getattr(item, "arguments", None) or item.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except Exception:
            args = {}
        calls.append({"id": call_id, "name": name, "arguments": args})
    return calls


def _assistant_message_from_chat(message: Any) -> Dict[str, Any]:
    if hasattr(message, "model_dump"):
        return message.model_dump()
    return {
        "role": "assistant",
        "content": getattr(message, "content", "") or "",
    }


def _tool_call_signature(name: str, arguments: Any) -> str:
    """Stable fingerprint of a (tool, args) pair for repeat detection."""
    try:
        if isinstance(arguments, str):
            args_repr = arguments
        else:
            args_repr = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        args_repr = str(arguments)
    return f"{name}::{args_repr}"


def _forced_final_completion(
    client: OpenAI,
    *,
    model: str,
    source_messages: List[Dict[str, Any]],
) -> str:
    """Run one tool-less completion to summarise tool results into a real answer.

    Shared by the loop-exhausted path and the circuit-breaker / cost-ceiling
    early exits so guardrails reuse the exact existing forced-final behaviour
    instead of introducing a new failure mode.
    """
    final_text = ""
    try:
        forced = client.chat.completions.create(
            model=model,
            messages=prepare_messages_for_turn(source_messages),
            stream=False,
            max_tokens=max_output_tokens(),
            temperature=0.4,
        )
        final_text = (forced.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"[Agent forced-final error] {exc}")
    if not final_text:
        final_text = "Jeg kunne ikke færdiggøre værktøjsflowet. Prøv at stille spørgsmålet lidt mere konkret."
    return final_text


def run_chat_agent(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tool_executor: Callable[..., str],
    username: Optional[str],
    session_id: Optional[str],
    model: Optional[str] = None,
    tool_choice: Any = "auto",
    max_iterations: int = 5,
    defer_final_stream: bool = False,
    agent_scope: str = "employee",
    on_tool_event: Optional[ToolEventCallback] = None,
    company_scope: Optional[str] = None,
) -> AgentRunResult:
    model = model or main_model()
    start = time.time()
    client = _openai_client()
    source_messages = list(messages)
    tool_results: List[ToolCallResult] = []
    tool_messages: List[Dict[str, Any]] = []
    final_text = ""
    usage: Dict[str, Any] = {}
    current_tool_choice = tool_choice
    compaction_level = compaction_level_for_messages(messages)
    runtime_path = "chat"
    # Runaway-loop / cost guardrails (do not affect happy-path outputs).
    run_usage: Dict[str, Any] = {}
    seen_signatures: set = set()
    total_tool_calls = 0
    tool_call_cap = max_tool_calls_per_run()
    forced_final = False

    for _ in range(max_iterations):
        resp, model, _prepared = _chat_completion_with_resilience(
            client,
            model=model,
            source_messages=source_messages,
            tools=tools,
            tool_choice=current_tool_choice,
            # RT-02: once a tool has executed, the next iteration may well be
            # the final answer — lift the tight tool-turn cap so the captured
            # answer is never truncated at 320 tokens.
            output_cap=max_output_tokens() if (tool_results and capture_final_enabled()) else None,
        )
        iter_usage = _usage_to_dict(getattr(resp, "usage", None))
        usage = iter_usage or usage
        run_usage = _accumulate_usage(run_usage, iter_usage)
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            if defer_final_stream:
                # RT-02 (AI_CAPTURE_FINAL, default on): the model already wrote
                # the final answer in THIS completion — capture it instead of
                # discarding it and paying a second full completion in the
                # caller's deferred stream. Fall back to the old discard+
                # regenerate path when the text is empty or was cut off.
                captured = (msg.content or "") if capture_final_enabled() else ""
                finish_reason = str(getattr(resp.choices[0], "finish_reason", "") or "")
                truncated = finish_reason in ("length", "max_output_tokens")
                if captured.strip() and not truncated:
                    return AgentRunResult(
                        text=captured,
                        messages=source_messages,
                        tool_results=tool_results,
                        tool_messages=tool_messages,
                        runtime="chat",
                        usage=usage,
                        latency_ms=int((time.time() - start) * 1000),
                        needs_final_stream=False,
                        stream_messages=list(source_messages),
                        compaction_level=compaction_level,
                        runtime_path=runtime_path,
                    )
                return AgentRunResult(
                    text="",
                    messages=source_messages,
                    tool_results=tool_results,
                    tool_messages=tool_messages,
                    runtime="chat",
                    usage=usage,
                    latency_ms=int((time.time() - start) * 1000),
                    needs_final_stream=True,
                    stream_messages=list(source_messages),
                    compaction_level=compaction_level,
                    runtime_path=runtime_path,
                )
            final_text = msg.content or ""
            break
        assistant_msg = _assistant_message_from_chat(msg)
        source_messages.append(assistant_msg)

        # --- Circuit breaker: stop on repeated identical calls or call-cap ---
        repeated = False
        for call in calls:
            fn = getattr(call, "function", None)
            name = getattr(fn, "name", "") if fn is not None else ""
            raw_args = getattr(fn, "arguments", "") if fn is not None else ""
            sig = _tool_call_signature(name, raw_args)
            if sig in seen_signatures:
                repeated = True
            seen_signatures.add(sig)
        total_tool_calls += len(calls)
        if repeated or total_tool_calls > tool_call_cap:
            # Same (tool, args) twice OR too many tool calls this run: stop the
            # loop and force a real final answer via the existing tool-less path.
            forced_final = True
            break

        executed = _execute_tool_calls_parallel(
            calls=calls,
            tools=tools,
            tool_executor=tool_executor,
            username=username,
            session_id=session_id,
            agent_scope=agent_scope,
            on_tool_event=on_tool_event,
            company_scope=company_scope,
        )
        for result in executed:
            tool_results.append(result)
            model_output = _strip_heavy_tool_payload(result.output)
            tool_msg = {
                "role": "tool",
                "tool_call_id": result.call_id,
                "name": result.name,
                "content": model_output,
            }
            tool_messages.append(tool_msg)
            source_messages.append(tool_msg)
        current_tool_choice = "auto"

        # --- Cost guard: stop if the run breaches the cost/token ceiling ---
        if _run_cost_exceeded(run_usage):
            final_text = _OVER_BUDGET_DA
            return AgentRunResult(
                text=final_text,
                messages=source_messages + [{"role": "assistant", "content": final_text}],
                tool_results=tool_results,
                tool_messages=tool_messages,
                runtime="chat",
                usage=usage,
                latency_ms=int((time.time() - start) * 1000),
                compaction_level="over_budget",
                runtime_path="chat-over-budget",
            )
    else:
        # Loop exhausted its tool iterations without the model producing a final
        # answer. Force one more completion WITHOUT tools so the user gets a real
        # response (summarising the tool results) instead of a generic error.
        forced_final = True

    if forced_final:
        final_text = _forced_final_completion(client, model=model, source_messages=source_messages)

    return AgentRunResult(
        text=final_text,
        messages=source_messages,
        tool_results=tool_results,
        tool_messages=tool_messages,
        runtime="chat",
        usage=usage,
        latency_ms=int((time.time() - start) * 1000),
        compaction_level=compaction_level,
        runtime_path=runtime_path,
    )


def run_responses_agent(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tool_executor: Callable[..., str],
    username: Optional[str],
    session_id: Optional[str],
    model: Optional[str] = None,
    tool_choice: Any = "auto",
    max_iterations: int = 5,
    prompt_cache_key: str = "",
    defer_final_stream: bool = False,
    agent_scope: str = "employee",
    on_tool_event: Optional[ToolEventCallback] = None,
    company_scope: Optional[str] = None,
) -> AgentRunResult:
    model = model or main_model()
    start = time.time()
    client = _openai_client()
    source_messages = list(messages)
    response_tools = [to_responses_tool(tool) for tool in tools]
    tool_results: List[ToolCallResult] = []
    tool_messages: List[Dict[str, Any]] = []
    usage: Dict[str, Any] = {}
    previous_response_id = None
    final_text = ""
    raw_response = None
    response_id = ""
    input_items: Optional[List[Dict[str, Any]]] = None
    compaction_level = compaction_level_for_messages(messages)
    runtime_path = "responses"
    # Runaway-loop / cost guardrails (do not affect happy-path outputs).
    run_usage: Dict[str, Any] = {}
    seen_signatures: set = set()
    total_tool_calls = 0
    tool_call_cap = max_tool_calls_per_run()
    forced_final = False

    for _ in range(max_iterations):
        resp, model, _prepared, input_items = _responses_create_with_resilience(
            client,
            model=model,
            source_messages=source_messages,
            response_tools=response_tools,
            tool_choice=tool_choice,
            prompt_cache_key=prompt_cache_key,
            previous_response_id=previous_response_id,
            input_items=input_items,
            # RT-02: once a tool has executed, the next iteration may well be
            # the final answer — lift the tight tool-turn cap so the captured
            # answer is never truncated at 320 tokens.
            output_cap=max_output_tokens() if (tool_results and capture_final_enabled()) else None,
        )
        raw_response = resp
        response_id = getattr(resp, "id", "") or response_id
        previous_response_id = response_id
        iter_usage = _usage_to_dict(getattr(resp, "usage", None))
        usage = iter_usage or usage
        run_usage = _accumulate_usage(run_usage, iter_usage)
        calls = _response_tool_calls(resp)
        if not calls:
            if defer_final_stream:
                # RT-02 (AI_CAPTURE_FINAL, default on): the model already wrote
                # the final answer in THIS completion — capture it instead of
                # discarding it and paying a second full completion in the
                # caller's deferred stream. Fall back to the old discard+
                # regenerate path when the text is empty or was cut off.
                captured = _response_text(resp) if capture_final_enabled() else ""
                if captured.strip() and not _response_truncated(resp):
                    return AgentRunResult(
                        text=captured,
                        messages=source_messages,
                        tool_results=tool_results,
                        tool_messages=tool_messages,
                        response_id=response_id,
                        runtime="responses",
                        usage=usage,
                        latency_ms=int((time.time() - start) * 1000),
                        raw_response=raw_response,
                        needs_final_stream=False,
                        stream_messages=list(source_messages),
                        compaction_level=compaction_level,
                        runtime_path=runtime_path,
                    )
                return AgentRunResult(
                    text="",
                    messages=source_messages,
                    tool_results=tool_results,
                    tool_messages=tool_messages,
                    response_id=response_id,
                    runtime="responses",
                    usage=usage,
                    latency_ms=int((time.time() - start) * 1000),
                    raw_response=raw_response,
                    needs_final_stream=True,
                    stream_messages=list(source_messages),
                    compaction_level=compaction_level,
                    runtime_path=runtime_path,
                )
            final_text = _response_text(resp)
            break

        # --- Circuit breaker: stop on repeated identical calls or call-cap ---
        repeated = False
        for c in calls:
            sig = _tool_call_signature(c.get("name", ""), c.get("arguments"))
            if sig in seen_signatures:
                repeated = True
            seen_signatures.add(sig)
        total_tool_calls += len(calls)
        if repeated or total_tool_calls > tool_call_cap:
            # Same (tool, args) twice OR too many tool calls this run: stop the
            # loop and force a real final answer via the existing tool-less path.
            forced_final = True
            break

        executed = _execute_tool_calls_parallel(
            calls=[{"id": c["id"], "name": c["name"], "arguments": c["arguments"]} for c in calls],
            tools=tools,
            tool_executor=tool_executor,
            username=username,
            session_id=session_id,
            agent_scope=agent_scope,
            on_tool_event=on_tool_event,
            company_scope=company_scope,
        )
        # Append a synthetic chat-format assistant message carrying this turn's
        # tool_calls BEFORE the tool outputs. Without it the tool messages are
        # orphaned and _sanitize_tool_sequence drops them from stream_messages,
        # so the deferred final stream never sees this turn's tool results (the
        # chat path already does this via _assistant_message_from_chat).
        source_messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["arguments"] or {}, ensure_ascii=False, default=str),
                    },
                }
                for c in calls
                if c.get("id")
            ],
        })
        outputs = []
        for call, result in zip(calls, executed):
            tool_results.append(result)
            model_output = _strip_heavy_tool_payload(result.output)
            tool_msg = {
                "role": "tool",
                "tool_call_id": result.call_id,
                "name": result.name,
                "content": model_output,
            }
            tool_messages.append(tool_msg)
            source_messages.append(tool_msg)
            outputs.append({
                "type": "function_call_output",
                "call_id": result.call_id,
                "output": model_output,
            })
        input_items = outputs
        tool_choice = "auto"

        # --- Cost guard: stop if the run breaches the cost/token ceiling ---
        if _run_cost_exceeded(run_usage):
            final_text = _OVER_BUDGET_DA
            return AgentRunResult(
                text=final_text,
                messages=source_messages + [{"role": "assistant", "content": final_text}],
                tool_results=tool_results,
                tool_messages=tool_messages,
                response_id=response_id,
                runtime="responses",
                usage=usage,
                latency_ms=int((time.time() - start) * 1000),
                raw_response=raw_response,
                compaction_level="over_budget",
                runtime_path="responses-over-budget",
            )
    else:
        # Loop exhausted without a final answer: force a final response (see
        # run_chat_agent) so the user gets a real reply, not a generic failure.
        forced_final = True

    if forced_final:
        final_text = _forced_final_completion(client, model=model, source_messages=source_messages)

    return AgentRunResult(
        text=final_text,
        messages=source_messages + ([{"role": "assistant", "content": final_text}] if final_text else []),
        tool_results=tool_results,
        tool_messages=tool_messages,
        response_id=response_id,
        runtime="responses",
        usage=usage,
        latency_ms=int((time.time() - start) * 1000),
        raw_response=raw_response,
        compaction_level=compaction_level,
        runtime_path=runtime_path,
    )


def run_agent_with_fallback(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tool_executor: Callable[..., str],
    username: Optional[str],
    session_id: Optional[str],
    model: Optional[str] = None,
    tool_choice: Any = "auto",
    max_iterations: int = 5,
    prompt_cache_key: str = "",
    defer_final_stream: bool = True,
    agent_scope: str = "employee",
    on_tool_event: Optional[ToolEventCallback] = None,
    company_scope: Optional[str] = None,
) -> AgentRunResult:
    if runtime_mode() == "chat":
        return run_chat_agent(
            messages=messages,
            tools=tools,
            tool_executor=tool_executor,
            username=username,
            session_id=session_id,
            model=model,
            tool_choice=tool_choice,
            max_iterations=max_iterations,
            defer_final_stream=defer_final_stream,
            agent_scope=agent_scope,
            on_tool_event=on_tool_event,
            company_scope=company_scope,
        )
    try:
        return run_responses_agent(
            messages=messages,
            tools=tools,
            tool_executor=tool_executor,
            username=username,
            session_id=session_id,
            model=model,
            tool_choice=tool_choice,
            max_iterations=max_iterations,
            prompt_cache_key=prompt_cache_key,
            defer_final_stream=defer_final_stream,
            agent_scope=agent_scope,
            on_tool_event=on_tool_event,
            company_scope=company_scope,
        )
    except Exception as exc:
        fallback = run_chat_agent(
            messages=messages,
            tools=tools,
            tool_executor=tool_executor,
            username=username,
            session_id=session_id,
            model=model,
            tool_choice=tool_choice,
            max_iterations=max_iterations,
            defer_final_stream=defer_final_stream,
            agent_scope=agent_scope,
            on_tool_event=on_tool_event,
            company_scope=company_scope,
        )
        fallback.fallback_reason = str(exc)[:1000]
        fallback.runtime_path = "chat-fallback"
        return fallback


def make_run_id() -> str:
    return uuid.uuid4().hex
