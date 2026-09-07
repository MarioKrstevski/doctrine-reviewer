"""Trace data assembled from plain values, so it is testable without Anki."""

import unittest

from payload import trace

CLOZE_TEMPLATES = ["Cloze"]
STANDARD_TEMPLATES = ["Card 1", "Card 2"]


class TraceTest(unittest.TestCase):
    def _t(self, **over):
        base = dict(tags=["a", "b"], note_mod=1788816928, card_id=42, card_ord=0,
                    template_names=STANDARD_TEMPLATES, deck_id=7, original_deck_id=0,
                    install_id="abc123", addon_version="1.1", anki_version="25.02")
        base.update(over)
        return trace(**base)

    def test_all_fields_present(self):
        t = self._t()
        for key in ("tags", "note_mod", "card_id", "card_ord", "template_name",
                    "deck_id", "original_deck_id", "install_id", "addon_version",
                    "anki_version"):
            self.assertIn(key, t)

    def test_standard_type_names_the_template_for_the_ord(self):
        self.assertEqual("Card 2", self._t(card_ord=1)["template_name"])

    def test_cloze_type_has_one_template_for_every_ord(self):
        # For cloze notes card.ord is the cloze number, not a template index.
        self.assertEqual("Cloze", self._t(template_names=CLOZE_TEMPLATES,
                                          card_ord=5)["template_name"])

    def test_unknown_ord_does_not_raise(self):
        self.assertIsNone(self._t(card_ord=9)["template_name"])

    def test_original_deck_is_none_outside_filtered_decks(self):
        self.assertIsNone(self._t(original_deck_id=0)["original_deck_id"])
        self.assertEqual(99, self._t(original_deck_id=99)["original_deck_id"])

    def test_tags_are_a_plain_list(self):
        self.assertEqual(["x"], self._t(tags=("x",))["tags"])
        self.assertEqual([], self._t(tags=None)["tags"])
