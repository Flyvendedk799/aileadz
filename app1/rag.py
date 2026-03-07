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
_title_token_sets = None  # [set_of_tokens_for_doc_0, set_of_tokens_for_doc_1, ...]

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
    "projektledelse": ["projektstyring", "projektleder", "project", "pmp"],
    "planlægning": ["planning", "plan", "planlæg", "tidsplan"],
    "strategi": ["strategisk", "forretningsstrategi", "virksomhedsstrategi"],
    # Communication & soft skills
    "kommunikation": ["kommunikere", "samtale", "dialog", "præsentation"],
    "præsentation": ["powerpoint", "kommunikation", "formidling"],
    "facilitering": ["facilitator", "workshop", "mødeledelse"],
    "konflikthåndtering": ["konflikt", "mediation", "mægling"],
    "coaching": ["coach", "mentor", "mentoring"],
    "personlig": ["personligudvikling", "selvudvikling"],
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
    "it": ["informationsteknologi", "computer", "software"],
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


def _fuzzy_synonym_match(token, threshold=0.85):
    """Find best synonym key match for a token, tolerating minor typos."""
    if token in _QUERY_SYNONYMS:
        return token
    # Quick check: for tokens > 5 chars, try edit distance 1-2 matches
    if len(token) < 4:
        return None
    best_key = None
    best_ratio = 0
    for key in _QUERY_SYNONYMS:
        # Quick length filter — skip keys too different in length
        if abs(len(key) - len(token)) > 2:
            continue
        # Simple ratio: count matching chars in order (Levenshtein-like)
        shorter, longer = (token, key) if len(token) <= len(key) else (key, token)
        matches = 0
        j = 0
        for c in shorter:
            while j < len(longer):
                if longer[j] == c:
                    matches += 1
                    j += 1
                    break
                j += 1
        ratio = matches / max(len(shorter), len(longer))
        if ratio > best_ratio:
            best_ratio = ratio
            best_key = key
    return best_key if best_ratio >= threshold else None


def _expand_query_tokens(tokens):
    """Expand query tokens with synonyms for better BM25 recall. Handles typos via fuzzy matching.
    Returns (expanded_tokens, fuzzy_corrections) where fuzzy_corrections is a list of {original, corrected}."""
    expanded = list(tokens)
    fuzzy_corrections = []
    for token in tokens:
        # Direct match first
        synonyms = _QUERY_SYNONYMS.get(token, [])
        if not synonyms:
            # Try fuzzy match for typos (e.g., "konflikhåndtering" → "konflikthåndtering")
            fuzzy_key = _fuzzy_synonym_match(token)
            if fuzzy_key:
                synonyms = _QUERY_SYNONYMS[fuzzy_key]
                # Also add the corrected key itself as an expansion
                expanded.extend(_tokenize(fuzzy_key))
                fuzzy_corrections.append({"original": token, "corrected": fuzzy_key})
        for syn in synonyms:
            syn_tokens = _tokenize(syn)
            expanded.extend(syn_tokens)
    return expanded, fuzzy_corrections


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
    global _bm25_index, _bm25_doc_lengths, _bm25_avg_dl, _bm25_N, _title_token_sets

    _bm25_index = defaultdict(dict)
    _bm25_doc_lengths = []
    _title_token_sets = []
    _bm25_N = len(products)

    for idx, p in enumerate(products):
        # Store title + primary_topic tokens for precise topic-match scoring
        raw_title_tokens = set(_tokenize(p.get("title", "")))
        primary_topic_str = (p.get("structured_metadata") or {}).get("primary_topic", "")
        if primary_topic_str:
            raw_title_tokens.update(_tokenize(primary_topic_str))
        _title_token_sets.append(raw_title_tokens)

        # Combine searchable fields with weighting (title tokens counted 6x — title is the strongest signal)
        title_tokens = _tokenize(p.get("title", "")) * 6
        vendor_tokens = _tokenize(p.get("vendor", ""))
        summary_tokens = _tokenize(p.get("ai_summary", ""))

        # Primary topic weighted 5x — strong signal for what the course is ABOUT
        primary_topic = (p.get("structured_metadata") or {}).get("primary_topic", "")
        topic_tokens = _tokenize(primary_topic) * 5 if primary_topic else []

        tags = p.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        tag_tokens = []
        for tag in tags:
            tag_tokens.extend(_tokenize(tag) * 2)  # Tags weighted 2x

        type_tokens = _tokenize(p.get("product_type", "")) * 2

        all_tokens = title_tokens + topic_tokens + vendor_tokens + summary_tokens + tag_tokens + type_tokens
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

