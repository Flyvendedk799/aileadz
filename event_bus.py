# event_bus.py
"""Integration event bus + transactional outbox.

Problem this solves (roadmap value-5)
-------------------------------------
Today integration webhooks fire on roughly 1 of 5 order paths, *synchronously*,
inside the request that created the order. That means:

  * 4 of 5 order paths never notify external systems at all (events are lost),
  * the one path that does fire blocks the user's request on an outbound HTTP
    call (and on DNS resolution for the SSRF guard),
  * a slow/broken subscriber slows down or fails the order request itself.

This module introduces a durable **outbox**: every integration event is written
to ``event_outbox`` as a single small ``INSERT`` (fast, transactional, never
blocks on the network). A separate *drain* step later reads pending rows and
delivers them to the company's ``company_webhooks`` subscriptions, using the
EXISTING SSRF-guarded delivery path. Delivery becomes at-least-once with retry
instead of fire-and-forget.

Design constraints (host has NO cron/Redis/Celery yet — PythonAnywhere)
-----------------------------------------------------------------------
``drain_outbox()`` is callable from two places:

  1. a token-protected HTTP endpoint (``POST /api/v1/_internal/drain-outbox``)
     that a PythonAnywhere scheduled task hits for reliable delivery
     (OPS-GATED — see enterprise_api), and
  2. a best-effort opportunistic drain that runs a tiny, time-boxed batch on a
     fraction of API requests so events still flow even with no job runner.

Everything here is GUARDED: ``emit_event`` and ``drain_outbox`` never raise into
their callers. The worst case is "the event stays pending and is retried later",
never "the order request fails".

Conventions (matching the rest of the codebase)
-----------------------------------------------
  * ``current_app.mysql.connection`` for DB access, ``DictCursor`` reads.
  * ``autocommit=False`` -> we ``commit()`` explicitly.
  * Schema is idempotent ``CREATE TABLE IF NOT EXISTS``.
  * Every query is scoped by ``company_id`` (no FKs in this codebase).
"""

import json
import logging
import time

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────

# Max delivery attempts before a row is permanently marked 'failed'.
MAX_DELIVERY_ATTEMPTS = 5
# Per-delivery HTTP timeout (seconds). Kept short so the drain stays bounded.
DELIVERY_TIMEOUT_SECONDS = 5
# Default batch size for a single drain pass.
DEFAULT_DRAIN_LIMIT = 50

