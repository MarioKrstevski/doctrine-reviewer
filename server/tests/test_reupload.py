"""Re-upload with diff: compare a deck file against the online deck.

Pure comparison over {guid: {"fields": {...}, "tags": [...]}} maps, then
an apply step that only touches what staff confirmed. Guards: refuse when
most guids are unknown (the export broke the ids); flag conflicts where a
note changed on the platform after the file's base release.
"""

import json
import unittest

import reupload
import revisions
import server
from tests.helpers import register_note, running_server

A, B, C = "P]%rwtghL]", "s.MBeTB!D.", "j,^]wFzg[,"


def online(conn):
    return reupload.snapshot(conn)


class CompareTest(unittest.TestCase):
    def test_classifies_new_changed_unchanged_missing(self):
        current = {A: {"fields": {"Text": "a", "Extra": "e"}, "tags": ["t"]},
                   B: {"fields": {"Text": "b", "Extra": ""}, "tags": []}}
        incoming = {A: {"fields": {"Text": "a2", "Extra": "e"}, "tags": ["t"]},
                    C: {"fields": {"Text": "c", "Extra": ""}, "tags": []}}
        d = reupload.compare(current, incoming)
        self.assertEqual({A: {"Text": ("a", "a2")}}, {k: v["fields"] for k, v in d["changed"].items()})
        self.assertEqual([C], list(d["new"]))
        self.assertEqual([B], d["missing"])
        self.assertEqual(0, d["unchanged"])

    def test_tag_changes_are_reported_separately_never_applied(self):
        current = {A: {"fields": {"Text": "a"}, "tags": ["x"]}}
        incoming = {A: {"fields": {"Text": "a"}, "tags": ["y"]}}
        d = reupload.compare(current, incoming)
        self.assertEqual({}, d["changed"])
        self.assertEqual({A: (["x"], ["y"])}, d["tag_changes"])
        self.assertEqual(1, d["unchanged"])

    def test_non_editable_field_changes_are_reported_but_not_applicable(self):
        current = {A: {"fields": {"Text": "a", "Pathoma": ""}, "tags": []}}
        incoming = {A: {"fields": {"Text": "a", "Pathoma": "filled"}, "tags": []}}
        d = reupload.compare(current, incoming)
        self.assertEqual({}, d["changed"])
        self.assertEqual({A: ["Pathoma"]}, d["other_field_changes"])

    def test_refuses_when_most_guids_are_unknown(self):
        current = {A: {"fields": {"Text": "a"}, "tags": []}, B: {"fields": {"Text": "b"}, "tags": []}}
        incoming = {"x1": {"fields": {"Text": "a"}, "tags": []}, "x2": {"fields": {"Text": "b"}, "tags": []},
                    "x3": {"fields": {"Text": "c"}, "tags": []}}
        with self.assertRaises(reupload.IdMismatch):
            reupload.compare(current, incoming)

    def test_small_set_of_new_notes_is_fine(self):
        current = {A: {"fields": {"Text": "a"}, "tags": []}}
        incoming = {A: {"fields": {"Text": "a"}, "tags": []}, C: {"fields": {"Text": "c"}, "tags": []}}
        self.assertEqual([C], list(reupload.compare(current, incoming)["new"]))


class ConflictTest(unittest.TestCase):
    def test_a_note_edited_on_the_platform_since_the_base_is_a_conflict(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "a", "Extra": "e"})
            with server.db() as conn:
                revisions.apply_edit(conn, A, {"Text": "platform"}, 1, "staff")
                current = online(conn)
                incoming = {A: {"fields": {"Text": "file", "Extra": "e"}, "tags": []}}
                d = reupload.compare(current, incoming, pending_guids=reupload.pending_guids(conn))
        self.assertIn(A, d["conflicts"])
        self.assertEqual(("platform", "file"), d["conflicts"][A]["Text"])
        self.assertNotIn(A, d["changed"])


class ApplyTest(unittest.TestCase):
    def test_apply_changed_and_new_records_revisions_with_upload_source(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "a", "Extra": "e"})
            with server.db() as conn:
                current = online(conn)
                incoming = {A: {"fields": {"Text": "a2", "Extra": "e"}, "tags": []},
                            C: {"fields": {"Text": "c", "Extra": ""}, "tags": ["t"]}}
                d = reupload.compare(current, incoming)
                out = reupload.apply(conn, d, incoming, decisions={A: "file", C: "add"},
                                     user_id=3, upload_id=5)
                fields = json.loads(conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (A,)).fetchone()["fields_json"])
                newrow = conn.execute("SELECT * FROM notes WHERE doctrine_id=?", (C,)).fetchone()
                srcs = {r["source"] for r in conn.execute("SELECT source FROM revisions")}
        self.assertEqual("a2", fields["Text"])
        self.assertIsNotNone(newrow)
        self.assertEqual(["t"], json.loads(newrow["tags_json"]))
        self.assertEqual({"upload:5"}, srcs)
        self.assertEqual({"changed": 1, "added": 1, "retired": 0, "kept": 0, "tagged": 0}, out)

    def test_decisions_keep_platform_or_skip(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "a"})
            with server.db() as conn:
                current = online(conn)
                incoming = {A: {"fields": {"Text": "file"}, "tags": []}}
                d = reupload.compare(current, incoming)
                out = reupload.apply(conn, d, incoming, decisions={A: "platform"}, user_id=1, upload_id=1)
                text = json.loads(conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (A,)).fetchone()["fields_json"])["Text"]
        self.assertEqual("a", text)
        self.assertEqual(1, out["kept"])

    def test_missing_notes_are_retired_only_when_asked(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "a"}); register_note(base, B, fields={"Text": "b"})
            with server.db() as conn:
                d = reupload.compare(online(conn), {A: {"fields": {"Text": "a"}, "tags": []}})
                reupload.apply(conn, d, {A: {"fields": {"Text": "a"}, "tags": []}}, decisions={}, user_id=1, upload_id=1)
                still = conn.execute("SELECT retired FROM notes WHERE doctrine_id=?", (B,)).fetchone()["retired"]
                reupload.apply(conn, d, {A: {"fields": {"Text": "a"}, "tags": []}}, decisions={B: "retire"}, user_id=1, upload_id=1)
                now = conn.execute("SELECT retired FROM notes WHERE doctrine_id=?", (B,)).fetchone()["retired"]
        self.assertEqual(0, still)
        self.assertEqual(1, now)
