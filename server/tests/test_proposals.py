"""Students may propose the exact change; reviewers see it as a diff."""

import json
import unittest
import urllib.request

import server
from tests.helpers import register_note, running_server

GUID = "P]%rwtghL]"


def suggest(base, proposed=None, text="wording"):
    body = {"doctrine_id": GUID, "guid": GUID, "text": text,
            "snapshot": {"fields": {"Text": "HPV11 has {{c1::low}} cancer risk", "Extra": "e"}}}
    if proposed is not None:
        body["proposed_fields"] = proposed
    req = urllib.request.Request(base + "/api/suggestions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def last():
    with server.db() as conn:
        return dict(conn.execute("SELECT * FROM suggestions ORDER BY id DESC LIMIT 1").fetchone())


def queue():
    return server.render_reviewer({"id": 1, "username": "r", "role": "reviewer"}, "s", "t")


class StoreProposalTest(unittest.TestCase):
    def test_editable_fields_are_kept_others_dropped(self):
        with running_server() as base:
            register_note(base, GUID)
            suggest(base, {"Text": "HPV11 has {{c1::low}} malignancy risk", "Pathoma": "x", "Extra": "e"})
            row = last()
        self.assertEqual({"Text": "HPV11 has {{c1::low}} malignancy risk", "Extra": "e"},
                         json.loads(row["proposed_fields_json"]))

    def test_absent_proposal_stores_null(self):
        with running_server() as base:
            register_note(base, GUID)
            suggest(base)
            self.assertIsNone(last()["proposed_fields_json"])

    def test_junk_proposal_is_ignored(self):
        with running_server() as base:
            register_note(base, GUID)
            suggest(base, "not a dict")
            self.assertIsNone(last()["proposed_fields_json"])


class DiffRenderTest(unittest.TestCase):
    def test_queue_shows_a_diff_with_ins_and_del(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "HPV11 has {{c1::low}} cancer risk", "Extra": "e"})
            suggest(base, {"Text": "HPV11 has {{c1::low}} malignancy risk"})
            html = queue()
        self.assertIn("Proposed change", html)
        self.assertIn("<del>cancer</del>", html)
        self.assertIn("<ins>malignancy</ins>", html)
        self.assertIn("Accept proposal", html)

    def test_diff_is_against_the_current_master_not_the_snapshot(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "CURRENT text", "Extra": "e"})
            suggest(base, {"Text": "CURRENT words"})   # snapshot differs from master
            html = queue()
        self.assertIn("<del>text</del>", html)
        self.assertIn("<ins>words</ins>", html)

    def test_diff_escapes_html(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "<b>bold</b>", "Extra": ""})
            suggest(base, {"Text": "<i>bold</i>"})
            html = queue()
        self.assertNotIn("<i>bold</i>", html)
        self.assertIn("&lt;i&gt;", html)

    def test_unit_diff_marks_unchanged_fields_as_such(self):
        out = server.diff_fields({"Text": "a b c", "Extra": "same"}, {"Text": "a X c", "Extra": "same"})
        self.assertEqual(["Text"], [f for f, _ in out])
