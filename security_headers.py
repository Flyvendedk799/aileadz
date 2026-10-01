"""
Security response headers for Futurematch.

register_security_headers(app) attaches an @app.after_request hook that adds a
small set of defensive HTTP headers to every response:

  - X-Content-Type-Options: nosniff
  - X-Frame-Options: SAMEORIGIN   (skipped when the route sets its own
    Content-Security-Policy frame-ancestors, e.g. the embeddable widget)
  - Referrer-Policy: strict-origin-when-cross-origin
  - Permissions-Policy: camera/geolocation/payment off, microphone on for the
    push-to-talk voice input only
  - Content-Security-Policy: ENFORCED (S-5.1). The policy still allows inline
    script/style because the Jinja templates ship inline handlers, but it pins
    every external script/style/font origin to the CDNs the UI actually uses and
    locks down object-src, base-uri, form-action and frame-ancestors. Violations
    are reported to /csp-report. Set CSP_ENFORCE=0 to drop back to report-only
    (break-glass if a page is found that needs another origin).
  - Strict-Transport-Security (S-5.1) on HTTPS requests only: a year by default
    (HSTS_MAX_AGE), subdomains opt-in (HSTS_INCLUDE_SUBDOMAINS=1).

Design notes (production-safety):
  - This module never raises. Header construction is wrapped in try/except and a
    failure simply leaves the response untouched, so a bug here can never take
    down a request.
  - Headers are only *added* if not already present, so a blueprint that sets
    its own (e.g. a tighter policy on a specific route) keeps winning.
"""

import logging
import os

from flask import request

# External origins the UI legitimately loads from (templates + static JS audit).
_SCRIPT_HOSTS = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com https://cdn.plot.ly"
_STYLE_HOSTS = "https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com"
_FONT_HOSTS = "https://fonts.gstatic.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net"

# 'unsafe-inline' / 'unsafe-eval' stay until the inline handlers and marked.js are
# nonce-ified (tracked separately); everything else is pinned. img-src allows any
# https image because course images come from many vendor domains.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' " + _SCRIPT_HOSTS + "; "
    "style-src 'self' 'unsafe-inline' " + _STYLE_HOSTS + "; "
    "font-src 'self' data: " + _FONT_HOSTS + "; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob:; "
    "connect-src 'self' https://cdn.jsdelivr.net https://unpkg.com; "
    "worker-src 'self' blob:; "
    "frame-src 'self'; "
    "frame-ancestors 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "report-uri /csp-report"
)

_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), geolocation=(), payment=(), microphone=(self)",
}


def _csp_enforced():
    return os.environ.get("CSP_ENFORCE", "1").strip().lower() not in ("0", "false", "no", "off")


def _hsts_value():
    """Strict-Transport-Security value, or None when disabled."""
    try:
        max_age = int(os.environ.get("HSTS_MAX_AGE", "31536000"))
    except ValueError:
        max_age = 31536000
    if max_age <= 0:
        return None
    value = "max-age=%d" % max_age
    if os.environ.get("HSTS_INCLUDE_SUBDOMAINS", "0").strip().lower() in ("1", "true", "yes", "on"):
        value += "; includeSubDomains"
    return value


def _request_is_https():
    try:
        return bool(request.is_secure) or (request.headers.get("X-Forwarded-Proto", "") or "").lower() == "https"
    except Exception:
        return False


def register_security_headers(app):
    """Attach an after_request hook that adds defensive security headers.

    Safe to call once during create_app(). Never raises; if attaching fails the
    caller's try/except can degrade gracefully.
    """

    @app.route("/csp-report", methods=["POST"], endpoint="csp_report")
    def _csp_report():
        """Collect CSP violation reports (S-5.1). Bounded, rate limited, logged only."""
        from flask import Response
        try:
            import rate_limit
            if rate_limit.hit("csp:" + (request.remote_addr or ""), 30, 60):
                body = request.get_data(cache=False, as_text=True)[:2000]
                logging.warning("CSP violation report: %s", " ".join(body.split()))
        except Exception:
            pass
        return Response(status=204)

    @app.after_request
    def _apply_security_headers(response):
        try:
            has_route_csp = "Content-Security-Policy" in response.headers
            route_frames = has_route_csp and "frame-ancestors" in response.headers["Content-Security-Policy"]
            for name, value in _STATIC_HEADERS.items():
                if name == "X-Frame-Options" and route_frames:
                    continue   # the route's CSP frame-ancestors is the authority
                if name not in response.headers:
                    response.headers[name] = value
            # S-5.1: the CSP is ENFORCED (CSP_ENFORCE=0 -> report-only break-glass).
            csp_header = "Content-Security-Policy" if _csp_enforced() else "Content-Security-Policy-Report-Only"
            if csp_header not in response.headers:
                response.headers[csp_header] = _CSP
            # S-5.1: HSTS, only on HTTPS responses (a plain-HTTP dev server must not pin).
            if _request_is_https() and os.environ.get("SANDBOX") != "1" \
                    and "Strict-Transport-Security" not in response.headers:
                hsts = _hsts_value()
                if hsts:
                    response.headers["Strict-Transport-Security"] = hsts
            # Long-lived caching for static assets that still go through the
            # worker (before the PythonAnywhere /static nginx mapping is set, or
            # on the dev server). Asset URLs are versioned with ?v=N, so caching
            # them aggressively is safe. Flask's own static view already sets a
            # Cache-Control from SEND_FILE_MAX_AGE_DEFAULT, so we only fill it in
            # when absent and never override a more specific one.
            if (request.path or "").startswith("/static/") and "Cache-Control" not in response.headers:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        except Exception as exc:  # never let header logic break a response
            logging.warning("Security headers not applied: %s", exc)
        return response

    return app
