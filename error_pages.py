"""Danish 404/500 pages and JSON errors for API callers (N-0.3).

The old handler answered every miss with a redirect *and* a 404 status, so the
browser sat on "Redirecting..." and never showed anything.
"""

from __future__ import annotations

import logging

from flask import jsonify, render_template, request

_JSON_PREFIXES = ('/api/', '/app1/', '/scim/', '/enterprise/', '/healthz', '/readyz')


def wants_json() -> bool:
    """True for API callers: API-ish path prefixes, XHR, or JSON-preferring Accept."""
    path = request.path or ''
    if path.startswith(_JSON_PREFIXES):
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    accept = request.accept_mimetypes
    return bool(accept.best == 'application/json' and accept['application/json'] > accept['text/html'])


def register_error_handlers(app) -> None:
    @app.errorhandler(404)
    def _not_found(error):
        if wants_json():
            return jsonify({'error': 'Siden eller ressourcen blev ikke fundet.', 'status': 404}), 404
        try:
            return render_template('fm/404.html'), 404
        except Exception:  # never let the error page itself fail
            logging.exception('404 template failed')
            return 'Siden blev ikke fundet.', 404

    @app.errorhandler(500)
    def _server_error(error):
        if wants_json():
            return jsonify({'error': 'Der opstod en fejl på serveren. Prøv igen om lidt.', 'status': 500}), 500
        try:
            return render_template('fm/500.html'), 500
        except Exception:
            return 'Der opstod en fejl. Prøv igen om lidt.', 500
