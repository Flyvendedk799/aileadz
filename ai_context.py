"""Shared AI context, warmup, and lightweight completion helpers."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from ai_runtime import (
    AgentRunResult,
    choose_turn_model,
    compaction_level_for_messages,
    estimate_messages_tokens,
    fast_model,
    main_model,
    max_output_tokens,
    prepare_messages_for_turn,
    run_direct_completion,
    user_facing_error_message,
)


def few_shot_mode() -> str:
    return os.getenv("AI_FEW_SHOT", "compact").lower().strip() or "compact"


def summary_mode() -> str:
    return os.getenv("AI_SUMMARY_MODE", "smart").lower().strip() or "smart"


_GOLD_STANDARD_COMPACT = """EKSEMPLER (kort stil):
Bruger: "Hej, jeg leder efter projektledelse"
Rådgiver: [søger] Her er nogle stærke bud — tag et kig på kortene! Går du efter certificering eller generel projektledelse?
<suggestions>["PRINCE2", "E-learning", "Under 10.000 kr"]</suggestions>

Bruger: "nej det var ikke det jeg mente"
Rådgiver: Beklager — hvad passede ikke? Emne, niveau, pris eller format?
<suggestions>["Andet emne", "Billigere", "Kun online"]</suggestions>"""


def build_few_shot_message(full_examples: str) -> Optional[Dict[str, str]]:
    mode = few_shot_mode()
    if mode in {"0", "false", "no", "off", "never"}:
        return None
    content = _GOLD_STANDARD_COMPACT if mode == "compact" else full_examples
    return {"role": "system", "content": content}


def summarize_pruned_messages_rule_based(messages_to_prune: List[Dict[str, Any]]) -> Optional[str]:
    """Fast local summary — no API call."""
    user_bits: List[str] = []
    assistant_bits: List[str] = []
    course_titles: List[str] = []
    for msg in messages_to_prune:
        role = msg.get("role", "")
        content = str(msg.get("content") or "").strip()
        if not content or role == "system":
            continue
        if role == "user":
            user_bits.append(content[:160])
        elif role == "assistant":
            assistant_bits.append(content[:120])
        elif role == "tool":
            try:
                data = json.loads(content) if isinstance(content, str) else content
                for row in (data.get("results") or [])[:5]:
                    title = row.get("title")
                    if title:
                        course_titles.append(str(title))
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

    if not user_bits and not assistant_bits and not course_titles:
        return None

    lines = ["SAMTALEOVERSIGT (kompakt):"]
    if user_bits:
        lines.append("Bruger sagde: " + " | ".join(user_bits[-4:]))
    if course_titles:
        lines.append("Viste kurser: " + ", ".join(dict.fromkeys(course_titles))[:400])
    if assistant_bits:
        lines.append("Seneste svar: " + assistant_bits[-1][:180])
    return "\n".join(lines)


def summarize_pruned_messages_gpt(messages_to_prune: List[Dict[str, Any]]) -> Optional[str]:
    """Structured GPT summary for long pruned segments."""
    from ai_runtime import run_direct_completion

    transcript_parts = []
    for msg in messages_to_prune:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not content or role == "system":
            continue
        if role == "user":
            transcript_parts.append(f"Bruger: {content[:300]}")
        elif role == "assistant":
            transcript_parts.append(f"Rådgiver: {content[:300]}")
        elif role == "tool":
            try:
                data = json.loads(content) if isinstance(content, str) else content
                results = data.get("results", [])
                if results:
                    titles = [r.get("title", "?") for r in results[:5]]
                    transcript_parts.append(f"Søgeresultater: {', '.join(titles)}")
            except (json.JSONDecodeError, AttributeError):
                pass

    if not transcript_parts:
        return None

    transcript = "\n".join(transcript_parts)
    try:
        summary = run_direct_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Udtræk nøglefakta fra samtalen som korte danske bullet points. "
                        "Bevar behov, præferencer, viste/afviste kurser, beslutningsfase. Maks 120 ord."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            max_tokens=220,
        )
        if summary:
            return f"SAMTALEOVERSIGT (tidligere i samtalen):\n{summary}"
    except Exception as exc:
        print(f"[Summary GPT Error] {exc}")
    return summarize_pruned_messages_rule_based(messages_to_prune)


def summarize_pruned_messages_smart(messages_to_prune: List[Dict[str, Any]]) -> Optional[str]:
    """Use rules first; escalate to GPT only when the pruned block is large."""
    mode = summary_mode()
    if mode in {"0", "false", "no", "off", "never", "rules", "rule"}:
        return summarize_pruned_messages_rule_based(messages_to_prune)
    if mode in {"gpt", "always"}:
        return summarize_pruned_messages_gpt(messages_to_prune)

    char_count = sum(len(str(m.get("content") or "")) for m in messages_to_prune)
    if char_count < 1800:
        return summarize_pruned_messages_rule_based(messages_to_prune)
    return summarize_pruned_messages_gpt(messages_to_prune)


def prune_conversation_memory(
    messages: List[Dict[str, Any]],
    *,
    keep_recent: int = 12,
    trigger_at: int = 18,
    profile_markers: Optional[tuple] = None,
) -> List[Dict[str, Any]]:
    """Trim history and inject a compact summary."""
    if len(messages) <= trigger_at:
        return messages

    profile_markers = profile_markers or (
        "BRUGERENS NUVÆRENDE PROFIL",
        "TILBAGEVENDENDE BRUGER",
        "TILBAGEVENDENDE ANONYM",
    )
    system_msg = messages[0]
    protected: List[Dict[str, Any]] = []
    to_prune: List[Dict[str, Any]] = []
    for msg in messages[1:-keep_recent]:
        if msg.get("role") == "system" and any(m in (msg.get("content") or "") for m in profile_markers):
            protected.append(msg)
        else:
            to_prune.append(msg)
    recent = messages[-keep_recent:]
    summary_text = summarize_pruned_messages_smart(to_prune)
    new_messages = [system_msg] + protected
    if summary_text:
        new_messages.append({"role": "system", "content": summary_text})
    new_messages.extend(recent)
    return new_messages


def should_skip_anonymous_profile_extraction(*, logged_in: bool, user_msg_count: int) -> bool:
    if logged_in:
        return True
    if user_msg_count < 3:
        return True
    return user_msg_count % 4 != 0


def warm_ai_subsystems() -> Dict[str, int]:
    """Preload indexes used by chat tools."""
    stats: Dict[str, int] = {"products": 0, "catalog": 0}
    try:
        from app1.rag import warm_rag_index
        stats["products"] = warm_rag_index()
    except Exception as exc:
        print(f"[Warmup] RAG skipped: {exc}")
    try:
        import catalog_service as catalog
        stats["catalog"] = len(catalog.warm_catalog())
    except Exception as exc:
        print(f"[Warmup] Catalog skipped: {exc}")
    return stats


def run_chitchat_turn(
    messages: List[Dict[str, Any]],
    *,
    intent: str = "chit_chat",
) -> AgentRunResult:
    """Single-shot completion for greetings/thanks — no tools, minimal tokens."""
    start = time.time()
    prepared = prepare_messages_for_turn(messages, aggressive=False)
    model = choose_turn_model(intent=intent, tool_count=0, token_estimate=estimate_messages_tokens(prepared))
    text = run_direct_completion(prepared, model=model, max_tokens=min(280, max_output_tokens()))
    return AgentRunResult(
        text=text,
        messages=messages + [{"role": "assistant", "content": text}],
        runtime="direct",
        runtime_path="direct",
        usage={},
        latency_ms=int((time.time() - start) * 1000),
        compaction_level=compaction_level_for_messages(messages),
    )


def choose_max_iterations(intent: str, *, scope: str = "employee") -> int:
    try:
        default = 4 if scope == "employee" else 4
        base = int(os.getenv("AI_MAX_TOOL_ITERATIONS", str(default)))
    except ValueError:
        base = 4
    if intent in {"chit_chat", "follow_up", "detail"}:
        return max(2, base - 1)
    if intent in {"comparison", "buying", "team_buying", "profile_and_search"}:
        return base
    return max(2, base - 1)
