from flask import Flask, render_template, Blueprint, render_template_string, request, jsonify, session, current_app, Response, stream_with_context, url_for, abort
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

app1_bp = Blueprint('app1', __name__, template_folder='templates')

# Register order routes as nested blueprint
try:
    from app1.order_routes import order_routes_bp
    app1_bp.register_blueprint(order_routes_bp, url_prefix='/orders')
except Exception as _e:
    logging.warning("Order routes not loaded: %s", _e)

@app1_bp.app_template_filter('dkprice')
def _dkprice_filter(value):
    """Format a price string with Danish thousands separator (dot)."""
    try:
        num = float(str(value).replace(",", "."))
        if num == int(num):
            return f"{int(num):,}".replace(",", ".")
        return f"{num:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return value

@app1_bp.route('')
@app1_bp.route('/')
def index():
    return render_template('index.html', logged_in_user=session.get('user'), demo_mode=False, demo_messages=[])

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
<div class="fm-product-card course-card premium-course-card premium-course-featured" onclick="this.classList.toggle('expanded');">
  {% if product.image and product.image.src %}
  <div class="premium-course-media">
    <img src="{{ product.image.src | e }}" alt="{{ product.title | e }}">
  </div>
  {% endif %}
  <div class="premium-course-body">
    <div class="premium-course-header">
      <div class="premium-course-main">
        <div class="premium-course-kicker" title="{{ product.vendor | e }}">{{ product.vendor | e }}</div>
        <h3 class="premium-course-title">
          <a href="https://futurematch.dk/products/{{ product.handle }}" target="_blank" onclick="event.stopPropagation();">{{ product.title | e }}</a>
        </h3>
      </div>
      <div class="premium-course-price">
        {% set price = (product.variants[0].price | string | trim) if product.variants and product.variants|length > 0 else '0' %}
        {% set price_val = price | float(-1) %}
        {% if product._original_price %}
        <span class="premium-course-price-old">kr {{ product._original_price | dkprice }}</span>
        {% endif %}
        <span>{% if price in ["", "None", "N/A"] or price_val == 0 %}Gratis{% elif price_val < 0 %}Pris på forespørgsel{% else %}kr {{ price | dkprice }}{% endif %}</span>
        {% if product._agreement_name %}
        <span class="premium-course-agreement">{{ product._agreement_name | e }}</span>
        {% endif %}
      </div>
    </div>
    {% if product.ai_summary or product.body_html %}
    {% set _desc = get_short_description(product) %}
    <div class="premium-course-summary">{{ (_desc[:170] + '...') if _desc|length > 170 else _desc | e }}</div>
    {% endif %}
    <div class="premium-course-meta">
      <span>{% if product.variants and product.variants|length > 1 %}{{ product.variants|length }} varianter{% else %}1 variant{% endif %}</span>
      <span>{% if product.location %}{{ product.location | e }}{% else %}Online{% endif %}</span>
      <span class="premium-expand-label">Se tider</span>
    </div>
    {% if product.variants %}
    <div class="course-details-wrapper">
      <div class="course-details-inner">
        <div class="course-details-content">
          <div class="variant-panel">
            <div class="variant-panel-title">Tider og lokationer</div>
            {% for variant in product.variants[:5] %}
            {% set v_price = (variant.price | string | trim) if variant.price is defined else '' %}
            {% set v_price_val = v_price | float(-1) %}
            <div class="variant-row">
              <span class="variant-date">{{ variant.option2 or 'Dato efter aftale' }}</span>
              <span class="variant-location">{{ variant.option1 or product.location or 'Online' }}</span>
              <span class="variant-price">{% if v_price in ["", "None", "N/A"] or v_price_val == 0 %}Gratis{% elif v_price_val < 0 %}Efter aftale{% else %}kr {{ v_price | dkprice }}{% endif %}</span>
            </div>
            {% endfor %}
            {% if product.variants|length > 5 %}
            <div class="variant-more">+{{ product.variants|length - 5 }} flere muligheder</div>
            {% endif %}
          </div>
        </div>
      </div>
    </div>
    {% endif %}
    <div class="premium-course-actions">
      <a class="course-primary-action" onclick="event.stopPropagation();" href="https://futurematch.dk/products/{{ product.handle }}" target="_blank">Vælg kursus</a>
      <button class="course-secondary-action" onclick='event.stopPropagation(); window.attachProductToChat({{ product.handle | tojson }}, {{ product.title | tojson }})'>Spørg om</button>
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

