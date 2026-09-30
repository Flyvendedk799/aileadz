"""Priority- and budget-aware assembly of the per-turn system context.

Why this exists
---------------
The agent builds a turn from many system layers (profile, memories, company
rules, the profiler playbook, stage hints, few-shot examples, ...). The old
pipeline merged them into ONE "[SESSION KONTEKST]" message and then cut that
message to a flat 1800 characters. Because the cheapest layers (few-shot,
stage playbooks) were inserted first, the profile, the memories and the
profiler instructions were cut away on almost every turn — on both providers.

This module replaces the flat cut with an explicit budget:

* Each layer is a tagged system message (see :func:`layer`) with a priority, a
  cap, a floor and a zone. Untagged system messages are treated as the
  ``legacy`` layer, so callers that were never migrated (the HR agent) keep
  working unchanged.
* Over budget, the least important layers shrink to their floor first, then
  layers of priority >= 30 are dropped, then the mid-priority layers shrink.
  Priority 0 (the active mode's core playbook) is never dropped.
* Bodies are trimmed at line boundaries BEFORE they are fenced as untrusted
  data, so a fence can never be cut open.
* Output layout: ``[static, knowledge, steering, *history]``.
  - ``static`` (messages[0]) stays byte-identical: it is the cached prefix.
  - ``knowledge`` (what we know about the user/company) is emitted in a fixed
    order, so its bytes only change when the data changes — the prompt cache
    extends past the static prompt.
  - ``steering`` ("[SESSION KONTEKST]", per-turn guidance) is volatile and is
    placed next to the turn it shapes by the provider adapters.

The module is pure (no Flask, no provider SDKs) so it is unit-testable offline.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

PRIVATE_KEYS = ("_layer", "_header", "_fence", "_zone")

STEERING_HEADER = "[SESSION KONTEKST]"
KNOWLEDGE_HEADER = "[KONTEKST OM BRUGEREN]"

KNOWLEDGE = "knowledge"
STEERING = "steering"
LEGACY = "legacy"


@dataclass(frozen=True)
class LayerSpec:
    priority: int  # lower = more important
    cap: int       # max body chars
    floor: int     # body chars kept when shrinking under pressure
    zone: str


LAYER_SPECS: Dict[str, LayerSpec] = {
    "mode_core_playbook": LayerSpec(0, 4500, 4500, STEERING),
    "company_rules": LayerSpec(5, 2500, 900, KNOWLEDGE),
    "profile": LayerSpec(10, 4500, 1500, KNOWLEDGE),
    "guidance": LayerSpec(12, 1500, 600, STEERING),
    "turn_hint": LayerSpec(13, 600, 300, STEERING),
    "profiler_state": LayerSpec(14, 1500, 600, STEERING),
    "cv_just_applied": LayerSpec(14, 500, 200, STEERING),
    "employee_info": LayerSpec(15, 600, 300, KNOWLEDGE),
    "learning_context": LayerSpec(16, 1500, 500, KNOWLEDGE),
    "memories": LayerSpec(20, 2000, 600, KNOWLEDGE),
    "session_summary": LayerSpec(22, 2000, 800, KNOWLEDGE),
    "flow_playbooks": LayerSpec(25, 3000, 1200, STEERING),
    "hr_learning": LayerSpec(30, 1800, 600, KNOWLEDGE),
    "mode_digest": LayerSpec(32, 1500, 500, KNOWLEDGE),
    "shown_products": LayerSpec(35, 1500, 400, STEERING),
    "rejections": LayerSpec(38, 800, 0, STEERING),
    "recall": LayerSpec(40, 2000, 0, KNOWLEDGE),
    "returning_note": LayerSpec(45, 600, 0, KNOWLEDGE),
    "smart_context": LayerSpec(50, 800, 0, STEERING),
    "other_mode_digest": LayerSpec(52, 1000, 0, KNOWLEDGE),
    LEGACY: LayerSpec(55, 4000, 800, STEERING),
    "few_shot": LayerSpec(60, 1200, 0, STEERING),
}

_NEVER_DROP_BELOW = 15  # layers with priority < 15 are shrunk, never dropped
_DROP_FIRST_FROM = 30   # layers with priority >= 30 are dropped before mid layers shrink
_AGGRESSIVE_SCALE = 0.6


def layer(name: str, body: Any, *, header: str = "", fence: Optional[str] = None) -> Dict[str, Any]:
    """Build a tagged system layer.

    ``header`` is trusted text rendered above the body. ``fence`` is the label
    used to wrap the body as untrusted DATA (grounding.delimit_untrusted);
    leave it None for trusted, platform-authored text such as playbooks.
    """
    return {
        "role": "system",
        "content": "" if body is None else str(body),
        "_layer": name,
        "_header": header or "",
        "_fence": fence,
    }


def assembler_enabled() -> bool:
    return (os.getenv("AI_CONTEXT_ASSEMBLER", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def steering_placement() -> str:
    raw = (os.getenv("AI_STEERING_PLACEMENT", "trailing") or "trailing").strip().lower()
    return raw if raw in ("trailing", "leading") else "trailing"


def context_max_tokens() -> int:
    try:
        return max(1500, int(os.getenv("AI_CONTEXT_MAX_TOKENS", "10000")))
    except ValueError:
        return 10000


def context_budget_chars(
    limit_tokens: int,
    reserved_tokens: int = 0,
    *,
    chars_per_token: float = 4.0,
    aggressive: bool = False,
) -> int:
    """Characters available to the dynamic context layers for one request.

    At most ``AI_CONTEXT_MAX_TOKENS`` and at most 40% of what remains of the
    input budget once tool schemas (``reserved_tokens``) are paid for, so
    history always keeps room.
    """
    available = max(0, int(limit_tokens) - max(0, int(reserved_tokens or 0)))
    tokens = min(context_max_tokens(), int(available * 0.4))
    chars = int(tokens * max(1.0, float(chars_per_token or 4.0)))
    if aggressive:
        chars = int(chars * _AGGRESSIVE_SCALE)
    return max(0, chars)


def is_budgeted(msg: Dict[str, Any]) -> bool:
    return bool(msg.get("_zone"))


def strip_private_keys(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for msg in messages or []:
        if any(k in msg for k in PRIVATE_KEYS):
            msg = {k: v for k, v in msg.items() if k not in PRIVATE_KEYS}
        out.append(msg)
    return out


def finalize_for_openai(messages: List[Dict[str, Any]], placement: Optional[str] = None) -> List[Dict[str, Any]]:
    """Provider-ready message list for the OpenAI Chat/Responses APIs.

    With ``trailing`` placement the steering layer moves to just before the
    last user message (the turn it shapes); the static prompt and the knowledge
    layer stay at the top as the cacheable prefix. Private keys are stripped.
    """
    msgs = list(messages or [])
    if (placement or steering_placement()) == "trailing":
        steer_idx = next((i for i, m in enumerate(msgs) if m.get("_zone") == STEERING), None)
        if steer_idx is not None:
            last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=None)
            if last_user is not None and last_user > steer_idx:
                steering = msgs.pop(steer_idx)
                msgs.insert(last_user - 1, steering)
    return strip_private_keys(msgs)


# ── internals ────────────────────────────────────────────────────────────────

def _spec_for(name: Optional[str]) -> LayerSpec:
    return LAYER_SPECS.get(name or LEGACY, LAYER_SPECS[LEGACY])


def _trim_lines(text: str, max_chars: int) -> str:
    """Cut to at most ``max_chars``, preferring a line boundary."""
    text = (text or "").strip()
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n…"
    room = max(1, max_chars - len(marker))
    cut = text[:room]
    newline = cut.rfind("\n")
    if newline >= room * 0.5:
        cut = cut[:newline]
    return cut.rstrip() + marker


def _fence_body(label: str, body: str) -> str:
    try:
        import grounding

        fenced = grounding.delimit_untrusted(label, body)
        if fenced:
            return fenced
    except Exception:
        pass
    return body


def _render(entry: "_Entry", body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    text = _fence_body(entry.fence, body) if entry.fence else body
    return f"{entry.header}\n{text}" if entry.header else text


class _Entry:
    __slots__ = ("name", "header", "fence", "body", "spec", "order", "alloc", "overhead")

    def __init__(self, name, header, fence, body, spec, order):
        self.name = name
        self.header = header
        self.fence = fence
        self.body = body
        self.spec = spec
        self.order = order
        self.alloc = 0
        self.overhead = 0


def render_tagged(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Render tagged layers to plain system messages (header + fence), no budget.

    Used by the legacy path (AI_CONTEXT_ASSEMBLER=0) so a tagged layer still
    gets its header and its untrusted-data fence."""
    out = []
    for msg in messages or []:
        if msg.get("_layer"):
            entry = _Entry(msg["_layer"], msg.get("_header") or "", msg.get("_fence"),
                           str(msg.get("content") or ""), _spec_for(msg["_layer"]), 0)
            out.append({"role": "system", "content": _render(entry, entry.body)})
        else:
            out.append({k: v for k, v in msg.items() if k not in PRIVATE_KEYS})
    return out


