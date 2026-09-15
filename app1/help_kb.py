"""Curated platform-help knowledge base for the employee AI advisor.

Answers "how does the platform work" questions (upload a CV, get an order
approved, delete what the AI remembers, ...) from a small set of hand-written
Danish markdown articles in ``app1/help_kb/*.md``. It is NOT a course
recommender — course search lives in ``app1.rag``.

Public API (called by the tool registry):
    help_kb_enabled() -> bool
    load_articles() -> list[dict]
    chunk_articles(articles) -> list[dict]
    build_index(path=None, *, embed=True) -> dict
    search_help(query, k=3) -> list[dict]
    execute_search_platform_help(args) -> str   (JSON tool result)
    TRIGGER_HOW_TOKENS, TRIGGER_PLATFORM_NOUNS, looks_like_platform_help(query)
    SEARCH_PLATFORM_HELP_TOOL                    (OpenAI function schema)

Retrieval is hybrid: keyword overlap (title + front-matter keywords weigh most)
blended with cosine similarity when an embedding index exists and the chunk's
hash still matches the current markdown. With no index, no network, or a failed
embedding call it silently degrades to keyword-only. Nothing here raises.

Article format::

    ---
    title: Sådan bestiller du et kursus
    slug: bestilling-og-godkendelse
    url: /min-tidslinje
    keywords: bestil, tilmeld, godkendelse
    audience: employee
    ---
    ## Kort fortalt
    ...

Rebuild the (optional) embedding index with ``python -m app1.help_kb --build``.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(_HERE, "help_kb")
INDEX_PATH = os.path.join(_HERE, "help_kb_index.json")

MAX_CHUNK_CHARS = 1200
EXCERPT_CHARS = 420
MAX_QUERY_CHARS = 500
MAX_CHUNKS_PER_ARTICLE = 2

# Hybrid scoring knobs.
_COSINE_WEIGHT = 0.55
_KEYWORD_WEIGHT = 0.45
_MIN_KEYWORD_SCORE = 0.15
_MIN_COSINE = 0.30

# Field weights for keyword matching.
_W_TITLE_KEYWORDS = 1.0
_W_SECTION = 0.6
_W_TEXT = 0.45

_FALSY = {"0", "false", "no", "off", "nej"}


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def help_kb_enabled() -> bool:
    """AI_HELP_KB env flag — on unless explicitly disabled."""
    raw = os.getenv("AI_HELP_KB")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in _FALSY


# ---------------------------------------------------------------------------
# Loading + parsing
# ---------------------------------------------------------------------------

def _parse_front_matter(raw: str) -> Tuple[Dict[str, str], str]:
    """Split ``---``-fenced ``key: value`` front matter from the body."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    meta: Dict[str, str] = {}
    if not text.startswith("---\n"):
        return meta, text.strip()
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text.strip()
    header = text[4:end]
    rest = text[end + 4:]
    nl = rest.find("\n")
    body = rest[nl + 1:] if nl != -1 else ""
    for line in header.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta, body.strip()


def _md_files(kb_dir: str) -> List[str]:
    try:
        names = sorted(n for n in os.listdir(kb_dir) if n.lower().endswith(".md"))
    except OSError:
        return []
    return [os.path.join(kb_dir, n) for n in names]


