"""
OpenID Connect ID-token validation and JWKS handling (S-3.1).

What the login callback must prove before it trusts an identity:
  1. the ``id_token`` signature verifies against a key published by the issuer
     (JWKS), with a pinned asymmetric algorithm (``none`` / HMAC are refused);
  2. ``iss`` equals the configured issuer and ``aud`` contains our client id
     (and ``azp`` matches when there are several audiences);
  3. ``exp`` / ``iat`` are present and in range (small clock leeway);
  4. ``nonce`` equals the value we stored in the session for THIS login attempt
     (binds the token to the browser that started the flow -> no replay).

Anything else raises ``OIDCError``; callers turn that into a Danish "login
failed" message and never reveal which check failed to the user.
"""

import hmac
import json
import logging
import threading
import time

import jwt

import safe_http

logger = logging.getLogger(__name__)

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")
JWKS_TTL_SECONDS = 600
LEEWAY_SECONDS = 60


class OIDCError(Exception):
    pass


_jwks_cache = {}          # uri -> (fetched_at, jwks dict)
_jwks_lock = threading.Lock()


def clear_cache():
    with _jwks_lock:
        _jwks_cache.clear()


def fetch_jwks(jwks_uri, force=False, now=None):
    """Fetch (and cache) the issuer's JWKS over SSRF-safe HTTPS."""
    now = time.time() if now is None else now
    with _jwks_lock:
        hit = _jwks_cache.get(jwks_uri)
    if hit and not force and now - hit[0] < JWKS_TTL_SECONDS:
        return hit[1]
    try:
        status, raw = safe_http.request("GET", jwks_uri, timeout=8, max_bytes=500_000, require_https=True)
    except (safe_http.UnsafeURL, OSError) as exc:
        raise OIDCError("jwks fetch failed: %s" % exc)
    if status != 200:
        raise OIDCError("jwks fetch returned HTTP %d" % status)
    try:
        jwks = json.loads(raw.decode("utf-8"))
    except Exception:
        raise OIDCError("jwks is not JSON")
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise OIDCError("jwks has no keys")
    with _jwks_lock:
        _jwks_cache[jwks_uri] = (now, jwks)
    return jwks


def _select_key(jwks, kid, alg):
    keys = [k for k in jwks.get("keys", []) if isinstance(k, dict)]
    if kid:
        keys = [k for k in keys if k.get("kid") == kid]
    elif len(keys) != 1:
        raise OIDCError("id_token has no kid and the issuer publishes several keys")
    keys = [k for k in keys if k.get("use", "sig") == "sig"]
    if not keys:
        raise OIDCError("no matching signing key")
    try:
        return jwt.PyJWK(keys[0]).key
    except Exception as exc:
        raise OIDCError("unusable signing key: %s" % exc)


def validate_id_token(id_token, *, jwks, issuer, audience, nonce, now=None):
    """Return the verified claims or raise OIDCError."""
    if not id_token or not isinstance(id_token, str) or id_token.count(".") != 2:
        raise OIDCError("malformed id_token")
    if not issuer or not audience or not nonce:
        raise OIDCError("issuer, audience and nonce are required")
    try:
        header = jwt.get_unverified_header(id_token)
    except Exception:
        raise OIDCError("unreadable id_token header")
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise OIDCError("algorithm %r not allowed" % alg)
    key = _select_key(jwks, header.get("kid"), alg)
    try:
        options = {"require": ["exp", "iat", "iss", "aud", "sub"]}
        claims = jwt.decode(
            id_token, key=key, algorithms=[alg], audience=audience, issuer=issuer,
            leeway=LEEWAY_SECONDS, options=options,
        )
    except jwt.PyJWTError as exc:
        raise OIDCError("id_token rejected: %s" % exc)

    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != audience:
        raise OIDCError("azp mismatch")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], str(nonce)):
        raise OIDCError("nonce mismatch")
    now = time.time() if now is None else now
    if float(claims["iat"]) > now + LEEWAY_SECONDS:
        raise OIDCError("id_token issued in the future")
    return claims


def extract_identity(claims):
    """Map verified claims to the small identity dict the app uses."""
    email = None
    for key in ("email", "preferred_username", "upn"):
        val = claims.get(key)
        if isinstance(val, str) and "@" in val:
            email = val.strip().lower()
            break
    if not email:
        raise OIDCError("no e-mail claim")
    if claims.get("email_verified") is False or str(claims.get("email_verified")).lower() == "false":
        raise OIDCError("e-mail not verified by the identity provider")
    name = claims.get("name") or " ".join(
        p for p in (claims.get("given_name"), claims.get("family_name")) if p) or ""
    return {
        "email": email,
        "full_name": str(name)[:255],
        "sub": str(claims["sub"]),
        "job_title": str(claims.get("jobTitle") or "")[:150],
        "department": str(claims.get("department") or "")[:100],
    }
