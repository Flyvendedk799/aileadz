"""
Per-user knowledge index — a semantic memory of what the AI knows about a user.

Why this module exists
----------------------
The AI used to pick which memories to inject by raw keyword overlap
(``select_relevant_memories``). That misses paraphrases ("jeg vil gerne lede
et team" never matches a memory labelled "ambition om ledelse") and knows
nothing about structured profile facts or earlier conversations. This module
keeps one ``user_knowledge`` row per atomic fact — a memory, a profile fact or a
conversation digest — with an optional embedding, so the model can *recall*
relevant context fluently instead of being handed a checklist.

Design rules:
  * Rows are derived data. The source of truth stays in user_memories / the
    profile tables / conversation summaries; ``sync_user`` diffs by content hash
    and re-derives. Losing this table only costs a re-embed.
  * Embeddings are optional. Rows without a vector (offline, no key, API down,
    model/dims changed) still serve a keyword fallback — search never fails
    just because OpenAI is unreachable.
  * Every SQL statement is parameterised and scoped ``WHERE username = %s``.
  * Every public helper is guarded: it never raises into the chat turn.
"""
import hashlib
import json
import os
import struct
import time

from flask import current_app
import MySQLdb.cursors

try:  # numpy is in requirements, but the index must not hard-depend on it.
    import numpy as _np
except Exception:  # pragma: no cover - exercised by patching _np = None
    _np = None

try:  # Shared per-worker TTL cache (repo root). Fallback keeps us importable.
    from perf_cache import cache_get as _cache_get, cache_set as _cache_set
except Exception:  # pragma: no cover
    _FALLBACK_CACHE = {}

    def _cache_get(key):
        entry = _FALLBACK_CACHE.get(key)
        if entry is None or entry[0] < time.monotonic():
            _FALLBACK_CACHE.pop(key, None)
            return None, False
        return entry[1], True

    def _cache_set(key, value, ttl):
        _FALLBACK_CACHE[key] = (time.monotonic() + float(ttl), value)


SOURCE_TYPES = ("memory", "profile_fact", "conversation")
_SYNC_TYPES = ("memory", "profile_fact")

SYNC_THROTTLE_SECONDS = 300
_THROTTLE_KEY = "user_knowledge.sync"
EMBED_BATCH_LIMIT = 32
EMBED_TIMEOUT_SECONDS = 1.5
SEARCH_SCAN_CAP = 500

_SOURCE_ID_MAX = 64
_FACT_CONTENT_MAX = 2000
_CONVERSATION_CONTENT_MAX = 4000

_SEMANTIC_WEIGHT = 0.8
_KEYWORD_WEIGHT = 0.2


def _log(msg):
    print(f"[UserKnowledge] {msg}")


# ── Feature flags ──

def _env_on(name, default=True):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def knowledge_enabled():
    """Master switch (AI_USER_KNOWLEDGE, default on). Off = no sync, no search."""
    return _env_on("AI_USER_KNOWLEDGE", True)


def _openai_key_available():
    if (os.getenv("OPENAI_API_KEY") or "").strip():
        return True
    try:
        import openai
        return bool((getattr(openai, "api_key", None) or "").strip())
    except Exception:
        return False


def embeddings_enabled():
    """Embedding switch (AI_USER_KNOWLEDGE_EMBEDDINGS, default on). Also False
    when no OpenAI key is resolvable — then everything runs keyword-only rather
    than burning a timeout per request on a call that can't succeed."""
    if not _env_on("AI_USER_KNOWLEDGE_EMBEDDINGS", True):
        return False
    return _openai_key_available()


# ── Vector packing ──

def _normalise(values):
    try:
        vals = [float(v) for v in values]
    except Exception:
        return None
    if not vals:
        return None
    norm = sum(v * v for v in vals) ** 0.5
    if norm > 0:
        vals = [v / norm for v in vals]
    return vals


def pack_vector(vec):
    """L2-normalise and serialise as little-endian float32 bytes. Normalising
    at write time turns cosine similarity into a plain dot product at search
    time. Returns b"" for an empty/invalid vector."""
    if vec is None:
        return b""
    if _np is not None:
        try:
            arr = _np.asarray(vec, dtype="<f4").ravel()
            if arr.size == 0:
                return b""
            norm = float(_np.linalg.norm(arr))
            if norm > 0:
                arr = arr / norm
            return arr.astype("<f4").tobytes()
        except Exception:
            pass
    vals = _normalise(vec)
    if not vals:
        return b""
    return struct.pack("<%df" % len(vals), *vals)


