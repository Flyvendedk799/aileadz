"""
Regression tests for Part A tier S-5 (platform hardening & the security suite).

S-5.1 CSP + HSTS, S-5.2 /readyz, S-5.3 CI, S-5.4 tenant isolation / SCIM /
API-key auth / CSRF presence, S-5.5 uploads, S-5.6 widget embedding allowlist.
"""

import io
import json
import os
import re
import unittest
from unittest import mock

import db_compat  # noqa: F401
from tests import secapp
from tests.secapp import client_as, get_app, patch_mysql

REPO = secapp._REPO_ROOT


def _n(sql):
    return " ".join(sql.split())


# ---------------------------------------------------------------------------
# S-5.1
# ---------------------------------------------------------------------------
class S51_Headers(unittest.TestCase):
    def _get(self, path="/login", headers=None, env=None):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch.dict(os.environ, env or {}, clear=False):
            return app.test_client().get(path, headers=headers or {})

    def test_csp_is_enforced_not_report_only(self):
        r = self._get()
        csp = r.headers.get("Content-Security-Policy")
        self.assertTrue(csp)
        self.assertNotIn("Content-Security-Policy-Report-Only", r.headers)
        for directive in ("object-src 'none'", "base-uri 'self'", "form-action 'self'",
                          "frame-ancestors 'self'", "report-uri /csp-report"):
            self.assertIn(directive, csp)

    def test_script_origins_are_pinned_to_the_known_cdns(self):
        csp = self._get().headers["Content-Security-Policy"]
        script = re.search(r"script-src ([^;]*)", csp).group(1)
        self.assertNotIn(" https: ", " " + script + " ")          # no blanket https:
        self.assertNotIn("*", script)
        for host in ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com"):
            self.assertIn(host, script)

    def test_every_external_origin_the_templates_use_is_allowed(self):
        """Enforcing must not break the UI: each CDN host referenced by a
        template has to be covered by the policy."""
        csp = self._get().headers["Content-Security-Policy"]
        hosts = set()
        for root, _d, files in os.walk(os.path.join(REPO, "templates")):
            for fn in files:
                text = open(os.path.join(root, fn), encoding="utf-8", errors="ignore").read()
                hosts |= set(re.findall(r"""(?:src|href)=["']https://([a-z0-9.-]+)""", text))
        hosts.discard("futurematch.dk")          # an ordinary outbound link
        for h in hosts:
            self.assertIn(h, csp, "template loads from %s but the CSP does not allow it" % h)

    def test_break_glass_switch_falls_back_to_report_only(self):
        r = self._get(env={"CSP_ENFORCE": "0"})
        self.assertNotIn("Content-Security-Policy", r.headers)
        self.assertIn("Content-Security-Policy-Report-Only", r.headers)

    def test_hsts_only_on_https_and_not_in_sandbox(self):
        plain = self._get(env={"SANDBOX": "0"})
        self.assertNotIn("Strict-Transport-Security", plain.headers)
        secure = self._get(headers={"X-Forwarded-Proto": "https"}, env={"SANDBOX": "0"})
        self.assertRegex(secure.headers["Strict-Transport-Security"], r"^max-age=\d{7,}$")
        self.assertNotIn("includeSubDomains", secure.headers["Strict-Transport-Security"])
        sub = self._get(headers={"X-Forwarded-Proto": "https"},
                        env={"SANDBOX": "0", "HSTS_INCLUDE_SUBDOMAINS": "1", "HSTS_MAX_AGE": "600"})
        self.assertEqual(sub.headers["Strict-Transport-Security"], "max-age=600; includeSubDomains")
        off = self._get(headers={"X-Forwarded-Proto": "https"}, env={"SANDBOX": "0", "HSTS_MAX_AGE": "0"})
        self.assertNotIn("Strict-Transport-Security", off.headers)
        sand = self._get(headers={"X-Forwarded-Proto": "https"}, env={"SANDBOX": "1"})
        self.assertNotIn("Strict-Transport-Security", sand.headers)

    def test_other_defensive_headers(self):
        h = self._get().headers
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("camera=()", h["Permissions-Policy"])

    def test_csp_violation_reports_are_accepted_without_a_csrf_token(self):
        app = get_app()
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            r = app.test_client().post("/csp-report", data='{"csp-report":{"blocked-uri":"x"}}',
                                       content_type="application/csp-report")
        finally:
            app.config["WTF_CSRF_ENABLED"] = False
        self.assertEqual(r.status_code, 204)


