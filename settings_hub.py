"""Company settings hub (N-4.6, N-7.1, N-7.2): ONE place for a company's settings.

``/virksomhed/indstillinger`` with tabs:

    Virksomhed | Branding | Chatbot & widget | Webhooks | SSO | API-nøgler | Integrationer | Bestillingspolitik

Branding, chatbot/widget and webhooks keep their existing (complete) views: the
hub calls them and they render inside the hub's tab bar, and their old GET URLs
redirect here (``hub_redirect``) - nothing is removed, everything is reachable
from one place. SSO (OAuth2/OIDC only), API keys, integrations and the
order policy are new hub-native tabs.

Visibility is by capability (``capabilities.can``): SSO and API keys need a
company admin, the rest HR managers.
"""

from __future__ import annotations

import json
import logging

from flask import (Blueprint, current_app, flash, g, redirect, render_template, request,
                   session, url_for)

import capabilities

logger = logging.getLogger(__name__)

settings_hub_bp = Blueprint("settings_hub", __name__, url_prefix="/virksomhed/indstillinger")
sso_discovery_bp = Blueprint("sso_discovery", __name__)

# key, label, icon, capability
TABS = [
    ("virksomhed", "Virksomhed", "fa-building", "company.settings"),
    ("branding", "Branding", "fa-palette", "company.branding"),
    ("chatbot", "Chatbot & widget", "fa-robot", "company.chatbot_widget"),
    ("webhooks", "Webhooks", "fa-plug", "company.webhooks"),
    ("sso", "SSO", "fa-key", "company.sso"),
    ("api", "API-nøgler", "fa-code", "company.api_keys"),
    ("integrationer", "Integrationer", "fa-diagram-project", "company.settings"),
    ("bestilling", "Bestillingspolitik", "fa-scale-balanced", "company.policies"),
]
_TAB_KEYS = {t[0] for t in TABS}


def visible_tabs():
    return [{"key": k, "label": l, "icon": i} for k, l, i, cap in TABS if capabilities.can(cap)]


@settings_hub_bp.app_context_processor
def _inject_tabs():
    def settings_hub_tabs():
        active = getattr(g, "settings_tab", None)
        return [dict(t, active=(t["key"] == active)) for t in visible_tabs()]

    def settings_hub_sub():
        return getattr(g, "settings_sub", None)

    return {"settings_hub_tabs": settings_hub_tabs, "settings_hub_sub": settings_hub_sub}


def hub_redirect(tab, **values):
    """Old standalone GET URLs call this first: outside the hub they redirect in."""
    if request.method == "GET" and not getattr(g, "via_settings_hub", False):
        if "settings_hub.tab" in current_app.view_functions:
            return redirect(url_for("settings_hub.tab", tab=tab, **values))
    return None


def _company_id():
    return session.get("company_id")


def _conn():
    return current_app.mysql.connection


def _company_row():
    import MySQLdb.cursors
    cur = _conn().cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT * FROM companies WHERE id = %s", (_company_id(),))
        return cur.fetchone() or {}
    finally:
        cur.close()


def _denied(message="Du har ikke adgang til denne indstilling."):
    flash(message, "danger")
    return redirect(url_for("dashboard.dashboard"))


def _delegate(endpoint, tab, sub=None):
    view = current_app.view_functions.get(endpoint)
    if view is None:
        flash("Denne indstilling er ikke tilgængelig i dette miljø.", "warning")
        return redirect(url_for("settings_hub.index"))
    g.via_settings_hub = True
    g.settings_tab = tab
    g.settings_sub = sub
    return view()


@settings_hub_bp.route("/", methods=["GET", "POST"])
def index():
    return tab("virksomhed")


@settings_hub_bp.route("/<tab>", methods=["GET", "POST"])
def tab(tab):
    if not session.get("user"):
        return redirect(url_for("auth.login"))
    if tab not in _TAB_KEYS:
        return redirect(url_for("settings_hub.index"))
    if not _company_id():
        if session.get("role") == "admin":
            flash("Vælg en virksomhed (Åbn som HR), før du åbner virksomhedsindstillinger.", "warning")
            return redirect(url_for("companies.admin_companies_list"))
        return _denied("Virksomhedsindstillinger kræver en virksomhedskonto.")
    cap = next(c for k, _l, _i, c in TABS if k == tab)
    if not capabilities.can(cap):
        return _denied()

    g.settings_tab = tab
    if tab == "virksomhed":
        return _tab_company()
    if tab == "branding":
        return _delegate("companies.branding", "branding")
    if tab == "chatbot":
        sub = request.args.get("view") if request.args.get("view") in ("chatbot", "widget") else "chatbot"
        endpoint = "hr_dashboard.widget_creator" if sub == "widget" else "hr_dashboard.chatbot_settings"
        return _delegate(endpoint, "chatbot", sub=sub)
    if tab == "webhooks":
        return _delegate("enterprise_settings.webhooks_page", "webhooks")
    if tab == "sso":
        return _tab_sso()
    if tab == "api":
        return _tab_api()
    if tab == "integrationer":
        return _tab_integrations()
    return _tab_policy()


