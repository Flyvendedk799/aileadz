"""HR learning / courses UI regressions: CSV import with Danish numbers, course form
rendering of NULL columns, Danish labels on the learning-path cards, and the page-aware
AI panel chips for the internal-courses page. Offline (FakeMySQL, no network)."""
import io
import os
import unittest

os.environ.setdefault("SANDBOX", "1")

from tests.secapp import client_as, get_app, patch_mysql  # noqa: E402


COMPANY = {"id": 7, "name": "Demo", "company_name": "Demo", "user_role": "hr_manager",
           "department": None, "permissions": None, "status": "active", "plan": "pro"}


def _responder(extra=None):
    def r(sql, params):
        s = " ".join(str(sql).split()).lower()
        if "from companies c join company_users" in s:
            return dict(COMPANY)
        if extra:
            return extra(s, params)
        return None
    return r


class CsvImportTests(unittest.TestCase):
    def _post(self, filename, body):
        app = get_app()
        fake, p = patch_mysql(app, _responder())
        with p:
            c = client_as(app, "hr_manager")
            resp = c.post("/hr/courses/import", data={"csv_file": (io.BytesIO(body.encode("utf-8")), filename)},
                          content_type="multipart/form-data", follow_redirects=False)
        return resp, fake

    def test_danish_decimals_and_uppercase_extension_import(self):
        csv_body = "titel;varighed;pris\nSikkerhed;1,5;4.500,00\n"
        resp, fake = self._post("KURSER.CSV", csv_body)
        self.assertEqual(resp.status_code, 302)
        inserts = fake.queries("insert into company_courses")
        self.assertEqual(len(inserts), 1)
        params = inserts[0][1]
        self.assertEqual(params[6], 1.5)       # duration_hours
        self.assertEqual(params[7], 4500.0)    # price

    def test_non_csv_is_rejected_without_insert(self):
        resp, fake = self._post("kurser.xlsx", "x")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(fake.queries("insert into company_courses"), [])


class CourseFormTests(unittest.TestCase):
    def test_null_columns_do_not_render_as_none(self):
        course = {"id": 1, "company_id": 7, "title": "Kursus", "description": None, "category": None,
                  "tags": None, "course_type": "internal", "format": "e-learning", "duration_hours": None,
                  "price": None, "instructor": None, "location": None, "max_participants": None,
                  "department": None, "skill_tags": None, "difficulty_level": "beginner",
                  "is_mandatory": 0, "is_active": 1, "external_url": None}

        def extra(s, params):
            if s.startswith("select * from company_courses where id"):
                return course
            return None
        app = get_app()
        fake, p = patch_mysql(app, _responder(extra))
        with p:
            html = client_as(app, "hr_manager").get("/hr/courses/1/edit").get_data(as_text=True)
        self.assertIn('name="external_url"', html)
        self.assertNotIn(">None<", html)
        self.assertNotIn('value="None"', html)   # type=url would reject "None" and block saving


class LearningPathCardTests(unittest.TestCase):
    def test_difficulty_label_is_danish(self):
        path = {"id": 1, "company_id": 7, "path_name": "Onboarding", "path_category": "", "difficulty_level": "advanced",
                "is_active": 1, "version": 1, "enrolled_count": 0, "completed_count": 0, "avg_progress": 0}

        def extra(s, params):
            if "from learning_paths lp" in s:
                return [path]
            return None
        app = get_app()
        fake, p = patch_mysql(app, _responder(extra))
        with p:
            html = client_as(app, "hr_manager").get("/hr/learning-paths").get_data(as_text=True)
        self.assertIn("Avanceret", html)
        self.assertNotIn("Advanced", html)


class PanelChipsTests(unittest.TestCase):
    def test_internal_courses_page_has_its_own_chips(self):
        src = open("templates/fm/_ai_panel.html", encoding="utf-8").read()
        self.assertIn("internal_courses: [", src)


if __name__ == "__main__":
    unittest.main()
