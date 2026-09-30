"""Credits as AI-usage metering (N-6.4).

* Every AI turn (employee advisor/profiler, HR assistant, vendor assistant,
  embeddable widget) is deducted through the ``credit_usage`` ledger. The size of
  the deduction comes from ``ai_cost_model`` (tokens x model price -> DKK) and
  ``AI_CREDITS_PER_DKK`` (default 10, i.e. 1 credit = 10 oere), minimum 1 credit.
* Credits are held per COMPANY (``company_credit_accounts``). A user without a
  company has a personal balance in ``users.credits``.
* Ledger sign convention (unchanged, reports already rely on it): usage is a
  POSITIVE ``credits_used``, a grant is NEGATIVE. For a company the invariant is
  ``balance == -SUM(credits_used)`` over its ledger rows - ``verify_ledger`` checks it.
* EVERY grant goes through ``grant`` (reason + actor recorded).
* Low balance: HR is notified once when the balance drops to/below the company's
  threshold. At zero the company setting decides: ``soft`` (default: keep working,
  flag it) or ``hard`` (AI paused with a friendly Danish message).
* Purchasing credits is off-platform like the rest of billing: an admin tops up.

Everything here is guarded: a metering failure must never break an AI turn.
"""

from __future__ import annotations

import datetime
import logging
import math
import os

logger = logging.getLogger(__name__)

LIMIT_SOFT = "soft"
LIMIT_HARD = "hard"
DEFAULT_LOW_THRESHOLD = 100
HARD_LIMIT_MESSAGE = (
    "AI-assistenten er sat på pause, fordi virksomhedens kreditter er brugt op. "
    "Bed din HR-ansvarlige om at få tilført flere kreditter, så kan vi fortsætte."
)

_ASSISTANT_BY_SCOPE = {"employee": "advisor", "hr": "hr", "vendor": "vendor", "widget": "widget",
                       "profiler": "profiler"}


def credits_per_dkk() -> float:
    try:
        return max(0.01, float(os.environ.get("AI_CREDITS_PER_DKK", "10")))
    except ValueError:
        return 10.0


def turn_credits(model, input_tokens, output_tokens, cached_tokens=0) -> int:
    """Credits for one AI turn (>= 1). Unknown models are billed the minimum."""
    try:
        import ai_cost_model
        dkk = float(ai_cost_model.estimate_cost(model, input_tokens, output_tokens, cached_tokens).get("dkk") or 0.0)
    except Exception:
        dkk = 0.0
    return max(1, int(math.ceil(dkk * credits_per_dkk())))