# ── Virksomhed ──────────────────────────────────────────────────────────────
def _tab_company():
    import MySQLdb.cursors
    company = _company_row()
    settings = {}
    cur = _conn().cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT * FROM company_settings WHERE company_id = %s", (_company_id(),))
        settings = cur.fetchone() or {}
    except Exception:
        settings = {}
    open_request = None
    try:
        cur.execute("SELECT id, created_at FROM account_requests WHERE company_id = %s AND kind = 'deactivate' "
                    "AND status = 'open' ORDER BY id DESC LIMIT 1", (_company_id(),))
        open_request = cur.fetchone()
    except Exception:
        open_request = None
    cur.close()
    return render_template("fm/settings_hub.html", company=company, settings=settings,
                           open_request=open_request, tab="virksomhed")


@settings_hub_bp.route("/deaktiver", methods=["POST"])
def request_deactivation():
    """'Deaktiver konto' as a real request flow: it creates a request the platform
    admin sees (and a notification to every platform admin); nothing is switched
    off automatically."""
    if not capabilities.can("company.sso") or not _company_id():   # company admin only
        return _denied()
    note = (request.form.get("note") or "").strip()[:1000]
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM account_requests WHERE company_id = %s AND kind = 'deactivate' AND status = 'open'",
                    (_company_id(),))
        if cur.fetchone():
            flash("Der ligger allerede en anmodning om deaktivering. Vi vender tilbage.", "info")
            return redirect(url_for("settings_hub.index"))
        cur.execute("INSERT INTO account_requests (company_id, kind, requested_by, requested_by_name, note) "
                    "VALUES (%s, 'deactivate', %s, %s, %s)",
                    (_company_id(), session.get("user_id"), session.get("user"), note or None))
        try:
            from notification_service import notify_user
            cur.execute("SELECT username FROM users WHERE role = 'admin'")
            for r in cur.fetchall() or []:
                uname = r["username"] if isinstance(r, dict) else r[0]
                notify_user(cur, title="Anmodning om deaktivering af virksomhed",
                            message="%s har bedt om at deaktivere virksomhedskontoen. %s" % (
                                session.get("company_name") or "En virksomhed", note),
                            username=uname, company_id=_company_id(), kind="account_request",
                            action_url="/companies/admin/%s" % _company_id(),
                            dedupe_key="deactivate:%s" % _company_id(), dedupe_hours=24 * 30)
        except Exception as e:
            logger.debug("deactivation notification skipped: %s", e)
        conn.commit()
        flash("Din anmodning er sendt. Virksomhedskontoen deaktiveres ikke, før vi har bekræftet med dig. "
              "Data bevares i 90 dage.", "success")
    except Exception as e:
        conn.rollback()
        logger.warning("request_deactivation failed: %s", e)
        flash("Anmodningen kunne ikke sendes. Prøv igen.", "danger")
    finally:
        cur.close()
    return redirect(url_for("settings_hub.index"))


# ── SSO (OAuth2 / OIDC only; SAML and LDAP are hidden until S-D.1) ─────────
SSO_PRESETS = {
    "entra": {
        "label": "Microsoft Entra ID", "icon": "fa-brands fa-microsoft",
        "authorization_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "userinfo_url": "https://graph.microsoft.com/oidc/userinfo",
        "issuer": "https://login.microsoftonline.com/{tenant}/v2.0",
        "needs_tenant": True,
    },
    "google": {
        "label": "Google Workspace", "icon": "fa-brands fa-google",
        "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "issuer": "https://accounts.google.com",
        "needs_tenant": False,
    },
    "custom": {
        "label": "Anden OIDC-udbyder", "icon": "fa-solid fa-key",
        "authorization_url": "", "token_url": "", "userinfo_url": "", "issuer": "", "needs_tenant": False,
    },
}
_INACTIVE_PROVIDERS = {"saml": "SAML", "ldap": "LDAP", "active_directory": "Active Directory"}


def _sso_rows(company_id):
    import MySQLdb.cursors
    cur = _conn().cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT * FROM company_sso_configs WHERE company_id = %s ORDER BY id", (company_id,))
        rows = list(cur.fetchall() or [])
    except Exception:
        rows = []
    finally:
        cur.close()
    return rows


