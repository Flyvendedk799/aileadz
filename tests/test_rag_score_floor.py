"""RG-01 — relative-to-top retrieval score floor + below-threshold backoff.

Covers the pure helper ``app1.rag._apply_score_floor`` plus two offline
integration paths through ``semantic_search_courses_detailed`` (all I/O seams
patched — no MySQL, no OpenAI):

  * RRF-fused (BM25+vector) lists use a RELATIVE floor max(0.02, top*0.25).
    The legacy absolute 0.12 floor silently emptied every fused result
    (RRF scores max out ≈0.03-0.05) — the Danish-paraphrase recall hole.
  * Single-source lists (cosine/BM25 scale) keep the legacy absolute 0.12.
  * Pure-semantic queries (zero lexical hits) retain the top 3 candidates.
  * When the floor filters EVERYTHING, the best 2 pre-filter candidates come
    back marked below_threshold=True with confidence='low' instead of nothing.
  * The dead ``min_score`` parameter is gone from both public signatures.
"""
import inspect
import unittest
from unittest.mock import patch

import app1.rag as rag
from app1.rag import (
    _apply_score_floor,
    _BELOW_THRESHOLD_KEEP,
    _MIN_COMBINED_SCORE,
    _PURE_SEMANTIC_KEEP,
    _RRF_FLOOR_MIN,
    semantic_search_courses,
    semantic_search_courses_detailed,
)


class ApplyScoreFloorUnitTests(unittest.TestCase):
    """Pure-helper behaviour on synthetic (handle, score) lists."""

    def test_empty_input_returns_empty_without_backoff(self):
        kept, meta = _apply_score_floor([], both_sources=True)
        self.assertEqual(kept, [])
        self.assertFalse(meta["below_threshold"])
        self.assertEqual(meta["filtered_below_threshold"], 0)

    def test_rrf_scale_top_0035_keeps_results_instead_of_empty(self):
        # REGRESSION: legacy absolute floor 0.12 dropped ALL of these. On the
        # RRF scale a top of 0.035 is a perfectly good fused result.
        candidates = [("kursus-a", 0.035), ("kursus-b", 0.024), ("kursus-c", 0.006)]
        kept, meta = _apply_score_floor(candidates, both_sources=True)
        # floor = max(0.02, 0.035*0.25=0.00875) = 0.02 → a+b survive, c filtered
        self.assertEqual([h for h, _ in kept], ["kursus-a", "kursus-b"])
        self.assertAlmostEqual(meta["floor"], _RRF_FLOOR_MIN)
        self.assertEqual(meta["filtered_below_threshold"], 1)
        self.assertFalse(meta["below_threshold"])

    def test_rrf_scale_relative_floor_scales_with_strong_top(self):
        # Title/phrase bonuses push fused scores well above raw RRF — the
        # floor must scale with the top instead of staying at the hard bottom.
        candidates = [("kursus-a", 0.20), ("kursus-b", 0.06), ("kursus-c", 0.03)]
        kept, meta = _apply_score_floor(candidates, both_sources=True)
        # floor = max(0.02, 0.20*0.25=0.05) → a+b survive, c filtered
        self.assertEqual([h for h, _ in kept], ["kursus-a", "kursus-b"])
        self.assertAlmostEqual(meta["floor"], 0.05)
        self.assertEqual(meta["filtered_below_threshold"], 1)

    def test_single_source_cosine_scale_keeps_legacy_absolute_floor(self):
        # Single-source lists (vector-only / BM25-only) behave exactly as before.
        candidates = [("kursus-a", 0.71), ("kursus-b", 0.30), ("kursus-c", 0.10)]
        kept, meta = _apply_score_floor(candidates, both_sources=False)
        self.assertEqual([h for h, _ in kept], ["kursus-a", "kursus-b"])
        self.assertAlmostEqual(meta["floor"], _MIN_COMBINED_SCORE)
        self.assertEqual(meta["filtered_below_threshold"], 1)
        self.assertFalse(meta["below_threshold"])

    def test_pure_semantic_retains_top3_regardless_of_floor(self):
        # Danish paraphrase: zero lexical hits, all scores below the floor —
        # the top 3 vector candidates must still come through.
        candidates = [("a", 0.10), ("b", 0.09), ("c", 0.08), ("d", 0.05)]
        kept, meta = _apply_score_floor(candidates, both_sources=False,
                                        pure_semantic=True)
        self.assertEqual([h for h, _ in kept], ["a", "b", "c"])
        self.assertEqual(len(kept), _PURE_SEMANTIC_KEEP)
        self.assertEqual(meta["filtered_below_threshold"], 1)
        self.assertFalse(meta["below_threshold"])

    def test_all_filtered_backs_off_to_top2_with_below_threshold(self):
        # Floor removes everything → graceful backoff instead of empty result.
        candidates = [("a", 0.015), ("b", 0.010), ("c", 0.004)]
        kept, meta = _apply_score_floor(candidates, both_sources=True)
        self.assertEqual([h for h, _ in kept], ["a", "b"])
        self.assertEqual(len(kept), _BELOW_THRESHOLD_KEEP)
        self.assertTrue(meta["below_threshold"])
        self.assertEqual(meta["filtered_below_threshold"], 1)

    def test_single_source_backoff_also_applies(self):
        candidates = [("a", 0.08), ("b", 0.05)]
        kept, meta = _apply_score_floor(candidates, both_sources=False)
        self.assertEqual([h for h, _ in kept], ["a", "b"])
        self.assertTrue(meta["below_threshold"])

    def test_dead_min_score_parameter_removed(self):
        for fn in (semantic_search_courses, semantic_search_courses_detailed):
            self.assertNotIn("min_score", inspect.signature(fn).parameters,
                             f"{fn.__name__} still exposes the dead min_score param")


