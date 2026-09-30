"""
Tiny in-process sliding-window rate limiter (S-1.11, S-2.2).

Deliberately dependency free. State is per worker process, which is good enough
for abuse brakes (Whisper spend, login guessing); it is not a billing meter.

    from rate_limit import hit, is_limited, reset

    if not hit("voice:" + user, limit=20, window=300):
        return "too many", 429
"""

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_events = defaultdict(deque)
_MAX_KEYS = 50000


def _prune(dq, now, window):
    while dq and now - dq[0] > window:
        dq.popleft()


def hit(key, limit, window, now=None):
    """Record one event for ``key``. Returns True if it is allowed, False if the
    ``limit`` per ``window`` seconds has been exceeded (the event is then not
    counted, so a blocked caller does not extend its own block)."""
    now = time.time() if now is None else now
    with _lock:
        if len(_events) > _MAX_KEYS:
            for k in [k for k, v in _events.items() if not v or now - v[-1] > window]:
                _events.pop(k, None)
        dq = _events[key]
        _prune(dq, now, window)
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def count(key, window, now=None):
    now = time.time() if now is None else now
    with _lock:
        dq = _events.get(key)
        if not dq:
            return 0
        _prune(dq, now, window)
        return len(dq)


def is_limited(key, limit, window, now=None):
    """Non-consuming check: True when ``key`` is already at/over ``limit``."""
    return count(key, window, now) >= limit


def retry_after(key, window, now=None):
    now = time.time() if now is None else now
    with _lock:
        dq = _events.get(key)
        if not dq:
            return 0
        return max(0, int(window - (now - dq[0])) + 1)


def reset(key=None):
    with _lock:
        if key is None:
            _events.clear()
        else:
            _events.pop(key, None)