def load_articles(kb_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Parse every ``*.md`` article. Unreadable files are skipped, never raised."""
    kb_dir = kb_dir or KB_DIR
    articles: List[Dict[str, Any]] = []
    for path in _md_files(kb_dir):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                raw = fh.read()
            meta, body = _parse_front_matter(raw)
            slug = meta.get("slug") or os.path.splitext(os.path.basename(path))[0]
            keywords = [k.strip().lower() for k in (meta.get("keywords") or "").split(",") if k.strip()]
            articles.append({
                "slug": slug,
                "title": meta.get("title") or slug,
                "url": meta.get("url") or "",
                "keywords": keywords,
                "audience": meta.get("audience") or "employee",
                "body": body,
                "path": path,
            })
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("help_kb: could not load %s: %s", path, exc)
    return articles


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


def _split_sections(body: str) -> List[Tuple[str, str]]:
    """[(section_heading, section_text)] split on level-2 ``## `` headings."""
    sections: List[Tuple[str, str]] = []
    current_title = ""
    buf: List[str] = []
    for line in (body or "").split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            sections.append((current_title, "\n".join(buf).strip()))
            current_title = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    sections.append((current_title, "\n".join(buf).strip()))
    return [(t, s) for t, s in sections if s]


def _split_text(text: str, limit: int = MAX_CHUNK_CHARS) -> List[str]:
    """Pack paragraphs/lines/words into pieces of at most ``limit`` chars."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    units: List[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            units.append(para)
            continue
        for line in para.split("\n"):
            line = line.strip()
            if not line:
                continue
            if len(line) <= limit:
                units.append(line)
                continue
            word_buf = ""
            for word in line.split(" "):
                while len(word) > limit:
                    if word_buf:
                        units.append(word_buf)
                        word_buf = ""
                    units.append(word[:limit])
                    word = word[limit:]
                candidate = f"{word_buf} {word}" if word_buf else word
                if len(candidate) > limit:
                    units.append(word_buf)
                    word_buf = word
                else:
                    word_buf = candidate
            if word_buf:
                units.append(word_buf)

    pieces: List[str] = []
    buf = ""
    for unit in units:
        candidate = f"{buf}\n\n{unit}" if buf else unit
        if len(candidate) > limit and buf:
            pieces.append(buf)
            buf = unit
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return [p for p in pieces if p.strip()]


def _chunk_hash(slug: str, title: str, section: str, url: str,
                keywords: List[str], text: str) -> str:
    payload = json.dumps([slug, title, section, url, list(keywords), text],
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def chunk_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One chunk per ``## `` section (split further to ≤ MAX_CHUNK_CHARS)."""
    chunks: List[Dict[str, Any]] = []
    for art in articles or []:
        try:
            slug = art.get("slug") or ""
            title = art.get("title") or slug
            url = art.get("url") or ""
            keywords = list(art.get("keywords") or [])
            n = 0
            for section, section_text in _split_sections(art.get("body") or ""):
                for piece in _split_text(section_text, MAX_CHUNK_CHARS):
                    chunks.append({
                        "chunk_id": f"{slug}:{n:02d}",
                        "slug": slug,
                        "title": title,
                        "section": section,
                        "url": url,
                        "keywords": keywords,
                        "text": piece,
                        "hash": _chunk_hash(slug, title, section, url, keywords, piece),
                    })
                    n += 1
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("help_kb: chunking failed for %r: %s", art.get("slug"), exc)
    return chunks


def _embed_input(chunk: Dict[str, Any]) -> str:
    return f"{chunk.get('title', '')}\n{chunk.get('section', '')}\n{chunk.get('text', '')}"


# ---------------------------------------------------------------------------
# app1.rag bridge (all optional)
# ---------------------------------------------------------------------------

def _rag():
    try:
        from app1 import rag as rag_module
        return rag_module
    except Exception as exc:  # pragma: no cover - depends on environment
        logger.debug("help_kb: app1.rag unavailable: %s", exc)
        return None


def _clean_vector(vec: Any) -> Optional[List[float]]:
    if not isinstance(vec, (list, tuple)) or not vec:
        return None
    try:
        out = [float(x) for x in vec]
    except (TypeError, ValueError):
        return None
    if any(math.isnan(x) or math.isinf(x) for x in out):
        return None
    return out


def _local_cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Index build
# ---------------------------------------------------------------------------

def build_index(path: Optional[str] = None, *, embed: bool = True) -> Dict[str, Any]:
    """Chunk all articles, embed them (if possible) and write the JSON index.

    Embeddings come from ``app1.rag.embed_texts`` (one batched call). Missing
    function, exceptions or ``None`` entries leave ``embedding`` null; the
    index is still written and search falls back to keywords for those chunks.
    """
    path = path or INDEX_PATH
    chunks = chunk_articles(load_articles())
    embeddings: List[Optional[List[float]]] = [None] * len(chunks)
    model = None

    if embed and chunks:
        rag = _rag()
        embed_fn = getattr(rag, "embed_texts", None) if rag is not None else None
        if callable(embed_fn):
            try:
                out = embed_fn([_embed_input(c) for c in chunks])
                if isinstance(out, (list, tuple)) and len(out) == len(chunks):
                    embeddings = [_clean_vector(v) for v in out]
                else:
                    logger.warning("help_kb: embed_texts returned an unexpected shape; keyword-only index")
            except Exception as exc:
                logger.warning("help_kb: embedding failed, writing keyword-only index: %s", exc)
            if any(embeddings):
                try:
                    model = rag.embedding_model()
                except Exception:
                    model = None

    dims = next((len(v) for v in embeddings if v), None)
    index = {
        "model": model if any(embeddings) else None,
        "dims": dims,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "chunks": [dict(c, embedding=e) for c, e in zip(chunks, embeddings)],
    }

    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("help_kb: could not write index %s: %s", path, exc)

    with _cache_lock:
        _index_cache.clear()
    return index


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_articles_cache: Dict[str, Any] = {}
_index_cache: Dict[str, Any] = {}


def _current_chunks() -> List[Dict[str, Any]]:
    """Freshly chunked markdown, cached on the (name, mtime, size) signature."""
    kb_dir = KB_DIR
    sig = []
    for p in _md_files(kb_dir):
        try:
            st = os.stat(p)
            sig.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    key = (kb_dir, tuple(sig))
    with _cache_lock:
        if _articles_cache.get("key") == key:
            return _articles_cache["chunks"]
    chunks = chunk_articles(load_articles(kb_dir))
    for c in chunks:
        c["_fields"] = _chunk_fields(c)
    with _cache_lock:
        _articles_cache.clear()
        _articles_cache.update(key=key, chunks=chunks)
    return chunks


def _index_vectors() -> Tuple[Dict[str, List[float]], Optional[str]]:
    """{chunk_hash: embedding} from the on-disk index (mtime-cached)."""
    path = INDEX_PATH
    try:
        st = os.stat(path)
    except OSError:
        return {}, None
    key = (path, st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if _index_cache.get("key") == key:
            return _index_cache["vectors"], _index_cache["model"]
    vectors: Dict[str, List[float]] = {}
    model = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        model = data.get("model") if isinstance(data, dict) else None
        for c in (data.get("chunks") or []) if isinstance(data, dict) else []:
            if not isinstance(c, dict):
                continue
            vec = _clean_vector(c.get("embedding"))
            if vec and c.get("hash"):
                vectors[str(c["hash"])] = vec
    except Exception as exc:
        logger.warning("help_kb: ignoring unreadable index %s: %s", path, exc)
        vectors, model = {}, None
    with _cache_lock:
        _index_cache.clear()
        _index_cache.update(key=key, vectors=vectors, model=model)
    return vectors, model


# ---------------------------------------------------------------------------
# Tokenising + keyword scoring
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9æøåäöüé]+")

# Question/filler words that carry no topical signal for help lookup.
_HELP_STOP_WORDS = frozenset({
    "og", "i", "at", "er", "en", "et", "den", "det", "de", "til", "på", "med", "for",
    "af", "fra", "som", "om", "kan", "har", "vil", "der", "ikke", "sig", "var", "ved",
    "så", "også", "eller", "hvad", "når", "du", "jeg", "dig", "mig", "os", "min", "mit",
    "mine", "din", "dit", "dine", "man", "hvordan", "hvor", "hvorfor", "hvilke", "hvilken",
    "skal", "gør", "gøre", "får", "få", "finder", "finde", "se", "ser", "hen", "lige",
    "nu", "noget", "ud", "op", "sådan", "bare", "hvis", "virker", "platformen", "siden",
    "the", "and", "of", "to", "in", "a", "an", "is", "for", "on", "with", "how", "do",
    "does", "can", "where", "what", "my", "me", "it", "that", "this", "about", "are",
})


def _fold(token: str) -> str:
    return token.replace("aa", "å")


def _simple_tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) > 1]


