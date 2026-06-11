"""RG-02 — RAG search-cache + diagnostics + expansion-hoist robustness.

Covers three verified bugs in ``app1/rag.py`` (all tests fully offline — no
MySQL, no OpenAI; every I/O seam patched like tests/test_rag_score_floor.py):

  * Search-cache hits never re-applied the shown_handles penalty (the code
    comment promised it; the code didn't), so "vis mig flere" inside the 1h
    TTL replayed the exact same list. The cache now stores SESSION-NEUTRAL
    ranked candidates; every hit re-applies the −0.04 penalty, re-sorts and
    re-slices.
  * Cached products were shared by reference — mutating a returned product
    corrupted every later hit's payload. Both miss and hit paths now return
    decoupled copies.
  * ``cross_encoder_applied`` was re-derived post-hoc and could disagree with
    what actually happened, corrupting AI_RERANK_AMBIGUITY_MARGIN tuning data.
    It is now recorded at the actual rerank call site.
  * ``_expand_query_tokens`` (fuzzywuzzy scans) ran inside the per-product
    loop of ``hybrid_rank_products`` — now hoisted, called exactly once per
    query, with unchanged ordering.
"""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import app1.rag as rag
from app1.rag import (
    _SHOWN_HANDLE_PENALTY,
    hybrid_rank_products,
    semantic_search_courses_detailed,
)


def _fake_products():
    return [
        {"handle": "kursus-a", "title": "Excel grundkursus", "vendor": "Udbyder A",
         "tags": [], "variants": []},
        {"handle": "kursus-b", "title": "Excel videregående", "vendor": "Udbyder B",
         "tags": [], "variants": []},
        {"handle": "kursus-c", "title": "Excel for økonomer", "vendor": "Udbyder C",
         "tags": [], "variants": []},
    ]


class SearchCacheShownHandlesTests(unittest.TestCase):
    """Cache hits must re-apply the session-specific already-shown penalty."""

    def setUp(self):
        rag._search_cache.clear()

    def _enter_patches(self, stack, products):
        """Standard offline seams: BM25-only single-source path, scores bunched
        tightly so the −0.04 penalty actually reorders (0.20/0.19/0.18, all
        above the absolute 0.12 floor)."""
        stack.enter_context(patch.object(
            rag, "load_augmented_products", return_value=products))
        bm25_mock = stack.enter_context(patch.object(
            rag, "_bm25_search", return_value=[(0, 0.20), (1, 0.19), (2, 0.18)]))
        stack.enter_context(patch.object(
            rag, "_bm25_confident_enough", return_value=True))
        stack.enter_context(patch.object(rag, "_title_token_sets", None))
        stack.enter_context(patch.object(
            rag, "_should_run_cross_encoder", return_value=False))
        return bm25_mock

    def test_cache_hit_demotes_shown_handle(self):
        # REGRESSION: same query twice within the TTL — the second call carries
        # shown_handles and MUST demote the already-shown course on the cache hit.
        with ExitStack() as stack:
            bm25_mock = self._enter_patches(stack, _fake_products())
            first = semantic_search_courses_detailed("regnearksanalyse", limit=3)
            second = semantic_search_courses_detailed(
                "regnearksanalyse", limit=3, shown_handles={"kursus-a"})

        self.assertNotIn("error", first)
        self.assertFalse(first["debug"].get("cache_hit"))
        self.assertEqual([p["handle"] for p in first["products"]],
                         ["kursus-a", "kursus-b", "kursus-c"])

        # Second call was served from cache (pipeline ran exactly once)…
        self.assertTrue(second["debug"]["cache_hit"])
        self.assertEqual(bm25_mock.call_count, 1)
        # …but the shown handle is demoted: 0.20−0.04=0.16 < 0.18 < 0.19.
        self.assertEqual([p["handle"] for p in second["products"]],
                         ["kursus-b", "kursus-c", "kursus-a"])
        self.assertEqual(second["debug"]["shown_handles_count"], 1)

        # The penalty is visible in the per-candidate explain.
        selected = {s["handle"]: s for s in second["debug"]["selected"]}
        self.assertAlmostEqual(
            selected["kursus-a"]["explain"]["shown_penalty"], _SHOWN_HANDLE_PENALTY)
        self.assertIn("already_shown", selected["kursus-a"]["explain"]["pref_reasons"])
        self.assertAlmostEqual(selected["kursus-b"]["explain"]["shown_penalty"], 0.0)
        self.assertNotIn("already_shown", selected["kursus-b"]["explain"]["pref_reasons"])

    def test_cache_stores_neutral_scores_so_penalty_does_not_stick(self):
        # First call WITH shown_handles bakes the penalty into its own ranking,
        # but the cache must store neutral scores: a later hit WITHOUT
        # shown_handles gets the un-penalised ordering back.
        with ExitStack() as stack:
            self._enter_patches(stack, _fake_products())
            first = semantic_search_courses_detailed(
                "regnearksanalyse", limit=3, shown_handles={"kursus-a"})
            second = semantic_search_courses_detailed("regnearksanalyse", limit=3)

        self.assertEqual([p["handle"] for p in first["products"]],
                         ["kursus-b", "kursus-c", "kursus-a"])
        self.assertTrue(second["debug"]["cache_hit"])
        self.assertEqual([p["handle"] for p in second["products"]],
                         ["kursus-a", "kursus-b", "kursus-c"])
        self.assertEqual(second["debug"]["shown_handles_count"], 0)

    def test_mutating_returned_products_does_not_corrupt_later_hits(self):
        products = _fake_products()
        with ExitStack() as stack:
            self._enter_patches(stack, products)
            first = semantic_search_courses_detailed("regnearksanalyse", limit=3)
            first["products"][0]["title"] = "MUTERET AF KALDER"
            first["products"][0]["vendor"] = "MUTERET"

            second = semantic_search_courses_detailed("regnearksanalyse", limit=3)
            self.assertTrue(second["debug"]["cache_hit"])
            self.assertEqual(second["products"][0]["title"], "Excel grundkursus")
            self.assertEqual(second["products"][0]["vendor"], "Udbyder A")

            # Mutating a HIT payload must not corrupt the next hit either —
            # including the penalty-re-ranked path.
            second["products"][0]["title"] = "MUTERET IGEN"
            third = semantic_search_courses_detailed(
                "regnearksanalyse", limit=3, shown_handles={"kursus-b"})
            by_handle = {p["handle"]: p for p in third["products"]}
            self.assertEqual(by_handle["kursus-a"]["title"], "Excel grundkursus")

        # The global product index itself was never touched.
        self.assertEqual(products[0]["title"], "Excel grundkursus")
        self.assertEqual(products[0]["vendor"], "Udbyder A")