def _current_oidc(rows):
    """(row, config dict without secrets) for the oauth2 config, if any."""
    from enterprise_sso import _normalize_config
    for r in rows:
        if (r.get("provider") or "") in ("oauth2", "oauth", "azure", "google"):
            cfg = _normalize_config(r.get("config"))
            safe = {k: v for k, v in cfg.items() if k != "client_secret"}
            safe["has_secret"] = bool(cfg.get("client_secret"))
            return r, safe
    return None, {}


def _tab_sso():
    company = _company_row()
    rows = _sso_rows(company.get("id"))
    row, cfg = _current_oidc(rows)
    inactive = [{"name": r.get("provider_name") or r.get("provider"),
                 "type": _INACTIVE_PROVIDERS.get(r.get("provider"), r.get("provider"))}
                for r in rows if (r.get("provider") or "") in _INACTIVE_PROVIDERS]
    slug = company.get("company_slug") or ""
    try:
        redirect_uri = url_for("sso.sso_callback", company_slug=slug, provider="oauth2", _external=True)
        login_url = url_for("sso.sso_login", company_slug=slug, provider="oauth2", _external=True)
    except Exception:
        redirect_uri = "/sso/callback/%s/oauth2" % slug
        login_url = "/sso/login/%s/oauth2" % slug
    return render_template(
        "fm/settings_sso.html", company=company, row=row, cfg=cfg, presets=SSO_PRESETS,
        inactive=inactive, redirect_uri=redirect_uri, login_url=login_url, tab="sso",
        domain_missing=not (company.get("company_domain") or "").strip(),
    )


@settings_hub_bp.route("/sso/gem", methods=["POST"])
def save_sso():
    if not capabilities.can("company.sso") or not _company_id():
        return _denied()
    company = _company_row()
    preset = request.form.get("preset") if request.form.get("preset") in SSO_PRESETS else "custom"
    p = SSO_PRESETS[preset]
    tenant = (request.form.get("tenant") or "").strip()
    client_id = (request.form.get("client_id") or "").strip()
    secret = (request.form.get("client_secret") or "").strip()
    if not client_id:
        flash("Angiv klient-id fra din identitetsudbyder.", "danger")
        return redirect(url_for("settings_hub.tab", tab="sso"))
    if p["needs_tenant"] and not tenant:
        flash("Angiv Tenant-ID (Directory ID) fra Microsoft Entra.", "danger")
        return redirect(url_for("settings_hub.tab", tab="sso"))

    def fill(url):
        return url.replace("{tenant}", tenant)

    cfg = {
        "preset": preset,
        "tenant": tenant,
        "authorization_url": fill(p["authorization_url"]) or (request.form.get("authorization_url") or "").strip(),
        "token_url": fill(p["token_url"]) or (request.form.get("token_url") or "").strip(),
        "userinfo_url": fill(p["userinfo_url"]) or (request.form.get("userinfo_url") or "").strip(),
        "issuer": fill(p["issuer"]) or (request.form.get("issuer") or "").strip(),
        "client_id": client_id,
        "scope": (request.form.get("scope") or "openid email profile").strip(),
        "redirect_uri": url_for("sso.sso_callback", company_slug=company.get("company_slug") or "", provider="oauth2",
                                _external=True),
    }
    if not (cfg["authorization_url"] and cfg["token_url"]):
        flash("Autorisations- og token-URL er påkrævet.", "danger")
        return redirect(url_for("settings_hub.tab", tab="sso"))
    from enterprise_sso import _encrypt_config_secrets, _normalize_config

    conn = _conn()
    import MySQLdb.cursors
    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT id, config FROM company_sso_configs WHERE company_id = %s AND provider = 'oauth2'",
                    (company["id"],))
        existing = cur.fetchone()
        if secret:
            cfg["client_secret"] = secret
        elif existing:   # blank means "keep the stored secret"
            old = _normalize_config(existing.get("config"))
            if old.get("client_secret"):
                cfg["client_secret"] = old["client_secret"]
        if not cfg.get("client_secret"):
            flash("Angiv klienthemmeligheden (client secret).", "danger")
            return redirect(url_for("settings_hub.tab", tab="sso"))
        stored = json.dumps(_encrypt_config_secrets(cfg))
        enabled = 1 if request.form.get("is_enabled") else 0
        role = "employee"   # automatic provisioning never hands out elevated roles
        name = (request.form.get("provider_name") or p["label"])[:100]
        if existing:
            cur.execute("UPDATE company_sso_configs SET provider_name=%s, config=%s, is_enabled=%s, "
                        "auto_provision_users=%s, default_role=%s WHERE id=%s",
                        (name, stored, enabled, 1 if request.form.get("auto_provision") else 0, role, existing["id"]))
        else:
            cur.execute("INSERT INTO company_sso_configs (company_id, provider, provider_name, config, is_enabled, "
                        "auto_provision_users, default_role) VALUES (%s,'oauth2',%s,%s,%s,%s,%s)",
                        (company["id"], name, stored, enabled, 1 if request.form.get("auto_provision") else 0, role))
        conn.commit()
        flash("SSO er gemt." + ("" if enabled else " Den er endnu ikke aktiveret."), "success")
    except Exception as e:
        conn.rollback()
        logger.warning("save_sso failed: %s", e)
        flash("SSO-indstillingerne kunne ikke gemmes.", "danger")
    finally:
        cur.close()
    return redirect(url_for("settings_hub.tab", tab="sso"))


