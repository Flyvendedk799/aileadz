from flask import Flask, render_template, Blueprint, render_template_string, request, jsonify, session
from markupsafe import escape
import json
import logging
import openai
import os
import re
import uuid
import datetime

# Try to import fuzzywuzzy; if not installed, raise a clear error.
try:
    from fuzzywuzzy import fuzz, process
except ImportError:
    raise ImportError("Please install fuzzywuzzy (pip install fuzzywuzzy) or consider using RapidFuzz.")

app = Flask(__name__)
app.secret_key = "supersecretkey"  # For production, use a secure and unpredictable key.

@app.template_filter('dkprice')
def _dkprice_filter(value):
    """Format a price string with Danish thousands separator (dot)."""
    try:
        num = float(str(value).replace(",", "."))
        if num == int(num):
            return f"{int(num):,}".replace(",", ".")
        return f"{num:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return value

app1_bp = Blueprint('app1', __name__, template_folder='templates')

@app1_bp.route('/')
def index():
    return render_template('index.html', logged_in_user=session.get('user'))

SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL", "futurematch.dk")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Set the key for openai
openai.api_key = OPENAI_API_KEY

PRODUCTS_CACHE = None
FUZZY_THRESHOLD = 60

# For matching "13. februar" or "13. februar 2025" etc.
DATE_REGEX = r'\b(\d{1,2}(?:\.\s*|\s+)?[a-zæøå]+(?:\s+\d{4})?)\b'

# Mapping Danish month names to month numbers
DANISH_MONTHS = {
    "januar": 1,
    "februar": 2,
    "marts": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12
}

def load_products():
    global PRODUCTS_CACHE
    if PRODUCTS_CACHE is None:
        file_path = os.path.join(os.path.dirname(__file__), "shopify_products_all_pages.json")
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                PRODUCTS_CACHE = json.load(file)
        except Exception as e:
            print(f"Error loading JSON file: {e}")
            PRODUCTS_CACHE = []
    return PRODUCTS_CACHE

def extract_location_and_date(product):
    location = ""
    date_val = ""
    for option in product.get("options", []):
        name = option.get("name", "").lower()
        if name == "lokation" and option.get("values"):
            location = option.get("values")[0]
        elif name == "tidspunkt" and option.get("values"):
            date_val = option.get("values")[0]
    return location, date_val

def extract_product_from_query(query, last_handle=None):
    context_hint = f" The user might be referring to a previously discussed product with handle '{last_handle}'." if last_handle else ""
    prompt = (
        f"Extract the product name from the following query.{context_hint} "
        f"Return only the product name (no additional text) or an empty string if none is found:\n\n"
        f"Query: \"{query}\""
    )
    try:
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are an assistant that extracts product names from user queries. If the query is just asking for more information without specifying a name, and context is provided, you may return the context name."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
        )
        extracted_name = response.choices[0].message.content.strip()
        # If the model returns nothing, or a generic response, and we have a last_handle, fallback to it
        if not extracted_name and last_handle and any(word in query.lower() for word in ["den", "det", "dette", "mere", "info", "fortæl"]):
            return last_handle
        return extracted_name
    except Exception as e:
        print(f"Error extracting product name using NLP: {e}")
        return ""

def combined_score(a, b):
    return max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b))

def get_best_match(query, product_titles):
    query_lower = query.lower().strip()
    
    # 1. Exact match
    for title in product_titles:
        if query_lower == title:
            return (title, 100)
            
    # 2. Exact substring match (e.g. "teamledelse" in "Teamledelse")
    # Priority to titles that start with the query, or have it strictly as a word.
    for title in product_titles:
        if title.startswith(query_lower):
            return (title, 95)
    for title in product_titles:
        if f" {query_lower} " in f" {title} " or f" {query_lower}" in f" {title}":
            return (title, 90)

    # 3. Fuzzy match fallback
    best_match = process.extractOne(query_lower, product_titles, scorer=combined_score)
    return best_match if best_match and best_match[1] >= FUZZY_THRESHOLD else None

def suggest_courses(query, product_list, count=3):
    scored_products = []
    for product in product_list:
        score = combined_score(query, product.get("title_lower", ""))
        if score >= FUZZY_THRESHOLD:
            scored_products.append((score, product))
    scored_products.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored_products[:min(count, len(scored_products))]]

