# Custom RAG and Tool schemas for app1
# Phase 1: Intent classification & query rewriting
# Phase 2: Comparison & recommendation tools
# Phase 5: Hybrid search integration
import re
import json
import datetime
import openai
from app1.rag import semantic_search_courses, semantic_search_courses_detailed, load_augmented_products, hybrid_rank_products

import os as _os


def _normalize_price(price_raw):
    """Normalize price to a consistent display string: 'Gratis', 'kr X', or 'Pris på forespørgsel'."""
    if price_raw is None:
        return "Pris på forespørgsel"
    price_str = str(price_raw).strip()
    if price_str in ("0", "0.00", "0.0", "", "None", "N/A"):
        return "Gratis"
    try:
        val = float(price_str)
        if val == 0:
            return "Gratis"
        return f"kr {price_str}"
    except (ValueError, TypeError):
        return "Pris på forespørgsel"


# Tags to exclude from compact results (too generic or region-based)
_EXCLUDED_TAG_PREFIXES = {"region:", "by:", "land:"}
_EXCLUDED_TAGS = {"kursus", "kurser", "uddannelse", "training", "course", "denmark", "danmark"}

# 4.3: Vendor profiles
_VENDOR_PROFILES = None

def _load_vendor_profiles():
    global _VENDOR_PROFILES
    if _VENDOR_PROFILES is None:
        try:
            vp_path = _os.path.join(_os.path.dirname(__file__), "vendor_profiles.json")
            with open(vp_path, "r", encoding="utf-8") as f:
                _VENDOR_PROFILES = json.load(f)
        except Exception as e:
            print(f"[Vendor Profiles] Could not load: {e}")
            _VENDOR_PROFILES = {}
    return _VENDOR_PROFILES

# ── Location aliases for fuzzy matching (Improvement #5) ──
_LOCATION_ALIASES = {
    "kbh": "københavn",
    "kbh.": "københavn",
    "copenhagen": "københavn",
    "cph": "københavn",
    "århus": "aarhus",
    "odense c": "odense",
    "aalborg c": "aalborg",
    "ålborg": "aalborg",
}

# Copenhagen metro area — searching "København" should also match these
_COPENHAGEN_METRO = {"frederiksberg", "herlev", "ballerup", "glostrup", "taastrup",
                     "lyngby", "gentofte", "hellerup", "valby", "vanløse", "amager",
                     "nordhavn", "ørestad", "brøndby", "hvidovre", "rødovre"}


def _normalize_location(loc_input):
    """Normalize a location query: apply aliases, lowercase."""
    loc = loc_input.lower().strip()
    return _LOCATION_ALIASES.get(loc, loc)


def _location_matches(query_loc, variant_loc_raw):
    """Check if a normalized query location matches a variant location string."""
    variant_loc = variant_loc_raw.lower()
    normalized = _normalize_location(query_loc)

    # Direct substring match
    if normalized in variant_loc:
        return True

    # Copenhagen metro expansion: if searching for "københavn", also match metro cities
    if normalized == "københavn":
        for metro_city in _COPENHAGEN_METRO:
            if metro_city in variant_loc:
                return True

    return False

def extract_city_name(address):
    """Turn addresses like 'Kongsvang Alle 29, 8000 Aarhus C' into 'Aarhus'."""
    if not address:
        return None
    m = re.search(r'\b\d{4}\s+([A-ZÆØÅa-zæøå]+)', address)
    if m:
        return m.group(1)
    if ',' not in address and not re.search(r'\d', address):
        return address.strip()
    parts = address.split(',')
    last_part = parts[-1].strip()
    cleaned = re.sub(r'\b\d{4}\s*', '', last_part).strip()
    return cleaned if cleaned else address.strip()

def _extract_compact_fields(product):
    """Extract enriched compact fields from a product."""
    variants = product.get("variants", [])
    price = _normalize_price(variants[0].get("price") if variants else None)

    raw_locations = [v.get("option1") for v in variants if v.get("option1")]
    cities = list(dict.fromkeys(extract_city_name(loc) for loc in raw_locations if extract_city_name(loc)))[:3]

    dates = [v.get("option2") for v in variants if v.get("option2")][:4]
    product_type = product.get("product_type", "")

    all_tags = product.get("tags", [])
    if isinstance(all_tags, str):
        all_tags = [t.strip() for t in all_tags.split(",")]
    filtered_tags = []
    for tag in all_tags:
        tag_lower = tag.lower().strip()
        if tag_lower in _EXCLUDED_TAGS:
            continue
        if any(tag_lower.startswith(p) for p in _EXCLUDED_TAG_PREFIXES):
            continue
        if tag.strip():
            filtered_tags.append(tag.strip())
        if len(filtered_tags) >= 4:
            break

    result = {
        "title": product.get("title"),
        "handle": product.get("handle"),
        "vendor": product.get("vendor"),
        "price": price,
        "summary": product.get("ai_summary"),
        "locations": cities,
        "dates": dates,
        "product_type": product_type,
        "tags": filtered_tags,
    }

    # 4.2: Include structured metadata if available
    meta = product.get("structured_metadata", {})
    if meta:
        if meta.get("duration_days"):
            result["duration_days"] = meta["duration_days"]
        if meta.get("difficulty"):
            result["difficulty"] = meta["difficulty"]
        if meta.get("certification"):
            result["certification"] = meta["certification"]
        if meta.get("includes"):
            result["includes"] = meta["includes"][:3]
        if meta.get("target_audience"):
            result["target_audience"] = meta["target_audience"][:2]

    return result


