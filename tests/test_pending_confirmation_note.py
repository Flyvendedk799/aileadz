"""An answer may not say an action is done while its confirm card is still waiting."""
import json

import grounding

CARD = json.dumps({"needs_confirmation": True, "action": "share_learning_goal", "message_da": "Del målet?"})
DONE = json.dumps({"status": "success", "section": "goals", "visibility": "delt med HR"})
READ = json.dumps({"status": "success", "count": 2, "goals": []})


def note(text, *outputs):
    return grounding.pending_confirmation_note(text, list(outputs))


def test_claiming_done_while_the_card_waits_gets_the_note():
    assert note("Dit mål er nu delt med HR.", CARD) == grounding.PENDING_CONFIRMATION_NOTE
    assert note("Jeg har sendt mailen til alle i Salg.", {"needs_confirmation": True}) == grounding.PENDING_CONFIRMATION_NOTE
    assert note("Bestillingen er blevet annulleret, og budgettet er frigivet.", READ, CARD)


def test_an_honest_answer_is_left_alone():
    assert note("Jeg har lagt et kort klar. Tryk Bekræft for at dele målet med HR.", CARD) == ""
    assert note("Målet er ikke delt endnu: det sker, når du bekræfter på kortet.", CARD) == ""
    assert note("Vil du dele målet med HR?", CARD) == ""


def test_no_card_or_a_real_action_means_no_note():
    assert note("Dit mål er nu delt med HR.", DONE) == ""
    assert note("Dit mål er nu delt med HR.", CARD, DONE) == "", "something was carried out this turn"
    assert note("Du har nu gemt to mål.", READ) == ""
    assert note("", CARD) == "" and note("Dit mål er nu delt.", "not json", None) == ""


def test_the_three_assistants_apply_it():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in ("app1/agent.py", "hr_agent.py", "vendor_portal.py"):
        assert "pending_confirmation_note(" in open(os.path.join(root, path), encoding="utf-8").read(), path
