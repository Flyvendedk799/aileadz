"""cv_parse_store — the durable cross-worker CV parse-job store.

Offline these exercise the in-process fallback (no app context → no MySQL), which
is the same code path tests/anonymous boots use. The DB path is structurally
identical to app1/confirm_store (already covered by test_tool_confirm).

Also pins the cross-layer contract that every CV-apply level mapping lands on a
value the profile DB's skill-level validator accepts — so a parsed/portal level
can never silently fail the ENUM insert.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import api  # noqa: E402
import cv_parse_store  # noqa: E402
from app1.user_profile_db import is_valid_skill_level  # noqa: E402


class CvParseStoreRoundTrip(unittest.TestCase):
    def setUp(self):
        cv_parse_store.clear_all()

    def test_running_then_done_roundtrip(self):
        sid = "sess-roundtrip"
        self.assertIsNone(cv_parse_store.read(sid))      # nothing yet
        cv_parse_store.start(sid)
        self.assertIsNone(cv_parse_store.read(sid))      # running ≠ done
        cv_parse_store.finish(sid, {"proposal": {"skills": [{"name": "Python"}]}, "hint": ""})
        got = cv_parse_store.read(sid)
        self.assertTrue(got and got["proposal"]["skills"])
        cv_parse_store.discard(sid)
        self.assertIsNone(cv_parse_store.read(sid))      # consumed

    def test_error_payload_roundtrips(self):
        sid = "sess-error"
        cv_parse_store.start(sid)
        cv_parse_store.finish(sid, {"error": "boom", "proposal": {}})
        got = cv_parse_store.read(sid)
        self.assertEqual(got.get("error"), "boom")

    def test_start_clears_previous_result(self):
        sid = "sess-reuse"
        cv_parse_store.finish(sid, {"proposal": {"skills": [1]}, "hint": ""})
        cv_parse_store.start(sid)              # a new parse for the same id
        self.assertIsNone(cv_parse_store.read(sid))

    def test_unknown_session_is_none(self):
        self.assertIsNone(cv_parse_store.read("never-seen"))


class CvApplyLevelContract(unittest.TestCase):
    """Every level the CV-apply maps produce must be a valid skill level."""

    def test_all_mapped_skill_levels_are_valid(self):
        for canonical in set(api._SKILL_LEVEL_MAP.values()):
            self.assertTrue(is_valid_skill_level(canonical),
                            f"CV-apply maps to invalid skill level: {canonical!r}")

    def test_validator_rejects_garbage(self):
        self.assertFalse(is_valid_skill_level("expert"))   # English, not the enum
        self.assertFalse(is_valid_skill_level(""))
        self.assertTrue(is_valid_skill_level("Avanceret"))  # case-insensitive


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
