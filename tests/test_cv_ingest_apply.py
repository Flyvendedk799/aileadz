"""Tests for the interactive-CV ingestion + apply path.

Covers the two regressions found while wiring the 3D CV portal to live data:
  * level-vocabulary round-trip — /api/cv/apply must map BOTH the 3D portal's
    display labels (Begynder/Øvet/…) AND the cv_ingest parser's canonical
    lowercase output (begynder/mellem/…) onto the canonical set the profile DB
    understands. The capitalized-only map previously missed every parsed skill
    and silently inflated it to 'avanceret'.
  * image OCR routing — extract_text() must route image extensions through the
    GPT-4o vision path and degrade gracefully (text='', danish hint) when no
    AI client is available, instead of returning empty with no explanation.

Offline: no OpenAI, no MySQL. Run with the safe env prefix (see
reference_aileadz_local_verify): SANDBOX=1 MYSQL_HOST=127.0.0.1 ... OPENAI_API_KEY=sk-test.
"""
import io
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import api  # noqa: E402
import cv_ingest  # noqa: E402

_SKILL_CANON = {"begynder", "mellem", "avanceret", "ekspert"}
_LANG_CANON = {"begynder", "mellem", "flydende", "modersmaal"}

# What the 3D portal's <select> dropdowns offer the user.
_DISPLAY_LEVELS = ["Begynder", "Øvet", "Erfaren", "Ekspert", "Specialist"]
_DISPLAY_PROF = ["Grundlæggende", "Professionelt", "Flydende", "Modersmål"]
# What cv_ingest.parse_profile_from_text emits.
_PARSER_LEVELS = ["begynder", "mellem", "avanceret", "ekspert"]
_PARSER_PROF = ["begynder", "mellem", "flydende", "modersmaal"]


def _map_skill(v):
    return api._SKILL_LEVEL_MAP.get((v or "").strip().lower(), "mellem")


def _map_lang(v):
    return api._LANG_PROF_MAP.get((v or "").strip().lower(), "mellem")


class SkillLevelMapTests(unittest.TestCase):
    def test_display_labels_map_to_canonical(self):
        for lvl in _DISPLAY_LEVELS:
            self.assertIn(_map_skill(lvl), _SKILL_CANON, f"{lvl!r} -> not canonical")

    def test_parser_canonical_passes_through(self):
        # The regression: lowercase parser output must NOT fall to the default.
        for lvl in _PARSER_LEVELS:
            self.assertEqual(_map_skill(lvl), lvl)

    def test_beginner_not_inflated_to_advanced(self):
        # The exact symptom of the old bug.
        self.assertEqual(_map_skill("begynder"), "begynder")
        self.assertEqual(_map_skill("Begynder"), "begynder")

    def test_intermediate_synonyms(self):
        self.assertEqual(_map_skill("Øvet"), "mellem")
        self.assertEqual(_map_skill("mellem"), "mellem")
        self.assertEqual(_map_skill("intermediate"), "mellem")


class LanguageProfMapTests(unittest.TestCase):
    def test_display_labels_map_to_canonical(self):
        for p in _DISPLAY_PROF:
            self.assertIn(_map_lang(p), _LANG_CANON, f"{p!r} -> not canonical")

    def test_parser_canonical_passes_through(self):
        for p in _PARSER_PROF:
            self.assertEqual(_map_lang(p), p)

    def test_professional_is_fluent(self):
        self.assertEqual(_map_lang("Professionelt"), "flydende")

    def test_native_variants(self):
        self.assertEqual(_map_lang("Modersmål"), "modersmaal")
        self.assertEqual(_map_lang("modersmaal"), "modersmaal")


class _FakeUpload:
    """Minimal werkzeug FileStorage stand-in for extract_text()."""
    def __init__(self, filename, data):
        self.filename = filename
        self.stream = io.BytesIO(data)


class ExtractImageRoutingTests(unittest.TestCase):
    def test_image_routes_to_vision_path(self):
        captured = {}

        def fake_vision(raw, mime="image/jpeg"):
            captured["mime"] = mime
            return "Navn: Test Person\nKompetencer: Python", ""

        with mock.patch.object(cv_ingest, "_extract_image_text", side_effect=fake_vision):
            text, hint = cv_ingest.extract_text(_FakeUpload("cv.png", b"\x89PNG fake bytes"))
        self.assertEqual(captured.get("mime"), "image/png")
        self.assertIn("Python", text)
        self.assertEqual(hint, "")

    def test_jpeg_mime_resolved(self):
        captured = {}

        def fake_vision(raw, mime="image/jpeg"):
            captured["mime"] = mime
            return "x", ""

        with mock.patch.object(cv_ingest, "_extract_image_text", side_effect=fake_vision):
            cv_ingest.extract_text(_FakeUpload("scan.JPEG", b"\xff\xd8\xff fake"))
        self.assertEqual(captured.get("mime"), "image/jpeg")

    def test_image_degrades_gracefully_without_client(self):
        # No AI client -> empty text + a non-empty Danish hint (never raises).
        with mock.patch.object(cv_ingest, "_get_openai_client", return_value=None):
            text, hint = cv_ingest.extract_text(_FakeUpload("cv.jpg", b"\xff\xd8\xff fake jpeg"))
        self.assertEqual(text, "")
        self.assertTrue(hint)


if __name__ == "__main__":
    unittest.main()
