"""
Regression tests for Part A tier S-2 (authentication & access control).

S-2.1 CSRF, S-2.2 login hardening, S-2.3 role matrix, S-2.4 reset/invite tokens,
S-2.5 impersonation, S-2.6 TOTP 2FA, S-2.7 vendor auth. Real route table via
``run.create_app()`` with a recording fake MySQL (see tests/secapp.py).
"""

import re
import time
import unittest
from unittest import mock

from werkzeug.security import generate_password_hash

from tests import secapp
from tests.secapp import client_as, get_app, patch_mysql, login, ROLES

HASHED = generate_password_hash("Correct-Horse-9")


def _denied(resp):
    return resp.status_code in (401, 403, 301, 302, 303, 307)


def _csrf_token(html):
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


class CsrfOn:
    """Context manager: switch CSRF enforcement on for the shared test app."""

    def __enter__(self):
        self.app = get_app()
        self.prev = self.app.config.get("WTF_CSRF_ENABLED")
        self.app.config["WTF_CSRF_ENABLED"] = True
        return self.app

    def __exit__(self, *exc):
        self.app.config["WTF_CSRF_ENABLED"] = self.prev


# ---------------------------------------------------------------------------
# S-2.1 CSRF
# ---------------------------------------------------------------------------
class S21_Csrf(unittest.TestCase):
    def test_post_without_token_is_rejected(self):
        with CsrfOn() as app:
            fake, p = patch_mysql(app)
            with p:
                c = client_as(app, "admin")
                resp = c.post("/admin/make-superadmin/victim")
                self.assertEqual(resp.status_code, 400)
                self.assertIn("udløbet", resp.get_data(as_text=True))
                self.assertEqual(fake.queries("update users"), [])

    def test_ajax_failure_is_json(self):
        with CsrfOn() as app:
            c = client_as(app, "employee")
            resp = c.post("/api/learner/events", json={"event": "cv_start"})
            self.assertEqual(resp.status_code, 400)
            body = resp.get_json()
            self.assertTrue(body["csrf"])
            self.assertIn("Genindlæs", body["message"])

    def test_token_from_page_meta_unlocks_the_post(self):
        with CsrfOn() as app:
            fake, p = patch_mysql(app)
            with p:
                c = client_as(app, "employee")
                page = c.get("/login")
                token = _csrf_token(page.get_data(as_text=True))
                self.assertTrue(token, "csrf meta tag missing from HTML pages")
                resp = c.post("/api/learner/events", json={"event": "cv_start"},
                              headers={"X-CSRFToken": token})
            self.assertNotEqual(resp.status_code, 400)

    def test_forms_get_a_hidden_token_and_pages_load_csrf_js(self):
        with CsrfOn() as app:
            fake, p = patch_mysql(app)
            with p:
                c = app.test_client()
                html = c.get("/login").get_data(as_text=True)
            self.assertIn('name="csrf_token"', html)
            self.assertIn("csrf.js", html)
            token = _csrf_token(html)
            self.assertIn('value="%s"' % token, html.split('<form', 1)[1])

    def test_wrong_token_rejected(self):
        with CsrfOn() as app:
            c = client_as(app, "employee")
            resp = c.post("/api/learner/events", json={"event": "cv_start"},
                          headers={"X-CSRFToken": "forged"})
            self.assertEqual(resp.status_code, 400)

    def test_key_authenticated_surfaces_are_exempt(self):
        with CsrfOn() as app:
            c = app.test_client()
            fake, p = patch_mysql(app)
            with p:
                # API key missing -> the endpoint's OWN 401, not a CSRF 400.
                self.assertEqual(c.post("/api/v1/employees", json={}).status_code, 401)
                r = c.post("/scim/v2/Users", json={})
                self.assertNotEqual(r.status_code, 400)
                r = c.post("/app1/widget/tok/ask", json={"message": "hej"})
            self.assertNotIn(r.status_code, (400,))

    def test_logout_needs_post_and_get_does_not_log_out(self):
        app = get_app()
        c = client_as(app, "employee")
        resp = c.get("/logout")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Vil du logge ud", resp.get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertEqual(s.get("user"), "emp")
        c.post("/logout")
        with c.session_transaction() as s:
            self.assertNotIn("user", s)

    def test_csrf_js_present_and_patches_fetch(self):
        src = open(secapp._REPO_ROOT + "/static/futurematch/assets/csrf.js", encoding="utf-8").read()
        self.assertIn("X-CSRFToken", src)
        self.assertIn("window.fetch", src)
        self.assertIn("XMLHttpRequest", src)


