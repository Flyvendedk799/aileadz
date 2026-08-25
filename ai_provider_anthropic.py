"""Anthropic (Claude) runtime for the Futurematch AI agent.

This is the Claude-side twin of ``run_chat_agent`` in ``ai_runtime``. It is a
THIRD runtime alongside ``chat`` and ``responses``: ``run_agent_with_fallback``
dispatches here when :func:`ai_provider.provider` returns ``anthropic``.

Design rules
------------
1. **The canonical conversation history stays chat-shaped.** Everything else in
   the codebase — ``compact_messages_for_api``, ``estimate_messages_tokens``,
   ``_sanitize_tool_sequence``, conversation resume, telemetry, and the OpenAI
   fallback path — assumes the OpenAI ``{role, content, tool_calls, tool_call_id}``
   shape. We convert to Anthropic's block format only at the API boundary and
   convert the response straight back, so a session can be served by either
   provider without migrating stored history.

2. **No ``temperature`` / ``top_p`` / ``top_k``.** Those parameters are REMOVED
   on Claude Opus 5 / Sonnet 5 / Opus 4.6+ and return a 400. The intent the
   OpenAI path expressed with ``temperature=0.2`` on tool-deciding turns is
   expressed here with ``output_config.effort`` instead (see :func:`_effort_for`).

3. **``max_tokens`` gets a floor — a different one per turn kind.** On Claude,
   thinking tokens are charged against ``max_tokens``, and adaptive thinking is
   ON by default on Opus 5, so every OpenAI-era output cap is too tight here.
   :func:`_resolve_max_tokens` raises the ceiling to one of two floors:

   * *tool-deciding* turns → ``ANTHROPIC_MIN_MAX_TOKENS`` (default 4096). The
     OpenAI cap (320 tokens, see ``max_output_tokens_for_turn``) would be spent
     on thinking alone and the turn would stop at ``max_tokens`` with no
     ``tool_use`` block — breaking the agent loop.
   * *answer* turns → ``ANTHROPIC_ANSWER_MAX_TOKENS`` (default 16000). Reasoning
     over long tool output (course search, profile analysis) routinely spends
     several thousand thinking tokens BEFORE the first visible token, so the
     tool-turn floor truncates the answer mid-sentence — or leaves it empty.

   A ceiling is not a charge: only tokens actually generated are billed, answer
   length is governed by the prompt, and ``effort: low`` keeps tool turns short.
   Turns that do stop at ``max_tokens`` anyway are logged, never silently served.

4. **System prompt moves to the top-level ``system`` parameter** as a list of
   text blocks, with a ``cache_control`` breakpoint on the first (static) block.
   ``consolidate_system_layers`` already guarantees ``messages[0]`` is the
   byte-stable static prompt and that all volatile layers live in a separate
   later message, so this lands the breakpoint exactly where it should be. The
   cache prefix covers ``tools`` + ``system[0]``.

5. **Tool results become ``tool_result`` blocks in a single user message.**
   Splitting them across messages trains the model out of parallel tool calls.

Embeddings are deliberately not covered: Anthropic has no embeddings API, so
RAG retrieval stays on OpenAI in every configuration (see ``ai_provider``).
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import ai_provider
import ai_runtime
from ai_tool_registry import anthropic_tool_choice, to_anthropic_tool

logger = logging.getLogger(__name__)

_CLIENT = None
_CLIENT_KEY: Optional[Tuple[Any, ...]] = None
_CLIENT_LOCK = threading.Lock()

# Shadow-mode concurrency guard: comparison runs must never starve the request
# path or fan out unbounded under load.
_SHADOW_SLOTS = threading.BoundedSemaphore(2)

# Danish user-facing text for a Claude safety refusal (stop_reason="refusal").
_REFUSAL_DA = (
    "Jeg kan ikke besvare dette spørgsmål. Prøv at omformulere det, "
    "eller spørg om noget andet."
)


# --- Client -------------------------------------------------------------------

def anthropic_available() -> bool:
    return ai_provider.anthropic_configured()


def client():
    """Cached Anthropic client. Raises RuntimeError when unusable."""
    global _CLIENT, _CLIENT_KEY
    try:
        import anthropic
    except Exception as exc:  # pragma: no cover - depends on deploy
        raise RuntimeError(
            "anthropic-pakken er ikke installeret (pip install anthropic)"
        ) from exc
    import ai_secrets

    api_key = ai_secrets.get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY er ikke sat")
    try:
        timeout = float(os.getenv("AI_ANTHROPIC_TIMEOUT_SECONDS",
                                  os.getenv("AI_OPENAI_TIMEOUT_SECONDS", "45")))
    except ValueError:
        timeout = 45.0
    try:
        max_retries = int(os.getenv("AI_ANTHROPIC_MAX_RETRIES", "1"))
    except ValueError:
        max_retries = 1
    # id(anthropic.Anthropic) keeps monkeypatched test doubles from being served
    # a stale cached client, mirroring _openai_client()'s cache key.
    cache_key = (id(anthropic.Anthropic), api_key, timeout, max_retries)
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_KEY != cache_key:
            _CLIENT = anthropic.Anthropic(
                api_key=api_key, timeout=timeout, max_retries=max_retries
            )
            _CLIENT_KEY = cache_key
        return _CLIENT


def _error_body(exc: Exception) -> Dict[str, Any]:
    """The parsed ``error`` object from an Anthropic APIStatusError, if any."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return error
    return {}


