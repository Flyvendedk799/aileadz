"""Platform-help knowledge base (app1/help_kb.py) — offline tests.

No network, no DB: the index path is redirected to a temp dir, and every
embedding call is mocked. Includes a drift guard that every article's front-
matter `url` is a real route string in the Flask sources.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

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

import app1.help_kb as help_kb  # noqa: E402

_ROUTE_SOURCES = ("futurematch_ui.py", "pages.py", "api.py", "gdpr_routes.py")


class _IsolatedIndex(unittest.TestCase):
    """Point INDEX_PATH at a temp dir and reset caches around each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.index_path = os.path.join(self._tmp.name, "help_kb_index.json")
        self._patch = mock.patch.object(help_kb, "INDEX_PATH", self.index_path)
        self._patch.start()
        help_kb._index_cache.clear()
        help_kb._articles_cache.clear()

    def tearDown(self):
        self._patch.stop()
        help_kb._index_cache.clear()
        help_kb._articles_cache.clear()
        self._tmp.cleanup()

    def _fake_rag(self, **attrs):
        base = dict(
            cosine_similarity=help_kb._local_cosine,
            embedding_model=lambda: "test-embed",
            get_query_embedding=lambda q: None,
        )
        base.update(attrs)
        return types.SimpleNamespace(**base)


class TestArticles(unittest.TestCase):
    def setUp(self):
        self.articles = help_kb.load_articles()

    def test_has_curated_articles(self):
        self.assertGreaterEqual(len(self.articles), 10)

    def test_every_article_has_required_front_matter_and_body(self):
        slugs = set()
        for a in self.articles:
            with self.subTest(path=a["path"]):
                self.assertTrue(a["title"] and a["title"] != a["slug"])
                self.assertTrue(a["slug"])
                self.assertTrue(a["url"].startswith("/"))
                self.assertTrue(a["keywords"])
                self.assertTrue(a["body"].strip())
                self.assertIn("## ", a["body"])
                self.assertLessEqual(len(a["body"]), 2600, "keep articles short")
                self.assertNotIn(a["slug"], slugs)
                slugs.add(a["slug"])

    def test_every_url_is_a_real_route(self):
        """Drift guard: front-matter URLs must exist as route strings in the code."""
        source = ""
        for name in _ROUTE_SOURCES:
            with open(os.path.join(_REPO_ROOT, name), encoding="utf-8") as fh:
                source += fh.read()
        for a in self.articles:
            with self.subTest(slug=a["slug"], url=a["url"]):
                self.assertTrue(
                    f"'{a['url']}'" in source or f'"{a["url"]}"' in source,
                    f"{a['url']} is not a route in {_ROUTE_SOURCES}",
                )

    def test_front_matter_parser(self):
        meta, body = help_kb._parse_front_matter(
            "﻿---\r\ntitle: Hej: med kolon\r\nkeywords: a, b\r\n---\r\n## A\r\ntekst\r\n")
        self.assertEqual(meta["title"], "Hej: med kolon")
        self.assertEqual(meta["keywords"], "a, b")
        self.assertEqual(body, "## A\ntekst")
        meta, body = help_kb._parse_front_matter("## Ingen front matter\nx")
        self.assertEqual(meta, {})
        self.assertTrue(body.startswith("## Ingen"))


