# Custom RAG and Tool schemas for app1
# Phase 1: Intent classification & query rewriting
# Phase 2: Comparison & recommendation tools
# Phase 5: Hybrid search integration
import re
import json
import openai
from app1.rag import semantic_search_courses, load_augmented_products, hybrid_rank_products

# Tags to exclude from compact results (too generic or region-based)
_EXCLUDED_TAG_PREFIXES = {"region:", "by:", "land:"}
_EXCLUDED_TAGS = {"kursus", "kurser", "uddannelse", "training", "course", "denmark", "danmark"}

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
    price = variants[0].get("price", "N/A") if variants else "N/A"

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

    return {
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


# ── Phase 1: Intent Classification ──

def classify_intent(user_query, user_profile="", shown_products_count=0, conversation_messages=None):
    """
    Classify user intent and optionally rewrite the query for better search.
    Now receives conversation history for multi-turn context.
    Returns: {"intent": str, "search_query": str, "hint": str}
    """
    context = ""
    if user_profile:
        context += f"\nBrugerprofil: {user_profile}"
    if shown_products_count > 0:
        context += f"\nAntal viste kurser: {shown_products_count}"

    # Build conversation context from recent messages
    conv_context = ""
    if conversation_messages:
        recent = []
        for m in conversation_messages[-8:]:
            role = m.get("role", "")
            content = m.get("content", "")
            if not content or role == "system" or role == "tool":
                continue
            if role == "user":
                recent.append(f"Bruger: {content[:150]}")
            elif role == "assistant":
                recent.append(f"Rådgiver: {content[:100]}")
        if recent:
            conv_context = "\n\nSAMTALEHISTORIK:\n" + "\n".join(recent[-6:])

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"""Klassificer brugerens intent og omskriv søgeforespørgslen.

Mulige intents:
- discovery: Brugeren søger kurser eller giver nok info til at søge (emne, budget, format, lokation). Brug dette OGSÅ når brugeren besvarer spørgsmål med konkrete krav som "under 8000 og fysisk" — det ER en søgeanmodning.
- comparison: Brugeren vil sammenligne kurser ("hvad er forskellen?", "hvilken er bedst?")
- detail: Brugeren vil vide mere om ét kursus ("fortæl mere", "hvad koster den?")
- follow_up: Brugeren refererer til tidligere viste kurser ("den billigste", "nummer 2")
- chit_chat: Smalltalk, hilsner, tak ("hej", "tak", "hvem er du?")
- needs_clarification: KUN hvis det er umuligt at gætte hvad brugeren leder efter (bare "hej" eller "hjælp"). Brug IKKE dette hvis brugeren har givet budget, emne eller format.
{context}{conv_context}

VIGTIGT: Vær handlingsorienteret. Når brugeren giver budget, format eller krav, er intent ALTID "discovery" — ikke "needs_clarification".
VIGTIGT: search_query skal kombinere info fra HELE samtalen. Hvis brugeren først sagde "planlægning" og nu siger "under 8000 og fysisk", skal search_query være "planlægning kursus fysisk".

Svar PRÆCIS som JSON: {{"intent": "...", "search_query": "...", "hint": "..."}}
- search_query: Optimeret søgeterm baseret på ALLE brugerens behov fra samtalen (ikke kun denne besked). Kombiner emne + krav. Tomt KUN for chit_chat/follow_up.
- hint: Kort instruktion til AI-rådgiveren (på dansk, maks 1 sætning). For discovery: "Søg med det samme, stil ikke flere spørgsmål."."""},
                {"role": "user", "content": user_query}
            ],
            temperature=0.1,
            max_tokens=150
        )
        result = json.loads(response.choices[0].message.content.strip())
        return result
    except Exception as e:
        print(f"[Intent Error] {e}")
        return {"intent": "discovery", "search_query": user_query, "hint": ""}


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
    }
]


# ── Tool Execution ──

def _execute_search_courses(args):
    """Handle search_courses tool execution with hybrid search."""
    query = args.get("query", "")
    limit = args.get("limit", 3)

    results = semantic_search_courses(query, limit=limit)

    if isinstance(results, dict) and "error" in results:
        return json.dumps({"status": "error", "message": results["message"]})

    if not results:
        return json.dumps({"status": "no_results", "message": "Ingen kurser matchede din søgning."})

    compact_results = [_extract_compact_fields(r) for r in results]

    return json.dumps({
        "status": "success",
        "count": len(compact_results),
        "results": compact_results,
        "raw_products": results
    })


def _execute_filter_courses(args):
    """Handle filter_courses tool execution with hybrid ranking."""
    location = args.get("location", "").lower().strip()
    price_min = args.get("price_min")
    price_max = args.get("price_max")
    product_type = args.get("product_type", "").lower().strip()
    tag = args.get("tag", "").lower().strip()
    query = args.get("query", "").strip()
    limit = args.get("limit", 5)

    products = load_augmented_products()
    if not products:
        return json.dumps({"status": "error", "message": "Produktindekset er ikke indlæst."})

    filtered = []
    for p in products:
        variants = p.get("variants", [])

        if location:
            locs = [v.get("option1", "").lower() for v in variants if v.get("option1")]
            if not any(location in loc for loc in locs):
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

    compact_results = [_extract_compact_fields(r) for r in filtered]
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
            locations = set(v.get("option1") for v in p.get("variants", []) if v.get("option1"))
            dates = [v.get("option2") for v in p.get("variants", []) if v.get("option2")]
            return json.dumps({
                "title": p.get("title"),
                "price": p.get("variants", [{}])[0].get("price", "N/A"),
                "vendor": p.get("vendor"),
                "locations": list(locations),
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
            "price": variants[0].get("price", "N/A") if variants else "N/A",
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


def execute_tool(tool_call):
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
        else:
            return json.dumps({"status": "error", "message": f"Ukendt funktion: {function_name}"})
    except Exception as e:
        print(f"[Tool Error] {function_name}: {e}")
        return json.dumps({"status": "error", "message": f"Intern fejl i {function_name}: {str(e)}"})