def handle_legal_and_product_conflict(query, matched_products, product_titles):
    legal_terms = ["lov", "forordning", "regel", "akt"]
    if any(term in query for term in legal_terms):
        best_match = get_best_match(query, product_titles)
        if best_match:
            return matched_products[product_titles.index(best_match[0])]
    return None

def find_keywords_in_query(query):
    keywords = {
        "price": ["pris", "prisen", "koster", "hvad koster", "prisen på", "beløb", "omkostninger"],
        "date": ["dato", "start", "begynder", "startdato", "starttidspunkt", "år", "måned", "periode", "tidspunkt", "hvornår"],
        "description": ["beskrivelse", "info", "detaljer", "om", "indhold", "fortæl"],
        "location": ["sted", "lokation", "by", "adresse", "placering", "hvor er det", "hvor bliver det afholdt"],
        "vendor": ["udbyder", "leverandør", "sælger", "forhandler", "firma", "organisator", "arrangør"]
    }
    matched_keywords = {}
    for key, terms in keywords.items():
        if any(term in query for term in terms):
            matched_keywords[key] = True
    return matched_keywords

# --------------------
# DATE PARSING LOGIC
# --------------------
def parse_danish_date(date_str):
    """
    Parse a date string like "13. februar" or "13. februar 2025" into a datetime.date object.
    If no year is found, assume the current year.
    Returns None if parsing fails.
    """
    # Example input: "13. februar" or "13. februar 2025"
    # Remove "den " if present
    date_str = date_str.lower().replace("den ", "").strip()
    # Split by space to find day, month, possibly year
    parts = date_str.split()
    if len(parts) < 2:
        return None

    # day might have a '.' at the end, e.g. "13."
    day_part = parts[0].replace('.', '')
    try:
        day = int(day_part)
    except:
        return None

    month_str = parts[1]
    month_num = DANISH_MONTHS.get(month_str, None)
    if not month_num:
        return None

    # Try to detect a year
    year = datetime.date.today().year
    if len(parts) >= 3:
        # Attempt to parse the third part as a year
        try:
            year_candidate = int(parts[2])
            year = year_candidate
        except:
            pass

    try:
        parsed_date = datetime.date(year, month_num, day)
        return parsed_date
    except:
        return None


def filter_upcoming_variants(variants):
    """
    Convert each variant's date string (option2) into a date object.
    Return only those that are >= today's date, sorted ascending by date.
    """
    today = datetime.date.today()
    valid_variants = []
    for v in variants:
        raw_date = (v.get("option2") or "").strip()
        dt = parse_danish_date(raw_date)
        if dt and dt >= today:
            valid_variants.append((dt, v))
    # Sort by the date object
    valid_variants.sort(key=lambda x: x[0])
    # Return just the variant objects in ascending order
    return [variant for (_, variant) in valid_variants]

# Summarization Cache
_short_description_cache = {}

def get_short_description(product):
    # First check for the pre-computed RAG summary
    if product.get("ai_summary"):
        return product.get("ai_summary")

    pid = product.get("handle")
    if pid in _short_description_cache:
        return _short_description_cache[pid]
        
    full_desc = product.get("body_html", "")
    if not full_desc:
        summary = "Ingen beskrivelse tilgængelig."
    else:
        try:
            response = openai.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "Sammenfat følgende kursusbeskrivelse til en kort, attraktiv tekst på dansk (maks 50 ord)."},
                    {"role": "user", "content": full_desc}
                ],
                temperature=0.5,
            )
            summary = response.choices[0].message.content.strip()
        except Exception as e:
            print(f"Error summarizing product description: {e}")
            summary = (full_desc[:150] + '...') if len(full_desc) > 150 else full_desc
    _short_description_cache[pid] = summary
    return summary

