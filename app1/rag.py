"""
RAG module with hybrid search: Vector (semantic) + BM25 (keyword) + Reciprocal Rank Fusion.
Phase 5 upgrade from pure vector search.
"""
import copy
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
SOURCE_FILE = os.path.join(os.path.dirname(__file__), "shopify_products_all_pages.json")
_augmented_cache = None
_augmented_mtime = 0  # Track file modification time for auto-rebuild

# ── BM25 Inverted Index ──
_bm25_index = None  # {"token": {doc_idx: tf, ...}}
_bm25_doc_lengths = None  # [length_of_doc_0, length_of_doc_1, ...]
_bm25_avg_dl = 0
_bm25_N = 0
_title_token_sets = None  # [set_of_tokens_for_doc_0, set_of_tokens_for_doc_1, ...]
_vector_doc_indices = None  # global doc indices with embeddings
_vector_matrix = None  # numpy matrix (n_docs, dim) when numpy is available
_vector_norms = None

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


def _fuzzy_synonym_match(token, threshold=80):
    """Find best synonym key match for a token, tolerating typos.
    Uses Levenshtein ratio (0-100) via fuzzywuzzy for accurate typo detection.
    Also checks against synonym values, not just keys."""
    if token in _QUERY_SYNONYMS:
        return token
    if len(token) < 4:
        return None

    from fuzzywuzzy import fuzz

    best_key = None
    best_ratio = 0

    for key, synonyms in _QUERY_SYNONYMS.items():
        # Check against the key itself
        if abs(len(key) - len(token)) <= 3:
            ratio = fuzz.ratio(token, key)
            if ratio > best_ratio:
                best_ratio = ratio
                best_key = key

        # Also check against synonym values (e.g., user types "databeskytelse" → match "databeskyttelse" in gdpr synonyms)
        for syn in synonyms:
            if abs(len(syn) - len(token)) <= 3:
                ratio = fuzz.ratio(token, syn)
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


def _build_vector_index(products):
    """Precompute vector matrix for fast batch similarity search."""
    global _vector_doc_indices, _vector_matrix, _vector_norms
    expected = embedding_dimensions()
    indices = []
    vectors = []
    for idx, product in enumerate(products):
        vec = product.get("embedding")
        if vec and len(vec) == expected:
            indices.append(idx)
            vectors.append(vec)
    _vector_doc_indices = indices
    _vector_matrix = None
    _vector_norms = None
    if not vectors:
        return
    try:
        import numpy as np
        matrix = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        _vector_matrix = matrix
        _vector_norms = norms
    except Exception:
        _vector_matrix = None
        _vector_norms = None


