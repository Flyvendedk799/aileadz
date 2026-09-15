"""Priority/budget-aware context assembly (ai_context_layers) + runtime wiring.

Root cause guarded here: every dynamic system layer used to be merged into one
message and cut to 1800 chars, so the profile, memories and profiler playbook
never reached the model. Offline: no OpenAI, no MySQL.
"""
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import ai_context_layers as acl  # noqa: E402
import ai_runtime  # noqa: E402
import grounding  # noqa: E402


def _profiler_turn():
    """A realistic profiler turn: sizes mirror production layers."""
    playbook = "PROFILER-MODE:\n" + ("Du hjælper brugeren med karrieren.\n" * 85)       # ~3000
    profile = ("Erfaring: [#12] Teamleder @ Novo Nordisk (2019-nu)\n"
               + "Kompetencer: Python (avanceret), SQL (mellem)\n" * 70)                # ~3500
    memories = "- [Præference] Foretrækker online kurser om aftenen\n" * 28              # ~1500
    company = "Kun interne udbydere må bruges til IT-kurser.\n" * 45                     # ~2000
    few_shot = "EKSEMPEL: Bruger: hej\nAssistent: hej\n" * 18                            # ~650
    return [
        {"role": "system", "content": "STATIC CORE PROMPT"},
        acl.layer("few_shot", few_shot),
        {"role": "system", "content": "CV-INTELLIGENS\n" + ("regel\n" * 400)},          # untagged, ~2500
        acl.layer("profile", profile, header="BRUGERPROFIL:", fence="BRUGERPROFIL"),
        acl.layer("memories", memories, header="HVAD JEG VED OM DIG:", fence="HUKOMMELSE"),
        acl.layer("mode_core_playbook", playbook),
        acl.layer("company_rules", company, header="VIRKSOMHEDSREGLER:", fence="VIRKSOMHEDSREGLER"),
        {"role": "user", "content": "Start profilsamtalen"},
    ]


class AssembleTests(unittest.TestCase):
    def test_static_prompt_is_untouched_and_first(self):
        out, _ = acl.assemble(_profiler_turn(), budget_chars=40000)
        self.assertEqual(out[0], {"role": "system", "content": "STATIC CORE PROMPT"})
        self.assertEqual(out[-1]["role"], "user")

    def test_layout_is_static_knowledge_steering_history(self):
        out, _ = acl.assemble(_profiler_turn(), budget_chars=40000)
        self.assertEqual([m.get("_zone") for m in out], [None, "knowledge", "steering", None])
        self.assertTrue(out[1]["content"].startswith(acl.KNOWLEDGE_HEADER))
        self.assertTrue(out[2]["content"].startswith(acl.STEERING_HEADER))

    def test_tight_budget_keeps_playbook_profile_and_company_rules(self):
        out, report = acl.assemble(_profiler_turn(), budget_chars=9000)
        joined = "\n".join(m["content"] for m in out)
        self.assertIn("PROFILER-MODE", joined)
        self.assertIn("Teamleder @ Novo Nordisk", joined)
        self.assertIn("Kun interne udbydere", joined)
        status = {l["name"]: l["status"] for l in report["layers"]}
        self.assertEqual(status["mode_core_playbook"], "kept")
        self.assertEqual(status["few_shot"], "dropped")

    def test_priority_zero_is_never_dropped(self):
        out, _ = acl.assemble(_profiler_turn(), budget_chars=10)
        self.assertIn("PROFILER-MODE", "\n".join(m["content"] for m in out))

    def test_fence_is_never_cut_open(self):
        out, report = acl.assemble(_profiler_turn(), budget_chars=9000)
        knowledge = out[1]["content"]
        truncated = [l for l in report["layers"] if l["name"] == "profile"][0]
        self.assertEqual(truncated["status"], "truncated")
        self.assertEqual(knowledge.count(grounding._FENCE_CLOSE),
                         knowledge.count(grounding._FENCE_OPEN + ":"))

    def test_untagged_layers_become_legacy_steering(self):
        out, report = acl.assemble([
            {"role": "system", "content": "STATIC"},
            {"role": "system", "content": "profil info"},
            {"role": "user", "content": "hej"},
        ], budget_chars=5000)
        self.assertEqual(len(out), 3)
        self.assertIn("profil info", out[1]["content"])
        self.assertEqual(report["layers"][0]["name"], acl.LEGACY)

    def test_output_is_byte_stable(self):
        a, _ = acl.assemble(_profiler_turn(), budget_chars=12000)
        b, _ = acl.assemble(_profiler_turn(), budget_chars=12000)
        self.assertEqual(a, b)

    def test_aggressive_is_smaller(self):
        normal, _ = acl.assemble(_profiler_turn(), budget_chars=40000)
        aggressive, _ = acl.assemble(_profiler_turn(), budget_chars=40000, aggressive=True)
        size = lambda msgs: sum(len(m["content"]) for m in msgs)
        self.assertLess(size(aggressive), size(normal))

    def test_reprepare_keeps_budgeted_layers_whole(self):
        first, _ = acl.assemble(_profiler_turn(), budget_chars=40000)
        stripped = acl.strip_private_keys(first)
        second, _ = acl.assemble(stripped, budget_chars=40000)
        self.assertEqual(second[1]["content"], first[1]["content"])
        self.assertEqual(second[2]["content"], first[2]["content"])

    def test_late_system_hint_after_history_is_steering(self):
        msgs = _profiler_turn() + [{"role": "system", "content": "SYSTEMHINT: samtalen er lang"}]
        out, _ = acl.assemble(msgs, budget_chars=40000)
        self.assertIn("SYSTEMHINT", out[2]["content"])
        self.assertEqual(out[-1]["role"], "user")

    def test_budget_reserves_tool_schemas(self):
        without = acl.context_budget_chars(36000, 0, chars_per_token=4.0)
        with_tools = acl.context_budget_chars(12000, 6000, chars_per_token=4.0)
        self.assertGreater(without, with_tools)
        self.assertEqual(with_tools, int(6000 * 0.4) * 4)