def get_course_detail(query, product):
    """
    Provides a direct text-based answer (price, date, location, etc.)
    based on user query keywords. Also references the short summary for descriptions.
    Uses date parsing + filtering to ensure we only return future dates.
    """
    query_lower = query.lower()
    keywords = find_keywords_in_query(query_lower)

    # Price
    if "price" in keywords:
        if product["price"] in ["0", "0.00"]:
            return f"Prisen for {escape(product['title'])} er Efter aftale."
        else:
            return f"Prisen for {escape(product['title'])} er {escape(_dkprice_filter(product['price']))} kr."

    # Date
    if "date" in keywords and "hvornår" in query_lower:
        # Filter the variants to only future dates
        variants = product.get("variants", [])
        location_filter = None
        for loc in ["herlev", "hørkær", "københavn"]:
            if loc in query_lower:
                location_filter = loc
                break
        if location_filter:
            # Also filter by location
            variants = [v for v in variants if location_filter in (v.get("option1") or "").lower()]

        upcoming = filter_upcoming_variants(variants)

        if upcoming:
            # Return the earliest upcoming date
            next_date_str = upcoming[0].get("option2", "ikke angivet")
            if location_filter:
                return f"Næste startdato for {escape(product['title'])} i {escape(location_filter.capitalize())} er {escape(next_date_str)}."
            else:
                return f"Næste startdato for {escape(product['title'])} er {escape(next_date_str)}."
        else:
            return f"Der er ingen fremtidige datoer angivet for {escape(product['title'])}."

    # Location
    if ("location" in keywords) or ("hvor" in query_lower and "hvornår" not in query_lower):
        date_pattern = re.compile(DATE_REGEX, re.IGNORECASE)
        match = date_pattern.search(query_lower)
        query_date = match.group(1).strip() if match else None

        if query_date:
            query_date_normalized = query_date.replace('.', '').replace('den ', '').strip()
            matching_variant = None
            for variant in product.get("variants", []):
                variant_date = (variant.get("option2") or "").strip().lower()
                variant_date_normalized = variant_date.replace('.', '').replace('den ', '').strip()
                if query_date_normalized and query_date_normalized in variant_date_normalized:
                    matching_variant = variant
                    break
            if matching_variant:
                location = (matching_variant.get("option1") or "").strip()
                return f"Stedet for {escape(product['title'])} den {escape(matching_variant.get('option2', ''))} er {escape(location)}."
        # If no specific date was asked, show all variant combos
        variants = product.get("variants", [])
        if len(variants) > 1:
            details_list = []
            for variant in variants:
                opt_date = (variant.get("option2") or "").strip()
                opt_location = (variant.get("option1") or "").strip()
                if opt_date and opt_location:
                    details_list.append(f"- {escape(opt_date)}: {escape(opt_location)}")
            if details_list:
                return f"Steder for {escape(product['title'])}:\n" + "\n".join(details_list)
        general_location = product.get("location", "").strip()
        if general_location:
            return f"Stedet for {escape(product['title'])} er {escape(general_location)}."
        else:
            return "Undskyld, den information kunne jeg ikke finde. Prøv at spørg om noget andet, eller omformuler dit spørgsmål."

    # Description
    if "description" in keywords:
        return f"Beskrivelse: {escape(get_short_description(product))}."

    # Vendor
    if "vendor" in keywords:
        return f"Udbyderen for {escape(product['title'])} er {escape(product.get('vendor', 'ukendt'))}."

    # Fallback
    return f"Jeg har fundet {escape(product['title'])}. Se nedenfor for detaljer."

# -------------- RENDERING TEMPLATES --------------

