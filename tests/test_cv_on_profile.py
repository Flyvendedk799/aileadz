"""One CV feature, one home: the CV studio at /profil/cv (import, document, coach, export)."""
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

    def _get(self, url, profile=_PROFILE, client=None):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_full_profile", return_value=profile):
            return (client or self._client()).get(url)

    def test_old_portal_url_lands_on_the_studio_import(self):
        resp = self._client(False).get("/profil-upload")
        self.assertEqual(resp.status_code, 301)
        self.assertTrue(resp.headers["Location"].endswith("/profil/cv#import"), resp.headers["Location"])

    def test_no_form_fallback_routes_remain(self):
        client = self._client()
        self.assertEqual(client.post("/profil-upload").status_code, 405)
        self.assertEqual(client.post("/profil-upload/apply").status_code, 404)

    def test_every_cv_page_requires_login(self):
        for url in ("/profil/cv", "/profil/cv/print", "/profil/cv/download.txt"):
            self.assertEqual(self._client(False).get(url).status_code, 302, url)

    def test_studio_renders_the_document_import_and_escapes_it(self):
        html = self._get("/profil/cv").get_data(as_text=True)
        for needle in ("Teamleder", "Novo", "Cand.merc.", "CBS", "PMP", "Konflikthåndtering", "Engelsk, Flydende",
                       "Ekspert", "2019 – nu", 'id="cvImport"', "data-cv-edit", "CV-styrke", "cv-studio.js"):
            self.assertIn(needle, html)
        # a stored javascript: link never reaches the CV, not even as text
        self.assertNotIn("javascript:", html)

    def test_print_view_has_no_edit_hooks(self):
        html = self._get("/profil/cv/print?template=moderne").get_data(as_text=True)
        self.assertIn("Print / gem som PDF", html)
        self.assertIn("cvd--moderne", html)
        self.assertNotIn("data-cv-edit", html)

    def test_downloads(self):
        txt = self._get("/profil/cv/download.txt")
        self.assertEqual(txt.status_code, 200)
        self.assertIn("attachment", txt.headers["Content-Disposition"])
        body = txt.get_data(as_text=True)
        self.assertIn("ERFARING", body)
        self.assertIn("Teamleder, Novo (2019 – nu)", body)
        self.assertIn("## Erfaring", self._get("/profil/cv/download.md").get_data(as_text=True))
        data = self._get("/profil/cv/download.json").get_json()
        self.assertEqual(data["cv"]["experience"][0]["company"], "Novo")
        self.assertEqual(self._get("/profil/cv/download.exe").status_code, 404)

    def test_empty_profile_points_to_the_import(self):
        html = self._get("/profil/cv", profile={}).get_data(as_text=True)
        self.assertIn("endnu ikke noget CV at vise", html)
        self.assertIn('data-cv-open', html)

    def test_studio_survives_a_profile_read_failure(self):
        with mock.patch("app1.user_profile_db.ensure_tables", side_effect=RuntimeError("db down")):
            resp = self._client().get("/profil/cv")
        self.assertEqual(resp.status_code, 200)

    def test_document_api_returns_editable_html_and_quality(self):
        data = self._get("/api/cv/document").get_json()
        self.assertTrue(data["success"])
        self.assertIn("data-cv-edit", data["html"])
        self.assertIn("score", data["quality"])

    def test_document_api_previews_unsaved_prefs(self):
        import json
        q = json.dumps({"template": "kompakt", "hidden": ["skills"]})
        data = self._get("/api/cv/document?prefs=" + q).get_json()
        self.assertIn("cvd--kompakt", data["html"])
        self.assertNotIn('data-section="skills"', data["html"])

    def test_prefs_save_reports_failure_honestly(self):
        client = self._client()
        with mock.patch("app1.user_profile_db.set_cv_prefs", side_effect=RuntimeError("db down")):
            resp = client.post("/api/cv/prefs", json={"template": "moderne"})
        self.assertEqual(resp.status_code, 503)
        with mock.patch("app1.user_profile_db.set_cv_prefs") as save:
            resp = client.post("/api/cv/prefs", json={"template": "moderne", "hidden": ["bogus", "links"]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["prefs"]["hidden"], ["links"])
        save.assert_called_once()

    def test_coach_reads_the_stored_job_when_given_an_id(self):
        profile = dict(_PROFILE, experience=[dict(_PROFILE["experience"][0], id=7)])
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_full_profile", return_value=profile), \
                mock.patch("competency.compute_skill_gaps", return_value=[]), \
                mock.patch("cv_ingest.improve_cv_section", return_value={"suggestion": "Ledte otte."}) as coach:
            resp = self._client().post("/api/cv/improve", json={"section": "experience", "id": 7})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(coach.call_args.args[1]["company"], "Novo")
        self.assertNotIn("id", coach.call_args.args[1])

    def test_coach_rejects_unknown_sections(self):
        self.assertEqual(self._client().post("/api/cv/improve", json={"section": "x", "content": "y"}).status_code, 400)


class StudioPageTests(unittest.TestCase):
    def test_studio_hosts_the_import_and_its_assets(self):
        src = _read("templates/fm/my_cv.html")
        self.assertIn("{% include 'fm/_cv_import.html' %}", src)
        for asset in ("futurematch/assets/profile-cv.js", "futurematch/assets/profile-cv.css",
                      "futurematch/assets/cv-studio.js", "futurematch/assets/cv-studio.css",
                      "futurematch/assets/cv-document.css"):
            self.assertIn(f"?v={{{{ asset_version('{asset}') }}}}", src)
        self.assertIn("fm:cv-applied", _read("static/futurematch/assets/cv-studio.js"))  # reloads after import

    def test_profile_page_links_to_the_studio_instead_of_hosting_a_second_import(self):
        src = _read("templates/fm/my_profile.html")
        self.assertNotIn("_cv_import.html", src)
        self.assertNotIn("profile-cv.js", src)
        self.assertIn("futurematch.my_cv", src)
        self.assertNotIn("futurematch.cv_upload", src)

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
        for rel in ("static/futurematch/assets/profile-cv.js", "static/futurematch/assets/cv-studio.js"):
            js = _read(rel)
            self.assertIn("Brug forslag", js, rel)
            self.assertIn("Behold min tekst", js, rel)
        # the studio only writes on the explicit Gem click, through the existing profile endpoints
        studio = _read("static/futurematch/assets/cv-studio.js")
        self.assertEqual(studio.count('"/api/profile/experience"'), 1)
        self.assertEqual(studio.count('"/api/profile/summary"'), 1)

    def test_every_other_surface_points_at_the_studio(self):
        for rel in ("templates/fm/employee_home.html", "templates/fm/mind_map.html",
                    "static/futurematch/assets/chat.js", "app1/tools.py", "templates/fm/my_profile.html"):
            src = _read(rel)
            self.assertNotIn("/profil-upload", src, rel)
            self.assertNotIn("futurematch.cv_upload", src, rel)
            self.assertNotIn("/profil#cv", src, rel)

    def test_open_cv_upload_action_targets_the_studio_import(self):
        import app1.tools as tools
        import json
        out = json.loads(tools._execute_open_in_app({"action": "open_cv_upload"}, "ada"))
        self.assertEqual(out["target"], "/profil/cv#import")

    def test_old_template_and_three_js_copy_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "templates", "fm", "cv_upload.html")))
        self.assertNotIn("unpkg.com/three@0.160", _read("templates/fm/my_cv.html"))


if __name__ == "__main__":
    unittest.main()