# ---------------------------------------------------------------------------
# S-2.2 login hardening
# ---------------------------------------------------------------------------
class S22_LoginHardening(unittest.TestCase):
    def setUp(self):
        import login_guard
        login_guard.reset()
        self.lg = login_guard

    def _responder(self, stored=HASHED):
        def r(sql, params):
            if "from users where username" in " ".join(sql.split()).lower():
                return {"id": 5, "username": "anna", "password": stored, "credits": 1, "role": "user", "email": "a@x.dk"}
            return None
        return r

    def test_wrong_password_is_generic_and_counts(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder())
        with p:
            c = app.test_client()
            r = c.post("/login", data={"username": "anna", "password": "nope"}, follow_redirects=True)
            self.assertIn("Forkert brugernavn eller adgangskode", r.get_data(as_text=True))
            r2 = c.post("/login", data={"username": "ghost", "password": "nope"}, follow_redirects=True)
            self.assertIn("Forkert brugernavn eller adgangskode", r2.get_data(as_text=True))

    def test_account_locks_after_repeated_failures_even_with_right_password(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder())
        with p:
            c = app.test_client()
            for _ in range(self.lg.LOGIN_MAX_FAILURES):
                c.post("/login", data={"username": "anna", "password": "bad"})
            r = c.post("/login", data={"username": "anna", "password": "Correct-Horse-9"}, follow_redirects=True)
            self.assertIn("midlertidigt låst", r.get_data(as_text=True))
            with c.session_transaction() as s:
                self.assertNotIn("user", s)

    def test_success_clears_counter_and_logs_in(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder())
        with p:
            c = app.test_client()
            for _ in range(2):
                c.post("/login", data={"username": "anna", "password": "bad"})
            r = c.post("/login", data={"username": "anna", "password": "Correct-Horse-9"})
            self.assertEqual(r.status_code, 302)
            with c.session_transaction() as s:
                self.assertEqual(s.get("user"), "anna")
            ok, _ = self.lg.check("anna", "1.2.3.4")
            self.assertTrue(ok)

    def test_plaintext_passwords_no_longer_authenticate(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder(stored="hunter2-plain"))
        with p:
            c = app.test_client()
            c.post("/login", data={"username": "anna", "password": "hunter2-plain"})
            with c.session_transaction() as s:
                self.assertNotIn("user", s)

    def test_rehash_migration_converts_plaintext_in_place(self):
        from auth import rehash_plaintext_passwords
        rows = [{"id": 1, "password": "abc"}, {"id": 2, "password": "def"}]
        log = []

        class Cur:
            def execute(self, sql, params=None):
                log.append((" ".join(sql.split()), params))

            def fetchall(self):
                return rows

            def close(self):
                pass

        class Conn:
            def cursor(self, *a, **k):
                return Cur()

            def commit(self):
                log.append(("COMMIT", None))

        n = rehash_plaintext_passwords(Conn())
        self.assertEqual(n, 2)
        updates = [l for l in log if l[0].startswith("UPDATE users")]
        self.assertEqual(len(updates), 2)
        self.assertTrue(all(u[1][0].startswith(("scrypt:", "pbkdf2:")) for u in updates))

    def test_rehash_never_touches_other_hash_schemes_or_the_erased_marker(self):
        from auth import rehash_plaintext_passwords
        rows = [{"id": 1, "password": "plain-secret"},
                {"id": 2, "password": "$2b$12$abcdefghijklmnopqrstuuVwXyZ0123456789abcdefghijklmnopq"},
                {"id": 3, "password": "!erased"},
                {"id": 4, "password": "sha256$salt$" + "a" * 64},
                {"id": 5, "password": "$argon2id$v=19$m=65536,t=3,p=4$abc$def"}]
        updated = []

        class Cur:
            def execute(self, sql, params=None):
                if sql.startswith("UPDATE"):
                    updated.append(params[1])

            def fetchall(self):
                return rows

            def close(self):
                pass

        class Conn:
            def cursor(self, *a, **k):
                return Cur()

            def commit(self):
                pass

        self.assertEqual(rehash_plaintext_passwords(Conn()), 1)
        self.assertEqual(updated, [1])

    def test_register_enforces_password_policy_and_is_danish(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            c = app.test_client()
            r = c.post("/register", data={"username": "newbie", "email": "n@x.dk", "password": "short"},
                       follow_redirects=True)
            self.assertIn("mindst 10 tegn", r.get_data(as_text=True))
            self.assertEqual(fake.queries("insert into users"), [])
            r = c.post("/register", data={"username": "newbie", "email": "n@x.dk",
                                          "password": "a-long-unique-pass-42"})
            self.assertEqual(fake.queries("insert into users").__len__(), 1)
            stored = fake.queries("insert into users")[0][1][1]
            self.assertTrue(stored.startswith(("scrypt:", "pbkdf2:")))

    def test_password_policy_unit(self):
        from password_policy import validate_password
        self.assertTrue(validate_password("short"))
        self.assertTrue(validate_password("password123"))
        self.assertTrue(validate_password("1234567890"))
        self.assertTrue(validate_password("aaaaaaaaaaaa"))
        self.assertTrue(validate_password("annasmith-2024", username="annasmith"))
        self.assertTrue(validate_password("x" * 200))
        self.assertEqual(validate_password("A-long-unique-pass-42", "anna", "anna@x.dk"), [])

    def test_settings_password_change_uses_policy(self):
        src = open(secapp._REPO_ROOT + "/pages.py", encoding="utf-8").read()
        self.assertIn("validate_password", src)
        self.assertNotIn("stored_pw == current_password", src)

    def test_lockout_unit_window_and_expiry(self):
        lg = self.lg
        for i in range(lg.LOGIN_MAX_FAILURES):
            lg.record_failure("bob", "9.9.9.9", now=1000 + i)
        ok, retry = lg.check("bob", "9.9.9.9", now=1010)
        self.assertFalse(ok)
        self.assertGreater(retry, 0)
        ok, _ = lg.check("bob", "9.9.9.9", now=1010 + lg.LOGIN_LOCKOUT_SECONDS + 5)
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# S-2.3 role matrix
# ---------------------------------------------------------------------------
class S23_RoleMatrix(unittest.TestCase):
    """Every role against the sensitive route families."""

    # (method, path) -> roles that MAY reach the handler (others must be denied)
    EXPECT = {
        ("GET", "/hr/approvals"): {"dept_head", "hr_manager", "company_admin", "admin_acting"},
        ("GET", "/hr/budgets"): {"dept_head", "hr_manager", "company_admin", "admin_acting"},
        ("POST", "/hr/budgets/save"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/hr/roi"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/hr/learning-analytics"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/hr/employee-progress"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/hr/reports"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/hr/export/employees"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/hr/billing"): {"hr_manager", "company_admin", "admin_acting"},
        ("POST", "/hr/order/o1/billing"): {"hr_manager", "company_admin", "admin_acting"},
        ("POST", "/hr/employee/9/toggle-status"): {"hr_manager", "company_admin", "admin_acting"},
        ("POST", "/hr/employee/9/reset-password"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/hr/suppliers"): {"dept_head", "hr_manager", "company_admin", "admin_acting"},
        ("POST", "/hr/suppliers/toggle"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/companies/employees"): {"hr_manager", "company_admin", "admin_acting"},
        ("GET", "/multitenant-reports/"): {"hr_manager", "company_admin", "admin", "admin_acting"},
        ("GET", "/multitenant-reports/analytics/export"): {"hr_manager", "company_admin", "admin", "admin_acting"},
        ("GET", "/admin/"): {"admin", "admin_acting"},
        ("GET", "/admin/users"): {"admin", "admin_acting"},
        ("POST", "/admin/make-superadmin/x"): {"admin", "admin_acting"},
        ("GET", "/app1/adminlog"): {"admin", "admin_acting"},
        ("GET", "/admin/gdpr"): {"admin", "admin_acting"},
    }

    def _reaches_handler(self, app, role, method, path):
        """True when the request got past the guard (handler ran: any status that
        is not a 401/403/login-redirect produced BY the guard)."""
        fake, p = patch_mysql(app)
        with p:
            c = client_as(app, role)
            resp = c.open(path, method=method, data={"x": "1"})
        if resp.status_code in (401, 403):
            return False
        if resp.status_code in (301, 302, 303, 307) and not fake.log:
            # A guard denies BEFORE touching the database; a handler that ran
            # always issues at least its company lookup.
            return False
        return True

    def test_matrix(self):
        app = get_app()
        failures = []
        ROLES_ = dict(ROLES)
        ROLES_["admin_acting"] = {"user": "root", "user_id": 1, "role": "admin",
                                  "admin_acting_company_id": 7, "company_id": 7,
                                  "company_role": "company_admin"}
        ROLES.update(ROLES_)
        try:
            self._run_matrix(app, failures)
        finally:
            ROLES.pop("admin_acting", None)
        self.assertEqual(failures, [], "\n".join(failures))

    def _run_matrix(self, app, failures):
        for (method, path), allowed in self.EXPECT.items():
            for role in list(ROLES):
                got = self._reaches_handler(app, role, method, path)
                want = role in allowed
                if got != want:
                    failures.append("%s %s as %s: handler reached=%s expected=%s" % (method, path, role, got, want))

    def test_capabilities_are_consistent_with_the_ladder(self):
        import auth_decorators as ad
        def caps(role):
            sess = dict(ROLES[role]) if ROLES[role] else {}
            return {c for c in ad.CAPABILITY_MIN_ROLE if ad.can(c, sess)}
        self.assertEqual(caps("anon"), set())
        self.assertEqual(caps("employee"), set())
        self.assertLess(caps("dept_head"), caps("hr_manager"))
        self.assertLess(caps("hr_manager"), caps("company_admin"))
        self.assertEqual(caps("admin"), set(ad.CAPABILITY_MIN_ROLE))
        self.assertIn("company.integrations", caps("company_admin"))
        self.assertNotIn("company.integrations", caps("hr_manager"))
        self.assertNotIn("hr.analytics", caps("dept_head"))
        self.assertIn("hr.budget.view", caps("dept_head"))
        self.assertNotIn("hr.budget.edit", caps("dept_head"))

    def test_stored_permissions_override_only_real_deviations(self):
        import auth_decorators as ad
        sess = dict(ROLES["hr_manager"])
        # seeded default (manage_billing False for hr_manager) must NOT revoke billing
        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "hr_manager", "department": None,
                "permissions": '{"manage_billing": false, "view_analytics": true}'}):
            ad.invalidate_session_cache()
            self.assertTrue(ad.can("hr.billing", sess))
            self.assertTrue(ad.can("hr.analytics", sess))
        # an explicit restriction (view_analytics False, seeded True) does apply
        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "hr_manager", "department": None,
                "permissions": '{"view_analytics": false}'}):
            ad.invalidate_session_cache()
            self.assertFalse(ad.can("hr.analytics", sess))
        # a grant lifts a department head, but never to company-admin powers
        dh = dict(ROLES["dept_head"])
        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "department_head", "department": "Salg",
                "permissions": '{"manage_users": true, "manage_integrations": true}'}):
            ad.invalidate_session_cache()
            self.assertTrue(ad.can("hr.employees.manage", dh))
            self.assertFalse(ad.can("company.integrations", dh))
        ad.invalidate_session_cache()

    def test_demotion_takes_effect_without_relogin(self):
        import auth_decorators as ad
        app = get_app()
        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "employee", "department": None, "permissions": None}):
            ad.invalidate_session_cache()
            c = client_as(app, "hr_manager")
            fake, p = patch_mysql(app)
            with p:
                resp = c.get("/hr/approvals")
            self.assertTrue(_denied(resp))
            with c.session_transaction() as s:
                self.assertEqual(s["company_role"], "employee")
        ad.invalidate_session_cache()

    def test_department_head_scope(self):
        import auth_decorators as ad
        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "department_head", "department": "Salg", "permissions": None}):
            ad.invalidate_session_cache()
            self.assertEqual(ad.department_scope(dict(ROLES["dept_head"])), "Salg")
            self.assertIsNone(ad.department_scope(dict(ROLES["admin"])))
        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "hr_manager", "department": "Salg", "permissions": None}):
            ad.invalidate_session_cache()
            self.assertIsNone(ad.department_scope(dict(ROLES["hr_manager"])))
        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "department_head", "department": None, "permissions": None}):
            ad.invalidate_session_cache()
            self.assertEqual(ad.department_scope(dict(ROLES["dept_head"])), "")  # fail closed
        ad.invalidate_session_cache()

    def test_dept_head_budget_view_is_limited_to_own_department(self):
        import auth_decorators as ad
        app = get_app()

        def responder(sql, params):
            s = " ".join(sql.split()).lower()
            if "from companies c" in s and "company_users cu" in s:
                return {"id": 7, "company_name": "Acme", "user_role": "department_head",
                        "department": "Salg", "permissions": None}
            return None

        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "department_head", "department": "Salg", "permissions": None}):
            ad.invalidate_session_cache()
            fake, p = patch_mysql(app, responder)
            with p:
                client_as(app, "dept_head").get("/hr/budgets")
            q = fake.queries("from department_budgets db")
            self.assertEqual(len(q), 1)
            self.assertEqual(q[0][1][2], "Salg")
        ad.invalidate_session_cache()

    def test_dept_head_hr_landing_redirects_to_own_department(self):
        import auth_decorators as ad
        app = get_app()
        with mock.patch.object(ad, "_lookup_membership_status", return_value={
                "status": "active", "role": "department_head", "department": "Salg", "permissions": None}):
            ad.invalidate_session_cache()

            def responder(sql, params):
                s = " ".join(sql.split()).lower()
                if "from companies c" in s:
                    return {"id": 7, "company_name": "Acme", "user_role": "department_head",
                            "department": "Salg", "permissions": None}
                return None

            fake, p = patch_mysql(app, responder)
            with p:
                resp = client_as(app, "dept_head").get("/hr/")
            self.assertEqual(resp.status_code, 302)
            self.assertTrue(resp.headers["Location"].endswith("/hr/my-department"))
        ad.invalidate_session_cache()

    def test_hr_agent_audience_uses_real_role_names(self):
        import hr_tools
        captured = {}

        class Cur:
            def execute(self, sql, params):
                captured["sql"] = sql

            def fetchall(self):
                return []

        hr_tools._resolve_company_recipients(Cur(), 7, audience="managers")
        for role in ("company_admin", "hr_manager", "department_head"):
            self.assertIn(role, captured["sql"])
        self.assertNotIn("'hr'", captured["sql"])
        self.assertNotIn("'manager'", captured["sql"])