# Single-course HTML snippet
PRODUCT_MEDIA_TEMPLATE = """
<div style="background: rgba(255,255,255,0.03); border-radius: 10px; overflow: hidden; font-family: 'Inter', Arial, sans-serif; color: #d4d4d8; width: 100%; max-width: 420px; border: 1px solid rgba(255,255,255,0.06);">
  {% if product.image and product.image.src %}
  <div style="background: rgba(0,0,0,0.15); padding: 20px; display: flex; justify-content: center; align-items: center;">
    <img src="{{ product.image.src | e }}" alt="{{ product.title | e }}" style="max-height: 80px; object-fit: contain;">
  </div>
  {% endif %}
  <div style="padding: 16px;">
    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 10px;">
      <div>
        <h3 style="margin: 0 0 2px; font-size: 15px; font-weight: 700; color: #f4f4f5; line-height: 1.3;">
          <a href="https://futurematch.dk/products/{{ product.handle }}" target="_blank" style="color: inherit; text-decoration: none;" onmouseover="this.style.color='#c084fc'" onmouseout="this.style.color='#f4f4f5'">{{ product.title | e }}</a>
        </h3>
        <div style="font-size: 12px; color: #52525b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px;" title="{{ product.vendor | e }}">{{ product.vendor | e }}</div>
      </div>
      <div style="font-size: 14px; font-weight: 700; color: #a855f7; white-space: nowrap; flex-shrink: 0;">
        {% set price = (product.variants[0].price | string | trim) if product.variants and product.variants|length > 0 else '0' %}
        {% set price_val = price | float(-1) %}
        {% if price in ["", "None", "N/A"] or price_val == 0 %}Gratis{% elif price_val < 0 %}Pris på forespørgsel{% else %}kr {{ price | dkprice }}{% endif %}
      </div>
    </div>
    {% if product.ai_summary or product.body_html %}
    {% set _desc = get_short_description(product) %}
    <div style="font-size: 13px; color: #a1a1aa; line-height: 1.5; margin-bottom: 12px;">{{ (_desc[:150] + '...') if _desc|length > 150 else _desc | e }}</div>
    {% endif %}
    <div style="display: flex; gap: 16px; margin-bottom: 14px; font-size: 12px; color: #52525b;">
      <span>{% if product.variants and product.variants|length > 1 %}Flere varianter{% else %}Én variant{% endif %}</span>
      <span>{% if product.location %}{{ product.location | e }}{% else %}Online{% endif %}</span>
    </div>
    <div style="display: flex; gap: 8px;">
      <a href="https://futurematch.dk/products/{{ product.handle }}" target="_blank" style="flex: 1; text-align: center; background: #7c3aed; color: #fff; text-decoration: none; padding: 9px 0; border-radius: 8px; font-size: 13px; font-weight: 600; transition: background 0.15s;" onmouseover="this.style.background='#6d28d9'" onmouseout="this.style.background='#7c3aed'">Vælg kursus</a>
      <button onclick="event.stopPropagation(); window.attachProductToChat('{{ product.handle | e }}', '{{ product.title | e }}')" style="padding: 9px 12px; border-radius: 8px; border: 1px solid rgba(168,85,247,0.25); background: rgba(168,85,247,0.08); color: #c084fc; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; transition: all 0.15s; font-family: inherit;" onmouseover="this.style.background='rgba(168,85,247,0.15)';this.style.borderColor='rgba(168,85,247,0.4)'" onmouseout="this.style.background='rgba(168,85,247,0.08)';this.style.borderColor='rgba(168,85,247,0.25)'">Spørg om</button>
    </div>
  </div>
</div>
"""

def _extract_product_location(product):
    """Extract a display-friendly location string from variant option1 fields."""
    variants = product.get("variants") or []
    raw_locs = [v.get("option1") for v in variants if v.get("option1")]
    if not raw_locs:
        return None
    # Try to extract city names from addresses
    cities = []
    for loc in raw_locs:
        loc_stripped = loc.strip()
        if not loc_stripped:
            continue
        # Try postal-code based extraction: "Kongsvang Alle 29, 8000 Aarhus C" → "Aarhus"
        m = re.search(r'\b\d{4}\s+([A-ZÆØÅa-zæøå]+)', loc_stripped)
        if m:
            cities.append(m.group(1))
        elif ',' not in loc_stripped and not re.search(r'\d', loc_stripped):
            cities.append(loc_stripped)
        else:
            parts = loc_stripped.split(',')
            last_part = parts[-1].strip()
            cleaned = re.sub(r'\b\d{4}\s*', '', last_part).strip()
            cities.append(cleaned if cleaned else loc_stripped)
    unique = list(dict.fromkeys(c for c in cities if c))  # dedupe, preserve order
    if not unique:
        return None
    if len(unique) <= 3:
        return ", ".join(unique)
    return f"{unique[0]}, {unique[1]} +{len(unique)-2} mere"

def render_product_media(product):
    p = dict(product)
    if not p.get("location"):
        p["location"] = _extract_product_location(p)
    return render_template_string(PRODUCT_MEDIA_TEMPLATE, product=p, get_short_description=get_short_description)