def _tokenize(text: str) -> List[str]:
    tokens: Optional[List[str]] = None
    rag = _rag()
    rag_tok = getattr(rag, "_tokenize", None) if rag is not None else None
    if callable(rag_tok):
        try:
            tokens = list(rag_tok(text or ""))
        except Exception:
            tokens = None
    if tokens is None:
        tokens = _simple_tokenize(text)
    return [_fold(t) for t in tokens if t and t not in _HELP_STOP_WORDS]


def _chunk_fields(chunk: Dict[str, Any]) -> List[Tuple[float, frozenset]]:
    head = f"{chunk.get('title', '')} {' '.join(chunk.get('keywords') or [])}"
    return [
        (_W_TITLE_KEYWORDS, frozenset(_tokenize(head))),
        (_W_SECTION, frozenset(_tokenize(chunk.get("section") or ""))),
        (_W_TEXT, frozenset(_tokenize(chunk.get("text") or ""))),
    ]


def _token_match(q: str, t: str) -> float:
    if q == t:
        return 1.0
    short, long_ = (q, t) if len(q) <= len(t) else (t, q)
    if len(short) >= 4 and long_.startswith(short):
        return 0.8  # Danish inflection / compounds: godkend→godkendelse
    return 0.0


def _best_field_match(q: str, fields: List[Tuple[float, frozenset]]) -> float:
    best = 0.0
    for weight, toks in fields:
        if weight <= best:
            continue
        if q in toks:
            best = weight
            continue
        m = 0.0
        for t in toks:
            m = max(m, _token_match(q, t))
            if m >= 0.8:
                break
        best = max(best, weight * m)
    return best


