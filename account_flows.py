"""Account lifecycle screens and emails (N-2.1): forgot password, reset, invite.

Nobody should ever handle a plaintext password. HR no longer reads out a new
password; it sends a link. New employees get an invite with a link that lets them
choose their own password.

Tokens come from ``account_tokens`` (hash-only storage, expiry, single use). Part A
owns the token internals (S-2.4); this module only uses
``create_token(kind, subject_id, ttl_minutes) -> raw`` and
``consume_token(kind, raw) -> subject_id | None`` (plus an optional
``peek_token`` to validate a link before the form is shown).

Part A (S-2.4) ships its own token store and basic screens in ``auth`` (routes
``/forgot-password``, ``/reset-password/<t>``, ``/set-password/<t>``). This module is
the Part B layer and adapts to whichever is present:

* ``register_account_flows(app)`` only registers the Danish screens below when the
  ``auth`` blueprint does not already provide a forgot-password flow;
* ``send_reset_link`` / ``send_invite`` delegate to ``auth.send_user_password_link``
  when it exists, so HR and admin buttons always use the same tokens as the screens.

Routes (Danish URLs, all public; registered only without Part A's flow):
    GET/POST /glemt-adgangskode
    GET/POST /nulstil-adgangskode/<token>
    GET/POST /invitation/<token>
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

account_bp = Blueprint("account", __name__)

RESET_TTL_MINUTES = 60
INVITE_TTL_MINUTES = 7 * 24 * 60
MIN_PASSWORD_LENGTH = 10


def password_problem(password: str, *, username: str = "", email: str = ""):
    """Return a Danish message when the password is not acceptable, else None.
    (S-2.2 will own the full policy; this is the floor the UI already promises.)"""
    pw = password or ""
    if len(pw) < MIN_PASSWORD_LENGTH:
        return "Adgangskoden skal være mindst %d tegn." % MIN_PASSWORD_LENGTH
    low = pw.lower()
    if username and low == username.lower():
        return "Adgangskoden må ikke være det samme som dit brugernavn."
    if email and low == email.lower():
        return "Adgangskoden må ikke være din e-mailadresse."
    if re.fullmatch(r"(.)\1+", pw):
        return "Vælg en adgangskode, der ikke kun består af ét gentaget tegn."
    return None


def _tokens():
    import account_tokens
    return account_tokens


def _base_url() -> str:
    import os
    return (os.getenv("APP_BASE_URL") or request.url_root).rstrip("/")


def _cursor():
    import MySQLdb.cursors
    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def _user_by_email(email: str):
    cur = _cursor()
    try:
        cur.execute("SELECT id, username, email FROM users WHERE LOWER(email) = LOWER(%s) LIMIT 1", (email,))
        return cur.fetchone()
    finally:
        cur.close()


def _send_reset_email(user, raw_token: str) -> bool:
    from email_service import send_branded_email, _resolve_branding
    company_id = None
    try:
        cur = _cursor()
        cur.execute("SELECT company_id FROM company_users WHERE user_id = %s AND status = 'active' LIMIT 1",
                    (user["id"],))
        row = cur.fetchone()
        cur.close()
        company_id = row["company_id"] if row else None
    except Exception:
        company_id = None
    branding = _resolve_branding(company_id)
    return send_branded_email(
        user["email"], "Nulstil din adgangskode", "password_reset", branding,
        company_id=company_id, dedupe_key="pwreset:%s" % user["id"],
        company_name=branding.get("company_name") or "Futurematch",
        reset_url="%s%s" % (_base_url(), url_for("account.reset_password", token=raw_token)),
    )


def register_account_flows(app) -> bool:
    """Register the fallback screens unless ``auth`` already has a flow. Returns
    True when this module's routes are active."""
    if "auth.forgot_password" in app.view_functions:
        app.config["ACCOUNT_FLOW"] = "auth"
        return False
    app.register_blueprint(account_bp)
    app.config["ACCOUNT_FLOW"] = "account"
    return True


def _auth_helper():
    """Part A's ``auth.send_user_password_link`` when present."""
    try:
        import auth
        return getattr(auth, "send_user_password_link", None)
    except Exception:
        return None


