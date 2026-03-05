"""
RAG module with hybrid search: Vector (semantic) + BM25 (keyword) + Reciprocal Rank Fusion.
Phase 5 upgrade from pure vector search.
"""
import json
import math
import os
import re
import openai
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
openai.api_key = os.environ.get("OPENAI_API_KEY")

AUGMENTED_FILE = os.path.join(os.path.dirname(__file__), "shopify_products_augmented.json")
_augmented_cache = None

# ── BM25 Inverted Index ──
_bm25_index = None  # {"token": {doc_idx: tf, ...}}
_bm25_doc_lengths = None  # [length_of_doc_0, length_of_doc_1, ...]
_bm25_avg_dl = 0
_bm25_N = 0

# Danish stop words to exclude from indexing
_STOP_WORDS = {
    "og", "i", "at", "er", "en", "et", "den", "det", "de", "til", "på", "med",
    "for", "af", "fra", "som", "om", "kan", "har", "vil", "der", "ikke", "sig",
    "var", "ved", "så", "også", "efter", "eller", "mange", "alle", "hvad",
    "når", "hun", "han", "vi", "du", "jeg", "dig", "mig", "os", "dem",
    "the", "and", "of", "to", "in", "a", "is", "for", "on", "with",
}


# Danish synonym expansion for query enrichment
_QUERY_SYNONYMS = {
    "ledelse": ["leder", "leadership", "ledelsesstil", "teamledelse"],
    "projektledelse": ["projektstyring", "projektleder", "project", "planlægning"],
    "planlægning": ["projektledelse", "projektstyring", "planning", "plan"],
    "kommunikation": ["kommunikere", "samtale", "dialog", "præsentation"],
    "it": ["informationsteknologi", "computer", "software", "digital"],
    "økonomi": ["finans", "regnskab", "budget", "bogholderi"],
    "salg": ["sælger", "salgsarbejde", "forhandling", "kundekontakt"],
    "marketing": ["markedsføring", "digital", "kampagne", "branding"],
    "coaching": ["coach", "mentor", "mentoring", "personlig"],
    "agil": ["agile", "scrum", "kanban", "sprint"],
    "scrum": ["agil", "agile", "sprint", "kanban"],
    "prince2": ["prince", "projektledelse", "projektstyring"],
    "certificering": ["certifikat", "eksamen", "akkreditering"],
    "excel": ["regneark", "spreadsheet", "microsoft"],
    "personlig": ["personligudvikling", "selvudvikling", "udvikling"],
}


def _tokenize(text):
    """Simple tokenizer: lowercase, split on non-alphanumeric, filter stop words."""
    if not text:
        return []
    text = text.lower()
    tokens = re.findall(r'[a-zæøå0-9]+', text)
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


def _expand_query_tokens(tokens):
    """Expand query tokens with synonyms for better BM25 recall."""
    expanded = list(tokens)
    for token in tokens:
        synonyms = _QUERY_SYNONYMS.get(token, [])
        for syn in synonyms:
            syn_tokens = _tokenize(syn)
            expanded.extend(syn_tokens)
    return expanded


def _build_bm25_index(products):
    """Build an in-memory BM25 inverted index from products."""
    global _bm25_index, _bm25_doc_lengths, _bm25_avg_dl, _bm25_N

    _bm25_index = defaultdict(dict)
    _bm25_doc_lengths = []
    _bm25_N = len(products)

    for idx, p in enumerate(products):
        # Combine searchable fields with weighting (title tokens counted 3x)
        title_tokens = _tokenize(p.get("title", "")) * 3
        vendor_tokens = _tokenize(p.get("vendor", ""))
        summary_tokens = _tokenize(p.get("ai_summary", ""))

        tags = p.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        tag_tokens = []
        for tag in tags:
            tag_tokens.extend(_tokenize(tag) * 2)  # Tags weighted 2x

        type_tokens = _tokenize(p.get("product_type", "")) * 2

        all_tokens = title_tokens + vendor_tokens + summary_tokens + tag_tokens + type_tokens
        _bm25_doc_lengths.append(len(all_tokens))

        # Count term frequencies
        tf = defaultdict(int)
        for token in all_tokens:
            tf[token] += 1
        for token, count in tf.items():
            _bm25_index[token][idx] = count

    _bm25_avg_dl = sum(_bm25_doc_lengths) / max(_bm25_N, 1)


def _bm25_search(query_tokens, limit=20):
    """Score documents using BM25 formula."""
    if not _bm25_index or not query_tokens:
        return []

    k1 = 1.5
    b = 0.75
    scores = defaultdict(float)

    for token in query_tokens:
        if token not in _bm25_index:
            continue
        posting = _bm25_index[token]
        df = len(posting)
        idf = math.log((_bm25_N - df + 0.5) / (df + 0.5) + 1.0)

        for doc_idx, tf in posting.items():
            dl = _bm25_doc_lengths[doc_idx]
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(_bm25_avg_dl, 1)))
            scores[doc_idx] += idf * tf_norm

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:limit]


def load_augmented_products():
    """Load the pre-computed products with embeddings into memory. Builds BM25 index on first load."""
    global _augmented_cache
    if _augmented_cache is None:
        try:
            with open(AUGMENTED_FILE, "r", encoding="utf-8") as f:
                _augmented_cache = json.load(f)
            # Build BM25 index alongside
            _build_bm25_index(_augmented_cache)
            print(f"[RAG] Loaded {len(_augmented_cache)} products, BM25 index built ({len(_bm25_index)} terms)")
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


