"""Offline coverage for the edit-everywhere backend helpers (no Flask/DB).

These pin the validation/normalization the profile edit + CV-apply paths rely on:
skill-level validation parity, experience date sanity, and the no-op guard on the
new link update (so an empty edit can't issue a stray UPDATE).
"""
import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app1.user_profile_db import (  # noqa: E402
    _coerce_year, _coerce_skill_level, is_valid_skill_level, update_portfolio_link,
)


class YearCoercion(unittest.TestCase):
    def test_numeric_strings_and_ints(self):
        self.assertEqual(_coerce_year("2020"), 2020)
        self.assertEqual(_coerce_year(2024), 2024)

    def test_blank_and_garbage_become_none(self):
        for v in ("", None, "abc", "20xx"):
            self.assertIsNone(_coerce_year(v))


class SkillLevelValidation(unittest.TestCase):
    def test_valid(self):
        for lvl in ("begynder", "mellem", "avanceret", "ekspert", "Avanceret", "EKSPERT"):
            self.assertTrue(is_valid_skill_level(lvl))

    def test_invalid(self):
        for lvl in ("", None, "expert", "guru", "5"):
            self.assertFalse(is_valid_skill_level(lvl))

    def test_coerce_defaults_to_mellem(self):
        self.assertEqual(_coerce_skill_level("guru"), "mellem")
        self.assertEqual(_coerce_skill_level("Ekspert"), "ekspert")


class LinkUpdateNoOp(unittest.TestCase):
    def test_empty_update_returns_false_without_db(self):
        # No fields supplied → must short-circuit to False BEFORE touching the DB
        # (so the test runs with no MySQL, and a stray UPDATE is never issued).
        self.assertFalse(update_portfolio_link("u", 1))

    def test_blank_url_returns_false(self):
        self.assertFalse(update_portfolio_link("u", 1, url=""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
