"""Liveness and readiness probes for the Futurematch/aileadz Flask app.

Exposes two endpoints via the ``health_bp`` blueprint:

* ``GET /healthz`` — liveness. Returns ``{"status": "ok"}`` with HTTP 200.
  Never touches the database or any external dependency, so it stays cheap and
  always answers as long as the process can serve a request.
* ``GET /readyz`` — readiness. Reports whether the core dependencies the app
  needs to actually serve traffic are available (database, RAG catalog index,
  OpenAI configuration) and returns HTTP 200 when the DB is reachable, else 503.

Everything is wrapped defensively: a probe should never raise, so an unexpected
failure is reported as "not ready" rather than crashing the request.
"""

import logging
import os

from flask import Blueprint, current_app, jsonify

health_bp = Blueprint('health', __name__)

# RAG catalog index files (relative to this module, inside the app1 package).
# Either the augmented file or the raw all-pages export counts as "present".
_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG_FILES = (
    os.path.join(_HERE, 'app1', 'shopify_products_augmented.json'),
    os.path.join(_HERE, 'app1', 'shopify_products_all_pages.json'),
)

# OpenAI configuration is a static boolean for the lifetime of the process: it
# reflects whether OPENAI_API_KEY is set in the environment. We cache it at import
# so the readiness probe never has to re-read the environment (and never calls the
# OpenAI API).
_OPENAI_CONFIGURED = bool(os.environ.get('OPENAI_API_KEY'))


def _check_db():
    """Return True if a trivial query succeeds against the app DB, else False."""
    try:
        conn = current_app.mysql.connection
        cur = conn.cursor()
        try:
            cur.execute('SELECT 1')
            cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        return True
    except Exception as exc:
        logging.warning("Readiness DB check failed: %s", exc)
        return False


def _check_catalog():
    """Return True if at least one RAG catalog index file exists on disk."""
    try:
        return any(os.path.exists(path) for path in _CATALOG_FILES)
    except Exception as exc:
        logging.warning("Readiness catalog check failed: %s", exc)
        return False


@health_bp.route('/healthz')
def healthz():
    """Liveness probe — cheap, never touches the DB."""
    return jsonify({'status': 'ok'}), 200


@health_bp.route('/readyz')
def readyz():
    """Readiness probe — reports DB / catalog / OpenAI status."""
    try:
        db_ok = _check_db()
        catalog_ok = _check_catalog()
        openai_ok = _OPENAI_CONFIGURED
        status = 'ready' if db_ok else 'degraded'
        body = {
            'db': db_ok,
            'catalog': catalog_ok,
            'openai': openai_ok,
            'status': status,
        }
        return jsonify(body), (200 if db_ok else 503)
    except Exception as exc:
        # A probe must never raise — degrade gracefully.
        logging.warning("Readiness probe error: %s", exc)
        return jsonify({
            'db': False,
            'catalog': False,
            'openai': _OPENAI_CONFIGURED,
            'status': 'degraded',
        }), 503