MULTIPLE_COURSES_TEMPLATE = """
<div style="display: flex; flex-direction: column; gap: 6px; width: 100%; max-width: 420px;">
  {% for course in courses %}
    <div class="course-card" onclick="this.classList.toggle('expanded');">
      <div class="course-card-header">
        <div style="background: rgba(0,0,0,0.2); border-radius: 8px; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; overflow: hidden;">
          {% if course.image and course.image.src %}
              <img src="{{ course.image.src | e }}" style="max-width: 80%; max-height: 80%; object-fit: contain;">
          {% else %}
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#52525b" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>
          {% endif %}
        </div>
        <div style="flex-grow: 1; min-width: 0; font-family: 'Inter', Arial, sans-serif;">
          <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 2px;">
              <h4 style="margin: 0; font-size: 13.5px; font-weight: 600; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 65%; color: #e4e4e7;">
                  <a href="https://futurematch.dk/products/{{ course.handle }}" target="_blank" style="color: inherit; text-decoration: none;" onmouseover="this.style.color='#c084fc'" onmouseout="this.style.color='#e4e4e7'">{{ course.title | e }}</a>
              </h4>
              <div style="font-size: 12.5px; font-weight: 700; color: #a855f7; white-space: nowrap;">
                  {% set price = (course.variants[0].price | string | trim) if course.variants and course.variants|length > 0 else '0' %}
                  {% set price_val = price | float(-1) %}
                  {% if price in ['', 'None', 'N/A'] or price_val == 0 %}Gratis{% elif price_val < 0 %}Pris på forespørgsel{% else %}kr {{ price | dkprice }}{% endif %}
              </div>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #52525b;">
              <div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 65%;">{{ course.vendor | e }}</div>
              <svg class="course-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
          </div>
        </div>
      </div>
      <div class="course-details-wrapper">
        <div class="course-details-inner">
          <div class="course-details-content" style="font-family: 'Inter', Arial, sans-serif;">
            {% set _cdesc = get_short_description(course) %}
            <div style="margin-top: 8px; font-size: 13px; color: #a1a1aa; line-height: 1.55;">{{ (_cdesc[:150] + '...') if _cdesc|length > 150 else _cdesc | e }}</div>
            <div style="display: flex; gap: 16px; margin: 10px 0; font-size: 12px; color: #52525b;">
              <span>{% if course.variants and course.variants|length > 1 %}Flere varianter{% else %}Én variant{% endif %}</span>
              <span>{% if course.location %}{{ course.location | e }}{% else %}Online{% endif %}</span>
            </div>
            <div style="display: flex; gap: 8px;">
              <a onclick="event.stopPropagation();" href="https://futurematch.dk/products/{{ course.handle }}" target="_blank" style="flex: 1; text-align: center; background: #7c3aed; color: #fff; text-decoration: none; padding: 8px 0; border-radius: 8px; font-size: 13px; font-weight: 600; transition: background 0.15s;" onmouseover="this.style.background='#6d28d9'" onmouseout="this.style.background='#7c3aed'">Vælg kursus</a>
              <button onclick="event.stopPropagation(); window.attachProductToChat('{{ course.handle | e }}', '{{ course.title | e }}')" style="padding: 8px 12px; border-radius: 8px; border: 1px solid rgba(168,85,247,0.25); background: rgba(168,85,247,0.08); color: #c084fc; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; transition: all 0.15s; font-family: inherit;" onmouseover="this.style.background='rgba(168,85,247,0.15)';this.style.borderColor='rgba(168,85,247,0.4)'" onmouseout="this.style.background='rgba(168,85,247,0.08)';this.style.borderColor='rgba(168,85,247,0.25)'">Spørg om</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  {% endfor %}
</div>
"""


def render_multi_course_media(courses):
    unique_prefix = str(uuid.uuid4())[:8]
    enriched = []
    for c in courses:
        c2 = dict(c)
        if not c2.get("location"):
            c2["location"] = _extract_product_location(c2)
        enriched.append(c2)
    return render_template_string(MULTIPLE_COURSES_TEMPLATE, courses=enriched, get_short_description=get_short_description, unique_prefix=unique_prefix)

# 6.3: Anonymous browser token endpoint
@app1_bp.route("/anon-token", methods=["POST"])
def anon_token():
    """Create or retrieve anonymous browser token for cross-visit persistence."""
    try:
        data = request.json or {}
        existing_token = data.get("token", "")

        if existing_token:
            from app1.memory_store import load_anonymous_profile
            profile = load_anonymous_profile(existing_token)
            if profile:
                session["browser_token"] = existing_token
                return jsonify({"status": "ok", "token": existing_token, "has_profile": True})

        # Generate new token
        new_token = str(uuid.uuid4())
        session["browser_token"] = new_token
        from app1.memory_store import save_anonymous_profile
        save_anonymous_profile(new_token)
        return jsonify({"status": "ok", "token": new_token, "has_profile": False})
    except Exception as e:
        print(f"[Anon Token Error] {e}")
        return jsonify({"status": "ok", "token": "", "has_profile": False})