# ── Tool Definitions ──

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_courses",
            "description": "Perform a semantic + keyword hybrid search to find courses matching the user's criteria. Use this whenever a user is looking for course recommendations or asking general discovery questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The main search query (e.g. 'Ledelse', 'IT kursus', 'Noget med personlig udvikling')."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of courses to return. Default is 3.",
                        "default": 3
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "filter_courses",
            "description": "Filter and search courses by structured criteria like location, price range, product type, or tags. Use this when the user has specific constraints (e.g. 'e-learning under 5000 kr i Aarhus'). Can optionally rank results by semantic relevance if a query is also provided.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or location filter (case-insensitive partial match, e.g. 'Aarhus', 'København')."
                    },
                    "price_min": {
                        "type": "number",
                        "description": "Minimum price in DKK."
                    },
                    "price_max": {
                        "type": "number",
                        "description": "Maximum price in DKK."
                    },
                    "product_type": {
                        "type": "string",
                        "description": "Product type filter (e.g. 'E-learning', 'Kursus', 'Certificering')."
                    },
                    "tag": {
                        "type": "string",
                        "description": "Category tag filter (e.g. 'IT-professionel', 'Personlig udvikling', 'Projektledelse')."
                    },
                    "start_after": {
                        "type": "string",
                        "description": "Only include courses with a start date on or after this date. Format: YYYY-MM-DD (e.g. '2026-04-01')."
                    },
                    "start_before": {
                        "type": "string",
                        "description": "Only include courses with a start date on or before this date. Format: YYYY-MM-DD (e.g. '2026-06-30')."
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional semantic search query to rank filtered results by relevance."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of results to return. Default is 5.",
                        "default": 5
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_course_details",
            "description": "Get the exact details (price, dates, locations, description) for a specific course by its exact handle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "The exact shopify handle of the product."
                    }
                },
                "required": ["handle"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compare_courses",
            "description": "Compare 2-4 courses side by side. Use when the user asks to compare courses, wants to know differences, or asks 'which is better?'. Returns a structured comparison.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of 2-4 product handles to compare.",
                        "minItems": 2,
                        "maxItems": 4
                    }
                },
                "required": ["handles"]
            }
        }
    },
    # 4.3: Vendor intelligence tool
    {
        "type": "function",
        "function": {
            "name": "get_vendor_info",
            "description": "Get information about a course vendor/provider: their specializations, reputation, typical price range, locations, and what they're best for. Use when the user asks about a vendor ('Hvem er Teknologisk Institut?'), wants to know which vendor is best for a topic, or when comparing courses from different vendors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_name": {
                        "type": "string",
                        "description": "The vendor name to look up (e.g. 'Teknologisk Institut', 'SuperUsers')."
                    },
                    "topic": {
                        "type": "string",
                        "description": "Optional topic to find the best vendor for (e.g. 'ITIL', 'projektledelse', 'Excel')."
                    }
                },
                "required": []
            }
        }
    }
]


# ── Tool Execution ──

# Module-level state for contextual search (set by agent before tool execution)
_current_shown_handles = set()
_current_user_prefs = {}


def set_search_context(shown_handles=None, user_prefs=None):
    """Set contextual search state for the current request.
    Called by agent.py before tool execution to pass session context."""
    global _current_shown_handles, _current_user_prefs
    _current_shown_handles = shown_handles or set()
    _current_user_prefs = user_prefs or {}


def _execute_search_courses(args):
    """Handle search_courses tool execution with hybrid search."""
    query = args.get("query", "")
    limit = args.get("limit", 3)

    detailed = semantic_search_courses_detailed(
        query, limit=limit,
        shown_handles=_current_shown_handles,
        user_prefs=_current_user_prefs
    )

    if isinstance(detailed, dict) and "error" in detailed:
        return json.dumps({"status": "error", "message": detailed["message"]})

    results = detailed.get("products", [])
    confidence = detailed.get("confidence", "medium")

    if not results:
        return json.dumps({"status": "no_results", "message": "Ingen kurser matchede din søgning.", "confidence": confidence})

    compact_results = [_extract_compact_fields(r) for r in results]

    # Mark re-shown products so the AI can address it
    for cr in compact_results:
        if cr.get("handle") in _current_shown_handles:
            cr["previously_shown"] = True

    return json.dumps({
        "status": "success",
        "count": len(compact_results),
        "results": compact_results,
        "raw_products": results,
        "search_debug": detailed.get("debug", {}),
        "confidence": confidence,
    })