# ---------------------------------------------------------------------------
# S-5.2
# ---------------------------------------------------------------------------
class S52_Readyz(unittest.TestCase):
    def _readyz(self, role=None, headers=None, env=None):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch.dict(os.environ, env or {}, clear=False):
            c = client_as(app, role) if role else app.test_client()
            return c.get("/readyz", headers=headers or {})

    def test_public_sees_only_the_verdict(self):
        r = self._readyz()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"status": "ready"})

    def test_liveness_stays_public_and_minimal(self):
        app = get_app()
        r = app.test_client().get("/healthz")
        self.assertEqual(r.get_json(), {"status": "ok"})

    def test_admin_sees_the_details(self):
        body = self._readyz(role="admin").get_json()
        for key in ("db", "catalog", "openai", "status"):
            self.assertIn(key, body)

    def test_non_admin_users_do_not(self):
        for role in ("employee", "hr_manager", "company_admin"):
            self.assertEqual(list(self._readyz(role=role).get_json()), ["status"], role)

    def test_monitor_token(self):
        env = {"HEALTH_TOKEN": "s3cret-monitor-token"}
        ok = self._readyz(headers={"X-Health-Token": "s3cret-monitor-token"}, env=env).get_json()
        self.assertIn("db", ok)
        bad = self._readyz(headers={"X-Health-Token": "nope"}, env=env).get_json()
        self.assertEqual(list(bad), ["status"])
        unset = self._readyz(headers={"X-Health-Token": "anything"}, env={"HEALTH_TOKEN": ""}).get_json()
        self.assertEqual(list(unset), ["status"])

    def test_degraded_still_reports_503_to_the_load_balancer(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("health._check_db", return_value=False), \
                mock.patch("health._features_block", return_value=None), \
                mock.patch("health._provider_block", return_value=None):
            r = app.test_client().get("/readyz")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json(), {"status": "degraded"})