# 5.4: Observability dashboard endpoint
@app1_bp.route("/dashboard")
def dashboard():
    try:
        from app1.memory_store import get_observability_dashboard
        hours = request.args.get("hours", 24, type=int)
        data = get_observability_dashboard(hours=hours)
        return jsonify(data)
    except Exception as e:
        print(f"[Dashboard Error] {e}")
        return jsonify({"error": str(e)}), 500


from app1.agent import handle_agentic_ask

@app1_bp.route("/ask", methods=["POST"])
def ask():
    try:
        user_query = request.json.get("query", "").strip()
        if not user_query:
            return jsonify({"answers": [{"type": "text", "content": "Skriv venligst et spørgsmål."}]}), 400

        # Phase 1B: Input length cap
        if len(user_query) > 5000:
            return jsonify({"answers": [{"type": "text", "content": "Din besked er for lang. Prøv at forkorte den."}]}), 400
        if len(user_query) > 2000:
            user_query = user_query[:2000]

        return handle_agentic_ask(user_query, session)

    except Exception as ex:
        print(f"Unexpected error: {ex}")
        return jsonify({"answers": [
            {"type": "text", "content": "Der opstod en uventet fejl. Prøv venligst igen."}
        ]}), 500


# Phase 6: Feedback endpoint
@app1_bp.route("/feedback", methods=["POST"])
def feedback():
    try:
        from app1.memory_store import log_event
        data = request.json or {}
        sid = session.get("session_id", "unknown")
        rating = data.get("rating", 0)  # 1 = thumbs up, -1 = thumbs down
        message_index = data.get("message_index", 0)
        query_text = data.get("query_text", "")
        assistant_response = data.get("assistant_response", "")
        reason = data.get("reason", "")
        comment = data.get("comment", "")
        latency_ms = data.get("latency_ms", 0)

        try:
            latency_val = int(float(latency_ms))
        except (TypeError, ValueError):
            latency_val = 0

        log_event(
            session_id=sid,
            event_type="feedback",
            query_text=query_text,
            feedback_rating=rating,
            message_index=message_index,
            extra={
                "assistant_response": assistant_response[:300],
                "reason": reason[:80],
                "comment": comment[:400],
                "latency_ms": latency_val,
            }
        )
        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"[Feedback Error] {e}")
        return jsonify({"status": "ok"})  # Don't break UI on feedback errors

# ── Admin Debug Log ──

@app1_bp.route("/new_session", methods=["POST"])
def new_session():
    """Start a fresh conversation — saves current to history, then clears."""
    from app1.agent import CHAT_MEMORY, SHOWN_PRODUCTS, CONVERSATION_STAGES, REJECTED_SEARCHES
    old_sid = session.get("session_id")
    logged_in_user = session.get("user")

    # Save current conversation to history before clearing
    if logged_in_user and old_sid and old_sid in CHAT_MEMORY:
        try:
            from app1.user_profile_db import save_conversation_history, ensure_tables
            ensure_tables()
            save_conversation_history(logged_in_user, old_sid, CHAT_MEMORY[old_sid])
        except Exception as e:
            print(f"[Save History Error] {e}")

    # Clear in-memory state
    if old_sid:
        CHAT_MEMORY.pop(old_sid, None)
        SHOWN_PRODUCTS.pop(old_sid, None)
        CONVERSATION_STAGES.pop(old_sid, None)
        REJECTED_SEARCHES.pop(old_sid, None)

    # Generate new session ID
    new_sid = str(uuid.uuid4())
    session["session_id"] = new_sid

    # Clear active conversation in MySQL for logged-in users
    if logged_in_user:
        try:
            from app1.user_profile_db import clear_conversation, ensure_tables
            ensure_tables()
            clear_conversation(logged_in_user)
        except Exception as e:
            print(f"[New Session Error] {e}")

    return jsonify({"status": "ok", "session_id": new_sid})


@app1_bp.route("/load_conversation")
def load_conversation_endpoint():
    """Load saved conversation for logged-in user (for frontend restore)."""
    logged_in_user = session.get("user")
    if not logged_in_user:
        return jsonify({"status": "no_user", "messages": []})
    try:
        from app1.user_profile_db import load_conversation, ensure_tables
        ensure_tables()
        saved = load_conversation(logged_in_user)
        if saved and saved.get("messages"):
            return jsonify({"status": "ok", "messages": saved["messages"]})
        return jsonify({"status": "empty", "messages": []})
    except Exception as e:
        print(f"[Load Conversation Error] {e}")
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return jsonify({"status": "error", "messages": []})