def _keyword_scores(q_tokens: List[str], chunks: List[Dict[str, Any]]) -> List[float]:
    """IDF-weighted field overlap in [0, 1] for each chunk."""
    if not q_tokens or not chunks:
        return [0.0] * len(chunks)
    per_token: Dict[str, List[float]] = {}
    for q in q_tokens:
        per_token[q] = [_best_field_match(q, c.get("_fields") or _chunk_fields(c)) for c in chunks]

    n_articles = max(1, len({c.get("slug") for c in chunks}))
    idf: Dict[str, float] = {}
    for q, matches in per_token.items():
        df = len({chunks[i].get("slug") for i, m in enumerate(matches) if m > 0})
        idf[q] = math.log(1.0 + n_articles / (1.0 + df))
    total_idf = sum(idf.values()) or 1.0

    return [sum(idf[q] * per_token[q][i] for q in per_token) / total_idf for i in range(len(chunks))]


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:–-") + " …"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_help(query: str, k: int = 3) -> List[Dict[str, Any]]:
    """Top-k help chunks for ``query``: [{title, section, url, excerpt, score}]."""
    try:
        query = str(query or "").strip()[:MAX_QUERY_CHARS]
        if not query:
            return []
        try:
            k = max(1, min(10, int(k)))
        except (TypeError, ValueError):
            k = 3

        chunks = _current_chunks()
        if not chunks:
            return []

        q_tokens = list(dict.fromkeys(_tokenize(query)))
        kw_scores = _keyword_scores(q_tokens, chunks)

        # Cosine only when the index has vectors whose hash matches today's markdown.
        cos_scores: List[Optional[float]] = [None] * len(chunks)
        vectors, index_model = _index_vectors()
        chunk_vecs = [vectors.get(c["hash"]) for c in chunks] if vectors else []
        if any(chunk_vecs):
            rag = _rag()
            qvec = None
            if rag is not None:
                try:
                    current_model = rag.embedding_model() if hasattr(rag, "embedding_model") else None
                except Exception:
                    current_model = None
                if not (index_model and current_model and index_model != current_model):
                    try:
                        qvec = _clean_vector(rag.get_query_embedding(query))
                    except Exception as exc:
                        logger.info("help_kb: query embedding failed, keyword-only: %s", exc)
                        qvec = None
            if qvec:
                cos_fn = getattr(rag, "cosine_similarity", None) or _local_cosine
                for i, vec in enumerate(chunk_vecs):
                    if vec and len(vec) == len(qvec):
                        try:
                            cos_scores[i] = float(cos_fn(qvec, vec))
                        except Exception:
                            cos_scores[i] = None

        scored = []
        for i, c in enumerate(chunks):
            kw = kw_scores[i]
            cos = cos_scores[i]
            if cos is not None:
                if kw < _MIN_KEYWORD_SCORE and cos < _MIN_COSINE:
                    continue
                score = _COSINE_WEIGHT * max(0.0, cos) + _KEYWORD_WEIGHT * kw
            else:
                if kw < _MIN_KEYWORD_SCORE:
                    continue
                score = kw
            scored.append((score, i))

        scored.sort(key=lambda s: (-s[0], chunks[s[1]]["chunk_id"]))
        results: List[Dict[str, Any]] = []
        per_slug: Dict[str, int] = {}
        for score, i in scored:
            c = chunks[i]
            if per_slug.get(c["slug"], 0) >= MAX_CHUNKS_PER_ARTICLE:
                continue
            per_slug[c["slug"]] = per_slug.get(c["slug"], 0) + 1
            results.append({
                "title": c["title"],
                "section": c["section"],
                "url": c["url"],
                "excerpt": _excerpt(c["text"]),
                "score": round(score, 4),
                "slug": c["slug"],
            })
            if len(results) >= k:
                break
        return results
    except Exception as exc:  # never raise into the agent loop
        logger.warning("help_kb: search failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

_MSG_EMPTY = "Skriv hvad du leder efter, fx 'hvordan uploader jeg mit CV'."
_MSG_NO_HITS = ("Ingen hjælpeartikel matcher spørgsmålet. Gæt ikke om, hvordan platformen "
                "virker — henvis i stedet til Support (/support).")
_MSG_DISABLED = "Hjælpeartiklerne er ikke tilgængelige lige nu. Henvis til Support (/support)."
_MSG_ERROR = "Hjælpeartiklerne kunne ikke søges lige nu. Henvis til Support (/support)."

SEARCH_PLATFORM_HELP_TOOL = {
    "type": "function",
    "function": {
        "name": "search_platform_help",
        "description": (
            "Søg i Futurematchs hjælpeartikler om hvordan platformen virker: CV-upload, Mind-Map og "
            "hvad AI'en husker (og hvordan man sletter det), AI Profiler, bestilling og godkendelse, "
            "afdelingsbudget, læringsstier, udviklingsmål, obligatoriske kurser, Min læring/tidslinje, "
            "privatliv/GDPR, konto og support. Brug det til 'hvordan/hvor'-spørgsmål om platformen — "
            "IKKE til kursusanbefalinger. Svar ud fra artiklerne og henvis til deres url."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Brugerens spørgsmål om platformen."},
            },
            "required": ["query"],
        },
    },
}


