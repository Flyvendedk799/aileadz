# Verifying Futurematch webhooks

Every webhook delivery is an HTTPS `POST` with a JSON body and these headers:

| Header | Meaning |
|---|---|
| `X-Webhook-Signature` | `t=<unix seconds>,v1=<hex hmac>` |
| `X-Webhook-Timestamp` | the same timestamp, for convenience |
| `X-Webhook-Id` | id of the outbox event (stable across retries). Use it to de-duplicate. |
| `X-Event-Type` | e.g. `order.approved` |
| `X-Company-Slug` | your company slug |

The body is JSON `{"event": "<type>", "data": {...}, "timestamp": "<ISO time>", "company_slug": "<slug>"}`
(`event_bus.py`). A failed delivery is retried by the background worker, up to 5 attempts, then
marked failed (`event_bus.MAX_DELIVERY_ATTEMPTS`).

## How the signature is built

```
signed_payload = "<t>." + <raw request body, exactly as received>
v1             = hex( HMAC_SHA256( your_webhook_secret, signed_payload ) )
```

Because the timestamp is part of the signed payload, a captured delivery cannot
be replayed later: reject anything whose timestamp is more than **5 minutes**
away from your clock. The secret is generated server-side when the webhook is created (`POST /api/v1/webhooks`) and returned in that response as `secret`.

## Receiver checklist

1. Read the **raw** body. Do not re-serialise the JSON before verifying.
2. Split the header into `t` and `v1`.
3. Recompute the HMAC and compare in **constant time**.
4. Reject if `abs(now - t) > 300`.
5. Ignore (but return 2xx for) an `X-Webhook-Id` you have already processed.
6. Respond `2xx` within 5 seconds (the delivery timeout, `event_bus.DELIVERY_TIMEOUT_SECONDS`). A `3xx` response is treated as a **failed delivery**:
   redirects are never followed. Serve the endpoint directly on its final URL.

Delivery only ever goes to public `http`/`https` addresses. URLs that resolve to
private, loopback or link-local addresses are refused.

## Python

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t, v1 = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)
```

## Node.js

```js
const crypto = require("crypto");

function verify(secret, header, rawBody, tolerance = 300) {
  const parts = Object.fromEntries(header.split(",").map(p => p.split("=")));
  const t = parseInt(parts.t, 10);
  if (!t || Math.abs(Date.now() / 1000 - t) > tolerance) return false;
  const expected = crypto.createHmac("sha256", secret)
    .update(`${t}.`).update(rawBody).digest("hex");
  const a = Buffer.from(expected), b = Buffer.from(parts.v1 || "");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

## Migrating from the old scheme

The previous `X-Webhook-Signature` was a bare HMAC of the body with no timestamp.
It is no longer sent. Update your receiver to the format above; the secret does
not change.