def _apply_product_discount(product):
    """Apply supplier agreement discount to a product's display price."""
    from app1.tools import apply_discount
    vendor = product.get("vendor", "")
    variants = product.get("variants", [])
    if variants:
        raw_price = variants[0].get("price")
        discounted, original, agreement_name = apply_discount(raw_price, vendor)
        if discounted is not None:
            product["_original_price"] = original
            product["_agreement_name"] = agreement_name
            # Modify first variant price for template rendering
            product["variants"] = [dict(v) for v in variants]
            product["variants"][0] = dict(product["variants"][0])
            product["variants"][0]["price"] = str(discounted)
    return product


def render_product_media(product):
    p = dict(product)
    if not p.get("location"):
        p["location"] = _extract_product_location(p)
    p = _apply_product_discount(p)
    return render_template_string(PRODUCT_MEDIA_TEMPLATE, product=p, get_short_description=get_short_description)

MULTIPLE_COURSES_TEMPLATE = """
<div class="premium-course-stack">
  {% for course in courses %}
    <div class="course-card premium-course-card" onclick="this.classList.toggle('expanded');">
      <div class="premium-course-header premium-course-header-compact">
        <div class="premium-course-thumb">
          {% if course.image and course.image.src %}
              <img src="{{ course.image.src | e }}" alt="{{ course.title | e }}">
          {% else %}
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>
          {% endif %}
        </div>
        <div class="premium-course-main">
          <div class="premium-course-kicker" title="{{ course.vendor | e }}">{{ course.vendor | e }}</div>
          <h4 class="premium-course-title">
            <a href="https://futurematch.dk/products/{{ course.handle }}" target="_blank" onclick="event.stopPropagation();">{{ course.title | e }}</a>
          </h4>
        </div>
        <div class="premium-course-price">
          {% set price = (course.variants[0].price | string | trim) if course.variants and course.variants|length > 0 else '0' %}
          {% set price_val = price | float(-1) %}
          {% if course._original_price %}<span class="premium-course-price-old">kr {{ course._original_price | dkprice }}</span>{% endif %}
          <span>{% if price in ['', 'None', 'N/A'] or price_val == 0 %}Gratis{% elif price_val < 0 %}Pris på forespørgsel{% else %}kr {{ price | dkprice }}{% endif %}</span>
        </div>
        <svg class="course-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
      </div>
      <div class="course-details-wrapper">
        <div class="course-details-inner">
          <div class="course-details-content">
            {% set _cdesc = get_short_description(course) %}
            <div class="premium-course-summary">{{ (_cdesc[:170] + '...') if _cdesc|length > 170 else _cdesc | e }}</div>
            <div class="premium-course-meta">
              <span>{% if course.variants and course.variants|length > 1 %}{{ course.variants|length }} varianter{% else %}1 variant{% endif %}</span>
              <span>{% if course.location %}{{ course.location | e }}{% else %}Online{% endif %}</span>
            </div>
            {% if course.variants %}
            <div class="variant-panel">
              <div class="variant-panel-title">Tider og lokationer</div>
              {% for variant in course.variants[:5] %}
              {% set v_price = (variant.price | string | trim) if variant.price is defined else '' %}
              {% set v_price_val = v_price | float(-1) %}
              <div class="variant-row">
                <span class="variant-date">{{ variant.option2 or 'Dato efter aftale' }}</span>
                <span class="variant-location">{{ variant.option1 or course.location or 'Online' }}</span>
                <span class="variant-price">{% if v_price in ["", "None", "N/A"] or v_price_val == 0 %}Gratis{% elif v_price_val < 0 %}Efter aftale{% else %}kr {{ v_price | dkprice }}{% endif %}</span>
              </div>
              {% endfor %}
              {% if course.variants|length > 5 %}
              <div class="variant-more">+{{ course.variants|length - 5 }} flere muligheder</div>
              {% endif %}
            </div>
            {% endif %}
            <div class="premium-course-actions">
              <a class="course-primary-action" onclick="event.stopPropagation();" href="https://futurematch.dk/products/{{ course.handle }}" target="_blank">Vælg kursus</a>
              <button class="course-secondary-action" onclick='event.stopPropagation(); window.attachProductToChat({{ course.handle | tojson }}, {{ course.title | tojson }})'>Spørg om</button>
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
        c2 = _apply_product_discount(c2)
        enriched.append(c2)
    return render_template_string(MULTIPLE_COURSES_TEMPLATE, courses=enriched, get_short_description=get_short_description, unique_prefix=unique_prefix)


def _demo_enabled():
    """Keep the visual chat demo available locally without exposing it in production by accident."""
    host = request.host.split(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1"} or os.getenv("FUTUREMATCH_ENABLE_DEMO_CHAT") == "1"


def _demo_asset(label, start="#0b6b63", end="#2563eb"):
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='180' viewBox='0 0 320 180'>"
        f"<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop stop-color='{start}'/><stop offset='1' stop-color='{end}'/></linearGradient></defs>"
        "<rect width='320' height='180' rx='24' fill='url(#g)'/>"
        "<circle cx='270' cy='40' r='58' fill='rgba(255,255,255,.16)'/>"
        "<circle cx='42' cy='150' r='72' fill='rgba(255,255,255,.10)'/>"
        "<text x='28' y='100' fill='white' font-family='Inter,Arial,sans-serif' font-size='24' font-weight='700'>"
        f"{label}</text></svg>"
    )
    return "data:image/svg+xml;utf8," + svg.replace("#", "%23").replace(" ", "%20")


def _demo_products():
    return [
        {
            "title": "Agil projektledelse for teams",
            "handle": "demo-agil-projektledelse",
            "vendor": "Futurematch Academy",
            "ai_summary": "Et praktisk forløb for projektledere og team leads, der vil skabe mere fremdrift, bedre prioritering og klarere samarbejde i agile miljøer.",
            "location": "København",
            "image": {"src": _demo_asset("Agil ledelse", "#0b6b63", "#2563eb")},
            "variants": [
                {"price": "12900.00", "option1": "København", "option2": "18. juni 2026"},
                {"price": "12900.00", "option1": "Aarhus", "option2": "24. august 2026"},
            ],
        },
        {
            "title": "AI i HR og rekruttering",
            "handle": "demo-ai-hr-rekruttering",
            "vendor": "PeopleTech Institute",
            "ai_summary": "Lær at bruge AI sikkert i screening, onboarding og kompetencekortlægning uden at miste transparens, etik og menneskelig vurdering.",
            "location": "Online",
            "image": {"src": _demo_asset("AI i HR", "#2563eb", "#7c3aed")},
            "variants": [
                {"price": "7900.00", "option1": "Online", "option2": "3. juli 2026"},
                {"price": "8900.00", "option1": "København", "option2": "21. august 2026"},
            ],
        },
        {
            "title": "Power BI for HR-beslutninger",
            "handle": "demo-power-bi-hr",
            "vendor": "DataLab Learning",
            "ai_summary": "Byg dashboards for fravær, kompetencegab, læringsbudgetter og medarbejderudvikling med et sprog, ledelsen kan handle på.",
            "location": "Aarhus",
            "image": {"src": _demo_asset("HR analytics", "#0891b2", "#0b6b63")},
            "variants": [
                {"price": "9800.00", "option1": "Aarhus", "option2": "11. september 2026"},
                {"price": "8800.00", "option1": "Online", "option2": "2. oktober 2026"},
            ],
        },
    ]


def _demo_message_payload():
    return [
        {
            "role": "user",
            "content": "Vi skal finde kurser til HR og teamledere. Vis noget realistisk med priser og muligheder.",
        },
        {
            "role": "assistant",
            "content": (
                "Her er et realistisk test-scenarie med produktkort, priser, leverandører og næste handlinger. "
                "Kortene kan udvides, og du kan bruge **Spørg om** for at teste produkt-reference flowet."
            ),
            "products_html": render_multi_course_media(_demo_products()),
            "suggestions": [
                "Sammenlign de tre kurser",
                "Hvilket kursus passer bedst til HR?",
                "Vis et enkelt produktkort",
            ],
        },
    ]


def _demo_sse(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _chunk_demo_text(text, size=120):
    for idx in range(0, len(text), size):
        yield {"type": "chunk", "content": text[idx:idx + size]}


@app1_bp.route("/demo")
def demo_chat():
    if not _demo_enabled():
        abort(404)
    return render_template(
        "index.html",
        logged_in_user="Demo HR Manager",
        demo_mode=True,
        demo_messages=_demo_message_payload(),
    )


@app1_bp.route("/demo/ask", methods=["POST"])
def demo_ask():
    if not _demo_enabled():
        abort(404)

    data = request.json or {}
    query = (data.get("query") or "").lower()
    products = _demo_products()

    if "enkelt" in query or "agil" in query or "produktkort" in query:
        text = (
            "Her er et enkelt produktkort, så du kan teste layoutet for én anbefaling. "
            "Det viser titel, leverandør, pris, kort beskrivelse, lokation og CTA’er."
        )
        events = list(_chunk_demo_text(text)) + [
            {"type": "product", "html": render_product_media(products[0])},
            {"type": "suggestions", "items": ["Hvad koster det?", "Hvornår starter kurset?", "Sammenlign med AI i HR"]},
        ]
    elif "sammenlign" in query:
        text = (
            "Kort sammenligning: **Agil projektledelse** er bedst til team leads, "
            "**AI i HR** er stærkest for HR-processer, og **Power BI for HR** passer bedst til ledelsesrapportering. "
            "Hvis målet er hurtig business value, ville jeg starte med AI i HR og derefter Power BI."
        )
        events = list(_chunk_demo_text(text)) + [
            {"type": "product", "html": render_multi_course_media(products)},
            {"type": "suggestions", "items": ["Lav en anbefaling til ledelsen", "Vis budget på 25.000 kr", "Hvilket kursus er online?"]},
        ]
    elif "budget" in query:
        text = (
            "Med et budget på 25.000 kr kan I vælge **AI i HR og rekruttering** plus **Power BI for HR-beslutninger**. "
            "Det giver både procesforbedring og bedre rapportering uden at bruge hele budgettet."
        )
        events = list(_chunk_demo_text(text)) + [
            {"type": "product", "html": render_multi_course_media(products[1:])},
            {"type": "suggestions", "items": ["Lav en HR-plan", "Vis kun online kurser", "Hvad skal vi vælge først?"]},
        ]
    else:
        text = (
            "Demoen svarer uden OpenAI eller databasekald. Her er et anbefalingsflow med produktkort, "
            "klikbare kurser, forslagchips og feedback-knapper, så du kan vurdere selve UI’et."
        )
        events = list(_chunk_demo_text(text)) + [
            {"type": "product", "html": render_multi_course_media(products)},
            {"type": "suggestions", "items": ["Sammenlign de tre kurser", "Vis et enkelt produktkort", "Lav en anbefaling til HR"]},
        ]

    def generate():
        yield _demo_sse({"type": "meta", "message_index": 1})
        for event in events:
            yield _demo_sse(event)
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

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

        # Phase 1.2: Sync feedback to MySQL chatbot_interactions
        try:
            username = session.get('user') or session.get('browser_token', 'anonymous')
            cur = current_app.mysql.connection.cursor()
            cur.execute("""
                UPDATE chatbot_interactions
                SET feedback_rating = %s
                WHERE session_id = %s AND username = %s
                ORDER BY created_at DESC LIMIT 1
            """, (rating, sid, username))
            current_app.mysql.connection.commit()
            cur.close()
        except Exception as fb_err:
            print(f"[Feedback MySQL Sync] {fb_err}")

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
            from app1.user_profile_db import add_completed_course
            title = (payload.get("course_title") or payload.get("course_name") or "").strip()
            if not title:
                return jsonify({"status": "error", "message": "Kursusnavn mangler"}), 400
            add_completed_course(
                logged_in_user,
                course_title=title,
                vendor=payload.get("vendor", ""),
                certificate_note=payload.get("certificate_note", "")
            )
            return _success("Kursus tilføjet")

        elif action == "update_experience":
            from app1.user_profile_db import update_experience
            exp_id = payload.get("id")
            if not exp_id:
                return jsonify({"status": "error", "message": "id mangler"}), 400
            fields = {k: v for k, v in payload.items() if k != "id" and v is not None and str(v).strip()}
            if fields:
                update_experience(logged_in_user, exp_id, **fields)
                return _success("Erfaring opdateret")
            return _success("Ingen ændringer")

        elif action == "update_education":
            from app1.user_profile_db import update_education
            edu_id = payload.get("id")
            if not edu_id:
                return jsonify({"status": "error", "message": "id mangler"}), 400
            fields = {k: v for k, v in payload.items() if k != "id" and v is not None and str(v).strip()}
            if fields:
                update_education(logged_in_user, edu_id, **fields)
                return _success("Uddannelse opdateret")
            return _success("Ingen ændringer")

        elif action == "update_course":
            from app1.user_profile_db import add_completed_course
            title = (payload.get("course_title") or "").strip()
            if not title:
                return jsonify({"status": "error", "message": "Kursusnavn mangler"}), 400
            add_completed_course(
                logged_in_user,
                course_title=title,
                vendor=payload.get("vendor", ""),
                completed_date=payload.get("completed_date"),
                certificate_note=payload.get("certificate_note", "")
            )
            return _success("Kursus opdateret")

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


@app1_bp.route("/nudges")
def nudges():
    """Proactive chatbot nudges for the current user."""
    logged_in_user = session.get("user")
    if not logged_in_user:
        return jsonify({"nudges": []})

    nudge_list = []

    try:
        # 1. Check pending/approved order updates
        try:
            cur = current_app.mysql.connection.cursor()
            cur.execute("""
                SELECT status, product_title FROM course_orders
                WHERE user_id = (SELECT id FROM users WHERE username = %s LIMIT 1)
                  AND status IN ('approved', 'pending_approval')
                ORDER BY updated_at DESC LIMIT 1
            """, (logged_in_user,))
            row = cur.fetchone()
            cur.close()
            if row:
                status, title = row[0], row[1]
                if status == 'approved':
                    nudge_list.append({
                        "type": "order_update",
                        "text": f"Din kursusordre \"{title}\" er blevet godkendt!",
                        "action_url": "/profile"
                    })
                elif status == 'pending_approval':
                    nudge_list.append({
                        "type": "order_update",
                        "text": f"Din kursusordre \"{title}\" afventer godkendelse.",
                        "action_url": "/profile"
                    })
        except Exception as e:
            logging.debug("Nudge order check: %s", e)

        # 2. Profile incomplete check
        if len(nudge_list) < 3:
            try:
                from app1.user_profile_db import get_skills, get_experience, ensure_tables
                ensure_tables()
                skills = get_skills(logged_in_user)
                experience = get_experience(logged_in_user)
                if not skills and not experience:
                    nudge_list.append({
                        "type": "profile_incomplete",
                        "text": "Din profil mangler kompetencer og erfaring. Opdater den for bedre anbefalinger!",
                        "action_url": "/profile"
                    })
            except Exception as e:
                logging.debug("Nudge profile check: %s", e)

        # 3. Skill gaps (company users only)
        if len(nudge_list) < 3 and session.get('company_id'):
            try:
                cur = current_app.mysql.connection.cursor()
                company_id = session['company_id']
                cur.execute("""
                    SELECT cst.skill_name
                    FROM company_skill_targets cst
                    WHERE cst.company_id = %s
                      AND cst.skill_name NOT IN (
                          SELECT esm.skill_name FROM employee_skills_matrix esm
                          WHERE esm.company_id = %s
                            AND esm.employee_id = (SELECT id FROM users WHERE username = %s LIMIT 1)
                            AND esm.current_level >= cst.target_level
                      )
                    LIMIT 1
                """, (company_id, company_id, logged_in_user))
                gap = cur.fetchone()
                cur.close()
                if gap:
                    nudge_list.append({
                        "type": "skill_gap",
                        "text": f"Du mangler kompetencen \"{gap[0]}\" — find et relevant kursus!",
                        "action_url": "/app1/"
                    })
            except Exception as e:
                logging.debug("Nudge skill gap check: %s", e)

    except Exception as e:
        logging.warning("Nudge endpoint error: %s", e)

    return jsonify({"nudges": nudge_list[:3]})


@app1_bp.route("/widget/<token>")
def widget_embed(token):
    """Serve embeddable chat widget for external websites"""
    import MySQLdb.cursors
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT ws.*, c.company_name, c.id as cid
        FROM company_widget_settings ws
        JOIN companies c ON ws.company_id = c.id
        WHERE ws.widget_token = %s AND ws.is_active = 1
    """, (token,))
    widget = cur.fetchone()
    cur.close()

    if not widget:
        return "Widget not found or inactive", 404

    # Check allowed domains via Referer header
    referer = request.headers.get('Referer', '')
    allowed = widget.get('allowed_domains')
    if allowed and isinstance(allowed, str):
        from urllib.parse import urlparse
        ref_domain = urlparse(referer).netloc
        allowed_list = [d.strip().lower() for d in allowed.split(',') if d.strip()]
        if allowed_list and ref_domain and ref_domain.lower() not in allowed_list:
            return "Domain not allowed", 403

    return render_template('app1/widget_chat.html', widget=widget)


