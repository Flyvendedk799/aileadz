"""The learning-path step editor: courses are picked from the catalogue, errors keep the input."""

import datetime
import unittest
from unittest import mock

from tests.sqlite_platform import PlatformDB, make_app, client_as, render_patches

SCHEMA = """
CREATE TABLE learning_paths (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, path_name TEXT, path_category TEXT,
  difficulty_level TEXT, is_active INTEGER DEFAULT 1, version INTEGER NOT NULL DEFAULT 1, ordering_mode TEXT DEFAULT 'all_at_once',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE learning_path_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, path_id INTEGER, company_id INTEGER, position INTEGER,
  step_type TEXT, course_handle TEXT, title TEXT);
CREATE TABLE learning_path_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, path_id INTEGER, company_id INTEGER, version INTEGER,
  steps_json TEXT, saved_by INTEGER, note TEXT, saved_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

NEXT_YEAR = datetime.date.today().year + 1
CATALOG = {
    "prince2": {"handle": "prince2", "title": "PRINCE2 Foundation", "vendor": "Kursus ApS", "price_min": 12500.0, "format": "Fysisk",
                "variants": [{"date": "3. december %d" % NEXT_YEAR, "city": "Aarhus", "location": "Aarhus"}]},
    "excel": {"handle": "excel", "title": "Excel for ledere", "vendor": "Data A/S", "price_min": 4000.0, "format": "Online", "variants": []},
    "ledelse": {"handle": "ledelse", "title": "Nye ledere", "vendor": "Lead ApS", "price_min": 9000.0, "format": "Fysisk", "variants": []},
}


class EditorBase(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.db.raw.executescript(SCHEMA)
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma'),(8,'Anden')")
        d.execute("INSERT INTO users (id,username,email) VALUES (2,'hr','h@f.dk'),(3,'mads','m@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,role,status,department) VALUES "
                  "(7,2,'hr','hr_manager','active','HR'),(7,3,'mads','department_head','active','Salg')")
        d.execute("INSERT INTO learning_paths (id,company_id,path_name) VALUES (1,7,'Ledelse 1'),(2,8,'Andet')")
        d.execute("INSERT INTO company_courses(id,company_id,title,price,location,is_active) VALUES "
                  "(5,7,'Intern introduktion',0,'Kontoret',1),(6,8,'Intern hemmelig',0,'X',1)")
        for patcher in (
            mock.patch("catalog_service.get_product", side_effect=lambda h: CATALOG.get(h)),
            mock.patch("catalog_service.search_products",
                       side_effect=lambda f, per_page=24, company_id=None, **k: {"products": [
                           p for p in CATALOG.values() if (f.get("q") or "").lower() in p["title"].lower()]}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hr = client_as(self.app, user="hr", user_id=2, company_id=7, company_role="hr_manager")
        self.addCleanup(d.raw.close)

    def steps(self):
        return self.db.query("SELECT position, step_type, course_handle, title FROM learning_path_steps WHERE path_id = 1 ORDER BY position")


class StepEditorTests(EditorBase):
    def test_three_courses_and_a_guidance_step_save_as_a_new_version_without_typing_a_handle(self):
        resp = self.hr.post("/hr/learning-paths/1/trin", data={
            "step_type[]": ["catalog", "catalog", "info", "catalog"],
            "course_handle[]": ["prince2", "excel", "", "internal:5"],
            "title[]": ["", "Regneark", "Tal med din leder", ""],
            "note": "Første udgave",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/hr/learning-paths"))
        rows = self.steps()
        self.assertEqual([(r["step_type"], r["course_handle"], r["title"]) for r in rows], [
            ("catalog", "prince2", "PRINCE2 Foundation"),      # title defaults to the catalogue title
            ("catalog", "excel", "Regneark"),                  # an explicit label is kept
            ("info", None, "Tal med din leder"),
            ("catalog", "internal:5", "Intern introduktion"),
        ])
        self.assertEqual(self.db.one("SELECT version FROM learning_paths WHERE id = 1")["version"], 2)
        again = self.hr.post("/hr/learning-paths/1/trin", data={
            "step_type[]": ["catalog"], "course_handle[]": ["excel"], "title[]": [""], "note": "Kun Excel"})
        self.assertEqual(again.status_code, 302)
        version = self.db.one("SELECT version, note, steps_json FROM learning_path_versions")
        self.assertEqual((version["version"], version["note"]), (2, "Kun Excel"))
        self.assertIn("Tal med din leder", version["steps_json"])

    def test_an_unknown_handle_rerenders_everything_entered_with_an_inline_error(self):
        resp = self.hr.post("/hr/learning-paths/1/trin", data={
            "step_type[]": ["catalog", "catalog", "info"],
            "course_handle[]": ["prince2", "ledelse-nye-ledere", ""],
            "title[]": ["Mit navn for PRINCE2", "", "Tal med din leder"],
            "note": "Min note",
        })
        self.assertEqual(resp.status_code, 200)             # no redirect: nothing is discarded
        html = resp.get_data(as_text=True)
        self.assertIn("Kurset ‘ledelse-nye-ledere’ findes ikke i kataloget.", html)
        self.assertIn("PRINCE2 Foundation", html)
        self.assertIn('value="Mit navn for PRINCE2"', html)
        self.assertIn("Tal med din leder", html)
        self.assertIn('value="Min note"', html)
        self.assertIn("has-error", html)
        self.assertEqual(self.steps(), [])                  # and nothing was saved
        self.assertEqual(self.db.one("SELECT version FROM learning_paths WHERE id = 1")["version"], 1)

    def test_every_bad_row_is_flagged_not_only_the_first(self):
        html = self.hr.post("/hr/learning-paths/1/trin", data={
            "step_type[]": ["catalog", "catalog"], "course_handle[]": ["findes-ikke-1", ""], "title[]": ["", ""],
        }).get_data(as_text=True)
        self.assertIn("findes-ikke-1", html)
        self.assertIn("Vælg et kursus fra kataloget, eller fjern trinnet.", html)
        self.assertEqual(html.count('class="lps-row has-error"'), 2)

    def test_the_text_fallback_rerenders_the_posted_text_on_error(self):
        text = "prince2 | PRINCE2\nledelse-nye-ledere\n# Vejledning"
        resp = self.hr.post("/hr/learning-paths/1/steps", data={"steps": text})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("ledelse-nye-ledere", html)
        self.assertIn("prince2 | PRINCE2", html)
        self.assertEqual(self.steps(), [])
        ok = self.hr.post("/hr/learning-paths/1/steps", data={"steps": "prince2 | PRINCE2\n# Vejledning"})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual([r["course_handle"] for r in self.steps()], ["prince2", None])

    def test_steps_show_title_vendor_price_and_next_session(self):
        self.hr.post("/hr/learning-paths/1/trin", data={
            "step_type[]": ["catalog"], "course_handle[]": ["prince2"], "title[]": [""]})
        html = self.hr.get("/hr/learning-paths/1/trin").get_data(as_text=True)
        self.assertIn("PRINCE2 Foundation", html)
        self.assertIn("Kursus ApS", html)
        self.assertIn("12.500 kr.", html)
        self.assertIn("Næste hold 3. december %d · Aarhus" % NEXT_YEAR, html)

    def test_another_companys_path_is_not_editable(self):
        resp = self.hr.get("/hr/learning-paths/2/trin")
        self.assertEqual(resp.status_code, 302)

    def test_a_department_head_cannot_open_the_editor(self):
        head = client_as(self.app, user="mads", user_id=3, company_id=7, company_role="department_head")
        self.assertEqual(head.get("/hr/learning-paths/1/trin").status_code, 302)
        self.assertEqual(head.get("/hr/learning-paths/catalog-search?q=excel").status_code, 403)


class CatalogSearchTests(EditorBase):
    def search(self, q, client=None):
        resp = (client or self.hr).get("/hr/learning-paths/catalog-search?q=" + q)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["results"]

    def test_results_carry_what_hr_needs_to_choose(self):
        found = self.search("prince2")
        self.assertEqual(found[0]["handle"], "prince2")
        self.assertEqual((found[0]["vendor"], found[0]["price_label"], found[0]["format"]), ("Kursus ApS", "12.500 kr.", "Fysisk"))
        self.assertIn("Aarhus", found[0]["next_session"])

    def test_internal_courses_are_included_labelled_and_company_scoped(self):
        found = self.search("intern")
        self.assertEqual([(f["handle"], f["internal"], f["vendor"]) for f in found], [("internal:5", True, "Internt kursus")])
        self.assertEqual(self.search("hemmelig"), [])           # company 8's internal course

    def test_a_short_query_lists_nothing(self):
        self.assertEqual(self.search("e"), [])

    def test_anonymous_gets_401(self):
        self.assertEqual(self.app.test_client().get("/hr/learning-paths/catalog-search?q=excel").status_code, 401)


class GalleryTests(EditorBase):
    def test_the_editor_template_renders_in_the_design_gallery_without_a_route_context(self):
        admin = client_as(self.app, user="root", user_id=99, role="admin")
        resp = admin.get("/ui/learning_path_steps")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Eksempelforløb", resp.get_data(as_text=True))