_LAST = threading.local()


def last_report() -> Dict[str, Any]:
    """The report of the most recent assemble() call on this thread."""
    return dict(getattr(_LAST, "report", {}) or {})


def _cost(entries: List[_Entry]) -> int:
    total = 0
    for e in entries:
        if e.alloc > 0:
            total += e.alloc + e.overhead + 2
    return total


def fit_layers(entries: List[_Entry], budget_chars: int, *, aggressive: bool = False) -> None:
    """Set ``alloc`` (body chars) on every entry so the rendered total fits."""
    scale = _AGGRESSIVE_SCALE if aggressive else 1.0
    for e in entries:
        cap = e.spec.cap if e.spec.priority == 0 else int(e.spec.cap * scale)
        e.alloc = min(len(e.body), cap)
        e.overhead = len(_render(e, "x")) - 1 if e.body else 0

    def over() -> bool:
        return _cost(entries) > budget_chars

    by_least_important = sorted(entries, key=lambda e: (-e.spec.priority, -e.order))

    # 1. Shrink the low-priority layers (>= 30) to their floor.
    for e in by_least_important:
        if not over():
            return
        if e.spec.priority >= _DROP_FIRST_FROM:
            e.alloc = min(e.alloc, e.spec.floor)
    # 2. Drop the low-priority layers entirely.
    for e in by_least_important:
        if not over():
            return
        if e.spec.priority >= _DROP_FIRST_FROM:
            e.alloc = 0
    # 3. Shrink the mid layers (1-29) to their floor.
    for e in by_least_important:
        if not over():
            return
        if 0 < e.spec.priority < _DROP_FIRST_FROM:
            e.alloc = min(e.alloc, e.spec.floor)
    # 4. Last resort: drop mid layers that are allowed to go (priority >= 15).
    for e in by_least_important:
        if not over():
            return
        if e.spec.priority >= _NEVER_DROP_BELOW:
            e.alloc = 0