def _vector_search(query_vector, limit=20):
    if not query_vector:
        return []
    expected = embedding_dimensions()
    if len(query_vector) != expected:
        return []

    if _vector_matrix is not None and _vector_norms is not None and _vector_doc_indices:
        try:
            import numpy as np
            q = np.asarray(query_vector, dtype=np.float32)
            q_norm = float(np.linalg.norm(q)) or 1.0
            sims = (_vector_matrix @ q) / (_vector_norms * q_norm)
            order = np.argsort(sims)[::-1][:limit]
            return [
                (_vector_doc_indices[int(i)], float(sims[int(i)]))
                for i in order
                if float(sims[int(i)]) > 0
            ]
        except Exception:
            pass

    scored = []
    products = _augmented_cache or []
    for idx in _vector_doc_indices or []:
        if idx >= len(products):
            continue
        product_vector = products[idx].get("embedding")
        if not product_vector:
            continue
        score = cosine_similarity(query_vector, product_vector)
        scored.append((idx, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def embed_skip_margin() -> float:
    """Env-tunable confidence margin for skipping the query-embedding API call.

    The BM25 top result must beat the runner-up by at least this multiplicative
    factor before we trust lexical search alone and skip the (paid) embedding.
    Lower = skip more aggressively (cheaper); higher = embed more often (safer).
    Set to 0 (or AI_DISABLE_EMBED_SKIP=1) to ALWAYS embed (legacy behaviour with
    no lexical short-circuit). Default 1.75 matches the previous hard-coded value.
    """
    if os.getenv("AI_DISABLE_EMBED_SKIP", "").strip().lower() in {"1", "true", "yes", "on"}:
        return 0.0
    try:
        margin = float(os.getenv("AI_EMBED_SKIP_MARGIN", "1.75"))
    except ValueError:
        return 1.75
    # A margin <= 1.0 would let *any* top result skip; treat that as "disable skip"
    # unless the operator explicitly asked for it via the env (margin == 0).
    if margin <= 0:
        return 0.0
    return margin


def _bm25_confident_enough(core_query_tokens, bm25_ranked):
    """Skip embedding API when lexical search already has a strong winner.

    Returns True (=> SKIP the embedding call, use BM25 directly) only when:
      * there is a real top + runner-up to compare,
      * the top score clears the env-tunable margin over the runner-up, and
      * the top doc's title actually contains the core query terms.
    Margin of 0 (AI_EMBED_SKIP_MARGIN=0 / AI_DISABLE_EMBED_SKIP=1) disables the
    short-circuit entirely so the pipeline always embeds (reversible)."""
    margin = embed_skip_margin()
    if margin <= 0:  # skip disabled — always embed
        return False
    if not core_query_tokens or len(bm25_ranked) < 2 or not _title_token_sets:
        return False
    top_idx, top_score = bm25_ranked[0]
    _, second_score = bm25_ranked[1]
    if top_score <= 0 or second_score <= 0:
        return False
    if top_score < second_score * margin:
        return False
    if top_idx >= len(_title_token_sets):
        return False
    title_tokens = _title_token_sets[top_idx]
    hits = sum(1 for tok in core_query_tokens if tok in title_tokens)
    return hits >= max(1, min(2, len(core_query_tokens)))


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
    """Load the pre-computed products with embeddings into memory. Builds BM25 index on first load.
    Auto-rebuilds if the JSON file has been modified since last load."""
    global _augmented_cache, _augmented_mtime
    try:
        augmented_mtime = os.path.getmtime(AUGMENTED_FILE) if os.path.exists(AUGMENTED_FILE) else 0
        source_mtime = os.path.getmtime(SOURCE_FILE) if os.path.exists(SOURCE_FILE) else 0
        current_mtime = max(augmented_mtime, source_mtime)
    except OSError:
        current_mtime = 0

    if _augmented_cache is None or (current_mtime > _augmented_mtime and current_mtime > 0):
        try:
            load_path = AUGMENTED_FILE if os.path.exists(AUGMENTED_FILE) else SOURCE_FILE
            with open(load_path, "r", encoding="utf-8") as f:
                _augmented_cache = json.load(f)
            # Normalize tags to list at load time (avoids repeated isinstance checks)
            for p in _augmented_cache:
                tags = p.get("tags", [])
                if isinstance(tags, str):
                    p["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
            _augmented_mtime = current_mtime
            _build_bm25_index(_augmented_cache)
            _build_vector_index(_augmented_cache)
            print(f"[RAG] Loaded {len(_augmented_cache)} products from {os.path.basename(load_path)}, BM25 index built ({len(_bm25_index)} terms)")
        except Exception as e:
            print(f"Error loading augmented products: {e}")
            if _augmented_cache is None:
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


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive integer env var with a safe fallback (never crashes)."""
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (ValueError, TypeError):
        return default


_embedding_cache = {}  # {lowered_query: (embedding, timestamp)}
# Embedding cache: a popular catalog has a heavy long-tail of repeated queries,
# so a larger cache + longer TTL meaningfully raises the hit-rate. All env-tunable;
# defaults are >= the previous hard-coded values so behaviour only improves.
_EMBEDDING_CACHE_TTL = _env_int("AI_EMBEDDING_CACHE_TTL", 21600, minimum=1)  # 6h (was 1h)
_EMBEDDING_CACHE_MAX = _env_int("AI_EMBEDDING_CACHE_MAX", 5000, minimum=1)   # was 1000

# 5.3: Search result cache — avoids full pipeline (incl. embedding + rerank) for
# repeated queries. Most catalog queries repeat ("ledelse", "scrum", ...), so a
# bigger/longer cache is the single highest-leverage cost lever here.
_search_cache = {}  # {cache_key: (result_dict, timestamp)}
_SEARCH_CACHE_TTL = _env_int("AI_SEARCH_CACHE_TTL", 3600, minimum=1)  # 1h (was 15m)
_SEARCH_CACHE_MAX = _env_int("AI_SEARCH_CACHE_MAX", 1000, minimum=1)  # was 100


def embedding_model() -> str:
    """Query-embedding model. Default is text-embedding-3-small — the cheapest
    OpenAI embedding model (~5x cheaper than -3-large). Override with
    AI_EMBEDDING_MODEL only if the catalog was embedded with a different model."""
    return os.getenv("AI_EMBEDDING_MODEL", "text-embedding-3-small")


def embedding_dimensions() -> int:
    """Embedding output dimensions. text-embedding-3-small supports Matryoshka
    truncation, so lower dims = smaller payload = faster/cheaper search (and a
    smaller in-memory vector matrix) at a modest recall cost. Env-tunable via
    AI_EMBEDDING_DIMENSIONS; default 1024 is UNCHANGED. Floor of 256 keeps
    cosine math meaningful. MUST match the dims the catalog was embedded with."""
    try:
        return max(256, int(os.getenv("AI_EMBEDDING_DIMENSIONS", "1024")))
    except ValueError:
        return 1024


def cross_encoder_mode() -> str:
    return os.getenv("AI_RAG_CROSS_ENCODER", "auto").lower().strip() or "auto"


def cross_encoder_max_candidates() -> int:
    """Cap how many candidates the gpt-4o-mini reranker scores in one call.

    Honours the new AI_RERANK_MAX_CANDIDATES (preferred name) and falls back to
    the legacy AI_RAG_CROSS_ENCODER_MAX_CANDIDATES for backward compatibility.
    Fewer candidates => shorter prompt => cheaper rerank call. Default lowered
    from 6 to 4 (the reranker only matters for the closely-bunched top few)."""
    raw = os.getenv("AI_RERANK_MAX_CANDIDATES") or os.getenv("AI_RAG_CROSS_ENCODER_MAX_CANDIDATES") or "4"
    try:
        return max(2, int(raw))
    except (ValueError, TypeError):
        return 4


def cross_encoder_ambiguity_margin() -> float:
    """Env-tunable relative score gap below which the top candidates count as
    'ambiguous' enough to justify a paid rerank.

    The reranker is only worth its cost when the leader is NOT a clear winner.
    If (top_score - second_score) / top_score >= this margin, the leader is
    clear and we SKIP the rerank. Default 0.08 (8%). Set to 0 to fall back to
    the legacy gate (rerank fires on any >=2 ambiguous-ish candidate set);
    set very high to effectively force reranking whenever 'auto' allows it."""
    try:
        margin = float(os.getenv("AI_RERANK_AMBIGUITY_MARGIN", "0.08"))
    except (ValueError, TypeError):
        return 0.08
    return max(0.0, margin)


def _top_gap_is_clear(reranked, margin):
    """True when the #1 candidate clearly beats #2 by >= margin (relative).

    A clear winner means no rerank is needed. Defensive against missing/zero/
    negative scores so it never raises inside the hot search path."""
    if margin <= 0 or len(reranked) < 2:
        return False
    try:
        top_score = reranked[0][1]
        second_score = reranked[1][1]
    except (IndexError, TypeError):
        return False
    if top_score is None or second_score is None or top_score <= 0:
        return False
    return (top_score - second_score) / top_score >= margin


def warm_rag_index() -> int:
    """Eager-load product index + BM25 + vector matrix so first chat query is fast."""
    products = load_augmented_products()
    if products and _vector_matrix is None:
        _build_vector_index(products)
    return len(products or [])


def get_query_embedding(query_text):
    """Get the embedding vector for the user query, with caching.
    Uses embedding_model() (default text-embedding-3-small, the cheapest) at
    embedding_dimensions() (default 1024) — both env-tunable. Returns None on
    any failure so the pipeline degrades gracefully to BM25-only."""
    cache_key = query_text.lower().strip()
    now = time.time()

    # Check cache (update timestamp on access for LRU)
    if cache_key in _embedding_cache:
        emb, ts = _embedding_cache[cache_key]
        if now - ts < _EMBEDDING_CACHE_TTL:
            _embedding_cache[cache_key] = (emb, now)  # LRU: refresh access time
            return emb
        else:
            del _embedding_cache[cache_key]

    try:
        response = openai.embeddings.create(
            input=query_text,
            model=embedding_model(),
            dimensions=embedding_dimensions(),
        )
        embedding = response.data[0].embedding

        # Validate embedding dimensions before caching
        expected_dims = embedding_dimensions()
        if not embedding or len(embedding) != expected_dims:
            print(f"[Embedding Warning] Bad dimensions ({len(embedding) if embedding else 0}) for query: {query_text[:80]}")
            return embedding if embedding else None

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


# Hard ceiling on the profile-conditioned candidate pool. Even the richest
# profile never pulls more than this many candidates into the re-rank — keeps
# the (already-fetched, in-memory) lexical+vector pool bounded so there is no
# runaway cost. 40 comfortably covers a few dozen courses for re-ranking.
PROFILE_CANDIDATE_LIMIT_CAP = 40


def profile_candidate_limit(profile_boost, base_limit, cap=PROFILE_CANDIDATE_LIMIT_CAP):
    """Scale the RETRIEVAL candidate pool up with profile richness (value-5).

    Pure function (no I/O) so it is trivially unit-testable. The SHOWN result
    count stays = base_limit; this only widens the candidate set that the
    profile re-rank gets to reorder. Anonymous / thin-profile callers pass
    profile_boost=None (or an empty boost) and get back exactly base_limit, so
    there is no cost regression and the legacy path is preserved.

    Richness signal = number of distinct profile target terms (desired skills +
    learning-goal keywords + skill-gap terms) the learner has. Each ~4 distinct
    terms adds one base_limit-sized "page" of candidates, capped at `cap`.

    Returns an int >= base_limit, never above `cap`.
    """
    base = max(1, int(base_limit or 1))
    if not profile_boost:
        return base
    target_terms = profile_boost.get("target_terms") if isinstance(profile_boost, dict) else None
    richness = len(target_terms) if target_terms else 0
    if richness <= 1:
        # A single (or no) target term is a thin profile — no widening.
        return base
    # Each block of ~4 distinct target terms unlocks one extra base_limit page.
    extra_pages = richness // 4
    scaled = base * (1 + extra_pages)
    # Always give a meaningfully wider pool than base for any rich profile, so
    # the re-rank has material even when base_limit is tiny (e.g. 4).
    scaled = max(scaled, base + 8)
    return max(base, min(scaled, max(base, int(cap))))


def semantic_search_courses(query, limit=5, shown_handles=None, user_prefs=None):
    detailed = semantic_search_courses_detailed(query, limit=limit,
                                                 shown_handles=shown_handles, user_prefs=user_prefs)
    if isinstance(detailed, dict) and "error" in detailed:
        return detailed
    return detailed.get("products", [])


def _should_run_cross_encoder(query, core_query_tokens, reranked, confidence, top_vector_score):
    """Gate the (paid, gpt-4o-mini) cross-encoder rerank.

    'auto' mode now only fires when the top candidates are GENUINELY AMBIGUOUS:
    no clear vector winner, AND the #1/#2 retrieval scores are bunched within
    the env-tunable ambiguity margin. A clear leader => skip the rerank entirely.
    Reversible: AI_RERANK_AMBIGUITY_MARGIN=0 restores the legacy gate;
    AI_RAG_CROSS_ENCODER=always/never override the heuristic completely."""
    mode = cross_encoder_mode()
    if mode in {"0", "false", "no", "off", "never"}:
        return False
    if mode in {"always", "1", "true", "on"}:
        return len(reranked) >= 2 and bool(core_query_tokens)
    if confidence == "high" and top_vector_score > 0.65:
        return False
    if len(reranked) <= 1:
        return False
    if len(reranked) == 2 and confidence != "low":
        return False
    if not (len(reranked) >= 2 and bool(core_query_tokens)):
        return False
    # New ambiguity gate: a clear #1 winner means reranking can't change the
    # answer, so don't pay for it. Only ambiguous (bunched) tops get reranked.
    if _top_gap_is_clear(reranked, cross_encoder_ambiguity_margin()):
        return False
    return True


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
    for i, (doc_idx, score, explain) in enumerate(candidates[:cross_encoder_max_candidates()]):
        title = products[doc_idx].get("title", "Ukendt")
        summary = (products[doc_idx].get("ai_summary", "") or "")[:150]
        vendor = products[doc_idx].get("vendor", "")
        course_lines.append(f"{i+1}. {title} ({vendor}): {summary}")
        candidate_indices.append((doc_idx, score, explain))

    if not course_lines:
        return candidates

    try:
        from ai_runtime import fast_model
        ce_model = fast_model()
        response = openai.chat.completions.create(
            model=ce_model,
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
        raw = (response.choices[0].message.content or "").strip()
        # Parse the scores — handle GPT sometimes wrapping in markdown or adding text
        if not raw:
            return candidates
        # Try to extract JSON array from response
        import re as _json_re
        json_match = _json_re.search(r'\[[\d\s,\.]+\]', raw)
        if json_match:
            raw = json_match.group(0)
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


# 2.6: Soft demotion for already-shown products (not excluded, just ranked lower).
# Used both on the live ranking path and when re-applying the penalty on a
# search-cache hit, so the two paths can never drift apart.
_SHOWN_HANDLE_PENALTY = -0.04


def _copy_product(product):
    """Copy a product payload so callers can mutate results freely without
    corrupting the search cache or the global product index. The (large,
    never-mutated-in-practice) embedding vector is shared by reference so
    cache hits stay cheap."""
    if not isinstance(product, dict):
        return product
    return {k: (v if k == "embedding" else copy.deepcopy(v)) for k, v in product.items()}


def _build_cache_hit_result(cached_result, cached_candidates, shown_handles, pool):
    """Assemble the payload for a search-cache hit (RG-02).

    The cache stores the ranked candidate list with SESSION-NEUTRAL scores
    (any shown_handles penalty from the original call stripped at store time).
    Every hit re-applies the CURRENT caller's already-shown penalty, re-sorts
    and re-slices — so "vis mig flere" inside the TTL actually rotates results
    instead of replaying the exact same list. All returned products are copies,
    so mutating the payload never leaks back into the cache.
    """
    result = dict(cached_result)
    result["debug"] = copy.deepcopy(cached_result.get("debug") or {})
    result["debug"]["cache_hit"] = True

    shown_handles = shown_handles or set()
    if cached_candidates:
        rescored = []
        for cand in cached_candidates:
            handle = cand.get("handle", "")
            penalty = _SHOWN_HANDLE_PENALTY if handle in shown_handles else 0.0
            explain = copy.deepcopy(cand.get("explain") or {})
            explain["shown_penalty"] = round(penalty, 4)
            reasons = [r for r in explain.get("pref_reasons") or [] if r != "already_shown"]
            if penalty:
                reasons.append("already_shown")
            explain["pref_reasons"] = reasons
            rescored.append((cand, cand.get("score", 0.0) + penalty, explain))
        rescored.sort(key=lambda item: item[1], reverse=True)
        rescored = rescored[:pool]
        result["products"] = [_copy_product(cand.get("product")) for cand, _, _ in rescored]
        result["debug"]["selected"] = [
            {
                "handle": cand.get("handle"),
                "title": (cand.get("product") or {}).get("title"),
                "score": round(score, 4),
                "explain": explain,
            }
            for cand, score, explain in rescored
        ]
    else:
        # Defensive fallback for entries without a candidate list — still
        # decouple the products from the cached payload.
        result["products"] = [_copy_product(p) for p in cached_result.get("products") or []]
    result["debug"]["shown_handles_count"] = len(shown_handles)
    return result


# ── Relevance floor (step 10) ──
# Absolute floor for SINGLE-SOURCE candidate lists (cosine/BM25 scale, where a
# decent vector hit scores ~0.3-0.8). RRF-FUSED scores live on a much smaller
# scale (1/(60+rank) per leg ⇒ top ≈ 0.03-0.05), so the absolute floor would
# silently filter EVERY fused candidate — the vector leg could never surface a
# Danish paraphrase on its own. Fused lists therefore use a relative-to-top
# floor instead.
_MIN_COMBINED_SCORE = 0.12
# Relative floor for RRF-fused (BM25+vector) lists: keep candidates within 25%
# of the top score; 0.02 is a hard bottom so noise never passes on a very weak top.
_RRF_RELATIVE_FLOOR_RATIO = 0.25
_RRF_FLOOR_MIN = 0.02
# Pure-semantic queries (zero lexical hits on the core query tokens) keep this
# many top vector candidates regardless of floor — compound-heavy Danish
# paraphrases share no token with the right course but still match semantically.
_PURE_SEMANTIC_KEEP = 3
# Graceful backoff: when the floor filters EVERYTHING but the index did have
# candidates, surface this many of the best pre-filter candidates (marked
# below_threshold) instead of claiming the catalog holds nothing.
_BELOW_THRESHOLD_KEEP = 2


def _apply_score_floor(reranked, *, both_sources, pure_semantic=False):
    """Apply the minimum-relevance floor (step 10) — pure, unit-testable helper.

    Args:
        reranked: best-first list of tuples whose SECOND element is the
            combined score (typically ``(doc_idx, score, explain)``).
        both_sources: True when the list was RRF-fused from BOTH BM25 and
            vector results (small RRF score scale) → relative-to-top floor.
            False for single-source lists (cosine/BM25 scale) → the legacy
            absolute floor ``_MIN_COMBINED_SCORE`` applies unchanged.
        pure_semantic: True when zero candidates had any lexical hit on the
            core query tokens — the top ``_PURE_SEMANTIC_KEEP`` candidates are
            then retained regardless of floor.

    Returns:
        ``(kept, meta)`` where meta has keys:
            floor: the numeric floor that was applied
            filtered_below_threshold: how many candidates the floor removed
            below_threshold: True when the floor removed everything and the
                best pre-filter candidates were returned as a low-confidence
                backoff (caller should set confidence='low' and hedge).
    """
    meta = {"floor": 0.0, "filtered_below_threshold": 0, "below_threshold": False}
    if not reranked:
        return [], meta

    if both_sources:
        top_score = max(item[1] for item in reranked)
        floor = max(_RRF_FLOOR_MIN, top_score * _RRF_RELATIVE_FLOOR_RATIO)
    else:
        floor = _MIN_COMBINED_SCORE
    meta["floor"] = floor

    keep_top_n = _PURE_SEMANTIC_KEEP if pure_semantic else 0
    kept = [item for pos, item in enumerate(reranked)
            if item[1] >= floor or pos < keep_top_n]
    meta["filtered_below_threshold"] = len(reranked) - len(kept)

    if not kept:
        # Graceful backoff: better to show the closest matches we DO have,
        # clearly marked below-threshold, than to return an empty result.
        kept = list(reranked[:_BELOW_THRESHOLD_KEEP])
        meta["below_threshold"] = True
        meta["filtered_below_threshold"] = len(reranked) - len(kept)
    return kept, meta


def semantic_search_courses_detailed(query, limit=5, shown_handles=None,
                                     user_prefs=None, candidate_limit=None):
    """
    Hybrid search: combines vector similarity with BM25 keyword matching
    using weighted Reciprocal Rank Fusion for best results.

    Args:
        shown_handles: set of product handles already shown this session (soft deprioritized)
        user_prefs: dict with optional keys {location, format, budget_range} from user profile
        candidate_limit: optional retrieval pool size (value-5). When None or
            <= limit, behaviour is byte-for-byte the legacy path — exactly `limit`
            products are returned from the standard candidate pool. When a caller
            with a rich profile passes a larger value, the lexical+vector
            candidate set (already fetched in-memory, no extra API/embedding
            cost) is widened and up to `candidate_limit` ranked products are
            returned so a downstream profile re-rank has more material to
            reorder. The caller is responsible for slicing back to its shown
            top-N. Defaults to None so all existing callers are unaffected.

    Returns:
        dict with "products", "debug", "confidence" keys on success,
        dict with "error" key on failure
    """
    products = load_augmented_products()
    if not products:
        return {"error": "index_not_loaded", "message": "Produktindekset er ikke indlæst."}

    # value-5: the candidate pool (how many ranked products we return) can be
    # widened beyond the shown `limit` for rich-profile callers. `pool` only
    # ever grows the candidate set; it never shrinks below `limit`. The internal
    # BM25/vector fetch widths scale with it so the wider pool is actually filled
    # from the lexical+vector indices (both in-memory — no extra paid calls).
    pool = max(int(limit or 1), int(candidate_limit) if candidate_limit else 0)
    retrieval_width = max(20, pool)

    # 5.3: Check search cache (shown_handles excluded from key — the cache holds
    # session-neutral candidate scores, so every hit re-applies the caller's own
    # already-shown penalty, re-sorts and re-slices in _build_cache_hit_result)
    cache_key = _get_search_cache_key(query, pool, shown_handles, user_prefs)
    now = time.time()
    if cache_key in _search_cache:
        cached_result, cached_candidates, cached_at = _search_cache[cache_key]
        if now - cached_at < _SEARCH_CACHE_TTL:
            # LRU: refresh access time
            _search_cache[cache_key] = (cached_result, cached_candidates, now)
            return _build_cache_hit_result(cached_result, cached_candidates,
                                           shown_handles, pool)
        else:
            del _search_cache[cache_key]

    shown_handles = shown_handles or set()

    # ── 1+2. Vector + BM25 in parallel ──
    query_tokens = _tokenize(query)
    core_query_tokens = _core_query_tokens(query_tokens)
    expanded_tokens, fuzzy_corrections = _expand_query_tokens(query_tokens)
    bm25_ranked = _bm25_search(expanded_tokens, limit=retrieval_width)

    vector_ranked = []
    query_vector = None
    skip_embedding = _bm25_confident_enough(core_query_tokens, bm25_ranked)
    if not skip_embedding:
        query_vector = get_query_embedding(query)
        if query_vector:
            vector_ranked = _vector_search(query_vector, limit=retrieval_width)

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
        # Widen the BM25 "strong match" guard in lock-step with the pool so a
        # bigger candidate pool keeps more real lexical hits (legacy default 10).
        bm25_top_set = {bidx for bidx, _ in bm25_ranked[:max(10, pool)]}

        # Keep results that are either strong vector matches or strong BM25 matches
        top_rrf_score = fused[0][1] if fused else 0
        filtered_fused = []
        for doc_idx, rrf_score in fused:
            vec_score = vector_scores.get(doc_idx, 0)
            is_bm25_hit = doc_idx in bm25_top_set
            # Relative gap: keep if within 50% of top score, or strong BM25 match
            if rrf_score >= top_rrf_score * 0.5 or is_bm25_hit:
                filtered_fused.append((doc_idx, rrf_score))
        fused = filtered_fused if filtered_fused else fused[:pool]

    # ── 5. Title-match scoring + topical sanity check ──
    # Strong differentiation: courses where query tokens appear in the TITLE
    # are much more relevant than courses where they only appear in body text
    pure_semantic = False  # zero lexical hits → protect top vector candidates in step 10
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

        # Prefer results with at least some lexical match. When NO candidate
        # has a lexical hit the query is pure-semantic (Danish paraphrase /
        # compound mismatch) — the floor in step 10 must not empty the result.
        with_hits = [x for x in title_scored if x[2] > 0]
        pure_semantic = not with_hits
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
            shown_penalty = _SHOWN_HANDLE_PENALTY
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
    top_vector_score = vector_ranked[0][1] if vector_ranked else 0
    pre_confidence = (
        "high" if top_vector_score > 0.65
        else "medium" if top_vector_score > 0.45
        else "low"
    )
    cross_encoder_applied = False
    if len(reranked) >= 2 and core_query_tokens:
        if _should_run_cross_encoder(query, core_query_tokens, reranked, pre_confidence, top_vector_score):
            # Let the reranker reorder the head it scores, but preserve the tail
            # so a widened candidate pool (pool > limit) keeps its extra
            # candidates for the caller's downstream profile re-rank.
            rerank_head_len = min(len(reranked), max(limit, cross_encoder_max_candidates()))
            reranked_head = _crossencoder_rerank(
                query, reranked[:rerank_head_len], products, limit=rerank_head_len
            )
            # RG-02: record reality AT THE CALL SITE — the rerank only counts as
            # applied when the reranker actually scored the head (its error
            # fallback returns the input unchanged, without cross_encoder_score
            # markers). The old post-hoc re-derivation could disagree with what
            # actually happened and corrupt AI_RERANK_AMBIGUITY_MARGIN tuning data.
            cross_encoder_applied = any(
                isinstance(explain, dict) and "cross_encoder_score" in explain
                for _, _, explain in reranked_head
            )
            reranked = reranked_head + reranked[rerank_head_len:]

    # ── 9. Confidence signal for AI ──
    confidence = pre_confidence

    # ── 10. Apply relevance floor (relative for RRF-fused, absolute for
    # single-source) + graceful below-threshold backoff + return top N ──
    reranked, floor_meta = _apply_score_floor(
        reranked,
        both_sources=bool(vector_ranked and bm25_ranked),
        pure_semantic=pure_semantic,
    )
    filtered_below_threshold = floor_meta["filtered_below_threshold"]
    below_threshold = floor_meta["below_threshold"]
    if below_threshold:
        # Everything fell below the floor but the index DID have candidates —
        # surface the closest matches as an explicit low-confidence suggestion
        # so the model hedges ("tættest på, men ikke et præcist match") instead
        # of claiming kataloget intet har.
        confidence = "low"
        for _doc_idx, _score, explain in reranked:
            if isinstance(explain, dict):
                explain["below_threshold"] = True

    # Return up to `pool` ranked products. When pool == limit (the default for
    # every legacy caller) this is byte-for-byte the old behaviour; a rich-profile
    # caller that asked for a wider pool gets the extra candidates and is expected
    # to slice back to its own shown top-N after its profile re-rank.
    result_indices = [doc_idx for doc_idx, _, _ in reranked[:pool]]
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
        "score_floor": round(floor_meta["floor"], 4),
        "below_threshold": below_threshold,
        "pure_semantic": pure_semantic,
        "cross_encoder_applied": cross_encoder_applied,
        "embedding_skipped": skip_embedding,
        "top_vector_score": round(top_vector_score, 4),
        "confidence": confidence,
        "effective_limit": limit,
        "candidate_pool": pool,
        "shown_handles_count": len(shown_handles),
        "selected": [
            {
                "handle": products[idx].get("handle"),
                "title": products[idx].get("title"),
                "score": round(score, 4),
                "explain": explain,
            }
            for idx, score, explain in reranked[:pool]
        ]
    }
    result = {"products": selected_products, "debug": diagnostics,
              "confidence": confidence, "below_threshold": below_threshold}

    # 5.3: Store in search cache. The candidate list is stored with SESSION-
    # NEUTRAL scores (this call's shown_handles penalty stripped) so later hits
    # — possibly from sessions with different shown_handles — can re-apply
    # their own penalty, re-sort and re-slice (see _build_cache_hit_result).
    cached_candidates = []
    for doc_idx, score, explain in reranked:
        if doc_idx >= len(products):
            continue
        explain_dict = explain if isinstance(explain, dict) else {}
        neutral_score = score - (explain_dict.get("shown_penalty") or 0.0)
        cached_candidates.append({
            "handle": products[doc_idx].get("handle", ""),
            "score": neutral_score,
            "explain": explain_dict,
            "product": products[doc_idx],
        })
    if len(_search_cache) >= _SEARCH_CACHE_MAX:
        oldest_key = min(_search_cache, key=lambda k: _search_cache[k][-1])
        del _search_cache[oldest_key]
    _search_cache[cache_key] = (result, cached_candidates, time.time())

    # Return a payload decoupled from the cache and the global product index,
    # so callers can mutate it freely without corrupting later cache hits.
    result_out = dict(result)
    result_out["products"] = [_copy_product(p) for p in selected_products]
    result_out["debug"] = copy.deepcopy(diagnostics)
    return result_out


def _normalize_profile_boost(profile_boost):
    """Normalize a raw profile_boost dict into lowercased sets.

    Returns None when profile_boost is falsy (keeps the legacy code path
    byte-for-byte identical). Otherwise returns a dict with three sets:
      - target_terms: lowercased skill/goal/skill-gap keywords the learner wants
      - completed_handles: lowercased product handles already completed
      - completed_titles: lowercased product titles already completed
    Robust to list/set/str inputs and missing keys.
    """
    if not profile_boost:
        return None

    def _to_lower_set(value):
        if not value:
            return set()
        if isinstance(value, str):
            value = [value]
        out = set()
        for item in value:
            if item is None:
                continue
            s = str(item).strip().lower()
            if s:
                out.add(s)
        return out

    return {
        "target_terms": _to_lower_set(profile_boost.get("target_terms")),
        "completed_handles": _to_lower_set(profile_boost.get("completed_handles")),
        "completed_titles": _to_lower_set(profile_boost.get("completed_titles")),
    }


def _profile_score_adjustment(product, norm_boost):
    """Compute a multiplicative factor for a product given a normalized profile_boost.

    Returns 1.0 (no change) when norm_boost is None — preserving the exact
    legacy score. Otherwise:
      - BOOST: each distinct target term found in the product's
        title/tags/skills/category/primary_topic multiplies the score up
        (capped, moderate). A matching term adds +0.18 to the multiplier,
        capped at +0.54 (i.e. up to ~1.54x) so relevant matches rise but a
        single strong lexical winner is not overwhelmed.
      - DEMOTE: products whose handle or title is in completed_* are pushed
        down (multiplier 0.05) so they sort to the very end without being
        removed.
    Returns (factor, matched_terms, is_completed).
    """
    if norm_boost is None:
        return 1.0, [], False

    handle = str(product.get("handle", "") or "").lower()
    title_lower = str(product.get("title", "") or "").lower()

    # Demotion check first — completed courses go last regardless of match.
    is_completed = (
        (handle and handle in norm_boost["completed_handles"])
        or (title_lower and title_lower in norm_boost["completed_titles"])
    )

    matched_terms = []
    target_terms = norm_boost["target_terms"]
    if target_terms:
        # Build a haystack of the product's most signalling fields.
        haystack_parts = [title_lower]
        haystack_parts.append(str(product.get("product_type", "") or "").lower())
        primary_topic = str(
            (product.get("structured_metadata") or {}).get("primary_topic", "") or ""
        ).lower()
        if primary_topic:
            haystack_parts.append(primary_topic)
        tags = product.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        for tag in tags or []:
            haystack_parts.append(str(tag or "").lower())
        # structured skills, when present
        for skill in (product.get("structured_metadata") or {}).get("skills", []) or []:
            haystack_parts.append(str(skill or "").lower())
        haystack = " ".join(p for p in haystack_parts if p)

        for term in target_terms:
            if term and term in haystack:
                matched_terms.append(term)

    factor = 1.0
    if matched_terms:
        factor += min(len(matched_terms) * 0.18, 0.54)
    if is_completed:
        factor *= 0.05  # strong demotion, never deletion

    return factor, matched_terms, is_completed


def hybrid_rank_products(filtered_products, query, all_products, limit=5, profile_boost=None):
    """
    Rank a pre-filtered set of products using hybrid search.
    Used by filter_courses when a query is also provided.

    profile_boost (optional): dict like
        {"target_terms": set/list of lowercased skill/goal keywords,
         "completed_handles": set, "completed_titles": set}
    When provided, products matching the learner's target terms are boosted
    and completed courses are demoted (pushed to the end, not removed).
    When None (default) the ranking is byte-for-byte the legacy behaviour, so
    anonymous users and any existing callers are completely unaffected.
    """
    if not query or not filtered_products:
        return filtered_products[:limit]

    norm_boost = _normalize_profile_boost(profile_boost)

    query_tokens = _tokenize(query)
    # Use the true topical/core tokens for the BM25 confidence gate (matching
    # semantic_search_courses_detailed); the synonym-expanded set is used for
    # BM25 scoring below.
    core_query_tokens = _core_query_tokens(query_tokens)
    # RG-02: synonym expansion depends only on the query (loop-invariant) —
    # compute it ONCE here instead of inside the per-product loop (it runs
    # fuzzywuzzy scans over the whole synonym table, O(synonyms × tokens)).
    expanded_query_tokens, _ = _expand_query_tokens(query_tokens)

    # Build index mapping from filtered products to their global indices
    handle_to_product = {p.get("handle"): p for p in filtered_products}
    handle_to_global_idx = {}
    for idx, p in enumerate(all_products):
        if p.get("handle") in handle_to_product:
            handle_to_global_idx[p.get("handle")] = idx

    # BM25 on filtered set — skip embedding when keyword match is strong
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
            df = len(_bm25_index.get(qt, {})) if _bm25_index else 1
            idf = math.log((_bm25_N - df + 0.5) / (df + 0.5) + 1.0) if _bm25_N else 1.0
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(_bm25_avg_dl, 1)))
            score += idf * tf_norm
        bm25_scored.append((handle, score))
    bm25_scored.sort(key=lambda x: x[1], reverse=True)

    bm25_for_confidence = [
        (handle_to_global_idx[h], s)
        for h, s in bm25_scored
        if h in handle_to_global_idx
    ]
    skip_embedding = _bm25_confident_enough(core_query_tokens, bm25_for_confidence)

    # Vector scoring
    vector_ranked = []
    if not skip_embedding:
        query_vector = get_query_embedding(query)
        if query_vector:
            for handle, global_idx in handle_to_global_idx.items():
                vec = all_products[global_idx].get("embedding")
                if vec:
                    score = cosine_similarity(query_vector, vec)
                    vector_ranked.append((handle, score))
            vector_ranked.sort(key=lambda x: x[1], reverse=True)

    # Simple RRF on handles
    rrf = defaultdict(float)
    k = 60
    for rank, (handle, _) in enumerate(vector_ranked):
        rrf[handle] += 1.0 / (k + rank + 1)
    for rank, (handle, _) in enumerate(bm25_scored):
        rrf[handle] += 1.0 / (k + rank + 1)

    # ── Profile-conditioned re-weighting (Theme C, value-5) ──
    # Only runs for logged-in callers that supplied profile_boost. When
    # norm_boost is None this loop is skipped entirely, so the legacy ordering
    # is preserved byte-for-byte.
    if norm_boost is not None:
        adjusted = {}
        for handle, base_rrf in rrf.items():
            product = handle_to_product.get(handle)
            if product is None:
                adjusted[handle] = base_rrf
                continue
            factor, _matched, _completed = _profile_score_adjustment(product, norm_boost)
            adjusted[handle] = base_rrf * factor
        rrf = adjusted

    ranked_handles = sorted(rrf.items(), key=lambda x: x[1], reverse=True)
    return [handle_to_product[h] for h, _ in ranked_handles[:limit] if h in handle_to_product]
