"""
Timestamped HMAC signing for outbound webhooks (S-3.3).

Header:   X-Webhook-Signature: t=<unix seconds>,v1=<hex hmac>
Signed:   "<t>." + raw request body, HMAC-SHA256 with the subscription secret.
Replay:   receivers reject a timestamp outside a tolerance window (default 5 min)
          and should de-duplicate on X-Webhook-Id.

The old scheme signed the body alone, so any captured delivery could be replayed
forever. See docs/WEBHOOK_VERIFICATION.md for receiver code.
"""

import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300


def sign(secret, body, timestamp=None):
    """Return the ``X-Webhook-Signature`` header value and the timestamp used."""
    ts = int(time.time() if timestamp is None else timestamp)
    mac = hmac.new(str(secret or "").encode("utf-8"), b"%d." % ts + body, hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, mac), ts


def verify(secret, header, body, now=None, tolerance=TOLERANCE_SECONDS):
    """True iff ``header`` is a valid, fresh signature of ``body``."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        ts = int(parts["t"])
        candidate = parts["v1"]
    except (KeyError, ValueError):
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance:
        return False
    expected = hmac.new(str(secret or "").encode("utf-8"), b"%d." % ts + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, candidate)