def _no_results(message: str) -> str:
    return json.dumps({"status": "no_results", "message": message}, ensure_ascii=False)


def execute_search_platform_help(args: Dict[str, Any]) -> str:
    """Tool executor → JSON string. Never raises, never leaks exception text."""
    try:
        if not isinstance(args, dict):
            args = {}
        query = str(args.get("query") or "").strip()
        if not query:
            return _no_results(_MSG_EMPTY)
        if not help_kb_enabled():
            return _no_results(_MSG_DISABLED)
        try:
            k = max(1, min(5, int(args.get("k", 3))))
        except (TypeError, ValueError):
            k = 3
        results = search_help(query, k=k)
        if not results:
            return _no_results(_MSG_NO_HITS)
        payload = [
            {"title": r["title"], "section": r["section"], "url": r["url"], "excerpt": r["excerpt"]}
            for r in results
        ]
        return json.dumps({"status": "success", "count": len(payload), "results": payload},
                          ensure_ascii=False)
    except Exception as exc:
        logger.warning("help_kb: execute_search_platform_help failed: %s", exc)
        return _no_results(_MSG_ERROR)


# ---------------------------------------------------------------------------
# Cheap trigger heuristic
# ---------------------------------------------------------------------------

TRIGGER_HOW_TOKENS: Tuple[str, ...] = (
    "hvordan", "hvor finder jeg", "hvor kan jeg", "hvor ser jeg", "hvor er", "kan jeg",
    "skal jeg", "hvad gør jeg", "hvad betyder",
    "how do i", "how can i", "how to", "where",
)

