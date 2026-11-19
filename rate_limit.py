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


# ── Assistant turns ─────────────────────────────────────────────────────────
# Credits meter what a company spends; this is the brake on a script or a stuck
# client hammering an assistant endpoint. Generous for a person, per actor.
AI_TURN_MESSAGE = "Du har sendt mange beskeder på kort tid. Vent et øjeblik, og prøv igen."


def _env_int(name, default):
    import os
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


ANON_SHARED_ADDRESS_SCALE = 5


def ai_turn_allowed(actor, now=None, scale=1):
    """One assistant turn for ``actor`` (e.g. ``"user:42"``, ``"vendor:7"``,
    ``"anon:<ip>"``). False when the actor is over ``AI_RATE_TURNS_5MIN`` (default
    30) or ``AI_RATE_TURNS_HOUR`` (default 200); 0 switches a limit off.

    ``scale`` widens both limits for an actor that stands for several people:
    anonymous visitors are keyed on their address, and an office or a mobile
    carrier shares one."""
    short, hour = _env_int("AI_RATE_TURNS_5MIN", 30) * scale, _env_int("AI_RATE_TURNS_HOUR", 200) * scale
    if hour and is_limited("ai:h:%s" % actor, hour, 3600, now):
        return False
    if short and not hit("ai:5m:%s" % actor, short, 300, now):
        return False
    if hour:
        hit("ai:h:%s" % actor, hour, 3600, now)
    return True