def _val(row, key, idx=0, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        v = row.get(key)
    else:
        try:
            v = row[idx]
        except Exception:
            v = None
    return default if v is None else v


def _cursor(conn):
    try:
        import MySQLdb.cursors
        return conn.cursor(MySQLdb.cursors.DictCursor)
    except Exception:
        return conn.cursor()


def _ensure_account(cur, company_id):
    cur.execute("INSERT IGNORE INTO company_credit_accounts (company_id, balance, low_threshold, limit_mode) "
                "VALUES (%s, 0, %s, 'soft')", (company_id, DEFAULT_LOW_THRESHOLD))


def get_account(cur, company_id):
    """{'balance', 'low_threshold', 'limit_mode'} for a company (defaults if new)."""
    cur.execute("SELECT balance, low_threshold, limit_mode FROM company_credit_accounts WHERE company_id = %s",
                (company_id,))
    r = cur.fetchone()
    if not r:
        return {"balance": 0, "low_threshold": DEFAULT_LOW_THRESHOLD, "limit_mode": LIMIT_SOFT}
    return {"balance": int(_val(r, "balance", 0, 0)),
            "low_threshold": int(_val(r, "low_threshold", 1, DEFAULT_LOW_THRESHOLD)),
            "limit_mode": _val(r, "limit_mode", 2, LIMIT_SOFT) or LIMIT_SOFT}


def get_personal_balance(cur, username):
    cur.execute("SELECT credits FROM users WHERE username = %s", (username,))
    return int(_val(cur.fetchone(), "credits", 0, 0))


def balance_for(cur, company_id=None, username=None):
    """(scope, balance). scope is 'company' or 'personal'."""
    if company_id:
        return "company", get_account(cur, company_id)["balance"]
    if username:
        return "personal", get_personal_balance(cur, username)
    return "personal", 0


# ── guard (hard limit) ──────────────────────────────────────────────────────

def guard(mysql=None, company_id=None, username=None):
    """None when the turn may run, else a friendly Danish message. Fails open."""
    if not company_id:
        return None
    try:
        from flask import current_app
        mysql = mysql or current_app.mysql
        cur = _cursor(mysql.connection)
        try:
            acct = get_account(cur, company_id)
        finally:
            cur.close()
        if acct["limit_mode"] == LIMIT_HARD and acct["balance"] <= 0:
            return HARD_LIMIT_MESSAGE
    except Exception as e:
        logger.debug("credit guard failed open: %s", e)
    return None


# ── charging ────────────────────────────────────────────────────────────────

def charge_turn(conn, *, username, user_id=None, company_id=None, assistant="advisor", model=None,
                tokens_in=0, tokens_out=0, cached_tokens=0, description=None, billable=True):
    """Deduct one AI turn. Returns the credits charged (0 when not billable).

    Always writes a ledger row (so usage is visible for vendors/admin too); only
    billable turns move a balance.
    """
    n = turn_credits(model, tokens_in, tokens_out, cached_tokens)
    cur = _cursor(conn)
    try:
        cur.execute(
            """INSERT INTO credit_usage (username, user_id, company_id, credits_used, description, kind,
                                         assistant, model, tokens_in, tokens_out)
               VALUES (%s, %s, %s, %s, %s, 'usage', %s, %s, %s, %s)""",
            (username, user_id, company_id, n if billable else 0,
             description or ("AI-svar (%s)" % assistant), assistant, (model or "")[:80],
             int(tokens_in or 0), int(tokens_out or 0)))
        low_crossed = False
        if billable and company_id:
            _ensure_account(cur, company_id)
            before = get_account(cur, company_id)
            cur.execute("UPDATE company_credit_accounts SET balance = balance - %s WHERE company_id = %s",
                        (n, company_id))
            after_balance = before["balance"] - n
            thr = before["low_threshold"]
            low_crossed = before["balance"] > thr >= after_balance or (before["balance"] > 0 >= after_balance)
            if low_crossed:
                _notify_low_balance(cur, company_id, after_balance, thr, before["limit_mode"])
        elif billable and username:
            cur.execute("UPDATE users SET credits = credits - %s WHERE username = %s", (n, username))
        conn.commit()
        return n if billable else 0
    except Exception as e:
        logger.warning("credit charge failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _notify_low_balance(cur, company_id, balance, threshold, mode):
    try:
        from notification_service import notify_roles, HR_ROLES
        empty = balance <= 0
        title = "AI-kreditterne er brugt op" if empty else "AI-kreditterne er ved at løbe tør"
        if empty:
            msg = ("Virksomhedens AI-kreditter er nul. "
                   + ("AI-assistenterne er sat på pause, til der er tilført flere."
                      if mode == LIMIT_HARD else "Assistenterne kører videre, men saldoen er negativ."))
        else:
            msg = "Der er %d kreditter tilbage (advarsel ved %d). Bed en administrator tilføje flere." % (balance, threshold)
        notify_roles(cur, company_id, HR_ROLES, title=title, message=msg, kind="credits",
                     is_urgent=empty, action_url="/hr/credits",
                     dedupe_key="credits-low:%s:%s" % (company_id, "zero" if empty else "low"), dedupe_hours=24)
    except Exception as e:
        logger.debug("low-balance notification skipped: %s", e)


def charge_from_usage(mysql, *, username, company_id, agent_scope, model, usage, runtime=""):
    """Hook used by ``ai_runtime.log_agent_run`` for every logged AI turn."""
    if not mysql or not username or (runtime or "").endswith("shadow"):
        return 0
    try:
        usage = usage or {}
        t_in = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        t_out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        cached = int(usage.get("cached_tokens") or 0)
        is_vendor = (agent_scope or "") == "vendor" or str(username).startswith("vendor:")
        user_id = None
        if not is_vendor and not company_id:
            pass
        return charge_turn(mysql.connection, username=username, user_id=user_id, company_id=company_id,
                           assistant=_ASSISTANT_BY_SCOPE.get(agent_scope, agent_scope or "advisor"),
                           model=model, tokens_in=t_in, tokens_out=t_out, cached_tokens=cached,
                           billable=not is_vendor)
    except Exception as e:
        logger.debug("charge_from_usage skipped: %s", e)
        return 0


# ── grants ──────────────────────────────────────────────────────────────────

def grant(conn, *, amount, reason, actor, company_id=None, username=None):
    """Top up a company (or a personal balance) through the ledger."""
    amount = int(amount)
    if amount == 0:
        return {"success": False, "message": "Angiv et antal kreditter."}
    if not (company_id or username):
        return {"success": False, "message": "Vælg en virksomhed eller bruger."}
    reason = (reason or "").strip() or "Tildeling"
    cur = _cursor(conn)
    try:
        desc = "Admin-tildeling af %s: %s" % (actor or "admin", reason)
        cur.execute(
            """INSERT INTO credit_usage (username, company_id, credits_used, description, kind, actor)
               VALUES (%s, %s, %s, %s, 'grant', %s)""",
            (username, company_id, -amount, desc[:255], (actor or "admin")[:255]))
        if company_id:
            _ensure_account(cur, company_id)
            cur.execute("UPDATE company_credit_accounts SET balance = balance + %s, low_notified = 0 "
                        "WHERE company_id = %s", (amount, company_id))
            balance = get_account(cur, company_id)["balance"]
        else:
            cur.execute("UPDATE users SET credits = credits + %s WHERE username = %s", (amount, username))
            balance = get_personal_balance(cur, username)
        conn.commit()
        return {"success": True, "balance": balance}
    except Exception as e:
        logger.warning("credit grant failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "message": "Kunne ikke tildele kreditter."}
    finally:
        cur.close()


def update_settings(conn, company_id, *, limit_mode=None, low_threshold=None):
    cur = _cursor(conn)
    try:
        _ensure_account(cur, company_id)
        if limit_mode in (LIMIT_SOFT, LIMIT_HARD):
            cur.execute("UPDATE company_credit_accounts SET limit_mode = %s WHERE company_id = %s",
                        (limit_mode, company_id))
        if low_threshold is not None:
            cur.execute("UPDATE company_credit_accounts SET low_threshold = %s WHERE company_id = %s",
                        (max(0, int(low_threshold)), company_id))
        conn.commit()
        return get_account(cur, company_id)
    finally:
        cur.close()


def verify_ledger(cur, company_id):
    """(ok, balance, ledger_sum). The stored balance must equal -SUM(ledger)."""
    cur.execute("SELECT COALESCE(SUM(credits_used), 0) AS s FROM credit_usage WHERE company_id = %s", (company_id,))
    s = int(_val(cur.fetchone(), "s", 0, 0))
    bal = get_account(cur, company_id)["balance"]
    return bal == -s, bal, -s


# ── reading ─────────────────────────────────────────────────────────────────

def usage_summary(cur, *, company_id=None, username=None, days=30):
    """Usage for a company (per user + per assistant), or one user, for ``days``."""
    days = max(1, int(days or 30))
    where, params = "kind = 'usage' AND `timestamp` >= DATE_SUB(NOW(), INTERVAL %s DAY)", [days]
    if company_id:
        where += " AND company_id = %s"
        params.append(company_id)
    elif username:
        where += " AND username = %s"
        params.append(username)
    out = {"days": days, "total": 0, "turns": 0, "by_assistant": [], "by_user": [], "daily": []}
    cur.execute("SELECT COALESCE(SUM(credits_used),0) AS c, COUNT(*) AS n FROM credit_usage WHERE " + where, tuple(params))
    r = cur.fetchone()
    out["total"], out["turns"] = int(_val(r, "c", 0, 0)), int(_val(r, "n", 1, 0))
    cur.execute("SELECT COALESCE(assistant,'advisor') AS a, SUM(credits_used) AS c, COUNT(*) AS n FROM credit_usage WHERE "
                + where + " GROUP BY COALESCE(assistant,'advisor') ORDER BY c DESC", tuple(params))
    out["by_assistant"] = [{"assistant": _val(x, "a", 0), "credits": int(_val(x, "c", 1, 0)), "turns": int(_val(x, "n", 2, 0))}
                           for x in cur.fetchall() or []]
    if company_id:
        cur.execute("SELECT username AS u, SUM(credits_used) AS c, COUNT(*) AS n FROM credit_usage WHERE " + where
                    + " GROUP BY username ORDER BY c DESC LIMIT 50", tuple(params))
        out["by_user"] = [{"username": _val(x, "u", 0), "credits": int(_val(x, "c", 1, 0)), "turns": int(_val(x, "n", 2, 0))}
                          for x in cur.fetchall() or []]
    cur.execute("SELECT DATE(`timestamp`) AS d, SUM(credits_used) AS c FROM credit_usage WHERE " + where
                + " GROUP BY DATE(`timestamp`) ORDER BY d", tuple(params))
    out["daily"] = [{"date": str(_val(x, "d", 0)), "credits": int(_val(x, "c", 1, 0))} for x in cur.fetchall() or []]
    if company_id:
        out["account"] = get_account(cur, company_id)
    return out


def admin_overview(cur, days=30):
    """All companies: balance, usage, burn rate (credits/day) and days of runway."""
    days = max(1, int(days or 30))
    cur.execute("SELECT c.id AS id, c.company_name AS name, COALESCE(a.balance, 0) AS balance, "
                "COALESCE(a.limit_mode, 'soft') AS limit_mode, COALESCE(a.low_threshold, %s) AS low_threshold "
                "FROM companies c LEFT JOIN company_credit_accounts a ON a.company_id = c.id ORDER BY c.company_name",
                (DEFAULT_LOW_THRESHOLD,))
    companies = list(cur.fetchall() or [])
    cur.execute("SELECT company_id AS cid, SUM(credits_used) AS c FROM credit_usage WHERE kind = 'usage' AND company_id IS NOT NULL "
                "AND `timestamp` >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY company_id", (days,))
    used = {int(_val(x, "cid", 0)): int(_val(x, "c", 1, 0)) for x in cur.fetchall() or []}
    rows = []
    for c in companies:
        cid = int(_val(c, "id", 0))
        u = used.get(cid, 0)
        burn = round(u / days, 1)
        bal = int(_val(c, "balance", 2, 0))
        rows.append({"id": cid, "name": _val(c, "name", 1), "balance": bal, "used": u, "burn_per_day": burn,
                     "runway_days": (int(bal / burn) if burn > 0 and bal > 0 else None),
                     "limit_mode": _val(c, "limit_mode", 3, LIMIT_SOFT),
                     "low": bal <= int(_val(c, "low_threshold", 4, DEFAULT_LOW_THRESHOLD))})
    cur.execute("SELECT username AS u, company_id AS cid, -credits_used AS amount, description AS d, `timestamp` AS t "
                "FROM credit_usage WHERE kind = 'grant' ORDER BY `timestamp` DESC LIMIT 25")
    grants = list(cur.fetchall() or [])
    return {"days": days, "companies": rows, "recent_grants": grants}


# ── header chip ─────────────────────────────────────────────────────────────

def chip(session=None):
    """{'scope', 'balance', 'label'} for the header chip; cached 60 s per session."""
    try:
        from flask import session as flask_session, current_app
        sess = session if session is not None else flask_session
        if not sess.get("user"):
            return {"scope": "personal", "balance": 0, "label": "0"}
        now = datetime.datetime.now().timestamp()
        cached = sess.get("_credit_chip")
        if cached and now - cached.get("t", 0) < 60:
            return cached["v"]
        cur = _cursor(current_app.mysql.connection)
        try:
            scope, bal = balance_for(cur, sess.get("company_id"), sess.get("user"))
        finally:
            cur.close()
        v = {"scope": scope, "balance": bal, "label": str(bal)}
        try:
            sess["_credit_chip"] = {"t": now, "v": v}
        except Exception:
            pass
        return v
    except Exception:
        try:
            return {"scope": "personal", "balance": int(sess.get("credits", 0) or 0), "label": str(sess.get("credits", 0) or 0)}
        except Exception:
            return {"scope": "personal", "balance": 0, "label": "0"}


def register_jinja(app):
    app.jinja_env.globals["credit_chip"] = chip
