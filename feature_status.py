"""Observability for optional / degradable subsystems (Futurematch/aileadz).

Many subsystems in this app are *additive* and *boot-safe*: they import their
heavy or security-sensitive dependencies behind ``try/except`` and degrade with
a warning when something is missing (no scikit-learn -> no ML analytics, no
defusedxml -> SAML fails closed, no bleach -> HTML is fully escaped, etc.).

That defensive design keeps ``create_app()`` alive, but it also makes the
degradation *silent* — an operator has no easy way to see which features are
actually running. This module closes that gap. :func:`check_feature_status`
probes each optional subsystem with the same guarded imports / file checks the
real code uses and returns a flat, JSON-serialisable registry:

    {
        "vector_search": {"available": True,  "detail": "..."},
        "sso":           {"available": False, "detail": "..."},
        ...
    }

The result is surfaced by the ``/readyz`` health probe (see ``health.py``).

Design rules (this module is imported at request time, never at boot, but is
written to the same standard as the rest of the codebase):

* **Never raise.** Every probe is wrapped; an unexpected error is reported as
  ``available: False`` with the exception text in ``detail``, not propagated.
* **Compute once.** The full registry is cached in a module global the first
  time it is requested. Availability of an installed dependency / on-disk file
  does not change over the lifetime of a process, so re-probing is wasteful.
* **No side effects.** We only attempt imports and ``os.path.exists`` checks;
  we never open network connections (e.g. we do not actually connect to Redis).
"""

import importlib
import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))

# The augmented RAG catalog that the sklearn-backed vector search builds its
# index from (see app1/rag.py and catalog_service.py). Its presence is what
# distinguishes a usable vector-search subsystem from a cold one.
_AUGMENTED_CATALOG = os.path.join(_HERE, 'app1', 'shopify_products_augmented.json')

# Module-global cache. ``None`` means "not computed yet". Once populated it is a
# dict and is returned as-is on every subsequent call.
_CACHE = None


# ---------------------------------------------------------------------------
# Low-level helpers (each returns a (bool, detail) tuple and never raises)
# ---------------------------------------------------------------------------

def _try_import(module_name):
    """Attempt to import ``module_name``.

    Returns ``(True, "")`` on success, otherwise ``(False, <reason>)``. Never
    raises — any import-time error (ImportError, but also e.g. a broken native
    extension raising something else) is captured as a not-available reason.
    """
    try:
        importlib.import_module(module_name)
        return True, ''
    except Exception as exc:  # noqa: BLE001 - intentionally broad; must not raise
        return False, '{}: {}'.format(type(exc).__name__, exc)


def _all_importable(module_names):
    """Return (all_present, detail) for a list of modules.

    ``detail`` lists which modules are present and which are missing so an
    operator can see exactly what to install.
    """
    present = []
    missing = []
    for name in module_names:
        ok, reason = _try_import(name)
        if ok:
            present.append(name)
        else:
            missing.append('{} ({})'.format(name, reason) if reason else name)
    all_present = not missing
    if all_present:
        detail = 'available: ' + ', '.join(present)
    else:
        parts = []
        if present:
            parts.append('present: ' + ', '.join(present))
        parts.append('missing: ' + ', '.join(missing))
        detail = '; '.join(parts)
    return all_present, detail


def _file_present(path):
    """Return (exists, detail) for a filesystem path, never raising."""
    try:
        if os.path.exists(path):
            return True, 'found {}'.format(path)
        return False, 'missing {}'.format(path)
    except Exception as exc:  # noqa: BLE001 - never raise
        return False, 'check failed for {}: {}'.format(path, exc)


def _any_env(var_names):
    """Return (any_set, list_of_set_names) for a list of env var names."""
    set_names = [name for name in var_names if os.environ.get(name)]
    return bool(set_names), set_names


# ---------------------------------------------------------------------------
# Per-subsystem probes. Each returns {"available": bool, "detail": str}.
# Each is fully wrapped so a probe bug can never break the registry.
# ---------------------------------------------------------------------------

def _probe_vector_search():
    """Semantic catalog search: needs scikit-learn AND the augmented catalog."""
    try:
        sklearn_ok, sklearn_detail = _all_importable(['sklearn', 'numpy'])
        catalog_ok, catalog_detail = _file_present(_AUGMENTED_CATALOG)
        available = sklearn_ok and catalog_ok
        if available:
            detail = 'scikit-learn present and augmented catalog found'
        else:
            reasons = []
            if not sklearn_ok:
                reasons.append('deps: ' + sklearn_detail)
            if not catalog_ok:
                reasons.append('catalog: ' + catalog_detail)
            detail = '; '.join(reasons)
        return {'available': available, 'detail': detail}
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'detail': 'probe error: {}'.format(exc)}