def _error_code(exc: Exception) -> str:
    details = _error_body(exc).get("details")
    if isinstance(details, dict):
        return str(details.get("error_code") or "")
    return ""


def is_spend_limit(exc: Exception) -> bool:
    """Distinguish the monthly spend cap from a real rate limit.

    Both arrive as HTTP 429 with ``type: "rate_limit_error"``, but the spend cap
    carries ``details.error_code == "enforced_spend_limit_reached"`` and NO
    ``retry-after`` header: access does not come back until the next month (or a
    higher tier), so every retry is wasted RPM and "try a shorter question" is
    the wrong thing to tell the user.
    """
    if _error_code(exc) == "enforced_spend_limit_reached":
        return True
    message = str(_error_body(exc).get("message") or str(exc)).lower()
    return "usage limits" in message and "threshold" in message


def retry_after_seconds(exc: Exception) -> Optional[float]:
    """The server's ``retry-after``, in seconds. Retrying earlier always fails."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def is_permanent_request_error(exc: Exception) -> bool:
    """A 4xx the request will NEVER pass on retry — i.e. our bug, not an outage.

    The canonical case is a tool JSON-Schema Anthropic refuses (HTTP 400
    ``invalid_request_error``). It looks like "Claude is down" to a bare
    ``except Exception`` fallback, so every tool-carrying turn silently reroutes
    to OpenAI and the defect never surfaces: the admin UI still says Anthropic,
    the bill says OpenAI. These must be logged loudly and labelled distinctly in
    telemetry, even though we still serve the turn on the other provider.
    """
    status = getattr(exc, "status_code", None)
    if status is None or not (400 <= int(status) < 500):
        return False
    if int(status) in (408, 409, 429):  # transient by definition
        return False
    error_type = str(_error_body(exc).get("type") or "")
    if error_type in ("invalid_request_error", "not_found_error"):
        return True
    # authentication/permission failures are config problems, not outages, but
    # falling back on them IS the intended behaviour — treat them as transient.
    return False


def request_id(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return str(body.get("request_id") or "")
    return str(getattr(exc, "request_id", "") or "")


def _is_rate_limit(exc: Exception) -> bool:
    """Typed check first, then ai_runtime's string heuristic.

    The spend cap is deliberately NOT a rate limit here: it is not transient, so
    the retry ladder must not spend two more requests discovering that.
    """
    if is_spend_limit(exc):
        return False
    try:
        import anthropic

        if isinstance(exc, anthropic.RateLimitError):
            return True
        if isinstance(exc, anthropic.APIStatusError) and getattr(exc, "status_code", 0) == 429:
            return True
    except Exception:
        pass
    return ai_runtime._is_rate_limit_error(exc)


# --- Model tiering ------------------------------------------------------------

def _is_fast_tier(model: str) -> bool:
    return (model or "").strip() == ai_provider.anthropic_fast_model()


def _supports_effort(model: str) -> bool:
    """``output_config.effort`` is not accepted by the Haiku 4.5 tier."""
    return "haiku" not in (model or "").lower()


def _effort_for(model: str, has_tools: bool) -> Optional[str]:
    """Map our former temperature intent onto Claude's effort dial.

    Tool-deciding turns want short, deterministic output (the OpenAI path pinned
    temperature=0.2 for this) -> ``low``. Final answers on the main tier want
    quality -> ``high`` unless ANTHROPIC_EFFORT overrides it.
    """
    if not _supports_effort(model):
        return None
    override = (os.getenv("ANTHROPIC_EFFORT") or "").strip().lower()
    if override in ("low", "medium", "high", "xhigh", "max"):
        return override
    if has_tools or _is_fast_tier(model):
        return "low"
    return "high"


def _min_max_tokens() -> int:
    """Floor for a tool-DECIDING turn: room for thinking plus a tool_use block."""
    try:
        return max(1024, int(os.getenv("ANTHROPIC_MIN_MAX_TOKENS", "4096")))
    except ValueError:
        return 4096


def _answer_max_tokens() -> int:
    """Floor for an ANSWER turn: room for thinking plus the whole answer.

    Never below the tool-turn floor. See design rule 3 in the module docstring
    for why the two differ.
    """
    try:
        value = int(os.getenv("ANTHROPIC_ANSWER_MAX_TOKENS", "16000"))
    except ValueError:
        value = 16000
    return max(_min_max_tokens(), value)


def _resolve_max_tokens(
    requested: Optional[int],
    has_tools: bool,
    *,
    answer_turn: Optional[bool] = None,
) -> int:
    """Apply the thinking-aware floor. See design rule 3 in the module docstring.

    ``answer_turn`` defaults to "no tools declared" — a tool-less request can
    only be an answer turn — and is passed explicitly by the agent loop, where a
    turn that carries tools may still produce the final answer (RT-02).
    """
    base = int(requested) if requested else ai_runtime.max_output_tokens_for_turn(has_tools)
    if answer_turn is None:
        answer_turn = not has_tools
    return max(base, _answer_max_tokens() if answer_turn else _min_max_tokens())


def _is_truncated(resp: Any) -> bool:
    """The turn hit ``max_tokens``: thinking and/or text was cut off."""
    return getattr(resp, "stop_reason", None) == "max_tokens"


# --- Message conversion -------------------------------------------------------

def split_system(prepared: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split leading system layers into Anthropic ``system`` text blocks.

    A ``cache_control`` breakpoint is placed on the first block only: that is the
    static, byte-stable prompt that ``consolidate_system_layers`` guarantees at
    ``messages[0]``, so the cached prefix (tools + static system) survives every
    turn. Volatile layers land in later, uncached blocks.
    """
    system_blocks: List[Dict[str, Any]] = []
    rest: List[Dict[str, Any]] = []
    leading = True
    for msg in prepared or []:
        role = msg.get("role")
        if role == "system":
            text = str(msg.get("content") or "").strip()
            if not text:
                continue
            if leading:
                system_blocks.append({"type": "text", "text": text})
            else:
                # A system layer that appears mid-conversation: fold it into the
                # dialogue rather than dropping instructions on the floor.
                rest.append({"role": "user", "content": f"[SYSTEM]\n{text}"})
            continue
        leading = False
        rest.append(msg)
    if system_blocks:
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}
    return system_blocks, rest


