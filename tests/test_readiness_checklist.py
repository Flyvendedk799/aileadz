"""The "Kom i gang" readiness page is a real checklist: shared macro, progress bar, accessible
status icons, a specific action label per check, one name, and a dashboard card that goes away
when everything is done."""

import datetime
import os
import re
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import jinja2  # noqa: E402

import customer_success  # noqa: E402
from tests.jinja_globals import add_app_globals  # noqa: E402
from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _env():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES))
    env.globals["url_for"] = lambda ep, **kw: "/" + ep
    add_app_globals(env)
    env.globals["can"] = lambda cap: True
    return env


class ChecklistMacroTests(unittest.TestCase):
    def render(self, items, complete, total):
        tpl = _env().from_string("{% from 'fm/_checklist.html' import checklist %}{{ checklist(items, complete, total) }}")
        return tpl.render(items=items, complete=complete, total=total)

    def test_progress_text_icons_and_screen_reader_text(self):
        items = [{"label": "Ét", "done": True, "detail": "d1", "url": "/a", "cta": "Gør a"},
                 {"label": "To", "done": False, "detail": "d2", "url": "/b", "cta": "Gør b"}]
        html = self.render(items, 5, 8)
        self.assertIn("5 af 8 punkter", html)
        self.assertIn('aria-valuenow="5"', html)
        self.assertIn("width:62%", html)
        self.assertIn("fa-circle-check", html)
        self.assertIn("Færdig", html)
        self.assertIn("Mangler", html)
        self.assertIn("Gør a", html)
        self.assertIn("Gør b", html)
        self.assertNotIn("✓", html)
        self.assertNotIn("○", html)

    def test_item_without_action_has_no_link_and_zero_total_is_safe(self):
        html = self.render([{"label": "Kun tekst", "done": False}], 0, 0)
        self.assertNotIn("<a ", html)
        self.assertIn("0 af 0 punkter", html)


class ReadinessPageTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        patcher = render_patches()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.db.raw.close)
        self.db.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
        self.db.execute("INSERT INTO users (id,username,email) VALUES (2,'hr','hr@example.invalid')")
        self.db.execute(
            "INSERT INTO company_users (company_id,user_id,username,role,status,department) VALUES (7,2,'hr','hr_manager','active','HR')")
        self.hr = client_as(self.app, user="hr", user_id=2, company_id=7, company_role="hr_manager")

    def test_every_check_has_a_specific_action_label(self):
        result = customer_success.readiness(self.db.connection.cursor(), 7)
        self.assertEqual(len(result["checks"]), 8)
        labels = [c["cta"] for c in result["checks"]]
        self.assertTrue(all(labels))
        self.assertEqual(len(set(labels)), 8, "no two checks share an action label")
        self.assertNotIn("Åbn arbejdsområdet", labels)

    def test_page_shows_progress_cta_labels_and_one_name(self):
        html = self.hr.get("/hr/kom-i-gang").get_data(as_text=True)
        self.assertIn("0 af 8 punkter", html)
        self.assertIn("<title>Kom i gang", html.replace("\n", " ").replace("  ", " "))
        self.assertNotIn("Åbn arbejdsområdet", html)
        self.assertNotIn("Kom godt i gang", html)
        self.assertNotIn("opstartsforløb", html)
        self.assertNotIn("✓", html)
        self.assertNotIn("○", html)
        for check in customer_success.readiness(self.db.connection.cursor(), 7)["checks"]:
            self.assertIn(check["cta"], html)
        self.assertEqual(len(re.findall(r"Mangler: ", html)), 8)

    def test_budget_check_changes_its_action_once_budgets_exist(self):
        before = customer_success.readiness(self.db.connection.cursor(), 7)["checks"][1]
        self.assertEqual(before["cta"], "Opret afdelingsbudgetter")
        self.db.execute(
            "INSERT INTO department_budgets (company_id,department,annual_budget) VALUES (7,'HR',1000)")
        after = customer_success.readiness(self.db.connection.cursor(), 7)["checks"][1]
        self.assertEqual(after["cta"], "Gennemgå godkendelsesregler")


class DashboardCardTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.addCleanup(self.db.raw.close)

    def state(self, complete, total=8, now=None):
        fake = {"complete": complete, "total": total, "checks": []}
        with mock.patch.object(customer_success, "readiness", return_value=fake):
            return customer_success.onboarding_card(self.db.connection.cursor(), 7, now=now)

    def card_html(self, state):
        return _env().get_template("fm/_onboarding_card.html").render(onboarding_card=state)

    def test_card_shows_while_a_check_is_open(self):
        html = self.card_html(self.state(5))
        self.assertIn("Kom i gang", html)
        self.assertIn("5 af 8 punkter er på plads", html)
        self.assertIn("Fortsæt i Kom i gang", html)

    def test_card_is_gone_at_eight_of_eight_and_a_short_note_replaces_it(self):
        html = self.card_html(self.state(8))
        self.assertNotIn("Fortsæt i Kom i gang", html)
        self.assertNotIn("fm-card", html)
        self.assertIn("Opstart gennemført", html)

    def test_note_disappears_after_fourteen_days(self):
        start = datetime.datetime(2026, 10, 1, 9, 0)
        self.assertTrue(self.state(8, now=start)["show_done_note"])
        self.assertTrue(self.state(8, now=start + datetime.timedelta(days=13))["show_done_note"])
        late = self.state(8, now=start + datetime.timedelta(days=14))
        self.assertFalse(late["show_done_note"])
        self.assertFalse(late["show_card"])
        self.assertEqual(self.card_html(late).strip(), "")

    def test_card_returns_and_the_clock_restarts_if_a_check_falls_open(self):
        start = datetime.datetime(2026, 10, 1, 9, 0)
        self.state(8, now=start)
        self.assertTrue(self.state(7, now=start + datetime.timedelta(days=20))["show_card"])
        again = self.state(8, now=start + datetime.timedelta(days=21))
        self.assertTrue(again["show_done_note"])

    def test_card_is_shown_without_data_and_hidden_for_roles_without_the_capability(self):
        self.assertIn("Fortsæt i Kom i gang", self.card_html(None))
        env = _env()
        env.globals["can"] = lambda cap: False
        self.assertEqual(env.get_template("fm/_onboarding_card.html").render(onboarding_card=None).strip(), "")


if __name__ == "__main__":
    unittest.main()
