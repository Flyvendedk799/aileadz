# Custom RAG and Tool schemas for app1
# Phase 1: Intent classification & query rewriting
# Phase 2: Comparison & recommendation tools
# Phase 5: Hybrid search integration
import re
import json
import datetime
import openai
from app1.rag import semantic_search_courses, semantic_search_courses_detailed, load_augmented_products, hybrid_rank_products
import catalog_service as catalog

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
        if val == int(val):
            formatted = f"{int(val):,}".replace(",", ".")
        else:
            formatted = f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"kr {formatted}"
    except (ValueError, TypeError):
        return "Pris på forespørgsel"


def _date_filter_enabled():
    """Env gate for TL-02 expired-date filtering (AI_FILTER_PAST_DATES, default on).
    Set AI_FILTER_PAST_DATES=0 to fall back to the old unfiltered behavior."""
    return _os.environ.get("AI_FILTER_PAST_DATES", "1").strip().lower() not in ("0", "false", "no")


def _hard_filter_enabled():
    """Env gate for TL-03: carry hard filters into catalog_search's RAG fallback
    + progressive filter relaxation (AI_SEARCH_HARD_FILTERS, default on).
    Set AI_SEARCH_HARD_FILTERS=0 to fall back to the old behavior."""
    return _os.environ.get("AI_SEARCH_HARD_FILTERS", "1").strip().lower() not in ("0", "false", "no")


def _filter_upcoming_date_strings(dates):
    """TL-02: Drop date strings that parse strictly before today (parse_danish_date).

    Unparseable strings are KEPT — en ukendt dato er ikke det samme som en overstået.
    Year-less Danish dates parse with the current year (existing parse_danish_date
    semantics); no year-rolling heuristics here.

    Returns (kept_dates, no_upcoming_dates) where no_upcoming_dates is True when
    the input had dates but every single one was a parseable, past date — so the
    caller can tell the model 'ingen kommende datoer — kontakt udbyderen'.
    """
    dates = [d for d in (dates or []) if d and str(d).strip()]
    if not _date_filter_enabled():
        return dates, False
    from app1 import parse_danish_date  # lazy: app1/__init__ imports at runtime only
    today = datetime.date.today()
    kept = []
    for raw in dates:
        dt = parse_danish_date(str(raw))
        if dt is not None and dt < today:
            continue  # udløbet dato — må aldrig vises som kommende
        kept.append(raw)
    return kept, bool(dates) and not kept


def _upcoming_dates(variants):
    """TL-02: Variant dates (option2) with strictly-past dates filtered out.
    Returns (dates, no_upcoming_dates) — see _filter_upcoming_date_strings."""
    return _filter_upcoming_date_strings(
        [v.get("option2") for v in (variants or []) if isinstance(v, dict) and v.get("option2")]
    )


def _date_data_stale(product, max_age_days=90):
    """TL-02 (optional): True when the product's catalog data (updated_at /
    published_at) is older than ~90 days, so the model hedges about date freshness.
    Conservative: missing/unparseable timestamp -> not stale (we don't know)."""
    raw = product.get("raw") if isinstance(product.get("raw"), dict) else product
    ts = raw.get("updated_at") or raw.get("published_at")
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
    return (now - dt).days > max_age_days


def _variant_prices(variants):
    """Parseable numeric prices across a product's variants (0.0 = free is kept).

    Unparseable variants ('Pris på forespørgsel', None, '') are skipped so an
    unknown price never spuriously gates a course in or out of a budget filter.
    """
    out = []
    for v in (variants or []):
        if not isinstance(v, dict):
            continue
        raw = v.get("price")
        if raw in (None, "", "None", "N/A"):
            continue
        try:
            out.append(float(raw))
        except (ValueError, TypeError):
            continue
    return out


def _min_variant_price(variants):
    """Cheapest bookable variant price, or None when no variant has a price.

    Replaces the long-standing `variants[0].price` budget bug: a course whose
    first listed variant is expensive but has a cheaper bookable date was
    wrongly excluded from a price ceiling. The cheapest bookable option is what
    a buyer actually pays.
    """
    prices = _variant_prices(variants)
    return min(prices) if prices else None


def _price_in_budget(variants, price_min, price_max):
    """True when the course has at least one bookable variant inside the budget
    window [price_min, price_max]. An unknown price (no parseable variant) is
    never excluded — we don't know it, so we don't hide it."""
    prices = _variant_prices(variants)
    if not prices:
        return True
    lo = float(price_min) if price_min is not None else 0.0
    hi = float(price_max) if price_max is not None else float("inf")
    return any(lo <= p <= hi for p in prices)


# Language facet: structured_metadata.language is one of dansk|engelsk|begge.
_LANGUAGE_ALIASES = {
    "danish": "dansk", "da": "dansk", "dk": "dansk", "dansk": "dansk",
    "english": "engelsk", "en": "engelsk", "eng": "engelsk", "engelsk": "engelsk",
}
# Difficulty facet: structured_metadata.difficulty is beginner|intermediate|advanced.
_DIFFICULTY_ALIASES = {
    "begynder": "beginner", "grundlæggende": "beginner", "grundlaeggende": "beginner",
    "nybegynder": "beginner", "basis": "beginner", "beginner": "beginner",
    "mellem": "intermediate", "mellemniveau": "intermediate", "intermediate": "intermediate",
    "øvet": "intermediate", "oevet": "intermediate",
    "avanceret": "advanced", "ekspert": "advanced", "advanced": "advanced", "expert": "advanced",
}


def _product_meta(product):
    raw = product.get("structured_metadata")
    return raw if isinstance(raw, dict) else {}


def _matches_language(product, language):
    """True if the product matches the requested language. Unknown (no language
    metadata) is NOT excluded. A 'begge' product matches any requested language."""
    if not language:
        return True
    want = _LANGUAGE_ALIASES.get(language.strip().lower(), language.strip().lower())
    have = str(_product_meta(product).get("language") or "").strip().lower()
    if not have:
        return True  # unknown → don't hide
    if have in ("begge", "both"):
        return True
    return want in have or have in want


def _matches_difficulty(product, difficulty):
    """True if the product matches the requested difficulty. Unknown is NOT excluded."""
    if not difficulty:
        return True
    want = _DIFFICULTY_ALIASES.get(difficulty.strip().lower(), difficulty.strip().lower())
    have = str(_product_meta(product).get("difficulty") or "").strip().lower()
    if not have:
        return True
    return want == have


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


def _truncate_summary(text, max_len=200):
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _model_tool_json(**payload):
    """JSON payload for the LLM (UI blobs resolved lazily by handle)."""
    payload.pop("raw_products", None)
    payload.pop("raw_product", None)
    return json.dumps(payload, ensure_ascii=False, default=str)


def resolve_products_for_ui(compact_results=None, handles=None, single_handle=None):
    """Load full product dicts for card rendering from catalog or RAG cache."""
    from app1.rag import load_augmented_products

    wanted = []
    if single_handle:
        wanted.append(single_handle)
    if handles:
        wanted.extend(handles)
    if compact_results:
        for row in compact_results:
            if isinstance(row, dict) and row.get("handle"):
                wanted.append(row["handle"])
    wanted = list(dict.fromkeys(h for h in wanted if h))

    rag_by_handle = {}
    try:
        for p in load_augmented_products() or []:
            h = p.get("handle")
            if h:
                rag_by_handle[h] = p
    except Exception:
        pass

    resolved = []
    for handle in wanted:
        catalog_product = catalog.get_product(handle)
        if catalog_product:
            resolved.append(_catalog_legacy_raw(catalog_product))
            continue
        rag_product = rag_by_handle.get(handle)
        if rag_product:
            resolved.append(rag_product)
    return resolved


def _extract_compact_fields(product):
    """Extract enriched compact fields from a product."""
    variants = product.get("variants", [])
    raw_price = variants[0].get("price") if variants else None

    # Apply supplier agreement discount if available (shared helper: price_view)
    vendor = product.get("vendor", "")
    pv = price_view(raw_price, vendor)
    price = pv["price"]  # Discounted price shown as main price when an agreement applies
    discount_info = pv["discount"]

    raw_locations = [v.get("option1") for v in variants if v.get("option1")]
    cities = list(dict.fromkeys(extract_city_name(loc) for loc in raw_locations if extract_city_name(loc)))[:3]

    # TL-02: never surface expired dates as bookable
    dates, no_upcoming_dates = _upcoming_dates(variants)
    dates = dates[:4]
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
        "product_url": f"/products/{product.get('handle')}" if product.get("handle") else "",
        "vendor_url": f"/vendors/{catalog.slugify(product.get('vendor'))}" if product.get("vendor") else "/vendors",
        "price": price,
        "summary": _truncate_summary(product.get("ai_summary")),
        "locations": cities,
        "dates": dates,
        "product_type": product_type,
        "tags": filtered_tags,
    }
    if discount_info:
        result["discount"] = discount_info
    if no_upcoming_dates:
        result["no_upcoming_dates"] = True

    # 4.2: Include structured metadata if available
    meta = product.get("structured_metadata", {})
    if meta:
        if meta.get("duration_days"):
            result["duration_days"] = meta["duration_days"]
        if meta.get("difficulty"):
            result["difficulty"] = meta["difficulty"]
        if meta.get("language"):
            result["language"] = meta["language"]
        if meta.get("certification"):
            result["certification"] = meta["certification"]
        if meta.get("includes"):
            result["includes"] = meta["includes"][:3]
        if meta.get("target_audience"):
            result["target_audience"] = meta["target_audience"][:2]

    return result


# Danish difficulty labels for the per-card "why" line.
_DIFFICULTY_LABELS_DA = {
    "beginner": "begynderniveau", "intermediate": "mellemniveau", "advanced": "avanceret niveau",
}
# Generic terms that make a weak, non-informative match reason — skipped.
_REASON_STOPWORDS = {
    "kursus", "kurser", "uddannelse", "course", "training", "og", "i", "at", "er",
    "en", "et", "den", "det", "de", "til", "på", "med", "for", "af", "som", "om",
    "the", "and", "to", "of", "a", "in", "noget", "med",
}


def _course_match_reason(compact, query_terms=None, profile_terms=None):
    """Build a short, VERIFIABLE Danish reason a course fits — derived only from
    real fields (matched query/profile terms + concrete attributes), never an
    LLM guess. Returns "" when there is nothing honest to say.

    query_terms / profile_terms are lowercased token iterables. The reason is
    capped to two clauses so it reads as a hint, not prose.
    """
    title = (compact.get("title") or "").lower()
    tags_l = " ".join(str(t).lower() for t in (compact.get("tags") or []))
    haystack = title + " " + tags_l
    clauses = []

    # 1) Matched the learner's own words / profile target terms (strongest signal).
    matched = []
    for term in list(profile_terms or []) + list(query_terms or []):
        t = str(term).strip().lower()
        if len(t) < 3 or t in _REASON_STOPWORDS or t in matched:
            continue
        if t in haystack:
            matched.append(t)
        if len(matched) >= 2:
            break
    if matched:
        clauses.append("matcher " + " og ".join(matched))

    # 2) A concrete, checkable attribute (level / certification / language).
    if compact.get("certification"):
        clauses.append("giver certificering")
    elif compact.get("difficulty"):
        lvl = _DIFFICULTY_LABELS_DA.get(str(compact["difficulty"]).lower())
        if lvl:
            clauses.append(lvl)
    if len(clauses) < 2 and compact.get("language") in ("dansk", "engelsk"):
        clauses.append("på " + compact["language"])

    return " · ".join(clauses[:2])


def _annotate_match_reasons(compact_results, query="", profile_boost=None):
    """Attach a verifiable `match_reason` to each compact result, in place.

    Pulls query terms from the search query and profile target terms from the
    (already-built) profile_boost so logged-in learners get profile-anchored
    reasons. No-op-safe: empty reasons are simply not set.
    """
    q_terms = _goal_keywords(query, limit=6)
    p_terms = list((profile_boost or {}).get("target_terms") or [])
    for cr in compact_results:
        reason = _course_match_reason(cr, query_terms=q_terms, profile_terms=p_terms)
        if reason:
            cr["match_reason"] = reason
    return compact_results


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
                    "language": {
                        "type": "string",
                        "description": "Undervisningssprog: 'dansk' eller 'engelsk'. Brug når brugeren beder om kurser på et bestemt sprog (fx 'på dansk')."
                    },
                    "difficulty": {
                        "type": "string",
                        "enum": ["beginner", "intermediate", "advanced"],
                        "description": "Sværhedsgrad. Brug når brugeren angiver niveau (begynder/mellem/avanceret)."
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
    },
    # 4.4: Order creation tool
    {
        "type": "function",
        "function": {
            "name": "create_course_order",
            "description": (
                "Opret en kursusbestilling for brugeren. Brug dette når brugeren eksplicit vil tilmelde sig / bestille et kursus. "
                "Du SKAL have følgende inden du kalder dette: kursets handle (fra get_course_details), "
                "brugerens fulde navn, email og telefonnummer. Spørg brugeren om de manglende oplysninger først. "
                "For logget-ind brugere kan du hente info fra get_user_profile."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_handle": {
                        "type": "string",
                        "description": "Kursets handle (f.eks. 'prince2-grundkursus'). Fås fra get_course_details."
                    },
                    "user_name": {
                        "type": "string",
                        "description": "Brugerens fulde navn."
                    },
                    "user_email": {
                        "type": "string",
                        "description": "Brugerens email-adresse."
                    },
                    "user_phone": {
                        "type": "string",
                        "description": "Brugerens telefonnummer."
                    },
                    "variant_date": {
                        "type": "string",
                        "description": "Valgt startdato (f.eks. '2026-04-15'). Valgfrit."
                    },
                    "variant_location": {
                        "type": "string",
                        "description": "Valgt lokation (f.eks. 'København'). Valgfrit."
                    }
                },
                "required": ["product_handle", "user_name", "user_email", "user_phone"]
            }
        }
    },
    # Phase 2.2: Check order approval status
    {
        "type": "function",
        "function": {
            "name": "check_order_approval_status",
            "description": (
                "Tjek godkendelsesstatus for en ordre. Brug dette når en medarbejder spørger om deres "
                "ordrebestilling er godkendt af deres leder. Kan også vise alle afventende godkendelser for brugeren."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Ordre-ID (de første 8 tegn). Valgfrit — udelad for at se alle afventende."
                    }
                },
                "required": []
            }
        }
    },
    # Phase 3.1: Skill gap analysis
    {
        "type": "function",
        "function": {
            "name": "analyze_skill_gaps",
            "description": (
                "Analyser kompetencegab for brugerens afdeling eller hele virksomheden. "
                "Sammenligner medarbejdernes nuvaerende kompetencer med virksomhedens maal. "
                "Brug dette naar brugeren spoerger om kompetencer, skills, gaps, eller hvad de skal laere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Afdelingsnavn. Valgfrit — bruger brugerens egen afdeling."
                    }
                },
                "required": []
            }
        }
    },
    # Phase 2.3: Get department budget
    {
        "type": "function",
        "function": {
            "name": "get_department_budget",
            "description": (
                "Vis afdelingens uddannelsesbudget og forbrug. Brug dette når brugeren spørger om budget, "
                "resterende midler, eller om der er råd til et kursus. Virker kun for virksomhedsbrugere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Afdelingsnavn. Valgfrit — bruger brugerens egen afdeling som standard."
                    }
                },
                "required": []
            }
        }
    },
]