class FinalizeTests(unittest.TestCase):
    def _assembled(self):
        history = [
            {"role": "user", "content": "første"},
            {"role": "assistant", "content": "svar"},
            {"role": "user", "content": "sidste"},
        ]
        out, _ = acl.assemble(_profiler_turn()[:-1] + history, budget_chars=40000)
        return out

    def test_trailing_places_steering_before_last_user_and_strips_keys(self):
        final = acl.finalize_for_openai(self._assembled(), placement="trailing")
        self.assertFalse(any(k in m for m in final for k in acl.PRIVATE_KEYS))
        self.assertEqual(final[-1]["content"], "sidste")
        self.assertTrue(final[-2]["content"].startswith(acl.STEERING_HEADER))
        self.assertTrue(final[1]["content"].startswith(acl.KNOWLEDGE_HEADER))

    def test_leading_keeps_layout(self):
        final = acl.finalize_for_openai(self._assembled(), placement="leading")
        self.assertTrue(final[2]["content"].startswith(acl.STEERING_HEADER))


class RuntimeWiringTests(unittest.TestCase):
    def test_root_cause_profiler_context_survives_prepare(self):
        with mock.patch.dict(os.environ, {"AI_CONTEXT_ASSEMBLER": "1"}), \
                mock.patch.object(ai_runtime, "in_rate_limit_cooldown", return_value=False):
            prepared = ai_runtime.prepare_messages_for_turn(_profiler_turn())
        joined = "\n".join(str(m.get("content") or "") for m in prepared)
        self.assertIn("PROFILER-MODE", joined)
        self.assertIn("Teamleder @ Novo Nordisk", joined)
        self.assertIn("Foretrækker online kurser", joined)
        self.assertIn("Kun interne udbydere", joined)
        self.assertFalse(any(k in m for m in prepared for k in acl.PRIVATE_KEYS))

    def test_legacy_flag_still_renders_fences(self):
        with mock.patch.dict(os.environ, {"AI_CONTEXT_ASSEMBLER": "0"}):
            prepared = ai_runtime.prepare_messages_for_turn([
                {"role": "system", "content": "STATIC"},
                acl.layer("profile", "Teamleder", header="BRUGERPROFIL:", fence="BRUGERPROFIL"),
                {"role": "user", "content": "hej"},
            ])
        self.assertIn(grounding._FENCE_CLOSE, prepared[1]["content"])

    def test_keep_zones_for_anthropic(self):
        prepared = ai_runtime.prepare_messages_for_turn(_profiler_turn(), keep_zones=True)
        self.assertEqual([m.get("_zone") for m in prepared if m["role"] == "system"],
                         [None, "knowledge", "steering"])

    def test_tool_schemas_count_against_budget(self):
        tools = [{"type": "function", "function": {"name": f"t{i}", "description": "x" * 800}}
                 for i in range(10)]
        self.assertGreater(ai_runtime.estimate_tools_tokens(tools), 1500)
        self.assertEqual(ai_runtime.estimate_tools_tokens(None), 0)


class AnthropicPlacementTests(unittest.TestCase):
    def setUp(self):
        import ai_provider_anthropic as apa
        self.apa = apa
        self.prepared = ai_runtime.prepare_messages_for_turn(_profiler_turn(), keep_zones=True)

    def test_opus_keeps_knowledge_cached_and_steering_trailing(self):
        kwargs = self.apa._build_kwargs(model="claude-opus-5", prepared=self.prepared,
                                        tools=None, tool_choice="auto", max_tokens=None)
        system = kwargs["system"]
        self.assertEqual(len(system), 2)
        self.assertEqual(system[0]["text"], "STATIC CORE PROMPT")
        self.assertTrue(system[1]["text"].startswith(acl.KNOWLEDGE_HEADER))
        self.assertEqual(system[1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(kwargs["messages"][-1]["role"], "system")
        self.assertIn("PROFILER-MODE", kwargs["messages"][-1]["content"])

    def test_haiku_keeps_steering_in_system_uncached(self):
        kwargs = self.apa._build_kwargs(model="claude-haiku-4-5", prepared=self.prepared,
                                        tools=None, tool_choice="auto", max_tokens=None)
        system = kwargs["system"]
        self.assertEqual(len(system), 3)
        self.assertIn("cache_control", system[1])
        self.assertNotIn("cache_control", system[2])
        self.assertTrue(system[2]["text"].startswith(acl.STEERING_HEADER))
        self.assertFalse([m for m in kwargs["messages"] if m["role"] == "system"])


if __name__ == "__main__":
    unittest.main()