def unpack_vector(blob):
    """Inverse of pack_vector: numpy float32 array (or list of floats without
    numpy). Returns None for empty/corrupt blobs."""
    if not blob:
        return None
    try:
        data = bytes(blob)
    except Exception:
        return None
    if len(data) % 4:
        return None
    if _np is not None:
        try:
            return _np.frombuffer(data, dtype="<f4")
        except Exception:
            pass
    try:
        return list(struct.unpack("<%df" % (len(data) // 4), data))
    except Exception:
        return None


def _dot(a, b):
    if _np is not None:
        try:
            return float(_np.dot(_np.asarray(a, dtype="f4"), _np.asarray(b, dtype="f4")))
        except Exception:
            pass
    return float(sum(x * y for x, y in zip(a, b)))


# ── Fact derivation ──

def _clean(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def _source_id(prefix, key):
    """Stable source id that fits VARCHAR(64); very long keys collapse to a hash
    so the UNIQUE(username, source_type, source_id) key stays deterministic."""
    key = _clean(key)
    sid = f"{prefix}:{key}" if prefix else key
    if len(sid) <= _SOURCE_ID_MAX:
        return sid
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return (f"{prefix}:#{digest}" if prefix else f"#{digest}")[:_SOURCE_ID_MAX]


def _content_hash(content):
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()


def _with_detail(sentence, detail, limit=300):
    detail = _clean(detail)
    if detail:
        sentence = f"{sentence} {detail[:limit]}"
    return sentence[:_FACT_CONTENT_MAX]


_SUMMARY_FIELDS = (
    ("headline", "Profiloverskrift"),
    ("bio", "Om brugeren"),
    ("goals", "Brugerens mål"),
    ("target_role", "Ønsket rolle"),
    ("preferred_location", "Foretrukken lokation"),
    ("preferred_format", "Foretrukken undervisningsform"),
    ("budget_range", "Budget til kurser"),
)


def profile_facts(profile):
    """Flatten a ``get_full_profile`` dict into (source_id, Danish sentence)
    pairs — one atomic fact per row so a search hit is a precise, quotable
    statement rather than a whole profile blob."""
    facts = []
    seen = set()

    def add(sid, content):
        content = _clean(content)[:_FACT_CONTENT_MAX]
        if sid and content and sid not in seen:
            seen.add(sid)
            facts.append((sid, content))

    if not isinstance(profile, dict):
        return facts

    for e in profile.get("experience") or []:
        try:
            title, company = _clean(e.get("title")), _clean(e.get("company"))
            if not title and not company:
                continue
            role = title or "medarbejder"
            s = f"Arbejder som {role}" if e.get("is_current") else f"Har arbejdet som {role}"
            if company:
                s += f" hos {company}"
            start, end = _clean(e.get("start_year")), _clean(e.get("end_year"))
            if e.get("is_current") and start:
                s += f" (siden {start})"
            elif start or end:
                s += f" ({start or '?'}–{end or '?'})"
            key = e.get("id") if e.get("id") is not None else f"{title}|{company}".lower()
            add(_source_id("experience", key), _with_detail(s + ".", e.get("description")))
        except Exception:
            continue

    for ed in profile.get("education") or []:
        try:
            degree, inst = _clean(ed.get("degree")), _clean(ed.get("institution"))
            if not degree and not inst:
                continue
            s = f"Har uddannelsen {degree}" if degree else "Har en uddannelse"
            if inst:
                s += f" fra {inst}"
            year = _clean(ed.get("year_completed"))
            if year:
                s += f" ({year})"
            key = ed.get("id") if ed.get("id") is not None else f"{degree}|{inst}".lower()
            add(_source_id("education", key), _with_detail(s + ".", ed.get("description")))
        except Exception:
            continue

    for sk in profile.get("skills") or []:
        try:
            name = _clean(sk.get("name"))
            if not name:
                continue
            s = f"Kompetence: {name}"
            extra = []
            if _clean(sk.get("level")):
                extra.append(f"niveau {_clean(sk.get('level'))}")
            if _clean(sk.get("category")):
                extra.append(f"kategori {_clean(sk.get('category'))}")
            if extra:
                s += " (" + ", ".join(extra) + ")"
            add(_source_id("skill", name.lower()), s + ".")
        except Exception:
            continue

    for c in profile.get("certifications") or []:
        try:
            name = _clean(c.get("name"))
            if not name:
                continue
            s = f"Har certificeringen {name}"
            if _clean(c.get("issuer")):
                s += f" fra {_clean(c.get('issuer'))}"
            if _clean(c.get("expiry_date")):
                s += f" (udløber {_clean(c.get('expiry_date'))})"
            key = c.get("id") if c.get("id") is not None else name.lower()
            add(_source_id("certification", key), s + ".")
        except Exception:
            continue

    for lang in profile.get("languages") or []:
        try:
            name = _clean(lang.get("language"))
            if not name:
                continue
            s = f"Taler {name}"
            if _clean(lang.get("proficiency")):
                s += f" (niveau {_clean(lang.get('proficiency'))})"
            add(_source_id("language", name.lower()), s + ".")
        except Exception:
            continue

    for g in profile.get("learning_goals") or []:
        try:
            title = _clean(g.get("title"))
            if not title:
                continue
            s = f"Udviklingsmål: {title}"
            extra = []
            if _clean(g.get("target_date")):
                extra.append(f"inden {_clean(g.get('target_date'))}")
            if _clean(g.get("status")):
                extra.append(f"status {_clean(g.get('status'))}")
            if extra:
                s += " (" + ", ".join(extra) + ")"
            key = g.get("id") if g.get("id") is not None else title.lower()
            add(_source_id("goal", key), _with_detail(s + ".", g.get("description")))
        except Exception:
            continue

    for course in profile.get("completed_courses") or []:
        try:
            title = _clean(course.get("title"))
            if not title:
                continue
            s = f"Har gennemført kurset {title}"
            if _clean(course.get("vendor")):
                s += f" hos {_clean(course.get('vendor'))}"
            key = course.get("id") if course.get("id") is not None else title.lower()
            add(_source_id("course", key), s + ".")
        except Exception:
            continue

    for field, label in _SUMMARY_FIELDS:
        value = _clean(profile.get(field))
        if value:
            add(_source_id("summary", field), f"{label}: {value[:1500]}")

    return facts


def memory_facts(memories):
    """(memory id, "label — detail") pairs from ``get_memories`` rows."""
    facts = []
    seen = set()
    for m in memories or []:
        try:
            mid = m.get("id")
            label = _clean(m.get("label"))
            if mid is None or not label:
                continue
            sid = _source_id("", mid)
            if sid in seen:
                continue
            seen.add(sid)
            content = label
            detail = _clean(m.get("detail"))
            if detail:
                content = f"{label} — {detail}"
            facts.append((sid, content[:_FACT_CONTENT_MAX]))
        except Exception:
            continue
    return facts


# ── Embedding seams (patched in tests) ──

def _current_embedding_spec():
    """(model, dims) the query side will use; None when rag is unavailable."""
    try:
        from app1 import rag
        return rag.embedding_model(), int(rag.embedding_dimensions())
    except Exception:
        return None


def _embed_batch(texts):
    try:
        from app1 import rag
        return rag.embed_texts(texts, timeout=EMBED_TIMEOUT_SECONDS)
    except Exception:
        return [None] * len(texts)


def _query_embedding(text):
    try:
        from app1 import rag
        return rag.get_query_embedding(text)
    except Exception:
        return None


# ── DB helpers ──

def _cursor():
    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def _rollback():
    try:
        current_app.mysql.connection.rollback()
    except Exception:
        pass


def _close(cur):
    try:
        if cur is not None:
            cur.close()
    except Exception:
        pass


def _embed_pending(username, limit=EMBED_BATCH_LIMIT):
    """Embed up to ``limit`` rows lacking a current-model vector in ONE call.
    Returns how many rows got a vector. The UPDATE pins content_hash so a row
    whose content changed mid-flight is not stamped with a stale vector, and
    keeps updated_at so "when" still reflects the fact, not the embedding."""
    if not embeddings_enabled():
        return 0
    spec = _current_embedding_spec()
    if not spec:
        return 0
    model, dims = spec
    cur = None
    try:
        cur = _cursor()
        cur.execute(
            "SELECT id, content, content_hash FROM user_knowledge "
            "WHERE username = %s AND (embedding IS NULL OR embedding_model IS NULL "
            "OR embedding_model <> %s OR dims IS NULL OR dims <> %s) "
            "ORDER BY updated_at DESC LIMIT %s",
            (username, model, dims, int(limit)),
        )
        rows = list(cur.fetchall() or [])
        if not rows:
            return 0
        vectors = _embed_batch([r.get("content") or "" for r in rows]) or []
        done = 0
        for row, vec in zip(rows, vectors):
            if not vec or len(vec) != dims:
                continue
            cur.execute(
                "UPDATE user_knowledge SET embedding = %s, embedding_model = %s, dims = %s, "
                "updated_at = updated_at "
                "WHERE username = %s AND id = %s AND content_hash = %s",
                (pack_vector(vec), model, dims, username, row.get("id"), row.get("content_hash")),
            )
            done += 1
        if done:
            current_app.mysql.connection.commit()
        return done
    except Exception as e:
        _rollback()
        _log(f"embed pending failed: {type(e).__name__}")
        return 0
    finally:
        _close(cur)


# ── Sync ──

def sync_user(username, profile=None, memories=None, *, force=False):
    """Bring the user's memory/profile_fact rows in line with their sources.

    Diffs sha1(content) against stored rows: inserts new facts, rewrites
    changed ones (clearing the now-stale embedding), deletes facts whose source
    vanished, then embeds a bounded batch. Throttled per user for
    SYNC_THROTTLE_SECONDS because it runs on hot chat paths; ``force`` bypasses
    that right after an explicit write. Never raises — returns a stats dict.
    """
    stats = {"status": "ok", "inserted": 0, "updated": 0, "deleted": 0,
             "unchanged": 0, "embedded": 0}
    if not username:
        stats["status"] = "no_user"
        return stats
    if not knowledge_enabled():
        stats["status"] = "disabled"
        return stats
    throttle_key = (_THROTTLE_KEY, str(username))
    try:
        if not force:
            _, hit = _cache_get(throttle_key)
            if hit:
                stats["status"] = "throttled"
                return stats
        # Stamp before working so concurrent turns don't pile onto one sync.
        _cache_set(throttle_key, time.time(), SYNC_THROTTLE_SECONDS)
    except Exception:
        pass

    # Load sources per type; a type whose loader failed is left untouched
    # rather than diffed against an empty list (which would wipe it).
    desired = {}
    try:
        from app1 import user_profile_db as db
    except Exception:
        db = None
    if profile is None and db is not None:
        try:
            profile = db.get_full_profile(username)
        except Exception as e:
            _log(f"profile load failed: {type(e).__name__}")
            profile = None
    if memories is None and db is not None:
        try:
            memories = db.get_memories(username)
        except Exception as e:
            _log(f"memory load failed: {type(e).__name__}")
            memories = None
    if profile is not None:
        desired["profile_fact"] = dict(profile_facts(profile))
    if memories is not None:
        desired["memory"] = dict(memory_facts(memories))
    if not desired:
        stats["status"] = "error"
        return stats

    types = [t for t in _SYNC_TYPES if t in desired]
    cur = None
    try:
        cur = _cursor()
        placeholders = ", ".join(["%s"] * len(types))
        cur.execute(
            "SELECT id, source_type, source_id, content_hash FROM user_knowledge "
            f"WHERE username = %s AND source_type IN ({placeholders})",
            tuple([username] + types),
        )
        existing = {(r.get("source_type"), str(r.get("source_id"))): r
                    for r in (cur.fetchall() or [])}

        wrote = False
        for stype in types:
            for sid, content in desired[stype].items():
                chash = _content_hash(content)
                row = existing.get((stype, sid))
                if row is not None and row.get("content_hash") == chash:
                    stats["unchanged"] += 1
                    continue
                cur.execute(
                    "INSERT INTO user_knowledge (username, source_type, source_id, mode, "
                    "content, content_hash, embedding, embedding_model, dims) "
                    "VALUES (%s, %s, %s, NULL, %s, %s, NULL, NULL, NULL) "
                    "ON DUPLICATE KEY UPDATE content = VALUES(content), "
                    "content_hash = VALUES(content_hash), embedding = NULL, "
                    "embedding_model = NULL, dims = NULL",
                    (username, stype, sid, content, chash),
                )
                stats["updated" if row is not None else "inserted"] += 1
                wrote = True

        # Only delete within types we actually loaded (the SELECT already
        # filters, this keeps a failed loader from ever wiping its type).
        stale_ids = [r.get("id") for (stype, sid), r in existing.items()
                     if stype in desired and sid not in desired[stype]]
        for i in range(0, len(stale_ids), 200):
            chunk = stale_ids[i:i + 200]
            cur.execute(
                "DELETE FROM user_knowledge WHERE username = %s AND id IN ("
                + ", ".join(["%s"] * len(chunk)) + ")",
                tuple([username] + chunk),
            )
            stats["deleted"] += len(chunk)
            wrote = True
        if wrote:
            current_app.mysql.connection.commit()
    except Exception as e:
        _rollback()
        _log(f"sync failed for user: {type(e).__name__}")
        stats["status"] = "error"
        return stats
    finally:
        _close(cur)

    stats["embedded"] = _embed_pending(username)
    return stats


def index_conversation_summary(username, session_id, mode, summary_text):
    """Upsert the rolling digest of one conversation as a searchable row, so
    "hvad talte vi om sidst?" can be answered across sessions and modes.
    Skips the write when the digest is unchanged and already embedded. Never
    raises."""
    try:
        if not username or not session_id or not knowledge_enabled():
            return None
        content = _clean(summary_text)[:_CONVERSATION_CONTENT_MAX]
        if not content:
            return None
        sid = _source_id("", session_id)
        mode_val = (_clean(mode)[:20] or None) if mode else None
        chash = _content_hash(content)
    except Exception:
        return None

    cur = None
    try:
        cur = _cursor()
        cur.execute(
            "SELECT id, content_hash, embedding_model, dims FROM user_knowledge "
            "WHERE username = %s AND source_type = %s AND source_id = %s",
            (username, "conversation", sid),
        )
        row = cur.fetchone()
        spec = _current_embedding_spec() if embeddings_enabled() else None
        unchanged = bool(row) and row.get("content_hash") == chash
        embedded_current = bool(row) and spec is not None and \
            row.get("embedding_model") == spec[0] and row.get("dims") == spec[1]
        if unchanged and (embedded_current or spec is None):
            return None
        if not unchanged:
            cur.execute(
                "INSERT INTO user_knowledge (username, source_type, source_id, mode, "
                "content, content_hash, embedding, embedding_model, dims) "
                "VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, NULL) "
                "ON DUPLICATE KEY UPDATE mode = VALUES(mode), content = VALUES(content), "
                "content_hash = VALUES(content_hash), embedding = NULL, "
                "embedding_model = NULL, dims = NULL",
                (username, "conversation", sid, mode_val, content, chash),
            )
            current_app.mysql.connection.commit()
        if spec is not None:
            model, dims = spec
            vectors = _embed_batch([content]) or []
            vec = vectors[0] if vectors else None
            if vec and len(vec) == dims:
                cur.execute(
                    "UPDATE user_knowledge SET embedding = %s, embedding_model = %s, dims = %s, "
                    "updated_at = updated_at "
                    "WHERE username = %s AND source_type = %s AND source_id = %s "
                    "AND content_hash = %s",
                    (pack_vector(vec), model, dims, username, "conversation", sid, chash),
                )
                current_app.mysql.connection.commit()
    except Exception as e:
        _rollback()
        _log(f"conversation index failed: {type(e).__name__}")
    finally:
        _close(cur)
    return None


# ── Search ──

def _iso(value):
    if value is None:
        return None
    try:
        return value.isoformat(sep=" ", timespec="seconds")
    except Exception:
        return str(value)


def search(username, query, *, types=None, k=8, exclude_source_ids=None, min_score=0.0):
    """Rank the user's knowledge rows against ``query``.

    score = 0.8·cosine + 0.2·keyword_overlap when both the query and the row
    have a vector from the current model/dims; otherwise keyword overlap alone
    (|q∩r| / |q|, same tokenizer as the legacy memory selector). Rows with no
    signal at all (score <= 0) are dropped. Returns at most ``k`` dicts sorted
    by score; ``updated_at`` is an ISO string so results are JSON-safe. Never
    raises.
    """
    try:
        if not username or not knowledge_enabled():
            return []
        q = _clean(query)
        if not q:
            return []
        try:
            k = max(1, int(k))
        except Exception:
            k = 8
        wanted = None
        if types:
            if isinstance(types, str):
                types = [types]
            wanted = [t for t in types if t in SOURCE_TYPES]
            if not wanted:
                return []
        excluded = {str(s) for s in (exclude_source_ids or [])}

        cur = None
        try:
            cur = _cursor()
            sql = ("SELECT source_type, source_id, mode, content, embedding, embedding_model, "
                   "dims, updated_at FROM user_knowledge WHERE username = %s")
            params = [username]
            if wanted:
                sql += " AND source_type IN (" + ", ".join(["%s"] * len(wanted)) + ")"
                params.extend(wanted)
            sql += " ORDER BY updated_at DESC LIMIT %s"
            params.append(SEARCH_SCAN_CAP)
            cur.execute(sql, tuple(params))
            rows = list(cur.fetchall() or [])
        finally:
            _close(cur)
        if not rows:
            return []

        from app1.user_profile_db import _memory_tokens
        q_tokens = _memory_tokens(q)

        spec = _current_embedding_spec() if embeddings_enabled() else None
        q_vec = None
        if spec is not None and any(
                r.get("embedding") and r.get("embedding_model") == spec[0]
                and r.get("dims") == spec[1] for r in rows):
            raw = _query_embedding(q)
            if raw and len(raw) == spec[1]:
                q_vec = unpack_vector(pack_vector(raw))

        scored = []
        for order, r in enumerate(rows):
            sid = str(r.get("source_id"))
            if sid in excluded:
                continue
            content = r.get("content") or ""
            kw = 0.0
            if q_tokens:
                kw = len(q_tokens & _memory_tokens(content)) / float(len(q_tokens))
            score = kw
            if q_vec is not None and r.get("embedding_model") == spec[0] \
                    and r.get("dims") == spec[1]:
                r_vec = unpack_vector(r.get("embedding"))
                if r_vec is not None and len(r_vec) == len(q_vec):
                    score = _SEMANTIC_WEIGHT * _dot(q_vec, r_vec) + _KEYWORD_WEIGHT * kw
            if score <= 0 or score < min_score:
                continue
            scored.append((score, order, {
                "source_type": r.get("source_type"),
                "source_id": sid,
                "mode": r.get("mode"),
                "content": content,
                "score": round(float(score), 4),
                "updated_at": _iso(r.get("updated_at")),
            }))
        # Score desc; ties keep recency order (rows arrive updated_at DESC).
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [item for _, _, item in scored[:k]]
    except Exception as e:
        _log(f"search failed: {type(e).__name__}")
        return []


# ── Model tool: recall_about_user(query, scope) ──

_SCOPE_TYPES = {
    "alt": None,
    "hukommelse": ("memory",),
    "samtaler": ("conversation",),
    "profil": ("profile_fact",),
}
_TYPE_LABELS = {"memory": "hukommelse", "conversation": "samtale", "profile_fact": "profil"}


def execute_recall_about_user(args, username):
    """Tool executor: let the model look up what it knows about the user when
    it needs it, instead of front-loading everything into the prompt. Returns
    a JSON string; errors are Danish and never leak exception text."""
    if not username:
        return json.dumps({"status": "error", "message": "Brugeren er ikke logget ind."},
                          ensure_ascii=False)
    try:
        args = args if isinstance(args, dict) else {}
        query = _clean(args.get("query"))[:500]
        if not query:
            return json.dumps({"status": "error",
                               "message": "Angiv hvad du vil slå op om brugeren (query)."},
                              ensure_ascii=False)
        scope = _clean(args.get("scope") or "alt").lower()
        if scope not in _SCOPE_TYPES:
            scope = "alt"
        types = _SCOPE_TYPES[scope]
        if not knowledge_enabled():
            return json.dumps({"status": "error",
                               "message": "Opslag i brugerens viden er slået fra lige nu."},
                              ensure_ascii=False)
        sync_user(username)  # throttled; keeps freshly saved facts findable
        results = search(username, query, types=types, k=8)
        payload = {
            "status": "success",
            "count": len(results),
            "results": [
                {
                    "type": _TYPE_LABELS.get(r.get("source_type"), r.get("source_type")),
                    "text": r.get("content"),
                    "mode": r.get("mode"),
                    "when": (r.get("updated_at") or "")[:10] or None,
                }
                for r in results
            ],
        }
        if not results:
            payload["message"] = "Jeg fandt ikke noget relevant om brugeren til den forespørgsel."
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        _log(f"recall_about_user failed: {type(e).__name__}")
        return json.dumps({"status": "error",
                           "message": "Kunne ikke slå op i det, jeg ved om brugeren, lige nu."},
                          ensure_ascii=False)
