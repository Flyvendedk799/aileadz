"""Unit tests for the user-memory + profile-completeness logic that powers the
AI Profiler and Mind-Map (branch feature/ai-profiler-mindmap).

These pin the *hardened* behaviour added during the pre-merge review:
  * select_relevant_memories tags each result _relevant (keyword match) vs the
    recency fallback, drops sub-threshold confidence, and caps the fallback — so
    used_count stays meaningful and normal chat isn't polluted by low-confidence
    inferences.
  * profile_completeness scores the 8 CV sections the profiler drives to 100%.
  * the relevance tokenizer + AI formatter behave on Danish text.

All pure-function / fake-DB — no Flask boot, no live MySQL. app1.user_profile_db
imports flask.current_app + MySQLdb at import time, so we stub both first
(mirrors tests/test_rolling_summary.py).
"""
import importlib.util
import os
import sys
import types
import unittest


# ── Stub MySQLdb.cursors so importing user_profile_db succeeds without the driver.
if "MySQLdb" not in sys.modules:
    _mysqldb = types.ModuleType("MySQLdb")
    _cursors = types.ModuleType("MySQLdb.cursors")
    _cursors.DictCursor = object
    _mysqldb.cursors = _cursors
    sys.modules["MySQLdb"] = _mysqldb
    sys.modules["MySQLdb.cursors"] = _cursors

if "db_compat" not in sys.modules:
    _db_compat = types.ModuleType("db_compat")
    _db_compat.refresh_flask_mysql_connection = lambda *a, **k: None
    sys.modules["db_compat"] = _db_compat


def _load_user_profile_db():
    if "app1" not in sys.modules:
        pkg = types.ModuleType("app1")
        pkg.__path__ = [os.path.join(os.path.dirname(__file__), "..", "app1")]
        sys.modules["app1"] = pkg
    mod_name = "app1.user_profile_db"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    path = os.path.join(os.path.dirname(__file__), "..", "app1", "user_profile_db.py")
    spec = importlib.util.spec_from_file_location(mod_name, os.path.abspath(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


DB = _load_user_profile_db()


def _mem(id, label, detail="", confidence=0.9, category="andet"):
    return {"id": id, "label": label, "detail": detail, "category": category,
            "confidence": confidence, "used_count": 0}


class ProfileCompletenessTests(unittest.TestCase):
    def test_partial_profile_scores_and_lists_missing(self):
        p = {"headline": "Projektleder", "skills": [{"name": "Python"}],
             "experience": [], "education": [], "certifications": [],
             "languages": [], "goals": "", "learning_goals": [], "preferred_format": ""}
        c = DB.profile_completeness("u", profile=p)
        self.assertEqual(c["total"], 8)
        self.assertEqual(c["done"], 2)            # headline + skills
        self.assertEqual(c["pct"], 25)
        self.assertIn("Erfaring", c["missing"])
        self.assertNotIn("Kompetencer", c["missing"])

    def test_full_profile_is_100(self):
        p = {"headline": "x", "skills": [{"name": "s"}], "experience": [{"id": 1}],
             "education": [{"id": 1}], "certifications": [{"id": 1}],
             "languages": [{"id": 1}], "goals": "blive bedre",
             "learning_goals": [], "preferred_format": "online"}
        c = DB.profile_completeness("u", profile=p)
        self.assertEqual(c["pct"], 100)
        self.assertEqual(c["missing"], [])

    def test_goals_satisfied_by_learning_goals(self):
        p = {"headline": "", "skills": [], "experience": [], "education": [],
             "certifications": [], "languages": [], "goals": "",
             "learning_goals": [{"id": 1, "title": "PMP"}], "preferred_format": ""}
        c = DB.profile_completeness("u", profile=p)
        self.assertNotIn("Karrieremål", c["missing"])


class MemoryTokenizerTests(unittest.TestCase):
    def test_strips_stopwords_and_short_tokens(self):
        t = DB._memory_tokens("Jeg foretrækker aftenundervisning i København")
        self.assertIn("aftenundervisning", t)
        self.assertIn("københavn".replace("ø", "ø"), {x for x in t})  # token present
        self.assertNotIn("i", t)       # stopword
        self.assertNotIn("jeg", t)     # stopword

    def test_empty_input(self):
        self.assertEqual(DB._memory_tokens(""), set())
        self.assertEqual(DB._memory_tokens(None), set())


class FormatMemoriesTests(unittest.TestCase):
    def test_formats_with_category_label_and_detail(self):
        txt = DB.format_memories_for_ai([
            {"category": "praeference", "label": "Foretrækker aften", "detail": "pga børn"}])
        self.assertIn("Præference", txt)
        self.assertIn("Foretrækker aften", txt)
        self.assertIn("pga børn", txt)

    def test_empty_returns_blank(self):
        self.assertEqual(DB.format_memories_for_ai([]), "")
        self.assertEqual(DB.format_memories_for_ai(None), "")


class SelectRelevantMemoriesTests(unittest.TestCase):
    def setUp(self):
        self._orig = DB.get_memories

    def tearDown(self):
        DB.get_memories = self._orig

    def _patch(self, rows):
        DB.get_memories = lambda username, limit=None: list(rows)

    def test_keyword_match_is_tagged_relevant(self):
        self._patch([_mem(1, "Foretrækker aftenundervisning", "pga børn"),
                     _mem(2, "Skifter til data science")])
        r = DB.select_relevant_memories("u", "Jeg vil gerne have aftenundervisning")
        self.assertTrue(any(m["id"] == 1 and m["_relevant"] for m in r))

    def test_low_confidence_is_dropped(self):
        self._patch([_mem(1, "aftenundervisning", confidence=0.2)])
        r = DB.select_relevant_memories("u", "aftenundervisning tak")
        self.assertEqual(r, [])

    def test_no_overlap_falls_back_non_relevant_capped(self):
        rows = [_mem(i, f"emne nummer {i}") for i in range(1, 9)]
        self._patch(rows)
        r = DB.select_relevant_memories("u", "noget helt andet xyz")
        self.assertTrue(r)
        self.assertTrue(all(m["_relevant"] is False for m in r))
        self.assertLessEqual(len(r), 3)

    def test_empty_store_returns_empty(self):
        self._patch([])
        self.assertEqual(DB.select_relevant_memories("u", "anything"), [])

    def test_respects_limit(self):
        rows = [_mem(i, f"python kursus {i}") for i in range(1, 12)]
        self._patch(rows)
        r = DB.select_relevant_memories("u", "python", limit=4)
        self.assertLessEqual(len(r), 4)


if __name__ == "__main__":
    unittest.main()
