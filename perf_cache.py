"""Tiny thread-safe in-process TTL cache for stable-but-expensive results.

Why this module exists
----------------------
The dashboards recompute the same heavy aggregations on every load. PythonAnywhere
offers no Redis (the repo's ``redis`` dependency is optional and used only for
distributed rate-limiting), so this is a **per-worker, in-process** cache: each
uWSGI worker keeps its own copy. That is the right trade-off for the read-heavy,
low-write dashboards it backs — a short TTL bounds how stale any one worker can be,
and the cached values (counts, aggregates, derived option lists) tolerate a few
seconds of lag.

Rules of use:
  * Only cache data that is safe to serve a few seconds stale.
  * NEVER cache per-request correctness-critical or write-through data, or anything
    keyed by the live session/user in a way another user could read.
  * The cache must only ever change *speed*, never correctness — every code path
    here degrades to calling the underlying function uncached if anything goes
    wrong.

This centralises the ad-hoc module-level ``_CACHE`` dicts already scattered around
(catalog_service.py, feature_status.py, …) behind one small, lock-guarded API.
"""
import functools
import hashlib
import logging
import os
import pickle
import threading
import time

_LOCK = threading.RLock()
# key -> (expires_at_monotonic, value)
_STORE = {}

# ── optional shared backend (N-8.2) ─────────────────────────────────────────
# With REDIS_URL set (and the ``redis`` package installed) all workers share one
# cache, so a mutation that clears a key is seen everywhere instead of only in the
# worker that handled the request. Every Redis call is guarded: on any error the
# cache silently falls back to the in-process store for that call.
_REDIS_STATE = {"client": None, "retry_at": 0.0}
_REDIS_PREFIX = "fm:cache:"


def _redis():
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    if _REDIS_STATE["client"] is not None:
        return _REDIS_STATE["client"]
    if time.monotonic() < _REDIS_STATE["retry_at"]:
        return None
    try:
        import redis
        client = redis.Redis.from_url(url, socket_timeout=0.3, socket_connect_timeout=0.3)
        client.ping()
        _REDIS_STATE["client"] = client
        return client
    except Exception as exc:
        logging.getLogger(__name__).warning("perf_cache: redis unavailable, using in-process cache (%s)", exc)
        _REDIS_STATE["retry_at"] = time.monotonic() + 60
        return None


def _redis_key(key):
    base = key[0] if isinstance(key, tuple) and key else key
    rest = key[1:] if isinstance(key, tuple) else ()
    digest = hashlib.sha1(repr(rest).encode("utf-8", "replace")).hexdigest()
    return "%s%s:%s" % (_REDIS_PREFIX, base, digest)


def cache_get(key):
    """Return (value, hit). ``hit`` is False on miss or expiry."""
    client = _redis()
    if client is not None:
        try:
            raw = client.get(_redis_key(key))
            if raw is None:
                return None, False
            return pickle.loads(raw), True
        except Exception:
            pass
    now = time.monotonic()
    with _LOCK:
        entry = _STORE.get(key)
        if entry is None:
            return None, False
        expires_at, value = entry
        if expires_at < now:
            _STORE.pop(key, None)
            return None, False
        return value, True


def cache_set(key, value, ttl):
    client = _redis()
    if client is not None:
        try:
            client.set(_redis_key(key), pickle.dumps(value), px=max(1, int(float(ttl) * 1000)))
            return
        except Exception:
            pass
    with _LOCK:
        _STORE[key] = (time.monotonic() + float(ttl), value)


def cache_clear(prefix=None):
    """Drop everything, or just keys whose string form starts with ``prefix``."""
    client = _redis()
    if client is not None:
        try:
            pattern = _REDIS_PREFIX + (prefix + "*" if prefix else "*")
            for k in client.scan_iter(match=pattern, count=200):
                client.delete(k)
        except Exception:
            pass
    with _LOCK:
        if prefix is None:
            _STORE.clear()
            return
        for k in [k for k in _STORE if _key_matches_prefix(k, prefix)]:
            _STORE.pop(k, None)


def _key_matches_prefix(key, prefix):
    base = key[0] if isinstance(key, tuple) and key else key
    return isinstance(base, str) and base.startswith(prefix)


def ttl_cache(seconds, key=None):
    """Cache a function's return value for ``seconds`` (per-worker, in-process).

    ``key`` optionally maps the call args to a hashable cache-key suffix, e.g.
    ``key=lambda company_id: company_id``. Without it the (args, kwargs) tuple is
    used, so the wrapped function must take only hashable arguments.

    If key construction or the cache layer raises, the wrapped function is simply
    called uncached — caching never changes correctness.
    """
    def decorator(func):
        base = "{}.{}".format(func.__module__, func.__qualname__)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                suffix = key(*args, **kwargs) if key is not None else (
                    args, tuple(sorted(kwargs.items())))
                ck = (base, suffix)
            except Exception:
                return func(*args, **kwargs)
            value, hit = cache_get(ck)
            if hit:
                return value
            value = func(*args, **kwargs)
            try:
                cache_set(ck, value, seconds)
            except Exception:
                pass
            return value

        # Allow callers to invalidate this function's entries after a mutation.
        wrapper.cache_clear = lambda: cache_clear(base)
        wrapper.cache_key_base = base
        return wrapper
    return decorator
