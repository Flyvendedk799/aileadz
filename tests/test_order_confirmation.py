"""The order card after Bekræft: a written confirmation, and a card that settles.

Reported on the AI assistant's last order step: the confirm card lacked the chosen
course, stayed on "Bekræfter…" after the order went through, and the assistant said
nothing in text. The button label was never reset (the result was appended under
still-disabled buttons), and the confirm route ran the tool without any reply.
"""
import os
import unittest
from unittest import mock

from app1.order_confirmation import order_confirmation_text

ASSETS = os.path.join(os.path.dirname(__file__), "..", "static", "futurematch", "assets")


def _read(name):
    with open(os.path.join(ASSETS, name), encoding="utf-8") as fh:
        return fh.read()


class ConfirmationTextTests(unittest.TestCase):
    def _text(self, result, args=None, title="Projektledelse Grundkursus"):
        with mock.patch("catalog_service.get_product", return_value={"title": title} if title else None):
            return order_confirmation_text(result, args or {"product_handle": "pl"})

    def test_a_created_order_names_the_course_and_links_to_it(self):
        text = self._text({"status": "order_created", "order_url": "/ordre/1", "order_status_label": "Bekræftet"})
        self.assertIn("**Projektledelse Grundkursus**", text)
        self.assertIn("Status: Bekræftet.", text)
        self.assertIn("[Se bestillingen](/ordre/1)", text)

    def test_an_order_that_needs_approval_says_so_instead_of_a_status(self):
        text = self._text({"status": "order_created", "needs_approval": True, "order_status_label": "x"})
        self.assertIn("afventer godkendelse", text)
        self.assertNotIn("Status:", text)

    def test_an_unknown_course_still_gets_a_confirmation(self):
        self.assertEqual(self._text({"status": "order_created"}, title=None), "Din bestilling er registreret.")

    def test_team_and_hr_results_keep_their_own_sentence(self):
        self.assertEqual(self._text({"status": "team_orders_created", "message": "2 af 2 ordrer er oprettet."}),
                         "2 af 2 ordrer er oprettet.")

    def test_anything_that_booked_nothing_gets_no_confirmation(self):
        for result in ({"status": "error", "message": "Fejl"}, {"status": "needs_info"}, None, "x"):
            self.assertEqual(order_confirmation_text(result, {}), "")


class CardSourceTests(unittest.TestCase):
    """The card scripts are not run offline; pin the parts the report was about."""

    def test_the_chat_card_settles_instead_of_leaving_bekraefter_behind(self):
        js = _read("chat.js")
        self.assertIn('card.querySelector(".confirm-card-actions").hidden = true;', js)
        # a network error is the one case with nothing booked: the buttons come back
        self.assertIn('okBtn.textContent = "Bekræft";', js)

    def test_the_chat_card_shows_the_course_and_the_written_confirmation(self):
        js = _read("chat.js")
        self.assertIn("data.course", js)
        self.assertIn("courseCard(data.course", js)
        self.assertIn("result.confirmation_text", js)

    def test_hidden_actions_are_not_overridden_by_display_flex(self):
        self.assertIn(".confirm-card-actions[hidden] { display: none; }", _read("chat.css"))
        self.assertIn(".fm-confirm-actions[hidden]{display:none}", _read("fm-pages.css"))

    def test_the_shared_card_recognises_order_results(self):
        js = _read("ai-stream.js")
        for status in ("order_created", "team_orders_created", "handed_off_to_hr"):
            self.assertIn(f'"{status}"', js)
        self.assertIn('card.querySelector(".fm-confirm-actions").hidden = true;', js)


if __name__ == "__main__":
    unittest.main()