class TestChunking(unittest.TestCase):
    def test_real_chunks_respect_cap_and_sections(self):
        articles = help_kb.load_articles()
        chunks = help_kb.chunk_articles(articles)
        self.assertGreater(len(chunks), len(articles))
        ids = [c["chunk_id"] for c in chunks]
        self.assertEqual(len(ids), len(set(ids)))
        for c in chunks:
            with self.subTest(chunk=c["chunk_id"]):
                self.assertLessEqual(len(c["text"]), help_kb.MAX_CHUNK_CHARS)
                self.assertTrue(c["section"])
                self.assertNotIn("\n## ", "\n" + c["text"])
                for key in ("chunk_id", "slug", "title", "section", "url", "text", "hash"):
                    self.assertIn(key, c)
        sections = {c["section"] for c in chunks if c["slug"] == "cv-upload"}
        self.assertIn("Kort fortalt", sections)
        self.assertIn("Trin for trin", sections)

    def test_long_section_is_split_under_cap(self):
        para = ("Dette er en lang sætning om platformen der gentages. " * 12).strip()
        body = "## Kort\nkort tekst\n\n## Lang\n" + "\n\n".join([para] * 8) + "\n### Under\nmere"
        art = {"slug": "x", "title": "X", "url": "/x", "keywords": ["x"], "body": body}
        chunks = help_kb.chunk_articles([art])
        long_chunks = [c for c in chunks if c["section"] == "Lang"]
        self.assertGreater(len(long_chunks), 1)
        self.assertTrue(all(len(c["text"]) <= help_kb.MAX_CHUNK_CHARS for c in chunks))
        self.assertIn("### Under", long_chunks[-1]["text"])  # ### stays inside the section
        self.assertEqual([c["section"] for c in chunks][0], "Kort")

    def test_giant_word_is_hard_split(self):
        art = {"slug": "y", "title": "Y", "url": "/y", "keywords": [], "body": "## S\n" + "a" * 3000}
        chunks = help_kb.chunk_articles([art])
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c["text"]) <= help_kb.MAX_CHUNK_CHARS for c in chunks))

    def test_hash_tracks_content(self):
        art = {"slug": "z", "title": "Z", "url": "/z", "keywords": ["k"], "body": "## S\ntekst"}
        h1 = help_kb.chunk_articles([art])[0]["hash"]
        self.assertEqual(h1, help_kb.chunk_articles([dict(art)])[0]["hash"])
        h2 = help_kb.chunk_articles([dict(art, body="## S\nanden tekst")])[0]["hash"]
        self.assertNotEqual(h1, h2)