# The single canonical outbox DDL. enterprise_tables.ensure_enterprise_tables
# also creates this (it owns the authoritative table list); event_bus keeps an
# identical copy so it can lazily self-heal if called before that runs.
EVENT_OUTBOX_DDL = """CREATE TABLE IF NOT EXISTS event_outbox (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL,
    event_type VARCHAR(120) NOT NULL,
    payload TEXT,
    status ENUM('pending', 'delivered', 'failed') NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered_at DATETIME NULL,
    INDEX idx_status (status),
    INDEX idx_company (company_id),
    INDEX idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def _get_conn():
    """Return the request-scoped MySQL connection, or None if unavailable.

    Guarded: importing flask / touching current_app must never raise out of the
    helpers in this module.
    """
    try:
        from flask import current_app
        return current_app.mysql.connection
    except Exception:
        return None


def ensure_outbox_table(conn=None):
    """Lazily ensure the event_outbox table exists (idempotent).

    enterprise_tables owns the authoritative table list; this is a self-heal so
    event_bus works even if emit/drain runs before ensure_enterprise_tables.
    Returns True on success, False otherwise. Never raises.
    """
    try:
        conn = conn or _get_conn()
        if conn is None:
            return False
        cur = conn.cursor()
        try:
            cur.execute(EVENT_OUTBOX_DDL)
            conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        return True
    except Exception as e:
        logger.warning("event_bus: ensure_outbox_table failed: %s", e)
        return False


def emit_event(company_id, event_type, payload):
    """Record ONE integration event into the outbox (the single emit point).

    Writes a single pending row and commits. This is intentionally cheap and
    network-free so it can be called from inside order/employee request paths
    without blocking them. Guarded: never raises into the caller.

    Args:
        company_id: tenant id (every row is company-scoped).
        event_type: e.g. 'order.created', 'employee.added'.
        payload: JSON-serialisable dict (or anything json.dumps can handle).

    Returns:
        The new outbox row id on success, or None on failure.
    """
    if not company_id or not event_type:
        return None

    conn = _get_conn()
    if conn is None:
        return None

    try:
        try:
            body = json.dumps(payload, default=str)
        except Exception:
            # Last-resort: still record the event with a stringified payload so
            # it is never silently dropped.
            body = json.dumps({'_unserializable': str(payload)})

        cur = conn.cursor()
        try:
            try:
                cur.execute(
                    """INSERT INTO event_outbox (company_id, event_type, payload, status, attempts)
                       VALUES (%s, %s, %s, 'pending', 0)""",
                    (company_id, str(event_type)[:120], body),
                )
            except Exception as first_err:
                # Table may not exist yet on a fresh DB — create then retry once.
                logger.info("event_bus: emit retry after ensuring table (%s)", first_err)
                if not ensure_outbox_table(conn):
                    raise
                cur.execute(
                    """INSERT INTO event_outbox (company_id, event_type, payload, status, attempts)
                       VALUES (%s, %s, %s, 'pending', 0)""",
                    (company_id, str(event_type)[:120], body),
                )
            new_id = cur.lastrowid
            conn.commit()
            return new_id
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(
            "event_bus: emit_event failed for company %s event %s: %s",
            company_id, event_type, e,
        )
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def _safe_webhook_url(url):
    """Reuse enterprise_api's SSRF guard. Imported lazily to avoid a circular
    import (enterprise_api imports event_bus for the drain endpoint). Fails
    CLOSED (returns False) if the guard cannot be imported."""
    try:
        from enterprise_api import _is_safe_webhook_url
        return bool(_is_safe_webhook_url(url))
    except Exception as e:
        logger.warning("event_bus: SSRF guard unavailable, rejecting url: %s", e)
        return False


def _parse_events(raw):
    """Normalise a company_webhooks.events value to a list of event names."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, str):
                return [parsed]
        except Exception:
            # Tolerate a bare/comma-separated string.
            return [p.strip() for p in raw.split(',') if p.strip()]
    return []


