"""Shared AI runtime for Futurematch employee and HR agents."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from openai import OpenAI

from db_compat import refresh_flask_mysql_connection
from ai_tool_registry import (
    chat_tool_choice,
    is_parallel_safe,
    responses_tool_choice,
    sanitize_args_for_tool,
    tool_cache_ttl,
    to_responses_tool,
    tool_name,
)


PROMPT_VERSION = "futurematch-ai-v3"
_TOOL_CACHE: Dict[str, Tuple[float, str]] = {}
_TOOL_CACHE_MAX = 512
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


def main_model() -> str:
    return os.getenv("AI_MAIN_MODEL", "gpt-4o")


def fast_model() -> str:
    return os.getenv("AI_FAST_MODEL", "gpt-4o-mini")


def runtime_mode() -> str:
    return os.getenv("AI_RUNTIME", "responses").lower().strip() or "responses"


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
    """Keep the static system prompt isolated for cache hits; merge dynamic layers."""
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
    return compact


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
    """Route easy turns to the fast model without sacrificing complex flows."""
    if in_rate_limit_cooldown():
        return fast_model()
    if prefer_quality and not in_rate_limit_cooldown():
        return main_model()
    if tool_count == 0 and intent in {"chit_chat", "needs_clarification"}:
        return fast_model()
    if token_estimate > int(tpm_budget() * 0.75):
        return fast_model()
    if intent in {"follow_up", "detail"} and token_estimate < 12000:
        return fast_model()
    return main_model()


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


def _is_rate_limit_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    return "rate_limit" in name or "429" in text or "tokens per min" in text or "tpm" in text


def _cache_tool_result(cache_key: str, ttl: int, output: str) -> None:
    if not cache_key or ttl <= 0:
        return
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
) -> Tuple[Any, str, List[Dict[str, Any]]]:
    attempts = [
        (model, False),
        (model, True),
        (fast_model(), True),
    ]
    last_exc: Optional[Exception] = None
    for attempt_model, aggressive in attempts:
        working = prepare_messages_for_turn(source_messages, aggressive=aggressive)
        kwargs: Dict[str, Any] = {
            "model": attempt_model,
            "messages": working,
            "stream": False,
            "max_tokens": max_output_tokens(),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = chat_tool_choice(tool_choice)
        try:
            return client.chat.completions.create(**kwargs), attempt_model, working
        except Exception as exc:
            last_exc = exc
            if not _is_rate_limit_error(exc):
                raise
            _note_rate_limit_hit()
            wait = rate_limit_retry_seconds()
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
    for attempt_model, payload_items, prev_id, _max_chars in attempts:
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
            "max_output_tokens": max_output_tokens(),
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
            return client.responses.create(**kwargs), attempt_model, working, payload
        except Exception as exc:
            last_exc = exc
            if not _is_rate_limit_error(exc):
                raise
            _note_rate_limit_hit()
            wait = rate_limit_retry_seconds()
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
) -> None:
    if not mysql or trace_sample_rate() <= 0:
        return
    try:
        refresh_flask_mysql_connection(mysql)
        ensure_ai_log_tables(mysql)
        cur = mysql.connection.cursor()
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        cur.execute(
            """
            INSERT INTO ai_agent_runs
            (run_id, session_id, company_id, username, agent_scope, runtime, model,
             prompt_version, toolset_version, tool_names, response_id, status,
             fallback_reason, latency_ms, input_tokens, output_tokens, cached_tokens,
             compaction_level, runtime_path, embedding_skipped)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ",".join(tool_names),
                response_id,
                status,
                fallback_reason,
                latency_ms,
                input_tokens,
                output_tokens,
                _cached_tokens_from_usage(usage),
                compaction_level,
                runtime_path,
                (1 if embedding_skipped else 0 if embedding_skipped is False else None),
            ),
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
                _safe_json(result.arguments),
                result.status,
                result.latency_ms,
                result_count,
                result.error,
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
) -> ToolCallResult:
    start = time.time()
    status = "ok"
    error = ""
    ttl = tool_cache_ttl(name)
    cache_key = ""
    if ttl > 0:
        cache_key = _safe_json({
            "name": name,
            "arguments": arguments,
            "username": username or "",
            "session_id": session_id or "",
        })
        cached = _TOOL_CACHE.get(cache_key)
        if cached and cached[0] > time.time():
            return ToolCallResult(
                call_id=call_id,
                name=name,
                arguments=arguments,
                output=cached[1],
                latency_ms=0,
                status="cached",
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

    if len(parallel_calls) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(parallel_calls))) as pool:
            futures = {}
            for call in parallel_calls:
                futures[pool.submit(
                    _execute_one_tool,
                    executor=tool_executor,
                    name=call["name"],
                    call_id=call["id"],
                    arguments=call["arguments"],
                    username=username,
                    session_id=session_id,
                )] = call["id"]
            for future, call_id in futures.items():
                result_by_id[call_id] = future.result()
    else:
        serial_calls = parallel_calls + serial_calls

    for call in serial_calls:
        if call["id"] in result_by_id:
            continue
        result_by_id[call["id"]] = _execute_one_tool(
            executor=tool_executor,
            name=call["name"],
            call_id=call["id"],
            arguments=call["arguments"],
            username=username,
            session_id=session_id,
        )

    return [result_by_id[call["id"]] for call in normalized if call["id"] in result_by_id]


def _messages_to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    converted = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            converted.append({"role": "developer", "content": msg.get("content") or ""})
        elif role in {"user", "assistant"}:
            converted.append({"role": role, "content": msg.get("content") or ""})
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

    for _ in range(max_iterations):
        resp, model, _prepared = _chat_completion_with_resilience(
            client,
            model=model,
            source_messages=source_messages,
            tools=tools,
            tool_choice=current_tool_choice,
        )
        usage = _usage_to_dict(getattr(resp, "usage", None)) or usage
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            if defer_final_stream:
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
        executed = _execute_tool_calls_parallel(
            calls=calls,
            tools=tools,
            tool_executor=tool_executor,
            username=username,
            session_id=session_id,
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
    else:
        final_text = "Jeg kunne ikke færdiggøre værktøjsflowet. Prøv at stille spørgsmålet lidt mere konkret."

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
        )
        raw_response = resp
        response_id = getattr(resp, "id", "") or response_id
        previous_response_id = response_id
        usage = _usage_to_dict(getattr(resp, "usage", None)) or usage
        calls = _response_tool_calls(resp)
        if not calls:
            if defer_final_stream:
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

        executed = _execute_tool_calls_parallel(
            calls=[{"id": c["id"], "name": c["name"], "arguments": c["arguments"]} for c in calls],
            tools=tools,
            tool_executor=tool_executor,
            username=username,
            session_id=session_id,
        )
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
    else:
        final_text = "Jeg kunne ikke færdiggøre værktøjsflowet. Prøv at stille spørgsmålet lidt mere konkret."

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
        )
        fallback.fallback_reason = str(exc)[:1000]
        fallback.runtime_path = "chat-fallback"
        return fallback


def make_run_id() -> str:
    return uuid.uuid4().hex