@app1_bp.route("/widget/<token>/ask", methods=["POST"])
def widget_ask(token):
    """Handle chat messages from embedded widget — no login required"""
    import MySQLdb.cursors
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT ws.*, c.company_name, c.id as cid
        FROM company_widget_settings ws
        JOIN companies c ON ws.company_id = c.id
        WHERE ws.widget_token = %s AND ws.is_active = 1
    """, (token,))
    widget = cur.fetchone()
    cur.close()

    if not widget:
        return jsonify({"error": "Widget not found"}), 404

    data = request.get_json(silent=True) or {}
    user_query = (data.get("query") or "").strip()
    if not user_query:
        return jsonify({"error": "No query"}), 400

    # Use a widget-specific session ID
    widget_session_id = session.get('widget_session_id')
    if not widget_session_id:
        widget_session_id = f"widget_{uuid.uuid4().hex}"
        session['widget_session_id'] = widget_session_id

    session['session_id'] = widget_session_id

    from app1.agent import handle_agentic_ask
    response = Response(
        stream_with_context(handle_agentic_ask(user_query, session)),
        mimetype="text/event-stream"
    )
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app1_bp.route("/widget/<token>/loader.js")
def widget_loader_js(token):
    """Serve the loader script that creates the chat widget on external sites"""
    import MySQLdb.cursors
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT ws.*, c.company_name
        FROM company_widget_settings ws
        JOIN companies c ON ws.company_id = c.id
        WHERE ws.widget_token = %s AND ws.is_active = 1
    """, (token,))
    widget = cur.fetchone()
    cur.close()

    if not widget:
        return "/* Widget not found */", 404, {'Content-Type': 'application/javascript'}

    # Extract settings
    primary = widget.get('theme_primary_color', '#0f766e')
    text_color = widget.get('theme_text_color', '#FFFFFF')
    position = widget.get('position', 'bottom-right') or 'bottom-right'
    size = widget.get('widget_size', 'medium')
    title = widget.get('widget_title', 'Kursusrådgiver')

    size_map = {'small': 350, 'medium': 400, 'large': 450}
    width = size_map.get(size, 400)
    height = int(width * 1.4)

    # Position CSS
    pos_parts = position.split('-')
    vert = pos_parts[0] if pos_parts else 'bottom'
    horiz = pos_parts[1] if len(pos_parts) > 1 else 'right'
    pos_css = f"{vert}:20px;{horiz}:20px;"
    frame_pos = f"{vert}:{80}px;{horiz}:20px;"

    iframe_url = request.url_root.rstrip('/') + url_for('app1.widget_embed', token=token)

    js = f"""(function(){{
  if(window.__ailead_widget)return;
  window.__ailead_widget=true;
  var s=document.createElement('style');
  s.textContent=`
    #ailead-widget-btn{{
      position:fixed;{pos_css}z-index:99998;
      width:56px;height:56px;border-radius:50%;border:none;
      background:{primary};color:{text_color};cursor:pointer;
      box-shadow:0 4px 16px rgba(0,0,0,0.2);
      display:flex;align-items:center;justify-content:center;
      transition:transform 0.2s,box-shadow 0.2s;
    }}
    #ailead-widget-btn:hover{{transform:scale(1.08);box-shadow:0 6px 24px rgba(0,0,0,0.25);}}
    #ailead-widget-btn svg{{width:26px;height:26px;fill:currentColor;}}
    #ailead-widget-frame{{
      position:fixed;{frame_pos}z-index:99999;
      width:{width}px;height:{height}px;max-height:calc(100vh - 100px);
      border:none;border-radius:16px;
      box-shadow:0 8px 40px rgba(0,0,0,0.18);
      display:none;overflow:hidden;
      transition:opacity 0.2s,transform 0.2s;
    }}
    #ailead-widget-frame.open{{display:block;}}
    @media(max-width:500px){{
      #ailead-widget-frame{{width:calc(100vw - 20px);height:calc(100vh - 80px);
        left:10px!important;right:10px!important;bottom:70px!important;top:auto!important;border-radius:12px;}}
    }}
  `;
  document.head.appendChild(s);
  var btn=document.createElement('button');
  btn.id='ailead-widget-btn';
  btn.title='{title}';
  btn.innerHTML='<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>';
  document.body.appendChild(btn);
  var frame=document.createElement('iframe');
  frame.id='ailead-widget-frame';
  frame.src='{iframe_url}';
  frame.allow='clipboard-write';
  document.body.appendChild(frame);
  var open=false;
  btn.addEventListener('click',function(){{
    open=!open;
    frame.classList.toggle('open',open);
    btn.innerHTML=open?'<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>':'<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>';
  }});
  window.addEventListener('message',function(e){{
    if(e.data==='ailead-close'){{open=false;frame.classList.remove('open');
      btn.innerHTML='<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>';
    }}
  }});
}})();"""

    resp = Response(js, mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


app.register_blueprint(app1_bp, url_prefix='/app1')

if __name__ == "__main__":
    app.run(debug=False)