def _reciprocal_rank_fusion(ranked_lists, k=60):
    """
    Merge multiple ranked result lists using RRF.
    Each list is [(doc_idx, score), ...] sorted by score descending.
    Returns [(doc_idx, rrf_score)] sorted descending.
    """
    rrf_scores = defaultdict(float)
    for ranked_list in ranked_lists:
        for rank, (doc_idx, _score) in enumerate(ranked_list):
            rrf_scores[doc_idx] += 1.0 / (k + rank + 1)
    return sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)


def semantic_search_courses(query, limit=5, min_score=0.35):
    """
    Hybrid search: combines vector similarity with BM25 keyword matching
    using Reciprocal Rank Fusion for best results.

    Returns:
        list of products on success,
        dict with "error" key on failure
    """
    products = load_augmented_products()
    if not products:
        return {"error": "index_not_loaded", "message": "Produktindekset er ikke indlæst."}

    # ── 1. Vector search ──
    vector_ranked = []
    query_vector = get_query_embedding(query)
    if query_vector:
        scored = []
        for idx, product in enumerate(products):
            product_vector = product.get("embedding")
            if not product_vector:
                continue
            score = cosine_similarity(query_vector, product_vector)
            scored.append((idx, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        # Take top 20 for RRF
        vector_ranked = scored[:20]

    # ── 2. BM25 keyword search (with synonym expansion) ──
    query_tokens = _tokenize(query)
    expanded_tokens = _expand_query_tokens(query_tokens)
    bm25_ranked = _bm25_search(expanded_tokens, limit=20)

    # ── 3. Reciprocal Rank Fusion ──
    if vector_ranked and bm25_ranked:
        fused = _reciprocal_rank_fusion([vector_ranked, bm25_ranked])
    elif vector_ranked:
        fused = vector_ranked
    elif bm25_ranked:
        fused = bm25_ranked
    else:
        return {"error": "embedding_failed", "message": "Kunne ikke oprette embedding for søgningen."}

    # ── 4. Dynamic min_score filtering ──
    # If we have vector scores, use them for quality filtering
    if vector_ranked and query_vector:
        top_vector_score = vector_ranked[0][1] if vector_ranked else 0
        # Adaptive threshold: strict if we have strong matches, lenient if not
        if top_vector_score > 0.65:
            effective_min = 0.45
        elif top_vector_score > 0.50:
            effective_min = 0.38
        else:
            effective_min = min_score

        # Filter fused results by vector score where available
        vector_scores = {idx: score for idx, score in vector_ranked}
        filtered_fused = []
        for doc_idx, rrf_score in fused:
            vec_score = vector_scores.get(doc_idx, 0)
            # Include if: good vector score OR strong BM25 match (even if low vector)
            bm25_match = any(doc_idx == bidx for bidx, _ in bm25_ranked[:10])
            if vec_score >= effective_min or bm25_match:
                filtered_fused.append((doc_idx, rrf_score))
        fused = filtered_fused if filtered_fused else fused[:limit]

    # ── 5. Return top N products ──
    result_indices = [doc_idx for doc_idx, _ in fused[:limit]]
    return [products[idx] for idx in result_indices if idx < len(products)]


def hybrid_rank_products(filtered_products, query, all_products, limit=5):
    """
    Rank a pre-filtered set of products using hybrid search.
    Used by filter_courses when a query is also provided.
    """
    if not query or not filtered_products:
        return filtered_products[:limit]

    # Build index mapping from filtered products to their global indices
    handle_to_product = {p.get("handle"): p for p in filtered_products}
    handle_to_global_idx = {}
    for idx, p in enumerate(all_products):
        if p.get("handle") in handle_to_product:
            handle_to_global_idx[p.get("handle")] = idx

    # Vector scoring
    query_vector = get_query_embedding(query)
    vector_ranked = []
    if query_vector:
        for handle, global_idx in handle_to_global_idx.items():
            vec = all_products[global_idx].get("embedding")
            if vec:
                score = cosine_similarity(query_vector, vec)
                vector_ranked.append((handle, score))
        vector_ranked.sort(key=lambda x: x[1], reverse=True)

    # BM25 scoring (on filtered set only)
    query_tokens = _tokenize(query)
    bm25_scored = []
    for handle, p in handle_to_product.items():
        doc_tokens = _tokenize(p.get("title", "")) * 3 + _tokenize(p.get("ai_summary", ""))
        if not doc_tokens:
            bm25_scored.append((handle, 0))
            continue
        score = 0
        for qt in query_tokens:
            tf = doc_tokens.count(qt)
            if tf > 0:
                score += tf / len(doc_tokens)
        bm25_scored.append((handle, score))
    bm25_scored.sort(key=lambda x: x[1], reverse=True)

    # Simple RRF on handles
    rrf = defaultdict(float)
    k = 60
    for rank, (handle, _) in enumerate(vector_ranked):
        rrf[handle] += 1.0 / (k + rank + 1)
    for rank, (handle, _) in enumerate(bm25_scored):
        rrf[handle] += 1.0 / (k + rank + 1)

    ranked_handles = sorted(rrf.items(), key=lambda x: x[1], reverse=True)
    return [handle_to_product[h] for h, _ in ranked_handles[:limit] if h in handle_to_product]
