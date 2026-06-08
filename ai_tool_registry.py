"""Futurematch AI tool registry and selection policy.

This module keeps tool schemas, metadata, and per-turn tool selection separate
from the agent prompts. The goal is to expose fewer, better tools per turn
while keeping schemas strict enough for reliable function calling.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


TOOLSET_VERSION = "futurematch-tools-v2"


@dataclass(frozen=True)
class ToolMeta:
    name: str
    agent_scope: str = "employee"
    auth_required: bool = False
    company_required: bool = False
    side_effect: bool = False
    parallel_safe: bool = True
    cache_ttl: int = 0
    toolset_tags: Tuple[str, ...] = ()


_EMPLOYEE_META = {
    "catalog_search": ToolMeta("catalog_search", toolset_tags=("catalog", "search"), cache_ttl=300),
    "catalog_get_product": ToolMeta("catalog_get_product", toolset_tags=("catalog", "product"), cache_ttl=300),
    "catalog_get_category": ToolMeta("catalog_get_category", toolset_tags=("catalog", "category"), cache_ttl=300),
    "catalog_get_vendor": ToolMeta("catalog_get_vendor", toolset_tags=("catalog", "vendor"), cache_ttl=300),
    "catalog_compare_products": ToolMeta("catalog_compare_products", toolset_tags=("catalog", "compare"), cache_ttl=300),
    "get_learning_context": ToolMeta("get_learning_context", toolset_tags=("context",), cache_ttl=60),
    "check_course_readiness": ToolMeta("check_course_readiness", toolset_tags=("order", "context"), cache_ttl=60),
    "prepare_course_order": ToolMeta("prepare_course_order", toolset_tags=("order",), cache_ttl=0),
    "search_courses": ToolMeta("search_courses", toolset_tags=("legacy_search", "search"), cache_ttl=120),
    "filter_courses": ToolMeta("filter_courses", toolset_tags=("legacy_search", "filter"), cache_ttl=120),
    "get_course_details": ToolMeta("get_course_details", toolset_tags=("legacy_search", "product"), cache_ttl=300),
    "compare_courses": ToolMeta("compare_courses", toolset_tags=("legacy_search", "compare"), cache_ttl=300),
    "get_vendor_info": ToolMeta("get_vendor_info", toolset_tags=("vendor",), cache_ttl=300),
    "create_course_order": ToolMeta(
        "create_course_order",
        side_effect=True,
        parallel_safe=False,
        toolset_tags=("order", "mutation"),
    ),
    "check_order_approval_status": ToolMeta(
        "check_order_approval_status",
        company_required=True,
        toolset_tags=("approval", "company"),
        cache_ttl=30,
    ),
    "analyze_skill_gaps": ToolMeta(
        "analyze_skill_gaps",
        company_required=True,
        toolset_tags=("skills", "company"),
        cache_ttl=120,
    ),
    "get_department_budget": ToolMeta(
        "get_department_budget",
        company_required=True,
        toolset_tags=("budget", "company"),
        cache_ttl=60,
    ),
    "get_user_profile": ToolMeta("get_user_profile", auth_required=True, toolset_tags=("profile",), cache_ttl=60),
    "update_user_profile": ToolMeta(
        "update_user_profile",
        auth_required=True,
        side_effect=True,
        parallel_safe=False,
        toolset_tags=("profile", "mutation"),
    ),
    "request_user_input": ToolMeta(
        "request_user_input",
        auth_required=True,
        parallel_safe=False,
        toolset_tags=("profile", "ui"),
    ),
    "suggest_learning_path": ToolMeta(
        "suggest_learning_path",
        auth_required=True,
        toolset_tags=("profile", "path"),
        cache_ttl=60,
    ),
    "recommend_for_profile": ToolMeta(
        "recommend_for_profile",
        auth_required=True,
        toolset_tags=("profile", "recommendation"),
        cache_ttl=120,
    ),
    "set_learning_goal": ToolMeta(
        "set_learning_goal", auth_required=True, parallel_safe=False, toolset_tags=("profile", "goals"),
    ),
    "get_learning_goals": ToolMeta(
        "get_learning_goals", auth_required=True, toolset_tags=("profile", "goals"), cache_ttl=20,
    ),
    "update_learning_goal": ToolMeta(
        "update_learning_goal", auth_required=True, parallel_safe=False, toolset_tags=("profile", "goals"),
    ),
    # --- Specialised employee tools (keyword-gated, NOT in core seed) ---
    "get_my_course_status": ToolMeta(
        "get_my_course_status", auth_required=True, toolset_tags=("course", "status"), cache_ttl=30,
    ),
    "get_negotiated_discount": ToolMeta(
        "get_negotiated_discount", auth_required=True, toolset_tags=("pricing", "discount"), cache_ttl=120,
    ),
    "check_course_prerequisites": ToolMeta(
        "check_course_prerequisites", auth_required=True, toolset_tags=("course", "prerequisites"), cache_ttl=120,
    ),
    "get_course_sequel": ToolMeta(
        "get_course_sequel", auth_required=True, toolset_tags=("course", "path"), cache_ttl=120,
    ),
    "find_certification_path": ToolMeta(
        "find_certification_path", auth_required=True, toolset_tags=("certification", "path"), cache_ttl=120,
    ),
    "track_goal_progress": ToolMeta(
        "track_goal_progress", auth_required=True, toolset_tags=("goals", "progress"), cache_ttl=30,
    ),
    "add_to_calendar": ToolMeta(
        "add_to_calendar", auth_required=True, toolset_tags=("calendar", "export"), cache_ttl=0,
    ),
    "mark_course_complete": ToolMeta(
        "mark_course_complete",
        auth_required=True,
        side_effect=True,
        parallel_safe=False,
        toolset_tags=("course", "mutation"),
    ),
}

_HR_META = {
    "get_team_training_status": ToolMeta("get_team_training_status", "hr", company_required=True, cache_ttl=60, toolset_tags=("status",)),
    "get_company_skill_gaps": ToolMeta("get_company_skill_gaps", "hr", company_required=True, cache_ttl=120, toolset_tags=("skills",)),
    "get_budget_overview": ToolMeta("get_budget_overview", "hr", company_required=True, cache_ttl=60, toolset_tags=("budget",)),
    "get_employee_overview": ToolMeta("get_employee_overview", "hr", company_required=True, cache_ttl=60, toolset_tags=("employee",)),
    "get_training_report": ToolMeta("get_training_report", "hr", company_required=True, cache_ttl=120, toolset_tags=("report",)),
    "get_pending_actions": ToolMeta("get_pending_actions", "hr", company_required=True, cache_ttl=45, toolset_tags=("actions",)),
    "search_courses_for_team": ToolMeta("search_courses_for_team", "hr", company_required=True, cache_ttl=120, toolset_tags=("catalog", "search")),
    "get_chatbot_usage_stats": ToolMeta("get_chatbot_usage_stats", "hr", company_required=True, cache_ttl=120, toolset_tags=("usage",)),
    "hr_get_company_learning_context": ToolMeta("hr_get_company_learning_context", "hr", company_required=True, cache_ttl=60, toolset_tags=("context",)),
    "hr_recommend_training_plan": ToolMeta("hr_recommend_training_plan", "hr", company_required=True, cache_ttl=120, toolset_tags=("plan", "catalog")),
    "hr_get_supplier_coverage": ToolMeta("hr_get_supplier_coverage", "hr", company_required=True, cache_ttl=120, toolset_tags=("supplier", "catalog")),
    "hr_get_ai_usage_risks": ToolMeta("hr_get_ai_usage_risks", "hr", company_required=True, cache_ttl=120, toolset_tags=("usage", "risk")),
    "get_compliance_status": ToolMeta("get_compliance_status", "hr", company_required=True, cache_ttl=120, toolset_tags=("compliance",)),
    # --- Specialised HR tools (keyword-gated, NOT in core seed) ---
    "get_team_non_starters": ToolMeta("get_team_non_starters", "hr", company_required=True, cache_ttl=60, toolset_tags=("status", "team")),
    "hr_team_compliance": ToolMeta("hr_team_compliance", "hr", company_required=True, cache_ttl=120, toolset_tags=("compliance", "team")),
    "hr_roi_summary": ToolMeta("hr_roi_summary", "hr", company_required=True, cache_ttl=120, toolset_tags=("roi", "report")),
    "hr_benchmark": ToolMeta("hr_benchmark", "hr", company_required=True, cache_ttl=120, toolset_tags=("benchmark",)),
    "hr_trial_and_seat_status": ToolMeta("hr_trial_and_seat_status", "hr", company_required=True, cache_ttl=60, toolset_tags=("subscription", "seats")),
    "approve_order_from_chat": ToolMeta(
        "approve_order_from_chat", "hr",
        company_required=True, side_effect=True, parallel_safe=False, toolset_tags=("approval", "mutation"),
    ),
    "assign_learning_path_to_team": ToolMeta(
        "assign_learning_path_to_team", "hr",
        company_required=True, side_effect=True, parallel_safe=False, toolset_tags=("path", "mutation"),
    ),
    "hr_inactive_employees": ToolMeta("hr_inactive_employees", "hr", company_required=True, cache_ttl=60, toolset_tags=("employee", "inactive")),
    "hr_expiring_agreements": ToolMeta("hr_expiring_agreements", "hr", company_required=True, cache_ttl=120, toolset_tags=("supplier", "agreements")),
}


def _property_type(prop: Dict[str, Any]) -> Any:
    return prop.get("type")


def _is_nullable(prop: Dict[str, Any]) -> bool:
    typ = _property_type(prop)
    return (isinstance(typ, list) and "null" in typ) or prop.get("nullable") is True


def _nullable_copy(prop: Dict[str, Any]) -> Dict[str, Any]:
    prop = deepcopy(prop)
    typ = prop.get("type")
    if typ and not _is_nullable(prop):
        if isinstance(typ, list):
            prop["type"] = list(dict.fromkeys(typ + ["null"]))
        else:
            prop["type"] = [typ, "null"]
        if "enum" in prop and None not in prop["enum"]:
            prop["enum"] = list(prop["enum"]) + [None]
    return prop


def _strict_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    schema = deepcopy(schema or {"type": "object", "properties": {}, "required": []})
    schema.pop("default", None)
    schema.pop("nullable", None)
    if schema.get("type") == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        original_required = set(schema.get("required") or [])
        strict_props = {}
        for name, prop in props.items():
            prop = _strict_schema(prop) if isinstance(prop, dict) else prop
            if name not in original_required and isinstance(prop, dict):
                prop = _nullable_copy(prop)
            strict_props[name] = prop
        schema["type"] = "object"
        schema["properties"] = strict_props
        schema["required"] = list(props.keys())
        schema["additionalProperties"] = False
    elif schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        schema["items"] = _strict_schema(schema["items"])
    return schema


def _normalize_chat_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    fn = deepcopy(tool.get("function") or tool)
    # Tools may opt out of strict mode (e.g. polymorphic-payload tools like
    # update_user_profile, where _strict_schema would force every `data` field
    # required / additionalProperties:false and the model is left only able to
    # send an empty object).
    is_strict = bool(fn.get("strict", True))
    raw_params = fn.get("parameters") or {"type": "object", "properties": {}, "required": []}
    params = _strict_schema(raw_params) if is_strict else raw_params
    return {
        "type": "function",
        "function": {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": params,
            "strict": is_strict,
        },
    }


def to_responses_tool(chat_tool: Dict[str, Any]) -> Dict[str, Any]:
    fn = chat_tool.get("function") or chat_tool
    return {
        "type": "function",
        "name": fn["name"],
        "description": fn.get("description", ""),
        "parameters": deepcopy(fn.get("parameters") or {"type": "object", "properties": {}, "required": []}),
        "strict": bool(fn.get("strict", True)),
    }


def tool_name(tool: Dict[str, Any]) -> str:
    fn = tool.get("function") or tool
    return fn.get("name", "")


def _by_name(tools: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {tool_name(t): _normalize_chat_tool(t) for t in tools}


def _has_any(text: str, words: Iterable[str]) -> bool:
    text = (text or "").lower()
    return any(word in text for word in words)


def _explicit_order_confirmation(text: str) -> bool:
    text = (text or "").lower().strip()
    if not text:
        return False
    strong = (
        "bekræft tilmelding", "bekraeft tilmelding", "bekræft bestilling",
        "bekraeft bestilling", "opret ordre", "lav ordren",
        "bestil kurset", "tilmeld mig", "ja tak tilmeld", "ja, tilmeld",
    )
    return any(token in text for token in strong)


def get_employee_tool_selection(
    *,
    logged_in: bool,
    company_id: Optional[Any],
    intent: str,
    user_query: str,
    shown_count: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return strict Chat-style tools plus selection metadata for one employee turn."""
    from app1.tools import OPENAI_TOOLS, PROFILE_TOOLS

    tool_map = _by_name(OPENAI_TOOLS + (PROFILE_TOOLS if logged_in else []))
    query = user_query or ""
    names = set()
    forced_tool = None
    is_approval_query = _has_any(query, ("godkend", "approval", "afventer", "ordrestatus"))

    # CORE tools are ALWAYS on the menu so the MODEL — not a brittle keyword/regex
    # gate — decides when to use them. The keyword branches below only ADD
    # specialised/expensive tools or FORCE a choice; they can no longer EXCLUDE the
    # everyday ones. This is the robust, model-driven design: a profile statement the
    # regex doesn't recognise (e.g. the compound "erhvervserfaring") still lets the
    # model offer to save it, because request_user_input/update_user_profile are
    # always available. Cost is tiny (a few small schemas) and worth the reliability.
    names.add("catalog_search")  # can always search the catalog
    if logged_in:
        names.update({"get_user_profile", "request_user_input", "update_user_profile"})

    # Pure small-talk fast-path: only for genuine greetings/thanks with NO substantive
    # signal. Anything mentioning the catalog OR the user's own background falls through
    # to the model-driven core above (so "jeg har erhvervserfaring …" is never swallowed
    # here even if it were misclassified as chit_chat).
    if intent == "chit_chat" and not _has_any(query, (
            "kursus", "produkt", "budget", "ordre", "profil", "leverandør", "leverandor",
            "erfaring", "uddannelse", "uddannet", "arbejd", "ansat", "kompetence",
            "baggrund", "stilling", "mit job", "min titel", "lære", "laere", "mål", "maal")):
        return [], {"version": TOOLSET_VERSION, "tool_names": [], "forced_tool": None}

    if intent in {"discovery", "follow_up", "profile_and_search"}:
        names.add("catalog_search")
        if _has_any(query, ("under", "over", "budget", "pris", "kr", "københavn", "aarhus", "online", "e-learning")):
            names.add("catalog_search")
    if intent in {"detail"} or _has_any(query, ("[vedhæftet kursus", "handle:", "produkt", "kurset", "detaljer", "hvornår", "dato", "start", "hvor foregår")):
        names.add("catalog_get_product")
        if _has_any(query, ("[vedhæftet kursus", "handle:")):
            forced_tool = "catalog_get_product"
    if intent in {"comparison"} or _has_any(query, ("sammenlign", "forskel", "bedst", "versus", "vs")):
        names.update({"catalog_compare_products", "catalog_get_product"})
    # Refined intents from the LLM router (item #2). These abstract labels only ever
    # arrive after the regex classifier returned its ambiguous "discovery" catch-all,
    # so they pull in the specialised tools that catch-all wouldn't have. catalog_search
    # is always seeded, so a path/gap answer is built from REAL courses on the topic.
    if intent == "skill_gap":
        names.add("analyze_skill_gaps")
    if intent == "learning_path" and logged_in:
        names.update({"get_user_profile", "recommend_for_profile", "suggest_learning_path"})
    if _has_any(query, ("kategori", "category", "type kurser")):
        names.add("catalog_get_category")
        forced_tool = "catalog_get_category"
    if _has_any(query, ("udbyder", "leverandør", "leverandor", "vendor", "hvem er", "fra hvem")):
        names.add("catalog_get_vendor")
        forced_tool = "catalog_get_vendor"
    if (intent in {"buying", "team_buying"} or _has_any(query, ("tilmeld", "bestil", "ordre", "køb", "koeb", "plads"))) and not is_approval_query:
        names.update({"catalog_get_product", "check_course_readiness", "prepare_course_order"})
        if _explicit_order_confirmation(query):
            names.add("create_course_order")
    if is_approval_query:
        names.add("check_order_approval_status")
        forced_tool = "check_order_approval_status"
    if _has_any(query, ("budget", "råd", "raad", "resterende midler")):
        names.add("get_department_budget")
        forced_tool = "get_department_budget" if company_id else forced_tool
    if _has_any(query, ("kompetencegab", "skill gap", "skills gap", "hvad skal jeg lære", "laere",
                        "hvad skal jeg lære for", "kompetencer mangler", "mangler jeg", "mangler kompetence",
                        "for at blive", "for at arbejde med", "hvilke kompetencer", "blive bedre til")):
        names.add("analyze_skill_gaps")
        # Upskilling/career questions ("hvad skal jeg lære for at blive X", "hvilke
        # kompetencer mangler jeg for Y") are fundamentally course-discovery: the user
        # wants concrete courses for the target topic, not just a gap analysis (which
        # is often empty without profile data). Always offer the catalog search too.
        names.add("catalog_search")
    if logged_in:
        if intent in {"profile_update", "profile_and_search"} or _has_any(query, ("profil", "cv", "kompetence", "erfaring", "uddannelse")):
            names.update({"get_user_profile", "update_user_profile", "request_user_input"})
        if _has_any(query, ("anbefal til mig", "min profil", "læringssti", "laeringssti", "næste skridt", "naeste skridt")):
            # +catalog_search so a learning path is built from REAL courses on the topic
            # (a path without surfaced courses isn't actionable).
            names.update({"get_user_profile", "recommend_for_profile", "suggest_learning_path", "catalog_search"})
        if intent in {"profile_update", "profile_and_search"} or _has_any(query, (
                "mål", "maal", "udviklingsplan", "udviklingsmål", "udviklingsmaal", "blive bedre til",
                "vil gerne lære", "vil gerne laere", "vil gerne blive", "karriere", "udvikle mig",
                "lære at", "laere at", "sæt et mål", "saet et maal", "mit mål", "mine mål")):
            names.update({"set_learning_goal", "get_learning_goals", "update_learning_goal", "recommend_for_profile", "catalog_search"})
        # --- Specialised employee tools: keyword-gated only, NOT in the core seed.
        # Each stays off the menu until a matching Danish keyword activates it.
        if _has_any(query, (
                "status på mit kursus", "status paa mit kursus", "hvornår starter", "hvornaar starter",
                "frist", "deadline", "forfald", "mit kursus", "hvor er mit",
                "er jeg forsinket", "hvad mangler")):
            names.add("get_my_course_status")
        if _has_any(query, (
                "rabat", "aftalepris", "hvad koster det med", "firma-rabat",
                "hvad koster det med rabat")):
            names.add("get_negotiated_discount")
        if _has_any(query, (
                "forudsætninger", "forudsaetninger", "krav", "sværhedsgrad", "svaerhedsgrad",
                "hvad kræver", "hvad kraever", "er jeg klar til")):
            names.add("check_course_prerequisites")
        if _has_any(query, (
                "næste kursus", "naeste kursus", "hvad nu", "bygge videre",
                "efter dette", "hvad efter")):
            names.add("get_course_sequel")
        if _has_any(query, (
                "certificering", "cert", "blive certificeret", "pmp", "itil",
                "prince2", "vej til")):
            names.add("find_certification_path")
        if _has_any(query, (
                "mål-progress", "maal-progress", "hvor langt er jeg", "mit mål", "mit maal",
                "mangler jeg")):
            names.add("track_goal_progress")
        if _has_any(query, (
                "kalender", "tilføj til kalender", "tilfoej til kalender", ".ics", "outlook")):
            names.add("add_to_calendar")
        if _has_any(query, (
                "jeg har gennemført", "jeg har gennemfoert", "marker som færdig",
                "marker som faerdig", "fuldført kurset", "fuldfoert kurset")):
            names.add("mark_course_complete")
    if shown_count:
        names.update({"catalog_get_product", "catalog_compare_products"})

    selected = []
    for name in sorted(names):
        tool = tool_map.get(name)
        meta = _EMPLOYEE_META.get(name, ToolMeta(name))
        if not tool:
            continue
        if meta.auth_required and not logged_in:
            continue
        if meta.company_required and not company_id:
            continue
        if meta.side_effect and name not in ("create_course_order", "update_user_profile", "mark_course_complete") and not _explicit_order_confirmation(query):
            continue
        selected.append(tool)

    if not selected:
        selected = [tool_map[name] for name in ("catalog_search", "catalog_get_product") if name in tool_map]

    return selected, {
        "version": TOOLSET_VERSION,
        "tool_names": [tool_name(t) for t in selected],
        "forced_tool": forced_tool if forced_tool in {tool_name(t) for t in selected} else None,
    }