class CrossEncoderDiagnosticsTests(unittest.TestCase):
    """cross_encoder_applied must reflect what ACTUALLY happened at the call site."""

    def setUp(self):
        rag._search_cache.clear()

    def _run(self, rerank_fn):
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                rag, "load_augmented_products", return_value=_fake_products()))
            stack.enter_context(patch.object(
                rag, "_bm25_search", return_value=[(0, 0.50), (1, 0.40), (2, 0.30)]))
            stack.enter_context(patch.object(
                rag, "_bm25_confident_enough", return_value=True))
            stack.enter_context(patch.object(rag, "_title_token_sets", None))
            stack.enter_context(patch.object(
                rag, "_should_run_cross_encoder", return_value=True))
            stack.enter_context(patch.object(
                rag, "_crossencoder_rerank", side_effect=rerank_fn))
            return semantic_search_courses_detailed("excel analyse", limit=3)

    def test_applied_true_when_reranker_actually_scored(self):
        def fake_rerank(query, candidates, products, limit=5):
            out = []
            for doc_idx, score, explain in candidates:
                explain = dict(explain)
                explain["cross_encoder_score"] = 8
                out.append((doc_idx, score, explain))
            return out

        result = self._run(fake_rerank)
        self.assertNotIn("error", result)
        self.assertTrue(result["debug"]["cross_encoder_applied"])

    def test_applied_false_when_reranker_fell_back(self):
        # _crossencoder_rerank's error path returns the candidates UNCHANGED
        # (no cross_encoder_score markers) — diagnostics must say False even
        # though the gate said "run it".
        result = self._run(lambda query, candidates, products, limit=5: candidates)
        self.assertNotIn("error", result)
        self.assertFalse(result["debug"]["cross_encoder_applied"])


class HybridRankExpansionHoistTests(unittest.TestCase):
    """_expand_query_tokens runs once per query, not once per product."""

    def _products(self):
        return [
            {"handle": "h-excel", "title": "Excel masterclass",
             "ai_summary": "excel excel excel regneark", "tags": [], "variants": []},
            {"handle": "h-python", "title": "Python basics",
             "ai_summary": "programmering python", "tags": [], "variants": []},
            {"handle": "h-ledelse", "title": "Ledelse i praksis",
             "ai_summary": "ledelse teams", "tags": [], "variants": []},
            {"handle": "h-excel-2", "title": "Excel for økonomi",
             "ai_summary": "excel regneark økonomi", "tags": [], "variants": []},
        ]

    def _rank(self, products, counter):
        real_expand = rag._expand_query_tokens

        def counting_expand(tokens):
            counter["n"] += 1
            return real_expand(tokens)

        with ExitStack() as stack:
            stack.enter_context(patch.object(
                rag, "_expand_query_tokens", side_effect=counting_expand))
            stack.enter_context(patch.object(
                rag, "get_query_embedding", return_value=None))
            stack.enter_context(patch.object(
                rag, "_bm25_confident_enough", return_value=False))
            # Pin index globals so BM25 idf math is deterministic and
            # independent of whatever other tests loaded.
            stack.enter_context(patch.object(rag, "_bm25_index", None))
            stack.enter_context(patch.object(rag, "_bm25_N", 0))
            stack.enter_context(patch.object(rag, "_bm25_avg_dl", 0))
            stack.enter_context(patch.object(rag, "_title_token_sets", None))
            return hybrid_rank_products(products, "excel regneark", products, limit=4)

    def test_expansion_called_exactly_once_per_query(self):
        counter = {"n": 0}
        ranked = self._rank(self._products(), counter)
        # 4 products in the loop — pre-hoist this was 4 calls; now exactly 1.
        self.assertEqual(counter["n"], 1)
        self.assertEqual(len(ranked), 4)

    def test_ordering_unchanged_and_deterministic_after_hoist(self):
        counter1, counter2 = {"n": 0}, {"n": 0}
        ranked1 = self._rank(self._products(), counter1)
        ranked2 = self._rank(self._products(), counter2)
        handles1 = [p["handle"] for p in ranked1]
        handles2 = [p["handle"] for p in ranked2]
        # Byte-identical across runs (expansion is loop-invariant).
        self.assertEqual(handles1, handles2)
        # Lexically matching docs outrank non-matching, strongest match first —
        # exactly the pre-hoist ordering on this fixture.
        self.assertEqual(handles1, ["h-excel", "h-excel-2", "h-python", "h-ledelse"])


if __name__ == "__main__":
    unittest.main()