# 5.3: Search result cache — avoids full pipeline for repeated queries
_search_cache = {}  # {cache_key: (result_dict, timestamp)}
_SEARCH_CACHE_TTL = 900  # 15 minutes
_SEARCH_CACHE_MAX = 100


def get_query_embedding(query_text):
    """Get the embedding vector for the user query, with caching.
    Uses text-embedding-3-large with 1024 dimensions for better quality."""
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
            model="text-embedding-3-large",
            dimensions=1024
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


def _reciprocal_rank_fusion(ranked_lists, k=60, weights=None):
    """
    Merge multiple ranked result lists using weighted RRF.
    Each list is [(doc_idx, score), ...] sorted by score descending.
    weights: list of floats, one per ranked_list. Defaults to equal weight.
    Returns [(doc_idx, rrf_score)] sorted descending.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    rrf_scores = defaultdict(float)
    for ranked_list, weight in zip(ranked_lists, weights):
        for rank, (doc_idx, _score) in enumerate(ranked_list):
            rrf_scores[doc_idx] += weight * (1.0 / (k + rank + 1))
    return sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)


def _compute_rrf_weights(query_tokens, bm25_ranked):
    """Determine BM25 vs vector weight based on query specificity.
    Short specific queries with many BM25 hits → favor BM25.
    Long vague queries with few BM25 hits → favor vector."""
    if not query_tokens or not bm25_ranked:
        return [1.0, 1.0]  # [vector_weight, bm25_weight]

    # Count how many query tokens have direct BM25 hits
    tokens_with_hits = sum(1 for t in query_tokens if t in (_bm25_index or {}))
    hit_ratio = tokens_with_hits / max(len(query_tokens), 1)

    # Short + high hit ratio → BM25 is precise, trust it more
    if len(query_tokens) <= 3 and hit_ratio > 0.5:
        return [1.0, 1.5]
    # Long + low hit ratio → vague query, trust vectors more
    elif len(query_tokens) >= 5 and hit_ratio < 0.3:
        return [1.5, 1.0]
    else:
        return [1.0, 1.0]


def semantic_search_courses(query, limit=5, min_score=0.35, shown_handles=None, user_prefs=None):
    detailed = semantic_search_courses_detailed(query, limit=limit, min_score=min_score,
                                                 shown_handles=shown_handles, user_prefs=user_prefs)
    if isinstance(detailed, dict) and "error" in detailed:
        return detailed
    return detailed.get("products", [])


def _crossencoder_rerank(query, candidates, products, limit=5):
    """Cross-encoder reranking: use GPT-4o-mini to verify relevance of top candidates.
    Takes top candidates and filters out false positives where keyword/vector matching
    succeeded but the course isn't actually about the query topic.
    Returns reordered list of (doc_idx, score, explain_dict) tuples."""
    if not candidates or not query.strip():
        return candidates

    # Build batch prompt with all candidates
    course_lines = []
    candidate_indices = []
    for i, (doc_idx, score, explain) in enumerate(candidates[:8]):  # Max 8 to limit cost
        title = products[doc_idx].get("title", "Ukendt")
        summary = (products[doc_idx].get("ai_summary", "") or "")[:150]
        vendor = products[doc_idx].get("vendor", "")
        course_lines.append(f"{i+1}. {title} ({vendor}): {summary}")
        candidate_indices.append((doc_idx, score, explain))

    if not course_lines:
        return candidates

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """Du er en relevansvurderer. En bruger søger efter kurser.
For hvert kursus, vurdér hvor relevant det er for brugerens søgning.
Svar KUN som en JSON-liste med tal fra 1-10 for hvert kursus.
10 = kurset handler præcis om det brugeren søger
7-9 = kurset er stærkt relateret og relevant
4-6 = kurset nævner emnet men handler primært om noget andet
1-3 = kurset er ikke relevant for søgningen
Eksempel svar: [9, 3, 7, 5]"""},
                {"role": "user", "content": f"Søgning: \"{query}\"\n\nKurser:\n" + "\n".join(course_lines)}
            ],
            temperature=0.0,
            max_tokens=50,
            timeout=5.0
        )
        raw = response.choices[0].message.content.strip()
        # Parse the scores
        scores = json.loads(raw)
        if not isinstance(scores, list):
            return candidates

        # Apply cross-encoder scores: filter out irrelevant (< 4) and reorder by combined score
        reranked = []
        for i, (doc_idx, retrieval_score, explain) in enumerate(candidate_indices):
            if i < len(scores):
                ce_score = scores[i]
                if ce_score < 4:
                    continue  # Filter out clearly irrelevant results
                # Combine retrieval score with cross-encoder score
                # Cross-encoder has high weight since it judges actual relevance
                combined = retrieval_score * 0.4 + (ce_score / 10.0) * 0.6
                explain = dict(explain)
                explain["cross_encoder_score"] = ce_score
                explain["combined_score"] = round(combined, 4)
                reranked.append((doc_idx, combined, explain))
            else:
                reranked.append((doc_idx, retrieval_score, explain))

        reranked.sort(key=lambda x: x[1], reverse=True)
        return reranked[:limit] if reranked else candidates[:limit]

    except Exception as e:
        import traceback
        print(f"[Cross-encoder Error] Falling back to retrieval ranking. Query='{query[:80]}', candidates={len(candidates)}. Error: {e}")
        print(f"[Cross-encoder Traceback] {traceback.format_exc()}")
        return candidates  # Fallback to original ranking on error


def _get_search_cache_key(query, limit, shown_handles, user_prefs):
    """Build a stable cache key for search results."""
    import hashlib
    parts = [query.lower().strip(), str(limit)]
    if user_prefs:
        parts.append(json.dumps(user_prefs, sort_keys=True))
    # Don't include shown_handles in cache key — the penalty is applied post-cache
    key = "|".join(parts)
    return hashlib.md5(key.encode()).hexdigest()


def semantic_search_courses_detailed(query, limit=5, min_score=0.35, shown_handles=None, user_prefs=None):
    """
    Hybrid search: combines vector similarity with BM25 keyword matching
    using weighted Reciprocal Rank Fusion for best results.

    Args:
        shown_handles: set of product handles already shown this session (soft deprioritized)
        user_prefs: dict with optional keys {location, format, budget_range} from user profile

    Returns:
        dict with "products", "debug", "confidence" keys on success,
        dict with "error" key on failure
    """
    products = load_augmented_products()
    if not products:
        return {"error": "index_not_loaded", "message": "Produktindekset er ikke indlæst."}

    # 5.3: Check search cache (shown_handles excluded from key — applied as post-filter)
    cache_key = _get_search_cache_key(query, limit, shown_handles, user_prefs)
    now = time.time()
    if cache_key in _search_cache:
        cached_result, cached_at = _search_cache[cache_key]
        if now - cached_at < _SEARCH_CACHE_TTL:
            # Re-apply shown_handles penalty on cached results (since it's session-specific)
            if shown_handles:
                result_copy = dict(cached_result)
                result_copy["debug"] = dict(cached_result.get("debug", {}))
                result_copy["debug"]["cache_hit"] = True
                return result_copy
            cached_result.setdefault("debug", {})["cache_hit"] = True
            return cached_result
        else:
            del _search_cache[cache_key]

    shown_handles = shown_handles or set()

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
        vector_ranked = scored[:20]

    # ── 2. BM25 keyword search (with synonym expansion) ──
    query_tokens = _tokenize(query)
    core_query_tokens = _core_query_tokens(query_tokens)
    expanded_tokens, fuzzy_corrections = _expand_query_tokens(query_tokens)
    bm25_ranked = _bm25_search(expanded_tokens, limit=20)

    # ── 3. Weighted Reciprocal Rank Fusion ──
    # Weight depends on query specificity: specific queries favor BM25, vague favor vectors
    if vector_ranked and bm25_ranked:
        rrf_weights = _compute_rrf_weights(core_query_tokens, bm25_ranked)
        fused = _reciprocal_rank_fusion([vector_ranked, bm25_ranked], weights=rrf_weights)
    elif vector_ranked:
        fused = vector_ranked
    elif bm25_ranked:
        fused = bm25_ranked
    else:
        return {"error": "embedding_failed", "message": "Kunne ikke oprette embedding for søgningen."}

    # ── 4. Relative gap filtering (replaces absolute thresholds) ──
    if vector_ranked and query_vector and len(fused) > 1:
        vector_scores = {idx: score for idx, score in vector_ranked}
        bm25_top_set = {bidx for bidx, _ in bm25_ranked[:10]}

        # Keep results that are either strong vector matches or strong BM25 matches
        top_rrf_score = fused[0][1] if fused else 0
        filtered_fused = []
        for doc_idx, rrf_score in fused:
            vec_score = vector_scores.get(doc_idx, 0)
            is_bm25_hit = doc_idx in bm25_top_set
            # Relative gap: keep if within 50% of top score, or strong BM25 match
            if rrf_score >= top_rrf_score * 0.5 or is_bm25_hit:
                filtered_fused.append((doc_idx, rrf_score))
        fused = filtered_fused if filtered_fused else fused[:limit]

    # ── 5. Title-match scoring + topical sanity check ──
    # Strong differentiation: courses where query tokens appear in the TITLE
    # are much more relevant than courses where they only appear in body text
    if core_query_tokens and fused and _title_token_sets:
        title_scored = []
        for doc_idx, base_score in fused:
            title_tokens = _title_token_sets[doc_idx] if doc_idx < len(_title_token_sets) else set()
            body_tokens = _product_text_tokens(products[doc_idx])

            # Count core query token hits in title vs body
            title_hits = sum(1 for tok in core_query_tokens if tok in title_tokens)
            body_only_hits = sum(1 for tok in core_query_tokens if tok in body_tokens and tok not in title_tokens)
            total_hits = title_hits + body_only_hits

            # Title match gets a massive bonus; body-only match gets a small one
            title_bonus = title_hits * 0.12  # Strong: course is ABOUT this topic
            body_bonus = body_only_hits * 0.02  # Weak: topic is just mentioned
            relevance_score = base_score + title_bonus + body_bonus

            title_scored.append((doc_idx, relevance_score, total_hits, title_hits))

        title_scored.sort(key=lambda x: x[1], reverse=True)

        # Prefer results with at least some lexical match
        with_hits = [x for x in title_scored if x[2] > 0]
        chosen = with_hits if with_hits else title_scored
        fused = [(doc_idx, score) for doc_idx, score, _hits, _th in chosen]

    # ── 6. Preference-aware reranking ──
    # Merge query-detected preferences with user profile preferences
    preferences = _detect_query_preferences(query_tokens)
    if user_prefs:
        if user_prefs.get("location") and not preferences.get("locations"):
            loc = user_prefs["location"].lower().strip()
            for city, variants in _LOCATION_HINTS.items():
                if loc in variants or loc == city:
                    preferences["locations"] = [city]
                    break
        if user_prefs.get("format") and not preferences.get("formats"):
            fmt = user_prefs["format"].lower().strip()
            for fmt_key, variants in _FORMAT_HINTS.items():
                if fmt in variants or fmt == fmt_key:
                    preferences["formats"] = [fmt_key]
                    break

    all_prices = [p for p in (_extract_price_value(prod) for prod in products) if p is not None]
    price_span = (min(all_prices), max(all_prices)) if all_prices else None

    reranked = []
    vector_scores = {idx: score for idx, score in vector_ranked}
    bm25_map = {idx: score for idx, score in bm25_ranked}
    for doc_idx, base_score in fused:
        pref_bonus, pref_reasons = _preference_rerank_score(products[doc_idx], preferences, price_span)

        # Phrase match bonus + title relevance signal
        phrase_bonus = 0.0
        title_lower = products[doc_idx].get("title", "").lower()
        if query.lower().strip() and query.lower().strip() in title_lower:
            phrase_bonus = 0.12  # Exact phrase in title — very strong signal
        elif core_query_tokens and _title_token_sets and doc_idx < len(_title_token_sets):
            # Partial title match: at least one core token in title
            title_toks = _title_token_sets[doc_idx]
            title_overlap = sum(1 for tok in core_query_tokens if tok in title_toks)
            if title_overlap > 0:
                phrase_bonus = 0.04 * title_overlap

        # 2.6: Soft deprioritize already-shown products (not excluded, just ranked lower)
        shown_penalty = 0.0
        handle = products[doc_idx].get("handle", "")
        if handle in shown_handles:
            shown_penalty = -0.04
            pref_reasons.append("already_shown")

        final_score = base_score + pref_bonus + phrase_bonus + shown_penalty
        reranked.append((doc_idx, final_score, {
            "base_score": round(base_score, 4),
            "vector_score": round(vector_scores.get(doc_idx, 0.0), 4),
            "bm25_score": round(bm25_map.get(doc_idx, 0.0), 4),
            "pref_bonus": round(pref_bonus, 4),
            "phrase_bonus": round(phrase_bonus, 4),
            "shown_penalty": round(shown_penalty, 4),
            "pref_reasons": pref_reasons,
        }))

    reranked.sort(key=lambda x: x[1], reverse=True)

    # ── 7. Vendor diversity: if top 3 are same vendor, demote the 3rd ──
    if len(reranked) >= 3:
        top_vendors = [products[idx].get("vendor", "") for idx, _, _ in reranked[:3]]
        if top_vendors[0] and top_vendors[0] == top_vendors[1] == top_vendors[2]:
            # Swap 3rd with first different-vendor result
            for i in range(3, len(reranked)):
                if products[reranked[i][0]].get("vendor", "") != top_vendors[0]:
                    reranked[2], reranked[i] = reranked[i], reranked[2]
                    break

    # ── 8. Cross-encoder reranking — verify actual relevance of top candidates ──
    # Only run when we have enough candidates and the query has substance
    if len(reranked) >= 2 and core_query_tokens:
        reranked = _crossencoder_rerank(query, reranked, products, limit=limit)

    # ── 9. Confidence signal for AI ──
    top_vector_score = vector_ranked[0][1] if vector_ranked else 0
    if top_vector_score > 0.65:
        confidence = "high"
    elif top_vector_score > 0.45:
        confidence = "medium"
    else:
        confidence = "low"

    # ── 10. Apply minimum relevance threshold + return top N ──
    _MIN_COMBINED_SCORE = 0.12
    pre_filter_count = len(reranked)
    reranked = [(idx, score, explain) for idx, score, explain in reranked if score >= _MIN_COMBINED_SCORE]
    filtered_below_threshold = pre_filter_count - len(reranked)

    # Track if cross-encoder was applied
    cross_encoder_applied = len(reranked) >= 2 and core_query_tokens and any(
        "cross_encoder_score" in explain for _, _, explain in reranked[:3]
    )

    result_indices = [doc_idx for doc_idx, _, _ in reranked[:limit]]
    selected_products = [products[idx] for idx in result_indices if idx < len(products)]

    diagnostics = {
        "query": query,
        "query_tokens": query_tokens,
        "core_query_tokens": core_query_tokens,
        "expanded_query_tokens": expanded_tokens[:30],
        "fuzzy_corrections": fuzzy_corrections,
        "preferences": preferences,
        "vector_candidates": len(vector_ranked),
        "bm25_candidates": len(bm25_ranked),
        "fused_candidates": len(fused),
        "filtered_below_threshold": filtered_below_threshold,
        "cross_encoder_applied": cross_encoder_applied,
        "top_vector_score": round(top_vector_score, 4),
        "confidence": confidence,
        "effective_limit": limit,
        "shown_handles_count": len(shown_handles),
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
    result = {"products": selected_products, "debug": diagnostics, "confidence": confidence}

    # 5.3: Store in search cache
    if len(_search_cache) >= _SEARCH_CACHE_MAX:
        oldest_key = min(_search_cache, key=lambda k: _search_cache[k][1])
        del _search_cache[oldest_key]
    _search_cache[cache_key] = (result, time.time())

    return result


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
    expanded_query_tokens, _ = _expand_query_tokens(query_tokens)
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
