"""Accessibility basics: one h1, a specific page title and labelled form fields.

Source checks cover every template that extends the app shell (titles) and the
form templates listed in the accessibility plan (labels). Rendered checks cover
the public pages, which can be fetched without a session.
"""
import glob
import os
import re
import unittest

from tests.sqlite_platform import PlatformDB, make_app, render_patches

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")

LABEL = re.compile(r"<label(?P<attrs>[^>]*)>(?P<text>.*?)</label>", re.S)
CONTROL = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.S)

# Form templates whose labels were unassociated (accessibility review E15).
LABELLED_FORMS = (
    "my_profile", "branding", "company_register", "settings_sso", "add_employee", "edit_employee", "register",
)


def _src(rel):
    with open(os.path.join(TEMPLATES, rel), encoding="utf-8") as fh:
        return fh.read()


def unassociated_labels(source):
    """Labels with neither a ``for`` attribute nor a nested control."""
    bad = []
    for m in LABEL.finditer(source):
        if re.search(r"\bfor=", m.group("attrs")) or re.search(r"<(input|select|textarea)\b", m.group("text")):
            continue
        bad.append(m.group(0)[:80])
    return bad


def unlabelled_controls(source):
    """Visible controls with no id referenced by a label, no aria-label and no wrapping label."""
    for_ids = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', source))
    missing = []
    for m in CONTROL.finditer(source):
        attrs = m.group(2)
        if re.search(r'type="(hidden|submit|button|checkbox|radio|file)"', attrs):
            continue
        if "aria-label" in attrs or "aria-labelledby" in attrs:
            continue
        ident = re.search(r'\bid="([^"{}]+)"', attrs)
        if ident and ident.group(1) in for_ids:
            continue
        # Nested in a label: the nearest preceding <label> is still open.
        head = source[:m.start()]
        if head.rfind("<label") > head.rfind("</label>"):
            continue
        missing.append(m.group(0)[:80])
    return missing


class TemplateSourceTests(unittest.TestCase):
    def test_listed_forms_have_every_label_associated(self):
        for name in LABELLED_FORMS:
            with self.subTest(template=name):
                self.assertEqual(unassociated_labels(_src("fm/%s.html" % name)), [])

    def test_listed_forms_have_no_unlabelled_visible_controls(self):
        for name in LABELLED_FORMS:
            with self.subTest(template=name):
                self.assertEqual(unlabelled_controls(_src("fm/%s.html" % name)), [])

    def test_every_page_template_has_a_specific_title(self):
        for path in sorted(glob.glob(os.path.join(TEMPLATES, "fm", "*.html"))):
            source = open(path, encoding="utf-8").read()
            if "extends" not in source or "fm_base.html" not in source:
                continue
            name = os.path.basename(path)
            with self.subTest(template=name):
                m = re.search(r"\{% block title %\}(.*?)\{% endblock %\}", source, re.S)
                self.assertIsNotNone(m, "%s has no title block" % name)
                self.assertNotEqual(m.group(1).strip(), "Futurematch", "%s has the bare default title" % name)

    def test_no_english_dashboard_label_in_hr_templates(self):
        for name in ("chatbot", "analytics"):
            self.assertNotIn("</i> Dashboard", _src("fm/%s.html" % name))

    def test_login_password_toggle_has_a_toggling_aria_label(self):
        source = _src("fm/login.html")
        self.assertIn('aria-label="Vis adgangskode"', source)
        self.assertIn("Skjul adgangskode", source)


class PublicPagesRenderedTests(unittest.TestCase):
    PAGES = (
        "/", "/catalog", "/catalog/categories", "/catalog/vendors", "/support", "/privacy", "/terms", "/about",
        "/for-virksomheder", "/login", "/register", "/does-not-exist",
    )

    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.db.raw.close)
        self.client = self.app.test_client()

    def test_each_public_page_has_one_h1_and_a_specific_title(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertIn(r.status_code, (200, 404), path)
                html = r.get_data(as_text=True)
                self.assertEqual(len(re.findall(r"<h1\b", html)), 1, "%s should have exactly one h1" % path)
                title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
                self.assertNotIn(title, ("", "Futurematch"), path)

    def test_forgot_password_page_has_an_h1(self):
        html = self.client.get("/forgot-password").get_data(as_text=True)
        self.assertEqual(len(re.findall(r"<h1\b", html)), 1)

    def test_public_form_controls_have_labels_in_rendered_html(self):
        for path in ("/login", "/register", "/for-virksomheder"):
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                body = html[html.find("<body"):]
                self.assertEqual(unassociated_labels(body), [], path)


if __name__ == "__main__":
    unittest.main()
