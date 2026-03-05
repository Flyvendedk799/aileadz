# Custom RAG and Tool schemas for app1
import re
from app1.rag import semantic_search_courses, load_augmented_products

# Tags to exclude from compact results (too generic or region-based)
_EXCLUDED_TAG_PREFIXES = {"region:", "by:", "land:"}
_EXCLUDED_TAGS = {"kursus", "kurser", "uddannelse", "training", "course", "denmark", "danmark"}

def extract_city_name(address):
    """Turn addresses like 'Kongsvang Alle 29, 8000 Aarhus C' into 'Aarhus'."""
    if not address:
        return None
    # Try pattern: 4-digit postal code followed by city name
    m = re.search(r'\b\d{4}\s+([A-ZÆØÅa-zæøå]+)', address)
    if m:
        return m.group(1)
    # Fallback: if it's already a simple city name (no digits, no commas)
    if ',' not in address and not re.search(r'\d', address):
        return address.strip()
    # Last resort: take last comma-separated part, strip postal codes
    parts = address.split(',')
    last_part = parts[-1].strip()
    cleaned = re.sub(r'\b\d{4}\s*', '', last_part).strip()
    return cleaned if cleaned else address.strip()

def _extract_compact_fields(product):
    """Extract enriched compact fields from a product."""
    variants = product.get("variants", [])
    price = variants[0].get("price", "N/A") if variants else "N/A"

    # Locations — extract city names from variant option1, deduplicated
    raw_locations = [v.get("option1") for v in variants if v.get("option1")]
    cities = list(dict.fromkeys(extract_city_name(loc) for loc in raw_locations if extract_city_name(loc)))[:3]

    # Dates — from variant option2, first 4
    dates = [v.get("option2") for v in variants if v.get("option2")][:4]

    # Product type
    product_type = product.get("product_type", "")

    # Tags — first 4 relevant tags
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

# Tool definitions for the LLM
OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_courses",
            "description": "Perform a semantic vector search to find courses matching the user's criteria. Use this whenever a user is looking for course recommendations, filtering by location, or asking general discovery questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The main search query to embed (e.g. 'Ledelse', 'IT kursus', 'Noget med personlig udvikling')."
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
    }
]

def _execute_search_courses(args):
    """Handle search_courses tool execution."""
    import json
    query = args.get("query", "")
    limit = args.get("limit", 3)

    results = semantic_search_courses(query, limit=limit)

    # Handle error dicts from rag.py
    if isinstance(results, dict) and "error" in results:
        return json.dumps({"status": "error", "message": results["message"]})

    if not results:
        return json.dumps({"status": "no_results"})

    compact_results = [_extract_compact_fields(r) for r in results]

    return json.dumps({
        "status": "success",
        "results": compact_results,
        "raw_products": results
    })


def _execute_filter_courses(args):
    """Handle filter_courses tool execution."""
    import json
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

        # Location filter — partial match on variant option1
        if location:
            locs = [v.get("option1", "").lower() for v in variants if v.get("option1")]
            if not any(location in loc for loc in locs):
                continue

        # Price filter — check first variant price
        if price_min is not None or price_max is not None:
            try:
                price_val = float(variants[0].get("price", 0)) if variants else 0
            except (ValueError, TypeError):
                price_val = 0
            if price_min is not None and price_val < float(price_min):
                continue
            if price_max is not None and price_val > float(price_max):
                continue

        # Product type filter
        if product_type:
            pt = p.get("product_type", "").lower()
            if product_type not in pt:
                continue

        # Tag filter
        if tag:
            all_tags = p.get("tags", [])
            if isinstance(all_tags, str):
                all_tags = [t.strip().lower() for t in all_tags.split(",")]
            else:
                all_tags = [t.lower() for t in all_tags]
            if not any(tag in t for t in all_tags):
                continue

        filtered.append(p)

    # Rank by semantic similarity if query provided, else sort by price ascending
    if query and filtered:
        from app1.rag import get_query_embedding, cosine_similarity
        query_vector = get_query_embedding(query)
        if query_vector:
            scored = []
            for p in filtered:
                vec = p.get("embedding")
                if vec:
                    score = cosine_similarity(query_vector, vec)
                    scored.append((score, p))
                else:
                    scored.append((0, p))
            scored.sort(key=lambda x: x[0], reverse=True)
            filtered = [item[1] for item in scored[:limit]]
        else:
            filtered = filtered[:limit]
    else:
        # Sort by price ascending
        def sort_price(p):
            try:
                return float(p.get("variants", [{}])[0].get("price", 999999))
            except (ValueError, TypeError, IndexError):
                return 999999
        filtered.sort(key=sort_price)
        filtered = filtered[:limit]

    if not filtered:
        return json.dumps({"status": "no_results"})

    compact_results = [_extract_compact_fields(r) for r in filtered]
    return json.dumps({
        "status": "success",
        "results": compact_results,
        "raw_products": filtered
    })


def _execute_get_course_details(args):
    """Handle get_course_details tool execution."""
    import json
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


def execute_tool(tool_call):
    """Router to execute the requested tool and return the output."""
    import json
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
        else:
            return json.dumps({"status": "error", "message": f"Ukendt funktion: {function_name}"})
    except Exception as e:
        print(f"[Tool Error] {function_name}: {e}")
        return json.dumps({"status": "error", "message": f"Intern fejl i {function_name}: {str(e)}"})
