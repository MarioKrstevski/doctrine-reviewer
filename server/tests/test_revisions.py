"""Edits change the current text in `notes` and are recorded as revisions.

A revision row per changed field: before, after, who, where it came from,
and the release it shipped in (NULL while pending). Cutting a release later
stamps the pending rows.
"""

import json
import unittest

import revisions
import server
from tests.helpers import register_note, running_server

GUID = "P]%rwtghL]"


def note(conn, guid=GUID):
    return dict(conn.execute("SELECT * FROM notes WHERE doctrine_id=?", (guid,)).fetchone())


class ApplyEditTest(unittest.TestCase):
    def test_changes_text_bumps_version_and_records_one_revision_per_field(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "old", "Extra": "e", "Pathoma": ""})
            with server.db() as conn:
                out = revisions.apply_edit(conn, GUID, {"Text": "new", "Extra": "e"},
                                           user_id=7, source="suggestion:3")
                row = note(conn)
                revs = [dict(r) for r in conn.execute("SELECT * FROM revisions")]
        self.assertEqual(["Text"], out["changed"])
        self.assertEqual("new", json.loads(row["fields_json"])["Text"])
        self.assertEqual("e", json.loads(row["fields_json"])["Extra"])
        self.assertEqual(2, row["version"])
        self.assertEqual(server.content_hash({"Text": "new", "Extra": "e", "Pathoma": ""}),
                         row["content_hash"])
        self.assertEqual(1, len(revs))
        r = revs[0]
        self.assertEqual((GUID, "Text", "old", "new", 7, "suggestion:3", None),
                         (r["guid"], r["field"], r["before"], r["after"], r["user_id"],
                          r["source"], r["release"]))

    def test_identical_edit_changes_nothing(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "same"})
            with server.db() as conn:
                out = revisions.apply_edit(conn, GUID, {"Text": "same"}, 1, "staff")
                row = note(conn)
                n = conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        self.assertEqual([], out["changed"])
        self.assertEqual(1, row["version"])
        self.assertEqual(0, n)

    def test_non_editable_field_is_refused(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "t", "Pathoma": ""})
            with server.db() as conn:
                with self.assertRaises(ValueError):
                    revisions.apply_edit(conn, GUID, {"Pathoma": "x"}, 1, "staff")

    def test_unknown_guid_is_refused(self):
        with running_server():
            with server.db() as conn:
                with self.assertRaises(LookupError):
                    revisions.apply_edit(conn, "nope", {"Text": "x"}, 1, "staff")

    def test_second_edit_records_the_previous_value_as_before(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "v1"})
            with server.db() as conn:
                revisions.apply_edit(conn, GUID, {"Text": "v2"}, 1, "staff")
                revisions.apply_edit(conn, GUID, {"Text": "v3"}, 1, "staff")
                befores = [r["before"] for r in conn.execute("SELECT before FROM revisions ORDER BY id")]
        self.assertEqual(["v1", "v2"], befores)


class PendingTest(unittest.TestCase):
    def test_lists_unreleased_revisions_grouped_by_note(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "a", "Extra": "b"})
            register_note(base, "s.MBeTB!D.", fields={"Text": "c"})
            with server.db() as conn:
                revisions.apply_edit(conn, GUID, {"Text": "a2", "Extra": "b2"}, 1, "staff")
                revisions.apply_edit(conn, "s.MBeTB!D.", {"Text": "c2"}, 1, "staff")
                conn.execute("UPDATE revisions SET release='1.1' WHERE guid=?", ("s.MBeTB!D.",))
                pending = revisions.pending(conn)
        self.assertEqual([GUID], [p["guid"] for p in pending])
        self.assertEqual({"Text", "Extra"}, {r["field"] for r in pending[0]["revisions"]})


class AddNoteTest(unittest.TestCase):
    def test_creates_a_note_with_a_fresh_anki_style_guid(self):
        with running_server():
            with server.db() as conn:
                guid = revisions.add_note(conn, "AnKingOverhaul++++++",
                                          {"Text": "{{c1::new}}", "Extra": "x"},
                                          ["Doctorine::Yield::Step1::Gold"], user_id=2)
                row = note(conn, guid)
                revs = [dict(r) for r in conn.execute("SELECT * FROM revisions")]
        self.assertEqual(10, len(guid))
        self.assertTrue(all(c in revisions.GUID_ALPHABET for c in guid))
        self.assertEqual("{{c1::new}}", json.loads(row["fields_json"])["Text"])
        self.assertEqual(["Doctorine::Yield::Step1::Gold"], json.loads(row["tags_json"]))
        self.assertEqual(1, row["version"])
        self.assertEqual(0, row["retired"])
        self.assertEqual(1, len(revs))
        self.assertIsNone(revs[0]["before"])
        self.assertEqual("added", revs[0]["field"])

    def test_new_notes_are_official_immediately(self):
        with running_server():
            with server.db() as conn:
                guid = revisions.add_note(conn, "AnKingOverhaul++++++", {"Text": "t"}, [], 1)
            self.assertTrue(server.is_official(guid))

    def test_requires_text(self):
        with running_server():
            with server.db() as conn:
                with self.assertRaises(ValueError):
                    revisions.add_note(conn, "AnKingOverhaul++++++", {"Text": "  "}, [], 1)


class NewGuidAlphabetTest(unittest.TestCase):
    def test_new_guids_are_letters_and_digits_only(self):
        import re
        with running_server():
            with server.db() as conn:
                guids = [revisions.new_guid(conn) for _ in range(500)]
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9]{10}", g) for g in guids), guids[:5])
