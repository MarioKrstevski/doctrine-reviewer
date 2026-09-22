import unittest

from identity import ID_FIELD, identity_of, is_doctrine_card, primary_id

V2 = ["Text", "Extra", "Personal Notes", "Review", "Brief Vignette",
      "Full Vignette", ID_FIELD]
ANKING = ["Text", "Extra", "Lecture Notes", "Missed Questions", "Pathoma"]


class GateTest(unittest.TestCase):
    def test_note_type_with_the_field_is_ours_even_when_empty(self):
        self.assertTrue(is_doctrine_card(V2, "Anything"))

    def test_note_type_without_the_field_is_not_ours(self):
        self.assertFalse(is_doctrine_card(ANKING, "AnKing"))

    def test_field_name_must_match_exactly_including_the_space(self):
        self.assertFalse(is_doctrine_card(["DoctorineID"], "x"))
        self.assertFalse(is_doctrine_card(["doctorine id"], "x"))

    def test_deck_named_doctrine_is_a_fallback(self):
        self.assertTrue(is_doctrine_card(ANKING, "Doctrine"))
        self.assertTrue(is_doctrine_card(ANKING, "Doctorine::Cardio"))
        self.assertFalse(is_doctrine_card(ANKING, "My Doctrine Notes"))

    def test_missing_inputs_are_handled(self):
        self.assertFalse(is_doctrine_card(None, None))
        self.assertFalse(is_doctrine_card([], ""))


class IdentityTest(unittest.TestCase):
    def test_filled_field_wins(self):
        self.assertEqual(("abc123", "P]%rwtghL]"),
                         identity_of({ID_FIELD: " abc123 "}, "P]%rwtghL]"))
        self.assertEqual("abc123", primary_id({ID_FIELD: "abc123"}, "P]%rwtghL]"))

    def test_empty_field_falls_back_to_guid(self):
        self.assertEqual(("", "P]%rwtghL]"), identity_of({ID_FIELD: ""}, "P]%rwtghL]"))
        self.assertEqual("P]%rwtghL]", primary_id({ID_FIELD: ""}, "P]%rwtghL]"))

    def test_no_field_at_all_falls_back_to_guid(self):
        self.assertEqual("s.MBeTB!D.", primary_id({"Text": "x"}, "s.MBeTB!D."))

    def test_never_returns_none(self):
        self.assertEqual(("", ""), identity_of(None, None))
        self.assertEqual("", primary_id(None, None))