def _deliver_to_subscribers(conn, row, company_slug):
    """Deliver one outbox row to all matching subscriptions for its company.

    Returns (delivered: bool, error: str|None). A row counts as delivered if
    every matching active subscription accepted it (or there were no matching
    subscriptions — there is nothing to deliver, so it is considered done and
    will not be retried forever). Network errors / SSRF rejections -> failure.
    """
    company_id = row['company_id']
    event_type = row['event_type']

    cur = conn.cursor()
    try:
        import MySQLdb.cursors
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
    except Exception:
        # Fall back to default cursor type if MySQLdb cursors unavailable.
        pass

    try:
        cur.execute(
            """SELECT id, url, secret, events FROM company_webhooks
               WHERE company_id = %s AND is_active = 1""",
            (company_id,),
        )
        webhooks = cur.fetchall() or []
    except Exception as e:
        try:
            cur.close()
        except Exception:
            pass
        return False, "subscription lookup failed: %s" % e

    # Decode the payload once for the delivery body.
    try:
        data = json.loads(row.get('payload') or 'null')
    except Exception:
        data = row.get('payload')

    matched = 0
    errors = []
    import hashlib
    import hmac
    import urllib.request
    from datetime import datetime

    for wh in webhooks:
        # company_webhooks may come back as a dict (DictCursor) or tuple.
        if isinstance(wh, dict):
            wh_id = wh.get('id')
            url = wh.get('url')
            secret = wh.get('secret') or ''
            events = _parse_events(wh.get('events'))
        else:
            wh_id, url, secret, raw_events = wh
            secret = secret or ''
            events = _parse_events(raw_events)

        if event_type not in events and '*' not in events:
            continue
        matched += 1

        # SSRF guard: never fetch a tenant-supplied URL that is not a public
        # http/https endpoint. A rejected URL is a delivery failure.
        if not _safe_webhook_url(url):
            errors.append("webhook %s: blocked unsafe url" % wh_id)
            _bump_webhook_stats(conn, wh_id, ok=False)
            continue

        try:
            body = json.dumps({
                'event': event_type,
                'data': data,
                'timestamp': datetime.now().isoformat(),
                'company_slug': company_slug,
            }).encode()
            sig = hmac.new(str(secret).encode(), body, hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                url, data=body,
                headers={
                    'Content-Type': 'application/json',
                    'X-Webhook-Signature': sig,
                    'X-Company-Slug': company_slug or '',
                    'X-Event-Type': event_type,
                },
            )
            urllib.request.urlopen(req, timeout=DELIVERY_TIMEOUT_SECONDS)
            _bump_webhook_stats(conn, wh_id, ok=True)
        except Exception as e:
            errors.append("webhook %s: %s" % (wh_id, e))
            _bump_webhook_stats(conn, wh_id, ok=False)

    try:
        cur.close()
    except Exception:
        pass

    if errors:
        # Some subscriber failed -> the row is not fully delivered; retry later.
        return False, "; ".join(errors)[:2000]
    # All matched subscribers accepted, OR there were none to deliver to.
    return True, None


def _bump_webhook_stats(conn, wh_id, ok):
    """Best-effort per-subscription delivery counters (mirrors existing code)."""
    if not wh_id:
        return
    try:
        cur = conn.cursor()
        if ok:
            cur.execute(
                "UPDATE company_webhooks SET total_deliveries=total_deliveries+1, "
                "successful_deliveries=successful_deliveries+1, last_delivery_at=NOW() WHERE id=%s",
                (wh_id,),
            )
        else:
            cur.execute(
                "UPDATE company_webhooks SET total_deliveries=total_deliveries+1, "
                "failed_deliveries=failed_deliveries+1, last_delivery_at=NOW() WHERE id=%s",
                (wh_id,),
            )
        cur.close()
    except Exception:
        pass


