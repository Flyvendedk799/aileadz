"""app1/user_knowledge.py — per-user semantic knowledge index.

Fully offline: no MySQL (fake cursor/connection behind a patched current_app),
no OpenAI (the module's embedding seams are patched). Covers vector packing,
fact derivation, the hash-diff sync, keyword fallback, ranking, username
scoping of every SQL statement, throttling and the recall tool executor.
"""
import datetime
import json
import os
import sys
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

import app1.user_knowledge as uk  # noqa: E402
from perf_cache import cache_clear  # noqa: E402

MODEL = "text-embedding-3-small"
DIMS = 4


class FakeCursor:
    """Routes SELECTs to canned rows by SQL prefix and records every statement."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        if self.db.raise_on and self.db.raise_on in sql:
            raise RuntimeError("boom secret-detail")
        norm = " ".join(sql.split())
        self._result = []
        if norm.startswith("SELECT id, source_type, source_id, content_hash"):
            self._result = list(self.db.existing)
        elif norm.startswith("SELECT id, content, content_hash"):
            self._result = list(self.db.pending)
        elif norm.startswith("SELECT source_type, source_id, mode"):
            self._result = list(self.db.search_rows)
        elif norm.startswith("SELECT id, content_hash, embedding_model"):
            self._result = [self.db.conversation_row] if self.db.conversation_row else []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.executed = []
        self.existing = []
        self.pending = []
        self.search_rows = []
        self.conversation_row = None
        self.raise_on = None
        self.commits = 0
        self.rollbacks = 0

    def app(self):
        app = mock.MagicMock()
        conn = app.mysql.connection
        conn.cursor.side_effect = lambda *a, **kw: FakeCursor(self)

        def _commit():
            self.commits += 1

        def _rollback():
            self.rollbacks += 1
        conn.commit.side_effect = _commit
        conn.rollback.side_effect = _rollback
        return app

    def statements(self, prefix):
        return [(s, p) for s, p in self.executed if " ".join(s.split()).startswith(prefix)]


class _Base(unittest.TestCase):
    def setUp(self):
        cache_clear("user_knowledge.sync")
        self.db = FakeDB()
        self.patches = [
            mock.patch.object(uk, "current_app", new=self.db.app()),
            mock.patch.object(uk, "_current_embedding_spec", new=lambda: (MODEL, DIMS)),
            mock.patch.dict(os.environ, {"AI_USER_KNOWLEDGE": "1",
                                         "AI_USER_KNOWLEDGE_EMBEDDINGS": "1",
                                         "OPENAI_API_KEY": "sk-test"}),
        ]
        for p in self.patches:
            p.start()
        self.embed_calls = []

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        cache_clear("user_knowledge.sync")

    def assert_all_sql_scoped(self):
        self.assertTrue(self.db.executed)
        for sql, params in self.db.executed:
            self.assertIn("username", sql, sql)
            self.assertIn("%s", sql, sql)


class VectorPackingTests(unittest.TestCase):
    def test_roundtrip_is_normalised(self):
        vec = uk.unpack_vector(uk.pack_vector([3.0, 4.0, 0.0, 0.0]))
        self.assertEqual(len(vec), 4)
        self.assertAlmostEqual(float(vec[0]), 0.6, places=5)
        self.assertAlmostEqual(float(vec[1]), 0.8, places=5)
        self.assertAlmostEqual(sum(float(x) ** 2 for x in vec), 1.0, places=5)

    def test_float32_size(self):
        self.assertEqual(len(uk.pack_vector([1.0] * 10)), 40)

    def test_fallback_without_numpy(self):
        with mock.patch.object(uk, "_np", new=None):
            blob = uk.pack_vector([0.0, 2.0])
            vec = uk.unpack_vector(blob)
        self.assertIsInstance(vec, list)
        self.assertAlmostEqual(vec[1], 1.0, places=5)
        # Same bytes either way, so vectors written by one worker read in another.
        self.assertEqual(blob, uk.pack_vector([0.0, 2.0]))

    def test_empty_and_corrupt(self):
        self.assertEqual(uk.pack_vector([]), b"")
        self.assertEqual(uk.pack_vector(None), b"")
        self.assertIsNone(uk.unpack_vector(b""))
        self.assertIsNone(uk.unpack_vector(b"abc"))

    def test_zero_vector_does_not_divide_by_zero(self):
        vec = uk.unpack_vector(uk.pack_vector([0.0, 0.0]))
        self.assertEqual([float(x) for x in vec], [0.0, 0.0])


class FlagTests(unittest.TestCase):
    def test_defaults_on(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}):
            os.environ.pop("AI_USER_KNOWLEDGE", None)
            os.environ.pop("AI_USER_KNOWLEDGE_EMBEDDINGS", None)
            self.assertTrue(uk.knowledge_enabled())
            self.assertTrue(uk.embeddings_enabled())

    def test_off_values(self):
        with mock.patch.dict(os.environ, {"AI_USER_KNOWLEDGE": "0",
                                          "AI_USER_KNOWLEDGE_EMBEDDINGS": "false"}):
            self.assertFalse(uk.knowledge_enabled())
            self.assertFalse(uk.embeddings_enabled())

    def test_embeddings_off_without_key(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "",
                                          "AI_USER_KNOWLEDGE_EMBEDDINGS": "1"}), \
                mock.patch.object(uk, "_openai_key_available", return_value=False):
            self.assertFalse(uk.embeddings_enabled())


class FactDerivationTests(unittest.TestCase):
    PROFILE = {
        "experience": [
            {"id": 12, "title": "Projektleder", "company": "Novo", "start_year": 2019,
             "end_year": None, "is_current": True, "description": "Leder et team på 8."},
            {"id": 13, "title": "Konsulent", "company": "Deloitte", "start_year": 2015,
             "end_year": 2019, "is_current": False, "description": ""},
        ],
        "education": [{"id": 3, "degree": "Cand.merc.", "institution": "CBS",
                       "year_completed": 2014, "description": ""}],
        "skills": [{"id": 1, "name": "Python", "level": "avanceret", "category": "it"}],
        "certifications": [{"id": 5, "name": "PRINCE2", "issuer": "Axelos", "expiry_date": "2027-01-01"}],
        "languages": [{"id": 2, "language": "Engelsk", "proficiency": "flydende"}],
        "learning_goals": [{"id": 7, "title": "Blive agil coach", "description": "",
                            "target_date": "2027-06-01", "status": "aktiv"}],
        "completed_courses": [{"id": 9, "title": "Scrum Master", "vendor": "Teknologisk"}],
        "headline": "Erfaren projektleder",
        "bio": "",
        "target_role": "Programleder",
        "budget_range": None,
    }

    def test_profile_fact_ids_and_content(self):
        facts = dict(uk.profile_facts(self.PROFILE))
        for sid in ("experience:12", "experience:13", "education:3", "skill:python",
                    "certification:5", "language:engelsk", "goal:7", "course:9",
                    "summary:headline", "summary:target_role"):
            self.assertIn(sid, facts)
        self.assertNotIn("summary:bio", facts)
        self.assertNotIn("summary:budget_range", facts)
        self.assertIn("Arbejder som Projektleder hos Novo (siden 2019)", facts["experience:12"])
        self.assertIn("Leder et team", facts["experience:12"])
        self.assertIn("Har arbejdet som Konsulent hos Deloitte (2015–2019)", facts["experience:13"])
        self.assertIn("Python", facts["skill:python"])
        self.assertIn("Programleder", facts["summary:target_role"])
        self.assertTrue(all(len(sid) <= 64 for sid in facts))

    def test_long_keys_fit_source_id_column(self):
        facts = uk.profile_facts({"skills": [{"name": "x" * 200, "level": "mellem"}]})
        self.assertEqual(len(facts), 1)
        self.assertLessEqual(len(facts[0][0]), 64)
        self.assertTrue(facts[0][0].startswith("skill:#"))

    def test_garbage_profile(self):
        self.assertEqual(uk.profile_facts(None), [])
        self.assertEqual(uk.profile_facts({"skills": [None, {"name": ""}]}), [])

    def test_memory_facts(self):
        facts = uk.memory_facts([
            {"id": 412, "category": "praeference", "label": "Foretrækker online kurser",
             "detail": "Har små børn"},
            {"id": 413, "category": "andet", "label": "Kan lide kaffe", "detail": None},
            {"id": None, "label": "ingen id"},
        ])
        self.assertEqual(facts, [("412", "Foretrækker online kurser — Har små børn"),
                                 ("413", "Kan lide kaffe")])


class SyncTests(_Base):
    def _existing(self, rid, stype, sid, content):
        return {"id": rid, "source_type": stype, "source_id": sid,
                "content_hash": uk._content_hash(content)}

    def test_diff_insert_update_delete_noop(self):
        memories = [
            {"id": 1, "label": "Uændret", "detail": None},
            {"id": 2, "label": "Ændret nu", "detail": None},
            {"id": 3, "label": "Ny", "detail": None},
        ]
        profile = {"skills": [{"name": "Python", "level": "mellem", "category": ""}]}
        skill_content = dict(uk.profile_facts(profile))["skill:python"]
        self.db.existing = [
            self._existing(10, "memory", "1", "Uændret"),
            self._existing(11, "memory", "2", "Gammel tekst"),
            self._existing(12, "memory", "99", "Slettet"),
            self._existing(13, "profile_fact", "skill:python", skill_content),
            self._existing(14, "profile_fact", "experience:5", "Væk"),
        ]
        with mock.patch.object(uk, "_embed_batch", return_value=[]):
            stats = uk.sync_user("alice", profile=profile, memories=memories)
        self.assertEqual(stats["status"], "ok")
        self.assertEqual((stats["inserted"], stats["updated"], stats["deleted"], stats["unchanged"]),
                         (1, 1, 2, 2))
        upserts = self.db.statements("INSERT INTO user_knowledge")
        self.assertEqual(sorted(p[2] for _, p in upserts), ["2", "3"])
        self.assertIn("embedding = NULL", upserts[0][0])
        deletes = self.db.statements("DELETE FROM user_knowledge")
        self.assertEqual(len(deletes), 1)
        self.assertEqual(sorted(deletes[0][1][1:]), [12, 14])
        self.assertEqual(deletes[0][1][0], "alice")
        self.assertGreaterEqual(self.db.commits, 1)
        self.assert_all_sql_scoped()

    def test_unchanged_is_noop(self):
        memories = [{"id": 1, "label": "Uændret", "detail": None}]
        self.db.existing = [self._existing(10, "memory", "1", "Uændret")]
        with mock.patch.object(uk, "_embed_batch", return_value=[]):
            stats = uk.sync_user("alice", profile={}, memories=memories)
        self.assertEqual(stats["unchanged"], 1)
        self.assertFalse(self.db.statements("INSERT"))
        self.assertFalse(self.db.statements("DELETE"))

    def test_embeds_pending_in_one_batch(self):
        self.db.pending = [
            {"id": 1, "content": "a", "content_hash": "h1"},
            {"id": 2, "content": "b", "content_hash": "h2"},
        ]
        calls = []

        def fake_embed(texts):
            calls.append(list(texts))
            return [[1.0, 0.0, 0.0, 0.0], None]
        with mock.patch.object(uk, "_embed_batch", side_effect=fake_embed):
            stats = uk.sync_user("alice", profile={}, memories=[])
        self.assertEqual(calls, [["a", "b"]])
        self.assertEqual(stats["embedded"], 1)
        updates = self.db.statements("UPDATE user_knowledge SET embedding")
        self.assertEqual(len(updates), 1)
        self.assertIn("updated_at = updated_at", updates[0][0])
        self.assertEqual(updates[0][1][1:], (MODEL, DIMS, "alice", 1, "h1"))
        select = self.db.statements("SELECT id, content, content_hash")[0]
        self.assertEqual(select[1][-1], 32)
        self.assert_all_sql_scoped()

    def test_embeddings_disabled_skips_embedding(self):
        self.db.pending = [{"id": 1, "content": "a", "content_hash": "h1"}]
        embed = mock.MagicMock()
        with mock.patch.dict(os.environ, {"AI_USER_KNOWLEDGE_EMBEDDINGS": "0"}), \
                mock.patch.object(uk, "_embed_batch", new=embed):
            stats = uk.sync_user("alice", profile={}, memories=[])
        embed.assert_not_called()
        self.assertEqual(stats["embedded"], 0)

    def test_throttle_and_force(self):
        with mock.patch.object(uk, "_embed_batch", return_value=[]):
            first = uk.sync_user("alice", profile={}, memories=[])
            second = uk.sync_user("alice", profile={}, memories=[])
            other_user = uk.sync_user("bob", profile={}, memories=[])
            forced = uk.sync_user("alice", profile={}, memories=[], force=True)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "throttled")
        self.assertEqual(other_user["status"], "ok")
        self.assertEqual(forced["status"], "ok")

    def test_loads_sources_when_none(self):
        fake_db = mock.MagicMock()
        fake_db.get_full_profile.return_value = {"target_role": "CTO"}
        fake_db.get_memories.return_value = [{"id": 5, "label": "Hej", "detail": ""}]
        import app1
        with mock.patch.object(app1, "user_profile_db", new=fake_db, create=True), \
                mock.patch.dict(sys.modules, {"app1.user_profile_db": fake_db}), \
                mock.patch.object(uk, "_embed_batch", return_value=[]):
            stats = uk.sync_user("alice")
        fake_db.get_full_profile.assert_called_once_with("alice")
        fake_db.get_memories.assert_called_once_with("alice")
        self.assertEqual(stats["inserted"], 2)

    def test_failed_loader_does_not_wipe_that_type(self):
        fake_db = mock.MagicMock()
        fake_db.get_full_profile.side_effect = RuntimeError("db down")
        fake_db.get_memories.return_value = []
        self.db.existing = [self._existing(14, "profile_fact", "skill:python", "x")]
        import app1
        with mock.patch.object(app1, "user_profile_db", new=fake_db, create=True), \
                mock.patch.dict(sys.modules, {"app1.user_profile_db": fake_db}), \
                mock.patch.object(uk, "_embed_batch", return_value=[]):
            uk.sync_user("alice")
        select = self.db.statements("SELECT id, source_type")[0]
        self.assertEqual(select[1], ("alice", "memory"))
        self.assertFalse(self.db.statements("DELETE"))

    def test_db_error_never_raises(self):
        self.db.raise_on = "SELECT id, source_type"
        stats = uk.sync_user("alice", profile={}, memories=[], force=True)
        self.assertEqual(stats["status"], "error")
        self.assertEqual(self.db.rollbacks, 1)

    def test_disabled(self):
        with mock.patch.dict(os.environ, {"AI_USER_KNOWLEDGE": "0"}):
            self.assertEqual(uk.sync_user("alice", profile={}, memories=[])["status"], "disabled")
        self.assertFalse(self.db.executed)


class ConversationIndexTests(_Base):
    def test_upsert_and_embed(self):
        with mock.patch.object(uk, "_embed_batch", return_value=[[0.0, 1.0, 0.0, 0.0]]):
            self.assertIsNone(uk.index_conversation_summary("alice", "sess-1", "profiler",
                                                            "Talte om ledelse"))
        upsert = self.db.statements("INSERT INTO user_knowledge")[0]
        self.assertEqual(upsert[1][:4], ("alice", "conversation", "sess-1", "profiler"))
        self.assertEqual(len(self.db.statements("UPDATE user_knowledge SET embedding")), 1)
        self.assert_all_sql_scoped()

    def test_unchanged_and_embedded_skips(self):
        self.db.conversation_row = {"id": 1, "content_hash": uk._content_hash("Talte om ledelse"),
                                    "embedding_model": MODEL, "dims": DIMS}
        embed = mock.MagicMock()
        with mock.patch.object(uk, "_embed_batch", new=embed):
            uk.index_conversation_summary("alice", "sess-1", "chat", "Talte om ledelse")
        embed.assert_not_called()
        self.assertFalse(self.db.statements("INSERT"))

    def test_never_raises(self):
        self.db.raise_on = "SELECT id, content_hash"
        self.assertIsNone(uk.index_conversation_summary("alice", "s", "chat", "x"))
        self.assertIsNone(uk.index_conversation_summary("", "s", "chat", "x"))


def _row(stype, sid, content, vec=None, model=MODEL, dims=DIMS, day=1, mode=None):
    return {"source_type": stype, "source_id": sid, "mode": mode, "content": content,
            "embedding": uk.pack_vector(vec) if vec else None,
            "embedding_model": model if vec else None, "dims": dims if vec else None,
            "updated_at": datetime.datetime(2026, 9, day, 12, 0, 0)}


class SearchTests(_Base):
    def test_keyword_fallback_when_query_embedding_fails(self):
        self.db.search_rows = [
            _row("memory", "1", "Foretrækker online kurser", vec=[1, 0, 0, 0]),
            _row("memory", "2", "Kan lide kaffe", vec=[0, 1, 0, 0]),
        ]
        with mock.patch.object(uk, "_query_embedding", return_value=None):
            res = uk.search("alice", "online kurser om ledelse")
        self.assertEqual([r["source_id"] for r in res], ["1"])
        self.assertAlmostEqual(res[0]["score"], 2 / 3, places=3)
        self.assertEqual(res[0]["updated_at"], "2026-09-01 12:00:00")
        self.assert_all_sql_scoped()

    def test_keyword_only_when_embeddings_disabled(self):
        self.db.search_rows = [_row("memory", "1", "Python programmering", vec=[1, 0, 0, 0])]
        qe = mock.MagicMock()
        with mock.patch.dict(os.environ, {"AI_USER_KNOWLEDGE_EMBEDDINGS": "0"}), \
                mock.patch.object(uk, "_query_embedding", new=qe):
            res = uk.search("alice", "python")
        qe.assert_not_called()
        self.assertEqual(res[0]["score"], 1.0)

    def test_semantic_ranking(self):
        self.db.search_rows = [
            _row("memory", "far", "Kan lide kaffe", vec=[0, 1, 0, 0], day=5),
            _row("profile_fact", "near", "Ambition om at lede et team", vec=[0.9, 0.1, 0, 0], day=2),
            _row("conversation", "mid", "Talte om karriere", vec=[0.5, 0.5, 0, 0], day=3, mode="chat"),
            _row("memory", "stale-model", "ledelse ledelse", vec=[1, 0, 0, 0], model="old-model", day=4),
        ]
        with mock.patch.object(uk, "_query_embedding", return_value=[1.0, 0.0, 0.0, 0.0]):
            res = uk.search("alice", "ledelse")
        ids = [r["source_id"] for r in res]
        # Wrong-model row falls back to keyword scoring (full overlap = 1.0);
        # semantic rows score 0.8*cos (+0 keyword); orthogonal "far" has no
        # signal at all and is dropped.
        self.assertEqual(ids, ["stale-model", "near", "mid"])
        self.assertEqual(res[0]["score"], 1.0)
        self.assertAlmostEqual(res[1]["score"], 0.8 * 0.9 / (0.82 ** 0.5), places=3)
        self.assertEqual(res[2]["mode"], "chat")
        scores = [r["score"] for r in res]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_types_exclude_k_min_score(self):
        self.db.search_rows = [
            _row("memory", "1", "ledelse kursus", day=3),
            _row("memory", "2", "ledelse", day=2),
            _row("memory", "3", "ledelse", day=1),
        ]
        with mock.patch.object(uk, "_query_embedding", return_value=None):
            res = uk.search("alice", "ledelse kursus", types=["memory", "bogus"], k=1,
                            exclude_source_ids=[1])
            self.assertEqual([r["source_id"] for r in res], ["2"])  # recency tie-break
            sql, params = self.db.executed[-1]
            self.assertIn("source_type IN (%s)", sql)
            self.assertEqual(params, ("alice", "memory", 500))
            self.assertEqual(uk.search("alice", "ledelse kursus", min_score=0.9), [
                r for r in uk.search("alice", "ledelse kursus") if r["score"] >= 0.9])
            self.assertEqual(uk.search("alice", "ledelse", types=["bogus"]), [])

    def test_never_raises(self):
        self.db.raise_on = "SELECT source_type"
        self.assertEqual(uk.search("alice", "hej"), [])
        self.assertEqual(uk.search("", "hej"), [])
        self.assertEqual(uk.search("alice", "   "), [])


class RecallToolTests(_Base):
    def test_not_logged_in(self):
        out = json.loads(uk.execute_recall_about_user({"query": "x"}, ""))
        self.assertEqual(out, {"status": "error", "message": "Brugeren er ikke logget ind."})

    def test_scope_mapping(self):
        expected = {"alt": None, "hukommelse": ("memory",), "samtaler": ("conversation",),
                    "profil": ("profile_fact",), "ukendt": None}
        for scope, types in expected.items():
            with mock.patch.object(uk, "sync_user") as sync, \
                    mock.patch.object(uk, "search", return_value=[]) as search:
                out = json.loads(uk.execute_recall_about_user(
                    {"query": "ledelse", "scope": scope}, "alice"))
            self.assertEqual(out["status"], "success")
            self.assertEqual(out["count"], 0)
            sync.assert_called_once_with("alice")
            self.assertEqual(search.call_args.kwargs["types"], types, scope)

    def test_results_shape(self):
        hit = {"source_type": "conversation", "source_id": "s1", "mode": "profiler",
               "content": "Talte om ledelse", "score": 0.9, "updated_at": "2026-09-01 12:00:00"}
        with mock.patch.object(uk, "sync_user"), \
                mock.patch.object(uk, "search", return_value=[hit]):
            out = json.loads(uk.execute_recall_about_user({"query": "ledelse"}, "alice"))
        self.assertEqual(out, {"status": "success", "count": 1, "results": [
            {"type": "samtale", "text": "Talte om ledelse", "mode": "profiler", "when": "2026-09-01"}]})

    def test_search_exception_is_danish_without_detail(self):
        with mock.patch.object(uk, "sync_user"), \
                mock.patch.object(uk, "search", side_effect=RuntimeError("secret-detail")):
            raw = uk.execute_recall_about_user({"query": "ledelse"}, "alice")
        out = json.loads(raw)
        self.assertEqual(out["status"], "error")
        self.assertIn("brugeren", out["message"])
        self.assertNotIn("secret-detail", raw)
        self.assertNotIn("RuntimeError", raw)

    def test_missing_query(self):
        out = json.loads(uk.execute_recall_about_user({"scope": "alt"}, "alice"))
        self.assertEqual(out["status"], "error")


class EmbedTextsTests(unittest.TestCase):
    """app1.rag.embed_texts: one batched call, aligned None entries, never raises."""

    def test_single_batched_call_aligned_output(self):
        import app1.rag as rag
        dims = rag.embedding_dimensions()
        data = [mock.MagicMock(index=0, embedding=[0.1] * dims),
                mock.MagicMock(index=1, embedding=[0.2] * 3)]  # bad dims -> None
        create = mock.MagicMock(return_value=mock.MagicMock(data=data))
        with mock.patch.object(rag.openai.embeddings, "create", new=create):
            out = rag.embed_texts(["a", "", "b"], timeout=1.5)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["input"], ["a", "b"])
        self.assertEqual(create.call_args.kwargs["timeout"], 1.5)
        self.assertEqual(len(out), 3)
        self.assertEqual(len(out[0]), dims)
        self.assertIsNone(out[1])
        self.assertIsNone(out[2])

    def test_failure_returns_nones(self):
        import app1.rag as rag
        with mock.patch.object(rag.openai.embeddings, "create",
                               new=mock.MagicMock(side_effect=RuntimeError("down"))):
            self.assertEqual(rag.embed_texts(["a", "b"]), [None, None])
        self.assertEqual(rag.embed_texts([]), [])


if __name__ == "__main__":
    unittest.main()
