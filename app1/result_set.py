"""Conversation-scoped active result set and versioned search constraints.

Owns the state that fixes L01 / L02 / L11 for the learner assistant:

* L01 — follow-up detail/correction binds to a stable focused course
  (handle + provider + optional session); prose and cards must agree.
* L02 — ``active_result_set`` is separate from the historical shown-product
  list; "de foreslåede" / ordinals / corrections resolve against it; a new
  search (e.g. physical → online) *replaces* the set, never mixes.
* L11 — versioned ``search_constraints`` live in the conversation's
  ``state_json`` (keyed by owned session id). "i denne samtale" uses this
  thread only; cross-chat digests must not overwrite it.

Pure helpers — no Flask / DB / network. Callers keep the dict on
``SESSION_STATE[sid]`` and persist it via ``conversation_state.save_turn``.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Dict, List, Optional

# ── Patterns for constraint extraction / read-only lookups ──────────────────

_BUDGET_RE = re.compile(
    r"(?:under|maks(?:imalt)?|budget|max|op til|højst|omkring|prisgrænse|"
    r"ikke over|højst)\s*(\d[\d.]*)(?:[,](\d{1,2}))?\s*(?:kr)?",
    re.IGNORECASE,
)
_BUDGET_INCL_VAT = re.compile(
    r"(inkl(?:usive|\.?)?\s*moms|inkl\.?\s*moms|med\s*moms)",
    re.IGNORECASE,
)
_ONLINE_RE = re.compile(
    r"\b(kun\s+online|online(?:kurser)?|e-?learning|hjemmefra|virtuel(?:t)?)\b",
    re.IGNORECASE,
)
_ONLINE_NEGATION = re.compile(
    r"\b(ikke\s+online|ingen\s+online|ej\s+online|ikke\s+e-?learning)\b",
    re.IGNORECASE,
)
_PHYSICAL_RE = re.compile(
    r"\b(fysisk(?:\s+fremmøde)?|fremmøde|klasselokale|på\s+lokation)\b",
    re.IGNORECASE,
)
_PHYSICAL_NEGATION = re.compile(
    r"\b(ikke\s+fysisk|ingen\s+fysisk|ej\s+fysisk|ikke\s+fremmøde)\b",
    re.IGNORECASE,
)
_READ_ONLY_RE = re.compile(
    r"(?:bestil\s+(?:og\s+gem\s+)?intet|gem\s+(?:stadig\s+)?intet|"
    r"opret\s+ingen\s+ordre|ikke\s+bestil|uden\s+at\s+bestille|"
    r"kun\s+(?:et\s+)?opslag|dette\s+er\s+stadig\s+kun\s+et\s+opslag|"
    r"foretag\s+ingen\s+ændringer|ret\s+kun\s+dit\s+svar)",
    re.IGNORECASE,
)
_THIS_THREAD_RE = re.compile(
    r"(?:i\s+denne\s+samtale|senest\s+gældende\s+søgekrav|"
    r"opsu(?:mmer|mmer)\s+kun\s+de\s+senest|"
    r"søgekrav\s+i\s+denne|gældende\s+søgekrav)",
    re.IGNORECASE,
)
# Topic cues for conversation-scoped constraints (not profile preferences).
_TOPIC_CUES = (
    (re.compile(r"\bexcel\b", re.IGNORECASE), "Excel"),
    (re.compile(r"\bledelse\b|\bleder(?:skab|kurser)?\b|\bleadership\b", re.IGNORECASE), "ledelse"),
    (re.compile(r"\bagil(?:e)?\b|projektledelse", re.IGNORECASE), "projektledelse"),
    (re.compile(r"\bitil\b", re.IGNORECASE), "ITIL"),
    (re.compile(r"\bpowerpoint\b|\bppt\b", re.IGNORECASE), "PowerPoint"),
    (re.compile(r"\bsql\b|database", re.IGNORECASE), "SQL"),
)
_ORDINAL_RE = re.compile(
    r"\b(?:nummer|nr\.?)\s*(\d+)\b|"
    r"\bden\s+(første|anden|anden|tredje|fjerde|femte|sidste)\b|"
    r"\bdet\s+(første|andet|tredje|fjerde|femte|sidste)\b",
    re.IGNORECASE,
)
_ORDINAL_WORDS = {
    "første": 1, "andet": 2, "anden": 2, "tredje": 3,
    "fjerde": 4, "femte": 5, "sidste": -1,
}


def _session_bucket(state: Optional[dict]) -> dict:
    if state is None:
        raise TypeError("session state dict is required")
    return state


def get_search_constraints(state: Optional[dict]) -> dict:
    """Current-thread search constraints, or an empty versioned shell."""
    bucket = (state or {}).get("search_constraints")
    if isinstance(bucket, dict) and bucket.get("version"):
        return dict(bucket)
    return {"version": 0}


def get_active_result_set(state: Optional[dict]) -> Optional[dict]:
    ars = (state or {}).get("active_result_set")
    return dict(ars) if isinstance(ars, dict) and ars.get("id") else None


def get_focused_course(state: Optional[dict]) -> Optional[dict]:
    ars = get_active_result_set(state)
    if not ars:
        return None
    focused = ars.get("focused")
    return dict(focused) if isinstance(focused, dict) and focused.get("handle") else None


def _parse_price(match) -> Optional[float]:
    if not match:
        return None
    whole = (match.group(1) or "").replace(".", "")
    frac = match.group(2) or "0"
    try:
        return float(f"{whole}.{frac}") if match.group(2) else float(whole)
    except ValueError:
        return None


def parse_constraint_updates(user_text: str, current: Optional[dict] = None) -> dict:
    """Derive constraint patches from one user turn (negation-aware).

    Returns only keys that this turn changes. Format replacement is explicit:
    "kun online, ikke fysisk" → format=online; "ikke online" → format=fysisk
    when physical is also requested, else clears online.
    """
    text = user_text or ""
    patch: Dict[str, Any] = {}
    if not text.strip():
        return patch

    online_neg = bool(_ONLINE_NEGATION.search(text))
    physical_neg = bool(_PHYSICAL_NEGATION.search(text))
    online = bool(_ONLINE_RE.search(text)) and not online_neg
    physical = bool(_PHYSICAL_RE.search(text)) and not physical_neg

    if online and not physical:
        patch["format"] = "online"
    elif physical and not online:
        patch["format"] = "fysisk"
    elif online_neg and not physical:
        # Explicit "ikke online" without a physical request → prefer fysisk
        patch["format"] = "fysisk"
    elif physical_neg and not online:
        patch["format"] = "online"
    elif online and physical:
        # Contradiction — prefer the last mentioned cue
        last_online = max((m.start() for m in _ONLINE_RE.finditer(text)), default=-1)
        last_phys = max((m.start() for m in _PHYSICAL_RE.finditer(text)), default=-1)
        patch["format"] = "online" if last_online >= last_phys else "fysisk"

    budget = _BUDGET_RE.search(text)
    price = _parse_price(budget)
    if price is not None:
        patch["price_max"] = price
        patch["price_inclusive"] = bool(_BUDGET_INCL_VAT.search(text))

    # Capture explicit topic cues so leadership vs Excel chats stay distinct
    # even before a catalog_search tool call writes search_args.
    for cre, label in _TOPIC_CUES:
        if cre.search(text):
            patch["topic"] = label
            break

    return patch


def apply_constraint_updates(state: dict, patch: dict, *, source: str = "user") -> dict:
    """Merge ``patch`` into versioned search_constraints on ``state``."""
    _session_bucket(state)
    if not patch:
        return get_search_constraints(state)
    current = get_search_constraints(state)
    version = int(current.get("version") or 0) + 1
    updated = dict(current)
    updated.update({k: v for k, v in patch.items() if v is not None})
    updated["version"] = version
    updated["updated_at"] = time.time()
    updated["source"] = source
    state["search_constraints"] = updated
    return updated


def update_constraints_from_user(state: dict, user_text: str) -> dict:
    patch = parse_constraint_updates(user_text, get_search_constraints(state))
    return apply_constraint_updates(state, patch, source="user")


def update_constraints_from_search_args(state: dict, arguments: Optional[dict]) -> dict:
    """Mirror catalog_search tool filters into versioned constraints."""
    args = arguments if isinstance(arguments, dict) else {}
    patch: Dict[str, Any] = {}
    delivery = (args.get("delivery") or args.get("format") or "").strip().lower()
    if delivery in ("online", "e-learning", "elearning", "virtuel"):
        patch["format"] = "online"
    elif delivery in ("fysisk", "physical", "fremmøde", "in-person", "in_person"):
        patch["format"] = "fysisk"
    price_max = args.get("price_max")
    if price_max is not None:
        try:
            patch["price_max"] = float(price_max)
        except (TypeError, ValueError):
            pass
    location = (args.get("location") or "").strip()
    if location and location.lower() not in ("fysisk", "online"):
        patch["location"] = location
    query = (args.get("query") or "").strip()
    if query:
        patch["topic"] = query[:120]
    limit = args.get("limit")
    if limit is not None:
        try:
            patch["limit"] = int(limit)
        except (TypeError, ValueError):
            pass
    return apply_constraint_updates(state, patch, source="search_tool")


def _compact_entry(cr: dict, index: int) -> dict:
    return {
        "index": index,
        "handle": cr.get("handle") or "",
        "title": cr.get("title") or "",
        "vendor": cr.get("vendor") or cr.get("provider") or "",
        "price": cr.get("price") or cr.get("price_label") or cr.get("price_min"),
        "locations": list(cr.get("locations") or [])[:4],
        "product_type": cr.get("product_type") or cr.get("format") or "",
        "summary": (cr.get("summary") or "")[:120],
    }


def replace_active_result_set(
    state: dict,
    compact_results: Optional[List[dict]],
    *,
    filters: Optional[dict] = None,
) -> Optional[dict]:
    """Replace the active result set from a fresh search (never merge)."""
    _session_bucket(state)
    results = [r for r in (compact_results or []) if isinstance(r, dict) and r.get("handle")]
    if not results:
        # Empty search still clears the prior active set so follow-ups cannot
        # silently fall back to the obsolete physical/online mix.
        state["active_result_set"] = {
            "id": f"rs-{uuid.uuid4().hex[:12]}",
            "handles": [],
            "products": [],
            "filters": dict(filters or get_search_constraints(state)),
            "selected_sessions": {},
            "focused": None,
            "created_at": time.time(),
        }
        return state["active_result_set"]

    products = [_compact_entry(cr, i + 1) for i, cr in enumerate(results)]
    ars = {
        "id": f"rs-{uuid.uuid4().hex[:12]}",
        "handles": [p["handle"] for p in products],
        "products": products,
        "filters": dict(filters or get_search_constraints(state)),
        "selected_sessions": {},
        "focused": None,
        "created_at": time.time(),
    }
    state["active_result_set"] = ars
    return ars


def set_focused_course(
    state: dict,
    *,
    handle: str,
    title: str = "",
    vendor: str = "",
    price: Any = None,
    session: Optional[dict] = None,
) -> Optional[dict]:
    """Bind follow-up detail/correction to one stable course identity."""
    _session_bucket(state)
    handle = (handle or "").strip()
    if not handle:
        return None
    ars = get_active_result_set(state)
    if not ars:
        ars = {
            "id": f"rs-{uuid.uuid4().hex[:12]}",
            "handles": [handle],
            "products": [],
            "filters": dict(get_search_constraints(state)),
            "selected_sessions": {},
            "focused": None,
            "created_at": time.time(),
        }
        state["active_result_set"] = ars
    # Prefer facts already in the active set (same identity after correction).
    match = next((p for p in ars.get("products") or [] if p.get("handle") == handle), None)
    focused = {
        "handle": handle,
        "title": title or (match or {}).get("title") or "",
        "vendor": vendor or (match or {}).get("vendor") or "",
        "price": price if price is not None else (match or {}).get("price"),
        "session": session,
    }
    ars = dict(ars)
    ars["focused"] = focused
    if session and handle:
        sessions = dict(ars.get("selected_sessions") or {})
        sessions[handle] = session
        ars["selected_sessions"] = sessions
    if handle not in (ars.get("handles") or []):
        ars["handles"] = list(ars.get("handles") or []) + [handle]
    state["active_result_set"] = ars
    return focused


def resolve_ordinal(state: Optional[dict], user_text: str) -> Optional[dict]:
    """Resolve 'nummer 2' / 'den anden' against the *active* result set only."""
    ars = get_active_result_set(state)
    products = (ars or {}).get("products") or []
    if not products:
        return None
    m = _ORDINAL_RE.search(user_text or "")
    if not m:
        return None
    if m.group(1):
        idx = int(m.group(1))
    else:
        word = (m.group(2) or m.group(3) or "").lower()
        idx = _ORDINAL_WORDS.get(word)
        if idx is None:
            return None
        if idx == -1:
            idx = len(products)
    if idx < 1 or idx > len(products):
        return None
    return dict(products[idx - 1])


def is_read_only_lookup(user_text: str) -> bool:
    """True when the user forbids ordering/saving (\"bestil intet\", etc.)."""
    return bool(_READ_ONLY_RE.search(user_text or ""))


def asks_for_this_thread_constraints(user_text: str) -> bool:
    return bool(_THIS_THREAD_RE.search(user_text or ""))


def should_suppress_cross_session_digests(user_text: str) -> bool:
    """L11: when the user asks for *this* chat's search constraints, digests
    from other conversations (Excel/budget etc.) must not enter context."""
    return asks_for_this_thread_constraints(user_text)


def filter_cards_to_focus(cards: List[dict], state: Optional[dict]) -> List[dict]:
    """When a focused course is set, drop cards that do not match its handle.

    Prevents L01's similarly-named substitution (DANSK IT 4.950 vs TI 14.499).
    """
    focused = get_focused_course(state)
    if not focused or not cards:
        return cards
    handle = focused.get("handle")
    vendor = (focused.get("vendor") or "").strip().lower()
    kept = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        if card.get("handle") != handle:
            continue
        if vendor:
            card_vendor = (card.get("vendor") or card.get("provider") or "").strip().lower()
            if card_vendor and card_vendor != vendor:
                continue
        kept.append(card)
    # Empty is intentional: better no card than a similarly-named substitute (L01).
    return kept


def claims_match_active_facts(
    *,
    handle: str = "",
    vendor: str = "",
    price: Any = None,
    state: Optional[dict] = None,
) -> bool:
    """True when the claimed identity agrees with the focused / active course."""
    focused = get_focused_course(state)
    if focused and handle and focused.get("handle") and handle != focused.get("handle"):
        return False
    if focused and vendor and focused.get("vendor"):
        if vendor.strip().lower() != str(focused.get("vendor") or "").strip().lower():
            return False
    ars = get_active_result_set(state)
    if handle and ars and ars.get("handles") and handle not in ars["handles"]:
        # Detail on a course outside the active set is allowed only when focused
        # was explicitly set to it (set_focused_course adds the handle).
        if not (focused and focused.get("handle") == handle):
            return False
    return True


def evidence_from_state(state: Optional[dict]) -> List[dict]:
    """Canonical fact snippets for grounding (active set + focused course)."""
    evidence: List[dict] = []
    ars = get_active_result_set(state)
    if not ars:
        return evidence
    for p in ars.get("products") or []:
        evidence.append({
            "handle": p.get("handle"),
            "title": p.get("title"),
            "vendor": p.get("vendor"),
            "price": p.get("price"),
            "locations": p.get("locations"),
        })
    focused = ars.get("focused")
    if isinstance(focused, dict) and focused.get("handle"):
        evidence.append({
            "handle": focused.get("handle"),
            "title": focused.get("title"),
            "vendor": focused.get("vendor"),
            "price": focused.get("price"),
            "focused": True,
        })
    return evidence


def build_active_set_message(state: Optional[dict]) -> Optional[dict]:
    """System message: active set is authoritative for follow-ups/ordinals."""
    ars = get_active_result_set(state)
    if not ars:
        return None
    products = ars.get("products") or []
    focused = ars.get("focused")
    lines = [
        f"AKTIVT RESULTATSÆT id={ars.get('id')} (brug KUN disse til "
        f"'de foreslåede', ordinaler, rettelser og opfølgning — "
        f"ikke den historiske viste-kurser-liste):",
    ]
    if not products:
        lines.append("(tomt — seneste søgning gav ingen kurser)")
    else:
        compact = [{
            "i": p.get("index"),
            "t": p.get("title"),
            "h": p.get("handle"),
            "p": p.get("price"),
            "v": p.get("vendor"),
            "l": (p.get("locations") or [])[:2],
        } for p in products]
        import json
        lines.append(json.dumps(compact, ensure_ascii=False))
    filters = ars.get("filters") or {}
    filt_bits = []
    if filters.get("format"):
        filt_bits.append(f"format={filters['format']}")
    if filters.get("price_max") is not None:
        incl = " inkl. moms" if filters.get("price_inclusive") else ""
        filt_bits.append(f"pris≤{filters['price_max']}{incl}")
    if filters.get("location"):
        filt_bits.append(f"sted={filters['location']}")
    if filters.get("topic"):
        filt_bits.append(f"emne={filters['topic']}")
    if filt_bits:
        lines.append("Filtre for dette sæt: " + ", ".join(filt_bits))
    if isinstance(focused, dict) and focused.get("handle"):
        lines.append(
            "FOKUSERET KURSUS (bind rettelser/detaljer hertil — byt ALDRIG til "
            "et lignende kursus fra en anden udbyder uden at spørge): "
            f"handle={focused.get('handle')}, "
            f"udbyder={focused.get('vendor') or '?'}, "
            f"titel={focused.get('title') or '?'}, "
            f"pris={focused.get('price') if focused.get('price') is not None else '?'}"
        )
    return {"role": "system", "content": "\n".join(lines)}


def build_thread_constraints_message(state: Optional[dict]) -> Optional[dict]:
    """Current-thread search requirements for 'i denne samtale' (L11)."""
    c = get_search_constraints(state)
    if not c or int(c.get("version") or 0) <= 0:
        return {
            "role": "system",
            "content": (
                "SØGEKRAV I DENNE SAMTALE (autoritative): ingen midlertidige "
                "søgekrav er registreret endnu for DENNE session. Opsummér KUN "
                "det der står i denne tråds egne beskeder. FORBUDT: at bruge "
                "Excel/budget/emne fra andre samtaler, mode-digest, "
                "other_mode_digest eller gemte profilpræferencer som om de var "
                "denne samtale søgekrav. Skeln mellem midlertidige "
                "katalogopslag og gemte profilfakta."
            ),
        }
    bits = [f"version={c.get('version')}"]
    if c.get("topic"):
        bits.append(f"emne={c['topic']}")
    if c.get("format"):
        bits.append(f"format={c['format']}")
    if c.get("price_max") is not None:
        incl = " inkl. moms" if c.get("price_inclusive") else ""
        bits.append(f"prisgrænse={c['price_max']}{incl}")
    if c.get("location"):
        bits.append(f"sted={c['location']}")
    if c.get("limit") is not None:
        bits.append(f"antal≤{c['limit']}")
    return {
        "role": "system",
        "content": (
            "SØGEKRAV I DENNE SAMTALE — AUTORITATIVE (midlertidige "
            "katalogopslag). OVERSKRIV digests/andre chats/profilpræferencer:\n• "
            + "\n• ".join(bits)
            + "\nNår brugeren spørger 'i denne samtale' / om senest gældende "
            "søgekrav, svar KUN ud fra disse felter og trådens egne beskeder. "
            "Nævn ALDRIG emne/budget fra en anden samtale her."
        ),
    }


def format_preference_from_constraints(state: Optional[dict]) -> Optional[str]:
    """One-line format/budget hint for smart context (conversation-scoped)."""
    c = get_search_constraints(state)
    if not c or int(c.get("version") or 0) <= 0:
        return None
    parts = []
    if c.get("format") == "online":
        parts.append("Format: online (seneste korrektion i denne samtale)")
    elif c.get("format") == "fysisk":
        parts.append("Format: fysisk fremmøde (seneste korrektion i denne samtale)")
    if c.get("price_max") is not None:
        incl = " inkl. moms" if c.get("price_inclusive") else ""
        parts.append(f"Prisgrænse: under {c['price_max']}{incl}")
    if c.get("topic"):
        parts.append(f"Emne: {c['topic']}")
    return "; ".join(parts) if parts else None
