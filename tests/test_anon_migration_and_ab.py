"""N-5.8: guest memory moves to the account on login; prompt variants are stable."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import anon_migration  # noqa: E402

PROFILE = {"interests": ["projektledelse", "lean"], "preferred_location": "Aarhus", "preferred_format": "",
           "budget_range": "", "last_viewed": [{"handle": "h", "title": "PRINCE2"}],
           "conversation_summary": "Ville gerne certificeres."}


class MigrationTests(unittest.TestCase):
    def test_profile_becomes_memories_then_anonymous_row_is_erased(self):
        written = []
        with mock.patch("app1.memory_store.load_anonymous_profile", return_value=PROFILE), \
                mock.patch("app1.memory_store.erase_subject") as erase, \
                mock.patch("app1.user_profile_db.add_memory",
                           side_effect=lambda u, label, **k: written.append((u, label, k["category"], k["source"])) or 1):
            n = anon_migration.migrate("tok", "anna")
        self.assertEqual(n, len(written))
        self.assertIn(("anna", "Interesse: lean", "interesse", "anonymous"), written)
        self.assertTrue(any(w[1] == "Foretrukken lokation" for w in written))
        erase.assert_called_once_with(browser_token="tok")

    def test_nothing_to_move_is_a_noop(self):
        with mock.patch("app1.memory_store.load_anonymous_profile", return_value=None), \
                mock.patch("app1.memory_store.erase_subject") as erase:
            self.assertEqual(anon_migration.migrate("tok", "anna"), 0)
        erase.assert_not_called()
        self.assertEqual(anon_migration.migrate("", "anna"), 0)
        self.assertEqual(anon_migration.migrate("tok", ""), 0)

    def test_failed_copy_keeps_the_anonymous_row(self):
        with mock.patch("app1.memory_store.load_anonymous_profile", return_value=PROFILE), \
                mock.patch("app1.memory_store.erase_subject") as erase, \
                mock.patch("app1.user_profile_db.add_memory", side_effect=RuntimeError("db")):
            self.assertEqual(anon_migration.migrate("tok", "anna"), 0)
        erase.assert_not_called()


class PromptVariantTests(unittest.TestCase):
    def setUp(self):
        from app1 import agent
        self.agent = agent
        agent._SESSION_VERSIONS.clear()

    def test_default_is_single_control_without_addendum(self):
        with mock.patch.dict(os.environ, {"AI_PROMPT_VARIANTS": ""}):
            self.assertEqual(self.agent._get_prompt_version("s1"), "v2.0")
            self.assertEqual(self.agent.prompt_variant_addendum("s1"), "")

    def test_assignment_is_deterministic_and_splits_sessions(self):
        with mock.patch.dict(os.environ, {"AI_PROMPT_VARIANTS": "v2.0,v2.1"}):
            first = {f"s{i}": self.agent._get_prompt_version(f"s{i}") for i in range(40)}
            self.agent._SESSION_VERSIONS.clear()
            again = {f"s{i}": self.agent._get_prompt_version(f"s{i}") for i in range(40)}
        self.assertEqual(first, again)
        self.assertEqual(set(first.values()), {"v2.0", "v2.1"})

    def test_only_the_variant_gets_its_addendum(self):
        with mock.patch.dict(os.environ, {"AI_PROMPT_VARIANTS": "v2.0,v2.1", "AI_PROMPT_ADDENDUM_V2_1": "Vær kortfattet."}):
            by_version = {}
            for i in range(40):
                sid = f"s{i}"
                by_version.setdefault(self.agent._get_prompt_version(sid), self.agent.prompt_variant_addendum(sid))
        self.assertEqual(by_version["v2.0"], "")
        self.assertEqual(by_version["v2.1"], "Vær kortfattet.")


if __name__ == "__main__":
    unittest.main()
