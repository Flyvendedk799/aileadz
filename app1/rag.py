import json
import math
import os
import openai
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
openai.api_key = os.environ.get("OPENAI_API_KEY")

AUGMENTED_FILE = os.path.join(os.path.dirname(__file__), "shopify_products_augmented.json")
_augmented_cache = None

def load_augmented_products():
    """Load the pre-computed products with embeddings into memory."""
    global _augmented_cache
    if _augmented_cache is None:
        try:
            with open(AUGMENTED_FILE, "r", encoding="utf-8") as f:
                _augmented_cache = json.load(f)
        except Exception as e:
            print(f"Error loading augmented products: {e}")
            _augmented_cache = []
    return _augmented_cache

def cosine_similarity(v1, v2):
    """Compute cosine similarity between two numeric vectors."""
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_a = math.sqrt(sum(a * a for a in v1))
    norm_b = math.sqrt(sum(b * b for b in v2))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)

def get_query_embedding(query_text):
    """Get the embedding vector for the user query."""
    try:
        response = openai.embeddings.create(
            input=query_text,
            model="text-embedding-3-small"
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"Error getting query embedding: {e}")
        return None

def semantic_search_courses(query, limit=5, min_score=0.4):
    """
    Given a user query (e.g., "Ledelse kursus i Aarhus"), find the top matching
    products based on vector cosine similarity.

    Returns:
        list of products on success,
        dict with "error" key on failure: {"error": "embedding_failed"|"index_not_loaded"}
    """
    products = load_augmented_products()
    if not products:
        return {"error": "index_not_loaded", "message": "Produktindekset er ikke indlæst."}

    query_vector = get_query_embedding(query)
    if not query_vector:
        return {"error": "embedding_failed", "message": "Kunne ikke oprette embedding for søgningen."}

    scored_products = []
    for product in products:
        product_vector = product.get("embedding")
        if not product_vector:
            continue

        score = cosine_similarity(query_vector, product_vector)
        if score >= min_score:
            scored_products.append((score, product))

    # Sort descendants by score
    scored_products.sort(key=lambda x: x[0], reverse=True)

    # Return top N products
    return [item[1] for item in scored_products[:limit]]
