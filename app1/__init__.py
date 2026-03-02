from flask import Flask, render_template, Blueprint, render_template_string, request, jsonify, session
from markupsafe import escape
import json
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

@app1_bp.route('/')
def index():
    return render_template('index.html')

SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL", "futurematch.dk")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
else:
    print("WARNING: OPENAI_API_KEY environment variable is not set.")

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

def extract_product_from_query(query):
    prompt = (
        f"Extract the product name from the following query. "
        f"Return only the product name (no additional text) or an empty string if none is found:\n\n"
        f"Query: \"{query}\""
    )
    try:
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are an assistant that extracts product names from user queries."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
        )
        extracted_name = response.choices[0].message.content.strip()
        return extracted_name
    except Exception as e:
        print(f"Error extracting product name using NLP: {e}")
        return ""

def combined_score(a, b):
    return max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b))

def get_best_match(query, product_titles):
    best_match = process.extractOne(query, product_titles, scorer=combined_score)
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
    pid = product.get("handle")
    if pid in _short_description_cache:
        return _short_description_cache[pid]
    full_desc = product.get("description", "")
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
            return f"Prisen for {escape(product['title'])} er {escape(product['price'])} kr."

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
<div style="background-color: #f7f5f2; border-radius: 12px; overflow: hidden; width: 100%; max-width: 400px; margin: 0 auto; box-shadow: 0 4px 12px rgba(0,0,0,0.1); font-family: sans-serif; text-align: left; color: #333;">
  
  {# Top Graphic Area #}
  <div style="background-color: #fae1cd; padding: 40px 20px; display: flex; justify-content: center; align-items: center; position: relative; min-height: 120px;">
    {% if product.image_url %}
        <img src="{{ product.image_url | e }}" alt="{{ product.title | e }}" style="max-height: 80px; width: auto; object-fit: contain;">
    {% else %}
        <div style="font-size: 24px; font-weight: bold; color: #333;">{{ product.vendor | e }}</div>
    {% endif %}
  </div>

  {# Content Area #}
  <div style="padding: 24px;">
    <h2 style="font-size: 20px; margin: 0 0 8px 0; font-weight: 600; line-height: 1.3;">{{ product.title | e }}</h2>
    <div style="font-size: 14px; color: #666; margin-bottom: 24px;">Kursus</div>

    {# Grid for Details #}
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px;">
      
      {# Duration / Date block (using first date for now) #}
      <div>
        <div style="display: flex; align-items: center; font-size: 12px; color: #666; margin-bottom: 4px;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right: 6px;"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
          Næste startdato
        </div>
        <div style="font-size: 14px; font-weight: 500;">
          {% if product.variants and product.variants|length > 0 and product.variants[0].option2 %}
            {{ product.variants[0].option2 | e }}
          {% else %}
             TBA
          {% endif %}
        </div>
      </div>

      {# Location block #}
      <div>
        <div style="display: flex; align-items: center; font-size: 12px; color: #666; margin-bottom: 4px;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right: 6px;"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>
          Sted
        </div>
        <div style="font-size: 14px; font-weight: 500;">
          {% if product.variants and product.variants|length > 0 and product.variants[0].option1 %}
            {{ product.variants[0].option1 | e }}
          {% elif product.location %}
             {{ product.location | e }}
          {% else %}
             Online / TBA
          {% endif %}
        </div>
      </div>

    </div>

    {# Price Block #}
    <div style="background-color: #fff; border-radius: 8px; padding: 16px; margin-bottom: 24px; display: flex; align-items: baseline;">
      <span style="font-weight: 600; font-size: 18px;">
        {% if product.price in ["0", "0.00"] %}
          Efter aftale
        {% else %}
          kr {{ product.price | e }}
        {% endif %}
      </span>
    </div>

    {# Description snippet #}
    {% if product.description %}
    <div style="font-size: 13px; color: #555; line-height: 1.5; margin-bottom: 24px;">
      {{ get_short_description(product) | e }}
    </div>
    {% endif %}

    {# CTA #}
    <div style="text-align: center;">
      <button onclick="window.open('{{ product.url | e }}', '_blank')" style="background-color: #222; color: #fff; border: none; border-radius: 24px; padding: 12px 32px; font-size: 14px; font-weight: 500; cursor: pointer; transition: background-color 0.2s;">
        Vælg kursus
      </button>
    </div>

  </div>
</div>
"""

def render_product_media(product):
    return render_template_string(PRODUCT_MEDIA_TEMPLATE, product=product, get_short_description=get_short_description)

MULTIPLE_COURSES_TEMPLATE = """
<div style="display: flex; flex-direction: column; gap: 12px; max-width: 600px; margin: 10px auto; font-family: sans-serif;">
  {% for course in courses %}
  <div style="display: flex; background: #fff; border: 1px solid #eaeaea; border-radius: 8px; overflow: hidden; text-align: left; cursor: pointer; transition: box-shadow 0.2s; color: #333;" onclick="window.open('{{ course.url | e }}', '_blank')" onmouseover="this.style.boxShadow='0 4px 12px rgba(0,0,0,0.08)'" onmouseout="this.style.boxShadow='none'">
    
    {# Left graphic block #}
    <div style="width: 80px; min-width: 80px; background-color: #fcf9f5; display: flex; justify-content: center; align-items: center; padding: 10px; border-right: 1px solid #eaeaea;">
      {% if course.image_url %}
        <img src="{{ course.image_url | e }}" alt="{{ course.vendor | e }}" style="max-width: 100%; max-height: 40px; object-fit: contain;">
      {% else %}
        <div style="font-size: 12px; font-weight: bold; color: #555; text-align: center;">{{ course.vendor | e }}</div>
      {% endif %}
    </div>

    {# Middle content block #}
    <div style="flex-grow: 1; padding: 12px 16px; display: flex; flex-direction: column; justify-content: center;">
      <div style="font-size: 14px; font-weight: 600; margin-bottom: 4px; line-height: 1.2;">{{ course.title | e }}</div>
      <div style="font-size: 12px; color: #777;">{{ course.vendor | e }}</div>
    </div>

    {# Right price block #}
    <div style="padding: 12px 16px; display: flex; flex-direction: column; align-items: flex-end; justify-content: center; min-width: 100px;">
      <div style="font-weight: 600; font-size: 14px;">
        {% if course.price in ['0', '0.00'] %}
          Gratis
        {% else %}
          kr {{ course.price | e }}
        {% endif %}
      </div>
      <div style="font-size: 10px; color: #999; margin-top: 2px;">
        {% if course.price in ['0', '0.00'] %}
          Intern kursus
        {% else %}
          ekskl. moms
        {% endif %}
      </div>
    </div>
    
  </div>
  {% endfor %}
</div>
"""

def render_multi_course_media(courses):
    unique_prefix = str(uuid.uuid4())[:8]
    return render_template_string(MULTIPLE_COURSES_TEMPLATE, courses=courses, get_short_description=get_short_description, unique_prefix=unique_prefix)

@app1_bp.route("/ask", methods=["POST"])
def ask():
    try:
        user_query = request.json.get("query", "").strip()
        query_lower = user_query.lower()

        course_keywords = [
            "kursus", "uddannelse", "træning", "certificering", "læring",
            "prisen på", "prisen for", "pris", "koster", "hvad koster",
            "dato", "sted", "udbyder", "leverandør", "hvornår", "tid", "tidspunkt",
            "afholdt", "hvor", "anbefal", "forslå", "find", "fortæl"
        ]
        is_course_query = any(keyword in query_lower for keyword in course_keywords)

        if is_course_query:
            products = load_products()
            if not products:
                return jsonify({"answers": [
                    {"type": "text", "content": "Undskyld, den information kunne jeg ikke finde. Prøv venligst et andet spørgsmål."}
                ]}), 200

            product_list = []
            for product in products:
                location, date_val = extract_location_and_date(product)
                product_list.append({
                    "title": product.get("title", ""),
                    "title_lower": product.get("title", "").lower(),
                    "description": product.get("body_html", ""),
                    "price": product.get("variants", [{}])[0].get("price", "N/A"),
                    "url": f"https://{SHOPIFY_STORE_URL}/products/{product.get('handle', '')}",
                    "image_url": (product.get("images", [{}])[0].get("src", None)),
                    "date": date_val,
                    "location": location,
                    "vendor": product.get("vendor", "Ukendt"),
                    "variants": product.get("variants", []),
                    "handle": product.get("handle", "")
                })

            titles = [p["title_lower"] for p in product_list]

            multi_course_flag = False
            requested_count = 0
            m = re.search(r'\b(\d+)\b', query_lower)
            if m:
                requested_count = int(m.group(1))
                if requested_count > 1:
                    multi_course_flag = True
            if any(word in query_lower for word in ["flere", "anbefal", "forslå", "find"]):
                multi_course_flag = True

            # If user requests multiple courses
            if multi_course_flag:
                if requested_count < 2:
                    requested_count = 3
                suggested_courses = suggest_courses(query_lower, product_list, requested_count)
                if not suggested_courses:
                    return jsonify({"answers": [
                        {"type": "text", "content": "Undskyld, den information kunne jeg ikke finde. Prøv venligst et andet spørgsmål."}
                    ]}), 200

                multi_course_media = render_multi_course_media(suggested_courses)
                reasoning_message = (
                    "Jeg har valgt disse kurser, da de matcher dine kriterier. "
                    "Er du interesseret i en specifik lokation eller startdato? Fortæl mig dine præferencer, "
                    "så finder jeg den perfekte løsning til dig."
                )
                messages = [
                    {"type": "text", "content": reasoning_message},
                    {"type": "product", "content": multi_course_media}
                ]
                return jsonify({"answers": messages}), 200

            else:
                extracted_name = extract_product_from_query(query_lower)
                matched_product = None

                if extracted_name:
                    match_extracted = get_best_match(extracted_name, titles)
                    match_full = get_best_match(query_lower, titles)
                    best_match = match_extracted if (match_extracted and (not match_full or match_extracted[1] >= match_full[1])) else match_full

                    if best_match:
                        matched_product = next((p for p in product_list if p["title_lower"] == best_match[0]), None)
                        if matched_product:
                            session["last_product_handle"] = matched_product.get("handle")
                else:
                    last_handle = session.get("last_product_handle")
                    if last_handle:
                        matched_product = next((p for p in product_list if p.get("handle") == last_handle), None)

                if matched_product:
                    details = get_course_detail(query_lower, matched_product)
                    product_media = render_product_media(matched_product)
                    messages = []
                    if details and details.strip():
                        messages.append({"type": "text", "content": details})
                    messages.append({"type": "product", "content": product_media})
                    return jsonify({"answers": messages}), 200
                else:
                    return jsonify({"answers": [
                        {"type": "text", "content": "Undskyld, den information kunne jeg ikke finde. Prøv venligst et andet spørgsmål."}
                    ]}), 200

        else:
            # For non-course queries, use the generic GPT response.
            bot_reply = ""
            try:
                response = openai.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role": "system", "content": "Du er en ekspert i kurser og uddannelse, og hjælper brugeren med at finde de bedste kurser baseret på data fra JSON-filen. Husk at integrere svar, der kan håndtere flere varianter og give et klart overblik."},
                        {"role": "user", "content": user_query}
                    ]
                )
                bot_reply = response.choices[0].message.content
            except Exception as e:
                print(f"Error calling GPT-4 API: {e}")
                try:
                    response = openai.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": "Du er en ekspert i kurser og uddannelse, og hjælper brugeren med at finde de bedste kurser baseret på data fra JSON-filen. Husk at integrere svar, der kan håndtere flere varianter og give et klart overblik."},
                            {"role": "user", "content": user_query}
                        ]
                    )
                    bot_reply = response.choices[0].message.content
                except Exception as e2:
                    print(f"Error calling GPT-3.5-turbo API: {e2}")
                    bot_reply = "Der opstod en fejl under behandlingen af din forespørgsel."
            return jsonify({"answers": [{"type": "text", "content": bot_reply}]}), 200
    except Exception as ex:
        print(f"Unexpected error: {ex}")
        return jsonify({"answers": [
            {"type": "text", "content": "Der opstod en uventet fejl. Prøv venligst igen."}
        ]}), 500

app.register_blueprint(app1_bp, url_prefix='/app1')

if __name__ == "__main__":
    app.run(debug=False)
