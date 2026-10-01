"""Observability (N-8.2): request ids, structured logs, error tracking, ops alerts.

* Every request gets an id (``X-Request-ID`` from the proxy, else generated). It is
  returned in the response header and stamped on every log line.
* ``LOG_FORMAT=json`` switches the root logger to one JSON object per line
  (request id, path, user, level, message) so the VPS log stream is greppable.
* ``SENTRY_DSN`` turns on Sentry error tracking when ``sentry-sdk`` is installed;
  without the DSN nothing is sent and nothing breaks.
* ``check_ops_alerts`` (run by the worker) raises an alert to platform admins when
  emails or integration events start failing, so a dead SMTP account or a broken
  webhook receiver is noticed in minutes instead of weeks.

Everything is guarded: a failure here must never affect a request.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

EMAIL_ERRORS_THRESHOLD = 3          # failed mails in the window
OUTBOX_FAILED_THRESHOLD = 5         # failed integration events
WINDOW_MINUTES = 60


class RequestIdFilter(logging.Filter):
    """Adds ``request_id``/``path``/``user`` to every record (``-`` outside a request)."""

    def filter(self, record):
        record.request_id = "-"
        record.path = "-"
        record.user = "-"
        try:
            from flask import g, has_request_context, request, session
            if has_request_context():
                record.request_id = getattr(g, "request_id", "-")
                record.path = request.path
                record.user = session.get("user") or "-"
        except Exception:
            pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "path": getattr(record, "path", "-"),
            "user": getattr(record, "user", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging():
    """Attach the request-id filter and (optionally) JSON formatting."""
    root = logging.getLogger()
    flt = RequestIdFilter()
    for handler in root.handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(flt)
        if os.getenv("LOG_FORMAT", "").lower() == "json":
            handler.setFormatter(JsonFormatter())
        elif not isinstance(handler.formatter, JsonFormatter):
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"))


def init_error_tracking(app):
    """Sentry, only when DSN + SDK are present."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0") or 0),
            send_default_pii=False,
            environment=os.getenv("APP_ENV", "production"),
        )
        app.config["ERROR_TRACKING"] = "sentry"
        return True
    except Exception as exc:
        logging.getLogger(__name__).warning("error tracking not started: %s", exc)
        return False


def register_observability(app):
    from flask import g, request

    configure_logging()
    init_error_tracking(app)

    @app.before_request
    def _assign_request_id():
        incoming = (request.headers.get("X-Request-ID") or "").strip()
        g.request_id = incoming[:64] if incoming and incoming.isascii() else uuid.uuid4().hex[:16]

    @app.after_request
    def _echo_request_id(resp):
        try:
            resp.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        except Exception:
            pass
        return resp


# ── ops alerts (worker job) ─────────────────────────────────────────────────

def _scalar(cur, sql, params=()):
    cur.execute(sql, params)
    r = cur.fetchone()
    if r is None:
        return 0
    v = r.get("n") if isinstance(r, dict) else r[0]
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def collect_ops_signals(cur, window_minutes=WINDOW_MINUTES):
    """Counts of failed emails and failed/stuck integration events."""
    signals = {"email_errors": 0, "outbox_failed": 0}
    try:
        signals["email_errors"] = _scalar(
            cur,
            "SELECT COUNT(*) AS n FROM email_log WHERE status = 'error' "
            "AND created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)", (int(window_minutes),))
    except Exception as e:
        logger.debug("ops signals: email_log unavailable (%s)", e)
    try:
        signals["outbox_failed"] = _scalar(
            cur, "SELECT COUNT(*) AS n FROM event_outbox WHERE status = 'failed'")
    except Exception as e:
        logger.debug("ops signals: event_outbox unavailable (%s)", e)
    return signals


def check_ops_alerts(conn, now=None):
    """Notify platform admins when failures cross the thresholds. Returns a summary."""
    from notification_service import notify_user
    summary = {"alerts": 0, "signals": {}}
    cur = conn.cursor()
    try:
        signals = collect_ops_signals(cur)
        summary["signals"] = signals
        problems = []
        if signals["email_errors"] >= EMAIL_ERRORS_THRESHOLD:
            problems.append(("ops-email",
                             "E-mail kan ikke sendes",
                             "%d e-mails fejlede den seneste time. Tjek SMTP-opsætningen under Systemstatus." % signals["email_errors"]))
        if signals["outbox_failed"] >= OUTBOX_FAILED_THRESHOLD:
            problems.append(("ops-outbox",
                             "Integrationer kan ikke leveres",
                             "%d webhook-events er fejlet. Åbn Systemstatus for at se modtagere og gensend." % signals["outbox_failed"]))
        if problems:
            cur.execute("SELECT id, username FROM users WHERE role = 'admin'")
            admins = cur.fetchall() or []
            stamp = time.strftime("%Y%m%d%H", time.gmtime(now or time.time()))
            for key, title, msg in problems:
                logger.error("ops alert: %s - %s", title, msg)
                for a in admins:
                    uid = a.get("id") if isinstance(a, dict) else a[0]
                    uname = a.get("username") if isinstance(a, dict) else a[1]
                    if notify_user(cur, title=title, message=msg, username=uname, user_id=uid,
                                   kind="ops", is_urgent=True, action_url="/admin/system-health",
                                   dedupe_key="%s:%s" % (key, stamp), dedupe_hours=6):
                        summary["alerts"] += 1
            conn.commit()
    except Exception as e:
        logger.warning("ops alert check failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        summary["error"] = str(e)
    finally:
        cur.close()
    return summary