@app1_bp.route("/conversations")
def list_conversations_endpoint():
    """List conversation history for the logged-in user."""
    logged_in_user = session.get("user")
    if not logged_in_user:
        return jsonify({"conversations": []})
    try:
        from app1.user_profile_db import list_conversations, ensure_tables
        ensure_tables()
        convs = list_conversations(logged_in_user)
        return jsonify({"conversations": [
            {"id": c["id"], "title": c["title"],
             "updated_at": c["updated_at"].isoformat() if c.get("updated_at") else None}
            for c in convs
        ]})
    except Exception as e:
        print(f"[List Conversations Error] {e}")
        return jsonify({"conversations": []})


@app1_bp.route("/conversations/<int:conv_id>")
def load_conversation_history_endpoint(conv_id):
    """Load a specific past conversation."""
    logged_in_user = session.get("user")
    if not logged_in_user:
        return jsonify({"status": "error"}), 401
    try:
        from app1.user_profile_db import load_conversation_by_id, ensure_tables
        ensure_tables()
        conv = load_conversation_by_id(logged_in_user, conv_id)
        if not conv:
            print(f"[Load Conv] Not found: id={conv_id}, user={logged_in_user}")
            return jsonify({"status": "not_found"}), 404
        return jsonify({"status": "ok", "conversation": {
            "id": conv["id"], "session_id": conv["session_id"],
            "title": conv["title"], "messages": conv["messages"]
        }})
    except Exception as e:
        import traceback
        print(f"[Load Conversation History Error] {e}")
        traceback.print_exc()
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return jsonify({"status": "error"}), 500


@app1_bp.route("/conversations/<int:conv_id>", methods=["DELETE"])
def delete_conversation_endpoint(conv_id):
    """Delete a conversation from history."""
    logged_in_user = session.get("user")
    if not logged_in_user:
        return jsonify({"status": "error"}), 401
    try:
        from app1.user_profile_db import delete_conversation, ensure_tables
        ensure_tables()
        ok = delete_conversation(logged_in_user, conv_id)
        return jsonify({"status": "ok" if ok else "not_found"})
    except Exception as e:
        print(f"[Delete Conversation Error] {e}")
        return jsonify({"status": "error"}), 500


@app1_bp.route("/confirm_profile_update", methods=["POST"])
def confirm_profile_update():
    """Execute a previously proposed profile update after user clicks 'Gem'."""
    logged_in_user = session.get("user")
    if not logged_in_user:
        return jsonify({"status": "error", "message": "Ikke logget ind"}), 401

    data = request.json or {}
    action = data.get("action", "")
    payload = data.get("data", {})
    # Trim whitespace from all string fields
    payload = {k: v.strip() if isinstance(v, str) else v for k, v in payload.items()}

    def _success(msg):
        """Return success and log the profile update."""
        try:
            from app1.memory_store import log_event
            log_event(session.get("sid", "unknown"), "profile_confirm",
                      tool_used=action, extra={"status": "success", "user": logged_in_user})
        except Exception:
            pass
        return jsonify({"status": "success", "message": msg})

    try:
        from app1.user_profile_db import (
            add_skill, add_experience, add_education, ensure_tables
        )
        ensure_tables()

        if action == "add_skill":
            add_skill(logged_in_user, payload.get("skill_name", ""), payload.get("skill_level", "mellem"))
            return _success("Kompetence tilføjet")

        elif action == "add_experience":
            # Check if this is a follow-up update (entry already exists)
            from app1.user_profile_db import get_experience, update_experience
            existing = get_experience(logged_in_user)
            title_match = payload.get("title", "").strip().lower()
            company_match = payload.get("company", "").strip().lower()
            existing_entry = None
            for e in existing:
                if e["title"].lower() == title_match and (e.get("company", "") or "").lower() == company_match:
                    existing_entry = e
                    break
            if existing_entry:
                # Update existing entry with new details
                updates = {}
                if payload.get("start_year"): updates["start_year"] = payload["start_year"]
                if payload.get("end_year"): updates["end_year"] = payload["end_year"]
                if payload.get("description"): updates["description"] = payload["description"]
                if updates:
                    update_experience(logged_in_user, existing_entry["id"], **updates)
                    return _success("Detaljer opdateret")
                return _success("Ingen ændringer")
            else:
                add_experience(
                    logged_in_user,
                    title=payload.get("title", ""),
                    company=payload.get("company", ""),
                    start_year=payload.get("start_year"),
                    end_year=payload.get("end_year"),
                    is_current=payload.get("is_current", False),
                    description=payload.get("description", ""),
                )
                return _success("Erfaring tilføjet")

        elif action == "add_education":
            add_education(
                logged_in_user,
                degree=payload.get("degree", ""),
                institution=payload.get("institution", ""),
                year_completed=payload.get("year_completed"),
                description=payload.get("description", ""),
            )
            return _success("Uddannelse tilføjet")

        elif action == "add_course":
            add_skill(logged_in_user, payload.get("course_title", "") or payload.get("course_name", ""), "kursus")
            return _success("Kursus tilføjet")

        elif action == "update_summary":
            from app1.user_profile_db import update_profile_summary
            clean = {k: v for k, v in payload.items() if v is not None and str(v).strip()}
            if clean:
                update_profile_summary(logged_in_user, **clean)
                return _success("Profil opdateret")
            return _success("Ingen ændringer")

        else:
            return jsonify({"status": "error", "message": f"Ukendt handling: {action}"}), 400

    except Exception as e:
        logging.error("Confirm profile update error: %s", e)
        try:
            from app1.memory_store import log_event
            log_event(session.get("sid", "unknown"), "profile_confirm",
                      tool_used=action, extra={"status": "error", "error": str(e)})
        except Exception:
            pass
        return jsonify({"status": "error", "message": "Fejl ved opdatering"}), 500