class TestKeywordSearch(_IsolatedIndex):
    CASES = [
        ("Hvordan uploader jeg mit CV?", "cv-upload"),
        ("Hvordan sletter jeg det AI'en husker om mig?", "ai-hukommelse"),
        ("Hvordan får jeg mit kursus godkendt af HR?", "bestilling-og-godkendelse"),
        ("Hvad viser Mind-Map?", "mind-map"),
        ("Hvordan opretter jeg et udviklingsmål?", "udviklingsmaal"),
        ("Hvor meget har min afdeling tilbage på budgettet?", "afdelingsbudget"),
        ("Hvordan henter jeg en kopi af mine data efter GDPR?", "privatliv-og-gdpr"),
        ("Hvordan skifter jeg min adgangskode?", "konto-og-notifikationer"),
        ("Hvilke obligatoriske kurser mangler jeg?", "obligatoriske-kurser"),
        ("Hvad er AI Profiler?", "ai-profiler"),
        ("How do I delete what the AI remembers about me?", "ai-hukommelse"),
    ]

    def test_finds_the_right_article(self):
        self.assertFalse(os.path.exists(self.index_path))
        for query, slug in self.CASES:
            with self.subTest(query=query):
                results = help_kb.search_help(query, k=3)
                self.assertTrue(results, f"no results for {query!r}")
                self.assertEqual(results[0]["slug"], slug,
                                 [(r["slug"], r["section"], r["score"]) for r in results])

    def test_result_shape_and_k(self):
        results = help_kb.search_help("Hvordan bestiller jeg et kursus?", k=2)
        self.assertEqual(len(results), 2)
        for r in results:
            for key in ("title", "section", "url", "excerpt", "score"):
                self.assertIn(key, r)
            self.assertTrue(r["url"].startswith("/"))
            self.assertLessEqual(len(r["excerpt"]), help_kb.EXCERPT_CHARS + 2)
        self.assertGreaterEqual(results[0]["score"], results[1]["score"])

    def test_empty_and_nonsense_queries(self):
        self.assertEqual(help_kb.search_help(""), [])
        self.assertEqual(help_kb.search_help(None), [])
        self.assertEqual(help_kb.search_help("   "), [])
        self.assertEqual(help_kb.search_help("xyzzy qwertyuiop"), [])

    def test_missing_or_corrupt_index_still_works(self):
        self.assertTrue(help_kb.search_help("upload cv"))
        with open(self.index_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        help_kb._index_cache.clear()
        self.assertEqual(help_kb.search_help("upload cv")[0]["slug"], "cv-upload")

    def test_search_never_raises(self):
        with mock.patch.object(help_kb, "_current_chunks", side_effect=RuntimeError("boom")):
            self.assertEqual(help_kb.search_help("upload cv"), [])


class TestIndexAndEmbeddings(_IsolatedIndex):
    def test_build_without_embeddings_writes_null_vectors(self):
        index = help_kb.build_index(self.index_path, embed=False)
        self.assertTrue(os.path.exists(self.index_path))
        with open(self.index_path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertIsNone(on_disk["model"])
        self.assertIsNone(on_disk["dims"])
        self.assertEqual(len(on_disk["chunks"]), len(index["chunks"]))
        self.assertTrue(all(c["embedding"] is None for c in on_disk["chunks"]))
        self.assertEqual(help_kb.search_help("Hvordan uploader jeg mit CV?")[0]["slug"], "cv-upload")

    def test_build_without_embed_texts_function(self):
        fake = self._fake_rag()  # no embed_texts attribute
        with mock.patch.object(help_kb, "_rag", return_value=fake):
            index = help_kb.build_index(self.index_path)
        self.assertTrue(all(c["embedding"] is None for c in index["chunks"]))

    def test_embed_failure_falls_back_to_keywords(self):
        def boom(texts):
            raise RuntimeError("openai down sk-secret")

        fake = self._fake_rag(embed_texts=boom)
        with mock.patch.object(help_kb, "_rag", return_value=fake):
            index = help_kb.build_index(self.index_path)
            self.assertTrue(all(c["embedding"] is None for c in index["chunks"]))
            self.assertEqual(help_kb.search_help("Hvordan uploader jeg mit CV?")[0]["slug"], "cv-upload")

    def test_query_embedding_failure_uses_keywords(self):
        n = len(help_kb.chunk_articles(help_kb.load_articles()))
        fake = self._fake_rag(embed_texts=lambda texts: [[1.0, 0.0, 0.0]] * len(texts),
                              get_query_embedding=mock.Mock(side_effect=RuntimeError("net")))
        with mock.patch.object(help_kb, "_rag", return_value=fake):
            index = help_kb.build_index(self.index_path)
            self.assertEqual(index["dims"], 3)
            self.assertEqual(index["model"], "test-embed")
            self.assertEqual(sum(1 for c in index["chunks"] if c["embedding"]), n)
            results = help_kb.search_help("Hvordan skifter jeg min adgangskode?")
        fake.get_query_embedding.assert_called_once()
        self.assertEqual(results[0]["slug"], "konto-og-notifikationer")

    def test_cosine_blends_in_and_stale_hash_is_ignored(self):
        chunks = help_kb.chunk_articles(help_kb.load_articles())
        target = next(c for c in chunks if c["slug"] == "kontakt-support")

        def embed(texts):
            return [[1.0, 0.0] if t == help_kb._embed_input(target) else [0.0, 1.0] for t in texts]

        fake = self._fake_rag(embed_texts=embed, get_query_embedding=lambda q: [1.0, 0.0])
        with mock.patch.object(help_kb, "_rag", return_value=fake):
            help_kb.build_index(self.index_path)
            # Query with no keyword overlap: only the embedding can surface it.
            results = help_kb.search_help("zzz qqq")
            self.assertEqual(results[0]["slug"], "kontakt-support")
            self.assertEqual(results[0]["section"], target["section"])

            # Corrupt the stored hash -> vector no longer trusted -> no results.
            with open(self.index_path, encoding="utf-8") as fh:
                data = json.load(fh)
            for c in data["chunks"]:
                c["hash"] = "stale-" + c["hash"]
            with open(self.index_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            help_kb._index_cache.clear()
            self.assertEqual(help_kb.search_help("zzz qqq"), [])

    def test_model_mismatch_skips_cosine(self):
        fake = self._fake_rag(embed_texts=lambda texts: [[1.0, 0.0]] * len(texts),
                              get_query_embedding=mock.Mock(return_value=[1.0, 0.0]))
        with mock.patch.object(help_kb, "_rag", return_value=fake):
            help_kb.build_index(self.index_path)
            fake.embedding_model = lambda: "another-model"
            help_kb.search_help("upload cv")
        fake.get_query_embedding.assert_not_called()


class TestExecutor(_IsolatedIndex):
    def test_success_shape(self):
        out = json.loads(help_kb.execute_search_platform_help({"query": "Hvor uploader jeg mit CV?"}))
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["count"], len(out["results"]))
        self.assertGreaterEqual(out["count"], 1)
        for r in out["results"]:
            self.assertEqual(set(r), {"title", "section", "url", "excerpt"})
        self.assertEqual(out["results"][0]["url"], "/profil-upload")

    def test_empty_query_and_bad_args(self):
        for args in ({"query": ""}, {}, None, "upload", {"query": None}):
            with self.subTest(args=args):
                out = json.loads(help_kb.execute_search_platform_help(args))
                self.assertEqual(out["status"], "no_results")
                self.assertTrue(out["message"])

    def test_no_hits(self):
        out = json.loads(help_kb.execute_search_platform_help({"query": "xyzzy qwertyuiop"}))
        self.assertEqual(out["status"], "no_results")
        self.assertIn("/support", out["message"])

    def test_k_is_clamped(self):
        out = json.loads(help_kb.execute_search_platform_help({"query": "hvordan virker kurser", "k": "99"}))
        if out["status"] == "success":
            self.assertLessEqual(out["count"], 5)

    def test_exception_text_never_leaks(self):
        with mock.patch.object(help_kb, "search_help", side_effect=RuntimeError("boom sk-secret")):
            raw = help_kb.execute_search_platform_help({"query": "upload cv"})
        out = json.loads(raw)
        self.assertEqual(out["status"], "no_results")
        self.assertNotIn("boom", raw)
        self.assertNotIn("sk-secret", raw)

    def test_disabled_flag(self):
        with mock.patch.dict(os.environ, {"AI_HELP_KB": "0"}):
            self.assertFalse(help_kb.help_kb_enabled())
            out = json.loads(help_kb.execute_search_platform_help({"query": "upload cv"}))
            self.assertEqual(out["status"], "no_results")
        with mock.patch.dict(os.environ, {"AI_HELP_KB": ""}):
            self.assertTrue(help_kb.help_kb_enabled())
        env = {k: v for k, v in os.environ.items() if k != "AI_HELP_KB"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(help_kb.help_kb_enabled())

    def test_tool_schema(self):
        fn = help_kb.SEARCH_PLATFORM_HELP_TOOL["function"]
        self.assertEqual(fn["name"], "search_platform_help")
        self.assertEqual(fn["parameters"]["required"], ["query"])


class TestTrigger(unittest.TestCase):
    def test_token_tuples(self):
        for tup in (help_kb.TRIGGER_HOW_TOKENS, help_kb.TRIGGER_PLATFORM_NOUNS):
            self.assertIsInstance(tup, tuple)
            self.assertTrue(all(t == t.lower() for t in tup))
        for t in ("hvordan", "hvor finder jeg", "hvor kan jeg", "kan jeg", "how do i", "where"):
            self.assertIn(t, help_kb.TRIGGER_HOW_TOKENS)
        for t in ("upload", "cv-portal", "godkend", "godkendelse", "bestilling", "bestille",
                  "mind-map", "profilsiden", "min profil", "hukommelse", "slette", "budget",
                  "læringssti", "notifikation", "login", "adgangskode", "profiler"):
            self.assertIn(t, help_kb.TRIGGER_PLATFORM_NOUNS)

    def test_positives(self):
        for q in (
            "Hvordan uploader jeg mit CV?",
            "Hvor finder jeg min tidslinje?",
            "Hvordan får jeg min bestilling godkendt?",
            "Kan jeg slette det du husker om mig?",
            "hvordan virker Mind-Map",
            "Hvor kan jeg se afdelingens budget?",
            "Hvordan skifter jeg adgangskode",
            "How do I change my password?",
            "Where is my order approval?",
        ):
            with self.subTest(q=q):
                self.assertTrue(help_kb.looks_like_platform_help(q))

    def test_negatives(self):
        for q in (
            "hvordan bliver jeg projektleder",
            "hvordan lærer jeg python",
            "Hvordan bliver jeg projektleder?",
            "Find et kursus i Excel",
            "Hvor ligger kurset?",
            "upload",
            "",
            None,
            "I would like a course somewhere in Aarhus",
        ):
            with self.subTest(q=q):
                self.assertFalse(help_kb.looks_like_platform_help(q))


if __name__ == "__main__":
    unittest.main()
