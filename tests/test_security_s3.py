"""
Regression tests for Part A tier S-3 (enterprise identity & integrations).

S-3.1 OAuth2/OIDC, S-3.2 API keys, S-3.3 webhooks (SSRF + signing),
S-3.4 SCIM login identities.
"""

import base64
import hashlib
import http.server
import json
import threading
import time
import unittest
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

import db_compat  # noqa: F401  (installs the MySQLdb shim that enterprise_sso imports)
from tests import secapp
from tests.secapp import FakeMySQL, client_as, get_app, login, patch_mysql

ISSUER = "https://login.example.com/tenant-1/v2.0"
CLIENT_ID = "client-abc"
NONCE = "nonce-123"


def _keypair(kid="k1"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, {"keys": [jwk]}


def _token(key, **over):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "abc", "exp": now + 300, "iat": now - 5,
              "nonce": NONCE, "email": "Anna@Firma.dk", "name": "Anna Hansen"}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": over.get("_kid", "k1")})


# ---------------------------------------------------------------------------
# S-3.1 OIDC
# ---------------------------------------------------------------------------
class S31_IdTokenValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key, cls.jwks = _keypair()
        cls.other_key, _ = _keypair("k1")   # same kid, different key material

    def _validate(self, token, **kw):
        import oidc
        args = dict(jwks=self.jwks, issuer=ISSUER, audience=CLIENT_ID, nonce=NONCE)
        args.update(kw)
        return oidc.validate_id_token(token, **args)

    def test_valid_token_passes_and_yields_identity(self):
        import oidc
        claims = self._validate(_token(self.key))
        ident = oidc.extract_identity(claims)
        self.assertEqual(ident["email"], "anna@firma.dk")
        self.assertEqual(ident["full_name"], "Anna Hansen")
        self.assertEqual(ident["sub"], "abc")

    def _rejects(self, token, **kw):
        import oidc
        with self.assertRaises(oidc.OIDCError):
            self._validate(token, **kw)

    def test_wrong_nonce(self):
        self._rejects(_token(self.key, nonce="someone-elses"))

    def test_missing_nonce_claim(self):
        self._rejects(_token(self.key, nonce=None))

    def test_wrong_audience(self):
        self._rejects(_token(self.key, aud="another-client"))

    def test_wrong_issuer(self):
        self._rejects(_token(self.key, iss="https://evil.example.com/"))

    def test_expired(self):
        self._rejects(_token(self.key, exp=int(time.time()) - 3600, iat=int(time.time()) - 7200))

    def test_issued_in_the_future(self):
        self._rejects(_token(self.key, iat=int(time.time()) + 3600, exp=int(time.time()) + 7200))

    def test_missing_required_claims(self):
        self._rejects(_token(self.key, exp=None))
        self._rejects(_token(self.key, sub=None))

    def test_signature_from_a_different_key_is_rejected(self):
        self._rejects(_token(self.other_key))

    def test_tampered_payload(self):
        h, p, s = _token(self.key).split(".")
        payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        payload["email"] = "ceo@firma.dk"
        forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        self._rejects(".".join([h, forged, s]))

    def test_alg_none_is_refused(self):
        unsigned = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "nonce": NONCE,
                               "exp": int(time.time()) + 99, "iat": int(time.time())}, None, algorithm="none")
        self._rejects(unsigned)

    def test_hmac_algorithm_confusion_is_refused(self):
        # The classic attack signs with HS256 using the (public) key as the secret.
        forged = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "nonce": NONCE,
                             "exp": int(time.time()) + 99, "iat": int(time.time())},
                            "x" * 64, algorithm="HS256", headers={"kid": "k1"})
        self._rejects(forged)

    def test_unknown_kid(self):
        self._rejects(_token(self.key, _kid="nope"))

    def test_multiple_audiences_require_matching_azp(self):
        self._rejects(_token(self.key, aud=[CLIENT_ID, "other"], azp="other"))
        self.assertTrue(self._validate(_token(self.key, aud=[CLIENT_ID, "other"], azp=CLIENT_ID)))

    def test_garbage(self):
        for junk in ("", None, "a.b", "x" * 50, "a.b.c"):
            self._rejects(junk)

    def test_unverified_email_is_refused(self):
        import oidc
        claims = self._validate(_token(self.key, email_verified=False))
        with self.assertRaises(oidc.OIDCError):
            oidc.extract_identity(claims)

    def test_token_without_email_is_refused(self):
        import oidc
        claims = self._validate(_token(self.key, email=None, name="No Mail"))
        with self.assertRaises(oidc.OIDCError):
            oidc.extract_identity(claims)

    def test_entra_style_preferred_username_is_accepted(self):
        import oidc
        claims = self._validate(_token(self.key, email=None, preferred_username="bo@firma.dk"))
        self.assertEqual(oidc.extract_identity(claims)["email"], "bo@firma.dk")


