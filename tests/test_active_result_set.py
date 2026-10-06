"""L01 / L02 / L11 — conversation-scoped active_result_set + search constraints.

Proves:
* a new search *replaces* the active set (physical → online does not mix);
* ordinals / focus bind to the active set, not historical shown products;
* similarly-named cards from another provider are filtered out when focused;
* constraints are keyed on the conversation state dict (no cross-chat bleed);
* "bestil intet" is a read-only lookup (vetoes buying intent).

Imports result_set as a free-standing module so the suite stays offline
(no Flask / MySQL / catalog boot).
"""
import importlib.util
import os
import re
import sys
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)


def _load_result_set():
    path = os.path.join(_REPO_ROOT, "app1", "result_set.py")
    spec = importlib.util.spec_from_file_location("app1_result_set_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load_result_set()


def _course(handle, title, vendor, price, locations=None):
    return {
        "handle": handle,
        "title": title,
        "vendor": vendor,
        "price": price,
        "locations": locations or [],
    }


PHYSICAL = [
    _course("agil-ti-fysisk", "Agil projektledelse", "Teknologisk Institut", "14.499", ["København"]),
    _course("ledelse-aros", "Ledelse i praksis", "Aros", "8.900", ["Taastrup"]),
    _course("ledelse-mannaz", "Ledelse grundlæggende", "Mannaz", "12.000", ["København"]),
]
ONLINE = [
    _course("agil-dansk-it", "Agil Projektledelse – Agile On Time", "DANSK IT", "4.950", ["Online"]),
    _course("gratis-ledelse-1", "Ledelse online gratis", "OpenLearn", "0", ["Online"]),
    _course("gratis-ledelse-2", "Intro til ledelse", "Coursera-ish", "0", ["Online"]),
    _course("gratis-ledelse-3", "Teamledelse basis", "FreeCo", "0", ["Online"]),
]


# Mirror of agent._HIGH_INTENT_PATTERNS — kept local so we can prove the veto
# without importing app1.agent (heavy Flask boot).
_HIGH_INTENT = re.compile(
    r"\b(tilmeld|tilmelding|tilmeld mig|meld mig til|book|bestil|køb|signup|sign up|"
    r"registrer|registrering|faktura|betaling|betale|"
    r"hvornår starter|næste hold|er der pladser|ledig[e]? plads|startdato|"
    r"rabat|rabatkode|grupperabat|firmapris|vi vil gerne bestille|"
    r"jeg vil gerne (?:tilmelde|bestille|booke|købe)|"
    r"kan jeg (?:tilmelde|bestille|booke)|få adgang|enroll)\b",
    re.IGNORECASE,
)


def _would_classify_buying(user_query, shown_count):
    """Reproduce the agent buying branch after the read-only veto was added."""
    if shown_count > 0 and _HIGH_INTENT.search(user_query) and not rs.is_read_only_lookup(user_query):
        return "buying"
    return "other"


class ActiveResultSetReplaceTests(unittest.TestCase):
    def test_physical_then_online_replaces_and_does_not_mix(self):
        state = {}
        rs.apply_constraint_updates(state, {
            "format": "fysisk", "price_max": 6250.50,
            "price_inclusive": True, "topic": "ledelse",
        })
        rs.replace_active_result_set(state, PHYSICAL)
        first_id = state["active_result_set"]["id"]
        self.assertEqual(
            state["active_result_set"]["handles"],
            [c["handle"] for c in PHYSICAL],
        )

        rs.apply_constraint_updates(state, {"format": "online"})
        rs.replace_active_result_set(state, ONLINE)
        ars = state["active_result_set"]
        self.assertNotEqual(ars["id"], first_id)
        self.assertEqual(ars["handles"], [c["handle"] for c in ONLINE])
        self.assertNotIn("agil-ti-fysisk", ars["handles"])
        self.assertEqual(ars["filters"].get("format"), "online")
        self.assertEqual(ars["filters"].get("price_max"), 6250.50)

    def test_ordinal_resolves_against_active_set_only(self):
        state = {}
        rs.replace_active_result_set(state, PHYSICAL)
        rs.replace_active_result_set(state, ONLINE)
        second = rs.resolve_ordinal(state, "Hvad med nummer 2?")
        self.assertEqual(second["handle"], "gratis-ledelse-1")
        self.assertEqual(second["title"], "Ledelse online gratis")


class FocusIdentityTests(unittest.TestCase):
    def test_correction_keeps_dansk_it_handle_and_drops_substitute_card(self):
        state = {}
        rs.replace_active_result_set(state, ONLINE)
        rs.set_focused_course(
            state,
            handle="agil-dansk-it",
            title="Agil Projektledelse – Agile On Time",
            vendor="DANSK IT",
            price="4.950",
        )
        focused = rs.get_focused_course(state)
        self.assertEqual(focused["handle"], "agil-dansk-it")
        self.assertEqual(focused["vendor"], "DANSK IT")
        self.assertEqual(str(focused["price"]), "4.950")

        cards = [
            {
                "handle": "agil-ti-fysisk",
                "title": "Agil projektledelse",
                "vendor": "Teknologisk Institut",
                "price": "14.499",
            },
            {
                "handle": "agil-dansk-it",
                "title": "Agil Projektledelse – Agile On Time",
                "vendor": "DANSK IT",
                "price": "4.950",
            },
        ]
        kept = rs.filter_cards_to_focus(cards, state)
        self.assertEqual([c["handle"] for c in kept], ["agil-dansk-it"])
        self.assertEqual(kept[0]["price"], "4.950")

    def test_mismatched_provider_same_handle_is_rejected(self):
        state = {}
        rs.set_focused_course(
            state, handle="agil-dansk-it", vendor="DANSK IT", price="4.950",
        )
        cards = [{
            "handle": "agil-dansk-it",
            "vendor": "Teknologisk Institut",
            "price": "14.499",
        }]
        self.assertEqual(rs.filter_cards_to_focus(cards, state), [])


class ConversationIsolationTests(unittest.TestCase):
    def test_two_conversations_keep_separate_constraints(self):
        chat_a = {}
        chat_b = {}
        rs.apply_constraint_updates(chat_a, {
            "topic": "ledelse", "format": "online", "price_max": 6250.50,
            "price_inclusive": True,
        })
        rs.replace_active_result_set(chat_a, ONLINE)
        rs.apply_constraint_updates(chat_b, {
            "topic": "Excel", "format": "online", "price_max": 5000,
        })
        rs.replace_active_result_set(chat_b, [
            _course("excel-1", "Excel videregående", "SuperUsers", "4.999", ["Online"]),
        ])

        msg_a = rs.build_thread_constraints_message(chat_a)["content"]
        self.assertIn("ledelse", msg_a)
        self.assertIn("6250.5", msg_a)
        self.assertNotIn("Excel", msg_a)

        msg_b = rs.build_thread_constraints_message(chat_b)["content"]
        self.assertIn("Excel", msg_b)
        self.assertIn("5000", msg_b)
        self.assertNotIn("ledelse", msg_b)
        self.assertNotIn("6250", msg_b)

    def test_negation_online_updates_format_without_scanning_history(self):
        state = {}
        rs.update_constraints_from_user(
            state,
            "Find op til tre kurser i ledelse med fysisk fremmøde i København.",
        )
        self.assertEqual(rs.get_search_constraints(state)["format"], "fysisk")
        rs.update_constraints_from_user(
            state,
            "Jeg ændrer søgningen: kun online, ikke fysisk fremmøde. Behold prisgrænsen.",
        )
        c = rs.get_search_constraints(state)
        self.assertEqual(c["format"], "online")
        self.assertGreaterEqual(c["version"], 2)

    def test_budget_with_danish_decimal_comma(self):
        state = {}
        rs.update_constraints_from_user(
            state, "Sæt budgettet til under 6.250,50 kr inklusive moms.",
        )
        c = rs.get_search_constraints(state)
        self.assertEqual(c["price_max"], 6250.50)
        self.assertTrue(c["price_inclusive"])


class ReadOnlyLookupTests(unittest.TestCase):
    def test_bestil_intet_is_read_only(self):
        self.assertTrue(rs.is_read_only_lookup(
            "Er der faktisk ledige pladser på de foreslåede hold? Bestil intet.",
        ))
        self.assertTrue(rs.is_read_only_lookup(
            "Dette er stadig kun et opslag; bestil og gem intet.",
        ))
        self.assertFalse(rs.is_read_only_lookup(
            "Jeg vil gerne bestille kurset i morgen.",
        ))

    def test_intent_buying_branch_respects_read_only_veto(self):
        self.assertEqual(
            _would_classify_buying("Er der ledige pladser? Bestil intet.", 1),
            "other",
        )
        self.assertEqual(
            _would_classify_buying("Jeg vil gerne bestille kurset.", 1),
            "buying",
        )


class EvidenceAndMessagesTests(unittest.TestCase):
    def test_evidence_includes_focused_price(self):
        state = {}
        rs.replace_active_result_set(state, ONLINE)
        rs.set_focused_course(
            state, handle="agil-dansk-it", vendor="DANSK IT", price="4.950",
        )
        evidence = rs.evidence_from_state(state)
        handles = {e.get("handle") for e in evidence}
        self.assertIn("agil-dansk-it", handles)
        focused = [e for e in evidence if e.get("focused")]
        self.assertEqual(focused[0]["price"], "4.950")

    def test_active_set_message_marks_set_as_authoritative(self):
        state = {}
        rs.replace_active_result_set(state, ONLINE)
        msg = rs.build_active_set_message(state)["content"]
        self.assertIn("AKTIVT RESULTATSÆT", msg)
        self.assertIn("agil-dansk-it", msg)

    def test_claims_match_rejects_wrong_handle_when_focused(self):
        state = {}
        rs.set_focused_course(
            state, handle="agil-dansk-it", vendor="DANSK IT", price="4.950",
        )
        self.assertFalse(rs.claims_match_active_facts(
            handle="agil-ti-fysisk", vendor="Teknologisk Institut", state=state,
        ))
        self.assertTrue(rs.claims_match_active_facts(
            handle="agil-dansk-it", vendor="DANSK IT", state=state,
        ))


if __name__ == "__main__":
    unittest.main()


class CrossChatDigestBleedTests(unittest.TestCase):
    """L11 live follow-up: reopening leadership must not inherit Excel/budget."""

    def test_should_suppress_digests_on_this_thread_summary(self):
        prompt = (
            "Opsummer kun de senest gældende søgekrav i denne samtale: "
            "emne, format og prisgrænse. Skeln mellem det hypotetiske "
            "katalogopslag og oplysninger, der faktisk er gemt på min profil. "
            "Gem eller ændr intet."
        )
        self.assertTrue(rs.asks_for_this_thread_constraints(prompt))
        self.assertTrue(rs.should_suppress_cross_session_digests(prompt))
        self.assertFalse(rs.should_suppress_cross_session_digests(
            "Find online Excel under 5000 kr.",
        ))

    def test_reopen_leadership_keeps_own_constraints_not_excel(self):
        """Simulate chat A (leadership) then chat B (Excel); A state untouched."""
        leadership = {}
        excel_chat = {}
        rs.update_constraints_from_user(
            leadership,
            "Find op til tre kurser i ledelse med fysisk fremmøde i København. "
            "Sæt budgettet til under 6.250,50 kr inklusive moms.",
        )
        rs.update_constraints_from_user(
            leadership,
            "Jeg ændrer søgningen: kun online, ikke fysisk fremmøde. Behold prisgrænsen.",
        )
        rs.replace_active_result_set(leadership, ONLINE)

        rs.update_constraints_from_user(
            excel_chat,
            "Find online Excel-kurser under 5.000 kr.",
        )
        rs.replace_active_result_set(excel_chat, [
            _course("excel-1", "Excel videregående", "SuperUsers", "4.999", ["Online"]),
        ])

        # "Reopen" leadership: only leadership state is restored (resume path).
        restored = {
            "search_constraints": dict(leadership["search_constraints"]),
            "active_result_set": dict(leadership["active_result_set"]),
        }
        msg = rs.build_thread_constraints_message(restored)["content"]
        self.assertIn("ledelse", msg.lower())
        self.assertIn("6250.5", msg)
        self.assertIn("online", msg.lower())
        self.assertNotIn("Excel", msg)
        self.assertNotIn("5000", msg)

        # Digest-like bleed must be ignored when summarizing this thread.
        self.assertTrue(rs.should_suppress_cross_session_digests(
            "Opsummer kun de senest gældende søgekrav i denne samtale."
        ))

    def test_topic_extracted_from_user_text_without_search_tool(self):
        state = {}
        rs.update_constraints_from_user(state, "Find kurser i ledelse online under 6000 kr.")
        c = rs.get_search_constraints(state)
        self.assertEqual(c.get("topic"), "ledelse")
        self.assertEqual(c.get("format"), "online")
        self.assertEqual(c.get("price_max"), 6000.0)

        state_b = {}
        rs.update_constraints_from_user(state_b, "Online Excel under 5.000 kr tak.")
        cb = rs.get_search_constraints(state_b)
        self.assertEqual(cb.get("topic"), "Excel")
        self.assertEqual(cb.get("price_max"), 5000.0)
        self.assertNotEqual(c.get("topic"), cb.get("topic"))