def _execute_filter_courses(args):
    """Handle filter_courses tool execution with hybrid ranking."""
    location = args.get("location", "").lower().strip()
    price_min = args.get("price_min")
    price_max = args.get("price_max")
    product_type = args.get("product_type", "").lower().strip()
    tag = args.get("tag", "").lower().strip()
    start_after_str = args.get("start_after", "").strip()
    start_before_str = args.get("start_before", "").strip()
    query = args.get("query", "").strip()
    limit = args.get("limit", 5)

    # Parse date filters
    start_after = None
    start_before = None
    try:
        if start_after_str:
            start_after = datetime.date.fromisoformat(start_after_str)
        if start_before_str:
            start_before = datetime.date.fromisoformat(start_before_str)
    except ValueError:
        pass  # Ignore invalid date formats

    products = load_augmented_products()
    if not products:
        return json.dumps({"status": "error", "message": "Produktindekset er ikke indlæst."})

    filtered = []
    for p in products:
        variants = p.get("variants", [])

        if location:
            variant_locs = [v.get("option1", "") for v in variants if v.get("option1")]
            if not any(_location_matches(location, vloc) for vloc in variant_locs):
                continue

        if price_min is not None or price_max is not None:
            try:
                price_val = float(variants[0].get("price", 0)) if variants else 0
            except (ValueError, TypeError):
                price_val = 0
            if price_min is not None and price_val < float(price_min):
                continue
            if price_max is not None and price_val > float(price_max):
                continue

        if product_type:
            pt = p.get("product_type", "").lower()
            if product_type not in pt:
                continue

        if tag:
            all_tags = p.get("tags", [])
            if isinstance(all_tags, str):
                all_tags = [t.strip().lower() for t in all_tags.split(",")]
            else:
                all_tags = [t.lower() for t in all_tags]
            if not any(tag in t for t in all_tags):
                continue

        # Date range filter: check if any variant date falls within range
        if start_after or start_before:
            from app1 import parse_danish_date
            has_matching_date = False
            for v in variants:
                raw_date = (v.get("option2") or "").strip()
                dt = parse_danish_date(raw_date)
                if dt:
                    if start_after and dt < start_after:
                        continue
                    if start_before and dt > start_before:
                        continue
                    has_matching_date = True
                    break
            if not has_matching_date:
                continue

        filtered.append(p)

    # Use hybrid ranking when query is provided (Phase 5)
    if query and filtered:
        filtered = hybrid_rank_products(filtered, query, products, limit=limit)
    else:
        def sort_price(p):
            try:
                return float(p.get("variants", [{}])[0].get("price", 999999))
            except (ValueError, TypeError, IndexError):
                return 999999
        filtered.sort(key=sort_price)
        filtered = filtered[:limit]

    if not filtered:
        return json.dumps({"status": "no_results", "message": "Ingen kurser matchede dine filtre."})

    # Deprioritize already-shown products (move to end, don't remove)
    if _current_shown_handles:
        new_results = [p for p in filtered if p.get("handle") not in _current_shown_handles]
        reshown = [p for p in filtered if p.get("handle") in _current_shown_handles]
        filtered = new_results + reshown

    compact_results = [_extract_compact_fields(r) for r in filtered]

    # Mark re-shown products
    for cr in compact_results:
        if cr.get("handle") in _current_shown_handles:
            cr["previously_shown"] = True

    return json.dumps({
        "status": "success",
        "count": len(compact_results),
        "results": compact_results,
        "raw_products": filtered
    })


def _execute_get_course_details(args):
    """Handle get_course_details tool execution."""
    handle = args.get("handle")
    products = load_augmented_products()

    for p in products:
        if p.get("handle") == handle:
            raw_locs = [v.get("option1") for v in p.get("variants", []) if v.get("option1")]
            locations = list(dict.fromkeys(extract_city_name(loc) for loc in raw_locs if extract_city_name(loc)))
            dates = [v.get("option2") for v in p.get("variants", []) if v.get("option2")]
            return json.dumps({
                "title": p.get("title"),
                "price": _normalize_price(p.get("variants", [{}])[0].get("price") if p.get("variants") else None),
                "vendor": p.get("vendor"),
                "locations": locations,
                "upcoming_dates": dates,
                "description": p.get("ai_summary", p.get("body_html", "")[:200]),
                "raw_product": p
            })

    return json.dumps({"status": "not_found", "message": f"Kunne ikke finde kursus med handle '{handle}'."})


