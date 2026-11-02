"""The answer text must not restate the course cards (offline, pure text)."""
import unittest

from app1.card_text import CARDS_MARK, fix_markdown_links, strip_images, tidy_answer

CARDS = [
    {"title": "Distanceledelse"},
    {"title": "Bliv klar til Distanceledelse – Få succes som leder på afstand"},
]

# The shape the model produced in production: a numbered list, a description, a link and a
# pasted logo per course, then a closing remark and the suggestions tag.
LISTING = (
    "Her er to relevante kurser, som kan styrke dine færdigheder som leder:\n\n"
    "1. **Distanceledelse** fra TACK International A/S\n"
    "   Dette kursus henvender sig til ledere og teams, der arbejder på forskellige lokationer.\n"
    "   [Læs mere om kurset her](https://example.com/a)\n\n"
    "![TACK](https://cdn.example.com/tack.png)\n\n"
    "2. **Bliv klar til Distanceledelse – Få succes som leder på afstand** fra Uddannelseshuset\n"
    "   Dette e-learning kursus henvender sig til ledere.\n"
    "   [Læs mere om kurset her](https://example.com/b)\n\n"
    "![UH](https://cdn.example.com/uh.png)\n\n"
    "Begge kurser er værdifulde for dig. Vil du dykke dybere ned i et af dem?\n"
    '<suggestions>["Sammenlign de to"]</suggestions>'
)


class TidyAnswerTests(unittest.TestCase):
    def test_spaced_product_links_are_closed(self):
        raw = "Se [Vild med dansk 2]( /products/vild-med-dansk-2) og [andet]( https://futurematch.dk/products/excel )."
        out = fix_markdown_links(raw)
        self.assertIn("[Vild med dansk 2](/products/vild-med-dansk-2)", out)
        self.assertIn("[andet](/products/excel)", out)
        self.assertNotIn("( /", out)
        self.assertEqual(tidy_answer(raw, []), out)

    def test_images_are_removed(self):
        self.assertNotIn("![", strip_images(LISTING))
        self.assertEqual(strip_images("a\n\n\n\n![x](y)\n\nb"), "a\n\nb")

    def test_restated_courses_are_cut_and_text_wraps_the_cards(self):
        out = tidy_answer(LISTING, CARDS)
        self.assertNotIn("![", out)
        self.assertNotIn("Læs mere om kurset", out)
        self.assertNotIn("TACK International", out)
        before, after = out.split(CARDS_MARK)
        self.assertIn("Her er to relevante kurser", before)
        self.assertIn("Vil du dykke dybere", after)
        self.assertTrue(out.endswith('<suggestions>["Sammenlign de to"]</suggestions>'))

    def test_answer_without_a_listing_is_left_alone(self):
        text = "Distanceledelse passer godt til dig, fordi du leder et spredt team."
        self.assertEqual(tidy_answer(text, CARDS), text)

    def test_unrelated_lists_survive(self):
        text = "Tre ting at tænke over:\n\n- budget\n- tid\n- niveau\n\nSkal vi tage dem én ad gangen?"
        self.assertEqual(tidy_answer(text, CARDS), text)

    def test_no_cards_means_no_cutting(self):
        self.assertEqual(tidy_answer("1. **Distanceledelse** fra TACK\n   beskrivelse", []),
                         "1. **Distanceledelse** fra TACK\n   beskrivelse")

    def test_a_pure_listing_is_kept_rather_than_emptied(self):
        text = "1. **Distanceledelse** fra TACK\n   beskrivelse\n2. **Bliv klar til Distanceledelse – Få succes som leder på afstand**"
        out = tidy_answer(text, CARDS)
        self.assertNotIn(CARDS_MARK, out)
        self.assertIn("Distanceledelse", out)

    def test_bold_paragraph_per_course_is_cut_too(self):
        text = (
            "Jeg fandt to kurser.\n\n"
            "**Distanceledelse** fra TACK\nEt kursus om ledelse på afstand.\n\n"
            "Hvilket vil du høre mere om?"
        )
        out = tidy_answer(text, CARDS)
        self.assertNotIn("Et kursus om ledelse", out)
        self.assertIn("Hvilket vil du høre mere om?", out.split(CARDS_MARK)[1])

    def test_comparison_bullets_and_rejection_lists_are_not_cut(self):
        comparison = (
            "Mit valg afhænger af dit mål:\n\n"
            "- **Hurtigst i gang:** Vælg **Distanceledelse**, hvis du vil have værktøjer nu.\n"
            "- **E-learning i eget tempo:** Vælg **Bliv klar til Distanceledelse – Få succes som leder på afstand**.\n\n"
            "Jeg ville fravælge:\n"
            "- **Distanceledelse**, hvis du kun ønsker ren selvstudie."
        )
        out = tidy_answer(comparison, CARDS)
        self.assertNotIn(CARDS_MARK, out)
        self.assertEqual(out, comparison)


if __name__ == "__main__":
    unittest.main()
