import json
import os
import sys
import re
import openai
from dotenv import load_dotenv
import time

# Load environment variables (to get OPENAI_API_KEY)
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

# Ensure API key is set
openai.api_key = os.environ.get("OPENAI_API_KEY")
if not openai.api_key:
    print("Error: OPENAI_API_KEY environment variable not found.")
    exit(1)

INPUT_FILE = os.path.join(os.path.dirname(__file__), "shopify_products_all_pages.json")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "shopify_products_augmented.json")

# Tags to exclude from embedding context (too generic or region-based)
_EXCLUDED_TAG_PREFIXES = {"region:", "by:", "land:"}
_EXCLUDED_TAGS = {"kursus", "kurser", "uddannelse", "training", "course", "denmark", "danmark"}

BATCH_SIZE = 100  # Embeddings per API call


def get_price_tier(price_str):
    """Convert a price string to a Danish price tier label."""
    try:
        price = float(price_str)
    except (ValueError, TypeError):
        return ""
    if price == 0:
        return "Gratis"
    elif price < 5000:
        return "Budget: under 5000 kr"
    elif price <= 15000:
        return "Standard: 5000-15000 kr"
    else:
        return "Premium: over 15000 kr"


def get_filtered_tags(product):
    """Extract relevant category tags, filtering out generic/region tags."""
    all_tags = product.get("tags", [])
    if isinstance(all_tags, str):
        all_tags = [t.strip() for t in all_tags.split(",")]
    filtered = []
    for tag in all_tags:
        tag_lower = tag.lower().strip()
        if tag_lower in _EXCLUDED_TAGS:
            continue
        if any(tag_lower.startswith(p) for p in _EXCLUDED_TAG_PREFIXES):
            continue
        if tag.strip():
            filtered.append(tag.strip())
    return filtered


def extract_structured_metadata(product):
    """Extract structured metadata from body_html using GPT-4o-mini.
    Returns a dict with: duration_days, difficulty, prerequisites, target_audience,
    certification, language, includes."""
    description = product.get("body_html", "")
    title = product.get("title", "")
    tags = get_filtered_tags(product)
    product_type = product.get("product_type", "")
    vendor = product.get("vendor", "")

    # Build context for extraction
    context_parts = [f"Kursusnavn: {title}"]
    if vendor:
        context_parts.append(f"Udbyder: {vendor}")
    if product_type:
        context_parts.append(f"Produkttype: {product_type}")
    if tags:
        context_parts.append(f"Tags: {', '.join(tags[:8])}")

    # Clean HTML for the description
    clean_desc = ""
    if description:
        clean_desc = re.sub(r'<[^>]+>', ' ', description)
        clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()[:1500]
        context_parts.append(f"Beskrivelse: {clean_desc}")

    if not clean_desc and not tags:
        # Not enough info to extract metadata
        return {}

    context = "\n".join(context_parts)

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """Udtræk struktureret metadata fra denne kursusbeskrivelse.
Svar PRÆCIS som JSON med disse felter (udelad felter du ikke kan bestemme):
{
  "duration_days": <antal dage som tal, f.eks. 1, 2, 3, 5. Brug 0.5 for halv dag>,
  "difficulty": "<beginner|intermediate|advanced>",
  "prerequisites": ["forudsætning 1", "forudsætning 2"],
  "target_audience": ["målgruppe 1", "målgruppe 2"],
  "certification": "<certificeringsnavn hvis relevant, ellers udelad>",
  "language": "<dansk|engelsk|begge>",
  "includes": ["hvad der er inkluderet, f.eks. eksamen, materiale, frokost, certifikat"],
  "primary_topic": "<det primære emne kurset handler om i 2-4 ord, f.eks. 'projektledelse', 'Excel grundlæggende', 'ITIL certificering', 'personlig udvikling'>"
}
Vær konservativ — kun medtag info der klart fremgår af teksten. Gæt ikke.
primary_topic skal altid udfyldes baseret på titel og indhold."""},
                {"role": "user", "content": context}
            ],
            temperature=0.1,
            max_tokens=300
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        metadata = json.loads(raw)
        # Validate types
        if not isinstance(metadata, dict):
            return {}
        # Normalize difficulty
        if metadata.get("difficulty") not in ("beginner", "intermediate", "advanced", None):
            metadata.pop("difficulty", None)
        return metadata
    except Exception as e:
        print(f"  [X] Error extracting metadata: {e}")
        return {}


