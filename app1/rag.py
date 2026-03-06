"""
RAG module with hybrid search: Vector (semantic) + BM25 (keyword) + Reciprocal Rank Fusion.
Phase 5 upgrade from pure vector search.
"""
import json
import math
import os
import re
import time
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

_GENERIC_QUERY_TOKENS = {
    "kursus", "kurser", "uddannelse", "forløb", "træning", "training", "course"
}

_LOCATION_HINTS = {
    "københavn": {"københavn", "herlev", "hørkær", "lyngby", "taastrup", "ballerup", "glostrup", "brøndby"},
    "aarhus": {"aarhus", "århus"},
    "odense": {"odense"},
    "aalborg": {"aalborg", "ålborg"},
    "online": {"online", "virtuelt", "fjern", "remote"},
}

_FORMAT_HINTS = {
    "online": {"online", "e-learning", "elearning", "fjernundervisning", "virtuelt", "remote"},
    "fysisk": {"fysisk", "fremmøde", "klasseundervisning", "on-site", "onsite"},
}


# Danish synonym expansion for query enrichment
_QUERY_SYNONYMS = {
    # Management & leadership
    "ledelse": ["leder", "leadership", "ledelsesstil", "teamledelse", "management"],
    "projektledelse": ["projektstyring", "projektleder", "project", "planlægning", "pmp"],
    "planlægning": ["projektledelse", "projektstyring", "planning", "plan"],
    "strategi": ["strategisk", "forretningsstrategi", "virksomhedsstrategi"],
    # Communication & soft skills
    "kommunikation": ["kommunikere", "samtale", "dialog", "præsentation"],
    "præsentation": ["powerpoint", "kommunikation", "formidling"],
    "facilitering": ["facilitator", "workshop", "mødeledelse"],
    "konflikthåndtering": ["konflikt", "mediation", "mægling"],
    "coaching": ["coach", "mentor", "mentoring", "personlig"],
    "personlig": ["personligudvikling", "selvudvikling", "udvikling"],
    # IT certifications
    "itil": ["itsm", "it service management", "serviceledelse", "itil4"],
    "devops": ["ci cd", "continuous", "deployment", "automation", "pipeline"],
    "cloud": ["azure", "aws", "skyløsning", "cloudcomputing"],
    "azure": ["microsoft", "cloud", "skyløsning"],
    "aws": ["amazon", "cloud", "skyløsning"],
    "cybersecurity": ["sikkerhed", "informationssikkerhed", "it sikkerhed", "hacking"],
    "sikkerhed": ["cybersecurity", "informationssikkerhed", "datasikkerhed"],
    # Data & analytics
    "power bi": ["powerbi", "bi", "dashboard", "datavisualisering", "rapport"],
    "powerbi": ["power bi", "bi", "dashboard", "datavisualisering"],
    "bi": ["business intelligence", "powerbi", "dashboard", "rapport", "datavisualisering"],
    "data": ["dataanalyse", "analytics", "statistik", "big data"],
    "analyse": ["analytics", "dataanalyse", "data", "statistik"],
    "tableau": ["datavisualisering", "dashboard", "bi"],
    "excel": ["regneark", "spreadsheet", "microsoft", "vba"],
    # Business & finance
    "it": ["informationsteknologi", "computer", "software", "digital"],
    "økonomi": ["finans", "regnskab", "budget", "bogholderi"],
    "salg": ["sælger", "salgsarbejde", "forhandling", "kundekontakt"],
    "marketing": ["markedsføring", "digital", "kampagne", "branding"],
    "forhandling": ["forhandlingsteknik", "salg", "aftale", "negotiation"],
    "innovation": ["nytænkning", "forretningsudvikling", "kreativitet"],
    # Lean & quality
    "lean": ["lean management", "waste", "kaizen", "procesforbedring", "six sigma"],
    "six sigma": ["sixsigma", "kvalitetsstyring", "lean", "procesforbedring"],
    "kvalitet": ["kvalitetsstyring", "kvalitetsledelse", "six sigma"],
    # HR & organization
    "hr": ["human resources", "personale", "rekruttering", "medarbejder"],
    "rekruttering": ["ansættelse", "hr", "talent", "onboarding"],
    "onboarding": ["introduktion", "rekruttering", "ny medarbejder"],
    # Legal & compliance
    "jura": ["juridisk", "lovgivning", "kontrakt", "ret"],
    "gdpr": ["persondataforordningen", "persondata", "databeskyttelse", "privacy"],
    "compliance": ["regeloverholdelse", "gdpr", "lovgivning", "audit"],
    "arbejdsmiljø": ["trivsel", "sikkerhed", "hms", "ergonomi"],
    # Agile & project methods
    "agil": ["agile", "scrum", "kanban", "sprint"],
    "scrum": ["agil", "agile", "sprint", "kanban", "scrummaster"],
    "kanban": ["agil", "lean", "flow", "board"],
    "prince2": ["prince", "projektledelse", "projektstyring"],
    "pmp": ["projektledelse", "pmi", "project management"],
    "certificering": ["certifikat", "eksamen", "akkreditering"],
    # Programming
    "programmering": ["kode", "software", "udvikling", "coding"],
    "python": ["programmering", "scripting", "dataanalyse", "machine learning"],
    "sql": ["database", "forespørgsel", "data"],
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


def _core_query_tokens(tokens):
    """Keep only topical query terms (drop generic course words)."""
    return [t for t in tokens if t not in _GENERIC_QUERY_TOKENS]


def _product_text_tokens(product):
    """Flatten searchable product fields into a token set for lexical checks."""
    tokens = []
    tokens.extend(_tokenize(product.get("title", "")))
    tokens.extend(_tokenize(product.get("ai_summary", "")))
    tokens.extend(_tokenize(product.get("product_type", "")))

    tags = product.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    for tag in tags:
        tokens.extend(_tokenize(tag))
    return set(tokens)


def _extract_price_value(product):
    variants = product.get("variants", [])
    if not variants:
        return None
    raw = variants[0].get("price")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _detect_query_preferences(query_tokens):
    """Extract soft constraints from query: location, format, and price intent."""
    token_set = set(query_tokens)
    location_targets = set()
    for city, variants in _LOCATION_HINTS.items():
        if token_set.intersection(variants):
            location_targets.add(city)

    format_targets = set()
    for fmt, variants in _FORMAT_HINTS.items():
        if token_set.intersection(variants):
            format_targets.add(fmt)

    price_intent = None
    if token_set.intersection({"billig", "billigste", "budget", "prisvenlig", "lavpris"}):
        price_intent = "low"
    elif token_set.intersection({"avanceret", "premium", "eksklusiv", "mest", "dyreste"}):
        price_intent = "high"

    return {
        "locations": sorted(location_targets),
        "formats": sorted(format_targets),
        "price_intent": price_intent,
    }


def _preference_rerank_score(product, preferences, global_price_span):
    """Score product against inferred user preferences for smarter ranking."""
    bonus = 0.0
    reasons = []

    # Location preference
    if preferences.get("locations"):
        locations_text = " ".join((v.get("option1") or "").lower() for v in product.get("variants", []))
        for loc in preferences["locations"]:
            aliases = _LOCATION_HINTS.get(loc, {loc})
            if any(alias in locations_text for alias in aliases):
                bonus += 0.06
                reasons.append(f"location:{loc}")
                break

    # Format preference
    if preferences.get("formats"):
        searchable = " ".join([
            product.get("title", ""),
            product.get("ai_summary", ""),
            product.get("product_type", ""),
            " ".join(product.get("tags", []) if isinstance(product.get("tags", []), list) else [str(product.get("tags", ""))])
        ]).lower()
        for fmt in preferences["formats"]:
            if any(tok in searchable for tok in _FORMAT_HINTS.get(fmt, {fmt})):
                bonus += 0.05
                reasons.append(f"format:{fmt}")
                break

    # Price preference
    price_intent = preferences.get("price_intent")
    price = _extract_price_value(product)
    if price_intent and price is not None and global_price_span:
        low, high = global_price_span
        span = max(high - low, 1.0)
        normalized = (price - low) / span
        if price_intent == "low":
            bonus += (1 - normalized) * 0.05
            reasons.append("price:low")
        elif price_intent == "high":
            bonus += normalized * 0.05
            reasons.append("price:high")

    return bonus, reasons


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


_embedding_cache = {}  # {lowered_query: (embedding, timestamp)}
_EMBEDDING_CACHE_TTL = 3600  # 1 hour
_EMBEDDING_CACHE_MAX = 200


def get_query_embedding(query_text):
    """Get the embedding vector for the user query, with caching."""
    cache_key = query_text.lower().strip()
    now = time.time()

    # Check cache
    if cache_key in _embedding_cache:
        emb, ts = _embedding_cache[cache_key]
        if now - ts < _EMBEDDING_CACHE_TTL:
            return emb
        else:
            del _embedding_cache[cache_key]

    try:
        response = openai.embeddings.create(
            input=query_text,
            model="text-embedding-3-small"
        )
        embedding = response.data[0].embedding

        # Evict oldest entries if cache is full
        if len(_embedding_cache) >= _EMBEDDING_CACHE_MAX:
            oldest_key = min(_embedding_cache, key=lambda k: _embedding_cache[k][1])
            del _embedding_cache[oldest_key]

        _embedding_cache[cache_key] = (embedding, now)
        return embedding
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
    detailed = semantic_search_courses_detailed(query, limit=limit, min_score=min_score)
    if isinstance(detailed, dict) and "error" in detailed:
        return detailed
    return detailed.get("products", [])


def semantic_search_courses_detailed(query, limit=5, min_score=0.35):
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
    core_query_tokens = _core_query_tokens(query_tokens)
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

    # ── 5. Topical lexical sanity-check + rerank ──
    # Avoid returning unrelated courses just to fill `limit`.
    if core_query_tokens and fused:
        lexical_scored = []
        for doc_idx, base_score in fused:
            product_tokens = _product_text_tokens(products[doc_idx])
            core_hits = sum(1 for tok in core_query_tokens if tok in product_tokens)
            # Large bonus for direct topical overlap.
            lexical_scored.append((doc_idx, base_score + (core_hits * 0.03), core_hits))

        lexical_scored.sort(key=lambda x: x[1], reverse=True)
        with_hits = [x for x in lexical_scored if x[2] > 0]

        # If we have at least one topical match, prioritize those results.
        # This ensures e.g. "planlægning kursus" does not get padded with unrelated items.
        chosen = with_hits if with_hits else lexical_scored
        fused = [(doc_idx, score) for doc_idx, score, _hits in chosen]

    # ── 6. Preference-aware reranking (location/format/price intent) ──
    preferences = _detect_query_preferences(query_tokens)
    all_prices = [p for p in (_extract_price_value(prod) for prod in products) if p is not None]
    price_span = (min(all_prices), max(all_prices)) if all_prices else None

    reranked = []
    vector_scores = {idx: score for idx, score in vector_ranked}
    bm25_map = {idx: score for idx, score in bm25_ranked}
    for doc_idx, base_score in fused:
        pref_bonus, pref_reasons = _preference_rerank_score(products[doc_idx], preferences, price_span)
        phrase_bonus = 0.0
        title_lower = products[doc_idx].get("title", "").lower()
        if query.lower().strip() and query.lower().strip() in title_lower:
            phrase_bonus = 0.08
        final_score = base_score + pref_bonus + phrase_bonus
        reranked.append((doc_idx, final_score, {
            "base_score": round(base_score, 4),
            "vector_score": round(vector_scores.get(doc_idx, 0.0), 4),
            "bm25_score": round(bm25_map.get(doc_idx, 0.0), 4),
            "pref_bonus": round(pref_bonus, 4),
            "phrase_bonus": round(phrase_bonus, 4),
            "pref_reasons": pref_reasons,
        }))

    reranked.sort(key=lambda x: x[1], reverse=True)

    # ── 7. Return top N products + debug diagnostics ──
    result_indices = [doc_idx for doc_idx, _, _ in reranked[:limit]]
    selected_products = [products[idx] for idx in result_indices if idx < len(products)]

    diagnostics = {
        "query": query,
        "query_tokens": query_tokens,
        "core_query_tokens": core_query_tokens,
        "expanded_query_tokens": expanded_tokens[:30],
        "preferences": preferences,
        "vector_candidates": len(vector_ranked),
        "bm25_candidates": len(bm25_ranked),
        "fused_candidates": len(fused),
        "top_vector_score": round(vector_ranked[0][1], 4) if vector_ranked else 0,
        "effective_limit": limit,
        "selected": [
            {
                "handle": products[idx].get("handle"),
                "title": products[idx].get("title"),
                "score": round(score, 4),
                "explain": explain,
            }
            for idx, score, explain in reranked[:limit]
        ]
    }
    return {"products": selected_products, "debug": diagnostics}


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

    # BM25 scoring (on filtered set only) — proper BM25 with IDF from global index
    query_tokens = _tokenize(query)
    expanded_query_tokens = _expand_query_tokens(query_tokens)
    k1 = 1.5
    b = 0.75
    bm25_scored = []
    for handle, p in handle_to_product.items():
        title_tokens = _tokenize(p.get("title", "")) * 3
        summary_tokens = _tokenize(p.get("ai_summary", ""))
        tags = p.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        tag_tokens = []
        for tg in tags:
            tag_tokens.extend(_tokenize(tg) * 2)
        doc_tokens = title_tokens + summary_tokens + tag_tokens
        if not doc_tokens:
            bm25_scored.append((handle, 0))
            continue
        dl = len(doc_tokens)
        tf_map = defaultdict(int)
        for tok in doc_tokens:
            tf_map[tok] += 1
        score = 0.0
        for qt in expanded_query_tokens:
            tf = tf_map.get(qt, 0)
            if tf == 0:
                continue
            # IDF from global index
            df = len(_bm25_index.get(qt, {})) if _bm25_index else 1
            idf = math.log((_bm25_N - df + 0.5) / (df + 0.5) + 1.0) if _bm25_N else 1.0
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(_bm25_avg_dl, 1)))
            score += idf * tf_norm
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
