"""Phase 11: memory dedup/supersede logic in _execute_remember_about_user.

Tests the _normalize_memory_label and _find_supersedable_memory helpers, plus
the full executor path with mocked DB (no live MySQL). The dedup pass is
best-effort — a DB failure inside it must fall through to a normal add_memory.
"""
import json
import unittest
from unittest import mock


# Import the helpers directly from tools (SANDBOX env is set by the test runner)
import os
import sys
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

_SAFE_ENV = {
    "SANDBOX": "1",
    "AI_WARMUP_ON_IMPORT": "0",
    "SCHEDULER_OPPORTUNISTIC": "0",
    "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": "3306",
    "MYSQL_USER": "none", "MYSQL_PASSWORD": "none", "MYSQL_DB": "none",
    "OPENAI_API_KEY": "sk-test",
}
for k, v in _SAFE_ENV.items():
    os.environ.setdefault(k, v)

import app1.tools as tools


class NormalizeLabelTests(unittest.TestCase):
    def test_lowercase_and_strip(self):
        self.assertEqual(tools._normalize_memory_label("  Python Basis  "), "python basis")

    def test_collapses_spaces(self):
        self.assertEqual(tools._normalize_memory_label("er  meget   interesseret"), "er meget interesseret")

    def test_empty_string(self):
        self.assertEqual(tools._normalize_memory_label(""), "")


class FindSupersedableTests(unittest.TestCase):
    def _mem(self, id_, label):
        return {"id": id_, "label": label}

    def test_exact_match_returns_id(self):
        existing = [self._mem(1, "Interesseret i Python")]
        sup_id, sup_label = tools._find_supersedable_memory(
            existing, "interesseret i python"
        )
        self.assertEqual(sup_id, 1)

    def test_old_is_substring_of_new_supersedes(self):
        # "Python" is contained in "Python kurser og workshops"
        existing = [self._mem(2, "Python")]
        sup_id, _ = tools._find_supersedable_memory(
            existing, "python kurser og workshops"
        )
        self.assertEqual(sup_id, 2)

    def test_new_is_substring_of_old_supersedes_when_close_in_length(self):
        # "Interesseret i python" is ≥80% of "Interesseret i python kursus" length
        existing = [self._mem(3, "Interesseret i python kursus")]
        sup_id, _ = tools._find_supersedable_memory(
            existing, "interesseret i python"
        )
        self.assertEqual(sup_id, 3)

    def test_unrelated_labels_not_superseded(self):
        existing = [self._mem(4, "Foretrækker online kurser")]
        sup_id, _ = tools._find_supersedable_memory(
            existing, "interesseret i python"
        )
        self.assertIsNone(sup_id)

    def test_short_label_skipped(self):
        existing = [self._mem(5, "OK")]
        sup_id, _ = tools._find_supersedable_memory(existing, "ok lidt mere tekst")
        self.assertIsNone(sup_id)


class ExecuteRememberDeduplicatesTests(unittest.TestCase):
    def _memories(self, label):
        return [{"id": 7, "label": label, "category": "andet"}]

    def test_new_supersedes_shorter_existing(self):
        """When new label extends an existing one, update_memory is called."""
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_memories",
                           return_value=self._memories("Python")), \
                mock.patch("app1.user_profile_db.update_memory") as upd, \
                mock.patch("app1.user_profile_db.add_memory") as add:
            out = json.loads(tools._execute_remember_about_user(
                {"label": "Python kurser og projekter", "category": "interesse"},
                username="eva"
            ))
        self.assertEqual(out["status"], "memory_saved")
        upd.assert_called_once()
        add.assert_not_called()
        # The update should set the new label
        _, kwargs = upd.call_args
        self.assertEqual(kwargs.get("label"), "Python kurser og projekter")

    def test_exact_duplicate_label_falls_through_to_add_memory(self):
        """Exact duplicate: _find_supersedable returns (id, same_label) → add_memory."""
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_memories",
                           return_value=self._memories("Python")), \
                mock.patch("app1.user_profile_db.update_memory") as upd, \
                mock.patch("app1.user_profile_db.add_memory") as add:
            out = json.loads(tools._execute_remember_about_user(
                {"label": "Python", "category": "interesse"},
                username="eva"
            ))
        self.assertEqual(out["status"], "memory_saved")
        # Exact match → sup_label == label → fall through to add_memory
        add.assert_called_once()
        upd.assert_not_called()

    def test_unrelated_label_goes_to_add_memory(self):
        """No near-duplicate → normal add_memory path."""
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_memories", return_value=[]), \
                mock.patch("app1.user_profile_db.add_memory") as add:
            out = json.loads(tools._execute_remember_about_user(
                {"label": "Foretrækker online kurser"},
                username="eva"
            ))
        self.assertEqual(out["status"], "memory_saved")
        add.assert_called_once()

    def test_dedup_failure_falls_through(self):
        """If get_memories raises, dedup is skipped and add_memory is still called."""
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_memories",
                           side_effect=RuntimeError("db down")), \
                mock.patch("app1.user_profile_db.add_memory") as add:
            out = json.loads(tools._execute_remember_about_user(
                {"label": "Interesseret i projektledelse"},
                username="eva"
            ))
        self.assertEqual(out["status"], "memory_saved")
        add.assert_called_once()


if __name__ == "__main__":
    unittest.main()