# ---------------------------------------------------------------------------
# S-2.4 reset / invite tokens
# ---------------------------------------------------------------------------
class FakeTokenDb:
    """Just enough of MySQL for password_tokens: an in-memory token table."""

    def __init__(self):
        self.tokens = {}   # hash -> dict
        self.now = 1000.0

    def cursor(self, *a, **k):
        return _TokCur(self)

    def commit(self):
        pass


class _TokCur:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._row = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        db = self.db
        self.rowcount = 0
        if s.startswith("create table"):
            return
        if s.startswith("update password_reset_tokens set used_at = now() where account_type"):
            for t in db.tokens.values():
                if t["type"] == params[0] and t["id"] == params[1] and t["used"] is None:
                    t["used"] = db.now
                    self.rowcount += 1
        elif s.startswith("insert into password_reset_tokens"):
            h, typ, aid, purpose, ttl = params
            db.tokens[h] = {"type": typ, "id": aid, "purpose": purpose, "used": None,
                            "expires": db.now + ttl * 60}
        elif s.startswith("select account_type"):
            t = db.tokens.get(params[0])
            ok = t and t["used"] is None and t["expires"] >= db.now
            self._row = {"account_type": t["type"], "account_id": t["id"], "purpose": t["purpose"]} if ok else None
        elif s.startswith("update password_reset_tokens set used_at = now() where token_hash"):
            t = db.tokens.get(params[0])
            if t and t["used"] is None and t["expires"] >= db.now:
                t["used"] = db.now
                self.rowcount = 1

    def fetchone(self):
        return self._row

    def close(self):
        pass


