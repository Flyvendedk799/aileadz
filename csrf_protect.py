"""
CSRF protection for the session-authenticated app (S-2.1).

* Flask-WTF ``CSRFProtect`` enforces a token on every POST/PUT/PATCH/DELETE.
* Key-authenticated surfaces (enterprise API, SCIM, token-drain, the embeddable
  widget and the anonymous demo chat) are exempt: they carry no ambient
  cookie authority a third-party page could ride on.
* Tokens reach the browser three ways, so no template can "forget":
    1. a ``<meta name="csrf-token">`` and ``csrf.js`` are injected into every
       HTML page (``inject_csrf`` after_request);
    2. every ``<form method="post">`` gets a hidden ``csrf_token`` input;
    3. ``csrf.js`` adds ``X-CSRFToken`` to same-origin ``fetch``/XHR writes and
       tops up forms created later by JavaScript.
* Tokens are valid for the lifetime of the session (no 1 h limit), so a chat
  tab left open overnight still works.
* ``SANDBOX=1`` (tests/dev harness) turns enforcement off unless
  ``CSRF_IN_SANDBOX=1``; tests that verify CSRF flip
  ``app.config['WTF_CSRF_ENABLED']`` on their own app instance.
"""

import logging
import os
import re

from flask import current_app, jsonify, request, session

try:  # guarded: a missing wheel must never stop the app from booting in dev
    from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
    _HAVE_WTF = True
except Exception:  # pragma: no cover
    CSRFError = Exception
    CSRFProtect = None
    generate_csrf = None
    _HAVE_WTF = False

logger = logging.getLogger(__name__)

csrf = CSRFProtect() if _HAVE_WTF else None

# Endpoints that authenticate by something other than the session cookie.
EXEMPT_BLUEPRINTS = ("api_enterprise", "scim")
EXEMPT_ENDPOINTS = (
    "app1.widget_ask",     # embeddable widget: origin-bound, anonymous, third-party iframe
    "app1.demo_ask",       # anonymous public demo chat
    "sso.sso_callback",    # IdP -> us POST; protected by state/signature instead
    "csp_report",          # browser-generated violation reports carry no token
)

_FORM_RE = re.compile(r"<form\b([^>]*)>", re.IGNORECASE)
_POST_RE = re.compile(r"""\bmethod\s*=\s*["']?post["']?""", re.IGNORECASE)
_ACTION_EXTERNAL_RE = re.compile(r"""\baction\s*=\s*["']?https?://""", re.IGNORECASE)
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_TOKEN_INPUT_RE = re.compile(r"""name\s*=\s*["']csrf_token["']""", re.IGNORECASE)


def csrf_enabled(app):
    return bool(app.config.get("WTF_CSRF_ENABLED", True))


def init_csrf(app):
    """Wire CSRF protection into ``app``. Call once, AFTER blueprints are
    registered (the exemptions look views up by endpoint)."""
    if not _HAVE_WTF:
        logger.error("Flask-WTF is not installed: CSRF protection is NOT active.")
        return False

    sandbox = os.environ.get("SANDBOX") == "1" and os.environ.get("CSRF_IN_SANDBOX") != "1"
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", None)        # session-lifetime tokens
    app.config.setdefault("WTF_CSRF_SSL_STRICT", False)       # proxies mangle Referer/Host
    app.config["WTF_CSRF_ENABLED"] = not sandbox
    csrf.init_app(app)

    for bp_name in EXEMPT_BLUEPRINTS:
        if bp_name in app.blueprints:
            csrf.exempt(app.blueprints[bp_name])   # a Blueprint object, not a name
    for endpoint in EXEMPT_ENDPOINTS:
        view = app.view_functions.get(endpoint)
        if view is not None:
            csrf.exempt(view)

    @app.errorhandler(CSRFError)
    def _csrf_failed(err):
        wants_json = (
            request.is_json
            or request.path.startswith("/api")
            or "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.headers.get("X-CSRFToken") is not None
        )
        msg = "Din session er udløbet eller siden er forældet. Genindlæs siden og prøv igen."
        if wants_json:
            resp = jsonify({"success": False, "error": msg, "message": msg, "csrf": True})
            resp.status_code = 400
            return resp
        html = (
            "<!doctype html><html lang='da'><head><meta charset='utf-8'>"
            "<title>Siden er udløbet</title></head><body style='font-family:system-ui;"
            "max-width:32rem;margin:4rem auto;padding:0 1rem'>"
            "<h1>Siden er udløbet</h1><p>%s</p>"
            "<p><a href='javascript:history.back()'>Gå tilbage</a> &middot; "
            "<a href='/'>Til forsiden</a></p></body></html>" % msg
        )
        from flask import Response
        return Response(html, status=400, mimetype="text/html")

    app.after_request(inject_csrf)
    return True


def _token():
    return generate_csrf() if generate_csrf else ""


def inject_csrf(response):
    """after_request: add the meta tag, csrf.js and hidden form inputs to HTML.

    Registered LAST so it runs FIRST on the way out (Flask runs after_request
    hooks in reverse order), i.e. before response compression.
    """
    try:
        if not csrf_enabled(current_app):
            return response
        if response.direct_passthrough or response.status_code >= 300 and response.status_code < 400:
            return response
        if not (response.mimetype or "").startswith("text/html"):
            return response
        if request.endpoint in EXEMPT_ENDPOINTS or (request.endpoint or "").startswith("app1.widget"):
            return response
        if response.is_streamed:
            return response
        body = response.get_data(as_text=True)
        if "<html" not in body[:2000].lower() and "</head" not in body.lower() and "<form" not in body.lower():
            return response  # an HTML fragment/partial, not a page

        token = _token()
        changed = False

        if "</head" in body.lower() and 'name="csrf-token"' not in body:
            tags = (
                '<meta name="csrf-token" content="%s">'
                '<script src="/static/futurematch/assets/csrf.js"></script>' % token
            )
            body, n = _HEAD_CLOSE_RE.subn(lambda m: tags + m.group(0), body, count=1)
            changed = changed or bool(n)

        def _add_token(m):
            attrs = m.group(1)
            if not _POST_RE.search(attrs) or _ACTION_EXTERNAL_RE.search(attrs):
                return m.group(0)
            return m.group(0) + '<input type="hidden" name="csrf_token" value="%s">' % token

        # Only touch forms that do not already carry a token input right after.
        def _guarded(m):
            tail = body[m.end(): m.end() + 200]
            if _TOKEN_INPUT_RE.search(tail.split("</form", 1)[0]):
                return m.group(0)
            return _add_token(m)

        new_body = _FORM_RE.sub(_guarded, body)
        if new_body != body:
            body = new_body
            changed = True

        if changed:
            response.set_data(body)
    except Exception as exc:  # never break a page over this
        logger.warning("csrf injection skipped: %s", exc)
    return response
