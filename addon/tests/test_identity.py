"""Decide, client-side, whether a note is a Doctrine card.

Identity is the note guid. The pipeline sets guid = database ObjectId
(24 lowercase hex), while Anki-native notes -- a student's own cards, or
public decks -- carry 10-char base91 guids like 'f{Q8k)Zx@3'. That shape
difference is the courtesy gate for showing the button; the server's
"is this guid registered?" check remains the real gate.
"""

import unittest

from identity import is_doctrine_card


class GuidShapeTest(unittest.TestCase):
    def test_a_pipeline_objectid_guid_is_accepted(self):
        self.assertTrue(is_doctrine_card("69444ce12cf1b3ba0261a175", "Other"))

    def test_an_anki_native_guid_is_rejected(self):
        for guid in ["f{Q8k)Zx@3", "Ab3$xY9!qZ", "abcdefghij"]:
            with self.subTest(guid=guid):
                self.assertFalse(is_doctrine_card(guid, "Other"))

    def test_uppercase_hex_is_rejected(self):
        # ObjectIds are always lowercase; be strict so the gate stays narrow.
        self.assertFalse(is_doctrine_card("69444CE12CF1B3BA0261A175", "Other"))

    def test_wrong_length_hex_is_rejected(self):
        self.assertFalse(is_doctrine_card("69444ce12cf1b3ba0261a17", "Other"))
        self.assertFalse(is_doctrine_card("69444ce12cf1b3ba0261a1755", "Other"))

    def test_missing_guid_is_rejected(self):
        for guid in [None, "", 123]:
            with self.subTest(guid=guid):
                self.assertFalse(is_doctrine_card(guid, "Doctrine"))


class DeckNameTest(unittest.TestCase):
    """A deck named Doctrine is also accepted, so a future change of id
    format does not silently hide the button on every card."""

    def test_the_doctrine_deck_is_accepted_even_with_a_native_guid(self):
        self.assertTrue(is_doctrine_card("f{Q8k)Zx@3", "Doctrine"))

    def test_subdecks_count(self):
        self.assertTrue(is_doctrine_card("f{Q8k)Zx@3", "Doctrine::Cardio"))

    def test_case_and_whitespace_are_forgiven(self):
        self.assertTrue(is_doctrine_card("f{Q8k)Zx@3", "  doctrine "))

    def test_a_deck_merely_containing_the_word_is_not_enough(self):
        self.assertFalse(is_doctrine_card("f{Q8k)Zx@3", "My Doctrine Notes"))
        self.assertFalse(is_doctrine_card("f{Q8k)Zx@3", "Doctrinez"))

    def test_missing_deck_name_is_handled(self):
        self.assertFalse(is_doctrine_card("f{Q8k)Zx@3", None))
        self.assertTrue(is_doctrine_card("69444ce12cf1b3ba0261a175", None))