class S24_PasswordTokens(unittest.TestCase):
    def setUp(self):
        import password_tokens
        password_tokens._TABLE_READY = False
        self.pt = password_tokens
        self.db = FakeTokenDb()

    def test_token_is_single_use_and_hashed_at_rest(self):
        raw = self.pt.issue_token(self.db, "user", 5)
        self.assertNotIn(raw, self.db.tokens)           # only the hash is stored
        self.assertIn(self.pt.hash_token(raw), self.db.tokens)
        self.assertIsNotNone(self.pt.lookup(self.db, raw, "user"))
        self.assertTrue(self.pt.consume(self.db, raw))
        self.assertFalse(self.pt.consume(self.db, raw))  # second use fails
        self.assertIsNone(self.pt.lookup(self.db, raw))

    def test_token_expires(self):
        raw = self.pt.issue_token(self.db, "user", 5)
        self.db.now += 61 * 60
        self.assertIsNone(self.pt.lookup(self.db, raw))
        self.assertFalse(self.pt.consume(self.db, raw))

    def test_invite_lives_longer_than_reset(self):
        r = self.pt.issue_token(self.db, "user", 1, purpose="reset")
        i = self.pt.issue_token(self.db, "user", 2, purpose="invite")
        self.db.now += 3 * 24 * 3600
        self.assertIsNone(self.pt.lookup(self.db, r))
        self.assertIsNotNone(self.pt.lookup(self.db, i))

    def test_new_token_voids_older_ones(self):
        a = self.pt.issue_token(self.db, "user", 5)
        b = self.pt.issue_token(self.db, "user", 5)
        self.assertIsNone(self.pt.lookup(self.db, a))
        self.assertIsNotNone(self.pt.lookup(self.db, b))

    def test_user_token_cannot_reset_a_vendor(self):
        raw = self.pt.issue_token(self.db, "user", 5)
        self.assertIsNone(self.pt.lookup(self.db, raw, account_type="vendor"))

    def test_junk_tokens_rejected(self):
        for junk in ("", None, "x" * 500, "../../etc/passwd"):
            self.assertIsNone(self.pt.lookup(self.db, junk))
            self.assertFalse(self.pt.consume(self.db, junk))


