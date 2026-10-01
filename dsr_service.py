"""
Data-subject requests (DSR) with an SLA (S-4.2).

A learner who wants their data erased used to have to e-mail support. Now they
press "Anmod om sletning" (settings / privacy page), which opens a ticket here:

  * ``due_at`` = requested_at + 30 days (GDPR art. 12(3): one month);
  * one open ticket per person and type (pressing the button twice is harmless);
  * platform admins see the queue, with overdue tickets flagged, on /admin/gdpr;
  * carrying out the erasure from that console closes the ticket;
  * once erased, the ticket itself is anonymised (``gdpr_service`` EXTRA_SPECS),
    keeping the proof that the request was handled without the person's data.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

SLA_DAYS = 30
REQUEST_TYPES = ("erasure", "export")
OPEN_STATUSES = ("open", "in_progress")

_DDL = """CREATE TABLE IF NOT EXISTS dsr_requests (
    id INT AUTO_INCREMENT PRIMARY KEY,
    request_type VARCHAR(20) NOT NULL,
    user_id INT NULL,
    username VARCHAR(255) NULL,
    email VARCHAR(255) NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    reason TEXT NULL,
    requested_at DATETIME NOT NULL,
    due_at DATETIME NOT NULL,
    handled_by VARCHAR(255) NULL,
    handled_at DATETIME NULL,
    note TEXT NULL,
    INDEX idx_status_due (status, due_at),
    INDEX idx_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
_TABLE_READY = False


def ensure_table(conn):
    global _TABLE_READY
    if _TABLE_READY:
        return
    cur = conn.cursor()
    try:
        cur.execute(_DDL)
        conn.commit()
        _TABLE_READY = True
    finally:
        cur.close()


def _rows(cur):
    rows = cur.fetchall() or []
    return [r if isinstance(r, dict) else r for r in rows]


def create_request(conn, *, user_id, username, email, request_type="erasure", reason=None, now=None):
    """Open a ticket, or return the existing open one. Returns ``(row, created)``."""
    if request_type not in REQUEST_TYPES:
        raise ValueError("bad request type")
    ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM dsr_requests WHERE request_type = %s AND user_id <=> %s AND status IN ('open','in_progress') "
            "ORDER BY id DESC LIMIT 1", (request_type, user_id))
        existing = cur.fetchone()
        if existing:
            return existing, False
        now = now or datetime.now()
        cur.execute(
            "INSERT INTO dsr_requests (request_type, user_id, username, email, status, reason, requested_at, due_at) "
            "VALUES (%s, %s, %s, %s, 'open', %s, %s, DATE_ADD(%s, INTERVAL %s DAY))",
            (request_type, user_id, username, email, (reason or "")[:1000] or None, now, now, SLA_DAYS))
        new_id = cur.lastrowid
        conn.commit()
        cur.execute("SELECT * FROM dsr_requests WHERE id = %s", (new_id,))
        return cur.fetchone(), True
    finally:
        cur.close()


def list_requests(conn, *, include_closed=False, limit=200):
    ensure_table(conn)
    cur = conn.cursor()
    try:
        where = "" if include_closed else "WHERE status IN ('open','in_progress')"
        cur.execute("SELECT * FROM dsr_requests %s ORDER BY due_at ASC, id ASC LIMIT %%s" % where, (int(limit),))
        return _rows(cur)
    finally:
        cur.close()


def is_overdue(row, now=None):
    due = row.get("due_at")
    if not due or (row.get("status") or "") not in OPEN_STATUSES:
        return False
    now = now or datetime.now()
    try:
        return due < now
    except TypeError:
        return False


def set_status(conn, request_id, status, actor, note=None):
    if status not in ("in_progress", "completed", "rejected"):
        raise ValueError("bad status")
    ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE dsr_requests SET status = %s, handled_by = %s, handled_at = NOW(), note = %s "
            "WHERE id = %s AND status IN ('open','in_progress')",
            (status, actor, (note or "")[:1000] or None, int(request_id)))
        ok = cur.rowcount == 1
        conn.commit()
        return ok
    finally:
        cur.close()


def open_erasure_ticket_ids(conn, *, user_id=None, username=None):
    """Ids of the subject's open erasure tickets. Call BEFORE the erasure: it
    anonymises the ticket rows (user_id / username / e-mail are nulled), so they
    can only be found by id afterwards -- then close them with ``set_status``."""
    ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM dsr_requests WHERE request_type='erasure' AND status IN ('open','in_progress') "
            "AND (user_id <=> %s OR (username IS NOT NULL AND username = %s))", (user_id, username))
        return [r["id"] if isinstance(r, dict) else r[0] for r in (cur.fetchall() or [])]
    finally:
        cur.close()