def drain_outbox(limit=DEFAULT_DRAIN_LIMIT):
    """Deliver up to ``limit`` pending outbox rows to their subscribers.

    For each pending row:
      * deliver to every matching active company_webhooks subscription via the
        EXISTING SSRF-guarded delivery path (short timeout),
      * on success -> status='delivered', delivered_at=NOW(),
      * on failure -> attempts+1, last_error recorded; the row stays 'pending'
        and is retried on a later drain UNTIL attempts >= MAX_DELIVERY_ATTEMPTS,
        at which point it is marked 'failed' (no further retries).

    Guarded: never raises. Returns a counts dict:
        {'scanned', 'delivered', 'failed', 'retry', 'skipped'}
    """
    counts = {'scanned': 0, 'delivered': 0, 'failed': 0, 'retry': 0, 'skipped': 0}

    conn = _get_conn()
    if conn is None:
        return counts

    try:
        limit = int(limit)
    except Exception:
        limit = DEFAULT_DRAIN_LIMIT
    if limit <= 0:
        return counts

    # Make sure the table exists before we try to read it.
    ensure_outbox_table(conn)

    try:
        import MySQLdb.cursors
        read_cur = conn.cursor(MySQLdb.cursors.DictCursor)
    except Exception:
        read_cur = conn.cursor()

    try:
        read_cur.execute(
            """SELECT id, company_id, event_type, payload, attempts
               FROM event_outbox
               WHERE status = 'pending'
               ORDER BY id ASC
               LIMIT %s""",
            (limit,),
        )
        rows = read_cur.fetchall() or []
    except Exception as e:
        logger.warning("event_bus: drain read failed: %s", e)
        try:
            read_cur.close()
        except Exception:
            pass
        return counts
    finally:
        try:
            read_cur.close()
        except Exception:
            pass

    if not rows:
        return counts

    # Cache company_slug per company_id to avoid repeated lookups.
    slug_cache = {}

    def _slug_for(cid):
        if cid in slug_cache:
            return slug_cache[cid]
        slug = None
        try:
            c = conn.cursor()
            c.execute("SELECT company_slug FROM companies WHERE id = %s", (cid,))
            r = c.fetchone()
            if r:
                slug = r[0] if not isinstance(r, dict) else r.get('company_slug')
            c.close()
        except Exception:
            slug = None
        slug_cache[cid] = slug
        return slug

    for raw in rows:
        # Normalise row to a dict regardless of cursor type.
        if isinstance(raw, dict):
            row = raw
        else:
            row = {
                'id': raw[0], 'company_id': raw[1], 'event_type': raw[2],
                'payload': raw[3], 'attempts': raw[4],
            }
        counts['scanned'] += 1
        row_id = row['id']
        attempts = (row.get('attempts') or 0) + 1

        try:
            delivered, error = _deliver_to_subscribers(conn, row, _slug_for(row['company_id']))
        except Exception as e:
            delivered, error = False, "delivery crashed: %s" % e

        try:
            upd = conn.cursor()
            if delivered:
                upd.execute(
                    """UPDATE event_outbox
                       SET status='delivered', attempts=%s, delivered_at=NOW(), last_error=NULL
                       WHERE id=%s""",
                    (attempts, row_id),
                )
                counts['delivered'] += 1
            elif attempts >= MAX_DELIVERY_ATTEMPTS:
                upd.execute(
                    """UPDATE event_outbox
                       SET status='failed', attempts=%s, last_error=%s
                       WHERE id=%s""",
                    (attempts, (error or 'delivery failed')[:2000], row_id),
                )
                counts['failed'] += 1
            else:
                # Leave 'pending' for retry on a later drain.
                upd.execute(
                    """UPDATE event_outbox
                       SET attempts=%s, last_error=%s
                       WHERE id=%s""",
                    (attempts, (error or 'delivery failed')[:2000], row_id),
                )
                counts['retry'] += 1
            upd.close()
            conn.commit()
        except Exception as e:
            logger.warning("event_bus: drain status update failed for row %s: %s", row_id, e)
            counts['skipped'] += 1
            try:
                conn.rollback()
            except Exception:
                pass

    return counts


def opportunistic_drain(limit=5, max_seconds=2.0):
    """Best-effort, time-boxed drain for hosts with no job runner.

    Intended to be called from a request hook on a small fraction of requests so
    events still flow without a scheduled task. Bounded by ``limit`` rows and a
    soft ``max_seconds`` budget; fully guarded so it never slows a request
    materially and never raises. Returns the same counts dict as drain_outbox.

    LIMITATION: this is opportunistic, not a substitute for the scheduled drain.
    If the app gets no traffic, nothing drains. Reliable delivery requires the
    OPS-GATED scheduled task hitting /api/v1/_internal/drain-outbox.
    """
    started = time.time()
    try:
        # Cap the batch small; the per-row delivery already has its own timeout,
        # so the worst case is roughly limit * DELIVERY_TIMEOUT_SECONDS. We keep
        # limit tiny to stay well-bounded.
        counts = drain_outbox(limit=limit)
        elapsed = time.time() - started
        if elapsed > max_seconds:
            logger.info(
                "event_bus: opportunistic drain took %.2fs (budget %.2fs)",
                elapsed, max_seconds,
            )
        return counts
    except Exception as e:
        logger.warning("event_bus: opportunistic drain failed: %s", e)
        return {'scanned': 0, 'delivered': 0, 'failed': 0, 'retry': 0, 'skipped': 0}
