"""Learner home recommendations: an honest header and a grid that fits a phone."""

import os
import re
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

import futurematch_ui as fm  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402
from tests.test_first_run import _client  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rec(source, why):
    return {"handle": "kursus-1", "title": "Førstehjælpsråd for ledere", "vendor": "Leverandør", "price_min": 4500.0,
            "price_label": None, "format": "Klassekursus", "why": why, "source": source}


class RecommendationSourceTests(unittest.TestCase):
    def _search(self, by_query):
        def search(filters=None, **kw):
            return {"products": by_query.get((filters or {}).get("q", ""), [])}
        return search

    def _build(self, profile, by_query):
        import catalog_service
        app = mock.MagicMock()
        with mock.patch.object(catalog_service, "search_products", side_effect=self._search(by_query)), \
                mock.patch.object(catalog_service, "exclude_stale", side_effect=lambda p: p), \
                mock.patch.object(fm, "current_app", app):
            return fm._home_recommendations(profile, 7)

    def test_empty_profile_is_popular(self):
        recs = self._build({}, {"": [{"handle": "a", "title": "A"}]})
        self.assertEqual(fm.recommendation_source(recs), "popular")
        self.assertEqual(recs[0]["why"], "Populært i kataloget lige nu")

    def test_skill_match_is_profile_based(self):
        recs = self._build({"skills": [{"name": "Ledelse"}]}, {"Ledelse": [{"handle": "a", "title": "A"}]})
        self.assertEqual(fm.recommendation_source(recs), "profile")
        self.assertIn("Ledelse", recs[0]["why"])

    def test_skill_without_hits_falls_back_to_popular(self):
        recs = self._build({"skills": [{"name": "Ledelse"}]}, {"": [{"handle": "a", "title": "A"}]})
        self.assertEqual(fm.recommendation_source(recs), "popular")
        self.assertEqual(recs[0]["why"], "Populært i kataloget lige nu")

    def test_no_recommendations_is_popular(self):
        self.assertEqual(fm.recommendation_source([]), "popular")


class HomeHeaderTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.execute("INSERT INTO users (id, username, email) VALUES (1, 'ada', 'ada@x.dk')")
        self.client = _client(self.db, user="ada", user_id=1, company_id=7, company_role="employee")
        patcher = mock.patch("white_label_global_integration.get_template_context", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def home(self, recs):
        with mock.patch("futurematch_ui._home_recommendations", return_value=recs):
            return self.client.get("/min-laering").get_data(as_text=True)

    def test_empty_profile_never_says_matched_to_your_profile(self):
        html = self.home([_rec("popular", "Populært i kataloget lige nu")])
        self.assertIn("Populært lige nu", html)
        self.assertIn("Udfyld din profil for personlige forslag", html)
        self.assertNotIn("Matchet til din profil", html)
        self.assertNotIn("Anbefalet til dig", html)

    def test_profile_based_recommendations_keep_the_personal_header(self):
        html = self.home([_rec("profile", "Matcher din kompetence: Ledelse")])
        self.assertIn("Matchet til din profil", html)
        self.assertIn("Anbefalet til dig", html)
        self.assertNotIn("Udfyld din profil for personlige forslag", html)

    def test_grid_uses_the_responsive_class_not_an_inline_three_columns(self):
        html = self.home([_rec("popular", "x")])
        self.assertIn("fm-auto-grid", html)
        self.assertNotIn("repeat(3,1fr)", html)


class GridCssTests(unittest.TestCase):
    def test_auto_grid_class_adapts_and_is_one_column_on_phones(self):
        css = open(os.path.join(ROOT, "static", "futurematch", "assets", "fm-pages.css"), encoding="utf-8").read()
        self.assertRegex(css, r"\.fm-auto-grid\s*\{[^}]*repeat\(auto-fill,\s*minmax\(220px,\s*1fr\)\)")
        self.assertRegex(css, r"@media \(max-width: 600px\)\s*\{\s*\.fm-auto-grid\s*\{\s*grid-template-columns:\s*1fr")
        self.assertIn("overflow-wrap: anywhere", css)

    def test_no_learner_template_uses_a_fixed_three_column_inline_grid(self):
        for name in ("employee_home.html", "categories.html", "my_profile.html"):
            src = open(os.path.join(ROOT, "templates", "fm", name), encoding="utf-8").read()
            self.assertFalse(re.search(r'style="[^"]*repeat\(3,\s*1fr\)', src), name)

    def test_price_stays_on_one_line(self):
        src = open(os.path.join(ROOT, "templates", "fm", "employee_home.html"), encoding="utf-8").read()
        self.assertRegex(src, r"\.rec-price\s*\{[^}]*white-space:\s*nowrap")


if __name__ == "__main__":
    unittest.main()
