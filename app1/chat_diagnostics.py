"""Correlate visible chat IDs, complete streamed turns and existing runtime logs.

Captured transcripts use ai_debug_logs and its existing seven-day retention.
Only the platform-admin endpoint exports this data; an ID is not authorization.
"""
import codecs
import json
import logging
import re
import time
import traceback
import uuid

from flask import current_app, g, session, stream_with_context

logger = logging.getLogger(__name__)
_CHAT_ID = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")


def valid_chat_id(value):
    return isinstance(value, str) and bool(_CHAT_ID.fullmatch(value))


def bind_chat(chat_id):
    """Bind the server-resolved ID to request logs, never a client-supplied ID."""
    g.chat_id = chat_id


def _record(chat_id, step, data):
    try:
        from app1.memory_store import log_debug
        if not log_debug(chat_id, step, data):
            logger.warning("Chat diagnostics could not be stored", extra={"chat_id": chat_id})
    except Exception:
        logger.warning("Chat diagnostics could not be stored", extra={"chat_id": chat_id})


def _diagnostic_event(value):
    # Confirmation tokens and authentication fields must not enter a shareable export.
    if isinstance(value, dict):
        return {key: "[redacted]" if key.lower() in {
            "token", "access_token", "refresh_token", "api_key", "password", "authorization"
        } else _diagnostic_event(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_diagnostic_event(item) for item in value]
    return value


def record_failure(chat_id, exc):
    """Persist a sanitized exception under the same ID as the visible chat."""
    try:
        from ai_runtime import _redact_pii
        detail = _redact_pii("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        _record(chat_id, "chat_error", {"error_type": type(exc).__name__, "detail": detail,
                                      "request_id": getattr(g, "request_id", "-"),
                                      "turn_id": getattr(g, "chat_turn_id", None)})
        logger.warning("Chat provider failed: %s", detail, extra={"chat_id": chat_id})
    except Exception:
        logger.warning("Chat provider failed (%s)", type(exc).__name__, extra={"chat_id": chat_id})


def instrument_response(response, *, chat_id, query, scope, company_id=None):
    """Observe SSE without changing its content; capture partial turns on close."""
    bind_chat(chat_id)
    turn_id = uuid.uuid4().hex
    g.chat_turn_id = turn_id
    request_id = getattr(g, "request_id", "-")
    context = {"turn_id": turn_id, "request_id": request_id, "scope": scope}
    _record(chat_id, "chat_turn_start", {
        **context, "user_message": query,
        "username": session.get("user") if scope in ("employee", "hr") else None,
        "company_id": company_id if scope == "widget" else session.get("company_id"),
    })
    response.headers["X-Chat-ID"] = chat_id
    response.headers["X-Chat-Scope"] = scope
    source = response.response

    def capture():
        decoder = codecs.getincrementaldecoder("utf-8")()
        pending, text, events = "", [], []
        outcome, recorded = "interrupted", False
        started = time.monotonic()

        def finish():
            nonlocal recorded
            if recorded:
                return
            recorded = True
            _record(chat_id, "chat_turn_end", {
                **context, "assistant_text": "".join(text), "events": events,
                "outcome": outcome, "elapsed_ms": int((time.monotonic() - started) * 1000),
            })
            logger.info("Chat turn %s %s", turn_id, outcome, extra={"chat_id": chat_id})

        def observe(frame):
            nonlocal outcome
            lines = [line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:")]
            if not lines:
                return
            raw = "\n".join(lines).strip()
            if raw == "[DONE]":
                if outcome == "interrupted":
                    outcome = "completed"
                finish()
                return
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                return
            if not isinstance(event, dict):
                return
            kind = event.get("type")
            if kind in ("chunk", "text") and isinstance(event.get("content"), str):
                text.append(event["content"])
            elif kind not in ("ping", "thinking", "done"):
                events.append(_diagnostic_event(event))
            if kind == "error":
                outcome = "error"
            elif kind == "fallback":
                outcome = "fallback"
            elif kind == "done":
                if outcome == "interrupted":
                    outcome = "completed"
                finish()

        try:
            yield "data: " + json.dumps({"type": "ping", "chat_id": chat_id, **context}) + "\n\n"
            for chunk in source:
                pending += decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
                frames = re.split(r"\r?\n\r?\n", pending)
                pending = frames.pop()
                for frame in frames:
                    observe(frame)
                yield chunk
            pending += decoder.decode(b"", final=True)
            if pending:
                observe(pending)
        except GeneratorExit:
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            finish()
            try:
                if hasattr(source, "close"):
                    source.close()
            finally:
                from db_compat import close_flask_mysql_connection
                close_flask_mysql_connection()

    response.response = stream_with_context(capture())
    return response


def load_diagnostics(chat_id):
    """Read every available source for an exact ID; report unavailable sources."""
    from app1.memory_store import get_debug_logs_for_session
    import MySQLdb.cursors

    result = {"chat_id": chat_id, "warnings": [], "debug_retention_days": 7}
    try:
        result["logs"] = get_debug_logs_for_session(chat_id, strict=True)
    except Exception:
        result["logs"] = []
        result["warnings"].append("Debug logs unavailable")

    queries = {
        "conversations": "SELECT username, mode, title, messages, updated_at FROM conversation_history WHERE session_id = %s ORDER BY id",
        "runs": "SELECT * FROM ai_agent_runs WHERE session_id = %s ORDER BY created_at, id",
        "tool_runs": "SELECT * FROM ai_tool_runs WHERE session_id = %s ORDER BY created_at, id",
    }
    for name, sql in queries.items():
        cur = None
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute(sql, (chat_id,))
            result[name] = list(cur.fetchall() or [])
        except Exception:
            result[name] = []
            result["warnings"].append(f"{name} unavailable")
            try:
                current_app.mysql.connection.rollback()
            except Exception:
                pass
        finally:
            if cur:
                cur.close()
    for conversation in result["conversations"]:
        if isinstance(conversation.get("messages"), str):
            try:
                conversation["messages"] = json.loads(conversation["messages"])
            except ValueError:
                result["warnings"].append("Stored conversation could not be decoded")
    turns = {}
    for entry in result["logs"]:
        data = entry.get("data") or {}
        if not isinstance(data, dict) or entry.get("step") not in ("chat_turn_start", "chat_turn_end"):
            continue
        turn_id = data.get("turn_id")
        if not turn_id:
            continue
        turn = turns.setdefault(turn_id, {"turn_id": turn_id, "outcome": "unfinished_or_running"})
        turn.update(data)
        turn["started_at" if entry["step"] == "chat_turn_start" else "ended_at"] = entry["timestamp"]
    result["turns"] = list(turns.values())
    if not result["turns"]:
        result["warnings"].append("No captured turns: this chat may predate capture or its debug logs may have expired")
    return result