# ---------------------------------------------------------------------------
# S-5.3
# ---------------------------------------------------------------------------
class S53_CiSecurity(unittest.TestCase):
    def setUp(self):
        self.wf = open(os.path.join(REPO, ".github", "workflows", "security.yml"), encoding="utf-8").read()

    def test_scheduled_full_history_secret_scan(self):
        self.assertIn("schedule:", self.wf)
        self.assertIn("cron:", self.wf)
        self.assertIn("fetch-depth: 0", self.wf)
        self.assertIn("gitleaks/gitleaks-action", self.wf)

    def test_dependency_audit_and_pinning_review_run(self):
        self.assertIn("pip-audit -r requirements.txt", self.wf)
        self.assertIn("check_requirements_pinning.py", self.wf)

    def test_every_requirement_is_bounded_on_both_sides(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pin", os.path.join(REPO, "scripts", "check_requirements_pinning.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        text = open(os.path.join(REPO, "requirements.txt"), encoding="utf-8", errors="ignore").read()
        self.assertEqual(mod.offenders(text), [])
        self.assertTrue(mod.offenders("flask\nrequests>=2\nnumpy>=1,<3\n"))

    def test_push_pipeline_scan_has_no_allowlist(self):
        toml = open(os.path.join(REPO, ".gitleaks.toml"), encoding="utf-8").read()
        self.assertNotIn("[allowlist]", "\n".join(l for l in toml.splitlines() if not l.strip().startswith("#")))


# ---------------------------------------------------------------------------
# S-5.4  tenant isolation: every statement that touches tenant data is scoped
# ---------------------------------------------------------------------------
TENANT_TABLES = {
    "course_orders", "order_approvals", "company_users", "department_budgets", "company_departments",
    "employee_learning_progress", "employee_goals", "employee_skills_matrix", "learning_paths",
    "company_courses", "company_supplier_agreements", "company_supplier_preferences",
    "company_approval_policies", "hr_chatbot_interactions", "chatbot_interactions", "company_widget_settings",
    "company_settings", "compliance_requirements", "company_notifications", "audit_log",
}


def _touched_tables(sql):
    return {m.group(2).lower() for m in re.finditer(r"\b(FROM|JOIN|UPDATE|INTO)\s+`?(\w+)`?", sql, re.I)}


class S54_TenantIsolation(unittest.TestCase):
    """Drive the tenant-scoped route families as a member of company 7 and assert
    that every statement on a tenant table is bound to company 7."""

    def _responder(self):
        def r(sql, params):
            s = _n(sql).lower()
            if "from companies c" in s and "company_users cu" in s:
                return {"id": 7, "company_name": "Acme", "company_slug": "acme", "user_role": "hr_manager",
                        "department": "Salg", "permissions": None}
            if s.startswith("select user_id, department from company_users"):
                return {"user_id": 9, "department": "Salg"}
            return None
        return r

    ROUTES = [
        ("GET", "/hr/order/o1/details"),
        ("POST", "/hr/order/o1/update"),
        ("POST", "/hr/approval/1/decide"),
        ("GET", "/hr/employee/9/details"),
        ("POST", "/hr/employee/9/toggle-status"),
        ("POST", "/hr/employee/9/reset-password"),
        ("POST", "/hr/budgets/save"),
        ("POST", "/hr/departments/3/edit"),
        ("POST", "/hr/departments/3/delete"),
        ("POST", "/hr/courses/3/delete"),
        ("POST", "/hr/courses/3/toggle"),
        ("POST", "/hr/learning-paths/3/toggle"),
        ("POST", "/hr/learning-paths/3/delete"),
        ("POST", "/hr/approval-policies/3/delete"),
        ("POST", "/hr/suppliers/agreements/3/delete"),
        ("GET", "/hr/chatbot/session/s1"),
        ("GET", "/hr/employee/9/goals"),
        ("POST", "/hr/goals/3/share"),
        ("GET", "/multitenant-reports/order/o1"),
        ("POST", "/multitenant-reports/order/o1/update"),
        ("GET", "/multitenant-reports/department/Salg"),
    ]

    def test_every_tenant_table_statement_is_bound_to_the_callers_company(self):
        app = get_app()
        offenders = []
        for method, path in self.ROUTES:
            fake, p = patch_mysql(app, self._responder())
            with p:
                client_as(app, "company_admin").open(
                    path, method=method,
                    json={"decision": "approved", "status": "completed", "shared": True, "annual_budget": 10,
                          "department": "Salg", "title": "x"} if method == "POST" else None)
            for sql, params in fake.log:
                low = _n(sql).lower()
                if low.startswith(("select 1", "show ", "create ", "alter ")):
                    continue
                tables = _touched_tables(low) & TENANT_TABLES
                if not tables:
                    continue
                first_from = re.search(r"\bfrom\s+`?(\w+)`?", low)
                if low.startswith("select") and first_from and first_from.group(1) == "companies":
                    continue   # the membership lookup that establishes the tenant
                if "insert into audit_log" in low:
                    continue   # append-only trail; carries company_id as a value
                scoped = ("company_id" in low) and (7 in (params or ()) or "'7'" in low)
                if not scoped:
                    offenders.append("%s %s -> %s" % (method, path, _n(sql)[:140]))
        self.assertEqual(offenders, [], "Statements on tenant tables without company scoping:\n" + "\n".join(offenders))

    def test_foreign_company_ids_in_the_url_or_body_are_never_trusted(self):
        """The tenant is ALWAYS the session's company; a company_id supplied by the
        caller must not change which tenant the query is bound to."""
        app = get_app()
        fake, p = patch_mysql(app, self._responder())
        with p:
            client_as(app, "company_admin").post(
                "/hr/budgets/save?company_id=99",
                json={"department": "Salg", "annual_budget": 5, "company_id": 99, "fiscal_year": 2026})
        writes = [(s, prm) for s, prm in fake.log if _n(s).lower().startswith("insert into department_budgets")]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1][0], 7)
        self.assertNotIn(99, writes[0][1])

    def test_search_never_crosses_tenants(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            client_as(app, "hr_manager").get("/api/search?q=anna&company_id=99")
        people = fake.queries("from company_users cu")
        self.assertEqual(len(people), 1)
        self.assertIn("cu.company_id = %s", people[0][0])
        self.assertEqual(people[0][1][0], 7)


# ---------------------------------------------------------------------------
# S-5.4  SCIM / API key authentication
# ---------------------------------------------------------------------------
class S54_ApiAuth(unittest.TestCase):
    def _call(self, method, path, key="ak_test", **kw):
        app = get_app()
        fake, p = patch_mysql(app)
        import enterprise_api
        data = {"id": 1, "company_id": 7, "rate_limit_per_hour": 100, "permissions": '["read:employees"]',
                "created_by": 1}
        with p, mock.patch.object(enterprise_api.api_manager, "authenticate_api_request", return_value=(data, None)) as auth, \
                mock.patch.object(enterprise_api.api_manager, "check_rate_limit", return_value=(True, 1)), \
                mock.patch.object(enterprise_api.api_manager, "log_api_request"):
            headers = {"X-API-Key": key} if key else {}
            resp = app.test_client().open(path, method=method, headers=headers, **kw)
        return resp, fake, auth

    def test_no_key_no_entry(self):
        resp, _fake, auth = self._call("GET", "/api/v1/employees", key=None)
        self.assertEqual(resp.status_code, 401)
        auth.assert_not_called()

    def test_invalid_key_is_401(self):
        app = get_app()
        import enterprise_api
        fake, p = patch_mysql(app)
        with p, mock.patch.object(enterprise_api.api_manager, "authenticate_api_request",
                                  return_value=(None, "Invalid or expired API key")):
            r = app.test_client().get("/api/v1/employees", headers={"X-API-Key": "ak_wrong"})
        self.assertEqual(r.status_code, 401)

    def test_scope_is_enforced(self):
        resp, *_ = self._call("POST", "/api/v1/employees", json={"email": "a@b.dk"})   # needs write:employees
        self.assertEqual(resp.status_code, 403)

    def test_scim_uses_the_keys_company_not_the_callers_claim(self):
        resp, fake, _ = self._call("GET", "/scim/v2/Users?companyId=99")
        queries = [(s, prm) for s, prm in fake.log if "company_users" in _n(s).lower()]
        for sql, params in queries:
            self.assertIn("company_id", _n(sql).lower())
            self.assertIn(7, params)
            self.assertNotIn(99, params)

    def test_scim_rejects_unauthenticated_calls(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            for method, path in (("GET", "/scim/v2/Users"), ("POST", "/scim/v2/Users"),
                                 ("DELETE", "/scim/v2/Users/5"), ("PUT", "/scim/v2/Users/5")):
                r = app.test_client().open(path, method=method, json={})
                self.assertEqual(r.status_code, 401, "%s %s" % (method, path))


# ---------------------------------------------------------------------------
# S-5.4  CSRF presence
# ---------------------------------------------------------------------------
class S54_CsrfPresence(unittest.TestCase):
    def test_standalone_pages_with_post_forms_get_tokens(self):
        app = get_app()
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            fake, p = patch_mysql(app)
            with p:
                for path in ("/login", "/register", "/vendor/login", "/forgot-password", "/vendor/forgot-password"):
                    html = app.test_client().get(path).get_data(as_text=True)
                    self.assertIn('name="csrf-token"', html, path)
                    forms = re.findall(r"<form\b[^>]*>", html, re.I)
                    post_forms = [f for f in forms if re.search(r"method=[\"']?post", f, re.I)]
                    self.assertTrue(post_forms, path)
                    self.assertEqual(html.count('name="csrf_token"'), len(post_forms), path)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_enforced_in_production_config_and_only_the_sandbox_switches_it_off(self):
        import csrf_protect
        src = open(os.path.join(REPO, "csrf_protect.py"), encoding="utf-8").read()
        self.assertIn('os.environ.get("SANDBOX") == "1"', src)
        self.assertIn("WTF_CSRF_TIME_LIMIT", src)
        self.assertEqual(csrf_protect.EXEMPT_BLUEPRINTS, ("api_enterprise", "scim"))

    def test_no_route_outside_the_exempt_list_is_csrf_exempt(self):
        import csrf_protect
        allowed = set(csrf_protect.EXEMPT_ENDPOINTS)
        app = get_app()
        exempt_views = {f"{v.__module__}.{v.__name__}" for v in
                        (app.view_functions.get(e) for e in allowed) if v is not None}
        self.assertEqual(csrf_protect.csrf._exempt_views, exempt_views)


# ---------------------------------------------------------------------------
# S-5.5
# ---------------------------------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class _FS:
    def __init__(self, data, name="x.png"):
        self.stream = io.BytesIO(data)
        self.filename = name

    def read(self, n=-1):
        return self.stream.read(n)


class S55_Uploads(unittest.TestCase):
    def setUp(self):
        import upload_guard
        self.ug = upload_guard

    def test_real_images_are_stored_under_random_names(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            name = self.ug.save_image(_FS(PNG, "../../evil.html"), d)
            self.assertTrue(name.endswith(".png"))
            self.assertNotIn("evil", name)
            self.assertEqual(os.listdir(d), [name])
            name2 = self.ug.save_image(_FS(JPG, "a.php"), d)
            self.assertTrue(name2.endswith(".jpg"))

    def test_html_svg_and_scripts_are_refused_whatever_the_extension(self):
        import tempfile
        payloads = {
            "evil.png": b"<html><script>alert(1)</script></html>",
            "evil.svg": b"<svg onload=alert(1)></svg>",
            "evil.jpg": b"GIF",
            "evil.gif": b"<?php system($_GET['c']); ?>",
        }
        with tempfile.TemporaryDirectory() as d:
            for name, data in payloads.items():
                with self.assertRaises(self.ug.UploadRejected, msg=name):
                    self.ug.save_image(_FS(data, name), d)
            self.assertEqual(os.listdir(d), [])

    def test_oversize_and_empty_are_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(self.ug.UploadRejected):
                self.ug.save_image(_FS(PNG + b"\x00" * (3 * 1024 * 1024)), d, max_bytes=2 * 1024 * 1024)
            with self.assertRaises(self.ug.UploadRejected):
                self.ug.save_image(_FS(b""), d)

    def test_cv_content_must_match_the_claimed_type(self):
        self.assertTrue(self.ug.looks_like(b"%PDF-1.7\n...", ".pdf"))
        self.assertFalse(self.ug.looks_like(b"MZ\x90\x00 exe", ".pdf"))
        self.assertTrue(self.ug.looks_like(PNG, ".png"))
        self.assertFalse(self.ug.looks_like(b"<script>", ".png"))
        self.assertTrue(self.ug.looks_like("Mit CV – æøå".encode("utf-8"), ".txt"))
        self.assertFalse(self.ug.looks_like(b"\x00\x01\x02binary", ".txt"))
        self.assertFalse(self.ug.looks_like(b"<!DOCTYPE html><html>", ".txt"))
        self.assertFalse(self.ug.looks_like(b"x", ".exe"))

    def test_admin_broadcast_route_rejects_a_disguised_html_upload(self):
        import tempfile
        app = get_app()
        fake, p = patch_mysql(app)
        with tempfile.TemporaryDirectory() as root, p, mock.patch.object(app, "root_path", root):
            c = client_as(app, "admin")
            r = c.post("/admin/notifications", data={
                "title": "Hej", "description": "x", "target": "all",
                "image_file": (io.BytesIO(b"<html><script>alert(1)</script>"), "bild.png"),
            }, content_type="multipart/form-data")
            self.assertEqual(r.status_code, 302)
            up = os.path.join(root, "static", "uploads", "notifications")
            self.assertFalse(os.path.isdir(up) and os.listdir(up))
            self.assertEqual(fake.queries("insert into notifications"), [])

    def test_admin_broadcast_route_accepts_a_real_image_and_renames_it(self):
        import tempfile
        app = get_app()
        fake, p = patch_mysql(app)
        with tempfile.TemporaryDirectory() as root, p, mock.patch.object(app, "root_path", root):
            c = client_as(app, "admin")
            c.post("/admin/notifications", data={
                "title": "Hej", "description": "x", "target": "all",
                "image_file": (io.BytesIO(PNG), "../../etc/bild.png"),
            }, content_type="multipart/form-data")
            up = os.path.join(root, "static", "uploads", "notifications")
            files = os.listdir(up)
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0], r"^[0-9a-f]{32}\.png$")
            ins = fake.queries("insert into notifications")
            self.assertEqual(len(ins), 1)
            self.assertIn(files[0], ins[0][1][-1])

    def test_cv_endpoint_rejects_mismatched_content_before_parsing(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("cv_parse_store.start") as start:
            r = client_as(app, "employee").post("/api/cv/parse", data={
                "session_id": "s1", "cv": (io.BytesIO(b"MZ\x90\x00 not a pdf"), "cv.pdf")},
                content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("passer ikke til filtypen", r.get_json()["error"])
        start.assert_not_called()

    def test_global_body_cap_returns_413(self):
        app = get_app()
        self.assertGreater(app.config["MAX_CONTENT_LENGTH"], 0)
        with mock.patch.dict(app.config, {"MAX_CONTENT_LENGTH": 1024}):
            r = client_as(app, "employee").post("/api/cv/parse", data={"session_id": "s", "cv_text": "x" * 5000})
        self.assertEqual(r.status_code, 413)


# ---------------------------------------------------------------------------
# S-5.6  widget embedding allowlist (Part A verification; the parent-origin fix is N-5.6)
# ---------------------------------------------------------------------------
class S56_WidgetAllowlist(unittest.TestCase):
    def _widget_row(self, allowed):
        return {"widget_token": "tok", "company_id": 7, "cid": 7, "company_name": "Acme", "company_slug": "acme",
                "allowed_domains": allowed, "theme_primary_color": "#0f766e", "theme_text_color": "#fff",
                "widget_title": "Hej", "widget_size": "medium", "position": "bottom-right", "is_active": 1}

    def _responder(self, allowed):
        def r(sql, params):
            if "company_widget_settings" in _n(sql):
                return self._widget_row(allowed)
            return None
        return r

    def test_lookalike_and_suffix_hosts_do_not_match(self):
        import app1
        allowed = app1._widget_allowed_hosts({"allowed_domains": "https://www.acme.dk, partner.example.com"})
        self.assertEqual(allowed, ["acme.dk", "partner.example.com"])
        ok = lambda h: app1._widget_host_allowed(h, allowed)    # noqa: E731
        self.assertTrue(ok("acme.dk"))
        self.assertTrue(ok("www.acme.dk"))
        self.assertTrue(ok("shop.acme.dk"))
        self.assertFalse(ok("evilacme.dk"))
        self.assertFalse(ok("acme.dk.evil.com"))
        self.assertFalse(ok("acme.dkx"))
        self.assertFalse(ok(""))

    def test_foreign_origin_is_rejected_on_ask(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder("acme.dk"))
        with p:
            r = app.test_client().post("/app1/widget/tok/ask", json={"query": "hej"},
                                       headers={"Origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_spoofed_subdomain_trick_is_rejected(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder("acme.dk"))
        with p:
            r = app.test_client().post("/app1/widget/tok/ask", json={"query": "hej"},
                                       headers={"Origin": "https://acme.dk.evil.com"})
        self.assertEqual(r.status_code, 403)

    def test_no_cors_grant_is_ever_given_to_a_foreign_origin(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder("acme.dk"))
        with p:
            r = app.test_client().options("/app1/widget/tok/ask", headers={"Origin": "https://evil.example.com"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)
        self.assertNotEqual(r.headers.get("Access-Control-Allow-Origin"), "*")

    def test_allowed_origin_gets_a_scoped_cors_grant(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder("acme.dk"))
        with p:
            r = app.test_client().options("/app1/widget/tok/ask", headers={"Origin": "https://shop.acme.dk"})
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "https://shop.acme.dk")
        self.assertIn("Origin", r.headers.get("Vary", ""))

    def test_browser_enforced_frame_ancestors_follow_the_allowlist(self):
        app = get_app()
        with mock.patch("branding_service.get_branding", return_value={}):
            fake, p = patch_mysql(app, self._responder("acme.dk, partner.example.com"))
            with p:
                r = app.test_client().get("/app1/widget/tok", headers={"Referer": "https://acme.dk/kurser"})
            csp = r.headers["Content-Security-Policy"]
            self.assertIn("frame-ancestors 'self' https://acme.dk https://*.acme.dk "
                          "https://partner.example.com https://*.partner.example.com", csp)
            self.assertNotIn("frame-ancestors *", csp)
            self.assertNotIn("X-Frame-Options", r.headers)          # the CSP is the authority here
            fake2, p2 = patch_mysql(app, self._responder("acme.dk"))
            with p2:
                foreign = app.test_client().get("/app1/widget/tok", headers={"Referer": "https://evil.example.com/"})
            self.assertEqual(foreign.status_code, 403)

    def test_widget_without_allowlist_stays_embeddable_but_the_rest_of_the_csp_holds(self):
        app = get_app()
        with mock.patch("branding_service.get_branding", return_value={}):
            fake, p = patch_mysql(app, self._responder(""))
            with p:
                r = app.test_client().get("/app1/widget/tok")
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors *", csp)
        self.assertIn("object-src 'none'", csp)


# ---------------------------------------------------------------------------
# S-1.4 follow-up: SECRET_KEY rotation re-encrypts derived-key secrets
# ---------------------------------------------------------------------------
class S14_SecretKeyRotation(unittest.TestCase):
    def _load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("rotate", os.path.join(REPO, "scripts", "rotate_secret_key.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_rotation_re_encrypts_and_old_key_no_longer_reads(self):
        rot = self._load()
        old_k, new_k = "o" * 40, "n" * 40
        old_f, new_f = rot.derive(old_k, "derived"), rot.derive(new_k, "derived")
        old_2fa, new_2fa = rot.derive(old_k, "2fa"), rot.derive(new_k, "2fa")
        store = {
            "ai": {"OPENAI_API_KEY": "fernet$" + old_f.encrypt(b"sk-live-123").decode()},
            "sso": {5: json.dumps({"client_id": "c", "client_secret": "fernet$" + old_f.encrypt(b"idp-secret").decode()})},
            "tfa": {9: old_2fa.encrypt(b"JBSWY3DPEHPK3PXP").decode()},
        }

        class Cur:
            def __init__(s):
                s.rows = []

            def execute(s, sql, params=None):
                q = " ".join(sql.split())
                if q.startswith("SELECT secret_name"):
                    s.rows = list(store["ai"].items())
                elif q.startswith("SELECT id, config"):
                    s.rows = list(store["sso"].items())
                elif q.startswith("SELECT user_id, secret_enc"):
                    s.rows = list(store["tfa"].items())
                elif q.startswith("UPDATE ai_secrets"):
                    store["ai"][params[1]] = params[0]
                elif q.startswith("UPDATE company_sso_configs"):
                    store["sso"][params[1]] = params[0]
                elif q.startswith("UPDATE user_2fa"):
                    store["tfa"][params[1]] = params[0]

            def fetchall(s):
                return s.rows

            def close(s):
                pass

        class Conn:
            committed = False

            def cursor(s):
                return Cur()

            def commit(s):
                Conn.committed = True

            def rollback(s):
                pass

        before = json.dumps(store, sort_keys=True)
        dry = rot.rotate(Conn(), old_k, new_k, apply=False, environ={})
        self.assertEqual(json.dumps(store, sort_keys=True), before)          # dry run changes nothing
        self.assertEqual(dry["ai_secrets"]["rotated"], 1)
        done = rot.rotate(Conn(), old_k, new_k, apply=True, environ={})
        self.assertEqual([done[k]["rotated"] for k in ("ai_secrets", "sso_client_secrets", "totp_secrets")], [1, 1, 1])
        self.assertEqual(new_f.decrypt(store["ai"]["OPENAI_API_KEY"][7:].encode()), b"sk-live-123")
        sso = json.loads(store["sso"][5])
        self.assertEqual(new_f.decrypt(sso["client_secret"][7:].encode()), b"idp-secret")
        self.assertEqual(new_2fa.decrypt(store["tfa"][9].encode()), b"JBSWY3DPEHPK3PXP")
        with self.assertRaises(Exception):
            old_f.decrypt(store["ai"]["OPENAI_API_KEY"][7:].encode())          # old key is dead
        again = rot.rotate(Conn(), old_k, new_k, apply=True, environ={})
        self.assertEqual(again["ai_secrets"]["unreadable"], 1)                   # idempotent: nothing double-encrypted

    def test_dedicated_key_families_are_skipped(self):
        rot = self._load()

        class Conn:
            def cursor(s):
                class C:
                    def execute(*a, **k):
                        raise AssertionError("must not query a family with its own key")

                    def fetchall(*a):
                        return []

                    def close(*a):
                        pass
                return C()

            def commit(s):
                pass

            def rollback(s):
                pass

        out = rot.rotate(Conn(), "a" * 40, "b" * 40, environ={
            "AI_SECRET_KEY": "x", "SSO_FERNET_KEY": "y", "TWOFA_FERNET_KEY": "z"})
        self.assertTrue(all("skipped_reason" in v for v in out.values()))


if __name__ == "__main__":
    unittest.main()