def _execute_compare_courses(args):
    """Phase 2: Compare 2-4 courses side by side."""
    handles = args.get("handles", [])
    if len(handles) < 2:
        return json.dumps({"status": "error", "message": "Mindst 2 kurser kræves for sammenligning."})

    products = load_augmented_products()
    handle_map = {p.get("handle"): p for p in products}

    comparisons = []
    raw_products = []
    for handle in handles[:4]:
        p = handle_map.get(handle)
        if not p:
            continue
        raw_products.append(p)
        variants = p.get("variants", [])
        locations = list(set(extract_city_name(v.get("option1", "")) for v in variants if v.get("option1") and extract_city_name(v.get("option1"))))
        dates = [v.get("option2") for v in variants if v.get("option2")][:3]

        comparisons.append({
            "title": p.get("title"),
            "handle": p.get("handle"),
            "price": _normalize_price(variants[0].get("price") if variants else None),
            "vendor": p.get("vendor"),
            "product_type": p.get("product_type", ""),
            "locations": locations[:3],
            "upcoming_dates": dates,
            "summary": p.get("ai_summary", "")[:200],
            "variant_count": len(variants),
        })

    if len(comparisons) < 2:
        return json.dumps({"status": "error", "message": "Kunne ikke finde nok kurser til sammenligning."})

    return json.dumps({
        "status": "success",
        "comparison": comparisons,
        "raw_products": raw_products
    })


# ── Profile Management Tools (connected to MySQL user system) ──

PROFILE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": "Hent brugerens fulde profil: kompetencer, erfaring, uddannelse, gennemførte kurser og præferencer. Brug dette til at give personlige anbefalinger baseret på brugerens baggrund.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_profile",
            "description": "Opdater brugerens profil: tilføj/fjern kompetencer, erfaring, uddannelse, gennemførte kurser, eller opdater profiloversigt (headline, bio, mål, præferencer). Brug dette når brugeren fortæller om sig selv, tilføjer en kompetence, eller vil opdatere sin profil.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_skill", "remove_skill", "update_skill_level",
                                 "add_experience", "remove_experience",
                                 "add_education", "remove_education",
                                 "add_course", "remove_course",
                                 "update_summary"],
                        "description": "The profile update action to perform."
                    },
                    "data": {
                        "type": "object",
                        "description": "Data for the action. For add_skill: {skill_name, skill_level?}. For remove_skill: {skill_name}. For update_skill_level: {skill_name, skill_level}. For add_experience: {title, company?, start_year?, end_year?, is_current?, description?}. For remove_experience: {id}. For add_education: {degree, institution?, year_completed?}. For remove_education: {id}. For add_course: {course_title, course_handle?, vendor?, completed_date?}. For remove_course: {course_title}. For update_summary: {headline?, bio?, goals?, preferred_location?, preferred_format?, budget_range?}."
                    }
                },
                "required": ["action", "data"]
            }
        }
    }
]