class S24_ResetRoutes(unittest.TestCase):
    def setUp(self):
        import rate_limit, login_guard
        rate_limit.reset()
        login_guard.reset()

    def test_forgot_password_is_identical_for_known_and_unknown_accounts(self):
        app = get_app()

        def known(sql, params):
            if "from users where username" in " ".join(sql.split()).lower():
                return {"id": 5, "username": "anna", "email": "anna@x.dk", "password": HASHED}
            return None

        texts = []
        for responder in (known, None):
            fake, p = patch_mysql(app, responder)
            with p, mock.patch("auth.send_user_password_link", return_value=True) as sender:
                c = app.test_client()
                r = c.post("/forgot-password", data={"identifier": "anna"}, follow_redirects=True)
                texts.append(re.sub(r"\s+", " ", re.search(r'class="vp-flash [^"]*">([^<]+)<', r.get_data(as_text=True)).group(1)))
                if responder:
                    sender.assert_called_once()
                else:
                    sender.assert_not_called()
        self.assertEqual(texts[0], texts[1])
        self.assertIn("Hvis der findes en konto", texts[0])

    def test_forgot_password_is_rate_limited(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("auth.send_user_password_link", return_value=True) as sender:
            c = app.test_client()
            for _ in range(6):
                c.post("/forgot-password", data={"identifier": "anna"})
        self.assertLessEqual(sender.call_count, 3)

    def test_reset_with_weak_password_keeps_token_and_does_not_update(self):
        app = get_app()
        fake, p = patch_mysql(app, lambda sql, params: (
            {"id": 5, "username": "anna", "email": "anna@x.dk"} if "from users where id" in " ".join(sql.split()).lower() else None))
        with p, mock.patch("password_tokens.lookup", return_value={"account_type": "user", "account_id": 5, "purpose": "reset"}), \
                mock.patch("password_tokens.consume", return_value=True) as consume:
            r = app.test_client().post("/reset-password/tok", data={"password": "short", "confirm": "short"})
            self.assertIn("mindst 10 tegn", r.get_data(as_text=True))
            consume.assert_not_called()
            self.assertEqual(fake.queries("update users set password"), [])

    def test_reset_happy_path_hashes_and_burns_token(self):
        app = get_app()
        fake, p = patch_mysql(app, lambda sql, params: (
            {"id": 5, "username": "anna", "email": "anna@x.dk"} if "from users where id" in " ".join(sql.split()).lower() else None))
        with p, mock.patch("password_tokens.lookup", return_value={"account_type": "user", "account_id": 5, "purpose": "reset"}), \
                mock.patch("password_tokens.consume", return_value=True) as consume:
            r = app.test_client().post("/reset-password/tok",
                                       data={"password": "a-long-unique-pass-42", "confirm": "a-long-unique-pass-42"})
            self.assertEqual(r.status_code, 302)
            consume.assert_called_once()
            upd = fake.queries("update users set password")
            self.assertEqual(len(upd), 1)
            self.assertTrue(upd[0][1][0].startswith(("scrypt:", "pbkdf2:")))

    def test_reused_or_expired_token_shows_danish_error_and_changes_nothing(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("password_tokens.lookup", return_value=None):
            r = app.test_client().get("/reset-password/dead")
            self.assertIn("ugyldigt eller udløbet", r.get_data(as_text=True))
            r = app.test_client().post("/reset-password/dead",
                                       data={"password": "a-long-unique-pass-42", "confirm": "a-long-unique-pass-42"})
            self.assertEqual(fake.queries("update users"), [])

    def test_hr_reset_sends_a_link_and_never_shows_a_password(self):
        app = get_app()

        def responder(sql, params):
            s = " ".join(sql.split()).lower()
            if "from companies c" in s:
                return {"id": 7, "company_name": "Acme", "user_role": "hr_manager", "department": None, "permissions": None}
            if "from company_users cu" in s and "join users u" in s:
                return {"user_id": 9, "username": "emma", "email": "emma@x.dk"}
            return None

        fake, p = patch_mysql(app, responder)
        with p, mock.patch("auth.send_user_password_link", return_value=True) as sender:
            c = client_as(app, "hr_manager")
            r = c.post("/hr/employee/9/reset-password", follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            sender.assert_called_once()
            self.assertEqual(sender.call_args[0][2], "reset")
            self.assertEqual(fake.queries("update users set password"), [])
            with c.session_transaction() as s:
                flashes = " ".join(m for _, m in s.get("_flashes", []))
            self.assertIn("emma@x.dk", flashes)
            self.assertNotRegex(flashes, r"Nyt password")

    def test_vendor_reset_routes_exist_and_are_generic(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            c = app.test_client()
            r = c.post("/vendor/forgot-password", data={"email": "nobody@x.dk"}, follow_redirects=True)
            self.assertIn("Hvis der findes en leverandørkonto", r.get_data(as_text=True))
            r = c.get("/vendor/reset-password/bad")
            self.assertIn("ugyldigt eller udløbet", r.get_data(as_text=True))

    def test_login_page_links_to_forgot_password(self):
        app = get_app()
        html = app.test_client().get("/login").get_data(as_text=True)
        self.assertIn("/forgot-password", html)
        self.assertNotIn('class="forgot" href="#"', html)


# ---------------------------------------------------------------------------
# S-2.5 impersonation
# ---------------------------------------------------------------------------
class S25_Impersonation(unittest.TestCase):
    def _company(self, sql, params):
        if "from companies where id" in " ".join(sql.split()).lower():
            return {"id": 7, "company_name": "Acme"}
        return None

    def test_act_as_is_post_only_and_admin_only(self):
        app = get_app()
        fake, p = patch_mysql(app, self._company)
        with p:
            self.assertEqual(client_as(app, "admin").get("/admin/impersonate/7").status_code, 405)
            self.assertTrue(_denied(client_as(app, "hr_manager").post("/admin/impersonate/7")))
            self.assertTrue(_denied(client_as(app, "anon").post("/admin/impersonate/7")))

    def test_begin_banner_audit_and_exit_restores_admin_context(self):
        app = get_app()
        fake, p = patch_mysql(app, self._company)
        with p:
            c = app.test_client()
            login(c, user="root", user_id=1, role="admin", company_id=3, company_role="company_admin",
                  company_name="Mine")
            r = c.post("/admin/impersonate/7")
            self.assertEqual(r.status_code, 302)
            with c.session_transaction() as s:
                self.assertEqual(s["admin_acting_company_id"], 7)
                self.assertEqual(s["company_id"], 7)
                self.assertGreater(s["admin_acting_until"], time.time())
                self.assertEqual(s["_imp_prev_company_id"], 3)
            self.assertTrue(any("impersonate.start" in str(q[1]) for q in fake.queries("insert into audit_log")))
            c.get("/admin/impersonate/exit")
            with c.session_transaction() as s:
                self.assertNotIn("admin_acting_company_id", s)
                self.assertEqual(s["company_id"], 3)
                self.assertEqual(s["company_name"], "Mine")
            self.assertTrue(any("impersonate.stop" in str(q[1]) for q in fake.queries("insert into audit_log")))

    def test_company_detail_view_does_not_silently_switch_context(self):
        app = get_app()

        def responder(sql, params):
            s = " ".join(sql.split()).lower()
            if "from companies c" in s:
                return {"id": 7, "company_name": "Acme", "features": "{}"}
            return None

        fake, p = patch_mysql(app, responder)
        with p:
            c = app.test_client()
            login(c, user="root", user_id=1, role="admin")
            c.get("/companies/admin/7")
            with c.session_transaction() as s:
                self.assertNotIn("admin_acting_company_id", s)
                self.assertNotIn("company_id", s)

    def test_exit_does_not_reenter_acting_state(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            c = app.test_client()
            login(c, user="root", user_id=1, role="admin", admin_acting_company_id=7, company_id=7)
            r = c.get("/admin/impersonate/exit")
            self.assertNotIn("/companies/admin/7", r.headers["Location"])

    def test_acting_session_expires(self):
        import impersonation
        sess = {"admin_acting_company_id": 7, "admin_acting_until": 100, "company_id": 7,
                "_imp_prev_company_id": 3, "_imp_prev_company_role": "x", "_imp_prev_company_name": "Mine"}
        self.assertIsNone(impersonation.expire_if_due(dict(sess), now=50))
        ended = impersonation.expire_if_due(sess, now=200)
        self.assertEqual(ended, 7)
        self.assertNotIn("admin_acting_company_id", sess)
        self.assertEqual(sess["company_id"], 3)

    def test_expiry_enforced_by_request_gate(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            c = app.test_client()
            login(c, user="root", user_id=1, role="admin", admin_acting_company_id=7, company_id=7,
                  admin_acting_until=int(time.time()) - 5, _imp_prev_company_id=None)
            c.get("/api/search?q=zz")
            with c.session_transaction() as s:
                self.assertNotIn("admin_acting_company_id", s)


# ---------------------------------------------------------------------------
# S-2.6 two-factor
# ---------------------------------------------------------------------------
class S26_TwoFactor(unittest.TestCase):
    def test_rfc6238_reference_vectors(self):
        import two_factor as tf
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"   # ASCII "12345678901234567890"
        # RFC 6238 appendix B, SHA-1, last 6 digits of the 8-digit values
        self.assertEqual(tf.totp_now(secret, now=59), "287082")
        self.assertEqual(tf.totp_now(secret, now=1111111109), "081804")
        self.assertEqual(tf.totp_now(secret, now=1234567890), "005924")

    def test_verify_window_and_replay(self):
        import two_factor as tf
        secret = tf.new_secret()
        now = 1_700_000_000
        code = tf.totp_now(secret, now=now)
        step = tf.verify_code(secret, code, now=now)
        self.assertIsNotNone(step)
        self.assertIsNotNone(tf.verify_code(secret, code, now=now + 30))      # 1 step drift ok
        self.assertIsNone(tf.verify_code(secret, code, now=now + 120))        # too old
        self.assertIsNone(tf.verify_code(secret, code, now=now, last_step=step))  # replay
        self.assertIsNone(tf.verify_code(secret, "000000" if code != "000000" else "111111", now=now))
        self.assertIsNone(tf.verify_code(secret, "abc", now=now))

    def test_backup_codes_are_hashed_and_formatted(self):
        import two_factor as tf
        plain, hashed = tf.new_backup_codes()
        self.assertEqual(len(set(plain)), tf.BACKUP_CODE_COUNT)
        self.assertTrue(all(len(h) == 64 for h in hashed))
        self.assertNotIn(plain[0], hashed)

    def test_secret_encrypted_at_rest(self):
        import two_factor as tf
        s = tf.new_secret()
        blob = tf.encrypt_secret(s)
        self.assertNotIn(s, blob)
        self.assertEqual(tf.decrypt_secret(blob), s)

    def test_provisioning_uri(self):
        import two_factor as tf
        uri = tf.provisioning_uri("ABCDEF", "anna@x.dk")
        self.assertTrue(uri.startswith("otpauth://totp/Futurematch%3Aanna%40x.dk?secret=ABCDEF"))

    def test_policy_enforced_for_platform_admins_only_by_default(self):
        import two_factor as tf
        self.assertTrue(tf.enrollment_required("admin"))
        self.assertFalse(tf.enrollment_required("user", "company_admin"))
        with mock.patch.dict("os.environ", {"TWOFA_ENFORCE_COMPANY_ADMIN": "1"}):
            self.assertTrue(tf.enrollment_required("user", "company_admin"))
            self.assertFalse(tf.enrollment_required("user", "hr_manager"))
        with mock.patch.dict("os.environ", {"TWOFA_ENFORCE_ADMIN": "0"}):
            self.assertFalse(tf.enrollment_required("admin"))

    def _admin_responder(self):
        def r(sql, params):
            s = " ".join(sql.split()).lower()
            if "from users where username" in s:
                return {"id": 1, "username": "root", "password": HASHED, "credits": 1, "role": "admin", "email": "r@x.dk"}
            if "from users where id" in s:
                return {"id": 1, "username": "root", "password": HASHED, "credits": 1, "role": "admin", "email": "r@x.dk"}
            return None
        return r

    def test_admin_without_2fa_is_forced_to_enroll(self):
        import login_guard
        login_guard.reset()
        app = get_app()
        fake, p = patch_mysql(app, self._admin_responder())
        with p, mock.patch("two_factor.is_enabled", return_value=False):
            c = app.test_client()
            r = c.post("/login", data={"username": "root", "password": "Correct-Horse-9"})
            self.assertTrue(r.headers["Location"].endswith("/account/2fa"))
            with c.session_transaction() as s:
                self.assertTrue(s["twofa_must_enroll"])
            # everything else bounces to the enrolment page
            r = c.get("/admin/")
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/account/2fa"))
            r = c.post("/admin/make-superadmin/victim")
            self.assertTrue(r.headers["Location"].endswith("/account/2fa"))
            self.assertEqual(fake.queries("update users"), [])

    def test_user_with_2fa_must_pass_the_challenge(self):
        import login_guard
        login_guard.reset()
        app = get_app()
        fake, p = patch_mysql(app, self._admin_responder())
        with p, mock.patch("two_factor.is_enabled", return_value=True), \
                mock.patch("two_factor.verify_login", return_value=False):
            c = app.test_client()
            r = c.post("/login", data={"username": "root", "password": "Correct-Horse-9"})
            self.assertTrue(r.headers["Location"].endswith("/login/2fa"))
            with c.session_transaction() as s:
                self.assertNotIn("user", s)             # password alone is not a session
            r = c.get("/admin/")
            self.assertTrue(_denied(r))
            r = c.post("/login/2fa", data={"code": "000000"})
            with c.session_transaction() as s:
                self.assertNotIn("user", s)

    def test_correct_code_completes_login(self):
        import login_guard
        login_guard.reset()
        app = get_app()
        fake, p = patch_mysql(app, self._admin_responder())
        with p, mock.patch("two_factor.is_enabled", return_value=True), \
                mock.patch("two_factor.verify_login", return_value=True):
            c = app.test_client()
            c.post("/login", data={"username": "root", "password": "Correct-Horse-9"})
            r = c.post("/login/2fa", data={"code": "123456"})
            self.assertEqual(r.status_code, 302)
            with c.session_transaction() as s:
                self.assertEqual(s["user"], "root")
                self.assertTrue(s["twofa_ok"])
                self.assertNotIn("twofa_pending", s)

    def test_challenge_without_pending_login_is_refused_and_bruteforce_locks(self):
        import login_guard
        login_guard.reset()
        app = get_app()
        c = app.test_client()
        r = c.get("/login/2fa")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        fake, p = patch_mysql(app, self._admin_responder())
        with p, mock.patch("two_factor.is_enabled", return_value=True), \
                mock.patch("two_factor.verify_login", return_value=False) as ver:
            c.post("/login", data={"username": "root", "password": "Correct-Horse-9"})
            for _ in range(login_guard.LOGIN_MAX_FAILURES + 3):
                c.post("/login/2fa", data={"code": "000000"})
            self.assertEqual(ver.call_count, login_guard.LOGIN_MAX_FAILURES)   # then locked out

    def test_enrolment_confirmation_clears_the_gate(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("two_factor.confirm_enrollment", return_value=["aaaaa-bbbbb"] * 8), \
                mock.patch("two_factor.is_enabled", return_value=True), \
                mock.patch("two_factor.backup_codes_left", return_value=8):
            c = app.test_client()
            login(c, user="root", user_id=1, role="admin", twofa_must_enroll=True, twofa_ok=False)
            r = c.post("/account/2fa", data={"action": "confirm", "code": "123456"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("aaaaa-bbbbb", r.get_data(as_text=True))
            with c.session_transaction() as s:
                self.assertNotIn("twofa_must_enroll", s)

    def test_required_2fa_cannot_be_disabled(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("two_factor.is_enabled", return_value=True), \
                mock.patch("two_factor.disable") as disable, \
                mock.patch("two_factor.backup_codes_left", return_value=3):
            c = app.test_client()
            login(c, user="root", user_id=1, role="admin", twofa_ok=True)
            c.post("/account/2fa", data={"action": "disable", "password": "x", "code": "1"})
            disable.assert_not_called()

    def test_legacy_admin_session_must_relogin(self):
        app = get_app()
        app.config["TESTING"] = False
        try:
            fake, p = patch_mysql(app)
            with p:
                c = app.test_client()
                login(c, user="root", user_id=1, role="admin")           # no twofa_ok marker
                r = c.get("/admin/")
                self.assertEqual(r.status_code, 302)
                self.assertIn("/login", r.headers["Location"])
                with c.session_transaction() as s:
                    self.assertNotIn("user", s)
        finally:
            app.config["TESTING"] = True

    def test_settings_links_to_2fa(self):
        src = open(secapp._REPO_ROOT + "/templates/fm/settings.html", encoding="utf-8").read()
        self.assertIn("auth.account_2fa", src)


# ---------------------------------------------------------------------------
# S-2.7 vendor auth
# ---------------------------------------------------------------------------
class S27_VendorAuth(unittest.TestCase):
    def setUp(self):
        import vendor_auth
        self.va = vendor_auth
        vendor_auth.invalidate_vendor_cache()

    tearDown = setUp

    def test_suspended_vendor_is_kicked_out_of_a_live_session(self):
        app = get_app()
        with mock.patch.object(self.va, "_lookup_vendor_status", return_value="suspended"):
            c = app.test_client()
            login(c, user_type="vendor", vendor_id=3, vendor_name="Acme Kurser")
            r = c.get("/vendor/")
            self.assertEqual(r.status_code, 302)
            self.assertIn("/vendor/login", r.headers["Location"])
            with c.session_transaction() as s:
                self.assertNotIn("vendor_id", s)

    def test_suspended_vendor_gets_json_401_on_the_assistant(self):
        app = get_app()
        with mock.patch.object(self.va, "_lookup_vendor_status", return_value="suspended"):
            c = app.test_client()
            login(c, user_type="vendor", vendor_id=3, vendor_name="Acme")
            r = c.post("/vendor/ask", json={"message": "hej"})
            self.assertEqual(r.status_code, 401)

    def test_status_change_invalidates_cache_immediately(self):
        with mock.patch.object(self.va, "_lookup_vendor_status", return_value="active"):
            self.assertTrue(self.va.vendor_session_is_live(3))
        self.va.invalidate_vendor_cache(3)
        with mock.patch.object(self.va, "_lookup_vendor_status", return_value="suspended"):
            self.assertFalse(self.va.vendor_session_is_live(3))

    def test_outage_fails_open_but_missing_row_fails_closed(self):
        with mock.patch.object(self.va, "_lookup_vendor_status", return_value=False):
            self.assertTrue(self.va.vendor_session_is_live(4))
        self.va.invalidate_vendor_cache()
        with mock.patch.object(self.va, "_lookup_vendor_status", return_value=None):
            self.assertFalse(self.va.vendor_session_is_live(4))

    def test_admin_status_route_invalidates_and_audits(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch.object(self.va, "invalidate_vendor_cache") as inv:
            client_as(app, "admin").post("/admin/vendors/3/status", data={"status": "suspended"})
            inv.assert_called_once_with(3)
            self.assertTrue(fake.queries("insert into audit_log"))

    CSV = ("title;handle;vendor;price\n"
           "Mit kursus;mit-kursus;NogetAndet A/S;1000\n"
           "Stjålet;shopify-handle;NogetAndet A/S;1\n")

    def _storage(self):
        class F:
            def __init__(s, text):
                s._b = text.encode("utf-8")

            def read(s):
                return s._b
        return F(self.CSV)

    def test_vendor_comes_from_the_session_not_the_csv(self):
        import catalog_service as cs
        with mock.patch.object(cs, "_existing_vendor_by_handle", return_value={}), \
                mock.patch.object(cs, "get_products", return_value=[]):
            parsed = cs.parse_catalog_csv(self._storage(), force_vendor="Acme Kurser")
        self.assertTrue(parsed["products"])
        self.assertTrue(all(p["vendor"] == "Acme Kurser" for p in parsed["products"]))

    def test_handle_owned_by_another_vendor_is_blocked_at_parse(self):
        import catalog_service as cs
        with mock.patch.object(cs, "_existing_vendor_by_handle", return_value={"shopify-handle": "Kursuszonen"}), \
                mock.patch.object(cs, "get_products", return_value=[]):
            parsed = cs.parse_catalog_csv(self._storage(), force_vendor="Acme Kurser")
        handles = [p["handle"] for p in parsed["products"]]
        self.assertEqual(handles, ["mit-kursus"])
        self.assertTrue(any("tilhører en anden leverandør" in i["message"] for i in parsed["issues"]))

    def test_admin_import_still_trusts_the_csv_vendor_column(self):
        import catalog_service as cs
        with mock.patch.object(cs, "get_products", return_value=[]):
            parsed = cs.parse_catalog_csv(self._storage())
        self.assertEqual({p["vendor"] for p in parsed["products"]}, {"NogetAndet A/S"})

    def test_confirm_blocks_takeover_even_if_the_draft_file_was_tampered_with(self):
        import catalog_service as cs
        draft = {
            "job_id": "j1", "uploaded_by": "vendor:3", "forced_vendor": "Acme Kurser",
            "products": [
                {"handle": "mine", "title": "Mit", "vendor": "Kursuszonen"},        # vendor field lies
                {"handle": "theirs", "title": "Deres", "vendor": "Acme Kurser"},    # owned by someone else
            ],
        }
        written = {}
        with mock.patch.object(cs, "get_import_draft", return_value=draft), \
                mock.patch.object(cs, "_read_json", return_value={"products": []}), \
                mock.patch.object(cs, "_write_json", side_effect=lambda path, payload: written.setdefault(str(path), payload)), \
                mock.patch.object(cs, "_existing_vendor_by_handle", return_value={"theirs": "Kursuszonen"}), \
                mock.patch.object(cs, "clear_catalog_cache"):
            out = cs.confirm_import_draft("j1")
        self.assertEqual(out["skipped_handles"], ["theirs"])
        saved = next(v for k, v in written.items() if "products" in v and "updated_at" in v)
        self.assertEqual([p["handle"] for p in saved["products"]], ["mine"])
        self.assertEqual(saved["products"][0]["vendor"], "Acme Kurser")   # pinned to the uploader

    def test_vendor_upload_passes_session_vendor_to_parser(self):
        src = open(secapp._REPO_ROOT + "/vendor_portal.py", encoding="utf-8").read()
        self.assertIn("force_vendor=session.get(\"vendor_name\")", src)


if __name__ == "__main__":
    unittest.main()
