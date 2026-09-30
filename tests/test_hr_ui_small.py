"""N-4.5 insight CTAs, N-2.3 shared admin subnav, N-4.7 approvals template."""

import os
import unittest

import jinja2

ROOT = os.path.join(os.path.dirname(__file__), "..")
TEMPLATES = os.path.join(ROOT, "templates")


def _env():
    return jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)


class InsightCtaTests(unittest.TestCase):
    def render(self, ins):
        t = _env().from_string("{% import 'fm/_macros.html' as ui %}{{ ui.insight_card(ins) }}")
        return t.render(ins=ins)

    def test_type_gets_a_default_call_to_action(self):
        html = self.render({"title": "Få bestillinger", "body": "b", "severity": "warning", "insight_type": "no_conversions"})
        self.assertIn("/hr/assign-path", html)
        self.assertIn("Tildel kursus", html)

    def test_explicit_action_url_wins(self):
        html = self.render({"title": "x", "body": "b", "severity": "info", "insight_type": "no_conversions",
                            "action_url": "/hr/budgets", "action_label": "Justér budget"})
        self.assertIn('href="/hr/budgets"', html)
        self.assertIn("Justér budget", html)
        self.assertNotIn("/hr/assign-path", html)

    def test_unknown_type_without_url_has_no_dead_button(self):
        html = self.render({"title": "x", "body": "b", "severity": "info", "insight_type": "ukendt"})
        self.assertNotIn("fm-insight-cta", html)


class SubnavTests(unittest.TestCase):
    def test_admin_pages_share_one_subnav(self):
        for name in ("admin_users", "admin_credits", "admin_vendors", "admin_agreements", "admin_catalog"):
            src = open(os.path.join(TEMPLATES, "fm", name + ".html"), encoding="utf-8").read()
            self.assertIn("fm/_admin_subnav.html", src, name)
            self.assertNotIn('<div class="pg-subnav">', src, name)

    def test_billing_is_linked_from_admin_nav(self):
        src = open(os.path.join(TEMPLATES, "fm", "_admin_subnav.html"), encoding="utf-8").read()
        self.assertIn("admin_billing", src)


if __name__ == "__main__":
    unittest.main()