OPENAI_TOOLS.extend([
    {
        "type": "function",
        "function": {
            "name": "catalog_search",
            "description": (
                "Search the Futurematch catalog and return products with internal product, category, vendor, and ask-AI URLs. "
                "Use this as the primary tool for course discovery, browsing, and product recommendations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query, topic, or user need."},
                    "category": {"type": "string", "description": "Category slug or category name filter."},
                    "vendor": {"type": "string", "description": "Vendor slug or vendor name filter."},
                    "format": {"type": "string", "description": "Course format such as E-learning, Kursus, Konference."},
                    "location": {"type": "string", "description": "City/location filter."},
                    "price_min": {"type": "number", "description": "Minimum price in DKK."},
                    "price_max": {"type": "number", "description": "Maximum price in DKK."},
                    "language": {"type": "string", "description": "Teaching language: 'dansk' or 'engelsk'. Use when the user asks for a specific language."},
                    "difficulty": {"type": "string", "enum": ["beginner", "intermediate", "advanced"], "description": "Difficulty level when the user specifies one."},
                    "limit": {"type": "integer", "description": "Max products to return. Default 4."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "catalog_get_product",
            "description": "Get canonical Futurematch catalog details and internal URL for one product by handle or title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "Product handle if known."},
                    "title": {"type": "string", "description": "Product title or partial title if handle is not known."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "catalog_get_category",
            "description": "Get a Futurematch category page, category summary, and representative products.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Category slug if known."},
                    "name": {"type": "string", "description": "Category name or partial name."},
                    "limit": {"type": "integer", "description": "Representative product limit. Default 4."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "catalog_get_vendor",
            "description": "Get a Futurematch vendor page, vendor profile, and representative products.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Vendor slug if known."},
                    "name": {"type": "string", "description": "Vendor name or partial name."},
                    "topic": {"type": "string", "description": "Optional topic to evaluate vendor fit."},
                    "limit": {"type": "integer", "description": "Representative product limit. Default 4."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "catalog_compare_products",
            "description": "Compare 2-4 Futurematch catalog products and return internal links and raw products for cards.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 product handles.",
                        "minItems": 2,
                        "maxItems": 4
                    }
                },
                "required": ["handles"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_learning_context",
            "description": (
                "Get the user's Futurematch learning context in one call: profile, shown products, company budget, "
                "supplier preferences/agreements, open orders, and completed courses."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_course_readiness",
            "description": (
                "Check whether a user is ready to request/enroll in a course: product exists, supplier is active, "
                "contact fields, variant, budget, and approval status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_handle": {"type": "string", "description": "Course/product handle."},
                    "variant_date": {"type": "string", "description": "Selected date if known."},
                    "variant_location": {"type": "string", "description": "Selected location if known."}
                },
                "required": ["product_handle"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_course_order",
            "description": (
                "Prepare an order request and return a confirmation summary. This does NOT create an order. "
                "Use before create_course_order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_handle": {"type": "string", "description": "Course/product handle."},
                    "user_name": {"type": "string", "description": "User full name if known."},
                    "user_email": {"type": "string", "description": "User email if known."},
                    "user_phone": {"type": "string", "description": "User phone if known."},
                    "variant_date": {"type": "string", "description": "Selected date if known."},
                    "variant_location": {"type": "string", "description": "Selected location if known."}
                },
                "required": ["product_handle"]
            }
        }
    },
])


# ── Course-journey tools (scoped to the logged-in user) ──
OPENAI_TOOLS.extend([
    {
        "type": "function",
        "function": {
            "name": "get_my_course_status",
            "description": (
                "Vis status på den indloggede brugers egne kurser: status, gennemførelse, startdato, "
                "frist (completion_deadline), pris og titel - med dage tilbage og et 'forsinket'-flag. "
                "Brug når brugeren spørger 'hvor er mit kursus', 'er jeg forsinket', 'hvad mangler jeg', "
                "'hvornår starter mit kursus', eller om deadlines/frister. Kun brugerens egne ordrer."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_negotiated_discount",
            "description": (
                "Slå brugerens VIRKSOMHEDS aktive aftalepris/firma-rabat op på et kursus hos en udbyder "
                "(company_supplier_agreements). Returnerer original pris, rabat, slutpris og besparelse. "
                "Brug ved 'hvad koster det med rabat', 'aftalepris', 'firma-rabat'. Scoped til brugerens virksomhed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_handle": {"type": "string", "description": "Kursets handle (for at finde pris + udbyder)."},
                    "vendor": {"type": "string", "description": "Udbyderens navn, hvis handle ikke kendes."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_course_prerequisites",
            "description": (
                "Hent et kursus' forudsætninger, sværhedsgrad, varighed (dage), certificering og målgruppe "
                "fra katalogets strukturerede metadata. Brug ved 'forudsætninger', 'krav', 'hvad kræver dette', "
                "'sværhedsgrad', 'er jeg klar til'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "Kursets eksakte handle."},
                    "title": {"type": "string", "description": "Kursets titel, hvis handle ikke kendes."},
                },
                "required": ["handle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_course_sequel",
            "description": (
                "Foreslå naturlige NÆSTE kurser efter et givet kursus, baseret på emne/certificering og næste "
                "sværhedsgrad fra strukturerede metadata. Brug ved 'næste kursus', 'hvad nu', 'bygge videre', "
                "'hvad efter dette'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "Det gennemførte/aktuelle kursus' handle."},
                    "title": {"type": "string", "description": "Kursets titel, hvis handle ikke kendes."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_certification_path",
            "description": (
                "Find katalogkurser hvis certificering eller tags matcher en ønsket certificering (fx PMP, ITIL, "
                "PRINCE2), ordnet efter sværhedsgrad, så de danner en vej mod certificeringen. Brug ved "
                "'blive certificeret', 'certificering', 'cert', 'vej til PMP/ITIL/PRINCE2'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "certification": {"type": "string", "description": "Mål-certificering, fx 'PMP', 'ITIL', 'PRINCE2'."},
                },
                "required": ["certification"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "track_goal_progress",
            "description": (
                "Vis den indloggede brugers fremgang mod sine udviklingsmål (user_learning_goals) holdt op mod "
                "gennemførte kurser - hvad er matchet og hvad mangler. Brug ved 'hvor langt er jeg', 'mit mål', "
                "'mål-progress', 'mangler jeg'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_calendar",
            "description": (
                "Lav en kalenderaftale (.ics) for et booket kursus, så brugeren kan tilføje det til Outlook/kalender. "
                "Brug ved 'tilføj til kalender', 'kalender', '.ics', 'outlook'. Returnerer en .ics-fil eller "
                "begivenhedsdetaljer som tekst."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Brugerens ordre-id for kurset, hvis kendt."},
                    "handle": {"type": "string", "description": "Kursets handle (alternativ til order_id)."},
                    "date": {"type": "string", "description": "Kursusdato (dansk eller ISO), hvis ikke fra ordren."},
                    "location": {"type": "string", "description": "Lokation/by for kurset."},
                    "title": {"type": "string", "description": "Kursustitel, hvis hverken order_id eller handle gives."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_course_complete",
            "description": (
                "MUTATION: Markér et af brugerens kurser som gennemført. Kun når brugeren bekræfter tydeligt med 'ja'. "
                "Opdaterer ordren, tilføjer kurset til brugerens gennemførte kurser og foreslår et næste kursus. "
                "Brug ved 'jeg har gennemført', 'marker som færdig', 'fuldført kurset'. Kun brugerens egne kurser."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "Kursets handle."},
                    "order_id": {"type": "string", "description": "Ordre-id for kurset (alternativ til handle)."},
                    "confirm": {
                        "type": "string",
                        "description": "Brugerens eksplicitte danske bekræftelse, fx 'ja' eller 'bekræft'. Kræves for at gennemføre.",
                    },
                },
                "required": ["confirm"],
            },
        },
    },
])


# ── AI Tooler 2 (Phase 7): employee-facing action tools ───────────────────────
OPENAI_TOOLS.extend([
    {
        "type": "function",
        "function": {
            "name": "save_course_for_later",
            "description": (
                "Gem et kursus på brugerens ønskeliste til senere (gemmes med det samme, lav risiko, "
                "kan slettes igen). Brug ved 'gem det her til senere', 'tilføj til min ønskeliste', "
                "'husk dette kursus'. Kun for indloggede brugere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_title": {"type": "string", "description": "Kursets titel."},
                    "product_handle": {"type": "string", "description": "Valgfrit kursus-handle."},
                },
                "required": ["product_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_course_reminder",
            "description": (
                "Sæt en personlig påmindelse om et kursus (gemmes med det samme, kan slettes igen). "
                "Brug ved 'mind mig om det her', 'sæt en påmindelse til på mandag', 'påmind mig om "
                "tilmelding'. Kun for indloggede brugere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_title": {"type": "string", "description": "Kursets titel."},
                    "remind_on": {"type": "string", "description": "Hvornår der skal mindes (fx 'på mandag', '2026-09-01')."},
                    "product_handle": {"type": "string", "description": "Valgfrit kursus-handle."},
                },
                "required": ["product_title", "remind_on"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_my_order",
            "description": (
                "MUTATION: Annullér brugerens EGEN bestilling. Kræver confirm=true; uden returneres kun "
                "en forhåndsvisning. Kun brugerens egne ordrer (fremmede ordrer afvises). Brug ved "
                "'annullér min bestilling', 'jeg fortryder kurset', 'slet min ordre'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Ordre-id på brugerens egen bestilling."},
                    "action": {"type": "string", "enum": ["cancel"], "description": "Handlingen. Lige nu kun 'cancel'."},
                    "confirm": {"type": "boolean", "description": "Skal være true for at annullere. Uden dette returneres kun en forhåndsvisning."},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_manager_approval",
            "description": (
                "MUTATION: Bed brugerens leder om at godkende en af brugerens afventende bestillinger "
                "(sender en påmindelse på mail til lederne). Kræver confirm=true. Kun brugerens egen ordre. "
                "Brug ved 'bed min leder om at godkende', 'kan du rykke for godkendelse', 'send det til godkendelse'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Ordre-id på brugerens egen afventende bestilling."},
                    "confirm": {"type": "boolean", "description": "Skal være true for at sende. Uden dette returneres kun en forhåndsvisning."},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_cv_summary",
            "description": (
                "Vis et øjebliksbillede af brugerens CV-data direkte i chatten: kompetencer, erfaring, "
                "uddannelse, certificeringer og sprog — med et 'Opdater CV'-link til den 3D-interaktive "
                "CV-portal. Brug dette når brugeren spørger om sin profil/CV, vil uploade nyt CV, eller "
                "du vil opsummere hvad systemet ved om dem fagligt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "enum": ["overview", "skills", "experience", "education", "certifications", "languages"],
                        "description": "Hvilken sektion der skal fremhæves. Standard: overview.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_mindmap_preview",
            "description": (
                "Vis et kompakt preview af brugerens 3D mind-map i chatten: profilkomplethed i procent, "
                "antal noder pr. kategori og de seneste AI-hukommelser. Inkluderer et link til den "
                "interaktive 3D-kuglevisning. Brug dette når brugeren spørger hvad AI'en husker om dem, "
                "vil se mind-mappet, eller du vil vise profilstatus."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_skill_gaps",
            "description": (
                "Vis brugerens KOMPETENCEGAB i chatten: forskellen mellem deres nuværende niveau og "
                "målniveauet — udledt af virksomhedens kompetencemål, deres ønskede rolle (target_role) "
                "og læringsmål — på en kanonisk 1-5 skala, sorteret efter vigtighed. Returnerer også "
                "selve gap-dataen, så du KAN OG BØR følge op med en kursusanbefaling (recommend_for_profile) "
                "der lukker de største gap. Brug dette når brugeren spørger 'hvad mangler jeg', 'hvilke "
                "kompetencer skal jeg udvikle', vil have en udviklingsplan, eller når du vil begrunde en "
                "anbefaling i et konkret, verificerbart gap frem for et gæt."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
])


# Cross-surface action tool: lets the assistant MOVE the user through the SPA
# (open a product, the compare view, a profile section, the catalog, the
# mind-map, or start an enrolment) instead of only describing links. It returns
# a structured directive the frontend acts on via a `ui_action` SSE event — it
# performs NO mutation itself (start_order only opens the enrolment flow).
OPENAI_TOOLS.append({
    "type": "function",
    "function": {
        "name": "open_in_app",
        "description": (
            "Åbn noget i appen for brugeren (navigation/handling i UI'et). Brug dette når du vil "
            "FØRE brugeren et sted hen i stedet for kun at beskrive et link: vis et konkret kursus, "
            "åbn sammenligningsvisning, åbn profil/CV, åbn kataloget (evt. filtreret), åbn mind-map, "
            "skift til AI Profiler, eller start en tilmelding. Det opretter IKKE en ordre — start_order "
            "åbner kun tilmeldingsflowet. Foretræk dette frem for at skrive rå URL'er i teksten."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["view_product", "open_compare", "open_profile", "open_mind_map",
                             "open_cv_upload", "open_learning_path", "open_catalog", "start_order", "open_profiler"],
                    "description": "Hvilken handling/navigation der skal udføres i UI'et."
                },
                "handle": {"type": "string", "description": "Kursets handle (view_product / start_order)."},
                "handles": {"type": "array", "items": {"type": "string"},
                            "description": "2-4 handles til open_compare."},
                "section": {"type": "string",
                            "description": "Profilsektion til open_profile (fx 'skills','experience','goals','certifications','languages')."},
                "query": {"type": "string", "description": "Søge-/kategoritekst til open_catalog."},
                "label": {"type": "string", "description": "Kort dansk knaptekst, fx 'Åbn kurset' eller 'Sammenlign'."}
            },
            "required": ["action"]
        }
    }
})


# ── Tool Execution ──

# Module-level state for contextual search (set by agent before tool execution)
_current_shown_handles = set()
_current_user_prefs = {}
_current_blocked_vendors = set()  # Vendors deactivated by the company
_current_supplier_agreements = {}  # {vendor_name: {discount_type, discount_value, agreement_name}}


def set_search_context(shown_handles=None, user_prefs=None, blocked_vendors=None, supplier_agreements=None):
    """Set contextual search state for the current request.
    Called by agent.py before tool execution to pass session context."""
    global _current_shown_handles, _current_user_prefs, _current_blocked_vendors, _current_supplier_agreements
    _current_shown_handles = shown_handles or set()
    _current_user_prefs = user_prefs or {}
    _current_blocked_vendors = blocked_vendors or set()
    _current_supplier_agreements = supplier_agreements or {}


def apply_discount(price_raw, vendor_name):
    """Apply company supplier agreement discount to a price. Returns (discounted_price, original_price, agreement_name) or (None, None, None)."""
    if not _current_supplier_agreements or not vendor_name:
        return None, None, None
    agreement = _current_supplier_agreements.get(vendor_name)
    if not agreement:
        return None, None, None
    try:
        original = float(price_raw) if price_raw else 0
        if original <= 0:
            return None, None, None
        dtype = agreement.get('discount_type', 'percentage')
        dvalue = float(agreement.get('discount_value', 0))
        if dvalue <= 0:
            return None, None, None
        if dtype == 'percentage':
            discounted = original * (1 - dvalue / 100)
        elif dtype == 'fixed_amount':
            discounted = max(0, original - dvalue)
        elif dtype == 'fixed_price':
            discounted = dvalue
        else:
            return None, None, None
        return round(discounted, 2), original, agreement.get('agreement_name', '')
    except (ValueError, TypeError):
        return None, None, None


def price_view(raw_price, vendor_name):
    """Single price-resolution helper so search, details and comparison all quote
    the same negotiated price. Applies the per-turn supplier agreement (set via
    set_search_context) when one exists for the vendor.

    Returns a dict:
      price    -> display string (discounted when an agreement applies)
      amount   -> numeric price actually quoted (None when unparseable)
      discount -> None, or {original_price, discounted_price, savings, agreement_name}
    """
    discounted, original, agreement_name = apply_discount(raw_price, vendor_name)
    if discounted is None or original is None:
        try:
            amount = float(raw_price) if raw_price not in (None, "") else None
        except (ValueError, TypeError):
            amount = None
        return {"price": _normalize_price(raw_price), "amount": amount, "discount": None}
    return {
        "price": _normalize_price(discounted),
        "amount": discounted,
        "discount": {
            "original_price": _normalize_price(original),
            "discounted_price": _normalize_price(discounted),
            "agreement_name": agreement_name,
            "savings": _normalize_price(original - discounted),
        },
    }


def _filter_blocked_vendors(products):
    """Remove products from deactivated vendors."""
    if not _current_blocked_vendors:
        return products
    return [p for p in products if p.get('vendor', '') not in _current_blocked_vendors]


def _catalog_product_url(product):
    return catalog.build_product_url(product["handle"])


def _catalog_vendor_url(product_or_vendor):
    slug = product_or_vendor.get("vendor_slug") or product_or_vendor.get("slug")
    return f"/vendors/{slug}" if slug else "/vendors"


def _catalog_category_urls(product):
    return [
        {"name": name, "slug": slug, "url": f"/categories/{slug}"}
        for name, slug in zip(product.get("categories") or [], product.get("category_slugs") or [])
    ]


def _catalog_legacy_raw(product):
    """Convert a normalized catalog product to the legacy raw shape used by chat cards."""
    raw = dict(product.get("raw") or {})
    raw.setdefault("handle", product.get("handle"))
    raw.setdefault("title", product.get("title"))
    raw.setdefault("vendor", product.get("vendor"))
    raw.setdefault("product_type", product.get("product_type") or product.get("format"))
    raw.setdefault("ai_summary", product.get("summary") or product.get("description_excerpt"))
    raw.setdefault("body_html", product.get("description_text") or product.get("summary") or "")
    if product.get("image_url") and not raw.get("image"):
        raw["image"] = {"src": product["image_url"]}
    variants = raw.get("variants") or []
    if not variants:
        variants = [
            {
                "id": v.get("id"),
                "title": v.get("title") or "",
                "price": "" if v.get("price") is None else str(v.get("price")),
                "option1": v.get("location") or v.get("city") or "",
                "option2": v.get("date") or "",
            }
            for v in product.get("variants", [])
        ]
    else:
        normalized = []
        for v in variants:
            if not isinstance(v, dict):
                continue
            vv = dict(v)
            if "option1" not in vv:
                vv["option1"] = vv.get("location") or vv.get("city") or ""
            if "option2" not in vv:
                vv["option2"] = vv.get("date") or ""
            normalized.append(vv)
        variants = normalized
    raw["variants"] = variants
    return raw


def _catalog_compact_fields(product):
    # TL-02: never surface expired dates as bookable
    dates, no_upcoming_dates = _filter_upcoming_date_strings(product.get("dates", []))
    fields = {
        "title": product.get("title"),
        "handle": product.get("handle"),
        "vendor": product.get("vendor"),
        "vendor_slug": product.get("vendor_slug"),
        "vendor_url": _catalog_vendor_url(product),
        "product_url": _catalog_product_url(product),
        "ask_ai_url": catalog.build_ask_ai_url(product),
        "price": product.get("price_label"),
        "price_min": product.get("price_min"),
        "format": product.get("format"),
        "summary": _truncate_summary(product.get("summary") or product.get("description_excerpt")),
        "locations": product.get("locations", [])[:5],
        "dates": dates[:5],
        "categories": _catalog_category_urls(product),
        "image_url": product.get("image_url"),
        "source": product.get("source"),
    }
    # Apply supplier agreement discount so catalog results quote the same
    # negotiated price as the legacy search path (shared helper: price_view).
    pv = price_view(product.get("price_min"), product.get("vendor"))
    if pv["discount"]:
        fields["price"] = pv["price"]
        fields["price_min"] = pv["amount"]
        fields["discount"] = pv["discount"]
    if no_upcoming_dates:
        fields["no_upcoming_dates"] = True
    return fields


def _resolve_category_slug(value):
    value = (value or "").strip()
    if not value:
        return ""
    value_slug = catalog.slugify(value)
    for category in catalog.get_categories():
        if category["slug"] == value or category["slug"] == value_slug or value.lower() in category["name"].lower():
            return category["slug"]
    return value_slug


def _resolve_vendor_slug(value):
    value = (value or "").strip()
    if not value:
        return ""
    value_slug = catalog.slugify(value)
    for vendor in catalog.get_vendors():
        if vendor["slug"] == value or vendor["slug"] == value_slug or value.lower() in vendor["name"].lower():
            return vendor["slug"]
    return value_slug


def _find_catalog_product(handle="", title=""):
    handle = (handle or "").strip()
    title = (title or "").strip()
    if handle:
        product = catalog.get_product(handle)
        if product:
            return product
    if title:
        title_lower = title.lower()
        products = catalog.get_products()
        exact = next((p for p in products if p["title"].lower() == title_lower), None)
        if exact:
            return exact
        partial = next((p for p in products if title_lower in p["title"].lower()), None)
        if partial:
            return partial
        search = catalog.search_products({"q": title}, page=1, per_page=1)
        if search["products"]:
            return search["products"][0]
    return None


def _completed_course_keys(username):
    """Return (titles, handles) sets of courses the user has already completed.

    Mirrors the completed-course extraction in _execute_recommend_for_profile.
    Fully guarded: returns empty sets for anonymous users or on any error, so
    search behaviour is unchanged when profile data is unavailable.
    """
    if not username:
        return set(), set()
    try:
        from app1.user_profile_db import get_full_profile, ensure_tables
        ensure_tables()
        profile = get_full_profile(username) or {}
        completed = profile.get("completed_courses", []) or []
        titles = {c["title"].lower() for c in completed if c.get("title")}
        handles = {c.get("handle", "").lower() for c in completed if c.get("handle")}
        return titles, handles
    except Exception:
        return set(), set()


def _demote_completed_results(compact, completed_titles, completed_handles):
    """Move already-completed courses to the end of the result list and mark
    them with already_completed=True. Preserves relative order within each
    group. No-op when there are no completed courses or no results."""
    if not compact or (not completed_titles and not completed_handles):
        return compact
    fresh = []
    done = []
    for r in compact:
        title_lower = (r.get("title") or "").lower()
        handle_lower = (r.get("handle") or "").lower()
        if title_lower in completed_titles or handle_lower in completed_handles:
            r["already_completed"] = True
            done.append(r)
        else:
            fresh.append(r)
    return fresh + done


def _mark_previously_shown(compact):
    """Mark re-included/re-shown results so the model can address it (matches
    the legacy _execute_search_courses behaviour). Mutates in place, returns
    the list for chaining."""
    for cr in compact:
        if cr.get("handle") in _current_shown_handles:
            cr["previously_shown"] = True
    return compact


def _product_passes_hard_filters(product, *, price_min=None, price_max=None, location="", fmt="",
                                 language="", difficulty=""):
    """TL-03: the same hard-filter predicates _execute_filter_courses uses
    (bookable-variant price, _location_matches, format substring, language /
    difficulty metadata), applied to a single raw/augmented product. Used to
    carry catalog_search's hard constraints into the RAG fallback, som ellers
    ignorerer dem."""
    variants = product.get("variants", []) or []

    if location:
        variant_locs = [v.get("option1", "") for v in variants if isinstance(v, dict) and v.get("option1")]
        if not any(_location_matches(location, vloc) for vloc in variant_locs):
            return False

    if (price_min is not None or price_max is not None) and not _price_in_budget(variants, price_min, price_max):
        return False

    if fmt:
        pt = (product.get("product_type") or product.get("format") or "").lower()
        if fmt.lower().strip() not in pt:
            return False

    if language and not _matches_language(product, language):
        return False

    if difficulty and not _matches_difficulty(product, difficulty):
        return False

    return True


def _apply_hard_filters(products, *, price_min=None, price_max=None, location="", fmt="",
                        language="", difficulty=""):
    """TL-03: post-filter a RAG result set with catalog_search's hard constraints.

    Returns (filtered_products, active_filters) where active_filters names the
    constraints that were in play — used to populate relaxed_filters when the
    constrained set is empty and the caller honestly falls back to the
    unfiltered results (filters_relaxed: true)."""
    active = []
    if price_min is not None:
        active.append("price_min")
    if price_max is not None:
        active.append("price_max")
    if location:
        active.append("location")
    if fmt:
        active.append("format")
    if language:
        active.append("language")
    if difficulty:
        active.append("difficulty")
    if not active:
        return products, []
    filtered = [
        p for p in products
        if _product_passes_hard_filters(p, price_min=price_min, price_max=price_max,
                                        location=location, fmt=fmt,
                                        language=language, difficulty=difficulty)
    ]
    return filtered, active


_PROFILE_GOAL_STOPWORDS = {
    "og", "i", "at", "er", "en", "et", "den", "det", "de", "til", "på", "med",
    "for", "af", "fra", "som", "om", "kan", "har", "vil", "der", "ikke", "sig",
    "var", "ved", "så", "også", "efter", "eller", "blive", "bedre", "lære",
    "lære", "mere", "min", "mine", "mit", "jeg", "vi", "du", "the", "and", "to",
    "of", "a", "in", "inden", "indenfor", "vil", "gerne", "skal", "være",
}


def _goal_keywords(text, limit=8):
    """Extract topical keywords from a free-text goal/learning-goal string.

    Lowercases, splits on non-alphanumeric, drops stop words and very short
    tokens. Mirrors how analyze_skill_gaps / the RAG tokenizer think about
    topical terms. Returns a list (order preserved, deduped)."""
    if not text:
        return []
    toks = re.findall(r"[a-zæøå0-9]+", str(text).lower())
    out = []
    seen = set()
    for t in toks:
        if len(t) <= 2 or t in _PROFILE_GOAL_STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _profile_boost_from_profile(profile):
    """Build a profile_boost dict from an already-loaded profile dict.

    Returns None when there is nothing to boost/demote, so callers can pass the
    result straight to hybrid_rank_products (None → legacy behaviour).

    target_terms = desired/target skills + learning-goal keywords + skill-gap
    terms (skills the learner has at a low level and therefore wants to grow,
    mirroring how analyze_skill_gaps reasons about gaps).
    """
    if not profile:
        return None

    target_terms = set()

    # 1) Skill-gap terms: skills held at begynder/mellem level are growth areas
    #    (target above current) — exactly the skills the learner wants to lift.
    for s in profile.get("skills", []) or []:
        name = (s.get("name") or "").strip().lower()
        if not name:
            continue
        if s.get("level") in ("begynder", "mellem"):
            target_terms.add(name)
            # also add individual keywords from multi-word skill names
            for kw in _goal_keywords(name, limit=4):
                target_terms.add(kw)

    # 2) Free-text learning goals (profile summary "goals")
    for kw in _goal_keywords(profile.get("goals", ""), limit=8):
        target_terms.add(kw)

    # 3) Active development goals (learning_goals with status "aktiv")
    for g in profile.get("learning_goals", []) or []:
        if g.get("status") != "aktiv":
            continue
        for kw in _goal_keywords(g.get("title", ""), limit=6):
            target_terms.add(kw)

    # Completed courses → demotion keys
    completed = profile.get("completed_courses", []) or []
    completed_titles = {c["title"].lower() for c in completed if c.get("title")}
    completed_handles = {
        c.get("handle", "").lower() for c in completed if c.get("handle")
    }

    if not target_terms and not completed_titles and not completed_handles:
        return None

    return {
        "target_terms": target_terms,
        "completed_handles": completed_handles,
        "completed_titles": completed_titles,
    }


def _build_profile_boost(username):
    """Fetch the logged-in user's profile and build a profile_boost dict for
    app1.rag.hybrid_rank_products.

    Returns None for anonymous users or on any error/empty profile, so the
    ranking falls back to the exact legacy (profile-agnostic) behaviour and
    anonymous users are completely unaffected.
    """
    if not username:
        return None
    try:
        from app1.user_profile_db import get_full_profile, ensure_tables
        ensure_tables()
        profile = get_full_profile(username) or {}
    except Exception:
        return None
    return _profile_boost_from_profile(profile)


def _execute_catalog_search(args, username=None):
    filters = {
        "q": args.get("query") or "",
        "category": _resolve_category_slug(args.get("category") or ""),
        "vendor": _resolve_vendor_slug(args.get("vendor") or ""),
        "format": args.get("format") or "",
        "location": args.get("location") or "",
        "price_min": args.get("price_min"),
        "price_max": args.get("price_max"),
        "sort": "relevance",
    }
    limit = int(args.get("limit") or 4)
    query = (args.get("query") or "").strip()
    # Metadata-backed facets (honoured on the augmented/RAG products that carry
    # structured_metadata; a no-op for catalog rows that lack it, so they are
    # never wrongly excluded).
    language = (args.get("language") or "").strip()
    difficulty = (args.get("difficulty") or args.get("level") or "").strip()
    products = []
    confidence = "medium"
    completed_titles, completed_handles = _completed_course_keys(username)
    profile_boost = _build_profile_boost(username)

    result = catalog.search_products(filters=filters, page=1, per_page=max(1, min(limit, 8)))
    products = [
        p for p in result["products"]
        if p.get("handle") not in _current_shown_handles and p.get("vendor") not in _current_blocked_vendors
    ]
    if not products and result["products"]:
        products = [p for p in result["products"] if p.get("vendor") not in _current_blocked_vendors]

    use_rag = query and (len(products) < max(1, limit // 2) or (result.get("total", 0) or 0) <= 1)
    if use_rag:
        # value-5: scale the RETRIEVAL candidate pool with profile richness.
        # A logged-in user with many target skills/goals pulls a wider (still
        # in-memory, no extra API cost) candidate set so the profile re-rank
        # below has real material to reorder. Anonymous / thin-profile users
        # have profile_boost=None → candidate_limit stays = limit (no widening,
        # no cost regression). The SHOWN count is always sliced back to `limit`.
        from app1.rag import profile_candidate_limit
        candidate_limit = profile_candidate_limit(profile_boost, limit)
        detailed = semantic_search_courses_detailed(
            query,
            limit=limit,
            shown_handles=_current_shown_handles,
            user_prefs=_current_user_prefs,
            candidate_limit=candidate_limit,
        )
        if isinstance(detailed, dict) and "error" not in detailed:
            confidence = detailed.get("confidence", confidence)
            rag_products = _filter_blocked_vendors(detailed.get("products", []))
            # TL-03: carry the hard constraints (pris/lokation/format) into the
            # RAG fallback — semantisk søgning kender ikke filtrene, så uden
            # dette kan "under 5.000 kr i Aarhus" returnere kurser over budget
            # i den forkerte by. Tømmer filtrene resultatsættet, falder vi
            # ærligt tilbage til de ufiltrerede resultater med
            # filters_relaxed: true + relaxed_filters, så modellen kan
            # fortælle præcist hvad der blev løsnet (samme flag-familie som
            # retrieval-lagets below_threshold).
            filters_relaxed = False
            relaxed_filters = []
            if rag_products and _hard_filter_enabled():
                constrained, active_filters = _apply_hard_filters(
                    rag_products,
                    price_min=filters["price_min"],
                    price_max=filters["price_max"],
                    location=filters["location"],
                    fmt=filters["format"],
                    language=language,
                    difficulty=difficulty,
                )
                if constrained:
                    rag_products = constrained
                elif active_filters:
                    filters_relaxed = True
                    relaxed_filters = [f"{name}_dropped" for name in active_filters]
            # Profile-conditioned re-rank for logged-in users: boost products
            # matching the learner's target skills/goals and demote completed
            # courses. profile_boost is None for anonymous users → no-op.
            if profile_boost and rag_products:
                try:
                    rag_products = hybrid_rank_products(
                        rag_products, query, load_augmented_products(),
                        limit=len(rag_products), profile_boost=profile_boost,
                    )
                except Exception:
                    pass  # fall back to the semantic order on any failure
            if rag_products:
                compact = [_extract_compact_fields(p) for p in rag_products[:limit]]
                compact = _demote_completed_results(compact, completed_titles, completed_handles)
                compact = _mark_previously_shown(compact)
                _annotate_match_reasons(compact, query, profile_boost)
                debug = detailed.get("debug") or {}
                extra = {}
                if filters_relaxed:
                    extra["filters_relaxed"] = True
                    extra["relaxed_filters"] = relaxed_filters
                return _model_tool_json(
                    status="success",
                    count=len(compact),
                    confidence=confidence,
                    search_mode="rag",
                    results=compact,
                    search_debug={
                        "embedding_skipped": debug.get("embedding_skipped"),
                        "cross_encoder_applied": debug.get("cross_encoder_applied"),
                    } if debug else None,
                    **extra,
                )

    compact = [_catalog_compact_fields(p) for p in products[:limit]]
    compact = _demote_completed_results(compact, completed_titles, completed_handles)
    # TL-03: re-included fallback results (shown-handle re-include above) are
    # marked previously_shown, matching the legacy search path.
    compact = _mark_previously_shown(compact)
    _annotate_match_reasons(compact, query, profile_boost)
    return _model_tool_json(
        status="success" if compact else "no_results",
        count=len(compact),
        total=result.get("total", len(compact)),
        confidence=confidence,
        search_mode="catalog",
        filters={k: v for k, v in filters.items() if v not in ("", None)},
        results=compact,
        catalog_url="/catalog",
        search_debug={"embedding_skipped": True},
    )


def _execute_catalog_get_product(args):
    product = _find_catalog_product(args.get("handle", ""), args.get("title", ""))
    if not product:
        return json.dumps({"status": "not_found", "message": "Produktet blev ikke fundet i Futurematch kataloget."}, ensure_ascii=False)
    vendor_state = _supplier_state_for_vendor(product.get("vendor"))
    return _model_tool_json(
        status="success",
        product=_catalog_compact_fields(product),
        supplier_state=vendor_state,
        variants=product.get("variants", [])[:4],
    )


def _execute_catalog_get_category(args):
    slug = _resolve_category_slug(args.get("slug") or args.get("name") or "")
    category = catalog.get_category(slug)
    if not category:
        return json.dumps({"status": "not_found", "message": "Kategorien blev ikke fundet.", "category_url": "/categories"}, ensure_ascii=False)
    limit = int(args.get("limit") or 4)
    result = catalog.search_products({"category": category["slug"], "sort": "relevance"}, page=1, per_page=max(1, min(limit, 8)))
    products = [p for p in result["products"] if p.get("vendor") not in _current_blocked_vendors]
    return _model_tool_json(
        status="success",
        category={
            "name": category["name"],
            "slug": category["slug"],
            "count": category["count"],
            "url": f"/categories/{category['slug']}",
        },
        count=len(products[:limit]),
        results=[_catalog_compact_fields(p) for p in products[:limit]],
    )


def _execute_catalog_get_vendor(args):
    slug = _resolve_vendor_slug(args.get("slug") or args.get("name") or "")
    vendor = catalog.get_vendor(slug)
    if not vendor:
        return json.dumps({"status": "not_found", "message": "Leverandøren blev ikke fundet.", "vendor_url": "/vendors"}, ensure_ascii=False)
    limit = int(args.get("limit") or 4)
    result = catalog.search_products({"vendor": vendor["slug"], "q": args.get("topic") or "", "sort": "relevance"}, page=1, per_page=max(1, min(limit, 8)))
    products = result["products"][:limit]
    return _model_tool_json(
        status="success",
        vendor={
            "name": vendor["name"],
            "slug": vendor["slug"],
            "url": f"/vendors/{vendor['slug']}",
            "course_count": vendor.get("course_count", 0),
            "categories": vendor.get("categories", [])[:5],
            "price_label": vendor.get("price_label", ""),
        },
        count=len(products),
        results=[_catalog_compact_fields(p) for p in products],
    )


# Hard cap on how many courses a single comparison may contain. More than a
# handful side-by-side is unreadable and overflows the cards, so extras are
# truncated and the model is told (in Danish) so it can mention it to the user.
COMPARE_MAX_COURSES = 4


def _comparison_guardrails(requested_count, vendors):
    """Pure helper (no I/O) that derives the comparison guardrail fields.

    Given how many handles the model asked to compare and the list of vendor
    names that ended up IN the comparison, returns a dict with:
      - "_compare_cap": the hard cap actually applied (COMPARE_MAX_COURSES)
      - "_truncation_note": Danish note (or None) when extras were dropped
      - "single_vendor_notice": Danish note (or None) when every compared course
        comes from one and the same supplier — a nudge for the model to flag the
        missing vendor diversity and offer an alternative from another supplier.

    Kept separate from the executors so it can be unit-tested with no DB/API.
    """
    truncation_note = None
    if requested_count and requested_count > COMPARE_MAX_COURSES:
        dropped = requested_count - COMPARE_MAX_COURSES
        truncation_note = (
            f"Du bad om at sammenligne {requested_count} kurser, men der kan "
            f"højst sammenlignes {COMPARE_MAX_COURSES} ad gangen. "
            f"De {dropped} sidste blev udeladt — fortæl brugeren det og tilbyd "
            f"at sammenligne resten i en ny omgang."
        )

    single_vendor_notice = None
    distinct_vendors = {(v or "").strip() for v in vendors if (v or "").strip()}
    if len(distinct_vendors) == 1 and len([v for v in vendors if (v or "").strip()]) >= 2:
        only_vendor = next(iter(distinct_vendors))
        single_vendor_notice = (
            f"Alle de sammenlignede kurser er fra samme udbyder ({only_vendor}). "
            f"Gør brugeren opmærksom på den manglende leverandørspredning og "
            f"tilbyd et tilsvarende alternativ fra en anden udbyder."
        )

    return {
        "_compare_cap": COMPARE_MAX_COURSES,
        "_truncation_note": truncation_note,
        "single_vendor_notice": single_vendor_notice,
    }


def _execute_catalog_compare_products(args):
    handles = args.get("handles") or []
    requested_count = len(handles)
    # Hard-cap to COMPARE_MAX_COURSES — extras are truncated (and noted below).
    products = [catalog.get_product(handle) for handle in handles[:COMPARE_MAX_COURSES]]
    products = [p for p in products if p]
    if len(products) < 2:
        return json.dumps({"status": "error", "message": "Mindst to gyldige produkter kræves for sammenligning."}, ensure_ascii=False)
    comparison = []
    for product in products:
        # TL-02: never surface expired dates as bookable
        dates, no_upcoming_dates = _filter_upcoming_date_strings(product.get("dates", []))
        entry = {
            "title": product["title"],
            "handle": product["handle"],
            "url": _catalog_product_url(product),
            "vendor": product["vendor"],
            "vendor_url": _catalog_vendor_url(product),
            "price": product["price_label"],
            "format": product["format"],
            "locations": product["locations"][:4],
            "dates": dates[:4],
            "categories": [c["name"] for c in _catalog_category_urls(product)[:4]],
            "summary": _truncate_summary(product.get("summary") or product.get("description_excerpt")),
        }
        # Same negotiated price here as in search/details (shared helper: price_view).
        pv = price_view(product.get("price_min"), product.get("vendor"))
        if pv["discount"]:
            entry["price"] = pv["price"]
            entry["discount"] = pv["discount"]
        if no_upcoming_dates:
            entry["no_upcoming_dates"] = True
        comparison.append(entry)

    # Guardrails: cap-truncation note + single-vendor (no diversity) notice.
    guards = _comparison_guardrails(requested_count, [c["vendor"] for c in comparison])
    payload = {"status": "success", "count": len(products), "comparison": comparison}
    if guards["_truncation_note"]:
        payload["truncated"] = True
        payload["truncation_note"] = guards["_truncation_note"]
    if guards["single_vendor_notice"]:
        payload["single_vendor_notice"] = guards["single_vendor_notice"]
    return _model_tool_json(**payload)


def _supplier_state_for_vendor(vendor_name):
    from flask import session as flask_session, current_app as app, has_request_context
    if not has_request_context():
        return {"known": False, "is_active": True, "notes": ""}
    company_id = flask_session.get("company_id")
    if not company_id or not vendor_name:
        return {"known": False, "is_active": True, "notes": ""}
    try:
        import db_compat  # noqa: F401
        import MySQLdb.cursors
        cur = app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT is_active, notes FROM company_supplier_preferences WHERE company_id = %s AND vendor_name = %s",
            (company_id, vendor_name),
        )
        row = cur.fetchone()
        cur.close()
        if row:
            return {"known": True, "is_active": bool(row.get("is_active")), "notes": row.get("notes") or ""}
    except Exception:
        pass
    return {"known": False, "is_active": True, "notes": ""}


def _company_employee_contact(username):
    from flask import session as flask_session, current_app as app, has_request_context
    if not has_request_context():
        return {}
    if not username or not flask_session.get("company_id"):
        return {}
    try:
        import db_compat  # noqa: F401
        import MySQLdb.cursors
        cur = app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT cu.full_name, cu.email, cu.phone, cu.department, cu.job_title
            FROM company_users cu JOIN users u ON cu.user_id = u.id
            WHERE u.username = %s AND cu.company_id = %s AND cu.status = 'active'
            LIMIT 1
            """,
            (username, flask_session.get("company_id")),
        )
        row = cur.fetchone() or {}
        cur.close()
        return row
    except Exception:
        return {}


def _open_orders_for_user(username, limit=5):
    from flask import session as flask_session, current_app as app, has_request_context
    if not has_request_context():
        return []
    if not username or not flask_session.get("company_id"):
        return []
    try:
        import db_compat  # noqa: F401
        import MySQLdb.cursors
        cur = app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT order_id, product_handle, product_title, status, price, created_at
            FROM course_orders
            WHERE company_id = %s AND username = %s AND status NOT IN ('completed', 'cancelled')
            ORDER BY created_at DESC LIMIT %s
            """,
            (flask_session.get("company_id"), username, limit),
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception:
        return []


def _supplier_agreements_for_company():
    from flask import session as flask_session, current_app as app, has_request_context
    if not has_request_context():
        return []
    if not flask_session.get("company_id"):
        return []
    try:
        import db_compat  # noqa: F401
        import MySQLdb.cursors
        cur = app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT vendor_name, discount_type, discount_value, agreement_name, valid_until
            FROM company_supplier_agreements
            WHERE company_id = %s AND is_active = 1
              AND (valid_from IS NULL OR valid_from <= CURDATE())
              AND (valid_until IS NULL OR valid_until >= CURDATE())
            ORDER BY vendor_name
            """,
            (flask_session.get("company_id"),),
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception:
        return []


def _execute_get_learning_context(args, username):
    """Return the user's full learning context in one call.

    The tool advertises "profile, shown products, company budget, supplier
    preferences/agreements, open orders, and completed courses" — this now
    actually returns ALL of those (budget + agreements + completed were
    previously fetched-but-dropped or never fetched, so the model claimed to
    have checked a budget it never saw). Every section is guarded so a missing
    table or an anonymous caller degrades to empty rather than erroring.
    """
    from flask import session as flask_session, has_app_context, has_request_context
    import datetime as _dt
    profile = {}
    completed = []
    profile_summary = {}
    if username and has_app_context():
        try:
            from app1.user_profile_db import get_full_profile, ensure_tables
            ensure_tables()
            profile = get_full_profile(username) or {}
            completed = [
                {"title": c.get("title"), "handle": c.get("handle"), "vendor": c.get("vendor")}
                for c in (profile.get("completed_courses", []) or [])[:10]
            ]
            profile_summary = {
                "headline": profile.get("headline", ""),
                "target_role": profile.get("target_role", ""),
                "goals": profile.get("goals", ""),
                "skills": [f"{s['name']} ({s['level']})" for s in (profile.get("skills") or [])[:12]],
                "preferred_location": profile.get("preferred_location", ""),
                "preferred_format": profile.get("preferred_format", ""),
            }
        except Exception:
            profile = {}

    employee = _company_employee_contact(username)
    department = (flask_session.get("company_department") if has_request_context() else "") or employee.get("department") or ""

    # Company budget for the user's department (the contract item that was
    # missing — the model previously asserted budget checks it never made).
    budget = None
    company_id = flask_session.get("company_id") if has_request_context() else None
    if company_id and department:
        try:
            import MySQLdb.cursors
            from flask import current_app as _ca
            cur = _ca.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute(
                "SELECT annual_budget, spent, fiscal_year FROM department_budgets "
                "WHERE company_id = %s AND department = %s AND fiscal_year = %s",
                (company_id, department, _dt.datetime.now().year),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                annual = float(row["annual_budget"] or 0)
                spent = float(row["spent"] or 0)
                budget = {
                    "annual_budget": f"{annual:,.0f} kr",
                    "spent": f"{spent:,.0f} kr",
                    "remaining": f"{annual - spent:,.0f} kr",
                    "remaining_num": round(annual - spent, 2),
                    "fiscal_year": row.get("fiscal_year"),
                }
        except Exception:
            budget = None

    # Active supplier agreements (negotiated discounts) — already a helper.
    agreements = []
    try:
        for a in _supplier_agreements_for_company():
            agreements.append({
                "vendor": a.get("vendor_name"),
                "discount_type": a.get("discount_type"),
                "discount_value": a.get("discount_value"),
                "agreement_name": a.get("agreement_name") or "",
            })
    except Exception:
        agreements = []

    shown = list(_current_shown_handles)[:10]
    return _model_tool_json(
        status="success",
        username=username or "",
        department=department,
        employee={
            "name": employee.get("full_name", ""),
            "email": employee.get("email", ""),
            "phone": employee.get("phone", ""),
            "job_title": employee.get("job_title", ""),
        },
        profile=profile_summary,
        completed_courses=completed,
        company_budget=budget,
        supplier_agreements=agreements,
        shown_product_handles=shown,
        open_orders=_open_orders_for_user(username),
    )


def _execute_check_course_readiness(args, username):
    product = catalog.get_product(args.get("product_handle", ""))
    if not product:
        return json.dumps({"status": "not_found", "message": "Kurset blev ikke fundet."}, ensure_ascii=False)
    supplier_state = _supplier_state_for_vendor(product.get("vendor"))
    employee = _company_employee_contact(username)
    missing = []
    if not employee.get("full_name"):
        missing.append("navn")
    if not employee.get("email"):
        missing.append("email")
    if not employee.get("phone"):
        missing.append("telefon")
    variant_date = args.get("variant_date") or ""
    variant_location = args.get("variant_location") or ""
    if product.get("variants") and not (variant_date or variant_location):
        missing.append("dato/lokation")
    readiness = "ready" if not missing and supplier_state.get("is_active", True) else "blocked" if not supplier_state.get("is_active", True) else "needs_info"
    return json.dumps({
        "status": "success",
        "readiness": readiness,
        "missing_fields": missing,
        "product": _catalog_compact_fields(product),
        "supplier_state": supplier_state,
        "employee": {
            "name": employee.get("full_name", ""),
            "email": employee.get("email", ""),
            "phone": employee.get("phone", ""),
            "department": employee.get("department", ""),
        },
        "message": "Klar til bekræftelse." if readiness == "ready" else "Der mangler oplysninger før tilmelding.",
    }, ensure_ascii=False, default=str)


def _execute_prepare_course_order(args, username):
    product = catalog.get_product(args.get("product_handle", ""))
    if not product:
        return json.dumps({"status": "not_found", "message": "Kurset blev ikke fundet."}, ensure_ascii=False)
    employee = _company_employee_contact(username)
    user_name = args.get("user_name") or employee.get("full_name") or username or ""
    user_email = args.get("user_email") or employee.get("email") or ""
    user_phone = args.get("user_phone") or employee.get("phone") or ""
    missing = []
    if not user_name:
        missing.append("user_name")
    if not user_email:
        missing.append("user_email")
    if not user_phone:
        missing.append("user_phone")
    payload = {
        "product_handle": product["handle"],
        "user_name": user_name,
        "user_email": user_email,
        "user_phone": user_phone,
        "variant_date": args.get("variant_date") or "",
        "variant_location": args.get("variant_location") or "",
    }
    return json.dumps({
        "status": "ready_for_confirmation" if not missing else "needs_info",
        "creates_order": False,
        "missing_fields": missing,
        "confirmation_payload": payload,
        "product": _catalog_compact_fields(product),
        "confirmation_text": (
            f"Bekræft at du vil anmode om tilmelding til {product['title']} "
            f"for {user_name or 'brugeren'}."
        ),
    }, ensure_ascii=False, default=str)


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

    results = _filter_blocked_vendors(detailed.get("products", []))
    confidence = detailed.get("confidence", "medium")

    if not results:
        return json.dumps({"status": "no_results", "message": "Ingen kurser matchede din søgning.", "confidence": confidence})

    compact_results = [_extract_compact_fields(r) for r in results]

    # Per-card "why": a verifiable reason derived from the matched query terms +
    # concrete attributes (threaded to the UI card by the agent).
    q_terms = _goal_keywords(query, limit=6)
    for cr in compact_results:
        reason = _course_match_reason(cr, query_terms=q_terms)
        if reason:
            cr["match_reason"] = reason

    # Mark re-shown products so the AI can address it
    for cr in compact_results:
        if cr.get("handle") in _current_shown_handles:
            cr["previously_shown"] = True

    return _model_tool_json(
        status="success",
        count=len(compact_results),
        results=compact_results,
        confidence=confidence,
    )


def _filter_products_by_constraints(products, *, location="", price_min=None, price_max=None,
                                    product_type="", tag="", start_after=None, start_before=None,
                                    language="", difficulty=""):
    """filter_courses' hard-filter predicates as a reusable pass (TL-03), so
    progressive relaxation can re-run the same predicates with loosened
    constraints.

    Budget now checks whether ANY bookable variant falls inside the window
    (via _price_in_budget) instead of only variants[0], and the language /
    difficulty facets honour structured_metadata when present."""
    filtered = []
    for p in products:
        variants = p.get("variants", [])

        if location:
            variant_locs = [v.get("option1", "") for v in variants if v.get("option1")]
            if not any(_location_matches(location, vloc) for vloc in variant_locs):
                continue

        if (price_min is not None or price_max is not None) and not _price_in_budget(variants, price_min, price_max):
            continue

        if language and not _matches_language(p, language):
            continue

        if difficulty and not _matches_difficulty(p, difficulty):
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
    return filtered


def _execute_filter_courses(args):
    """Handle filter_courses tool execution with hybrid ranking."""
    location = args.get("location", "").lower().strip()
    price_min = args.get("price_min")
    price_max = args.get("price_max")
    product_type = args.get("product_type", "").lower().strip()
    tag = args.get("tag", "").lower().strip()
    language = (args.get("language") or "").strip()
    difficulty = (args.get("difficulty") or args.get("level") or "").strip()
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

    products = _filter_blocked_vendors(load_augmented_products())
    if not products:
        return json.dumps({"status": "error", "message": "Produktindekset er ikke indlæst."})

    # Language + difficulty are hard, metadata-backed facets that are NOT part of
    # the progressive-relaxation ladder (we never silently switch a learner from
    # a Danish to an English course, or beginner to advanced). Apply them up
    # front so every relaxation re-run below operates on the constrained set.
    if language:
        products = [p for p in products if _matches_language(p, language)]
    if difficulty:
        products = [p for p in products if _matches_difficulty(p, difficulty)]

    filtered = _filter_products_by_constraints(
        products, location=location, price_min=price_min, price_max=price_max,
        product_type=product_type, tag=tag, start_after=start_after, start_before=start_before,
    )

    # TL-03: progressive relaxation i stedet for et dødt no_results — løsn ét
    # hårdt filter ad gangen (datointerval → pris +25% → lokation) og fortæl
    # modellen præcist hvad der blev løsnet via relaxed_filters, så den kan
    # narrere det ærligt i stedet for at dead-ende.
    relaxed_filters = []
    if not filtered and _hard_filter_enabled():
        if start_after or start_before:
            start_after = start_before = None
            relaxed_filters.append("date_range_dropped")
            filtered = _filter_products_by_constraints(
                products, location=location, price_min=price_min, price_max=price_max,
                product_type=product_type, tag=tag, start_after=start_after, start_before=start_before,
            )
        if not filtered and price_max is not None:
            try:
                price_max = float(price_max) * 1.25
                relaxed_filters.append("price_max_widened_25pct")
                filtered = _filter_products_by_constraints(
                    products, location=location, price_min=price_min, price_max=price_max,
                    product_type=product_type, tag=tag, start_after=start_after, start_before=start_before,
                )
            except (ValueError, TypeError):
                pass
        if not filtered and location:
            location = ""
            relaxed_filters.append("location_dropped")
            filtered = _filter_products_by_constraints(
                products, location=location, price_min=price_min, price_max=price_max,
                product_type=product_type, tag=tag, start_after=start_after, start_before=start_before,
            )
        if not filtered and relaxed_filters:
            # Lempelsen hjalp ikke — sig det eksplicit, så listen aldrig er
            # uforklaret tom.
            return json.dumps({
                "status": "no_results",
                "message": "Ingen kurser matchede dine filtre — heller ikke efter lempelse af dato, pris og lokation.",
                "relaxation_attempted": True,
                "relaxed_filters": relaxed_filters,
            }, ensure_ascii=False)

    # Use hybrid ranking when query is provided (Phase 5)
    if query and filtered:
        filtered = hybrid_rank_products(filtered, query, products, limit=limit)
    else:
        def sort_price(p):
            mp = _min_variant_price(p.get("variants"))
            return mp if mp is not None else 999999
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

    # Per-card "why" derived from the query/tag terms + concrete attributes.
    f_terms = _goal_keywords(" ".join([query, tag]).strip(), limit=6)
    for cr in compact_results:
        reason = _course_match_reason(cr, query_terms=f_terms)
        if reason:
            cr["match_reason"] = reason

    # Mark re-shown products
    _mark_previously_shown(compact_results)

    # TL-03: tell the model exactly which filters were loosened to produce
    # these results (samme flag-familie som catalog_search/RAG-fallback).
    extra = {}
    if relaxed_filters:
        extra["filters_relaxed"] = True
        extra["relaxed_filters"] = relaxed_filters
        if "location_dropped" in relaxed_filters:
            extra["relaxation_hint"] = (
                "Ingen kurser matchede lokationen — foreslå evt. e-learning, som kan tages uanset by."
            )

    return _model_tool_json(status="success", count=len(compact_results), results=compact_results, **extra)


def _execute_get_course_details(args):
    """Handle get_course_details tool execution."""
    handle = args.get("handle")
    products = load_augmented_products()

    for p in products:
        if p.get("handle") == handle:
            raw_locs = [v.get("option1") for v in p.get("variants", []) if v.get("option1")]
            locations = list(dict.fromkeys(extract_city_name(loc) for loc in raw_locs if extract_city_name(loc)))
            # TL-02: never label expired dates as "upcoming"
            dates, no_upcoming_dates = _upcoming_dates(p.get("variants", []))
            meta = p.get("structured_metadata", {}) or {}
            # Same negotiated price here as in search results (shared helper: price_view).
            pv = price_view(p.get("variants", [{}])[0].get("price") if p.get("variants") else None, p.get("vendor", ""))
            result = {
                "status": "success",
                "title": p.get("title"),
                "handle": handle,
                "price": pv["price"],
                "vendor": p.get("vendor"),
                "locations": locations,
                "upcoming_dates": dates[:4],
                "summary": _truncate_summary(p.get("ai_summary", p.get("body_html", "")[:200])),
            }
            if pv["discount"]:
                result["discount"] = pv["discount"]
            if no_upcoming_dates:
                result["no_upcoming_dates"] = True
            elif dates and _date_data_stale(p):
                # Datoerne stammer fra et >90 dage gammelt katalog-sync — bed
                # modellen tage forbehold for dem.
                result["date_data_stale"] = True
            if meta.get("duration_days"):
                result["duration_days"] = meta["duration_days"]
            return _model_tool_json(**result)

    return json.dumps({"status": "not_found", "message": f"Kunne ikke finde kursus med handle '{handle}'."})


def _execute_compare_courses(args):
    """Phase 2: Compare 2-4 courses side by side."""
    handles = args.get("handles", [])
    requested_count = len(handles)
    if requested_count < 2:
        return json.dumps({"status": "error", "message": "Mindst 2 kurser kræves for sammenligning."})

    products = load_augmented_products()
    handle_map = {p.get("handle"): p for p in products}

    comparisons = []
    # Hard-cap to COMPARE_MAX_COURSES — extras are truncated (and noted below).
    for handle in handles[:COMPARE_MAX_COURSES]:
        p = handle_map.get(handle)
        if not p:
            continue
        variants = p.get("variants", [])
        locations = list(set(extract_city_name(v.get("option1", "")) for v in variants if v.get("option1") and extract_city_name(v.get("option1"))))
        # TL-02: never label expired dates as "upcoming"
        dates, no_upcoming_dates = _upcoming_dates(variants)
        dates = dates[:3]

        # Same negotiated price here as in search/details (shared helper: price_view).
        pv = price_view(variants[0].get("price") if variants else None, p.get("vendor", ""))
        meta = (p.get("structured_metadata") or {})
        comp = {
            "title": p.get("title"),
            "handle": p.get("handle"),
            "price": pv["price"],
            "vendor": p.get("vendor"),
            "product_type": p.get("product_type", ""),
            "locations": locations[:3],
            "upcoming_dates": dates,
            "summary": _truncate_summary(p.get("ai_summary", "")),
            "variant_count": len(variants),
            # Numeric/structured axes used for the winners analysis (kept on the
            # payload so the comparison card can render them too).
            "price_num": _min_variant_price(variants),
            "duration_days": meta.get("duration_days"),
            "certification": meta.get("certification") or "",
            "difficulty": meta.get("difficulty") or "",
            "soonest_date": dates[0] if dates else "",
        }
        if pv["discount"]:
            comp["discount"] = pv["discount"]
        if no_upcoming_dates:
            comp["no_upcoming_dates"] = True
        comparisons.append(comp)

    if len(comparisons) < 2:
        return json.dumps({"status": "error", "message": "Kunne ikke finde nok kurser til sammenligning."})

    # Analytical decision support: per-axis winners + a short, grounded verdict.
    analysis = _comparison_analysis(comparisons)

    # Guardrails: cap-truncation note + single-vendor (no diversity) notice.
    guards = _comparison_guardrails(requested_count, [c["vendor"] for c in comparisons])
    payload = {"status": "success", "comparison": comparisons, "analysis": analysis}
    if guards["_truncation_note"]:
        payload["truncated"] = True
        payload["truncation_note"] = guards["_truncation_note"]
    if guards["single_vendor_notice"]:
        payload["single_vendor_notice"] = guards["single_vendor_notice"]
    return _model_tool_json(**payload)


def _comparison_analysis(comparisons):
    """Compute per-axis winners + a short Danish verdict from a comparison set.

    Every claim is derived from real fields (cheapest = min bookable price,
    shortest = min duration_days, certificering = has certification, soonest =
    first upcoming date), so the verdict is grounded, not an LLM guess. Returns
    {"winners": {axis: {handle, title, value}}, "verdict": str}.
    """
    winners = {}

    priced = [c for c in comparisons if isinstance(c.get("price_num"), (int, float))]
    if priced:
        cheapest = min(priced, key=lambda c: c["price_num"])
        winners["cheapest"] = {
            "handle": cheapest["handle"], "title": cheapest["title"],
            "value": _normalize_price(cheapest["price_num"]),
        }

    timed = [c for c in comparisons if isinstance(c.get("duration_days"), (int, float))]
    if timed:
        shortest = min(timed, key=lambda c: c["duration_days"])
        winners["shortest"] = {
            "handle": shortest["handle"], "title": shortest["title"],
            "value": f"{shortest['duration_days']} dag(e)",
        }

    certified = [c for c in comparisons if c.get("certification")]
    if certified:
        winners["certification"] = {
            "handle": certified[0]["handle"], "title": certified[0]["title"],
            "value": certified[0]["certification"],
            "all": [{"handle": c["handle"], "title": c["title"], "value": c["certification"]} for c in certified],
        }

    dated = [c for c in comparisons if c.get("soonest_date")]
    if dated:
        # _upcoming_dates already drops past dates, so the listed dates are
        # ordered as the catalog provides; first non-empty is the soonest shown.
        winners["soonest"] = {
            "handle": dated[0]["handle"], "title": dated[0]["title"],
            "value": dated[0]["soonest_date"],
        }

    # Verdict: a single, honest recommendation framed by the strongest axis.
    verdict_bits = []
    if "cheapest" in winners:
        verdict_bits.append(f"Billigst: {winners['cheapest']['title']} ({winners['cheapest']['value']})")
    if "certification" in winners:
        verdict_bits.append(f"Giver certificering: {winners['certification']['title']}")
    if "shortest" in winners:
        verdict_bits.append(f"Hurtigst overstået: {winners['shortest']['title']} ({winners['shortest']['value']})")
    verdict = " · ".join(verdict_bits[:3])

    return {"winners": winners, "verdict": verdict}


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
            "description": "Opdater brugerens profil: tilføj/fjern kompetencer, erfaring, uddannelse, gennemførte kurser, certificeringer, sprog, eller opdater profiloversigt (headline, bio, mål, præferencer). Brug dette når brugeren fortæller om sig selv, tilføjer en kompetence/certificering/sprog, eller vil opdatere sin profil. Brug add_certification (ikke add_course) når der er tale om en rigtig certificering med udsteder eller udløbsdato (fx PRINCE2, AWS, Google Ads, kørekort).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_skill", "remove_skill", "update_skill_level",
                                 "add_experience", "remove_experience", "update_experience",
                                 "add_education", "remove_education", "update_education",
                                 "add_course", "remove_course", "update_course",
                                 "add_certification", "remove_certification", "update_certification",
                                 "add_language", "remove_language", "update_language_level",
                                 "add_link", "remove_link",
                                 "update_summary"],
                        "description": "The profile update action to perform."
                    },
                    "data": {
                        "type": "object",
                        "description": "Fields for the action — include ONLY the relevant ones. add_skill: skill_name (required), skill_level. add_experience: title (required), company, start_year, end_year, is_current, description. add_education: degree (required), institution, year_completed. add_course: course_title (required), vendor, completed_date. add_certification: name (required), issuer, issue_date, expiry_date (tomt = udløber ikke), credential_id, credential_url. add_language: language (required), proficiency. add_link: url (required), label, kind. update_summary: headline, bio, goals, preferred_location, preferred_format, budget_range. remove_*/update_*: include id or the name/title.",
                        "properties": {
                            "skill_name": {"type": "string"},
                            "skill_level": {"type": "string", "enum": ["begynder", "mellem", "avanceret", "ekspert"]},
                            "title": {"type": "string"},
                            "company": {"type": "string"},
                            "start_year": {"type": "string"},
                            "end_year": {"type": "string"},
                            "is_current": {"type": "boolean"},
                            "description": {"type": "string"},
                            "degree": {"type": "string"},
                            "institution": {"type": "string"},
                            "year_completed": {"type": "string"},
                            "course_title": {"type": "string"},
                            "course_handle": {"type": "string"},
                            "vendor": {"type": "string"},
                            "completed_date": {"type": "string"},
                            "certificate_note": {"type": "string"},
                            "name": {"type": "string", "description": "Certificeringens navn (add_certification)."},
                            "issuer": {"type": "string", "description": "Udsteder af certificeringen, fx Microsoft, AWS, PMI."},
                            "issue_date": {"type": "string", "description": "Udstedt (YYYY, YYYY-MM eller YYYY-MM-DD)."},
                            "expiry_date": {"type": "string", "description": "Udløber (YYYY, YYYY-MM eller YYYY-MM-DD). Tom = udløber ikke."},
                            "credential_id": {"type": "string", "description": "Credential-/bevis-ID."},
                            "credential_url": {"type": "string", "description": "Verificerings-URL."},
                            "language": {"type": "string", "description": "Sprog (add_language/remove_language)."},
                            "proficiency": {"type": "string", "enum": ["begynder", "mellem", "flydende", "modersmaal"], "description": "Sprogniveau."},
                            "label": {"type": "string", "description": "Vist tekst for et link (add_link), fx 'LinkedIn'."},
                            "url": {"type": "string", "description": "URL for et portfolio-link (add_link)."},
                            "kind": {"type": "string", "enum": ["linkedin", "github", "portfolio", "website", "certificate", "other"], "description": "Linktype (valgfri — udledes ellers af URL'en)."},
                            "id": {"type": "integer"},
                            "headline": {"type": "string"},
                            "bio": {"type": "string"},
                            "goals": {"type": "string"},
                            "preferred_location": {"type": "string"},
                            "preferred_format": {"type": "string"},
                            "budget_range": {"type": "string"}
                        }
                    }
                },
                "required": ["action", "data"]
            },
            "strict": False
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember_about_user",
            "description": "Gem en løs, fritekst-kendsgerning om brugeren i langtidshukommelsen: en præference, livssituation, et personlighedstræk, en interesse eller et blødt mål, der IKKE passer i et struktureret profilfelt (kompetence/erfaring/uddannelse/certificering/sprog). Brug det når brugeren afslører noget personligt, der er værd at huske på tværs af samtaler (fx 'foretrækker aftenundervisning', 'skifter karriere til data science', 'lærer bedst praktisk'). Brug IKKE til strukturerede data — der bruges update_user_profile.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Kort kendsgerning i 3.-persons-stil, fx 'Foretrækker aftenundervisning'."},
                    "category": {"type": "string", "enum": ["praeference", "maal", "kontekst", "personlighed", "interesse", "andet"], "description": "Kategori for hukommelsen."},
                    "detail": {"type": "string", "description": "Valgfri kort uddybning."}
                },
                "required": ["label"]
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
        return _model_tool_json(
            status="success",
            profile_text=formatted if formatted else "Brugeren har endnu ikke udfyldt sin profil.",
        )
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Fejl ved hentning af profil: {e}"})


def _normalize_memory_label(label):
    """Normalize a memory label for near-duplicate detection."""
    import re as _re
    return _re.sub(r"\s+", " ", label.lower().strip())


def _find_supersedable_memory(existing_memories, new_label_norm, min_len=4):
    """Find an existing memory that should be superseded by the new label.

    Returns (memory_id, old_label) when:
    - The existing normalized label is a substring of the new (new is more specific), OR
    - The new normalized label is a substring of the existing one AND at least 70% as
      long (new is a slightly shorter variant of the same fact).
    Returns (None, None) when no near-duplicate is found.
    """
    if len(new_label_norm) < min_len:
        return None, None
    for m in existing_memories:
        old_norm = _normalize_memory_label(m.get("label") or "")
        if len(old_norm) < min_len:
            continue
        if old_norm == new_label_norm:
            # Exact duplicate — add_memory's UNIQUE constraint handles this,
            # return early so we don't supersede with the same text.
            return m["id"], m["label"]
        # Substring supersede: old is contained in new → new is more specific
        if old_norm in new_label_norm:
            return m["id"], m["label"]
        # Partial supersede: new is contained in old AND new is ≥70% of old's length
        if new_label_norm in old_norm and len(new_label_norm) >= 0.70 * len(old_norm):
            return m["id"], m["label"]
    return None, None


def _execute_remember_about_user(args, username):
    """Save a free-form atomic memory about the user (immediate, not proposed).

    Memories are low-stakes and the user can review/delete them on the Mind-Map,
    so unlike structured profile writes these save directly and surface a
    'memory_saved' chip rather than a confirm card.

    Phase 11: near-duplicate detection supersedes stale/redundant entries rather
    than growing the store unboundedly. Exact duplicates are already handled by
    add_memory's UNIQUE(username, label) constraint."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    label = (args.get("label") or "").strip()
    if not label or len(label) < 3:
        return json.dumps({"status": "error", "message": "label mangler eller er for kort."})
    category = (args.get("category") or "andet").strip()
    detail = (args.get("detail") or "").strip() or None
    try:
        from app1 import user_profile_db as db
        db.ensure_tables()
        # Near-duplicate check: scan existing memories for a supersedable entry.
        try:
            existing = db.get_memories(username, limit=200)
            new_norm = _normalize_memory_label(label)
            sup_id, sup_label = _find_supersedable_memory(existing, new_norm)
            if sup_id is not None and sup_label != label:
                # Supersede: update category/detail/label in place.
                db.update_memory(username, sup_id,
                                 label=label, category=category, detail=detail)
                return json.dumps({"status": "memory_saved", "label": label[:200],
                                   "category": category, "memory_id": sup_id,
                                   "message": f"Opdateret hukommelse: {label[:200]}"})
        except Exception as dedup_err:
            # Dedup is best-effort — fall through to normal add_memory on failure.
            print(f"[MemoryDedup] {dedup_err}")
        # add_memory is the authoritative truncation point (label->200, detail->5000).
        new_id = db.add_memory(username, label, category=category, detail=detail,
                               source="ai", confidence=0.9)
        # Only surface a real integer id (the inline-delete affordance needs one);
        # never a non-serialisable value.
        mem_id = new_id if isinstance(new_id, int) else None
        return json.dumps({"status": "memory_saved", "label": label[:200],
                           "category": category, "memory_id": mem_id,
                           "message": f"Husket: {label[:200]}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Kunne ikke gemme hukommelse: {e}"})


_FIELD_MAX_LENGTHS = {
    "skill_name": 100, "title": 150, "company": 150, "description": 500,
    "degree": 150, "institution": 150, "course_title": 200, "vendor": 150,
    "headline": 100, "bio": 500, "goals": 500, "budget_range": 50,
    "preferred_location": 100, "preferred_format": 50,
    "name": 200, "issuer": 150, "issue_date": 20, "expiry_date": 20,
    "credential_id": 120, "credential_url": 500, "language": 80,
}

def _clean_profile_data(data):
    """Trim whitespace and enforce max lengths on profile data fields."""
    cleaned = {}
    for k, v in data.items():
        if isinstance(v, str):
            v = v.strip()
            max_len = _FIELD_MAX_LENGTHS.get(k)
            if max_len and len(v) > max_len:
                v = v[:max_len]
        cleaned[k] = v
    return cleaned

def _execute_update_user_profile(args, username):
    """Validate profile update and return proposed action for user confirmation.
    Add-actions are NOT executed here — they go through frontend confirmation first.
    Remove/update actions execute immediately."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})

    action = args.get("action", "")
    data = _clean_profile_data(args.get("data", {}))

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
                # Incomplete — show an input form (prefilled) instead of erroring.
                return json.dumps({
                    "status": "ui_card", "ui_type": "form", "section": "experience",
                    "message": "Udfyld din erfaring, så gemmer jeg den på din profil:",
                    "save_action": "add_experience",
                    "prefilled": {k: v for k, v in data.items() if v},
                    "fields": [
                        {"name": "title", "label": "Stilling / titel", "type": "text", "placeholder": "F.eks. Projektleder"},
                        {"name": "company", "label": "Virksomhed", "type": "text", "placeholder": "F.eks. Nordi A/S"},
                        {"name": "start_year", "label": "Startår", "type": "number", "placeholder": "2020"},
                        {"name": "end_year", "label": "Slutår (tom = nuværende)", "type": "number", "placeholder": "2024"},
                    ],
                })
            # Validate year range
            start_yr = data.get("start_year")
            end_yr = data.get("end_year")
            if start_yr and end_yr:
                try:
                    if int(end_yr) < int(start_yr):
                        return json.dumps({"status": "error", "message": f"Slutår ({end_yr}) kan ikke være før startår ({start_yr})."})
                except (ValueError, TypeError):
                    pass
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
                # Incomplete — show an input form (prefilled) instead of erroring so
                # the user can add the education from a card.
                return json.dumps({
                    "status": "ui_card", "ui_type": "form", "section": "education",
                    "message": "Udfyld din uddannelse, så gemmer jeg den på din profil:",
                    "save_action": "add_education",
                    "prefilled": {k: v for k, v in data.items() if v},
                    "fields": [
                        {"name": "degree", "label": "Uddannelse / grad", "type": "text", "placeholder": "F.eks. HA, Cand.merc., Diplom"},
                        {"name": "institution", "label": "Institution", "type": "text", "placeholder": "F.eks. CBS"},
                        {"name": "year_completed", "label": "Afsluttet år", "type": "number", "placeholder": "2020"},
                    ],
                })
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

        elif action == "add_certification":
            name = data.get("name", "").strip()
            if not name:
                # Incomplete — show a prefilled input card instead of erroring.
                return json.dumps({
                    "status": "ui_card", "ui_type": "form", "section": "certifications",
                    "message": "Udfyld certificeringen, så gemmer jeg den på din profil:",
                    "save_action": "add_certification",
                    "prefilled": {k: v for k, v in data.items() if v},
                    "fields": [
                        {"name": "name", "label": "Certificering", "type": "text", "placeholder": "F.eks. PRINCE2 Foundation"},
                        {"name": "issuer", "label": "Udsteder", "type": "text", "placeholder": "F.eks. AXELOS"},
                        {"name": "issue_date", "label": "Udstedt (år)", "type": "text", "placeholder": "2023"},
                        {"name": "expiry_date", "label": "Udløber (tom = udløber ikke)", "type": "text", "placeholder": "2026"},
                    ],
                })
            issuer = data.get("issuer", "").strip()
            try:
                existing = db.get_certifications(username)
                for c in existing:
                    if c["name"].lower() == name.lower() and (c.get("issuer", "") or "").lower() == issuer.lower():
                        return json.dumps({"status": "already_exists",
                            "message": f'Certificeringen "{name}"' + (f' fra {issuer}' if issuer else '') + ' findes allerede.'})
            except Exception as dup_err:
                print(f"[ProfileUpdate] Certification duplicate check failed (proceeding): {dup_err}")
                try:
                    from flask import current_app as _app
                    _app.mysql.connection.rollback()
                except Exception:
                    pass
            label = name + (f' — {issuer}' if issuer else '')
            return json.dumps({"status": "proposed", "section": "certifications",
                "message": f'Tilføj certificering: {label}',
                "confirm": {"action": "add_certification", "data": data}})

        elif action == "add_language":
            language = data.get("language", "").strip()
            if not language:
                return json.dumps({"status": "error", "message": "language mangler."})
            proficiency = data.get("proficiency", "mellem")
            if proficiency not in ("begynder", "mellem", "flydende", "modersmaal"):
                proficiency = "mellem"
            try:
                existing = db.get_languages(username)
                for l in existing:
                    if l["language"].lower() == language.lower():
                        return json.dumps({"status": "already_exists",
                            "message": f'Sproget "{language}" findes allerede ({l["proficiency"]}).'})
            except Exception as dup_err:
                print(f"[ProfileUpdate] Language duplicate check failed (proceeding): {dup_err}")
                try:
                    from flask import current_app as _app
                    _app.mysql.connection.rollback()
                except Exception:
                    pass
            return json.dumps({"status": "proposed", "section": "languages",
                "message": f'Tilføj sprog: {language} ({proficiency})',
                "confirm": {"action": "add_language", "data": {"language": language, "proficiency": proficiency}}})

        elif action == "remove_certification":
            cert_id = data.get("id")
            name = data.get("name", "").strip()
            if not cert_id and name:
                try:
                    for c in db.get_certifications(username):
                        if c["name"].lower() == name.lower():
                            cert_id = c["id"]
                            break
                except Exception:
                    pass
            if not cert_id:
                return json.dumps({"status": "error", "message": "id eller name mangler."})
            removed = db.remove_certification(username, cert_id)
            if removed:
                return json.dumps({"status": "success", "section": "certifications", "message": "Certificering fjernet."})
            return json.dumps({"status": "not_found", "message": "Certificering ikke fundet."})

        elif action == "update_certification":
            cert_id = data.get("id")
            if not cert_id:
                return json.dumps({"status": "error", "message": "id mangler."})
            fields = {k: v for k, v in data.items() if k != "id" and v is not None and str(v).strip()}
            if not fields:
                return json.dumps({"status": "error", "message": "Ingen felter at opdatere."})
            updated = db.update_certification(username, cert_id, **fields)
            if updated:
                return json.dumps({"status": "success", "section": "certifications", "message": "Certificering opdateret."})
            return json.dumps({"status": "not_found", "message": "Certificering ikke fundet."})

        elif action == "remove_language":
            language = data.get("language", "").strip()
            if not language:
                return json.dumps({"status": "error", "message": "language mangler."})
            removed = db.remove_language(username, language)
            if removed:
                return json.dumps({"status": "success", "section": "languages", "message": f'"{language}" fjernet fra sprog.'})
            return json.dumps({"status": "not_found", "message": f'Sproget "{language}" blev ikke fundet.'})

        elif action == "update_language_level":
            language = data.get("language", "").strip()
            level = data.get("proficiency", "")
            if not language or not level:
                return json.dumps({"status": "error", "message": "language og proficiency kræves."})
            if level not in ("begynder", "mellem", "flydende", "modersmaal"):
                return json.dumps({"status": "error", "message": f'Ugyldigt niveau: "{level}". Brug: begynder/mellem/flydende/modersmaal.'})
            updated = db.update_language_level(username, language, level)
            if updated:
                return json.dumps({"status": "success", "section": "languages", "message": f'"{language}" opdateret til {level}.'})
            return json.dumps({"status": "not_found", "message": f'Sproget "{language}" blev ikke fundet.'})

        elif action == "add_link":
            url = data.get("url", "").strip()
            if not url:
                return json.dumps({"status": "error", "message": "url mangler."})
            if not (url.startswith("http://") or url.startswith("https://")):
                url = "https://" + url
            label = data.get("label", "").strip() or url
            payload = {"url": url, "label": label}
            if data.get("kind"):
                payload["kind"] = data.get("kind")
            return json.dumps({"status": "proposed", "section": "links",
                "message": f'Tilføj link: {label}',
                "confirm": {"action": "add_link", "data": payload}})

        elif action == "remove_link":
            link_id = data.get("id")
            url = data.get("url", "").strip()
            if not link_id and url:
                try:
                    for p in db.get_portfolio_links(username):
                        if (p.get("url") or "").rstrip("/") == url.rstrip("/"):
                            link_id = p["id"]
                            break
                except Exception:
                    pass
            if not link_id:
                return json.dumps({"status": "error", "message": "id eller url mangler."})
            removed = db.remove_portfolio_link(username, link_id)
            if removed:
                return json.dumps({"status": "success", "section": "links", "message": "Link fjernet."})
            return json.dumps({"status": "not_found", "message": "Link ikke fundet."})

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

        elif action == "update_experience":
            exp_id = data.get("id")
            if not exp_id:
                return json.dumps({"status": "error", "message": "id mangler."})
            fields = {k: v for k, v in data.items() if k != "id" and v is not None and str(v).strip()}
            if not fields:
                return json.dumps({"status": "error", "message": "Ingen felter at opdatere."})
            updated = db.update_experience(username, exp_id, **fields)
            if updated:
                return json.dumps({"status": "success", "section": "experience", "message": "Erfaring opdateret."})
            return json.dumps({"status": "not_found", "message": "Erfaring ikke fundet."})

        elif action == "remove_education":
            edu_id = data.get("id")
            if not edu_id:
                return json.dumps({"status": "error", "message": "id mangler."})
            removed = db.remove_education(username, edu_id)
            if removed:
                return json.dumps({"status": "success", "section": "education", "message": "Uddannelse fjernet."})
            return json.dumps({"status": "not_found", "message": "Uddannelse ikke fundet."})

        elif action == "update_education":
            edu_id = data.get("id")
            if not edu_id:
                return json.dumps({"status": "error", "message": "id mangler."})
            fields = {k: v for k, v in data.items() if k != "id" and v is not None and str(v).strip()}
            if not fields:
                return json.dumps({"status": "error", "message": "Ingen felter at opdatere."})
            updated = db.update_education(username, edu_id, **fields)
            if updated:
                return json.dumps({"status": "success", "section": "education", "message": "Uddannelse opdateret."})
            return json.dumps({"status": "not_found", "message": "Uddannelse ikke fundet."})

        elif action == "remove_course":
            title = data.get("course_title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "course_title mangler."})
            removed = db.remove_completed_course(username, title)
            if removed:
                return json.dumps({"status": "success", "section": "courses", "message": f'Kursus "{title}" fjernet.'})
            return json.dumps({"status": "not_found", "message": f'Kursus "{title}" ikke fundet.'})

        elif action == "update_course":
            title = data.get("course_title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "course_title mangler."})
            # Re-add with updated fields (ON DUPLICATE KEY UPDATE handles it)
            db.add_completed_course(
                username,
                course_title=title,
                vendor=data.get("vendor", ""),
                completed_date=data.get("completed_date"),
                certificate_note=data.get("certificate_note")
            )
            return json.dumps({"status": "success", "section": "courses", "message": f'Kursus "{title}" opdateret.'})

        elif action == "update_summary":
            # Fallback: if AI put fields at top level instead of in data
            summary_fields = {"headline", "bio", "goals", "preferred_location", "preferred_format", "budget_range", "summary", "goal", "location", "format"}
            if not data:
                data = {k: v for k, v in args.items() if k in summary_fields and v}
            # Also check top-level args as additional fallback
            for k, v in args.items():
                if k in summary_fields and v and k not in data:
                    data[k] = v
            # Map common AI aliases to actual field names
            if "summary" in data and "bio" not in data:
                data["bio"] = data.pop("summary")
            if "goal" in data and "goals" not in data:
                data["goals"] = data.pop("goal")
            if "location" in data and "preferred_location" not in data:
                data["preferred_location"] = data.pop("location")
            if "format" in data and "preferred_format" not in data:
                data["preferred_format"] = data.pop("format")
            # Filter out empty strings so we only update actual changes
            clean_data = {k: v for k, v in data.items() if v is not None and str(v).strip() and k in {"headline", "bio", "goals", "preferred_location", "preferred_format", "budget_range"}}
            if not clean_data:
                # Instead of error, return success with no-op — don't confuse the AI
                return json.dumps({"status": "success", "section": "summary",
                    "message": "Profilen er allerede opdateret."})
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

        # Compute the learner's REAL skill gaps (company targets + target role +
        # goals, on the canonical 1-5 scale) so the recommendation is grounded in
        # "the gap to where you want to go", not just "skills you rated low".
        try:
            from competency import compute_skill_gaps
            gaps = compute_skill_gaps(username, profile=profile)
        except Exception:
            gaps = []
        gap_skill_names = [g["skill"] for g in gaps]

        # Build search query from focus + target role + concrete gap skills.
        query_parts = []
        focus = args.get("focus", "").strip()
        if focus:
            query_parts.append(focus)

        # Target role is the strongest career-direction signal — it anchors the
        # gap reasoning toward where the learner wants to go, not just where
        # they are.
        target_role = (profile.get("target_role") or "").strip()
        if target_role:
            query_parts.append(target_role)

        goals = profile.get("goals", "")

        if gap_skill_names:
            # The gap skills ARE the upskilling target — most authoritative signal.
            query_parts.extend(gap_skill_names[:4])
        else:
            # Fallback (no computed gaps): legacy goals + low-level skills.
            if goals:
                query_parts.append(goals[:100])
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

        # Profile-conditioned re-rank: boost products matching the learner's
        # target skills/goals (reuses the profile already fetched above; no
        # extra DB round-trip). profile_boost is None when there is nothing to
        # boost → legacy semantic order is preserved. Completed courses are then
        # filtered out below (the recommend tool only surfaces NEW courses).
        profile_boost = _profile_boost_from_profile(profile)
        if profile_boost and results:
            try:
                results = hybrid_rank_products(
                    results, search_query, load_augmented_products(),
                    limit=len(results), profile_boost=profile_boost,
                )
            except Exception:
                pass  # fall back to the semantic order on any failure

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

        # Per-gap, verifiable reasons. recommendation_reason stays for backwards
        # compatibility; match_reason is the unified per-card "why" the UI shows.
        all_skills = {s["name"].lower(): s["level"] for s in profile.get("skills", [])}
        profile_boost = _profile_boost_from_profile(profile)
        profile_terms = list((profile_boost or {}).get("target_terms") or [])
        goal_terms = _goal_keywords(" ".join([target_role, goals]).strip(), limit=6)
        # Gap lookup by canonical key (len>=3 to avoid 2-char false positives).
        gap_by_key = {g["skill"].lower(): g for g in gaps if len(g["skill"]) >= 3}
        for cr in compact_results:
            reason_parts = []
            title_lower = cr.get("title", "").lower()
            tags_lower = " ".join(str(t).lower() for t in (cr.get("tags") or []))
            haystack = title_lower + " " + tags_lower
            # 1) Gap-grounded reason (strongest, verifiable): the course mentions a
            # skill the learner has a REAL computed gap in.
            matched_gap = next((g for key, g in gap_by_key.items() if key in haystack), None)
            if matched_gap:
                reason_parts.append(
                    f"lukker dit gap i {matched_gap['skill']} "
                    f"({matched_gap['current_label']}→{matched_gap['target_label']})"
                )
            else:
                for skill_name, skill_level in all_skills.items():
                    if skill_name in title_lower and skill_level in ("begynder", "mellem"):
                        reason_parts.append(f"bygger videre på din {skill_name}-kompetence")
                if target_role and any(w in title_lower for w in _goal_keywords(target_role, limit=4)):
                    reason_parts.append(f"bringer dig mod {target_role}")
                elif goals and any(w in title_lower for w in goals.lower().split()[:3]):
                    reason_parts.append("matcher dine mål")
            cr["recommendation_reason"] = "; ".join(reason_parts) if reason_parts else "relevant for din profil"
            # match_reason: the concise UI "why". A grounded gap reason wins; else
            # derive from profile/goal terms + concrete attributes.
            if matched_gap:
                cr["match_reason"] = reason_parts[0]
            else:
                mr = _course_match_reason(cr, query_terms=goal_terms, profile_terms=profile_terms)
                cr["match_reason"] = mr or cr["recommendation_reason"]

        return _model_tool_json(status="success", count=len(compact_results), results=compact_results, gaps=gaps[:6])
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
                    "enum": ["add_skill", "add_experience", "add_education", "add_course", "update_experience", "update_education", "update_course", "update_summary"],
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
        },
        "strict": False
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
        "name": "save_learning_path",
        "description": (
            "Gem en læringssti permanent på brugerens profil, så den overlever samtalen og kan ses/"
            "følges på profilsiden. Brug dette når brugeren beder dig 'gem læringsstien', 'gem planen', "
            "eller efter du har bygget en sti med suggest_learning_path og brugeren vil beholde den. "
            "suggest_learning_path gemmer allerede automatisk — brug kun dette til en sti du selv har "
            "sammensat fra konkrete kurser."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Kort titel på læringsstien."},
                "goal": {"type": "string", "description": "Det overordnede mål stien fører mod (valgfrit)."},
                "steps": {
                    "type": "array",
                    "description": "Trin i rækkefølge. Hvert trin: {order, level, topic, reason, courses:[{handle,title}]}.",
                    "items": {"type": "object"}
                }
            },
            "required": ["title", "steps"]
        },
        "strict": False
    }
})

PROFILE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "get_learning_path",
        "description": (
            "Hent brugerens gemte læringsstier (eller én bestemt via path_id). Brug dette når brugeren "
            "spørger 'vis min læringssti', 'hvad var min plan', 'hvor langt er jeg i min sti', eller vil "
            "fortsætte en tidligere sti."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path_id": {"type": "integer", "description": "ID på en bestemt sti (valgfrit — udelad for alle)."}
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


# ── Learning Goals (Udviklingsmål) ──

PROFILE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "set_learning_goal",
        "description": "Opret et udviklingsmål for brugeren (f.eks. 'blive bedre til projektledelse', 'lære Power BI inden Q3'). Brug dette når brugeren udtrykker et lærings- eller karrieremål. Efter oprettelse kan du foreslå relevante kurser til målet med recommend_for_profile eller catalog_search.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Kort titel på målet, f.eks. 'Blive certificeret projektleder'."},
                "description": {"type": "string", "description": "Valgfri uddybning af målet."},
                "target_date": {"type": "string", "description": "Valgfri måldato/tidsramme som fritekst, f.eks. 'december 2026' eller 'Q3'."}
            },
            "required": ["title"]
        },
        "strict": False
    }
})

PROFILE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "get_learning_goals",
        "description": "Hent brugerens udviklingsmål og deres status. Brug dette når brugeren spørger om sine mål, fremskridt, eller hvad de arbejder hen imod.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
})

PROFILE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "update_learning_goal",
        "description": "Opdater et eksisterende udviklingsmål: markér som fuldført, sæt på pause, genaktivér, slet, eller ret titel/dato. Kald get_learning_goals først for at finde goal_id, hvis du ikke kender det.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer", "description": "ID på målet (fra get_learning_goals)."},
                "status": {"type": "string", "enum": ["aktiv", "fuldfoert", "paa_pause", "slet"], "description": "Ny status, eller 'slet' for at fjerne målet."},
                "title": {"type": "string", "description": "Valgfri ny titel."},
                "target_date": {"type": "string", "description": "Valgfri ny måldato."}
            },
            "required": ["goal_id"]
        },
        "strict": False
    }
})


def _execute_set_learning_goal(args, username):
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    title = (args.get("title") or "").strip()
    if len(title) < 3:
        return json.dumps({"status": "error", "message": "title mangler eller er for kort."})
    try:
        from app1 import user_profile_db as db
        db.ensure_tables()
        gid = db.add_learning_goal(username, title, args.get("description", ""), args.get("target_date"))
        return json.dumps({"status": "success", "section": "goals", "goal_id": gid,
                           "message": f"Udviklingsmål oprettet: {title}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Fejl ved oprettelse af mål: {e}"})


def _execute_get_learning_goals(args, username):
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    try:
        from app1 import user_profile_db as db
        db.ensure_tables()
        goals = db.get_learning_goals(username)
        label = {"aktiv": "aktiv", "fuldfoert": "fuldført", "paa_pause": "på pause"}
        items = [{"id": g["id"], "title": g["title"], "description": g.get("description") or "",
                  "target_date": g.get("target_date") or "", "status": label.get(g["status"], g["status"])}
                 for g in goals]
        return json.dumps({"status": "success", "section": "goals", "count": len(items), "goals": items,
                           "message": ("Ingen udviklingsmål oprettet endnu." if not items else f"{len(items)} udviklingsmål.")},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Fejl ved hentning af mål: {e}"})


def _execute_update_learning_goal(args, username):
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    try:
        gid = int(args.get("goal_id"))
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "goal_id mangler eller er ugyldigt."})
    try:
        from app1 import user_profile_db as db
        db.ensure_tables()
        if args.get("status") == "slet":
            ok = db.delete_learning_goal(username, gid)
            return json.dumps({"status": "success" if ok else "not_found", "section": "goals",
                               "message": "Mål slettet." if ok else "Målet blev ikke fundet."}, ensure_ascii=False)
        fields = {}
        if args.get("status") in ("aktiv", "fuldfoert", "paa_pause"):
            fields["status"] = args["status"]
        for k in ("title", "target_date", "description"):
            if args.get(k):
                fields[k] = str(args[k]).strip()
        ok = db.update_learning_goal(username, gid, **fields)
        return json.dumps({"status": "success" if ok else "not_found", "section": "goals",
                           "message": "Mål opdateret." if ok else "Målet blev ikke fundet eller ingen ændringer."},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Fejl ved opdatering af mål: {e}"})


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

        # Inject the computed skill gaps (company targets + target role + goals,
        # on the canonical 1-5 scale) so the path is sequenced to close REAL gaps,
        # not just the user's free-text goal.
        try:
            from competency import compute_skill_gaps
            _gaps = compute_skill_gaps(username, profile=profile)
        except Exception:
            _gaps = []
        if _gaps:
            gap_strs = [f"{g['skill']} ({g['current_label']}→{g['target_label']})" for g in _gaps[:6]]
            context_parts.append("Vigtigste kompetencegab (prioritér disse): " + ", ".join(gap_strs))
        _target_role = (profile.get("target_role") or "").strip()
        if _target_role:
            context_parts.append(f"Ønsket rolle: {_target_role}")

        if not context_parts:
            context_parts.append("Ingen profildata — foreslå en generel læringssti")
            if goal:
                context_parts.append(f"Mål: {goal}")

        context = "\n".join(context_parts)

        # Use fast model to plan the learning path (shared runtime retry/cooldown)
        from ai_runtime import run_direct_completion
        raw = run_direct_completion(
            [
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
- Maks 3 trin
- Hvert trin bygger logisk på det forrige
- "topic" skal være et konkret søgeord der kan bruges til at finde kurser
- Spring kompetencer over som brugeren allerede mestrer (avanceret/ekspert niveau)
- Vær specifik — "PRINCE2 Foundation" er bedre end "projektledelse"
- Skriv på dansk"""},
                {"role": "user", "content": context},
            ],
            max_tokens=500,
        )
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        path_plan = json.loads(raw)

        # Ground each step in REAL catalog courses, de-duplicating across steps
        # (a course surfaced in step 1 must not reappear in step 2) and skipping
        # courses the learner has already completed — so the path is actionable,
        # not a list of repeats or things they've done.
        completed_titles, completed_handles = _completed_course_keys(username)
        seen_handles = set()
        steps_with_courses = []
        total_cost = 0
        any_priced = False
        total_days = 0
        any_dated = False
        for step in path_plan.get("steps", [])[:3]:
            topic = step.get("topic", "")
            if not topic:
                continue
            results = semantic_search_courses(topic, limit=4,
                                               shown_handles=_current_shown_handles)
            courses = []
            if isinstance(results, list):
                for r in results:
                    h = (r.get("handle") or "").lower()
                    t = (r.get("title") or "").lower()
                    if h in seen_handles:
                        continue
                    if t in completed_titles or h in completed_handles:
                        continue
                    cf = _extract_compact_fields(r)
                    courses.append(cf)
                    seen_handles.add(h)
                    if len(courses) >= 2:
                        break

            # Cost/duration rollup uses the cheapest bookable course per step.
            if courses:
                step_prices = [_min_variant_price(r.get("variants")) for r in results
                               if (r.get("handle") or "").lower() in {(c.get("handle") or "").lower() for c in courses}]
                step_prices = [p for p in step_prices if isinstance(p, (int, float))]
                if step_prices:
                    total_cost += min(step_prices)
                    any_priced = True
                step_days = [c.get("duration_days") for c in courses if isinstance(c.get("duration_days"), (int, float))]
                if step_days:
                    total_days += min(step_days)
                    any_dated = True

            steps_with_courses.append({
                "order": step.get("order", len(steps_with_courses) + 1),
                "level": step.get("level", ""),
                "topic": topic,
                "reason": step.get("reason", ""),
                "courses": courses,
            })

        # A path with no grounded courses is not actionable — say so honestly.
        if not any(s["courses"] for s in steps_with_courses):
            return json.dumps({
                "status": "no_results",
                "message": "Jeg kunne ikke finde konkrete kurser til en sammenhængende læringssti lige nu. Prøv at præcisere målet.",
            }, ensure_ascii=False)

        path_title = path_plan.get("path_title", "Din læringssti")
        total_cost_out = round(total_cost) if any_priced else None
        total_days_out = round(total_days) if any_dated else None

        # Persist so a finished profile turns into durable, trackable value
        # (the path shows up on the profile page + can be re-opened from chat).
        path_id = None
        try:
            from app1.user_profile_db import save_learning_path
            path_id = save_learning_path(
                username, path_title, steps_with_courses,
                goal=(goal or focus_area or "")[:500], source="ai",
                total_cost=total_cost_out, total_duration_days=total_days_out,
            )
        except Exception as save_err:
            print(f"[suggest_learning_path] persist skipped: {save_err}")
            try:
                from flask import current_app as _app
                _app.mysql.connection.rollback()
            except Exception:
                pass

        return _model_tool_json(
            status="success",
            path_id=path_id,
            path_title=path_title,
            steps=steps_with_courses,
            total_cost=total_cost_out,
            total_duration_days=total_days_out,
            persisted=bool(path_id),
        )

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


# ── Course-journey tools (status, prerequisites, sequels, certification, goals, calendar) ──

# Ordering for "next difficulty" reasoning in sequels / certification paths.
_DIFFICULTY_ORDER = {"beginner": 0, "begynder": 0, "grundlæggende": 0,
                     "intermediate": 1, "mellem": 1, "mellemniveau": 1,
                     "advanced": 2, "avanceret": 2, "ekspert": 3}

# Danish affirmatives accepted as an explicit confirmation for mutations.
_DANISH_YES = {"ja", "ja tak", "jatak", "bekræft", "bekraeft", "ja bekræft",
               "godkend", "fuldfør", "fuldfoer", "marker", "marker som færdig",
               "gør det", "goer det", "ok", "okay", "yes", "confirm", "true"}


def _is_danish_yes(value):
    """True when the model passed an explicit Danish/known affirmative confirmation."""
    if value is True:
        return True
    if not value:
        return False
    return str(value).strip().lower().strip(".!") in _DANISH_YES


def _augmented_by_handle():
    """Map handle -> augmented product dict (guarded; empty on failure)."""
    out = {}
    try:
        for p in load_augmented_products() or []:
            h = p.get("handle")
            if h:
                out[h] = p
    except Exception:
        pass
    return out


def _find_augmented_product(handle="", title=""):
    """Resolve an augmented product by exact handle, then by case-insensitive title."""
    handle = (handle or "").strip()
    title = (title or "").strip()
    try:
        products = load_augmented_products() or []
    except Exception:
        products = []
    if handle:
        for p in products:
            if p.get("handle") == handle:
                return p
    if title:
        tl = title.lower()
        for p in products:
            if (p.get("title") or "").strip().lower() == tl:
                return p
        for p in products:
            if tl in (p.get("title") or "").strip().lower():
                return p
    return None


def _meta_topic_terms(meta, product):
    """Build a set of lowercase topic/cert/tag terms describing a course."""
    terms = set()
    if not isinstance(meta, dict):
        meta = {}
    if meta.get("primary_topic"):
        terms.add(str(meta["primary_topic"]).strip().lower())
    if meta.get("certification"):
        terms.add(str(meta["certification"]).strip().lower())
    tags = product.get("tags") if isinstance(product, dict) else None
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    for t in (tags or []):
        tl = str(t).strip().lower()
        if tl and tl not in _EXCLUDED_TAGS and not any(tl.startswith(p) for p in _EXCLUDED_TAG_PREFIXES):
            terms.add(tl)
    return {t for t in terms if t}


def _course_summary_line(product, meta):
    """Compact display dict for a single course in journey results."""
    meta = meta or {}
    variants = product.get("variants", []) if isinstance(product, dict) else []
    out = {
        "title": product.get("title"),
        "handle": product.get("handle"),
        "vendor": product.get("vendor"),
        "price": _normalize_price(variants[0].get("price") if variants else None),
    }
    if meta.get("difficulty"):
        out["difficulty"] = meta["difficulty"]
    if meta.get("certification"):
        out["certification"] = meta["certification"]
    if meta.get("duration_days"):
        out["duration_days"] = meta["duration_days"]
    return out


def _execute_get_my_course_status(args, username):
    """Tool 1: The logged-in user's course_orders with days_left + overdue flag.

    Scoped to the session user (username) and company. Read-only.
    """
    from flask import session as flask_session, current_app as app, has_request_context
    if not username:
        return json.dumps({"status": "error", "message": "Du skal være logget ind for at se dine kurser."}, ensure_ascii=False)
    if not has_request_context():
        return json.dumps({"status": "error", "message": "Kan ikke hente kursusstatus uden for en session."}, ensure_ascii=False)

    company_id = flask_session.get("company_id")
    user_id = flask_session.get("user_id")
    try:
        import db_compat  # noqa: F401
        from db_compat import refresh_flask_mysql_connection
        import MySQLdb.cursors
        try:
            refresh_flask_mysql_connection(app.mysql)
        except Exception:
            pass
        cur = app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        # Scope to the session user; prefer user_id when present, else username.
        where = "username = %s"
        params = [username]
        if user_id:
            where = "(user_id = %s OR username = %s)"
            params = [user_id, username]
        if company_id:
            where += " AND company_id = %s"
            params.append(company_id)
        cur.execute(
            f"""
            SELECT order_id, product_handle, product_title, status, completion_status,
                   price, started_at, completion_deadline, completion_date
            FROM course_orders
            WHERE {where}
            ORDER BY (completion_deadline IS NULL), completion_deadline ASC, created_at DESC
            LIMIT 50
            """,
            tuple(params),
        )
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        print(f"[get_my_course_status] DB error: {e}")
        return json.dumps({"status": "error", "message": "Kunne ikke hente dine kurser lige nu."}, ensure_ascii=False)

    today = datetime.date.today()
    courses = []
    overdue_count = 0
    for r in rows:
        completion_status = (r.get("completion_status") or "").lower()
        is_done = completion_status == "completed"
        deadline = r.get("completion_deadline")
        days_left = None
        overdue = False
        if deadline and not is_done:
            try:
                d = deadline.date() if hasattr(deadline, "date") else datetime.date.fromisoformat(str(deadline)[:10])
                days_left = (d - today).days
                overdue = days_left < 0
            except Exception:
                days_left = None
        if overdue:
            overdue_count += 1
        courses.append({
            "order_id": r.get("order_id"),
            "product_handle": r.get("product_handle"),
            "product_title": r.get("product_title"),
            "status": r.get("status"),
            "completion_status": r.get("completion_status") or "ikke_startet",
            "price": _normalize_price(r.get("price")),
            "started_at": str(r.get("started_at"))[:10] if r.get("started_at") else None,
            "completion_deadline": str(deadline)[:10] if deadline else None,
            "completion_date": str(r.get("completion_date"))[:10] if r.get("completion_date") else None,
            "days_left": days_left,
            "overdue": overdue,
            "completed": is_done,
        })

    active = [c for c in courses if not c["completed"]]
    if not courses:
        message = "Du har ingen kurser registreret endnu."
    elif overdue_count:
        message = f"Du har {overdue_count} forsinket kursus/kurser ud af {len(active)} aktive."
    elif active:
        message = f"Du har {len(active)} aktive kursus/kurser - ingen er forsinket."
    else:
        message = "Alle dine kurser er gennemført."

    return json.dumps({
        "status": "success",
        "count": len(courses),
        "active_count": len(active),
        "overdue_count": overdue_count,
        "courses": courses,
        "message": message,
    }, ensure_ascii=False, default=str)


def _execute_get_negotiated_discount(args, username):
    """Tool 2: The user's COMPANY active negotiated discount on a course.

    Scoped to the session company_id. Read-only.
    """
    from flask import session as flask_session, current_app as app, has_request_context
    if not has_request_context():
        return json.dumps({"status": "error", "message": "Kan ikke slå rabat op uden for en session."}, ensure_ascii=False)
    company_id = flask_session.get("company_id")
    if not company_id:
        return json.dumps({"status": "error", "message": "Aftalepriser er kun for virksomhedsbrugere."}, ensure_ascii=False)

    handle = (args.get("product_handle") or "").strip()
    vendor = (args.get("vendor") or "").strip()

    product = _find_augmented_product(handle=handle) if handle else None
    if product and not vendor:
        vendor = product.get("vendor") or ""
    variants = product.get("variants", []) if product else []
    original_price_raw = variants[0].get("price") if variants else None

    if not vendor:
        return json.dumps({"status": "needs_info", "message": "Angiv kurset eller udbyderen, så jeg kan slå jeres aftalepris op."}, ensure_ascii=False)

    try:
        import db_compat  # noqa: F401
        from db_compat import refresh_flask_mysql_connection
        import MySQLdb.cursors
        try:
            refresh_flask_mysql_connection(app.mysql)
        except Exception:
            pass
        cur = app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT vendor_name, discount_type, discount_value, agreement_name,
                   valid_from, valid_until, min_participants
            FROM company_supplier_agreements
            WHERE company_id = %s AND vendor_name = %s AND is_active = 1
              AND (valid_from IS NULL OR valid_from <= CURDATE())
              AND (valid_until IS NULL OR valid_until >= CURDATE())
            LIMIT 1
            """,
            (company_id, vendor),
        )
        row = cur.fetchone()
        cur.close()
    except Exception as e:
        print(f"[get_negotiated_discount] DB error: {e}")
        return json.dumps({"status": "error", "message": "Kunne ikke slå jeres aftalepris op lige nu."}, ensure_ascii=False)

    if not row:
        return json.dumps({
            "status": "no_agreement",
            "vendor": vendor,
            "message": f"Jeres virksomhed har ingen aktiv aftalepris hos {vendor}.",
        }, ensure_ascii=False)

    discounted, original, agreement_name = (None, None, row.get("agreement_name", ""))
    try:
        if original_price_raw is not None:
            o = float(original_price_raw)
            if o > 0:
                dtype = row.get("discount_type", "percentage")
                dvalue = float(row.get("discount_value", 0) or 0)
                if dtype == "percentage":
                    discounted = round(o * (1 - dvalue / 100), 2)
                elif dtype == "fixed_amount":
                    discounted = round(max(0, o - dvalue), 2)
                elif dtype == "fixed_price":
                    discounted = round(dvalue, 2)
                original = o
    except (ValueError, TypeError):
        discounted = None

    result = {
        "status": "success",
        "vendor": vendor,
        "agreement_name": agreement_name,
        "discount_type": row.get("discount_type"),
        "discount_value": float(row.get("discount_value", 0) or 0),
        "valid_until": str(row.get("valid_until"))[:10] if row.get("valid_until") else None,
        "min_participants": row.get("min_participants"),
    }
    if product:
        result["product_title"] = product.get("title")
        result["product_handle"] = product.get("handle")
    if discounted is not None and original is not None:
        savings = round(original - discounted, 2)
        result.update({
            "original_price": _normalize_price(original),
            "final_price": _normalize_price(discounted),
            "savings": _normalize_price(savings),
            "discount": _normalize_price(savings),
            "message": f"Med jeres aftale hos {vendor} betaler I {_normalize_price(discounted)} (sparer {_normalize_price(savings)}).",
        })
    else:
        dv = result["discount_value"]
        dt = result["discount_type"]
        human = f"{dv:.0f}%" if dt == "percentage" else _normalize_price(dv)
        result["message"] = f"Jeres aftale hos {vendor} giver {human} rabat. Angiv kurset for at se den endelige pris."
    return json.dumps(result, ensure_ascii=False, default=str)


def _execute_check_course_prerequisites(args):
    """Tool 3: Prerequisites + difficulty/duration/cert/audience from the catalog."""
    handle = (args.get("handle") or "").strip()
    if not handle:
        return json.dumps({"status": "needs_info", "message": "Angiv kursets handle."}, ensure_ascii=False)
    product = _find_augmented_product(handle=handle, title=args.get("title", ""))
    if not product:
        return json.dumps({"status": "not_found", "message": f"Kunne ikke finde kursus '{handle}'."}, ensure_ascii=False)
    meta = product.get("structured_metadata", {}) or {}
    prerequisites = meta.get("prerequisites") or []
    if isinstance(prerequisites, str):
        prerequisites = [prerequisites]
    target_audience = meta.get("target_audience") or []
    if isinstance(target_audience, str):
        target_audience = [target_audience]
    has_prereq = bool(prerequisites)
    return json.dumps({
        "status": "success",
        "title": product.get("title"),
        "handle": product.get("handle"),
        "vendor": product.get("vendor"),
        "prerequisites": prerequisites,
        "has_prerequisites": has_prereq,
        "difficulty": meta.get("difficulty") or "",
        "duration_days": meta.get("duration_days"),
        "certification": meta.get("certification") or "",
        "target_audience": target_audience,
        "message": (
            f"{product.get('title')} kræver: " + "; ".join(prerequisites)
            if has_prereq else
            f"{product.get('title')} har ingen formelle forudsætninger."
        ),
    }, ensure_ascii=False, default=str)


def _execute_get_course_sequel(args):
    """Tool 4: Suggest natural NEXT courses after a given course.

    Matches catalog courses whose topic/cert overlaps this course AND that are at
    the same-or-next difficulty level. Read-only.
    """
    product = _find_augmented_product(handle=args.get("handle", ""), title=args.get("title", ""))
    if not product:
        return json.dumps({"status": "not_found", "message": "Kunne ikke finde det kursus, du vil bygge videre på."}, ensure_ascii=False)
    meta = product.get("structured_metadata", {}) or {}
    src_terms = _meta_topic_terms(meta, product)
    src_diff = _DIFFICULTY_ORDER.get((meta.get("difficulty") or "").lower(), 1)
    src_handle = product.get("handle")
    src_title_l = (product.get("title") or "").strip().lower()

    try:
        candidates = load_augmented_products() or []
    except Exception:
        candidates = []

    scored = []
    for p in candidates:
        if p.get("handle") == src_handle:
            continue
        if (p.get("title") or "").strip().lower() == src_title_l:
            continue
        pm = p.get("structured_metadata", {}) or {}
        p_terms = _meta_topic_terms(pm, p)
        overlap = src_terms & p_terms
        if not overlap:
            continue
        p_diff = _DIFFICULTY_ORDER.get((pm.get("difficulty") or "").lower(), 1)
        # Natural next: same level or one step up. Skip easier courses.
        if p_diff < src_diff:
            continue
        score = len(overlap) * 10
        if p_diff == src_diff + 1:
            score += 5  # prefer the genuine "next step up"
        elif p_diff == src_diff:
            score += 2
        scored.append((score, p_diff, p, pm))

    scored.sort(key=lambda t: (t[0], -t[1]), reverse=True)
    sequels = [_course_summary_line(p, pm) for _, _, p, pm in scored[:5]]

    if not sequels:
        return json.dumps({
            "status": "no_results",
            "title": product.get("title"),
            "message": f"Jeg fandt ikke et oplagt næste kursus efter {product.get('title')}.",
        }, ensure_ascii=False, default=str)

    return _model_tool_json(
        status="success",
        after_course=product.get("title"),
        after_handle=src_handle,
        count=len(sequels),
        sequels=sequels,
        message=f"Naturlige næste skridt efter {product.get('title')}.",
    )


def _execute_find_certification_path(args):
    """Tool 5: Ordered course list to reach a target certification.

    Matches catalog courses whose structured_metadata.certification or tags mention
    the target cert, ordered by difficulty (foundation -> advanced). Read-only.
    """
    cert = (args.get("certification") or "").strip()
    if not cert:
        return json.dumps({"status": "needs_info", "message": "Hvilken certificering vil du opnå (fx PMP, ITIL, PRINCE2)?"}, ensure_ascii=False)
    cert_l = cert.lower()
    try:
        products = load_augmented_products() or []
    except Exception:
        products = []

    matches = []
    for p in products:
        pm = p.get("structured_metadata", {}) or {}
        cert_field = (pm.get("certification") or "").lower()
        primary_topic = (pm.get("primary_topic") or "").lower()
        title_l = (p.get("title") or "").lower()
        tags = p.get("tags")
        if isinstance(tags, str):
            tags = [t.strip().lower() for t in tags.split(",")]
        else:
            tags = [str(t).strip().lower() for t in (tags or [])]
        hit = (
            cert_l in cert_field
            or cert_l in primary_topic
            or cert_l in title_l
            or any(cert_l in t for t in tags)
        )
        if not hit:
            continue
        diff = _DIFFICULTY_ORDER.get((pm.get("difficulty") or "").lower(), 1)
        matches.append((diff, p, pm))

    if not matches:
        return json.dumps({
            "status": "no_results",
            "certification": cert,
            "message": f"Jeg fandt ingen kurser i kataloget der fører mod '{cert}'.",
        }, ensure_ascii=False, default=str)

    # De-duplicate by handle, keep difficulty order then a stable title sort.
    seen = set()
    matches.sort(key=lambda t: (t[0], (t[1].get("title") or "")))
    path = []
    for diff, p, pm in matches:
        h = p.get("handle")
        if h in seen:
            continue
        seen.add(h)
        step = _course_summary_line(p, pm)
        step["step"] = len(path) + 1
        path.append(step)

    return _model_tool_json(
        status="success",
        certification=cert,
        count=len(path),
        path=path[:8],
        message=f"Forslag til vej mod {cert} ({len(path[:8])} trin).",
    )


def _execute_track_goal_progress(args, username):
    """Tool 6: Progress toward the user's learning goals vs completed courses.

    Scoped to the session user. Read-only.
    """
    if not username:
        return json.dumps({"status": "error", "message": "Du skal være logget ind for at se dine mål."}, ensure_ascii=False)
    try:
        from app1 import user_profile_db as db
        db.ensure_tables()
        goals = db.get_learning_goals(username) or []
        completed = db.get_completed_courses(username) or []
    except Exception as e:
        print(f"[track_goal_progress] DB error: {e}")
        return json.dumps({"status": "error", "message": "Kunne ikke hente dine mål lige nu."}, ensure_ascii=False)

    completed_titles = [(c.get("course_title") or "").strip() for c in completed if c.get("course_title")]
    completed_lc = [t.lower() for t in completed_titles]
    status_label = {"aktiv": "aktiv", "fuldfoert": "fuldført", "paa_pause": "på pause"}

    goal_rows = []
    for g in goals:
        title = (g.get("title") or "").strip()
        desc = (g.get("description") or "").strip()
        # Heuristic match: a completed course supports a goal when a meaningful word
        # from the goal title appears in the course title (or vice versa).
        keywords = [w for w in re.split(r"[\s,/]+", (title + " " + desc).lower()) if len(w) >= 4]
        matched = []
        for ct, ctl in zip(completed_titles, completed_lc):
            if any(kw in ctl for kw in keywords) or any(ctl_word in title.lower() for ctl_word in ctl.split() if len(ctl_word) >= 4):
                matched.append(ct)
        matched = list(dict.fromkeys(matched))
        is_done = (g.get("status") == "fuldfoert")
        if is_done:
            pct = 100
        elif matched:
            pct = min(90, 30 * len(matched))  # progress signalled, not "done" until marked
        else:
            pct = 0
        goal_rows.append({
            "goal_id": g.get("id"),
            "title": title,
            "status": status_label.get(g.get("status"), g.get("status")),
            "target_date": g.get("target_date") or "",
            "matched_courses": matched,
            "matched_count": len(matched),
            "progress_pct": pct,
            "remaining_hint": (
                "Måske gennemført - marker som færdig eller find næste kursus." if matched and not is_done
                else ("Opnået." if is_done else "Ingen relevante kurser gennemført endnu.")
            ),
        })

    if not goal_rows:
        message = "Du har ingen udviklingsmål endnu. Opret et mål, så kan jeg følge din fremgang."
    else:
        done = sum(1 for r in goal_rows if r["progress_pct"] >= 100)
        message = f"Du har {len(goal_rows)} mål - {done} er nået, og du har gennemført {len(completed_titles)} kurser."

    return json.dumps({
        "status": "success",
        "goal_count": len(goal_rows),
        "completed_course_count": len(completed_titles),
        "completed_courses": completed_titles[:20],
        "goals": goal_rows,
        "message": message,
    }, ensure_ascii=False, default=str)


def _execute_add_to_calendar(args, username):
    """Tool 7: Build an .ics entry for a booked course. Read-only (just generates).

    Resolves a booked course either from a course_orders row (order_id, session user)
    or from explicit handle+date+location args, then builds an .ics via the guarded
    calendar_service module. Falls back to plain event fields if the module is absent.
    """
    from flask import session as flask_session, current_app as app, has_request_context

    title = ""
    date_str = (args.get("date") or "").strip()
    location = (args.get("location") or "").strip()
    handle = (args.get("handle") or "").strip()
    order_id = (args.get("order_id") or "").strip()
    url = ""

    # Path A: resolve from the user's own order row.
    if order_id and has_request_context() and username:
        try:
            import db_compat  # noqa: F401
            from db_compat import refresh_flask_mysql_connection
            import MySQLdb.cursors
            try:
                refresh_flask_mysql_connection(app.mysql)
            except Exception:
                pass
            company_id = flask_session.get("company_id")
            user_id = flask_session.get("user_id")
            cur = app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            where = "order_id = %s AND (username = %s"
            params = [order_id, username]
            if user_id:
                where += " OR user_id = %s"
                params.append(user_id)
            where += ")"
            if company_id:
                where += " AND company_id = %s"
                params.append(company_id)
            cur.execute(
                f"""
                SELECT product_handle, product_title, variant_date, variant_location
                FROM course_orders WHERE {where} LIMIT 1
                """,
                tuple(params),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                title = row.get("product_title") or title
                handle = handle or (row.get("product_handle") or "")
                date_str = date_str or (row.get("variant_date") or "")
                location = location or (row.get("variant_location") or "")
        except Exception as e:
            print(f"[add_to_calendar] DB lookup error: {e}")

    # Path B / enrich: resolve title + url from the catalog by handle.
    if handle:
        product = _find_augmented_product(handle=handle)
        if product:
            title = title or product.get("title") or ""
            url = f"/products/{handle}"
            if not date_str:
                # TL-02: foreslå kun en kommende dato — aldrig en udløbet
                upcoming, _ = _upcoming_dates(product.get("variants", []))
                date_str = (upcoming[0] if upcoming else "") or ""
            if not location:
                variants = product.get("variants", [])
                location = next((v.get("option1") for v in variants if v.get("option1")), "") or ""

    if not title:
        title = (args.get("title") or "").strip()
    if not title:
        return json.dumps({"status": "needs_info", "message": "Angiv et kursus (order_id eller handle) jeg kan lægge i kalenderen."}, ensure_ascii=False)
    if not date_str:
        return json.dumps({"status": "needs_info", "message": "Jeg mangler en dato for at kunne lave kalenderaftalen."}, ensure_ascii=False)

    event = {
        "title": title,
        "date": date_str,
        "location": location,
        "url": url,
    }

    # Guarded import: calendar_service is built by the cross-channel agent.
    ics = ""
    try:
        import calendar_service  # noqa: F401
        ics = calendar_service.build_ics(
            title=title,
            start=date_str,
            location=location,
            description=f"Tilmeldt kursus: {title}",
            url=url,
        ) or ""
    except Exception as e:
        print(f"[add_to_calendar] calendar_service unavailable: {e}")
        ics = ""

    if ics:
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", title)[:40] or "kursus"
        return json.dumps({
            "status": "success",
            "has_ics": True,
            "ics": ics,
            "filename": f"{safe_name}.ics",
            "event": event,
            "message": f"Kalenderaftale klar for {title} ({date_str}). Du kan downloade .ics-filen.",
        }, ensure_ascii=False, default=str)

    # Fallback: no ics module / bad input -> return event fields as text.
    return json.dumps({
        "status": "success",
        "has_ics": False,
        "event": event,
        "message": (
            f"Kursus: {title}\nDato: {date_str}"
            + (f"\nSted: {location}" if location else "")
            + "\n(Kalenderfil kunne ikke genereres - her er detaljerne i stedet.)"
        ),
    }, ensure_ascii=False, default=str)


def _execute_mark_course_complete(args, username):
    """Tool 8 (MUTATION): Mark a booked course as completed for the session user.

    Requires an explicit Danish confirmation. Updates course_orders.completion_status
    + completion_date, adds the course to user_completed_courses, commits once, then
    suggests a next course. Scoped to the session user.
    """
    from flask import session as flask_session, current_app as app, has_request_context
    if not username:
        return json.dumps({"status": "error", "message": "Du skal være logget ind for at markere et kursus som gennemført."}, ensure_ascii=False)
    if not has_request_context():
        return json.dumps({"status": "error", "message": "Kan ikke opdatere kursus uden for en session."}, ensure_ascii=False)

    if not _is_danish_yes(args.get("confirm")):
        return json.dumps({
            "status": "needs_confirmation",
            "creates_change": True,
            "message": "Bekræft venligst med et tydeligt 'ja', før jeg markerer kurset som gennemført.",
        }, ensure_ascii=False)

    handle = (args.get("handle") or "").strip()
    order_id = (args.get("order_id") or "").strip()
    if not handle and not order_id:
        return json.dumps({"status": "needs_info", "message": "Angiv hvilket kursus (handle eller order_id) du har gennemført."}, ensure_ascii=False)

    company_id = flask_session.get("company_id")
    user_id = flask_session.get("user_id")

    try:
        import db_compat  # noqa: F401
        from db_compat import refresh_flask_mysql_connection
        import MySQLdb.cursors
        try:
            refresh_flask_mysql_connection(app.mysql)
        except Exception:
            pass
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)

        # Locate the user's own matching order (never cross-user/cross-tenant).
        where = "(username = %s"
        params = [username]
        if user_id:
            where += " OR user_id = %s"
            params.append(user_id)
        where += ")"
        if company_id:
            where += " AND company_id = %s"
            params.append(company_id)
        if order_id:
            where += " AND order_id = %s"
            params.append(order_id)
        else:
            where += " AND product_handle = %s"
            params.append(handle)
        cur.execute(
            f"""
            SELECT order_id, product_handle, product_title, completion_status
            FROM course_orders WHERE {where}
            ORDER BY created_at DESC LIMIT 1
            """,
            tuple(params),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return json.dumps({"status": "not_found", "message": "Jeg fandt ikke et tilmeldt kursus, der matcher det."}, ensure_ascii=False)

        if (row.get("completion_status") or "").lower() == "completed":
            cur.close()
            return json.dumps({
                "status": "already_completed",
                "product_title": row.get("product_title"),
                "message": f"{row.get('product_title')} er allerede markeret som gennemført.",
            }, ensure_ascii=False)

        cur.execute(
            """
            UPDATE course_orders
            SET completion_status = 'completed', completion_date = NOW()
            WHERE order_id = %s
            """,
            (row.get("order_id"),),
        )
        cur.close()
        conn.commit()
    except Exception as e:
        print(f"[mark_course_complete] DB error: {e}")
        try:
            app.mysql.connection.rollback()
        except Exception:
            pass
        return json.dumps({"status": "error", "message": "Kunne ikke markere kurset som gennemført lige nu."}, ensure_ascii=False)

    course_title = row.get("product_title") or ""
    course_handle = row.get("product_handle") or handle

    # Add to the user's completed-courses profile (best-effort; don't fail the mutation).
    product = _find_augmented_product(handle=course_handle, title=course_title)
    vendor = product.get("vendor", "") if product else ""
    try:
        from app1 import user_profile_db as db
        db.ensure_tables()
        if course_title:
            db.add_completed_course(
                username, course_title, course_handle=course_handle,
                vendor=vendor, completed_date=datetime.date.today().isoformat(),
            )
    except Exception as e:
        print(f"[mark_course_complete] profile add warning: {e}")

    # Suggest a natural next course.
    next_course = None
    if product:
        try:
            meta = product.get("structured_metadata", {}) or {}
            src_terms = _meta_topic_terms(meta, product)
            src_diff = _DIFFICULTY_ORDER.get((meta.get("difficulty") or "").lower(), 1)
            best = None
            best_score = 0
            for p in (load_augmented_products() or []):
                if p.get("handle") == course_handle:
                    continue
                pm = p.get("structured_metadata", {}) or {}
                overlap = src_terms & _meta_topic_terms(pm, p)
                if not overlap:
                    continue
                p_diff = _DIFFICULTY_ORDER.get((pm.get("difficulty") or "").lower(), 1)
                if p_diff < src_diff:
                    continue
                score = len(overlap) * 10 + (5 if p_diff == src_diff + 1 else 0)
                if score > best_score:
                    best_score, best = score, (p, pm)
            if best:
                next_course = _course_summary_line(best[0], best[1])
        except Exception:
            next_course = None

    message = f"Godt gået! {course_title} er nu markeret som gennemført."
    if next_course:
        message += f" Et oplagt næste skridt kunne være {next_course['title']}."

    result = {
        "status": "success",
        "completed": True,
        "order_id": row.get("order_id"),
        "product_title": course_title,
        "product_handle": course_handle,
        "message": message,
    }
    if next_course:
        result["suggested_next"] = next_course
    return json.dumps(result, ensure_ascii=False, default=str)


def _execute_create_order(args):
    """Create a course order from chatbot conversation."""
    from flask import session as flask_session
    from app1.rag import load_augmented_products
    from app1.order_handler import order_handler, store_user_info_for_order

    handle = args.get("product_handle", "")
    user_name = args.get("user_name", "").strip()
    user_email = args.get("user_email", "").strip()
    user_phone = args.get("user_phone", "").strip()

    if not handle:
        return json.dumps({"status": "error", "message": "product_handle mangler."})
    if not user_name or not user_email:
        return json.dumps({"status": "error", "message": "Navn og email er påkrævet."})

    # Look up product
    products = load_augmented_products()
    product = None
    for p in products:
        if p.get("handle") == handle:
            product = p
            break
    if not product:
        return json.dumps({"status": "error", "message": f"Kursus '{handle}' ikke fundet."})

    # Build product_data
    variants = product.get("variants", [])
    price_str = variants[0].get("price", "0") if variants else "0"
    vendor_name = product.get("vendor", "")

    # Apply the negotiated supplier discount AT CAPTURE so the order is charged
    # at the agreed price, not the list price. Reuse the existing per-request
    # discount logic; fall back to the company discount map when the in-memory
    # search context was not populated (e.g. a non-chat-loop entry point).
    discounted_price, _orig, _agr_name = apply_discount(price_str, vendor_name)
    if discounted_price is None:
        try:
            company_id = flask_session.get("company_id")
            if company_id and vendor_name:
                discount_map = catalog.get_company_discount_map(company_id)
                agreement = discount_map.get((vendor_name or "").lower())
                if agreement:
                    eff = catalog.apply_discount_to_price(price_str, agreement)
                    if eff is not None:
                        discounted_price = eff
        except Exception:
            discounted_price = None
    if discounted_price is not None:
        price_str = str(discounted_price)

    product_data = {
        "handle": handle,
        "title": product.get("title", ""),
        "price": price_str,
        "vendor": vendor_name,
        "product_type": product.get("product_type", ""),
    }

    # Store user info in session
    user_info = {"name": user_name, "email": user_email, "phone": user_phone}
    store_user_info_for_order(user_info)

    # Variant selection
    variant_selection = {}
    if args.get("variant_date"):
        variant_selection["date"] = args["variant_date"]
    if args.get("variant_location"):
        variant_selection["location"] = args["variant_location"]

    # Create order
    from app1.order_handler import create_order_from_chatbot
    result = create_order_from_chatbot(product_data, variant_selection or None)

    if result.get("success"):
        return json.dumps({
            "status": "order_created",
            "order_id": result.get("order_id", ""),
            "message": result.get("message", "Ordre oprettet!"),
        })
    else:
        return json.dumps({
            "status": "error",
            "action": result.get("action", ""),
            "message": result.get("message", "Ordre kunne ikke oprettes."),
            "errors": result.get("errors", []),
        })


def _execute_analyze_skill_gaps(args):
    """Analyze skill gaps for user's department or company."""
    from flask import session as flask_session, current_app
    import MySQLdb.cursors

    company_id = flask_session.get('company_id')
    if not company_id:
        return json.dumps({"status": "error", "message": "Denne funktion er kun for virksomhedsbrugere."})

    department = args.get("department", "").strip() or flask_session.get('company_department', '')

    conn = current_app.mysql.connection
    cur = conn.cursor(MySQLdb.cursors.DictCursor)

    # Get skill targets
    if department:
        cur.execute("""
            SELECT skill_name, target_level, priority
            FROM company_skill_targets
            WHERE company_id = %s AND (department = %s OR department IS NULL OR department = '')
            ORDER BY priority DESC, skill_name
        """, (company_id, department))
    else:
        cur.execute("""
            SELECT skill_name, target_level, priority, department
            FROM company_skill_targets WHERE company_id = %s
            ORDER BY priority DESC, skill_name
        """, (company_id,))
    targets = cur.fetchall()

    if not targets:
        cur.close()
        return json.dumps({
            "status": "ok",
            "message": "Der er ingen kompetencemaal defineret for din virksomhed endnu. "
                       "Bed din HR-afdeling om at opsaette kompetencemaal.",
            "gaps": []
        })

    # Get user's own skills
    user_id = flask_session.get('user_id')
    cur.execute("""
        SELECT skill_name, current_level FROM employee_skills_matrix
        WHERE employee_id = %s AND company_id = %s
    """, (user_id, company_id))
    user_skills = {r['skill_name']: r['current_level'] for r in cur.fetchall()}
    cur.close()

    level_labels = {0: 'Ingen', 1: 'Begynder', 2: 'Grundlaeggende', 3: 'Kompetent', 4: 'Avanceret', 5: 'Ekspert'}
    gaps = []
    for t in targets:
        current = user_skills.get(t['skill_name'], 0)
        target = t['target_level']
        gap = target - current
        gaps.append({
            "skill": t['skill_name'],
            "current_level": current,
            "current_label": level_labels.get(current, str(current)),
            "target_level": target,
            "target_label": level_labels.get(target, str(target)),
            "gap": gap,
            "status": "ok" if gap <= 0 else ("warning" if gap == 1 else "critical"),
            "priority": t.get('priority', 'medium'),
        })

    gaps.sort(key=lambda g: g['gap'], reverse=True)

    critical = [g for g in gaps if g['status'] == 'critical']
    summary = f"Du har {len(critical)} kritiske kompetencegab." if critical else "Du opfylder de fleste kompetencemaal."

    return json.dumps({
        "status": "ok",
        "department": department or "Alle",
        "summary": summary,
        "gaps": gaps[:10],
    })


def _execute_check_approval_status(args):
    """Check approval status for an order or list pending approvals."""
    from flask import session as flask_session, current_app
    import MySQLdb.cursors

    user_id = flask_session.get('user_id')
    company_id = flask_session.get('company_id')
    if not company_id:
        return json.dumps({"status": "error", "message": "Denne funktion er kun for virksomhedsbrugere."})

    order_id_prefix = args.get("order_id", "").strip()
    conn = current_app.mysql.connection
    cur = conn.cursor(MySQLdb.cursors.DictCursor)

    if order_id_prefix:
        # Look up specific order
        cur.execute("""
            SELECT oa.status, oa.notes, oa.requested_at, oa.decided_at,
                   co.product_title, co.price, co.order_id
            FROM order_approvals oa
            JOIN course_orders co ON oa.order_id = co.order_id
            WHERE oa.company_id = %s AND co.order_id LIKE %s
            ORDER BY oa.requested_at DESC LIMIT 1
        """, (company_id, f"{order_id_prefix}%"))
        row = cur.fetchone()
        cur.close()
        if not row:
            return json.dumps({"status": "not_found", "message": f"Ingen godkendelsesanmodning fundet for ordre {order_id_prefix}."})
        status_map = {'pending': 'Afventer godkendelse', 'approved': 'Godkendt', 'rejected': 'Afvist'}
        return json.dumps({
            "status": "ok",
            "approval_status": row['status'],
            "approval_status_text": status_map.get(row['status'], row['status']),
            "course": row['product_title'],
            "price": str(row['price']),
            "requested_at": str(row['requested_at']),
            "decided_at": str(row['decided_at']) if row['decided_at'] else None,
            "notes": row['notes'],
        })
    else:
        # List all pending for this user
        cur.execute("""
            SELECT oa.status, oa.requested_at, co.product_title, co.price, co.order_id
            FROM order_approvals oa
            JOIN course_orders co ON oa.order_id = co.order_id
            WHERE oa.company_id = %s AND oa.requester_user_id = %s AND oa.status = 'pending'
            ORDER BY oa.requested_at DESC LIMIT 10
        """, (company_id, user_id))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return json.dumps({"status": "ok", "message": "Du har ingen afventende godkendelser.", "pending": []})
        pending = []
        for r in rows:
            pending.append({
                "order_id_short": r['order_id'][:8],
                "course": r['product_title'],
                "price": str(r['price']),
                "requested_at": str(r['requested_at']),
            })
        return json.dumps({"status": "ok", "pending_count": len(pending), "pending": pending})


def _execute_get_department_budget(args):
    """Get department training budget and spending."""
    from flask import session as flask_session, current_app
    import MySQLdb.cursors

    company_id = flask_session.get('company_id')
    if not company_id:
        return json.dumps({"status": "error", "message": "Denne funktion er kun for virksomhedsbrugere."})

    department = args.get("department", "").strip() or flask_session.get('company_department', '')
    if not department:
        return json.dumps({"status": "error", "message": "Ingen afdeling angivet og din afdeling er ukendt."})

    fiscal_year = datetime.datetime.now().year
    conn = current_app.mysql.connection
    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT annual_budget, spent, fiscal_year FROM department_budgets
        WHERE company_id = %s AND department = %s AND fiscal_year = %s
    """, (company_id, department, fiscal_year))
    row = cur.fetchone()
    cur.close()

    if not row:
        return json.dumps({
            "status": "ok",
            "message": f"Intet budget fundet for afdeling '{department}' i {fiscal_year}.",
            "has_budget": False,
        })

    budget = float(row['annual_budget'] or 0)
    spent = float(row['spent'] or 0)
    remaining = budget - spent
    utilization = round((spent / budget * 100), 1) if budget > 0 else 0

    return json.dumps({
        "status": "ok",
        "has_budget": True,
        "department": department,
        "fiscal_year": fiscal_year,
        "annual_budget": f"{budget:,.0f} kr",
        "spent": f"{spent:,.0f} kr",
        "remaining": f"{remaining:,.0f} kr",
        "utilization_pct": utilization,
        "warning": "Budget er over 80% brugt!" if utilization > 80 else None,
    })


# ── AI Tooler 2 (Phase 7): employee-facing action tools ───────────────────────

def _execute_save_course_for_later(args, username):
    """Save a course to the user's wishlist (immediate, low-stakes, reviewable).

    Backed by the same memory store as remember_about_user (category 'wishlist'), so
    the user can review/delete it from the Mind-Map / memory drill-down. Returns a
    'memory_saved' chip rather than a confirm card.
    """
    if not username:
        return json.dumps({"status": "error", "message": "Du skal være logget ind for at gemme kurser."})
    title = (args.get("product_title") or "").strip()
    handle = (args.get("product_handle") or "").strip() or None
    if not title:
        return json.dumps({"status": "error", "message": "Angiv et kursusnavn (product_title)."})
    try:
        from app1 import user_profile_db as db
        db.ensure_tables()
        db.add_memory(username, f"Vil gerne tage: {title}", category="wishlist",
                      detail=handle, source="ai", confidence=0.9)
        return json.dumps({"status": "memory_saved", "label": title[:200], "category": "wishlist",
                           "message": f"Gemt til senere: {title[:200]}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Kunne ikke gemme kurset: {e}"})


def _execute_set_course_reminder(args, username):
    """Set a personal reminder for a course (immediate, self-scoped, reviewable).

    Stored in the user's memory store (category 'reminder'); low-stakes and reversible,
    so it saves directly like remember_about_user.
    """
    if not username:
        return json.dumps({"status": "error", "message": "Du skal være logget ind for at sætte påmindelser."})
    title = (args.get("product_title") or "").strip()
    when = (args.get("remind_on") or "").strip()
    if not title or not when:
        return json.dumps({"status": "error", "message": "Angiv både et kursus og en dato (remind_on)."})
    try:
        from app1 import user_profile_db as db
        db.ensure_tables()
        db.add_memory(username, f"Påmindelse: {title} ({when})", category="reminder",
                      detail=(args.get("product_handle") or "").strip() or None,
                      source="ai", confidence=0.9)
        return json.dumps({"status": "memory_saved", "label": f"{title} – {when}", "category": "reminder",
                           "message": f"Påmindelse sat for {title[:160]} den {when}."})
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Kunne ikke sætte påmindelsen: {e}"})


def _execute_manage_my_order(args, username):
    """MUTATION — cancel the logged-in user's OWN pending order. Confirm-gated.

    Strictly self-scoped: order_service.cancel_order enforces owner-or-same-company-
    manager ownership, so a foreign order_id behaves like not-found. Without confirm
    a preview is returned.
    """
    from tool_confirm import needs_confirmation_payload
    if not username:
        return json.dumps({"status": "error", "message": "Du skal være logget ind."})
    action = (args.get("action") or "cancel").strip().lower()
    if action != "cancel":
        return json.dumps({"status": "error", "message": "Kun 'cancel' understøttes lige nu."})
    order_id = (args.get("order_id") or "").strip()
    if not order_id:
        return json.dumps({"status": "error", "message": "Angiv et order_id."})

    try:
        from order_service import OrderContext, get_order, cancel_order
    except Exception:
        return json.dumps({"status": "error", "message": "Ordrehåndtering er ikke tilgængelig lige nu."})
    ctx = OrderContext.from_session(source="chat")

    order = get_order(ctx, order_id)
    if not isinstance(order, dict) or not order.get("success"):
        return json.dumps({"status": "error", "message": "Ordren blev ikke fundet."})
    o = order.get("order") or order

    if not bool(args.get("confirm")):
        return json.dumps(needs_confirmation_payload(
            action="manage_my_order",
            summary_da=(
                f"Bekræft at du vil annullere din bestilling '{o.get('product_title') or order_id}'. "
                f"Send confirm=true for at annullere."
            ),
            details={"order_id": order_id, "product_title": o.get("product_title")},
        ))

    result = cancel_order(ctx, order_id)
    if not isinstance(result, dict) or not result.get("success"):
        return json.dumps({"status": "error",
                           "message": (result or {}).get("message", "Annulleringen fejlede.")})
    return json.dumps({
        "status": "success",
        "order_id": order_id,
        "refunded": result.get("refunded"),
        "message": "Din bestilling er annulleret." + (" Budgettet er refunderet." if result.get("refunded") else ""),
    }, default=str)


def _execute_request_manager_approval(args, username):
    """MUTATION — nudge the company's managers to review the user's pending order.

    Confirm-gated. Self-scoped: only the user's own order is referenced. Sends a
    branded 'order_approval_needed' reminder to manager recipients via email_service.
    """
    from tool_confirm import needs_confirmation_payload
    from flask import session as flask_session
    if not username:
        return json.dumps({"status": "error", "message": "Du skal være logget ind."})
    company_id = flask_session.get("company_id")
    if not company_id:
        return json.dumps({"status": "error", "message": "Ingen virksomhed fundet."})
    order_id = (args.get("order_id") or "").strip()
    if not order_id:
        return json.dumps({"status": "error", "message": "Angiv et order_id."})

    try:
        from order_service import OrderContext, get_order
    except Exception:
        return json.dumps({"status": "error", "message": "Ordrehåndtering er ikke tilgængelig lige nu."})
    ctx = OrderContext.from_session(source="chat")
    order = get_order(ctx, order_id)
    if not isinstance(order, dict) or not order.get("success"):
        return json.dumps({"status": "error", "message": "Ordren blev ikke fundet."})
    o = order.get("order") or order
    product_title = o.get("product_title") or order_id

    if not bool(args.get("confirm")):
        return json.dumps(needs_confirmation_payload(
            action="request_manager_approval",
            summary_da=(
                f"Bekræft at du vil bede din leder om at godkende '{product_title}'. "
                f"Send confirm=true for at sende påmindelsen."
            ),
            details={"order_id": order_id, "product_title": product_title},
        ))

    try:
        from order_service import _manager_recipient_emails
        from email_service import send_branded_email
        managers = _manager_recipient_emails(company_id) or []
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Kunne ikke finde ledere: {e}"})
    if not managers:
        return json.dumps({"status": "error", "message": "Ingen ledere fundet at sende påmindelsen til."})

    sent = 0
    for email in managers:
        try:
            if send_branded_email(email, "Kursusbestilling afventer godkendelse",
                                  "order_approval_needed", {}, company_id=company_id,
                                  product_title=product_title, requester=username):
                sent += 1
        except Exception:
            continue
    return json.dumps({
        "status": "success",
        "sent": sent,
        "message": f"Påmindelse sendt til {sent} leder(e) om '{product_title}'.",
    }, default=str)


def _execute_open_in_app(args, username=None):
    """Resolve a cross-surface navigation directive for the SPA.

    Returns a structured payload the agent forwards as a `ui_action` SSE event.
    Performs NO mutation — `start_order` only opens the enrolment flow; the
    actual order still goes through the confirm-gated order tools. Every target
    is validated and a concrete internal URL is produced server-side so the
    client never has to guess a route.
    """
    action = (args.get("action") or "").strip()
    valid = {"view_product", "open_compare", "open_profile", "open_mind_map",
             "open_cv_upload", "open_learning_path", "open_catalog", "start_order", "open_profiler"}
    if action not in valid:
        return json.dumps({"status": "error", "message": f"Ukendt handling: {action}"}, ensure_ascii=False)

    label = (args.get("label") or "").strip()
    out = {"status": "success", "action": action}

    if action in ("view_product", "start_order"):
        handle = (args.get("handle") or "").strip()
        if not handle:
            return json.dumps({"status": "error", "message": "handle mangler."}, ensure_ascii=False)
        product = catalog.get_product(handle)
        if not product:
            return json.dumps({"status": "not_found", "message": f"Kurset '{handle}' blev ikke fundet."}, ensure_ascii=False)
        out["handle"] = handle
        out["target"] = f"/products/{handle}"
        out["title"] = product.get("title") or handle
        out["label"] = label or ("Start tilmelding" if action == "start_order" else "Åbn kurset")
        if action == "start_order":
            out["intent"] = "order"
            out["new_tab"] = False
        else:
            out["new_tab"] = True

    elif action == "open_compare":
        handles = [h for h in (args.get("handles") or []) if h]
        if len(handles) < 2:
            return json.dumps({"status": "error", "message": "Mindst 2 handles kræves for sammenligning."}, ensure_ascii=False)
        out["handles"] = handles[:4]
        out["target"] = "/catalog?compare=" + ",".join(handles[:4])
        out["label"] = label or "Sammenlign kurser"

    elif action == "open_profile":
        section = (args.get("section") or "").strip()
        out["section"] = section
        out["target"] = "/profile" + (f"#{section}" if section else "")
        out["label"] = label or "Åbn profil"

    elif action == "open_mind_map":
        out["target"] = "/mind-map"
        out["label"] = label or "Åbn 3D Mind-Map"

    elif action == "open_cv_upload":
        out["target"] = "/profil-upload"
        out["label"] = label or "Upload / opdater CV"

    elif action == "open_learning_path":
        out["target"] = "/profile#learning-paths"
        out["label"] = label or "Se min læringssti"

    elif action == "open_profiler":
        out["target"] = "/ai-profiler"
        out["label"] = label or "Gør profilen færdig"

    elif action == "open_catalog":
        q = (args.get("query") or "").strip()
        out["query"] = q
        out["target"] = "/catalog" + (f"?q={q}" if q else "")
        out["label"] = label or "Åbn kataloget"

    return _model_tool_json(**out)


def _execute_show_cv_summary(args, username):
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    focus = (args.get("focus") or "overview").strip()
    try:
        from app1.user_profile_db import get_full_profile, ensure_tables
        ensure_tables()
        profile = get_full_profile(username)
        skills = profile.get("skills") or []
        experience = profile.get("experience") or []
        education = profile.get("education") or []
        certifications = profile.get("certifications") or []
        languages = profile.get("languages") or []
        sections = {
            "skills": [{"name": s.get("skill_name", ""), "level": s.get("skill_level", "")} for s in skills[:6]],
            "experience": [{"title": e.get("title", ""), "company": e.get("company", ""), "years": str(e.get("start_year", "") or "")} for e in experience[:4]],
            "education": [{"degree": e.get("degree", ""), "institution": e.get("institution", ""), "year": str(e.get("year_completed", "") or "")} for e in education[:3]],
            "certifications": [{"name": c.get("name", ""), "issuer": c.get("issuer", "")} for c in certifications[:4]],
            "languages": [{"language": l.get("language", ""), "proficiency": l.get("proficiency", "")} for l in languages],
        }
        counts = {k: len(v_list) for k, v_list in [
            ("skills", skills), ("experience", experience), ("education", education),
            ("certifications", certifications), ("languages", languages),
        ]}
        total = sum(counts.values())
        return json.dumps({
            "status": "cv_summary",
            "sections": sections,
            "counts": counts,
            "total": total,
            "has_cv": total > 0,
            "focus": focus,
        }, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def _execute_show_mindmap_preview(args, username):
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    try:
        from app1.user_profile_db import (
            get_full_profile, get_memories, profile_completeness, ensure_tables,
        )
        ensure_tables()
        profile = get_full_profile(username)
        memories = get_memories(username) or []
        completeness = profile_completeness(username, profile=profile)
        skills = profile.get("skills") or []
        experience = profile.get("experience") or []
        education = profile.get("education") or []
        certifications = profile.get("certifications") or []
        languages = profile.get("languages") or []
        categories = {
            "kompetencer": len(skills),
            "erfaring": len(experience),
            "uddannelse": len(education),
            "certificeringer": len(certifications),
            "sprog": len(languages),
            "hukommelse": len(memories),
        }
        recent = []
        for m in sorted(memories, key=lambda x: x.get("created_at") or "", reverse=True)[:3]:
            recent.append({"label": m.get("label", ""), "category": m.get("category", "andet")})
        leaf_count = sum(categories.values())
        return json.dumps({
            "status": "mindmap_preview",
            "completeness": completeness,
            "categories": categories,
            "counts": {"leaves": leaf_count, "memories": len(memories)},
            "recent_memories": recent,
        }, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def _execute_show_skill_gaps(args, username):
    """Per-learner skill-gap card + data (the grounded bridge to recommendations).

    Returns the computed gaps (current vs target on the 1-5 scale, from company
    targets + target role + goals) so the model can both render the card AND
    follow up with a gap-closing recommend_for_profile."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    try:
        from app1.user_profile_db import get_full_profile, ensure_tables
        from competency import compute_skill_gaps
        ensure_tables()
        profile = get_full_profile(username)
        gaps = compute_skill_gaps(username, profile=profile)
        target_role = (profile.get("target_role") or "").strip()
        # Honest empty-states: distinguish "no target set" from "no gaps".
        if not gaps:
            reason = "no_target" if not target_role else "covered"
            return json.dumps({
                "status": "skill_gaps", "gaps": [], "count": 0,
                "target_role": target_role, "has_gaps": False, "reason": reason,
            }, ensure_ascii=False, default=str)
        return json.dumps({
            "status": "skill_gaps",
            "gaps": gaps[:8],
            "count": len(gaps),
            "target_role": target_role,
            "has_gaps": True,
            "next_action": "recommend_for_profile",
        }, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def _execute_save_learning_path(args, username):
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    title = (args.get("title") or "").strip()
    steps = args.get("steps") or []
    if not title or not isinstance(steps, list) or not steps:
        return json.dumps({"status": "error", "message": "title og mindst ét trin (steps) kræves."})
    try:
        from app1.user_profile_db import save_learning_path, ensure_tables
        ensure_tables()
        path_id = save_learning_path(username, title, steps, goal=(args.get("goal") or "")[:500], source="ai")
        return json.dumps({"status": "success", "section": "learning_path", "path_id": path_id,
                           "message": f"Læringssti gemt: {title}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Kunne ikke gemme læringssti: {e}"})


def _execute_get_learning_path(args, username):
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."})
    try:
        from app1.user_profile_db import get_learning_path, get_learning_paths, ensure_tables
        ensure_tables()
        pid = args.get("path_id")
        if pid:
            try:
                path = get_learning_path(username, int(pid))
            except (TypeError, ValueError):
                path = None
            if not path:
                return json.dumps({"status": "not_found", "message": "Læringsstien blev ikke fundet."})
            return _model_tool_json(status="success", section="learning_path", path=path)
        paths = get_learning_paths(username, limit=10)
        return _model_tool_json(
            status="success", section="learning_path", count=len(paths), paths=paths,
            message=("Ingen gemte læringsstier endnu." if not paths else f"{len(paths)} gemt(e) læringssti(er)."),
        )
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Fejl ved hentning af læringssti: {e}"})


def execute_tool(tool_call, username=None, session_id=None):
    """Router to execute the requested tool and return the output."""
    function_name = tool_call.function.name

    try:
        args = json.loads(tool_call.function.arguments)
    except Exception as e:
        print(f"[Tool ArgError] {function_name} session={session_id}: {e}")
        return json.dumps({"status": "error", "message": f"Kunne ikke parse tool-argumenter: {e}"})

    try:
        if function_name == "catalog_search":
            return _execute_catalog_search(args, username)
        elif function_name == "catalog_get_product":
            return _execute_catalog_get_product(args)
        elif function_name == "catalog_get_category":
            return _execute_catalog_get_category(args)
        elif function_name == "catalog_get_vendor":
            return _execute_catalog_get_vendor(args)
        elif function_name == "catalog_compare_products":
            return _execute_catalog_compare_products(args)
        elif function_name == "get_learning_context":
            return _execute_get_learning_context(args, username)
        elif function_name == "check_course_readiness":
            return _execute_check_course_readiness(args, username)
        elif function_name == "prepare_course_order":
            return _execute_prepare_course_order(args, username)
        elif function_name == "search_courses":
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
        elif function_name == "remember_about_user":
            return _execute_remember_about_user(args, username)
        elif function_name == "recommend_for_profile":
            return _execute_recommend_for_profile(args, username)
        elif function_name == "suggest_learning_path":
            return _execute_suggest_learning_path(args, username)
        elif function_name == "save_learning_path":
            return _execute_save_learning_path(args, username)
        elif function_name == "get_learning_path":
            return _execute_get_learning_path(args, username)
        elif function_name == "open_in_app":
            return _execute_open_in_app(args, username)
        elif function_name == "request_user_input":
            return _execute_request_user_input(args, username)
        elif function_name == "create_course_order":
            return _execute_create_order(args)
        elif function_name == "analyze_skill_gaps":
            return _execute_analyze_skill_gaps(args)
        elif function_name == "check_order_approval_status":
            return _execute_check_approval_status(args)
        elif function_name == "get_department_budget":
            return _execute_get_department_budget(args)
        elif function_name == "set_learning_goal":
            return _execute_set_learning_goal(args, username)
        elif function_name == "get_learning_goals":
            return _execute_get_learning_goals(args, username)
        elif function_name == "update_learning_goal":
            return _execute_update_learning_goal(args, username)
        elif function_name == "get_my_course_status":
            return _execute_get_my_course_status(args, username)
        elif function_name == "get_negotiated_discount":
            return _execute_get_negotiated_discount(args, username)
        elif function_name == "check_course_prerequisites":
            return _execute_check_course_prerequisites(args)
        elif function_name == "get_course_sequel":
            return _execute_get_course_sequel(args)
        elif function_name == "find_certification_path":
            return _execute_find_certification_path(args)
        elif function_name == "track_goal_progress":
            return _execute_track_goal_progress(args, username)
        elif function_name == "add_to_calendar":
            return _execute_add_to_calendar(args, username)
        elif function_name == "mark_course_complete":
            return _execute_mark_course_complete(args, username)
        # AI Tooler 2 (Phase 7): employee-facing action tools
        elif function_name == "save_course_for_later":
            return _execute_save_course_for_later(args, username)
        elif function_name == "set_course_reminder":
            return _execute_set_course_reminder(args, username)
        elif function_name == "manage_my_order":
            return _execute_manage_my_order(args, username)
        elif function_name == "request_manager_approval":
            return _execute_request_manager_approval(args, username)
        elif function_name == "show_cv_summary":
            return _execute_show_cv_summary(args, username)
        elif function_name == "show_mindmap_preview":
            return _execute_show_mindmap_preview(args, username)
        elif function_name == "show_skill_gaps":
            return _execute_show_skill_gaps(args, username)
        else:
            return json.dumps({"status": "error", "message": f"Ukendt funktion: {function_name}"})
    except Exception as e:
        import traceback
        print(f"[Tool Error] {function_name} session={session_id}: {e}")
        print(f"[Tool Traceback] {traceback.format_exc()}")
        return json.dumps({"status": "error", "message": f"Intern fejl i {function_name}: {str(e)}"})