def _execute_get_user_profile(args, username):
    """Fetch the full user profile from MySQL."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    try:
        from app1.user_profile_db import get_full_profile, format_profile_for_ai, ensure_tables
        ensure_tables()
        profile = get_full_profile(username)
        formatted = format_profile_for_ai(profile)
        return json.dumps({
            "status": "success",
            "profile": profile,
            "formatted": formatted if formatted else "Brugeren har endnu ikke udfyldt sin profil."
        })
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Fejl ved hentning af profil: {e}"})


def _execute_update_user_profile(args, username):
    """Validate profile update and return proposed action for user confirmation.
    Add-actions are NOT executed here — they go through frontend confirmation first.
    Remove/update actions execute immediately."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})

    action = args.get("action", "")
    data = args.get("data", {})

    try:
        from app1 import user_profile_db as db
        db.ensure_tables()

        # ── Add-actions: validate + return proposed (not executed yet) ──

        if action == "add_skill":
            name = data.get("skill_name", "").strip()
            level = data.get("skill_level", "mellem")
            if not name or len(name) < 2:
                return json.dumps({"status": "error", "message": "skill_name mangler eller er for kort."})
            try:
                existing = db.get_skills(username)
                existing_map = {s["skill_name"].lower(): s for s in existing}
                if name.lower() in existing_map:
                    old_level = existing_map[name.lower()]["skill_level"]
                    level_order = ["begynder", "mellem", "avanceret", "ekspert"]
                    if level in level_order and old_level in level_order:
                        if level_order.index(level) > level_order.index(old_level):
                            return json.dumps({"status": "proposed", "section": "skills",
                                "message": f'Opgradér "{name}" fra {old_level} til {level}?',
                                "confirm": {"action": "add_skill", "data": {"skill_name": name, "skill_level": level}}})
                    return json.dumps({"status": "already_exists",
                        "message": f'"{name}" findes allerede ({old_level}).'})
            except Exception as dup_err:
                print(f"[ProfileUpdate] Skill duplicate check failed (proceeding): {dup_err}")
                try:
                    from flask import current_app as _app
                    _app.mysql.connection.rollback()
                except Exception:
                    pass
            return json.dumps({"status": "proposed", "section": "skills",
                "message": f'Tilføj kompetence: {name} ({level})',
                "confirm": {"action": "add_skill", "data": {"skill_name": name, "skill_level": level}}})

        elif action == "add_experience":
            title = data.get("title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "title mangler."})
            company = data.get("company", "").strip()
            # Check for duplicates (non-blocking — if DB fails, just propose)
            try:
                existing = db.get_experience(username)
                for e in existing:
                    if e["title"].lower() == title.lower() and (e.get("company", "") or "").lower() == company.lower():
                        return json.dumps({"status": "already_exists",
                            "message": f'Erfaring "{title}" @ {company or "ukendt"} findes allerede.'})
            except Exception as dup_err:
                print(f"[ProfileUpdate] Duplicate check failed (proceeding): {dup_err}")
                try:
                    from flask import current_app as _app
                    _app.mysql.connection.rollback()
                except Exception:
                    pass
            label = f'{title}' + (f' @ {company}' if company else '')
            return json.dumps({"status": "proposed", "section": "experience",
                "message": f'Tilføj erfaring: {label}',
                "confirm": {"action": "add_experience", "data": data}})

        elif action == "add_education":
            degree = data.get("degree", "").strip()
            if not degree:
                return json.dumps({"status": "error", "message": "degree mangler."})
            institution = data.get("institution", "").strip()
            try:
                existing = db.get_education(username)
                for e in existing:
                    if e["degree"].lower() == degree.lower() and (e.get("institution", "") or "").lower() == institution.lower():
                        return json.dumps({"status": "already_exists",
                            "message": f'Uddannelse "{degree}" fra {institution or "ukendt"} findes allerede.'})
            except Exception as dup_err:
                print(f"[ProfileUpdate] Education duplicate check failed (proceeding): {dup_err}")
                try:
                    from flask import current_app as _app
                    _app.mysql.connection.rollback()
                except Exception:
                    pass
            label = degree + (f' — {institution}' if institution else '')
            return json.dumps({"status": "proposed", "section": "education",
                "message": f'Tilføj uddannelse: {label}',
                "confirm": {"action": "add_education", "data": data}})

        elif action == "add_course":
            title = data.get("course_title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "course_title mangler."})
            vendor = data.get("vendor", "")
            label = title + (f' ({vendor})' if vendor else '')
            return json.dumps({"status": "proposed", "section": "courses",
                "message": f'Tilføj kursus: {label}',
                "confirm": {"action": "add_course", "data": data}})

        # ── Remove/update actions: execute immediately (no confirmation needed) ──

        elif action == "remove_skill":
            name = data.get("skill_name", "").strip()
            if not name:
                return json.dumps({"status": "error", "message": "skill_name mangler."})
            removed = db.remove_skill(username, name)
            if removed:
                return json.dumps({"status": "success", "section": "skills", "message": f'"{name}" fjernet fra kompetencer.'})
            return json.dumps({"status": "not_found", "message": f'Kompetencen "{name}" blev ikke fundet.'})

        elif action == "update_skill_level":
            name = data.get("skill_name", "").strip()
            level = data.get("skill_level", "")
            if not name or not level:
                return json.dumps({"status": "error", "message": "skill_name og skill_level kræves."})
            if level not in ("begynder", "mellem", "avanceret", "ekspert"):
                return json.dumps({"status": "error", "message": f'Ugyldigt niveau: "{level}". Brug: begynder/mellem/avanceret/ekspert.'})
            updated = db.update_skill_level(username, name, level)
            if updated:
                return json.dumps({"status": "success", "section": "skills", "message": f'"{name}" opdateret til {level}.'})
            return json.dumps({"status": "not_found", "message": f'Kompetencen "{name}" blev ikke fundet.'})

        elif action == "remove_experience":
            exp_id = data.get("id")
            if not exp_id:
                return json.dumps({"status": "error", "message": "id mangler."})
            removed = db.remove_experience(username, exp_id)
            if removed:
                return json.dumps({"status": "success", "section": "experience", "message": "Erfaring fjernet."})
            return json.dumps({"status": "not_found", "message": "Erfaring ikke fundet."})

        elif action == "remove_education":
            edu_id = data.get("id")
            if not edu_id:
                return json.dumps({"status": "error", "message": "id mangler."})
            removed = db.remove_education(username, edu_id)
            if removed:
                return json.dumps({"status": "success", "section": "education", "message": "Uddannelse fjernet."})
            return json.dumps({"status": "not_found", "message": "Uddannelse ikke fundet."})

        elif action == "add_course":
            pass  # handled above in proposed section

        elif action == "remove_course":
            title = data.get("course_title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "course_title mangler."})
            removed = db.remove_completed_course(username, title)

        elif action == "remove_course":
            title = data.get("course_title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "course_title mangler."})
            removed = db.remove_completed_course(username, title)
            if removed:
                return json.dumps({"status": "success", "section": "courses", "message": f'Kursus "{title}" fjernet.'})
            return json.dumps({"status": "not_found", "message": f'Kursus "{title}" ikke fundet.'})

        elif action == "update_summary":
            # Fallback: if AI put fields at top level instead of in data
            if not data:
                summary_fields = {"headline", "bio", "goals", "preferred_location", "preferred_format", "budget_range", "summary"}
                data = {k: v for k, v in args.items() if k in summary_fields and v}
                # Map "summary" → "bio" if AI used generic key
                if "summary" in data and "bio" not in data:
                    data["bio"] = data.pop("summary")
            # Filter out empty strings so we only update actual changes
            clean_data = {k: v for k, v in data.items() if v is not None and str(v).strip()}
            if not clean_data:
                return json.dumps({"status": "error", "message": "Ingen data at opdatere."})
            db.update_profile_summary(username, **clean_data)
            fields_updated = ", ".join(clean_data.keys())
            return json.dumps({"status": "success", "section": "summary",
                "message": f'Profil opdateret: {fields_updated}'})

        else:
            return json.dumps({"status": "error", "message": f"Ukendt action: {action}"})

    except Exception as e:
        print(f"[ProfileUpdate Error] action={action}, username={username}, error={e}")
        try:
            from flask import current_app as _app
            _app.mysql.connection.rollback()
        except Exception:
            pass
        return json.dumps({"status": "error", "message": f"Fejl: {e}"})


def _execute_recommend_for_profile(args, username):
    """Phase 5A: Recommend courses based on user profile gaps."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    try:
        from app1.user_profile_db import get_full_profile, ensure_tables
        ensure_tables()
        profile = get_full_profile(username)

        # Build search query from goals + low-level skills
        query_parts = []
        focus = args.get("focus", "").strip()
        if focus:
            query_parts.append(focus)

        goals = profile.get("goals", "")
        if goals:
            query_parts.append(goals[:100])

        # Target skills at begynder/mellem level for upskilling
        low_skills = [s["name"] for s in profile.get("skills", []) if s.get("level") in ("begynder", "mellem")]
        if low_skills:
            query_parts.extend(low_skills[:3])

        if not query_parts:
            query_parts.append("populære kurser")

        search_query = " ".join(query_parts)
        detailed = semantic_search_courses_detailed(search_query, limit=6,
                                                     shown_handles=_current_shown_handles,
                                                     user_prefs=_current_user_prefs)

        if isinstance(detailed, dict) and "error" in detailed:
            return json.dumps({"status": "error", "message": detailed["message"]})

        results = detailed.get("products", [])

        # Filter out completed courses
        completed_titles = {c["title"].lower() for c in profile.get("completed_courses", [])}
        completed_handles = {c.get("handle", "").lower() for c in profile.get("completed_courses", []) if c.get("handle")}

        filtered = []
        for r in (results or []):
            title_lower = r.get("title", "").lower()
            handle_lower = r.get("handle", "").lower()
            if title_lower in completed_titles or handle_lower in completed_handles:
                continue
            filtered.append(r)

        if not filtered:
            return json.dumps({"status": "no_results", "message": "Ingen nye kursusanbefalinger fundet baseret på din profil."})

        compact_results = [_extract_compact_fields(r) for r in filtered[:4]]

        # Annotate with reason
        all_skills = {s["name"].lower(): s["level"] for s in profile.get("skills", [])}
        for cr in compact_results:
            reason_parts = []
            title_lower = cr.get("title", "").lower()
            for skill_name, skill_level in all_skills.items():
                if skill_name in title_lower and skill_level in ("begynder", "mellem"):
                    reason_parts.append(f"bygger videre på din {skill_name}-kompetence")
            if goals and any(w in title_lower for w in goals.lower().split()[:3]):
                reason_parts.append("matcher dine mål")
            cr["recommendation_reason"] = "; ".join(reason_parts) if reason_parts else "relevant for din profil"

        return json.dumps({
            "status": "success",
            "count": len(compact_results),
            "results": compact_results,
            "raw_products": filtered[:4]
        })
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Fejl: {e}"})


PROFILE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "request_user_input",
        "description": (
            "Vis et interaktivt UI-kort i chatten for at indsamle eller bekræfte information fra brugeren. "
            "Brug dette til at:\n"
            "- Bekræfte profildata med ekstra felter (f.eks. erfaring med startår/slutår)\n"
            "- Samle information via en formular (f.eks. uddannelsesdetaljer)\n"
            "- Give brugeren valgmuligheder (f.eks. 'online' vs 'fysisk')\n"
            "Kortet vises inline i chatten. Brugeren udfylder og klikker 'Gem'. "
            "Data gemmes automatisk i profilen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ui_type": {
                    "type": "string",
                    "enum": ["confirm", "form", "choice"],
                    "description": "confirm = vis Gem/Nej tak med forudfyldt data. form = vis inputfelter brugeren skal udfylde. choice = vis valgmuligheder."
                },
                "message": {
                    "type": "string",
                    "description": "Kort besked der vises øverst i kortet (dansk)."
                },
                "section": {
                    "type": "string",
                    "enum": ["skills", "experience", "education", "courses", "summary"],
                    "description": "Hvilken profil-sektion dette handler om."
                },
                "save_action": {
                    "type": "string",
                    "enum": ["add_skill", "add_experience", "add_education", "add_course", "update_summary"],
                    "description": "Hvilken profil-handling der udføres når brugeren bekræfter."
                },
                "prefilled": {
                    "type": "object",
                    "description": "Data der allerede er kendt (vises som forudfyldt/låst). F.eks. {\"title\": \"Varehuschef\", \"company\": \"Bilka\", \"is_current\": true}."
                },
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Feltnavn (bruges som nøgle i data)"},
                            "label": {"type": "string", "description": "Dansk label vist til brugeren"},
                            "type": {"type": "string", "enum": ["text", "number", "select"], "description": "Input-type"},
                            "placeholder": {"type": "string"},
                            "required": {"type": "boolean"},
                            "options": {"type": "array", "items": {"type": "string"}, "description": "Kun for select-type"}
                        },
                        "required": ["name", "label", "type"]
                    },
                    "description": "Felter brugeren skal udfylde. Udelad for confirm-type."
                },
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"}
                        }
                    },
                    "description": "Kun for choice-type: valgmuligheder brugeren kan vælge imellem."
                }
            },
            "required": ["ui_type", "message", "section", "save_action"]
        }
    }
})


PROFILE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "suggest_learning_path",
        "description": "Byg en sekventiel læringssti baseret på brugerens profil, kompetencer og mål. Analyserer kompetencehuller og foreslår kurser i logisk rækkefølge (fundament → mellemniveau → avanceret). Brug dette når brugeren spørger 'hvad bør jeg lære i hvilken rækkefølge?', 'lav en læringsplan', eller 'hvad er næste skridt?'.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Brugerens overordnede mål (f.eks. 'blive IT-projektleder', 'mestre data analytics')."
                },
                "focus_area": {
                    "type": "string",
                    "description": "Valgfrit fokusområde (f.eks. 'ledelse', 'data', 'IT-sikkerhed')."
                }
            },
            "required": []
        }
    }
})


PROFILE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "recommend_for_profile",
        "description": "Anbefal kurser baseret på brugerens profil: kompetenceniveauer, mål, og gennemførte kurser. Filtrerer automatisk allerede gennemførte kurser fra. Brug dette når brugeren spørger 'hvad bør jeg lære?', 'anbefal noget til mig', eller lignende.",
        "parameters": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "Valgfrit fokusområde for anbefalinger (f.eks. 'ledelse', 'programmering')."
                }
            },
            "required": []
        }
    }
})


def _execute_suggest_learning_path(args, username):
    """6.2: Build a sequenced learning path based on user profile and goals."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    try:
        from app1.user_profile_db import get_full_profile, ensure_tables
        ensure_tables()
        profile = get_full_profile(username)

        goal = args.get("goal", "").strip()
        focus_area = args.get("focus_area", "").strip()

        # Build context for GPT-4o-mini to reason about skill gaps
        context_parts = []
        skills = profile.get("skills", [])
        if skills:
            skill_strs = [f"{s['name']} ({s['level']})" for s in skills]
            context_parts.append(f"Nuværende kompetencer: {', '.join(skill_strs)}")
        completed = profile.get("completed_courses", [])
        if completed:
            context_parts.append(f"Gennemførte kurser: {', '.join(c['title'] for c in completed[:8])}")
        if profile.get("goals"):
            context_parts.append(f"Mål: {profile['goals']}")
        if goal:
            context_parts.append(f"Specifikt mål: {goal}")
        if focus_area:
            context_parts.append(f"Fokusområde: {focus_area}")
        exp = profile.get("experience", [])
        if exp:
            context_parts.append(f"Erfaring: {exp[0].get('title', '')} @ {exp[0].get('company', '')}")

        if not context_parts:
            context_parts.append("Ingen profildata — foreslå en generel læringssti")
            if goal:
                context_parts.append(f"Mål: {goal}")

        context = "\n".join(context_parts)

        # Use GPT-4o-mini to plan the learning path
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """Du er en uddannelsesrådgiver. Analyser brugerens profil og lav en læringssti.
Svar som JSON med denne struktur:
{
  "path_title": "Overskrift for læringssti",
  "steps": [
    {"order": 1, "level": "fundament", "topic": "emne at søge efter", "reason": "hvorfor dette er næste skridt"},
    {"order": 2, "level": "mellemniveau", "topic": "...", "reason": "..."},
    {"order": 3, "level": "avanceret", "topic": "...", "reason": "..."}
  ]
}
Regler:
- Maks 4 trin
- Hvert trin bygger logisk på det forrige
- "topic" skal være et konkret søgeord der kan bruges til at finde kurser
- Spring kompetencer over som brugeren allerede mestrer (avanceret/ekspert niveau)
- Vær specifik — "PRINCE2 Foundation" er bedre end "projektledelse"
- Skriv på dansk"""},
                {"role": "user", "content": context}
            ],
            temperature=0.3,
            max_tokens=500
        )

        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        path_plan = json.loads(raw)

        # Search for courses matching each step
        steps_with_courses = []
        for step in path_plan.get("steps", [])[:4]:
            topic = step.get("topic", "")
            if not topic:
                continue
            results = semantic_search_courses(topic, limit=2,
                                               shown_handles=_current_shown_handles)
            courses = []
            if isinstance(results, list):
                courses = [_extract_compact_fields(r) for r in results[:2]]

            steps_with_courses.append({
                "order": step.get("order", len(steps_with_courses) + 1),
                "level": step.get("level", ""),
                "topic": topic,
                "reason": step.get("reason", ""),
                "courses": courses,
                "raw_products": results[:2] if isinstance(results, list) else []
            })

        return json.dumps({
            "status": "success",
            "path_title": path_plan.get("path_title", "Din læringssti"),
            "steps": steps_with_courses,
            "raw_products": [c for step in steps_with_courses for c in step.get("raw_products", [])]
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Fejl ved opbygning af læringssti: {e}"})


def _execute_get_vendor_info(args):
    """4.3: Get vendor profile information."""
    vendor_name = args.get("vendor_name", "").strip()
    topic = args.get("topic", "").strip().lower()
    profiles = _load_vendor_profiles()

    if vendor_name:
        # Direct vendor lookup
        profile = profiles.get(vendor_name)
        if not profile:
            # Fuzzy match: try case-insensitive partial match
            for key, val in profiles.items():
                if key == "_default":
                    continue
                if vendor_name.lower() in key.lower() or key.lower() in vendor_name.lower():
                    profile = val
                    vendor_name = key
                    break
        if not profile:
            profile = profiles.get("_default", {})
            return json.dumps({
                "status": "not_found",
                "message": f"Ingen detaljeret profil fundet for '{vendor_name}'.",
                "available_vendors": [k for k in profiles.keys() if k != "_default"]
            })
        return json.dumps({
            "status": "success",
            "vendor": vendor_name,
            "profile": profile
        })

    elif topic:
        # Find best vendors for a topic
        matches = []
        for key, val in profiles.items():
            if key == "_default":
                continue
            specs = [s.lower() for s in val.get("specializations", [])]
            best_for = val.get("best_for", "").lower()
            score = 0
            if any(topic in s for s in specs):
                score = 2
            elif topic in best_for:
                score = 1
            if score > 0:
                matches.append({"vendor": key, "score": score, "best_for": val.get("best_for", ""), "reputation": val.get("reputation", ""), "price_range": val.get("price_range", "")})
        matches.sort(key=lambda x: x["score"], reverse=True)
        if matches:
            return json.dumps({
                "status": "success",
                "topic": topic,
                "recommended_vendors": matches[:3]
            })
        return json.dumps({
            "status": "no_match",
            "message": f"Ingen specifik udbyder-anbefaling fundet for '{topic}'.",
            "tip": "Prøv at søge med search_courses i stedet."
        })

    return json.dumps({"status": "error", "message": "Angiv enten vendor_name eller topic."})


def _execute_request_user_input(args, username):
    """Pass-through: AI requested a UI card. No DB calls — just return the card spec."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    ui_type = args.get("ui_type", "confirm")
    message = args.get("message", "")
    if not message:
        return json.dumps({"status": "error", "message": "Besked mangler."})
    return json.dumps({
        "status": "ui_card",
        "ui_type": ui_type,
        "message": message,
        "section": args.get("section", "summary"),
        "save_action": args.get("save_action", ""),
        "prefilled": args.get("prefilled", {}),
        "fields": args.get("fields", []),
        "choices": args.get("choices", []),
    })


def execute_tool(tool_call, username=None):
    """Router to execute the requested tool and return the output."""
    function_name = tool_call.function.name

    try:
        args = json.loads(tool_call.function.arguments)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Kunne ikke parse tool-argumenter: {e}"})

    try:
        if function_name == "search_courses":
            return _execute_search_courses(args)
        elif function_name == "filter_courses":
            return _execute_filter_courses(args)
        elif function_name == "get_course_details":
            return _execute_get_course_details(args)
        elif function_name == "compare_courses":
            return _execute_compare_courses(args)
        elif function_name == "get_vendor_info":
            return _execute_get_vendor_info(args)
        elif function_name == "get_user_profile":
            return _execute_get_user_profile(args, username)
        elif function_name == "update_user_profile":
            return _execute_update_user_profile(args, username)
        elif function_name == "recommend_for_profile":
            return _execute_recommend_for_profile(args, username)
        elif function_name == "suggest_learning_path":
            return _execute_suggest_learning_path(args, username)
        elif function_name == "request_user_input":
            return _execute_request_user_input(args, username)
        else:
            return json.dumps({"status": "error", "message": f"Ukendt funktion: {function_name}"})
    except Exception as e:
        import traceback
        print(f"[Tool Error] {function_name}: {e}")
        print(f"[Tool Traceback] {traceback.format_exc()}")
        return json.dumps({"status": "error", "message": f"Intern fejl i {function_name}: {str(e)}"})
