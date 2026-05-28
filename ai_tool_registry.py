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
    params = _strict_schema(fn.get("parameters") or {"type": "object", "properties": {}, "required": []})
    return {
        "type": "function",
        "function": {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": params,
            "strict": True,
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

    if intent == "chit_chat" and not _has_any(query, ("kursus", "produkt", "budget", "ordre", "profil", "leverandør", "leverandor")):
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
    if _has_any(query, ("kompetencegab", "skill gap", "skills gap", "hvad skal jeg lære", "laere")):
        names.add("analyze_skill_gaps")
    if logged_in:
        if intent in {"profile_update", "profile_and_search"} or _has_any(query, ("profil", "cv", "kompetence", "erfaring", "uddannelse")):
            names.update({"get_user_profile", "update_user_profile", "request_user_input"})
        if _has_any(query, ("anbefal til mig", "min profil", "læringssti", "laeringssti", "næste skridt", "naeste skridt")):
            names.update({"get_user_profile", "recommend_for_profile", "suggest_learning_path"})
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
        if meta.side_effect and name != "create_course_order" and not _explicit_order_confirmation(query):
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