class S31_ProviderFlow(unittest.TestCase):
    CONFIG = {"issuer": ISSUER, "jwks_uri": "https://login.example.com/keys", "client_id": CLIENT_ID,
              "token_url": "https://login.example.com/token", "redirect_uri": "https://app.example.com/cb"}

    @classmethod
    def setUpClass(cls):
        cls.key, cls.jwks = _keypair()

    def _auth(self, token_response, **kw):
        from enterprise_sso import OAuth2Provider
        import oidc
        with mock.patch.object(OAuth2Provider, "exchange_code_for_token", return_value=token_response), \
                mock.patch.object(oidc, "fetch_jwks", return_value=self.jwks):
            return OAuth2Provider().authenticate("code", self.CONFIG, **kw)

    def test_happy_path(self):
        ident = self._auth({"id_token": _token(self.key)}, expected_nonce=NONCE, code_verifier="v")
        self.assertEqual(ident["email"], "anna@firma.dk")

    def test_pure_oauth2_without_id_token_is_refused(self):
        self.assertIsNone(self._auth({"access_token": "abc"}, expected_nonce=NONCE))

    def test_missing_nonce_in_session_is_refused(self):
        self.assertIsNone(self._auth({"id_token": _token(self.key)}, expected_nonce=None))

    def test_replayed_token_with_other_nonce_is_refused(self):
        self.assertIsNone(self._auth({"id_token": _token(self.key)}, expected_nonce="different"))

    def test_config_without_issuer_or_jwks_is_refused(self):
        from enterprise_sso import OAuth2Provider
        import oidc
        with mock.patch.object(OAuth2Provider, "exchange_code_for_token", return_value={"id_token": _token(self.key)}):
            self.assertIsNone(OAuth2Provider().authenticate("c", {"client_id": CLIENT_ID}, expected_nonce=NONCE))

    def test_userinfo_is_not_used_for_identity(self):
        from enterprise_sso import OAuth2Provider
        src = open(secapp._REPO_ROOT + "/enterprise_sso/__init__.py", encoding="utf-8").read()
        self.assertNotIn("def get_user_info", src)

    def test_request_carries_state_nonce_and_pkce(self):
        from enterprise_sso import generate_oauth2_request
        app = get_app()
        cfg = {"config": {"authorization_url": "https://login.example.com/authorize", "client_id": CLIENT_ID,
                          "redirect_uri": "https://app.example.com/cb", "scope": "openid email profile"}}
        with app.test_request_context("/sso/login/acme/oauth2"):
            from flask import session
            url = generate_oauth2_request(cfg)
            from urllib.parse import parse_qs, urlparse
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            self.assertEqual(q["state"], session["oauth2_state"])
            self.assertEqual(q["nonce"], session["oauth2_nonce"])
            self.assertEqual(q["code_challenge_method"], "S256")
            expected = base64.urlsafe_b64encode(hashlib.sha256(session["oauth2_pkce"].encode()).digest()).rstrip(b"=").decode()
            self.assertEqual(q["code_challenge"], expected)

    def test_oauth2_is_live_but_saml_ldap_still_off(self):
        from enterprise_sso import sso_provider_enabled
        self.assertTrue(sso_provider_enabled("oauth2"))
        for p in ("saml", "ldap", "active_directory"):
            self.assertFalse(sso_provider_enabled(p))

    def test_manager_enforces_allowed_domains(self):
        from enterprise_sso import SSOManager
        mgr = SSOManager()
        cfg = {"is_enabled": True, "auto_provision_users": True,
               "config": dict(self.CONFIG, allowed_domains="firma.dk")}
        with mock.patch.object(mgr, "get_company_sso_config", return_value=cfg), \
                mock.patch.object(mgr.providers["oauth2"], "authenticate",
                                  return_value={"email": "eve@evil.com"}), \
                mock.patch.object(mgr, "provision_user") as prov:
            user, err = mgr.authenticate_user(7, "oauth2", "code", context={"expected_nonce": NONCE})
            self.assertIsNone(user)
            prov.assert_not_called()