def send_reset_link(user_id: int, *, actor: str = "self") -> bool:
    """Create a reset token for ``user_id`` and email the link. Used by the forgot-password
    form, HR ("Send nulstillingslink") and the admin user list. Never reveals whether
    mail could be sent to the caller beyond the boolean."""
    cur = _cursor()
    try:
        cur.execute("SELECT id, username, email FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
    finally:
        cur.close()
    if not user or not user.get("email"):
        return False
    delegate = _auth_helper()
    if delegate is not None and current_app.config.get("ACCOUNT_FLOW") == "auth":
        return bool(delegate(current_app.mysql.connection, user, purpose="reset"))
    raw = _tokens().create_token("password_reset", user["id"], RESET_TTL_MINUTES)
    logger.info("password reset link issued for user %s by %s", user["id"], actor)
    return _send_reset_email(user, raw)


def send_invite(user_id: int, *, email: str, name: str = "", company: dict | None = None) -> bool:
    """Invite a new employee: a link to choose their own password (valid 7 days)."""
    if not email:
        return False
    from email_service import send_employee_welcome
    delegate = _auth_helper()
    if delegate is not None and current_app.config.get("ACCOUNT_FLOW") == "auth":
        return bool(delegate(current_app.mysql.connection,
                             {"id": int(user_id), "email": email, "username": name}, purpose="invite"))
    raw = _tokens().create_token("invite", int(user_id), INVITE_TTL_MINUTES)
    url = "%s%s" % (_base_url(), url_for("account.accept_invite", token=raw))
    return send_employee_welcome(company or {}, {"email": email, "name": name},
                                 login_url=url_for("auth.login", _external=True),
                                 set_password_url=url)


def _token_ok(kind: str, token: str) -> bool:
    peek = getattr(_tokens(), "peek_token", None)
    if peek is None:
        return True            # validity is enforced when the form is submitted
    try:
        return peek(kind, token) is not None
    except Exception:
        return False


def _set_password(user_id: int, password: str) -> None:
    cur = current_app.mysql.connection.cursor()
    try:
        cur.execute("UPDATE users SET password = %s WHERE id = %s", (generate_password_hash(password), user_id))
        current_app.mysql.connection.commit()
    finally:
        cur.close()


@account_bp.route("/glemt-adgangskode", methods=["GET", "POST"])
def forgot_password():
    sent = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email or "@" not in email:
            flash("Skriv den e-mailadresse, du bruger til Futurematch.", "danger")
        else:
            # Same answer whether or not the address exists: no account enumeration.
            try:
                user = _user_by_email(email)
                if user:
                    send_reset_link(user["id"])
            except Exception as exc:
                logger.warning("forgot_password failed: %s", exc)
            sent = True
    return render_template("fm/account_flow.html", mode="forgot", sent=sent)


def _password_form(mode: str, token: str):
    title = {"reset": "Vælg en ny adgangskode", "invite": "Vælg din adgangskode"}[mode]
    kind = "password_reset" if mode == "reset" else "invite"
    if request.method == "GET":
        if not _token_ok(kind, token):
            return render_template("fm/account_flow.html", mode="invalid")
        return render_template("fm/account_flow.html", mode=mode, title=title, token=token)

    pw, pw2 = request.form.get("password") or "", request.form.get("password2") or ""
    if pw != pw2:
        flash("De to adgangskoder er ikke ens.", "danger")
        return render_template("fm/account_flow.html", mode=mode, title=title, token=token), 400
    problem = password_problem(pw)
    if problem:
        flash(problem, "danger")
        return render_template("fm/account_flow.html", mode=mode, title=title, token=token), 400
    user_id = _tokens().consume_token(kind, token)       # single use: consumed only now
    if not user_id:
        return render_template("fm/account_flow.html", mode="invalid"), 400
    _set_password(int(user_id), pw)
    flash("Din adgangskode er gemt. Log ind for at komme i gang.", "success")
    return redirect(url_for("auth.login"))


@account_bp.route("/nulstil-adgangskode/<token>", methods=["GET", "POST"])
def reset_password(token):
    return _password_form("reset", token)


@account_bp.route("/invitation/<token>", methods=["GET", "POST"])
def accept_invite(token):
    return _password_form("invite", token)