TRIGGER_PLATFORM_NOUNS: Tuple[str, ...] = (
    # CV / profile
    "upload", "cv-portal", "cv portal", "profilsiden", "min profil", "profil & cv", "profiler",
    # orders / approval / budget
    "godkend", "godkendelse", "bestilling", "bestille", "bestiller", "bestilt", "tilmelding",
    "tilmelde", "anmodning", "ordre", "budget",
    # memory / mind map
    "mind-map", "mindmap", "mind map", "hukommelse", "slette", "slet",
    # learning surfaces
    "læringssti", "tidslinje", "min læring", "udviklingsmål", "mine mål",
    "obligatorisk", "lovpligtig", "compliance",
    # account / privacy / support
    "notifikation", "login", "log ind", "adgangskode", "kodeord", "indstillinger",
    "gdpr", "mine data", "persondata", "support", "platformen", "futurematch",
    # English
    "approval", "approve", "order", "memory", "memories", "delete", "password",
    "notification", "learning path", "settings",
)

_LETTER = "0-9a-zæøåäöüé"
_HOW_RES = [re.compile(rf"(?<![{_LETTER}]){re.escape(t)}(?![{_LETTER}])") for t in TRIGGER_HOW_TOKENS]
# A where-question built on any verb: "hvor uploader jeg …", "hvor sletter man …".
# Still needs a platform noun, so "hvor bliver jeg projektleder" stays a career question.
_HOW_RES.append(re.compile(rf"(?<![{_LETTER}])hvor [{_LETTER}]+ (?:jeg|man|vi)(?![{_LETTER}])"))
# Nouns: start boundary only, so inflections match (godkend → godkendt, upload → uploader).
_NOUN_RES = [re.compile(rf"(?<![{_LETTER}]){re.escape(t)}") for t in TRIGGER_PLATFORM_NOUNS]


def looks_like_platform_help(query: str) -> bool:
    """True when the text asks how/where AND names a platform concept."""
    try:
        text = re.sub(r"\s+", " ", str(query or "").lower()).strip()
        if not text:
            return False
        return any(r.search(text) for r in _HOW_RES) and any(r.search(text) for r in _NOUN_RES)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Futurematch platform-help knowledge base")
    parser.add_argument("--build", action="store_true", help="build app1/help_kb_index.json")
    parser.add_argument("--no-embed", action="store_true", help="build without embeddings")
    parser.add_argument("--path", default=None, help="index output path")
    parser.add_argument("--search", default=None, help="run a test query")
    ns = parser.parse_args(argv)

    if ns.build:
        index = build_index(ns.path, embed=not ns.no_embed)
        embedded = sum(1 for c in index["chunks"] if c.get("embedding"))
        print(f"help_kb: {len(index['chunks'])} chunks, {embedded} embedded, "
              f"model={index['model']} dims={index['dims']} -> {ns.path or INDEX_PATH}")
    if ns.search:
        for r in search_help(ns.search, k=3):
            print(f"{r['score']:.3f}  {r['title']} / {r['section']}  ({r['url']})")
    if not (ns.build or ns.search):
        parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
