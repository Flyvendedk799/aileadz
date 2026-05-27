"""Shared AI runtime for Futurematch employee and HR agents."""
from __future__ import annotations

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
_LOG_TABLES_READY = set()
_OPENAI_CLIENT: Optional[OpenAI] = None
_OPENAI_CLIENT_KEY = None


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
             fallback_reason, latency_ms, input_tokens, output_tokens, cached_tokens)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        _TOOL_CACHE[cache_key] = (time.time() + ttl, output)
    return result


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
) -> AgentRunResult:
    model = model or main_model()
    start = time.time()
    client = _openai_client()
    working = list(messages)
    tool_results: List[ToolCallResult] = []
    tool_messages: List[Dict[str, Any]] = []
    final_text = ""
    usage: Dict[str, Any] = {}
    current_tool_choice = tool_choice

    for _ in range(max_iterations):
        kwargs = {"model": model, "messages": working, "stream": False}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = chat_tool_choice(current_tool_choice)
        resp = client.chat.completions.create(**kwargs)
        usage = _usage_to_dict(getattr(resp, "usage", None)) or usage
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            final_text = msg.content or ""
            break
        assistant_msg = _assistant_message_from_chat(msg)
        working.append(assistant_msg)
        for tc in calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            args = sanitize_args_for_tool(name, args, tools)
            result = _execute_one_tool(
                executor=tool_executor,
                name=name,
                call_id=tc.id,
                arguments=args,
                username=username,
                session_id=session_id,
            )
            tool_results.append(result)
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": result.output,
            }
            tool_messages.append(tool_msg)
            working.append(tool_msg)
        current_tool_choice = "auto"
    else:
        final_text = "Jeg kunne ikke færdiggøre værktøjsflowet. Prøv at stille spørgsmålet lidt mere konkret."

    return AgentRunResult(
        text=final_text,
        messages=working,
        tool_results=tool_results,
        tool_messages=tool_messages,
        runtime="chat",
        usage=usage,
        latency_ms=int((time.time() - start) * 1000),
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
) -> AgentRunResult:
    model = model or main_model()
    start = time.time()
    client = _openai_client()
    input_items = _messages_to_responses_input(messages)
    response_tools = [to_responses_tool(tool) for tool in tools]
    tool_results: List[ToolCallResult] = []
    tool_messages: List[Dict[str, Any]] = []
    usage: Dict[str, Any] = {}
    previous_response_id = None
    final_text = ""
    raw_response = None
    response_id = ""

    for _ in range(max_iterations):
        kwargs = {
            "model": model,
            "input": input_items,
            "store": True,
            "stream": False,
        }
        if response_tools:
            kwargs["tools"] = response_tools
            kwargs["tool_choice"] = responses_tool_choice(tool_choice)
            kwargs["parallel_tool_calls"] = True
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
            kwargs["input"] = input_items
        resp = client.responses.create(**kwargs)
        raw_response = resp
        response_id = getattr(resp, "id", "") or response_id
        previous_response_id = response_id
        usage = _usage_to_dict(getattr(resp, "usage", None)) or usage
        calls = _response_tool_calls(resp)
        if not calls:
            final_text = _response_text(resp)
            break

        outputs = []
        from concurrent.futures import ThreadPoolExecutor

        parallel_calls = [call for call in calls if is_parallel_safe(call["name"])]
        serial_calls = [call for call in calls if not is_parallel_safe(call["name"])]
        result_by_id: Dict[str, ToolCallResult] = {}
        if len(parallel_calls) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(parallel_calls))) as pool:
                futures = {}
                for call in parallel_calls:
                    args = sanitize_args_for_tool(call["name"], call["arguments"], tools)
                    futures[pool.submit(
                        _execute_one_tool,
                        executor=tool_executor,
                        name=call["name"],
                        call_id=call["id"],
                        arguments=args,
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
            args = sanitize_args_for_tool(call["name"], call["arguments"], tools)
            result_by_id[call["id"]] = _execute_one_tool(
                executor=tool_executor,
                name=call["name"],
                call_id=call["id"],
                arguments=args,
                username=username,
                session_id=session_id,
            )

        for call in calls:
            result = result_by_id[call["id"]]
            tool_results.append(result)
            tool_msg = {
                "role": "tool",
                "tool_call_id": result.call_id,
                "name": result.name,
                "content": result.output,
            }
            tool_messages.append(tool_msg)
            outputs.append({
                "type": "function_call_output",
                "call_id": result.call_id,
                "output": result.output,
            })
        input_items = outputs
        tool_choice = "auto"
    else:
        final_text = "Jeg kunne ikke færdiggøre værktøjsflowet. Prøv at stille spørgsmålet lidt mere konkret."

    return AgentRunResult(
        text=final_text,
        messages=messages + tool_messages + [{"role": "assistant", "content": final_text}],
        tool_results=tool_results,
        tool_messages=tool_messages,
        response_id=response_id,
        runtime="responses",
        usage=usage,
        latency_ms=int((time.time() - start) * 1000),
        raw_response=raw_response,
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
        )
        fallback.fallback_reason = str(exc)[:1000]
        return fallback


def make_run_id() -> str:
    return uuid.uuid4().hex