def get_hr_tool_selection(*, company_id: Optional[Any], user_query: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from hr_tools import HR_TOOLS

    tool_map = _by_name(HR_TOOLS)
    query = user_query or ""
    names = {"hr_get_company_learning_context", "get_pending_actions"}
    forced_tool = None
    if _has_any(query, ("hej", "hello", "tak", "thanks", "godmorgen", "god aften")) and len(query.split()) <= 4:
        return [], {"version": TOOLSET_VERSION, "tool_names": [], "forced_tool": None}
    if _has_any(query, ("budget", "forbrug", "økonomi", "remaining", "resterende")):
        names.update({"get_budget_overview", "hr_get_company_learning_context"})
        forced_tool = "get_budget_overview"
    if _has_any(query, ("kompetence", "skill", "gap", "mangler", "mål", "maal")):
        names.update({"get_company_skill_gaps", "hr_recommend_training_plan"})
        forced_tool = "get_company_skill_gaps"
    if _has_any(query, ("træning", "training", "status", "gennemført", "igang", "medarbejder")):
        names.update({"get_team_training_status", "get_employee_overview"})
    if _has_any(query, ("rapport", "roi", "spend", "udgifter", "effekt")):
        names.add("get_training_report")
    if _has_any(query, ("kursus", "kurser", "anbefal", "plan", "uddannelse")):
        names.update({"search_courses_for_team", "hr_recommend_training_plan"})
    if _has_any(query, ("leverandør", "leverandor", "supplier", "vendor", "udbyder", "aftale")):
        names.update({"hr_get_supplier_coverage", "search_courses_for_team"})
        forced_tool = "hr_get_supplier_coverage"
    if _has_any(query, ("chatbot", "ai", "brug", "usage", "risiko", "dårlige", "daarlige")):
        names.update({"get_chatbot_usage_stats", "hr_get_ai_usage_risks"})
    if _has_any(query, ("compliance", "overholdelse", "certificering", "recertificering", "lovpligtig", "obligatorisk", "arbejdsmiljø", "arbejdsmiljo", "gdpr-kursus")):
        names.add("get_compliance_status")
        forced_tool = "get_compliance_status"
    # --- Specialised HR tools: keyword-gated only, NOT in the core seed. Each
    # stays off the menu until a matching Danish keyword activates it. ---
    if _has_any(query, (
            "ikke startet", "ikke begyndt", "hvem mangler at starte", "ikke kommet i gang")):
        names.add("get_team_non_starters")
    if _has_any(query, (
            "compliance", "overholdelse", "lovpligtig", "forfaldne kurser", "hvem er overdue")):
        names.add("hr_team_compliance")
    if _has_any(query, (
            "roi", "afkast", "værdi af træning", "vaerdi af traening",
            "spend per", "spend per medarbejder")):
        names.add("hr_roi_summary")
    if _has_any(query, (
            "benchmark", "sammenlignet med branchen", "peers",
            "hvordan klarer vi os mod peers")):
        names.add("hr_benchmark")
    if _has_any(query, (
            "abonnement", "prøveperiode", "proeveperiode", "pladser", "seats", "licenser",
            "hvor mange pladser har vi tilbage")):
        names.add("hr_trial_and_seat_status")
    if _has_any(query, (
            "godkend ordre", "afvis ordre", "godkend bestilling")):
        names.add("approve_order_from_chat")
    if _has_any(query, (
            "tildel", "tilmeld holdet", "bulk", "tildel læringssti", "tildel laeringssti",
            "bulk-tildel")):
        names.add("assign_learning_path_to_team")
    if _has_any(query, (
            "inaktive", "ikke aktive medarbejdere", "hvem har ikke logget ind")):
        names.add("hr_inactive_employees")
    if _has_any(query, (
            "udløber aftale", "udloeber aftale", "leverandøraftaler udløber",
            "leverandoeraftaler udloeber", "hvilke aftaler skal fornyes")):
        names.add("hr_expiring_agreements")

    selected = []
    for name in sorted(names):
        tool = tool_map.get(name)
        meta = _HR_META.get(name, ToolMeta(name, "hr", company_required=True))
        if not tool:
            continue
        if meta.company_required and not company_id:
            continue
        selected.append(tool)

    if not selected:
        selected = [tool for tool in tool_map.values() if tool_name(tool) in {"hr_get_company_learning_context", "get_pending_actions"}]
    return selected, {
        "version": TOOLSET_VERSION,
        "tool_names": [tool_name(t) for t in selected],
        "forced_tool": forced_tool if forced_tool in {tool_name(t) for t in selected} else None,
    }


def is_parallel_safe(name: str) -> bool:
    meta = _EMPLOYEE_META.get(name) or _HR_META.get(name)
    return True if meta is None else meta.parallel_safe and not meta.side_effect


def tool_cache_ttl(name: str) -> int:
    meta = _EMPLOYEE_META.get(name) or _HR_META.get(name)
    return 0 if meta is None else meta.cache_ttl


def make_tool_choice(tool_name_value: Optional[str]) -> Any:
    if not tool_name_value:
        return "auto"
    return {"name": tool_name_value}


def chat_tool_choice(choice: Any) -> Any:
    if isinstance(choice, dict) and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return choice or "auto"


def responses_tool_choice(choice: Any) -> Any:
    if isinstance(choice, dict) and choice.get("name"):
        return {"type": "function", "name": choice["name"]}
    return choice or "auto"


def sanitize_args_for_tool(name: str, args: Dict[str, Any], tools: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Coerce nullable strict-schema values into the defaults older executors expect."""
    if not isinstance(args, dict):
        return {}
    schema = None
    if tools:
        for tool in tools:
            if tool_name(tool) == name:
                schema = (tool.get("function") or tool).get("parameters") or {}
                break
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    clean = dict(args)
    for key, value in list(clean.items()):
        if value is not None:
            continue
        prop = props.get(key, {})
        typ = prop.get("type") if isinstance(prop, dict) else None
        typ_list = typ if isinstance(typ, list) else [typ]
        if "string" in typ_list:
            clean[key] = ""
        elif "array" in typ_list:
            clean[key] = []
        elif "object" in typ_list:
            clean[key] = {}
    return clean


def toolset_enabled() -> bool:
    return os.getenv("AI_TOOL_ROUTER_V2", "1").lower() not in {"0", "false", "no", "off"}


def normalize_handle_candidate(text: str) -> str:
    value = (text or "").strip().lower()
    value = re.sub(r"[^a-z0-9æøå\- ]+", "", value)
    value = re.sub(r"\s+", "-", value)
    return value