def _tool_args_to_object(raw: Any) -> Dict[str, Any]:
    """Anthropic wants ``input`` as an object; chat history stores a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _prune_orphans(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop unpaired ``tool_use`` / ``tool_result`` blocks.

    Anthropic 400s on a ``tool_result`` with no matching ``tool_use`` and on a
    ``tool_use`` that is never answered. ``_sanitize_tool_sequence`` already
    pairs the chat-shaped history, but token trimming runs on a different pass,
    so this is a cheap last line of defence at the API boundary.
    """
    issued = set()
    forward: List[Dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use" and block.get("id"):
                    issued.add(block["id"])
            forward.append(msg)
        elif msg.get("role") == "user" and isinstance(content, list):
            kept = [
                b for b in content
                if b.get("type") != "tool_result" or b.get("tool_use_id") in issued
            ]
            if kept:
                forward.append({"role": "user", "content": kept})
        else:
            forward.append(msg)

    answered = {
        b.get("tool_use_id")
        for msg in forward if isinstance(msg.get("content"), list)
        for b in msg["content"] if b.get("type") == "tool_result"
    }
    final: List[Dict[str, Any]] = []
    for msg in forward:
        content = msg.get("content")
        if msg.get("role") == "assistant" and isinstance(content, list):
            kept = [
                b for b in content
                if b.get("type") != "tool_use" or b.get("id") in answered
            ]
            if kept:
                final.append({"role": "assistant", "content": kept})
        else:
            final.append(msg)

    # The conversation must open on a user turn.
    while final and final[0].get("role") != "user":
        final.pop(0)
    return final


def to_anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chat-shaped history -> Anthropic ``messages``.

    Consecutive ``role: "tool"`` messages are merged into ONE user message
    carrying every ``tool_result`` block, which is what keeps parallel tool use
    working on subsequent turns.
    """
    out: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    def flush() -> None:
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for msg in messages or []:
        role = msg.get("role")
        if role == "tool":
            tool_use_id = msg.get("tool_call_id") or msg.get("id") or ""
            if not tool_use_id:
                continue
            pending.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                # An empty content block is rejected; tools legitimately return
                # empty payloads, so substitute a marker the model can read.
                "content": str(msg.get("content") or "").strip() or "(tomt resultat)",
            })
            continue
        flush()
        if role == "user":
            text = str(msg.get("content") or "").strip()
            if text:
                out.append({"role": "user", "content": text})
        elif role == "assistant":
            blocks: List[Dict[str, Any]] = []
            text = str(msg.get("content") or "").strip()
            if text:
                blocks.append({"type": "text", "text": text})
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict) or not call.get("id"):
                    continue
                fn = call.get("function") or {}
                blocks.append({
                    "type": "tool_use",
                    "id": call["id"],
                    "name": fn.get("name") or "",
                    "input": _tool_args_to_object(fn.get("arguments")),
                })
            if blocks:
                out.append({"role": "assistant", "content": blocks})
    flush()
    return _prune_orphans(out)


# --- Response adapters --------------------------------------------------------

def response_text(resp: Any) -> str:
    parts = []
    for block in getattr(resp, "content", None) or []:
        if (getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)) == "text":
            value = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
            if value:
                parts.append(value)
    return "".join(parts)


def response_tool_calls(resp: Any) -> List[Dict[str, Any]]:
    """Return ``[{"id", "name", "arguments"}]`` — the shape _execute_tool_calls_parallel takes."""
    calls = []
    for block in getattr(resp, "content", None) or []:
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if btype != "tool_use":
            continue
        get = (lambda k: block.get(k)) if isinstance(block, dict) else (lambda k: getattr(block, k, None))
        raw_input = get("input")
        calls.append({
            "id": get("id"),
            "name": get("name") or "",
            "arguments": raw_input if isinstance(raw_input, dict) else _tool_args_to_object(raw_input),
        })
    return calls


def assistant_message_from_response(resp: Any) -> Dict[str, Any]:
    """Anthropic response -> chat-shaped assistant message (design rule 1)."""
    msg: Dict[str, Any] = {"role": "assistant", "content": response_text(resp)}
    tool_calls = []
    for call in response_tool_calls(resp):
        if not call.get("id"):
            continue
        tool_calls.append({
            "id": call["id"],
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call["arguments"], ensure_ascii=False, default=str),
            },
        })
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def normalize_usage(usage: Any) -> Dict[str, Any]:
    """Anthropic usage -> the dict shape ai_runtime / ai_cost_model expect.

    Anthropic reports ``cache_read_input_tokens`` where the OpenAI Responses API
    reports ``input_tokens_details.cached_tokens``; ``_cached_tokens_from_usage``
    reads the latter, so we mirror it. Note ``input_tokens`` EXCLUDES cached
    tokens on Anthropic (they are billed separately), whereas OpenAI counts them
    as a subset — we therefore add them back so the two providers' rows in
    ``ai_agent_runs`` mean the same thing.
    """
    if not usage:
        return {}
    if hasattr(usage, "model_dump"):
        try:
            usage = usage.model_dump()
        except Exception:
            pass
    if not isinstance(usage, dict):
        usage = {
            key: getattr(usage, key, None)
            for key in ("input_tokens", "output_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens")
        }

    def _int(key: str) -> int:
        try:
            return int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    cache_read = _int("cache_read_input_tokens")
    cache_write = _int("cache_creation_input_tokens")
    uncached_input = _int("input_tokens")
    return {
        "input_tokens": uncached_input + cache_read + cache_write,
        "output_tokens": _int("output_tokens"),
        "cached_tokens": cache_read,
        "input_tokens_details": {"cached_tokens": cache_read},
        "cache_creation_input_tokens": cache_write,
    }


# --- API calls ----------------------------------------------------------------

def flatten_tool_blocks(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Render ``tool_use`` / ``tool_result`` blocks as plain text.

    Anthropic rejects any request that carries tool blocks without a ``tools``
    definition. Our tool-LESS calls (the deferred final-answer stream, the
    forced-final summary, one-shot completions) are handed the full agent
    history — which is exactly the history that contains those blocks, and whose
    tool OUTPUT the model needs in order to write the answer. Dropping the
    blocks would drop the results, so we inline them as text instead.

    This is the Claude-side equivalent of OpenAI simply tolerating role="tool"
    messages on a tool-less completion.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        parts: List[str] = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text") or "")
            elif btype == "tool_use":
                args = json.dumps(block.get("input") or {}, ensure_ascii=False, default=str)
                parts.append(f"[VÆRKTØJSKALD {block.get('name') or ''}({args})]")
            elif btype == "tool_result":
                parts.append(f"[VÆRKTØJSRESULTAT]\n{block.get('content') or ''}")
        text = "\n\n".join(part for part in parts if part).strip()
        if text:
            out.append({"role": msg.get("role"), "content": text})
    while out and out[0].get("role") != "user":
        out.pop(0)
    return out


def _build_kwargs(
    *,
    model: str,
    prepared: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    tool_choice: Any,
    max_tokens: Optional[int],
    answer_turn: Optional[bool] = None,
) -> Dict[str, Any]:
    system_blocks, rest = split_system(prepared)
    messages = to_anthropic_messages(rest)
    if not tools:
        messages = flatten_tool_blocks(messages)
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": _resolve_max_tokens(
            max_tokens, bool(tools), answer_turn=answer_turn
        ),
    }
    if system_blocks:
        kwargs["system"] = system_blocks
    if tools:
        kwargs["tools"] = [to_anthropic_tool(tool) for tool in tools]
        kwargs["tool_choice"] = anthropic_tool_choice(tool_choice)
    effort = _effort_for(model, bool(tools))
    if effort:
        kwargs["output_config"] = {"effort": effort}
    # NB: no temperature / top_p / top_k — removed on current Claude models.
    return kwargs


def messages_create_with_resilience(
    *,
    model: str,
    source_messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    tool_choice: Any,
    output_cap: Optional[int] = None,
    answer_turn: Optional[bool] = None,
) -> Tuple[Any, str, List[Dict[str, Any]]]:
    """Mirror of ``_chat_completion_with_resilience`` for the Messages API.

    ``answer_turn`` picks the ``max_tokens`` floor (design rule 3); leave it
    ``None`` to infer it from whether tools were declared.
    """
    fast = ai_provider.anthropic_fast_model()
    attempts = [(model, False), (model, True), (fast, True)]
    last_exc: Optional[Exception] = None
    for attempt_index, (attempt_model, aggressive) in enumerate(attempts):
        prepared = ai_runtime.prepare_messages_for_turn(source_messages, aggressive=aggressive)
        kwargs = _build_kwargs(
            model=attempt_model,
            prepared=prepared,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=output_cap,
            answer_turn=answer_turn,
        )
        if not kwargs["messages"]:
            raise RuntimeError("ingen brugerbesked at sende til Claude")
        try:
            return client().messages.create(**kwargs), attempt_model, prepared
        except Exception as exc:
            last_exc = exc
            if not _is_rate_limit(exc):
                if is_spend_limit(exc):
                    logger.error(
                        "Anthropic SPEND CAP reached (not a rate limit, not "
                        "retryable): %s", _error_body(exc).get("message") or exc,
                    )
                raise
            ai_runtime._note_rate_limit_hit()
            # The 429 body names the limit that was hit (ITPM / OTPM / RPM) and
            # is the only way to tell them apart from the outside — without this
            # the turn surfaces a generic Danish apology and nothing else.
            server_wait = retry_after_seconds(exc)
            logger.warning(
                "Anthropic 429 (model=%s, attempt=%d/%d, retry_after=%s): %s",
                attempt_model, attempt_index + 1, len(attempts),
                "%ss" % server_wait if server_wait is not None else "absent",
                _error_body(exc).get("message") or exc,
            )
            backoff = ai_runtime._backoff_wait_seconds(attempt_index)
            # "Earlier retries will fail" — a blind backoff shorter than the
            # server's retry-after just burns another request against RPM.
            wait = max(backoff, server_wait or 0.0)
            wait = min(wait, ai_runtime.rate_limit_backoff_cap_seconds())
            if wait > 0:
                time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("anthropic messages.create failed without exception")


def run_direct(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Tool-less one-shot completion (Claude twin of ``run_direct_completion``)."""
    chosen = model or (
        ai_provider.anthropic_fast_model() if ai_runtime.in_rate_limit_cooldown()
        else ai_provider.anthropic_main_model()
    )
    resp, _model, _prepared = messages_create_with_resilience(
        model=chosen,
        source_messages=messages,
        tools=None,
        tool_choice="auto",
        output_cap=max_tokens or ai_runtime.max_output_tokens(),
        answer_turn=True,
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        return _REFUSAL_DA
    if _is_truncated(resp):
        logger.warning(
            "Anthropic run_direct stopped at max_tokens (model=%s, max_tokens=%d)",
            _model, _resolve_max_tokens(max_tokens or ai_runtime.max_output_tokens(),
                                        False, answer_turn=True),
        )
    return response_text(resp).strip()


def iter_stream(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
):
    """Stream final assistant text (Claude twin of ``iter_completion_stream``)."""
    chosen = model or (
        ai_provider.anthropic_fast_model() if ai_runtime.in_rate_limit_cooldown()
        else ai_provider.anthropic_main_model()
    )
    fast = ai_provider.anthropic_fast_model()
    main = ai_provider.anthropic_main_model()
    models_to_try = []
    for candidate in (chosen, fast, main):
        if candidate and candidate not in models_to_try:
            models_to_try.append(candidate)

    aggressive = ai_runtime.in_rate_limit_cooldown()
    for model_name in models_to_try:
        prepared = ai_runtime.prepare_messages_for_turn(messages, aggressive=aggressive)
        kwargs = _build_kwargs(
            model=model_name,
            prepared=prepared,
            tools=None,
            tool_choice="auto",
            max_tokens=max_tokens or ai_runtime.max_output_tokens(),
            answer_turn=True,
        )
        try:
            with client().messages.stream(**kwargs) as stream:
                for chunk in stream.text_stream:
                    if chunk:
                        yield chunk
                # Tokens are already on the wire, so this cannot be retried —
                # but a cut-off answer must not be invisible in the logs.
                try:
                    if _is_truncated(stream.get_final_message()):
                        logger.warning(
                            "Anthropic final stream stopped at max_tokens "
                            "(model=%s, max_tokens=%s) - answer is cut off",
                            model_name, kwargs["max_tokens"],
                        )
                except Exception:
                    pass
            return
        except Exception as exc:
            if _is_rate_limit(exc):
                ai_runtime._note_rate_limit_hit()
                aggressive = True
                continue
            raise


def classify_intent(query: str, system_prompt: str, *, fallback: str) -> str:
    """Cheap single-label classification on the fast tier.

    Kept separate from ``run_direct`` because the router needs a per-request
    timeout and must never propagate an error into the SSE turn.
    """
    model = ai_provider.anthropic_fast_model()
    try:
        scoped = client().with_options(timeout=ai_runtime._router_timeout_seconds())
    except Exception:
        try:
            scoped = client()
        except Exception:
            return fallback
    try:
        kwargs: Dict[str, Any] = {
            "model": model,
            "system": [{"type": "text", "text": system_prompt}],
            "messages": [{"role": "user", "content": query[:2000]}],
            # A 4-token cap is unsafe on a thinking model; the floor keeps room
            # for the label. Effort is omitted on the Haiku tier.
            "max_tokens": 64 if not _supports_effort(model) else _min_max_tokens(),
        }
        effort = _effort_for(model, True)  # router turn: shortest, cheapest tier
        if effort:
            kwargs["output_config"] = {"effort": effort}
        resp = scoped.messages.create(**kwargs)
        return ai_runtime._parse_router_label(response_text(resp), fallback)
    except Exception:
        return fallback


# --- Agent loop ---------------------------------------------------------------

def run_anthropic_agent(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tool_executor,
    username: Optional[str],
    session_id: Optional[str],
    model: Optional[str] = None,
    tool_choice: Any = "auto",
    max_iterations: int = 5,
    defer_final_stream: bool = False,
    agent_scope: str = "employee",
    on_tool_event=None,
    company_scope: Optional[str] = None,
):
    """Claude tool loop. Same guardrails and return contract as ``run_chat_agent``.

    Keeps every ai_runtime safety net: repeat-call circuit breaker, per-run tool
    cap, cost/token ceiling, forced-final on loop exhaustion, and the RT-02
    captured-final optimisation.
    """
    AgentRunResult = ai_runtime.AgentRunResult
    model = model or ai_provider.anthropic_main_model()
    start = time.time()
    source_messages = list(messages)
    tool_results = []
    tool_messages: List[Dict[str, Any]] = []
    final_text = ""
    usage: Dict[str, Any] = {}
    current_tool_choice = tool_choice
    compaction_level = ai_runtime.compaction_level_for_messages(messages)
    run_usage: Dict[str, Any] = {}
    seen_signatures: set = set()
    total_tool_calls = 0
    tool_call_cap = ai_runtime.max_tool_calls_per_run()
    forced_final = False

    for _ in range(max_iterations):
        resp, model, _prepared = messages_create_with_resilience(
            model=model,
            source_messages=source_messages,
            tools=tools,
            tool_choice=current_tool_choice,
            # RT-02: lift the tight tool-turn cap once a tool has run, so a
            # captured final answer is never truncated.
            output_cap=(ai_runtime.max_output_tokens()
                        if (tool_results and ai_runtime.capture_final_enabled()) else None),
            # ...and give that turn the answer-sized thinking headroom too: the
            # cap above is an OpenAI-era budget that the Claude floor overrides.
            answer_turn=bool(tool_results and ai_runtime.capture_final_enabled()),
        )
        iter_usage = normalize_usage(getattr(resp, "usage", None))
        usage = iter_usage or usage
        run_usage = ai_runtime._accumulate_usage(run_usage, iter_usage)

        stop_reason = getattr(resp, "stop_reason", None)
        if stop_reason == "refusal":
            return AgentRunResult(
                text=_REFUSAL_DA,
                messages=source_messages + [{"role": "assistant", "content": _REFUSAL_DA}],
                tool_results=tool_results,
                tool_messages=tool_messages,
                runtime="anthropic",
                usage=usage,
                latency_ms=int((time.time() - start) * 1000),
                compaction_level=compaction_level,
                runtime_path="anthropic-refusal",
            )

        calls = response_tool_calls(resp)
        if not calls:
            captured = response_text(resp) if ai_runtime.capture_final_enabled() else ""
            truncated = _is_truncated(resp)
            if truncated:
                # Visible in the logs instead of silently serving half an answer:
                # raise ANTHROPIC_ANSWER_MAX_TOKENS if this shows up regularly.
                logger.warning(
                    "Anthropic turn stopped at max_tokens (model=%s, tools_run=%d, "
                    "max_tokens=%d, output_tokens=%s) - answer regenerated",
                    model, len(tool_results),
                    _resolve_max_tokens(None, bool(tools), answer_turn=True),
                    (iter_usage or {}).get("output_tokens"),
                )
            if defer_final_stream:
                if captured.strip() and not truncated:
                    return AgentRunResult(
                        text=captured,
                        messages=source_messages,
                        tool_results=tool_results,
                        tool_messages=tool_messages,
                        runtime="anthropic",
                        usage=usage,
                        latency_ms=int((time.time() - start) * 1000),
                        needs_final_stream=False,
                        stream_messages=list(source_messages),
                        compaction_level=compaction_level,
                        runtime_path="anthropic",
                    )
                return AgentRunResult(
                    text="",
                    messages=source_messages,
                    tool_results=tool_results,
                    tool_messages=tool_messages,
                    runtime="anthropic",
                    usage=usage,
                    latency_ms=int((time.time() - start) * 1000),
                    needs_final_stream=True,
                    stream_messages=list(source_messages),
                    compaction_level=compaction_level,
                    runtime_path="anthropic",
                )
            if truncated:
                # Regenerate through the forced-final path (tool-less, answer
                # ceiling) rather than serving the sentence it stopped inside.
                forced_final = True
                break
            final_text = response_text(resp)
            break

        source_messages.append(assistant_message_from_response(resp))

        repeated = False
        for call in calls:
            sig = ai_runtime._tool_call_signature(call["name"], call["arguments"])
            if sig in seen_signatures:
                repeated = True
            seen_signatures.add(sig)
        total_tool_calls += len(calls)
        if repeated or total_tool_calls > tool_call_cap:
            forced_final = True
            break

        executed = ai_runtime._execute_tool_calls_parallel(
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
            tool_msg = {
                "role": "tool",
                "tool_call_id": result.call_id,
                "name": result.name,
                "content": ai_runtime._strip_heavy_tool_payload(result.output),
            }
            tool_results.append(result)
            tool_messages.append(tool_msg)
            source_messages.append(tool_msg)
        current_tool_choice = "auto"

        if ai_runtime._run_cost_exceeded(run_usage, model=model):
            return AgentRunResult(
                text=ai_runtime._OVER_BUDGET_DA,
                messages=source_messages + [{"role": "assistant",
                                             "content": ai_runtime._OVER_BUDGET_DA}],
                tool_results=tool_results,
                tool_messages=tool_messages,
                runtime="anthropic",
                usage=usage,
                latency_ms=int((time.time() - start) * 1000),
                compaction_level="over_budget",
                runtime_path="anthropic-over-budget",
            )
    else:
        forced_final = True

    runtime_path = "anthropic"
    if forced_final:
        runtime_path = "anthropic-forced-final"
        try:
            final_text = run_direct(source_messages, model=model)
        except Exception as exc:
            logger.warning("Anthropic forced-final failed: %s", exc)
            final_text = ""
        if not final_text:
            final_text = ("Jeg kunne ikke færdiggøre værktøjsflowet. "
                          "Prøv at stille spørgsmålet lidt mere konkret.")

    return AgentRunResult(
        text=final_text,
        messages=source_messages,
        tool_results=tool_results,
        tool_messages=tool_messages,
        runtime="anthropic",
        usage=usage,
        latency_ms=int((time.time() - start) * 1000),
        compaction_level=compaction_level,
        runtime_path=runtime_path,
    )


# --- Shadow comparison --------------------------------------------------------

def maybe_run_shadow(
    source_messages: List[Dict[str, Any]],
    *,
    session_id: Optional[str],
    username: Optional[str],
    agent_scope: str,
    app: Any = None,
) -> None:
    """Fire a sampled, tool-less Claude completion for comparison. Never blocks.

    SAFETY: shadow runs are deliberately TOOL-LESS. Re-running the tool loop
    would execute write tools (orders, profile mutations, HR writes) a second
    time, so shadow mode compares final-answer latency, tokens and cost — not
    tool selection. Results are written to ``ai_agent_runs`` with
    ``runtime='anthropic-shadow'`` and never reach the user.
    """
    try:
        if random.random() > ai_provider.shadow_sample_rate():
            return
        if not anthropic_available():
            return
    except Exception:
        return

    payload = list(source_messages)

    def _worker() -> None:
        if not _SHADOW_SLOTS.acquire(blocking=False):
            return
        try:
            started = time.time()
            resp, model, _prepared = messages_create_with_resilience(
                model=ai_provider.anthropic_main_model(),
                source_messages=payload,
                tools=None,
                tool_choice="auto",
                output_cap=ai_runtime.max_output_tokens(),
                answer_turn=True,
            )
            latency_ms = int((time.time() - started) * 1000)
            usage = normalize_usage(getattr(resp, "usage", None))
            if app is None:
                logger.info("[anthropic-shadow] model=%s latency=%sms usage=%s",
                            model, latency_ms, usage)
                return
            with app.app_context():
                ai_runtime.log_agent_run(
                    getattr(app, "mysql", None),
                    run_id=ai_runtime.make_run_id(),
                    session_id=session_id or "",
                    company_id=None,
                    username=username,
                    agent_scope=agent_scope,
                    runtime="anthropic-shadow",
                    model=model,
                    prompt_version=ai_runtime.PROMPT_VERSION,
                    toolset_version="",
                    tool_names=[],
                    response_id=getattr(resp, "id", "") or "",
                    status="ok",
                    fallback_reason="",
                    latency_ms=latency_ms,
                    usage=usage,
                    runtime_path="anthropic-shadow",
                )
        except Exception as exc:  # noqa: BLE001 - a shadow run must never surface
            logger.warning("[anthropic-shadow] comparison run failed: %s", exc)
        finally:
            _SHADOW_SLOTS.release()

    try:
        threading.Thread(target=_worker, name="anthropic-shadow", daemon=True).start()
    except Exception:
        pass
