import json
import os
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

def generate_summary(description):
    """Generate a short Danish summary from the HTML description."""
    if not description or description.strip() == "":
        return "Ingen beskrivelse tilgængelig."
    
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Sammenfat følgende kursusbeskrivelse til en kort, attraktiv og præcis tekst på dansk (maks 50 ord). Svaret må kun indeholde selve den korte tekst."},
                {"role": "user", "content": description}
            ],
            temperature=0.3,
            max_tokens=100
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"  [X] Error generating summary: {e}")
        # Fallback to pure substring
        clean_text = description.replace('<p>', '').replace('</p>', '').replace('<br>', ' ')
        return (clean_text[:150] + '...') if len(clean_text) > 150 else clean_text

def generate_embedding(text):
    """Generate text embeddings using OpenAI's small embedding model."""
    try:
        response = openai.embeddings.create(
            input=text,
            model="text-embedding-3-small"
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"  [X] Error generating embedding: {e}")
        return None

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Input file not found: {INPUT_FILE}")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        products = json.load(f)

    print(f"Loaded {len(products)} products. Starting augmentation process...\n")
    augmented_products = []

    for idx, product in enumerate(products, 1):
        title = product.get("title", "Ukendt Titel")
        print(f"[{idx}/{len(products)}] Processing: {title.encode('ascii', errors='replace').decode()}")
        
        # 1. Generate Summary
        description = product.get("body_html", "")
        summary = generate_summary(description)
        product["ai_summary"] = summary

        # 2. Extract General Context (Location, Vendor, Price bounds)
        vendor = product.get("vendor", "Ukendt Udbyder")
        
        # Build embedding context document
        context_string = f"Titel: {title}\nUdbyder: {vendor}\nBeskrivelse: {summary}"
        
        # Inject Locations from variants to enrich the embedding context
        locations = set()
        for variant in product.get("variants", []):
            if variant.get("option1"):
                locations.add(variant.get("option1").lower().strip())
        
        if locations:
            context_string += f"\nLokationer: {', '.join(locations)}"

        product["embedding_context"] = context_string
        
        # 3. Generate Embedding
        embedding = generate_embedding(context_string)
        if embedding:
            product["embedding"] = embedding
        
        augmented_products.append(product)
        
        # Sleep slightly to avoid strict rate-limits if using a free tier API
        time.sleep(0.5)

    # Save outputs
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(augmented_products, f, ensure_ascii=False, indent=2)

    print(f"\nSuccessfully augmented {len(augmented_products)} products.")
    print(f"Saved to: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