def generate_summary(product):
    """Generate a short Danish summary from the HTML description."""
    description = product.get("body_html", "")
    title = product.get("title", "")
    tags = get_filtered_tags(product)

    # For products with very weak body_html, supplement with title + tags
    has_weak_body = not description or len(description.strip()) < 100
    if has_weak_body:
        supplement = f"Kursusnavn: {title}."
        if tags:
            supplement += f" Kategorier: {', '.join(tags[:5])}."
        prompt_content = supplement + ("\n\n" + description if description.strip() else "")
    else:
        prompt_content = description

    if not prompt_content.strip():
        return "Ingen beskrivelse tilgængelig."

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Sammenfat følgende kursusbeskrivelse til en kort, attraktiv og præcis tekst på dansk (maks 120 ord). Inkluder hvem kurset henvender sig til og hvad man lærer. Svaret må kun indeholde selve teksten."},
                {"role": "user", "content": prompt_content}
            ],
            temperature=0.3,
            max_tokens=250
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"  [X] Error generating summary: {e}")
        # Fallback to pure substring
        clean_text = re.sub(r'<[^>]+>', ' ', description)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        return (clean_text[:250] + '...') if len(clean_text) > 250 else clean_text


def build_embedding_context(product, summary):
    """Build a rich embedding context string including all searchable dimensions."""
    title = product.get("title", "Ukendt Titel")
    vendor = product.get("vendor", "Ukendt Udbyder")
    product_type = product.get("product_type", "")
    variants = product.get("variants", [])

    # Primary topic first — this is the most important signal for what the course is ABOUT
    meta = product.get("structured_metadata", {})
    primary_topic = meta.get("primary_topic", "")

    parts = []
    if primary_topic:
        parts.append(f"Primært emne: {primary_topic}")
    parts.extend([f"Titel: {title}", f"Udbyder: {vendor}", f"Beskrivelse: {summary}"])

    # Product type
    if product_type:
        parts.append(f"Type: {product_type}")

    # Category tags
    tags = get_filtered_tags(product)
    if tags:
        parts.append(f"Kategorier: {', '.join(tags)}")

    # Price tier
    if variants:
        price_str = variants[0].get("price", "")
        tier = get_price_tier(price_str)
        if tier:
            parts.append(f"Pris: {price_str} kr ({tier})")

    # Locations from variants
    locations = set()
    for v in variants:
        loc = v.get("option1", "")
        if loc:
            locations.add(loc.lower().strip())
    if locations:
        parts.append(f"Lokationer: {', '.join(locations)}")

    # Dates from variants (first 5)
    dates = [v.get("option2") for v in variants if v.get("option2")][:5]
    if dates:
        parts.append(f"Datoer: {', '.join(dates)}")

    # 4.2: Structured metadata for richer embeddings
    meta = product.get("structured_metadata", {})
    if meta.get("duration_days"):
        parts.append(f"Varighed: {meta['duration_days']} dag(e)")
    if meta.get("difficulty"):
        diff_labels = {"beginner": "Begynder", "intermediate": "Mellemniveau", "advanced": "Avanceret"}
        parts.append(f"Niveau: {diff_labels.get(meta['difficulty'], meta['difficulty'])}")
    if meta.get("certification"):
        parts.append(f"Certificering: {meta['certification']}")
    if meta.get("target_audience"):
        parts.append(f"Målgruppe: {', '.join(meta['target_audience'][:3])}")
    if meta.get("includes"):
        parts.append(f"Inkluderer: {', '.join(meta['includes'][:4])}")

    return "\n".join(parts)