class _IdCur:
    """Minimal users/company_users emulation for identity + provisioning."""

    def __init__(self, users=None, members=None):
        self.users = users if users is not None else []
        self.members = members if members is not None else []
        self.lastrowid = 0
        self._result = []
        self.executed = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self.executed.append((s, params))
        self._result = []
        if s.startswith("select * from users where lower(email)"):
            self._result = [u for u in self.users if (u.get("email") or "").lower() == params[0]][:2]
        elif s.startswith("select 1 from users where username"):
            self._result = [{"x": 1}] if any(u["username"] == params[0] for u in self.users) else []
        elif s.startswith("insert into users"):
            uid = len(self.users) + 100
            self.users.append({"id": uid, "username": params[0], "email": params[1], "password": params[2],
                               "credits": params[3], "role": "user"})
            self.lastrowid = uid
        elif s.startswith("select * from users where id"):
            self._result = [u for u in self.users if u["id"] == params[0]]
        elif s.startswith("select * from company_users where company_id"):
            self._result = [m for m in self.members if m["company_id"] == params[0] and m["user_id"] == params[1]][:1]
        elif s.startswith("insert into company_users"):
            self.members.append({"company_id": params[0], "user_id": params[1], "role": params[5], "status": params[9]})
            self.lastrowid = 500 + len(self.members)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class S31_Identity(unittest.TestCase):
    def test_new_identity_gets_unusable_hashed_password_and_unique_username(self):
        import identity
        cur = _IdCur(users=[{"id": 1, "username": "anna", "email": "other@x.dk", "role": "user"}])
        user, created = identity.ensure_login_identity(cur, "Anna@Firma.dk")
        self.assertTrue(created)
        self.assertNotEqual(user["username"], "anna")             # collision avoided
        self.assertTrue(user["password"].startswith(("scrypt:", "pbkdf2:")))

    def test_existing_user_is_reused(self):
        import identity
        cur = _IdCur(users=[{"id": 9, "username": "bo", "email": "bo@firma.dk", "role": "user"}])
        user, created = identity.ensure_login_identity(cur, "BO@firma.dk")
        self.assertFalse(created)
        self.assertEqual(user["id"], 9)

    def test_platform_admin_is_never_provisioned_or_matched(self):
        import identity
        cur = _IdCur(users=[{"id": 1, "username": "root", "email": "root@firma.dk", "role": "admin"}])
        with self.assertRaises(identity.IdentityError):
            identity.ensure_login_identity(cur, "root@firma.dk")

    def test_ambiguous_email_is_refused(self):
        import identity
        cur = _IdCur(users=[{"id": 1, "username": "a", "email": "d@x.dk", "role": "user"},
                            {"id": 2, "username": "b", "email": "d@x.dk", "role": "user"}])
        with self.assertRaises(identity.IdentityError):
            identity.ensure_login_identity(cur, "d@x.dk")

    def test_roles_are_clamped_to_non_privileged(self):
        import identity
        for r in ("company_admin", "hr_manager", "admin", "", None, "department_head"):
            self.assertEqual(identity.safe_role(r), "employee")
        self.assertEqual(identity.safe_role("team_lead"), "team_lead")

    def _manager_with(self, cur):
        from enterprise_sso import SSOManager
        mgr = SSOManager()
        conn = mock.MagicMock()
        conn.cursor.return_value = cur
        return mgr, conn

    def test_provision_returns_users_row_and_creates_membership(self):
        app = get_app()
        cur = _IdCur()
        mgr, conn = self._manager_with(cur)
        with app.test_request_context("/"), mock.patch.object(app, "mysql", mock.MagicMock(connection=conn)), \
                mock.patch.object(mgr, "log_audit_event"):
            user = mgr.provision_user(7, {"email": "new@firma.dk", "full_name": "New Person"},
                                      {"default_role": "company_admin"})
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], 100)                 # a users.id ...
        self.assertEqual(len(cur.members), 1)
        self.assertEqual(cur.members[0]["user_id"], 100)  # ... linked from company_users
        self.assertEqual(cur.members[0]["role"], "employee")   # never auto-admin

    def test_deactivated_member_cannot_sso_back_in(self):
        app = get_app()
        cur = _IdCur(users=[{"id": 9, "username": "bo", "email": "bo@firma.dk", "role": "user"}],
                     members=[{"company_id": 7, "user_id": 9, "role": "employee", "status": "inactive"}])
        mgr, conn = self._manager_with(cur)
        with app.test_request_context("/"), mock.patch.object(app, "mysql", mock.MagicMock(connection=conn)):
            self.assertIsNone(mgr.provision_user(7, {"email": "bo@firma.dk"}, {}))
            self.assertIsNone(mgr.find_user_by_email(7, "bo@firma.dk"))

    def test_callback_session_carries_users_id_not_company_users_id(self):
        app = get_app()
        users_row = {"id": 42, "username": "anna", "credits": 10, "role": "user", "email": "anna@firma.dk"}

        def responder(sql, params):
            s = " ".join(sql.split()).lower()
            if "from companies where company_slug" in s:
                return {"id": 7, "company_name": "Acme", "company_slug": "acme"}
            if "select cu.company_id" in s:      # auth._apply_session_user_context
                return {"company_id": 7, "company_role": "employee", "company_name": "Acme", "company_slug": "acme"}
            if "select role, status from company_users" in s:
                return {"role": "employee", "status": "active"}
            return None

        fake, p = patch_mysql(app, responder)
        import enterprise_sso
        with p, mock.patch.object(enterprise_sso.sso_manager, "authenticate_user", return_value=(users_row, None)), \
                mock.patch.object(enterprise_sso.sso_manager, "log_audit_event"):
            c = app.test_client()
            with c.session_transaction() as s:
                s["oauth2_state"] = "st"
                s["oauth2_nonce"] = NONCE
                s["oauth2_pkce"] = "ver"
            r = c.get("/sso/callback/acme/oauth2?state=st&code=abc")
            self.assertEqual(r.status_code, 302)
            with c.session_transaction() as s:
                self.assertEqual(s["user_id"], 42)
                self.assertEqual(s["user"], "anna")              # username, not the e-mail
                self.assertEqual(s["company_id"], 7)
                self.assertNotIn("oauth2_nonce", s)
                self.assertNotIn("oauth2_pkce", s)

    def test_callback_passes_the_session_nonce_and_verifier_to_the_provider(self):
        app = get_app()
        fake, p = patch_mysql(app, lambda sql, params: (
            {"id": 7, "company_name": "Acme", "company_slug": "acme"}
            if "from companies where company_slug" in " ".join(sql.split()).lower() else None))
        import enterprise_sso
        with p, mock.patch.object(enterprise_sso.sso_manager, "authenticate_user",
                                  return_value=(None, "Authentication failed")) as auth:
            c = app.test_client()
            with c.session_transaction() as s:
                s["oauth2_state"] = "st"
                s["oauth2_nonce"] = "the-nonce"
                s["oauth2_pkce"] = "the-verifier"
            c.get("/sso/callback/acme/oauth2?state=st&code=abc")
            ctx = auth.call_args.kwargs["context"]
            self.assertEqual(ctx, {"expected_nonce": "the-nonce", "code_verifier": "the-verifier"})

    def test_callback_rejects_bad_state_without_authenticating(self):
        app = get_app()
        fake, p = patch_mysql(app, lambda sql, params: (
            {"id": 7, "company_name": "Acme", "company_slug": "acme"}
            if "from companies where company_slug" in " ".join(sql.split()).lower() else None))
        import enterprise_sso
        with p, mock.patch.object(enterprise_sso.sso_manager, "authenticate_user") as auth:
            c = app.test_client()
            with c.session_transaction() as s:
                s["oauth2_state"] = "st"
            r = c.get("/sso/callback/acme/oauth2?state=FORGED&code=abc")
            self.assertEqual(r.status_code, 302)
            auth.assert_not_called()

    def test_config_save_rejects_http_endpoints_and_keeps_incomplete_configs_inactive(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            c = client_as(app, "company_admin")
            c.post("/admin/sso/config/7", data={"provider": "oauth2", "provider_name": "Entra", "is_enabled": "on",
                                                "token_url": "http://insecure.example.com/token"})
            self.assertEqual(fake.queries("insert into company_sso_configs"), [])
            c.post("/admin/sso/config/7", data={"provider": "oauth2", "provider_name": "Entra", "is_enabled": "on",
                                                "client_id": "x"})
            ins = fake.queries("insert into company_sso_configs")
            self.assertEqual(len(ins), 1)
            self.assertIn(False, ins[0][1])      # incomplete -> stored inactive


# ---------------------------------------------------------------------------
# S-3.2 API keys
# ---------------------------------------------------------------------------
class S32_ApiKeys(unittest.TestCase):
    def test_key_in_url_is_rejected_even_if_valid(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            r = app.test_client().get("/api/v1/employees?api_key=ak_realkey")
            self.assertEqual(r.status_code, 400)
            self.assertIn("X-API-Key header", r.get_json()["message"])
            self.assertEqual(fake.queries("company_api_keys"), [])   # never even looked up

    def test_missing_header_is_401(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            self.assertEqual(app.test_client().get("/api/v1/employees").status_code, 401)

    def test_authentication_is_hash_only(self):
        import enterprise_api as ea
        app = get_app()
        seen = []

        def responder(sql, params):
            seen.append((" ".join(sql.split()).lower(), params))
            return None

        fake, p = patch_mysql(app, responder)
        with p, app.app_context():
            ea.api_manager.authenticate_api_request("ak_abc")
        lookups = [q for q in seen if "from company_api_keys" in q[0]]
        self.assertTrue(lookups)
        for sql, params in lookups:
            self.assertIn("key_hash = %s", sql)
            self.assertNotIn("ak.api_key", sql)
            self.assertEqual(params[0], hashlib.sha256(b"ak_abc").hexdigest())

    def test_created_key_is_stored_hashed_with_prefix_only(self):
        import enterprise_api as ea
        app = get_app()
        fake, p = patch_mysql(app)
        with p, app.test_request_context("/api/v1/admin/api-keys", method="POST",
                                         json={"key_name": "n", "permissions": ["read:employees"]}):
            from flask import g
            g.company_id = 7
            g.api_key_data = {"created_by": 1}
            body, status = ea.create_api_key.__wrapped__()
        self.assertEqual(status, 201)
        raw = body.get_json()["data"]["api_key"]
        ins = fake.queries("insert into company_api_keys")
        self.assertEqual(len(ins), 1)
        sql, params = ins[0]
        self.assertNotIn(" api_key,", sql)                     # no plaintext column
        self.assertIn(hashlib.sha256(raw.encode()).hexdigest(), params)
        self.assertIn(raw[:10], params)
        self.assertNotIn(raw, [p for p in params if isinstance(p, str)])   # full key never stored

    def test_migration_scrubs_and_drops_plaintext(self):
        import enterprise_api as ea
        app = get_app()
        log = []

        def responder(sql, params):
            s = " ".join(sql.split()).lower()
            log.append((s, params))
            if s.startswith("show columns"):
                return {"Field": "x"}           # every column "exists"
            if s.startswith("select id, api_key from company_api_keys"):
                return [{"id": 3, "api_key": "ak_legacy_plaintext_key"}]
            return None

        fake, p = patch_mysql(app, responder)
        with p, app.app_context():
            ea._ensure_security_schema()
        updates = [q for q in log if q[0].startswith("update company_api_keys set key_hash")]
        self.assertEqual(len(updates), 1)
        self.assertIn("api_key = null", updates[0][0])
        self.assertEqual(updates[0][1][0], hashlib.sha256(b"ak_legacy_plaintext_key").hexdigest())
        self.assertTrue(any("drop column api_key" in q[0] for q in log))

    def test_openapi_no_longer_advertises_query_auth(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p:
            spec = app.test_client().get("/api/v1/openapi.json").get_json()
        self.assertNotIn("ApiKeyQuery", json.dumps(spec))

    def test_usage_is_tracked(self):
        src = open(secapp._REPO_ROOT + "/enterprise_api/__init__.py", encoding="utf-8").read()
        self.assertIn("last_used_at = NOW()", src)


# ---------------------------------------------------------------------------
# S-3.3 webhooks
# ---------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        _Handler.hits.append((self.path, dict(self.headers), body))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    do_GET = do_POST

    def log_message(self, *a):
        pass


class S33_Webhooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Handler.hits.clear()

    def _allow_loopback(self):
        import safe_http
        return mock.patch.object(safe_http, "ip_is_public", return_value=True)

    def test_blocks_private_loopback_and_metadata_addresses(self):
        import safe_http
        for url in ("http://127.0.0.1/", "http://10.0.0.5/x", "http://169.254.169.254/latest/meta-data",
                    "http://192.168.1.1/", "http://[::1]/", "http://0.0.0.0/", "http://localhost:8080/",
                    "ftp://example.com/", "file:///etc/passwd", "http://user:pw@example.com/"):
            with self.assertRaises(safe_http.UnsafeURL, msg=url):
                safe_http.resolve_public(url)

    def test_any_private_address_in_dns_answer_blocks(self):
        import safe_http
        infos = [(2, 1, 6, "", ("93.184.216.34", 80)), (2, 1, 6, "", ("10.1.2.3", 80))]
        with mock.patch("socket.getaddrinfo", return_value=infos):
            with self.assertRaises(safe_http.UnsafeURL):
                safe_http.resolve_public("http://rebind.example.com/")

    def test_public_address_is_accepted_and_resolved_once(self):
        import safe_http
        infos = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with mock.patch("socket.getaddrinfo", return_value=infos) as gai:
            scheme, host, port, path, ip = safe_http.resolve_public("https://hooks.example.com/in?x=1")
        self.assertEqual((scheme, host, port, path, ip), ("https", "hooks.example.com", 443, "/in?x=1", "93.184.216.34"))
        gai.assert_called_once()

    def test_redirects_are_not_followed(self):
        import safe_http
        infos = [(2, 1, 6, "", ("127.0.0.1", self.port))]
        with self._allow_loopback(), mock.patch("socket.getaddrinfo", return_value=infos):
            ok, detail = safe_http.post_json("http://hook.test:%d/redirect" % self.port, b"{}")
        self.assertFalse(ok)
        self.assertIn("redirect refused", detail)
        self.assertEqual([h[0] for h in _Handler.hits], ["/redirect"])   # /landed never requested

    def test_delivery_connects_to_the_validated_ip_and_sets_host(self):
        import safe_http
        infos = [(2, 1, 6, "", ("127.0.0.1", self.port))]
        with self._allow_loopback(), mock.patch("socket.getaddrinfo", return_value=infos):
            ok, _ = safe_http.post_json("http://hook.test:%d/in" % self.port, b'{"a":1}',
                                        headers={"X-Webhook-Signature": "t=1,v1=x"})
        self.assertTrue(ok)
        path, headers, body = _Handler.hits[0]
        self.assertEqual(headers["Host"], "hook.test:%d" % self.port)
        self.assertEqual(body, b'{"a":1}')

    def test_signature_includes_timestamp_and_rejects_replay(self):
        import webhook_signing as ws
        body = b'{"event":"x"}'
        header, ts = ws.sign("s3cret", body, timestamp=1_700_000_000)
        self.assertTrue(header.startswith("t=1700000000,v1="))
        self.assertTrue(ws.verify("s3cret", header, body, now=1_700_000_100))
        self.assertFalse(ws.verify("s3cret", header, body, now=1_700_000_000 + 301))     # replay window
        self.assertFalse(ws.verify("s3cret", header, body + b" ", now=1_700_000_100))    # tampered body
        self.assertFalse(ws.verify("other", header, body, now=1_700_000_100))            # wrong secret
        swapped = header.replace("t=1700000000", "t=1700000200")
        self.assertFalse(ws.verify("s3cret", swapped, body, now=1_700_000_250))          # ts not bound -> fails
        for junk in ("", "garbage", "t=abc,v1=zz", None):
            self.assertFalse(ws.verify("s3cret", junk, body))

    def test_event_bus_delivery_uses_safe_transport_and_signed_headers(self):
        import event_bus
        import safe_http

        class Cur:
            def execute(self, sql, params=None):
                pass

            def fetchall(self):
                return [{"id": 4, "url": "https://hooks.example.com/in", "secret": "s3cret", "events": '["order.approved"]'}]

            def close(self):
                pass

        class Conn:
            def cursor(self, *a, **k):
                return Cur()

        captured = {}

        def fake_post(url, body, headers=None, timeout=10):
            captured.update(url=url, body=body, headers=headers)
            return True, "HTTP 200"

        with mock.patch.object(safe_http, "post_json", side_effect=fake_post), \
                mock.patch.object(event_bus, "_bump_webhook_stats"):
            ok, err = event_bus._deliver_to_subscribers(
                Conn(), {"id": 77, "company_id": 7, "event_type": "order.approved", "payload": '{"x":1}'}, "acme")
        self.assertTrue(ok, err)
        h = captured["headers"]
        import webhook_signing as ws
        self.assertTrue(ws.verify("s3cret", h["X-Webhook-Signature"], captured["body"]))
        self.assertEqual(h["X-Webhook-Id"], "77")
        self.assertIn("X-Webhook-Timestamp", h)

    def test_event_bus_no_longer_uses_urlopen(self):
        src = open(secapp._REPO_ROOT + "/event_bus.py", encoding="utf-8").read()
        self.assertNotIn("urlopen(", src)

    def test_failed_redirect_marks_delivery_failed(self):
        import event_bus
        import safe_http

        class Cur:
            def execute(self, *a, **k):
                pass

            def fetchall(self):
                return [{"id": 4, "url": "https://hooks.example.com/in", "secret": "s", "events": '["*"]'}]

            def close(self):
                pass

        class Conn:
            def cursor(self, *a, **k):
                return Cur()

        with mock.patch.object(safe_http, "post_json", return_value=(False, "redirect refused (HTTP 302)")), \
                mock.patch.object(event_bus, "_bump_webhook_stats"):
            ok, err = event_bus._deliver_to_subscribers(
                Conn(), {"id": 1, "company_id": 7, "event_type": "x.y", "payload": "{}"}, "acme")
        self.assertFalse(ok)
        self.assertIn("redirect refused", err)

    def test_verification_doc_exists(self):
        import os
        self.assertTrue(os.path.exists(secapp._REPO_ROOT + "/docs/WEBHOOK_VERIFICATION.md"))


# ---------------------------------------------------------------------------
# S-3.4 SCIM
# ---------------------------------------------------------------------------
class S34_Scim(unittest.TestCase):
    def _call_create(self, cur, body):
        import scim_api
        app = get_app()
        with app.test_request_context("/scim/v2/Users", method="POST", json=body):
            from flask import g
            g.company_id = 7
            g.api_key_data = {"created_by": 1}
            with mock.patch.object(scim_api, "_cursor", return_value=cur), \
                    mock.patch.object(scim_api, "_seat_check_ok", return_value=True), \
                    mock.patch.object(scim_api, "_emit"), \
                    mock.patch.object(scim_api, "_send_welcome_email"), \
                    mock.patch.object(scim_api, "_recalc_employee_count"), \
                    mock.patch.object(app, "mysql", mock.MagicMock()):
                return scim_api.create_user.__wrapped__()

    class Cur(_IdCur):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.scim_rows = []

        def execute(self, sql, params=None):
            s = " ".join(sql.split()).lower()
            if s.startswith("select id, status from company_users"):
                self._result = []
                self.executed.append((s, params))
            elif s.startswith("insert into company_users (company_id, user_id, username"):
                self.executed.append((s, params))
                self.lastrowid = 900
                self.scim_rows.append(params)
                self._result = []
            elif s.startswith("select id, company_id, user_id, username, full_name, email"):
                self.executed.append((s, params))
                p = self.scim_rows[-1]
                self._result = [{"id": 900, "company_id": p[0], "user_id": p[1], "username": p[2], "full_name": p[3],
                                 "email": p[4], "job_title": None, "employee_id": None, "status": p[8],
                                 "added_at": None, "updated_at": None}]
            else:
                super().execute(sql, params)

    def test_create_makes_a_login_identity_and_links_it(self):
        cur = self.Cur()
        resp = self._call_create(cur, {"userName": "new.person@firma.dk", "name": {"givenName": "New", "familyName": "Person"},
                                       "emails": [{"value": "new.person@firma.dk", "primary": True}]})
        body, status = (resp if isinstance(resp, tuple) else (resp, resp.status_code))
        self.assertEqual(getattr(resp, "status_code", status), 201)
        self.assertEqual(len(cur.users), 1)                         # a users row exists now
        self.assertTrue(cur.users[0]["password"].startswith(("scrypt:", "pbkdf2:")))
        self.assertEqual(cur.scim_rows[0][1], cur.users[0]["id"])   # company_users.user_id -> users.id

    def test_existing_login_is_linked_not_duplicated(self):
        cur = self.Cur(users=[{"id": 9, "username": "bo", "email": "bo@firma.dk", "role": "user"}])
        self._call_create(cur, {"userName": "bo@firma.dk", "emails": [{"value": "bo@firma.dk", "primary": True}]})
        self.assertEqual(len(cur.users), 1)
        self.assertEqual(cur.scim_rows[0][1], 9)

    def test_platform_admin_cannot_be_provisioned_via_scim(self):
        cur = self.Cur(users=[{"id": 1, "username": "root", "email": "root@firma.dk", "role": "admin"}])
        resp = self._call_create(cur, {"userName": "root@firma.dk", "emails": [{"value": "root@firma.dk"}]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(cur.scim_rows, [])

    def test_username_without_email_cannot_get_a_login(self):
        cur = self.Cur()
        resp = self._call_create(cur, {"userName": "justaname"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(cur.users, [])

    def test_backfill_links_legacy_rows(self):
        import scim_api
        cur = self.Cur()
        cur._result = []
        rows = [{"id": 5, "company_id": 7, "email": "old@firma.dk", "full_name": "Old"}]

        class C(self.Cur):
            def execute(s, sql, params=None):
                q = " ".join(sql.split()).lower()
                if q.startswith("select id, company_id, email"):
                    s._result = rows
                    return
                super().execute(sql, params)

        c = C()
        conn = mock.MagicMock()
        conn.cursor.return_value = c
        self.assertEqual(scim_api.backfill_identities(conn), 1)
        updates = [e for e in c.executed if e[0].startswith("update company_users set user_id")]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1][0], c.users[0]["id"])


if __name__ == "__main__":
    unittest.main()