def _probe_sso():
    """Enterprise SSO: PyJWT (module ``jwt``), ldap3 and cryptography/Fernet."""
    try:
        # ``import jwt`` is top-level in enterprise_sso/enterprise_api, so PyJWT
        # is effectively boot-critical; ldap3 + cryptography are needed for the
        # full SAML/LDAP/secret-encryption feature set.
        available, detail = _all_importable(['jwt', 'ldap3', 'cryptography'])
        return {'available': available, 'detail': detail}
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'detail': 'probe error: {}'.format(exc)}


def _probe_analytics_ml():
    """ML analytics (RandomForest / IsolationForest / KMeans): scikit-learn."""
    try:
        available, detail = _all_importable(['sklearn'])
        return {'available': available, 'detail': detail}
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'detail': 'probe error: {}'.format(exc)}


def _probe_rate_limit():
    """Distributed rate limiting via Redis (optional; falls back to in-proc)."""
    try:
        ok, reason = _try_import('redis')
        if ok:
            return {'available': True, 'detail': 'redis client importable (optional backend)'}
        return {
            'available': False,
            'detail': 'redis not installed ({}); using in-process fallback'.format(reason),
        }
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'detail': 'probe error: {}'.format(exc)}


def _probe_email():
    """Outbound email. smtplib is stdlib (always present); ESP config is extra."""
    try:
        smtp_ok, smtp_reason = _try_import('smtplib')
        esp_set, esp_names = _any_env([
            'SMTP_HOST', 'SMTP_SERVER', 'MAIL_SERVER',
            'SENDGRID_API_KEY', 'MAILGUN_API_KEY', 'POSTMARK_API_TOKEN',
            'SES_REGION', 'AWS_SES_REGION', 'EMAIL_API_KEY',
        ])
        # smtplib alone is enough to *attempt* delivery, so the subsystem is
        # considered available whenever the stdlib module imports.
        if esp_set:
            detail = 'smtplib available; ESP config detected: ' + ', '.join(esp_names)
        elif smtp_ok:
            detail = 'smtplib available; no ESP env configured (using SMTP/defaults)'
        else:
            detail = 'smtplib unavailable: ' + smtp_reason
        return {'available': bool(smtp_ok), 'detail': detail}
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'detail': 'probe error: {}'.format(exc)}


def _probe_xml_safe():
    """XXE-safe XML parsing for attacker-controlled SAML payloads: defusedxml."""
    try:
        ok, reason = _try_import('defusedxml')
        if ok:
            return {'available': True, 'detail': 'defusedxml present (SAML XXE-safe)'}
        return {
            'available': False,
            'detail': 'defusedxml missing ({}); SAML parsing FAILS CLOSED'.format(reason),
        }
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'detail': 'probe error: {}'.format(exc)}


def _probe_html_sanitize():
    """HTML sanitisation of user input: bleach (falls back to full escaping)."""
    try:
        ok, reason = _try_import('bleach')
        if ok:
            return {'available': True, 'detail': 'bleach present (rich sanitisation)'}
        return {
            'available': False,
            'detail': 'bleach missing ({}); input is fully HTML-escaped instead'.format(reason),
        }
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'detail': 'probe error: {}'.format(exc)}


# Registry of subsystem name -> probe callable. Ordering is preserved in output.
_PROBES = (
    ('vector_search', _probe_vector_search),
    ('sso', _probe_sso),
    ('analytics_ml', _probe_analytics_ml),
    ('rate_limit', _probe_rate_limit),
    ('email', _probe_email),
    ('xml_safe', _probe_xml_safe),
    ('html_sanitize', _probe_html_sanitize),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_feature_status(force_refresh=False):
    """Return the cached subsystem availability registry.

    The registry maps each subsystem name to ``{"available": bool,
    "detail": str}``. It is computed once and cached in a module global; pass
    ``force_refresh=True`` to recompute (used only by tests / manual ops).

    This function never raises: a failure inside any individual probe is
    captured as that subsystem being unavailable, and a catastrophic failure of
    the whole computation falls back to an empty dict.
    """
    global _CACHE
    if _CACHE is not None and not force_refresh:
        return _CACHE

    result = {}
    for name, probe in _PROBES:
        try:
            entry = probe()
            # Normalise defensively so callers can always rely on the shape.
            if not isinstance(entry, dict):
                entry = {'available': False, 'detail': 'invalid probe result'}
            entry.setdefault('available', False)
            entry.setdefault('detail', '')
            result[name] = {
                'available': bool(entry['available']),
                'detail': str(entry['detail']),
            }
        except Exception as exc:  # noqa: BLE001 - one bad probe must not break the rest
            try:
                logger.warning("feature_status probe '%s' failed: %s", name, exc)
            except Exception:
                pass
            result[name] = {'available': False, 'detail': 'probe error: {}'.format(exc)}

    _CACHE = result
    return _CACHE