def assemble(
    messages: List[Dict[str, Any]],
    *,
    budget_chars: int,
    aggressive: bool = False,
    static_max_chars: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Merge system layers into ``[static, knowledge, steering, *history]``.

    Every system message except an untagged messages[0] becomes a layer
    (wherever it sits in the list — late hints appended after the history are
    still steering). Non-system messages keep their order.
    """
    msgs = list(messages or [])
    if not msgs:
        return [], {"budget_chars": budget_chars, "used_chars": 0, "layers": []}

    static = None
    start = 0
    if msgs[0].get("role") == "system" and "_layer" not in msgs[0]:
        static = dict(msgs[0])
        start = 1
        if static_max_chars:
            content = str(static.get("content") or "")
            if len(content) > static_max_chars:
                static["content"] = content[: static_max_chars - 16] + "\n…[truncated]"

    entries: List[_Entry] = []
    history: List[Dict[str, Any]] = []
    for idx, msg in enumerate(msgs[start:]):
        if msg.get("role") != "system":
            history.append(msg)
            continue
        body = str(msg.get("content") or "").strip()
        if not body:
            continue
        name = msg.get("_layer")
        if not name:
            # Output of an earlier assemble() (a prepared list handed back in —
            # with or without its private keys) is already budgeted: keep it
            # whole instead of re-cutting it as a legacy layer.
            zone = msg.get("_zone")
            for prefix, prefix_zone in ((KNOWLEDGE_HEADER, KNOWLEDGE), (STEERING_HEADER, STEERING)):
                if body.startswith(prefix):
                    zone = prefix_zone
                    body = body[len(prefix):].strip()
                    break
            if zone and body:
                spec = LayerSpec(1, len(body), len(body), zone)
                entries.append(_Entry(f"prebudgeted_{zone}", "", None, body, spec, idx))
                continue
            name = LEGACY
        entries.append(_Entry(name, msg.get("_header") or "", msg.get("_fence"), body, _spec_for(name), idx))

    fit_layers(entries, budget_chars, aggressive=aggressive)

    knowledge_parts: List[str] = []
    steering_parts: List[str] = []
    report_layers = []
    knowledge_entries = sorted((e for e in entries if e.spec.zone == KNOWLEDGE),
                               key=lambda e: (e.spec.priority, e.order))
    steering_entries = sorted((e for e in entries if e.spec.zone != KNOWLEDGE), key=lambda e: e.order)
    for group, parts in ((knowledge_entries, knowledge_parts), (steering_entries, steering_parts)):
        for e in group:
            status = "dropped" if e.alloc <= 0 else ("truncated" if e.alloc < len(e.body) else "kept")
            if e.alloc > 0:
                rendered = _render(e, _trim_lines(e.body, e.alloc))
                if rendered:
                    parts.append(rendered)
            report_layers.append({
                "name": e.name, "priority": e.spec.priority, "zone": e.spec.zone,
                "chars": len(e.body), "kept": min(e.alloc, len(e.body)), "status": status,
            })

    out: List[Dict[str, Any]] = []
    if static is not None:
        out.append(static)
    if knowledge_parts:
        out.append({"role": "system", "content": KNOWLEDGE_HEADER + "\n" + "\n\n".join(knowledge_parts),
                    "_zone": KNOWLEDGE})
    if steering_parts:
        out.append({"role": "system", "content": STEERING_HEADER + "\n" + "\n\n".join(steering_parts),
                    "_zone": STEERING})
    out.extend(history)

    report = {
        "budget_chars": budget_chars,
        "used_chars": sum(len(p) for p in knowledge_parts + steering_parts),
        "aggressive": aggressive,
        "layers": report_layers,
    }
    _LAST.report = report
    return out, report