@app1_bp.route("/adminlog")
def adminlog():
    from app1.memory_store import get_debug_sessions, get_debug_logs_for_session
    import datetime as _dt

    sessions = get_debug_sessions(limit=50)

    # Enrich each session with formatted time and first query preview
    for s in sessions:
        ts = s.get("started", 0)
        s["started_fmt"] = _dt.datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "?"

        # Get quick per-session insights for better admin triage
        logs = get_debug_logs_for_session(s["session_id"])
        first_query = ""
        tool_calls = 0
        no_results = 0
        profile_events = 0
        errors = 0
        logged_in = False
        low_confidence = 0
        for log in logs:
            step = log.get("step", "")
            data = log.get("data", {}) or {}
            if step == "user_query" and not first_query:
                first_query = (data.get("query", "") or "")[:80]
                if data.get("logged_in"):
                    logged_in = True
            if step == "tool_call":
                tool_calls += 1
                status = data.get("status", "")
                if status == "no_results":
                    no_results += 1
                if status == "error":
                    errors += 1
                md = data.get("matching_debug", {})
                if md.get("confidence") == "low":
                    low_confidence += 1
            if step in ("profile_event", "ui_card"):
                profile_events += 1
            if step == "tool_error":
                errors += 1

        s["first_query"] = first_query
        s["tool_calls"] = tool_calls
        s["no_results"] = no_results
        s["profile_events"] = profile_events
        s["errors"] = errors
        s["logged_in"] = logged_in
        s["low_confidence"] = low_confidence

    return render_template("adminlog.html", sessions=sessions)


@app1_bp.route("/adminlog/session/<session_id>")
def adminlog_session(session_id):
    from app1.memory_store import get_debug_logs_for_session
    logs = get_debug_logs_for_session(session_id)
    return jsonify({"logs": logs})


@app1_bp.route("/adminlog/sessions_summary")
def adminlog_sessions_summary():
    """Lightweight endpoint for admin log auto-refresh polling."""
    from app1.memory_store import get_debug_sessions
    sessions = get_debug_sessions(limit=50)
    return jsonify({"sessions": [
        {"session_id": s["session_id"], "entry_count": s["entry_count"]}
        for s in sessions
    ]})


@app1_bp.route("/adminlog/clear", methods=["POST"])
def adminlog_clear():
    from app1.memory_store import clear_debug_logs
    clear_debug_logs()
    return jsonify({"status": "ok"})


app.register_blueprint(app1_bp, url_prefix='/app1')

if __name__ == "__main__":
    app.run(debug=False)
