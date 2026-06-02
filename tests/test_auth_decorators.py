"""
Tests for the centralised auth decorators.

These run under plain pytest with NO database and NO real app: we build a tiny
throwaway Flask app, register stub ``auth.login`` and ``dashboard.dashboard``
endpoints (so redirects resolve), attach decorated routes, and drive them with
the Werkzeug test client + ``session_transaction``.
"""

import os
import sys

import pytest
from flask import Flask, jsonify

# Make the project root importable when pytest is run from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from auth_decorators import (  # noqa: E402
    login_required,
    require_role,
    require_company,
    require_company_role,
    requires_feature,
)


def build_app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True

    # --- Stub endpoints that the decorators redirect to ---------------------
    # Register under the exact endpoint names the decorators use so that
    # url_for('auth.login') / url_for('dashboard.dashboard') resolve.
    app.add_url_rule("/auth/login", endpoint="auth.login",
                     view_func=lambda: ("LOGIN PAGE", 200))
    app.add_url_rule("/dashboard", endpoint="dashboard.dashboard",
                     view_func=lambda: ("DASHBOARD", 200))

    # --- Protected routes ---------------------------------------------------
    @app.route("/protected")
    @login_required
    def protected():
        return "SECRET"

    @app.route("/api/protected")
    @login_required
    def api_protected():
        return jsonify({"ok": True})

    @app.route("/hr-only")
    @require_role("hr_manager")
    def hr_only():
        return "HR AREA"

    @app.route("/api/hr-only")
    @require_role("hr_manager")
    def api_hr_only():
        return jsonify({"ok": True})

    @app.route("/company-scoped")
    @require_company
    def company_scoped():
        return "COMPANY"

    @app.route("/company-admin")
    @require_company_role("company_admin", "hr_manager")
    def company_admin_area():
        return "COMPANY ADMIN"

    @app.route("/premium")
    @requires_feature("advanced_analytics")
    def premium():
        return "PREMIUM"

    return app


@pytest.fixture
def app():
    return build_app()


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client, **session_values):
    with client.session_transaction() as sess:
        for k, v in session_values.items():
            sess[k] = v


# ---------------------------------------------------------------------------
# login_required
# ---------------------------------------------------------------------------

def test_anonymous_html_redirects_to_login(client):
    resp = client.get("/protected")
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.headers["Location"]


def test_anonymous_json_path_returns_401_json(client):
    resp = client.get("/api/protected")
    assert resp.status_code == 401
    assert resp.is_json
    body = resp.get_json()
    assert body["status"] == 401
    assert "Log ind" in body["error"]


def test_anonymous_accept_json_returns_401_json(client):
    # Non-/api path but client prefers JSON -> still a JSON 401.
    resp = client.get("/protected", headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert resp.is_json
    assert resp.get_json()["status"] == 401


def test_logged_in_user_allowed(client):
    _login(client, user="alice")
    resp = client.get("/protected")
    assert resp.status_code == 200
    assert resp.data == b"SECRET"


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------

def test_require_role_anonymous_redirects_to_login(client):
    resp = client.get("/hr-only")
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.headers["Location"]


def test_require_role_wrong_role_redirects_to_dashboard(client):
    _login(client, user="bob", role="user")
    resp = client.get("/hr-only")
    assert resp.status_code in (301, 302)
    assert "/dashboard" in resp.headers["Location"]


def test_require_role_wrong_role_json_returns_403(client):
    _login(client, user="bob", role="user")
    resp = client.get("/api/hr-only")
    assert resp.status_code == 403
    assert resp.is_json
    assert resp.get_json()["status"] == 403


def test_require_role_right_role_allowed(client):
    _login(client, user="carol", role="hr_manager")
    resp = client.get("/hr-only")
    assert resp.status_code == 200
    assert resp.data == b"HR AREA"


def test_require_role_admin_always_allowed(client):
    # 'admin' is the platform super-admin and is not in the allowed list,
    # yet must still be allowed through.
    _login(client, user="root", role="admin")
    resp = client.get("/hr-only")
    assert resp.status_code == 200
    assert resp.data == b"HR AREA"


# ---------------------------------------------------------------------------
# require_company
# ---------------------------------------------------------------------------

def test_require_company_anonymous_redirects_to_login(client):
    resp = client.get("/company-scoped")
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.headers["Location"]


def test_require_company_missing_company_redirects_to_dashboard(client):
    _login(client, user="dave")  # logged in but no company_id
    resp = client.get("/company-scoped")
    assert resp.status_code in (301, 302)
    assert "/dashboard" in resp.headers["Location"]


def test_require_company_with_company_allowed(client):
    _login(client, user="dave", company_id=42)
    resp = client.get("/company-scoped")
    assert resp.status_code == 200
    assert resp.data == b"COMPANY"


# ---------------------------------------------------------------------------
# require_company_role
# ---------------------------------------------------------------------------

def test_require_company_role_anonymous_redirects_to_login(client):
    resp = client.get("/company-admin")
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.headers["Location"]


def test_require_company_role_wrong_role_redirects(client):
    _login(client, user="erin", company_role="department_head")
    resp = client.get("/company-admin")
    assert resp.status_code in (301, 302)
    assert "/dashboard" in resp.headers["Location"]


def test_require_company_role_right_role_allowed(client):
    _login(client, user="frank", company_role="company_admin")
    resp = client.get("/company-admin")
    assert resp.status_code == 200
    assert resp.data == b"COMPANY ADMIN"


def test_require_company_role_super_admin_bypass(client):
    _login(client, user="root", role="admin", company_role="nobody")
    resp = client.get("/company-admin")
    assert resp.status_code == 200
    assert resp.data == b"COMPANY ADMIN"


# ---------------------------------------------------------------------------
# requires_feature  (fail-open behaviour, no DB)
# ---------------------------------------------------------------------------

def test_requires_feature_fails_open_without_db(client):
    # No mysql attached to the app and no company_id -> flags unresolvable
    # -> must FAIL OPEN (allow) so prod is not broken before the paywall wave.
    _login(client, user="grace")
    resp = client.get("/premium")
    assert resp.status_code == 200
    assert resp.data == b"PREMIUM"


def test_requires_feature_anonymous_fails_open(client):
    # Even anonymous fails open for now (no company to resolve flags from).
    resp = client.get("/premium")
    assert resp.status_code == 200
    assert resp.data == b"PREMIUM"


def test_requires_feature_blocks_when_flag_off(monkeypatch):
    # When flags ARE resolvable and the flag is off, the route is blocked.
    import auth_decorators

    monkeypatch.setattr(
        auth_decorators,
        "_resolve_company_features",
        lambda company_id: {"advanced_analytics": False},
    )
    app = build_app()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "heidi"
        sess["company_id"] = 7
    resp = client.get("/premium")
    assert resp.status_code in (301, 302)
    assert "/dashboard" in resp.headers["Location"]


def test_requires_feature_allows_when_flag_on(monkeypatch):
    import auth_decorators

    monkeypatch.setattr(
        auth_decorators,
        "_resolve_company_features",
        lambda company_id: {"advanced_analytics": True},
    )
    app = build_app()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "ivan"
        sess["company_id"] = 7
    resp = client.get("/premium")
    assert resp.status_code == 200
    assert resp.data == b"PREMIUM"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
