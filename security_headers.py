"""
Security response headers for Futurematch.

register_security_headers(app) attaches an @app.after_request hook that adds a
small set of defensive HTTP headers to every response:

  - X-Content-Type-Options: nosniff
  - X-Frame-Options: SAMEORIGIN
  - Referrer-Policy: strict-origin-when-cross-origin
  - Content-Security-Policy-Report-Only: a *permissive* policy that does NOT
    block the existing inline-<script>/inline-style Jinja+CDN frontend. It is
    report-only on purpose: the goal of this first wave is to start observing
    violations (e.g. unexpected script origins) without breaking anything.

Design notes (production-safety):
  - This module never raises. Header construction is wrapped in try/except and a
    failure simply leaves the response untouched, so a bug here can never take
    down a request.
  - Headers are only *added* if not already present, so a blueprint that sets
    its own (e.g. a tighter X-Frame-Options on a specific route) keeps winning.
  - The CSP is report-only, so even if a directive is too strict it cannot break
    the live UI; browsers only log the violation.
"""

import logging

# Report-only CSP. Intentionally permissive so the current inline-script /
# inline-style / CDN (fonts.googleapis.com, cdn.jsdelivr.net, cdnjs, ...) frontend
# keeps rendering. 'unsafe-inline' + 'unsafe-eval' are required because the Jinja
# templates ship inline <script> blocks and marked.js evaluates dynamically.
# https: and data: keep CDN assets, web fonts and inline images working.
_CSP_REPORT_ONLY = (
    "default-src 'self' https: data: blob:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
    "style-src 'self' 'unsafe-inline' https:; "
    "img-src 'self' data: https: blob:; "
    "font-src 'self' data: https:; "
    "connect-src 'self' https:; "
    "frame-ancestors 'self'; "
    "object-src 'none'; "
    "base-uri 'self'"
)

_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def register_security_headers(app):
    """Attach an after_request hook that adds defensive security headers.

    Safe to call once during create_app(). Never raises; if attaching fails the
    caller's try/except can degrade gracefully.
    """

    @app.after_request
    def _apply_security_headers(response):
        try:
            for name, value in _STATIC_HEADERS.items():
                if name not in response.headers:
                    response.headers[name] = value
            # Report-only CSP: collect violations, never block.
            if "Content-Security-Policy-Report-Only" not in response.headers:
                response.headers["Content-Security-Policy-Report-Only"] = _CSP_REPORT_ONLY
        except Exception as exc:  # never let header logic break a response
            logging.warning("Security headers not applied: %s", exc)
        return response

    return app
