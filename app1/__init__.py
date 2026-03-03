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
<div style="background: #fff; border-radius: 12px; overflow: hidden; font-family: 'Inter', Arial, sans-serif; color: #333; box-shadow: 0 8px 24px rgba(0,0,0,0.08); width: 100%; max-width: 450px; margin: 0 auto;">
  
  {# Image Header Area #}
  <div style="background-color: #f6efe8; padding: 30px; display: flex; justify-content: center; align-items: center; position: relative;">
    {% if product.image_url %}
      <img src="{{ product.image_url | e }}" alt="{{ product.title | e }}" style="max-height: 120px; object-fit: contain;">
    {% else %}
      <div style="height: 120px; display: flex; align-items: center; justify-content: center; color: #888;">Ingen Billede</div>
    {% endif %}
  </div>

  {# Details Area #}
  <div style="padding: 24px;">
    <h3 style="margin: 0 0 4px 0; font-size: 20px; font-weight: 700; color: #1a1a1a;">{{ product.title | e }}</h3>
    <p style="margin: 0 0 20px 0; font-size: 14px; color: #666;">Kursus</p>
    
    <div style="display: flex; gap: 40px; margin-bottom: 24px;">
      <div>
        <div style="display: flex; align-items: center; gap: 6px; color: #1a1a1a; font-weight: 600; font-size: 13px; margin-bottom: 4px;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
          Varighed
        </div>
        <div style="color: #666; font-size: 13px;">
          {% if product.variants and product.variants|length > 1 %}
            Flere muligheder
          {% else %}
            Ikke angivet
          {% endif %}
        </div>
      </div>
      <div>
        <div style="display: flex; align-items: center; gap: 6px; color: #1a1a1a; font-weight: 600; font-size: 13px; margin-bottom: 4px;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>
          Lokation
        </div>
        <div style="color: #666; font-size: 13px;">
          {% if product.location %}
            {{ product.location | e }}
          {% else %}
            Online / Flere
          {% endif %}
        </div>
      </div>
    </div>

    {# Price Floating Card #}
    <div style="background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); margin-bottom: 24px; font-weight: 600; font-size: 16px;">
      {% if product.price in ["0", "0.00"] %}
        Gratis
      {% else %}
        kr {{ product.price | e }}
      {% endif %}
    </div>
    
    {# Short desc #}
    {% if product.description %}
    <div style="font-size: 13px; color: #555; line-height: 1.5; margin-bottom: 24px;">
      {{ get_short_description(product) | e }}
    </div>
    {% endif %}

    {# Floating Action Button #}
    <div style="display: flex; justify-content: center;">
      <button onclick="window.open('{{ product.url | e }}', '_blank')" style="background-color: #1a1a1a; color: #fff; border: none; padding: 12px 24px; border-radius: 20px; font-size: 14px; font-weight: 600; cursor: pointer; transition: transform 0.2s ease;">
        Vælg kursus
      </button>
    </div>
  </div>
</div>
"""

def render_product_media(product):
    return render_template_string(PRODUCT_MEDIA_TEMPLATE, product=product, get_short_description=get_short_description)

MULTIPLE_COURSES_TEMPLATE = """
<div style="display: flex; flex-direction: column; gap: 12px; max-width: 450px;">
  {% for course in courses %}
    <div onclick="window.open('{{ course.url | e }}', '_blank')" style="background: #fff; border: 1px solid #eaeaea; border-radius: 10px; padding: 16px; display: flex; align-items: center; gap: 16px; cursor: pointer; transition: transform 0.2s ease, box-shadow 0.2s ease; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
      
      {# Left Icon/Logo Placeholder Box #}
      <div style="background-color: #f8f6f2; border-radius: 8px; width: 60px; height: 60px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; overflow: hidden; position: relative;">
        {% if course.image_url %}
            <img src="{{ course.image_url | e }}" style="max-width: 80%; max-height: 80%; object-fit: contain;">
        {% else %}
            <div style="font-size: 10px; color: #aaa;">Logo</div>
        {% endif %}
      </div>
      
      {# Right Details #}
      <div style="flex-grow: 1; min-width: 0; font-family: 'Inter', Arial, sans-serif;">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 4px;">
            <h4 style="margin: 0; font-size: 14px; font-weight: 600; color: #1a1a1a; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 70%;">
                {{ course.title | e }}
            </h4>
            <div style="font-size: 13px; font-weight: 600; color: #1a1a1a; white-space: nowrap;">
                {% if course.price in ['0', '0.00'] %}
                    Gratis
                {% else %}
                    kr {{ course.price | e }}
                {% endif %}
            </div>
        </div>
        <div style="display: flex; justify-content: space-between; font-size: 12px; color: #888;">
            <div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{{ course.vendor | e }}</div>
            {% if course.price not in ['0', '0.00'] %}
                <div>ekskl. moms</div>
            {% else %}
                <div>&nbsp;</div>
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
                last_handle = session.get("last_product_handle")
                extracted_name = extract_product_from_query(query_lower, last_handle)
                matched_product = None

                if extracted_name:
                    # check if the extracted name was actually our last_handle fallback
                    if extracted_name == last_handle:
                         matched_product = next((p for p in product_list if p.get("handle") == last_handle), None)
                    else:
                        match_extracted = get_best_match(extracted_name, titles)
                        match_full = get_best_match(query_lower, titles)
                        best_match = match_extracted if (match_extracted and (not match_full or match_extracted[1] >= match_full[1])) else match_full

                        if best_match:
                            matched_product = next((p for p in product_list if p["title_lower"] == best_match[0]), None)
                            
                    if matched_product:
                        session["last_product_handle"] = matched_product.get("handle")
                else:
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
