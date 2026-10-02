"""The CV portal was merged into the profile page: inline import + a generated CV view."""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


_PROFILE = {
    "headline": "Teamleder", "bio": "Leder et team på otte.", "target_role": "Afdelingsleder",
    "skills": [{"name": "Ledelse", "level": "ekspert"}, {"name": "Excel", "level": "mellem"}],
    "experience": [{"title": "Teamleder", "company": "Novo", "start_year": 2019, "end_year": None,
                    "is_current": True, "description": "Leder otte."}],
    "education": [{"degree": "Cand.merc.", "institution": "CBS", "year_completed": 2015, "description": ""}],
    "certifications": [{"name": "PMP", "issuer": "PMI", "issue_date": "2021", "expiry_date": ""}],
    "completed_courses": [{"title": "Konflikthåndtering", "vendor": "AMU", "completed_date": "2024"}],
    "languages": [{"language": "Engelsk", "proficiency": "flydende"}],
    "portfolio_links": [{"label": "LinkedIn", "url": "javascript:alert(1)"}],
}


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from run import create_app
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def _client(self, logged_in=True):
        client = self.app.test_client()
        if logged_in:
            with client.session_transaction() as sess:
                sess["user"] = "eva@example.dk"
        return client

    def test_old_portal_url_lands_on_the_profile_import(self):
        resp = self._client(False).get("/profil-upload")
        self.assertEqual(resp.status_code, 301)
        self.assertTrue(resp.headers["Location"].endswith("#cv"), resp.headers["Location"])
        self.assertIn("/profil", resp.headers["Location"])

    def test_no_form_fallback_routes_remain(self):
        client = self._client()
        self.assertEqual(client.post("/profil-upload").status_code, 405)
        self.assertEqual(client.post("/profil-upload/apply").status_code, 404)

    def test_generated_cv_requires_login(self):
        resp = self._client(False).get("/profil/cv")
        self.assertEqual(resp.status_code, 302)

    def test_generated_cv_renders_the_profile_and_escapes_it(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_full_profile", return_value=_PROFILE):
            html = self._client().get("/profil/cv").get_data(as_text=True)
        for needle in ("Teamleder", "Novo", "Cand.merc.", "CBS", "PMP", "Konflikthåndtering", "Engelsk, Flydende",
                       "Ekspert", "Afdelingsleder", "Print / gem som PDF"):
            self.assertIn(needle, html)
        self.assertIn("2019 &ndash; nu", html)
        # a stored link is shown as text, never as a clickable (possibly javascript:) href
        self.assertNotIn('href="javascript:', html)

    def test_generated_cv_for_an_empty_profile_points_to_the_import(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_full_profile", return_value={}):
            html = self._client().get("/profil/cv").get_data(as_text=True)
        self.assertIn("endnu ikke noget CV at vise", html)
        self.assertIn("#cv", html)

    def test_generated_cv_survives_a_profile_read_failure(self):
        with mock.patch("app1.user_profile_db.ensure_tables", side_effect=RuntimeError("db down")):
            resp = self._client().get("/profil/cv")
        self.assertEqual(resp.status_code, 200)


class ProfilePageTests(unittest.TestCase):
    def test_profile_page_hosts_the_import_and_its_assets(self):
        src = _read("templates/fm/my_profile.html")
        self.assertIn("{% include 'fm/_cv_import.html' %}", src)
        for asset in ("futurematch/assets/profile-cv.js", "futurematch/assets/profile-cv.css"):
            self.assertIn(f"?v={{{{ asset_version('{asset}') }}}}", src)
        self.assertIn("fm:cv-applied", src)               # reloads what the CV touched
        self.assertNotIn("futurematch.cv_upload", src)    # no link to a separate portal

    def test_import_markup_has_every_hook_the_script_binds(self):
        markup = _read("templates/fm/_cv_import.html")
        js = _read("static/futurematch/assets/profile-cv.js")
        hooks = set(re.findall(r'\$\("\[(data-[a-z-]+)\]"\)', js))
        hooks |= set(re.findall(r'\$\(\'\[(data-[a-z-]+)\]\'\)', js))
        self.assertTrue(hooks)
        for hook in hooks:
            self.assertIn(hook, markup, hook)
        for pane in ("intake", "progress", "review", "done"):
            self.assertIn(f'data-pane="{pane}"', markup)

    def test_script_uses_the_unchanged_cv_api(self):
        js = _read("static/futurematch/assets/profile-cv.js")
        for needle in ("/api/cv/parse-stream", "/api/cv/parse", "/api/cv/apply", "/api/cv/improve",
                       '"stage"', '"result"', '"error"'):
            self.assertIn(needle, js)
        # nothing is written until the explicit save click
        self.assertEqual(js.count('fetch("/api/cv/apply"'), 1)

    def test_coach_is_non_destructive(self):
        js = _read("static/futurematch/assets/profile-cv.js")
        self.assertIn("Brug forslag", js)
        self.assertIn("Behold min tekst", js)

    def test_every_other_surface_points_at_the_profile(self):
        for rel in ("templates/fm/employee_home.html", "templates/fm/mind_map.html",
                    "static/futurematch/assets/chat.js", "app1/tools.py"):
            src = _read(rel)
            self.assertNotIn("/profil-upload", src, rel)
            self.assertNotIn("futurematch.cv_upload", src, rel)

    def test_open_cv_upload_action_targets_the_profile_import(self):
        import app1.tools as tools
        import json
        out = json.loads(tools._execute_open_in_app({"action": "open_cv_upload"}, "ada"))
        self.assertEqual(out["target"], "/profil#cv")

    def test_old_template_and_three_js_copy_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "templates", "fm", "cv_upload.html")))
        self.assertNotIn("unpkg.com/three@0.160", _read("templates/fm/my_profile.html"))


if __name__ == "__main__":
    unittest.main()
