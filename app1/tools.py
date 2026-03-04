# Custom RAG and Tool schemas for app1
from app1.rag import semantic_search_courses, load_augmented_products

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

def execute_tool(tool_call):
    """Router to execute the requested tool and return the output."""
    function_name = tool_call.function.name
    import json
    
    try:
        args = json.loads(tool_call.function.arguments)
    except Exception:
        args = {}

    if function_name == "search_courses":
        query = args.get("query", "")
        limit = args.get("limit", 3)
        
        # Use our NumPy-free RAG module
        results = semantic_search_courses(query, limit=limit)
        
        if not results:
            return json.dumps({"status": "no_results"})
            
        # Return a compact summary to the LLM
        compact_results = []
        for r in results:
            compact_results.append({
                "title": r.get("title"),
                "handle": r.get("handle"),
                "vendor": r.get("vendor"),
                "summary": r.get("ai_summary")
            })
            
        return json.dumps({
            "status": "success",
            "results": compact_results,
            "raw_products": results # We will intercept this in the main loop to render UI HTML
        })

    elif function_name == "get_course_details":
        handle = args.get("handle")
        products = load_augmented_products()
        
        for p in products:
            if p.get("handle") == handle:
                # Compile details
                locations = set(v.get("option1") for v in p.get("variants", []) if v.get("option1"))
                dates = [v.get("option2") for v in p.get("variants", []) if v.get("option2")]
                return json.dumps({
                    "title": p.get("title"),
                    "price": p.get("variants", [{}])[0].get("price", "N/A"),
                    "vendor": p.get("vendor"),
                    "locations": list(locations),
                    "upcoming_dates": dates,
                    "description": p.get("ai_summary", p.get("body_html", "")[:200]),
                    "raw_product": p # Intercept for single card UI
                })
                
        return json.dumps({"status": "not_found", "message": f"Could not find a course with handle '{handle}'."})
        
    return json.dumps({"status": "error", "message": "Unknown function."})
