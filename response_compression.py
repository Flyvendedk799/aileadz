"""
Gzip compression for dynamic responses.

register_response_compression(app) attaches an @app.after_request hook that
gzip-compresses text responses (HTML, JSON, CSS, JS, SVG, XML) when the client
advertises gzip support. The big HR/dashboard pages render hundreds of KB of
HTML; gzip typically cuts that by ~75%, which is pure transfer-time win on every
page load.

PythonAnywhere's nginx gzips files served via the Web-tab *static* mapping, but
NOT the dynamic responses proxied from the worker — so this fills that gap.

Safety (this module never breaks a response):
  - Skips streamed / passthrough responses (SSE chat stream, send_file) — those
    must not be buffered/consumed here.
  - Skips `text/event-stream` explicitly (the /app1/ask chat SSE).
  - Only touches a small allowlist of text content types.
  - Skips anything already Content-Encoded, and bodies too small to be worth it.
  - All work is wrapped in try/except; on any error the original response is
    returned untouched.
"""
import gzip
import logging

from flask import request

# Content types worth compressing (prefix match on the part before any ';').
_COMPRESSIBLE_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "image/svg+xml",
)

# Don't bother below this size — the gzip header overhead isn't worth it and the
# CPU cost outweighs the tiny transfer saving.
_MIN_SIZE_BYTES = 600


def _is_compressible(content_type):
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct or ct == "text/event-stream":
        return False
    return any(ct.startswith(prefix) for prefix in _COMPRESSIBLE_PREFIXES)


def register_response_compression(app):
    """Attach the gzip after_request hook. Safe to call once in create_app()."""

    @app.after_request
    def _compress(response):
        try:
            if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
                return response
            # Streamed (SSE) or passthrough (send_file/static) bodies must not be
            # read here — doing so would consume the iterator / break streaming.
            if response.direct_passthrough or response.is_streamed:
                return response
            if response.status_code < 200 or response.status_code >= 300:
                return response
            if "Content-Encoding" in response.headers:
                return response
            if not _is_compressible(response.content_type):
                return response

            data = response.get_data()
            if data is None or len(data) < _MIN_SIZE_BYTES:
                return response

            compressed = gzip.compress(data, compresslevel=6)
            # Guard against the rare case where gzip didn't actually help.
            if len(compressed) >= len(data):
                return response

            response.set_data(compressed)
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
            response.headers.add("Vary", "Accept-Encoding")
        except Exception as exc:  # never let compression break a response
            logging.debug("Response compression skipped: %s", exc)
        return response

    return app
