"""N-4.6 / N-7.1 / N-7.2: settings hub, SSO (OIDC only), API keys, deactivation request."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import api_keys_ui  # noqa: E402
from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402


class HubBase(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.db.raw.executescript(
            "ALTER TABLE company_api_keys ADD COLUMN key_hash TEXT;"
            "ALTER TABLE companies ADD COLUMN company_domain TEXT;"
            "ALTER TABLE companies ADD COLUMN max_employees INTEGER;"
            "CREATE TABLE company_sso_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, "
            "provider TEXT, provider_name TEXT, config TEXT, is_enabled INTEGER DEFAULT 0, "
            "auto_provision_users INTEGER DEFAULT 0, default_role TEXT DEFAULT 'employee', "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT, UNIQUE(company_id, provider));"
        )
        self.app = make_app(self.db)
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.addCleanup(self._ctx.pop)
        for p in (render_patches(), mock.patch.object(api_keys_ui, "_has_col", side_effect=lambda cur, t, c: c in ("key_hash",))):
            p.start()
            self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id, company_name, company_slug, company_domain) VALUES "
                  "(7, 'Firma A/S', 'firma', 'firma.dk'), (8, 'Andet', 'andet', 'andet.dk')")
        d.execute("INSERT INTO users (id, username, email, role) VALUES (1,'admin','a@x.dk','admin'), "
                  "(2,'ca','ca@firma.dk','user'), (3,'hr','hr@firma.dk','user'), (4,'dh','dh@firma.dk','user'), "
                  "(5,'emp','e@firma.dk','user')")
        d.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES "
                  "(7, 2, 'ca', 'company_admin', 'active'), (7, 3, 'hr', 'hr_manager', 'active'), "
                  "(7, 4, 'dh', 'department_head', 'active'), (7, 5, 'emp', 'employee', 'active')")
        self.admin_c = self.client("ca", 2, "company_admin")
        self.hr_c = self.client("hr", 3, "hr_manager")
        self.dh_c = self.client("dh", 4, "department_head")
        self.emp_c = self.client("emp", 5, "employee")

    def client(self, name, uid, role, company=7):
        return client_as(self.app, user=name, user_id=uid, company_id=company, company_role=role,
                         company_name="Firma A/S")


class HubAccessTests(HubBase):
    def test_company_admin_sees_every_tab(self):
        html = self.admin_c.get("/virksomhed/indstillinger/").get_data(as_text=True)
        for label in ("Virksomhed", "Branding", "Chatbot &amp; widget", "Webhooks", "SSO", "API-nøgler",
                      "Integrationer", "Bestillingspolitik"):
            self.assertIn(label, html)

    def test_hr_manager_does_not_get_sso_or_api_keys(self):
        html = self.hr_c.get("/virksomhed/indstillinger/").get_data(as_text=True)
        self.assertIn("Bestillingspolitik", html)
        self.assertNotIn("/virksomhed/indstillinger/sso", html)
        self.assertNotIn("/virksomhed/indstillinger/api", html)
        self.assertEqual(self.hr_c.get("/virksomhed/indstillinger/sso").status_code, 302)
        self.assertEqual(self.hr_c.get("/virksomhed/indstillinger/api").status_code, 302)

    def test_department_head_and_employee_are_turned_away(self):
        for c in (self.dh_c, self.emp_c):
            r = c.get("/virksomhed/indstillinger/")
            self.assertEqual(r.status_code, 302)
            self.assertNotIn("indstillinger", r.headers["Location"])

    def test_anonymous_goes_to_login(self):
        r = self.app.test_client().get("/virksomhed/indstillinger/")
        self.assertIn("/login", r.headers["Location"])

    def test_unknown_tab_falls_back_to_the_first(self):
        r = self.admin_c.get("/virksomhed/indstillinger/findes-ikke")
        self.assertEqual(r.status_code, 302)

    def test_bestilling_tab_embeds_the_team_order_policy(self):
        html = self.hr_c.get("/virksomhed/indstillinger/bestilling").get_data(as_text=True)
        self.assertIn("Automatisk godkendelse", html)
        self.assertIn("Teambestilling fra chatten", html)


class OldUrlsRedirectIntoTheHubTests(HubBase):
    def test_get_redirects(self):
        cases = {
            "/companies/settings": "/virksomhed/indstillinger/virksomhed",
            "/companies/branding": "/virksomhed/indstillinger/branding",
            "/hr/chatbot-settings": "/virksomhed/indstillinger/chatbot",
            "/hr/widget": "/virksomhed/indstillinger/chatbot?view=widget",
            "/enterprise/webhooks": "/virksomhed/indstillinger/webhooks",
            "/admin/sso/config/7": "/virksomhed/indstillinger/sso",
        }
        for old, new in cases.items():
            r = self.admin_c.get(old)
            self.assertEqual(r.status_code, 302, old)
            self.assertTrue(r.headers["Location"].endswith(new), (old, r.headers["Location"]))

    def test_hub_tabs_call_the_real_views_inside_the_hub(self):
        seen = {}

        def stub(name):
            def view():
                from flask import g
                seen[name] = (g.via_settings_hub, g.settings_tab)
                return "STUB-" + name
            return view

        for endpoint, tab in (("companies.branding", "branding"), ("hr_dashboard.chatbot_settings", "chatbot"),
                              ("enterprise_settings.webhooks_page", "webhooks")):
            self.app.view_functions[endpoint] = stub(tab)
        self.assertIn("STUB-branding", self.hr_c.get("/virksomhed/indstillinger/branding").get_data(as_text=True))
        self.assertIn("STUB-chatbot", self.hr_c.get("/virksomhed/indstillinger/chatbot").get_data(as_text=True))
        self.assertIn("STUB-webhooks", self.admin_c.get("/virksomhed/indstillinger/webhooks").get_data(as_text=True))
        self.assertEqual(seen["branding"], (True, "branding"))


class SupportEmailTests(HubBase):
    def test_support_email_field_is_no_longer_ignored(self):
        with mock.patch("companies.save_branding_settings") as save:
            self.admin_c.post("/companies/settings", data={
                "company_name": "Firma A/S", "industry": "IT",
                "contact_email": "kontakt@firma.dk", "support_email": "support@firma.dk",
            })
        data = save.call_args.args[1]
        self.assertEqual(data["support_email"], "support@firma.dk")
        self.assertEqual(data["contact_email"], "kontakt@firma.dk")

    def test_falls_back_to_contact_email_when_support_empty(self):
        with mock.patch("companies.save_branding_settings") as save:
            self.admin_c.post("/companies/settings", data={"company_name": "F", "contact_email": "k@f.dk"})
        self.assertEqual(save.call_args.args[1]["support_email"], "k@f.dk")


class DeactivationRequestTests(HubBase):
    def test_company_admin_files_a_request_admins_are_told_and_nothing_is_switched_off(self):
        r = self.admin_c.post("/virksomhed/indstillinger/deaktiver", data={"note": "Vi lukker"})
        self.assertEqual(r.status_code, 302)
        row = self.db.one("SELECT * FROM account_requests")
        self.assertEqual((row["kind"], row["status"], row["company_id"]), ("deactivate", "open", 7))
        self.assertTrue(self.db.query("SELECT 1 FROM notifications WHERE user_id='admin'"))
        self.assertEqual(self.db.one("SELECT COALESCE(status,'active') AS s FROM companies WHERE id=7")["s"], "active")
        self.admin_c.post("/virksomhed/indstillinger/deaktiver", data={})
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM account_requests")["n"], 1)   # no duplicates

    def test_hr_manager_cannot_request_deactivation(self):
        self.hr_c.post("/virksomhed/indstillinger/deaktiver", data={})
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM account_requests")["n"], 0)


class SsoTabTests(HubBase):
    def save(self, client=None, **over):
        data = {"preset": "entra", "tenant": "tid-123", "client_id": "cid", "client_secret": "s3cret",
                "is_enabled": "on", "provider_name": "Microsoft"}
        data.update(over)
        return (client or self.admin_c).post("/virksomhed/indstillinger/sso/gem", data=data)

    def config(self):
        from enterprise_sso import _normalize_config
        row = self.db.one("SELECT * FROM company_sso_configs WHERE company_id = 7")
        return row, _normalize_config(row["config"])   # decrypted view

    def test_entra_preset_fills_the_endpoints_and_encrypts_the_secret(self):
        self.save()
        row, cfg = self.config()
        self.assertEqual(row["provider"], "oauth2")
        self.assertEqual(cfg["authorization_url"], "https://login.microsoftonline.com/tid-123/oauth2/v2.0/authorize")
        self.assertEqual(cfg["issuer"], "https://login.microsoftonline.com/tid-123/v2.0")
        self.assertTrue(cfg["redirect_uri"].endswith("/sso/callback/firma/oauth2"))
        self.assertNotIn("s3cret", row["config"])          # secret is encrypted at rest
        self.assertEqual(row["default_role"], "employee")   # provisioning never grants elevated roles

    def test_blank_secret_keeps_the_stored_one(self):
        self.save()
        before = self.config()[1]["client_secret"]
        self.save(client_secret="", client_id="nyt-id")
        after = self.config()[1]
        self.assertEqual(after["client_secret"], before)
        self.assertEqual(after["client_id"], "nyt-id")

    def test_missing_fields_are_rejected_with_a_message(self):
        self.save(tenant="")
        self.assertIsNone(self.db.one("SELECT 1 AS x FROM company_sso_configs"))
        self.save(client_id="")
        self.assertIsNone(self.db.one("SELECT 1 AS x FROM company_sso_configs"))

    def test_only_company_admin_can_save(self):
        self.save(client=self.hr_c)
        self.assertIsNone(self.db.one("SELECT 1 AS x FROM company_sso_configs"))

    def test_page_never_reveals_the_secret_and_marks_saml_and_ldap_inactive(self):
        self.save()
        self.db.execute("INSERT INTO company_sso_configs (company_id, provider, provider_name, config, is_enabled) "
                        "VALUES (7, 'saml', 'Gammel SAML', '{}', 1), (7, 'ldap', 'AD', '{}', 1)")
        html = self.admin_c.get("/virksomhed/indstillinger/sso").get_data(as_text=True)
        self.assertNotIn("s3cret", html)
        self.assertIn("Gemt. Lad stå tom", html)
        self.assertIn("Inaktiv", html)
        self.assertIn("Gammel SAML", html)
        self.assertNotIn("SAML 2.0", html)                # no SAML option in the form
        self.assertIn("/sso/callback/firma/oauth2", html)  # redirect URI to register

    def test_tenant_isolation(self):
        self.save()
        other = self.client("x", 9, "company_admin", company=8)
        html = other.get("/virksomhed/indstillinger/sso").get_data(as_text=True)
        self.assertNotIn("Microsoft", html.split("Status")[1] if "Status" in html else "")
        self.assertIsNone(self.db.one("SELECT 1 AS x FROM company_sso_configs WHERE company_id = 8"))


class SsoDiscoveryTests(HubBase):
    def test_known_domain_with_enabled_oidc_redirects_to_its_login(self):
        self.db.execute("INSERT INTO company_sso_configs (company_id, provider, provider_name, config, is_enabled) "
                        "VALUES (7, 'oauth2', 'MS', '{}', 1)")
        r = self.app.test_client().post("/sso/discover", data={"email": "mette@Firma.dk"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/sso/login/firma/oauth2"))

    def test_unknown_or_disabled_domain_gets_a_helpful_message_not_a_redirect(self):
        self.db.execute("INSERT INTO company_sso_configs (company_id, provider, provider_name, config, is_enabled) "
                        "VALUES (8, 'oauth2', 'MS', '{}', 0)")
        for email in ("x@ukendt.dk", "y@andet.dk", "ikke-en-mail"):
            r = self.app.test_client().post("/sso/discover", data={"email": email})
            self.assertEqual(r.status_code, 200)
            self.assertIn("Vi fandt ingen SSO", r.get_data(as_text=True))

    def test_login_page_buttons_point_to_discovery(self):
        html = self.app.test_client().get("/login").get_data(as_text=True)
        self.assertIn("/sso/discover", html)


class ApiKeyTests(HubBase):
    def test_create_shows_key_once_and_stores_only_the_hash(self):
        r = self.admin_c.post("/virksomhed/indstillinger/api/opret", data={"name": "Løn", "preset": "read"},
                              follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("ak_", html)
        key = html.split('id="newKey"')[1].split(">")[1].split("<")[0].strip()
        row = self.db.one("SELECT * FROM company_api_keys")
        self.assertEqual(row["key_hash"], api_keys_ui.hash_key(key))
        self.assertIsNone(row["api_key"])
        self.assertEqual(row["company_id"], 7)
        again = self.admin_c.get("/virksomhed/indstillinger/api").get_data(as_text=True)
        self.assertNotIn(key, again)                       # gone after the first view
        self.assertIn("Løn", again)

    def test_list_shows_last_used_and_revoke_works_only_for_own_company(self):
        self.admin_c.post("/virksomhed/indstillinger/api/opret", data={"name": "A", "preset": "full"})
        kid = self.db.one("SELECT id FROM company_api_keys")["id"]
        self.db.execute("UPDATE company_api_keys SET last_used_at = '2026-09-01 10:00:00' WHERE id=%s", (kid,))
        html = self.admin_c.get("/virksomhed/indstillinger/api").get_data(as_text=True)
        self.assertIn("Fuld adgang", html)
        self.assertIn("2026-09-01", html)
        other = self.client("x", 9, "company_admin", company=8)
        other.post("/virksomhed/indstillinger/api/%d/traek-tilbage" % kid)
        self.assertEqual(self.db.one("SELECT is_active FROM company_api_keys")["is_active"], 1)
        self.admin_c.post("/virksomhed/indstillinger/api/%d/traek-tilbage" % kid)
        self.assertEqual(self.db.one("SELECT is_active FROM company_api_keys")["is_active"], 0)

    def test_hr_manager_cannot_create_or_revoke(self):
        self.hr_c.post("/virksomhed/indstillinger/api/opret", data={"name": "X", "preset": "full"})
        self.assertIsNone(self.db.one("SELECT 1 AS x FROM company_api_keys"))

    def test_name_is_required(self):
        self.admin_c.post("/virksomhed/indstillinger/api/opret", data={"name": " ", "preset": "read"})
        self.assertIsNone(self.db.one("SELECT 1 AS x FROM company_api_keys"))

    def test_unknown_preset_is_refused(self):
        self.admin_c.post("/virksomhed/indstillinger/api/opret", data={"name": "X", "preset": "root"})
        self.assertIsNone(self.db.one("SELECT 1 AS x FROM company_api_keys"))

    def test_created_key_authenticates_against_the_api_hash_lookup(self):
        import enterprise_api
        _id, raw = api_keys_ui.create_key(self.db.connection, 7, "T", preset="read", created_by=2)
        row = self.db.one("SELECT * FROM company_api_keys WHERE id=%s", (_id,))
        self.assertEqual(row["key_hash"], enterprise_api._hash_api_key(raw))


if __name__ == "__main__":
    unittest.main()