# ── API-nøgler ──────────────────────────────────────────────────────────────
def _tab_api():
    import api_keys_ui
    try:
        keys = api_keys_ui.list_keys(_conn(), _company_id())
        error = False
    except Exception as e:
        logger.warning("list api keys failed: %s", e)
        keys, error = [], True
    return render_template("fm/settings_api.html", keys=keys, error=error, presets=api_keys_ui.PERMISSION_PRESETS,
                           new_key=session.pop("_new_api_key", None), tab="api")


@settings_hub_bp.route("/api/opret", methods=["POST"])
def create_api_key():
    if not capabilities.can("company.api_keys") or not _company_id():
        return _denied()
    import api_keys_ui
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Giv nøglen et navn, så du kan genkende den senere.", "danger")
        return redirect(url_for("settings_hub.tab", tab="api"))
    try:
        key_id, raw = api_keys_ui.create_key(_conn(), _company_id(), name, preset=request.form.get("preset") or "read",
                                             created_by=session.get("user_id"))
        session["_new_api_key"] = {"id": key_id, "name": name, "key": raw}   # shown once, then gone
        flash("API-nøglen er oprettet. Kopiér den nu - den vises kun denne ene gang.", "success")
    except ValueError:
        flash("Ugyldig adgangstype.", "danger")
    except Exception as e:
        logger.warning("create api key failed: %s", e)
        flash("API-nøglen kunne ikke oprettes.", "danger")
    return redirect(url_for("settings_hub.tab", tab="api"))


@settings_hub_bp.route("/api/<int:key_id>/traek-tilbage", methods=["POST"])
def revoke_api_key(key_id):
    if not capabilities.can("company.api_keys") or not _company_id():
        return _denied()
    import api_keys_ui
    if api_keys_ui.revoke_key(_conn(), _company_id(), key_id):
        flash("Nøglen er trukket tilbage og virker ikke længere.", "success")
    else:
        flash("Nøglen blev ikke fundet.", "warning")
    return redirect(url_for("settings_hub.tab", tab="api"))


# ── Integrationer ───────────────────────────────────────────────────────────
def _tab_integrations():
    base = request.url_root.rstrip("/")
    return render_template("fm/settings_integrations.html", base=base, tab="integrationer",
                           company=_company_row())


# ── Bestillingspolitik (team orders; the form partial is provided by the AI work) ──
def _tab_policy():
    try:
        current_app.jinja_env.get_template("fm/_team_order_policy.html")
        partial = True
    except Exception:
        partial = False
    return render_template("fm/settings_policy.html", tab="bestilling", team_policy_partial=partial)


# ── SSO discovery for the login page ────────────────────────────────────────
@sso_discovery_bp.route("/sso/discover", methods=["GET", "POST"])
def discover():
    """Email-domain discovery: type your work email, land on your company's SSO."""
    error = None
    email = (request.form.get("email") or "").strip().lower()
    if request.method == "POST":
        domain = email.rsplit("@", 1)[1] if "@" in email else ""
        target = None
        if domain:
            import MySQLdb.cursors
            cur = _conn().cursor(MySQLdb.cursors.DictCursor)
            try:
                cur.execute(
                    "SELECT c.company_slug FROM companies c JOIN company_sso_configs s ON s.company_id = c.id "
                    "WHERE LOWER(c.company_domain) = %s AND s.provider = 'oauth2' AND s.is_enabled = 1 "
                    "AND COALESCE(c.status, 'active') = 'active' LIMIT 1", (domain,))
                target = cur.fetchone()
            except Exception as e:
                logger.warning("sso discovery failed: %s", e)
            finally:
                cur.close()
        if target and target.get("company_slug"):
            return redirect(url_for("sso.sso_login", company_slug=target["company_slug"], provider="oauth2"))
        error = "Vi fandt ingen SSO for den e-mailadresse. Log ind med adgangskode, eller bed din administrator om at slå SSO til."
    return render_template("fm/sso_login.html", error=error, email=email, hint=request.args.get("hint"))
