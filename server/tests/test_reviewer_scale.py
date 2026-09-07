"""The reviewer page must not scale with the size of the deck.

It used to do `SELECT * FROM notes` to build a lookup dict -- every note's
fields, rendered HTML and a full copy of the note-type CSS, materialised
on every page view. Harmless at 5 notes; with 34,704 real notes it was
hundreds of MB per request and the container was OOM-killed.
"""

import re
import unittest
from unittest import mock

import server
from tests.helpers import register_note, running_server


class ReviewerQueryShapeTest(unittest.TestCase):
    def _trace_reviewer(self, base):
        statements = []
        real_db = server.db

        def traced_db():
            conn = real_db()
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(server, "db", traced_db):
            server.render_reviewer({"id": 1, "username": "r", "role": "reviewer"},
                                   "secret", "sess")
        return statements

    def test_an_empty_queue_never_touches_the_notes_table(self):
        with running_server() as base:
            for i in range(20):
                register_note(base, f"6944{i:020x}")
            statements = self._trace_reviewer(base)
        touching_notes = [s for s in statements
                          if re.search(r"\bFROM\s+notes\b", s, re.I)]
        self.assertEqual([], touching_notes,
                         "no suggestions, so there is nothing to look up")

    def test_every_read_of_notes_is_bounded_by_id(self):
        with running_server() as base:
            import json, urllib.request
            for i in range(20):
                register_note(base, f"6944{i:020x}")
            req = urllib.request.Request(
                base + "/api/suggestions",
                data=json.dumps({"doctrine_id": f"6944{5:020x}", "text": "x",
                                 "snapshot": {"fields": {"Front": "Q", "Back": "A"}}}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            statements = self._trace_reviewer(base)

        touching_notes = [s for s in statements
                          if re.search(r"\bFROM\s+notes\b", s, re.I)]
        self.assertTrue(touching_notes, "expected the page to look up the suggested note")
        for s in touching_notes:
            self.assertRegex(s, r"\bWHERE\b.*\bIN\b",
                             f"unbounded read of the notes table:\n{s}")

    def test_only_notes_with_suggestions_are_fetched(self):
        with running_server() as base:
            import json, urllib.request
            for i in range(20):
                register_note(base, f"6944{i:020x}")
            wanted = f"6944{3:020x}"
            req = urllib.request.Request(
                base + "/api/suggestions",
                data=json.dumps({"doctrine_id": wanted, "text": "x",
                                 "snapshot": {"fields": {"Front": "Q", "Back": "A"}}}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            statements = self._trace_reviewer(base)

        notes_reads = [s for s in statements if re.search(r"\bFROM\s+notes\b", s, re.I)]
        joined = " ".join(notes_reads)
        self.assertIn(wanted, joined, "the suggested note was not looked up by id")
        # A note with no suggestion must not appear in any query text.
        self.assertNotIn(f"6944{7:020x}", joined)