def _fake_products():
    return [
        {"handle": "kursus-excel", "title": "Avanceret Excel", "vendor": "Udbyder A",
         "tags": [], "variants": []},
        {"handle": "kursus-python", "title": "Python for begyndere", "vendor": "Udbyder B",
         "tags": [], "variants": []},
    ]


class ScoreFloorIntegrationTests(unittest.TestCase):
    """semantic_search_courses_detailed with all I/O seams patched (offline)."""

    def setUp(self):
        rag._search_cache.clear()

    def test_rrf_fused_result_survives_floor(self):
        # Both legs return hits → RRF scores ≈ 2/61 ≈ 0.033. The legacy 0.12
        # absolute floor returned ZERO products here; the relative floor keeps them.
        with patch.object(rag, "load_augmented_products", return_value=_fake_products()), \
             patch.object(rag, "_bm25_search", return_value=[(0, 4.0), (1, 3.0)]), \
             patch.object(rag, "_bm25_confident_enough", return_value=False), \
             patch.object(rag, "get_query_embedding", return_value=[0.1] * 8), \
             patch.object(rag, "_vector_search", return_value=[(0, 0.50), (1, 0.40)]), \
             patch.object(rag, "_title_token_sets", None), \
             patch.object(rag, "_should_run_cross_encoder", return_value=False):
            result = semantic_search_courses_detailed("digitale regneark superbruger", limit=5)

        self.assertNotIn("error", result)
        handles = [p["handle"] for p in result["products"]]
        self.assertIn("kursus-excel", handles)
        self.assertFalse(result["below_threshold"])
        self.assertFalse(result["debug"]["below_threshold"])
        self.assertGreater(result["debug"]["score_floor"], 0)

    def test_floor_filtered_search_returns_below_threshold_items(self):
        # Single-source BM25-only path with weak scores: legacy behaviour was an
        # empty product list. Now: top 2 pre-filter candidates, confidence='low',
        # below_threshold flagged on result, debug and per-candidate explain.
        with patch.object(rag, "load_augmented_products", return_value=_fake_products()), \
             patch.object(rag, "_bm25_search", return_value=[(0, 0.05), (1, 0.03)]), \
             patch.object(rag, "_bm25_confident_enough", return_value=True), \
             patch.object(rag, "_title_token_sets", None), \
             patch.object(rag, "_should_run_cross_encoder", return_value=False):
            result = semantic_search_courses_detailed("kompetenceudvikling tabelværktøj", limit=5)

        self.assertNotIn("error", result)
        self.assertEqual(len(result["products"]), 2)
        self.assertTrue(result["below_threshold"])
        self.assertEqual(result["confidence"], "low")
        self.assertTrue(result["debug"]["below_threshold"])
        for selected in result["debug"]["selected"]:
            self.assertTrue(selected["explain"].get("below_threshold"),
                            f"explain mangler below_threshold: {selected}")


if __name__ == "__main__":
    unittest.main()