def generate_embeddings_batch(texts):
    """Generate embeddings for a batch of texts using text-embedding-3-large (1024 dims)."""
    try:
        response = openai.embeddings.create(
            input=texts,
            model="text-embedding-3-large",
            dimensions=1024
        )
        return [item.embedding for item in response.data]
    except Exception as e:
        print(f"  [X] Batch embedding error: {e}")
        return [None] * len(texts)


def generate_embedding(text):
    """Generate text embedding for a single text (fallback)."""
    try:
        response = openai.embeddings.create(
            input=text,
            model="text-embedding-3-large",
            dimensions=1024
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"  [X] Error generating embedding: {e}")
        return None


def main():
    skip_existing = "--skip-existing" in sys.argv

    if not os.path.exists(INPUT_FILE):
        print(f"Input file not found: {INPUT_FILE}")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        products = json.load(f)

    # Load existing augmented data if skip_existing
    existing_data = {}
    if skip_existing and os.path.exists(OUTPUT_FILE):
        print("Loading existing augmented data for incremental rebuild...")
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
        for p in existing:
            handle = p.get("handle")
            if handle and p.get("embedding"):
                existing_data[handle] = p
        print(f"  Found {len(existing_data)} existing products with embeddings.")

    print(f"Loaded {len(products)} products. Starting augmentation process...\n")
    augmented_products = []
    pending_embeddings = []  # (index_in_augmented, context_string)

    for idx, product in enumerate(products, 1):
        title = product.get("title", "Ukendt Titel")
        handle = product.get("handle", "")
        print(f"[{idx}/{len(products)}] Processing: {title.encode('ascii', errors='replace').decode()}")

        # Skip if existing and --skip-existing
        if skip_existing and handle in existing_data:
            print("  -> Skipping (already exists)")
            augmented_products.append(existing_data[handle])
            continue

        # 1. Generate Summary
        summary = generate_summary(product)
        product["ai_summary"] = summary

        # 1b. Extract structured metadata (4.2)
        metadata = extract_structured_metadata(product)
        if metadata:
            product["structured_metadata"] = metadata
            print(f"  -> Metadata: {json.dumps(metadata, ensure_ascii=False)[:120]}")

        # 2. Build rich embedding context (now includes structured metadata)
        context_string = build_embedding_context(product, summary)
        product["embedding_context"] = context_string

        # 3. Queue for batch embedding
        augmented_products.append(product)
        pending_embeddings.append((len(augmented_products) - 1, context_string))

        # Process embeddings in batches
        if len(pending_embeddings) >= BATCH_SIZE:
            print(f"  -> Generating batch of {len(pending_embeddings)} embeddings...")
            texts = [ctx for _, ctx in pending_embeddings]
            embeddings = generate_embeddings_batch(texts)
            for (aug_idx, _), emb in zip(pending_embeddings, embeddings):
                if emb:
                    augmented_products[aug_idx]["embedding"] = emb
            pending_embeddings = []
            time.sleep(0.5)  # Rate limit courtesy

        # Sleep slightly between summary generations
        time.sleep(0.3)

    # Process remaining embeddings
    if pending_embeddings:
        print(f"\n  -> Generating final batch of {len(pending_embeddings)} embeddings...")
        texts = [ctx for _, ctx in pending_embeddings]
        embeddings = generate_embeddings_batch(texts)
        for (aug_idx, _), emb in zip(pending_embeddings, embeddings):
            if emb:
                augmented_products[aug_idx]["embedding"] = emb

    # Save outputs
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(augmented_products, f, ensure_ascii=False, indent=2)

    products_with_embeddings = sum(1 for p in augmented_products if p.get("embedding"))
    print(f"\nSuccessfully augmented {len(augmented_products)} products ({products_with_embeddings} with embeddings).")
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
