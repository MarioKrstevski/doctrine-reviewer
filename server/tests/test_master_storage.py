"""Master copies are fields only.

Rendered card HTML was 47 KB per note against 509 bytes of actual content,
because the deck's card template inlines its scripts and styles into every
card; multiplied by 34,704 notes that was a 2 GB database and a six-minute
registration. The master record is now the note's fields, which is also
what a reviewer edits in Anki. The student's snapshot keeps its rendered
HTML: that is "exactly what they saw", and exists only per suggestion.
"""

import json
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path

import config
import server
from tests.helpers import TEST_PIPELINE_KEY, register_note, running_server

OLD_NOTES_SCHEMA = """
CREATE TABLE notes (
    doctrine_id TEXT PRIMARY KEY, anki_note_id INTEGER, note_type TEXT,
    deck TEXT, fields_json TEXT, question_html TEXT, answer_html TEXT,
    css TEXT, content_hash TEXT, version INTEGER DEFAULT 1, updated_at TEXT
);
"""


def columns(path, table):
    c = sqlite3.connect(path)
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    c.close()
    return cols


class MigrationTest(unittest.TestCase):
    def test_rendered_html_columns_are_dropped_and_fields_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            c = sqlite3.connect(path)
            c.executescript(OLD_NOTES_SCHEMA)
            c.execute("INSERT INTO notes (doctrine_id, fields_json, question_html, "
                      "answer_html, css, content_hash, version) VALUES (?,?,?,?,?,?,?)",
                      ("6944" + "0" * 20, json.dumps({"Text": "keep me"}),
                       "<big html>", "<big html>", "<big css>", "h", 3))
            c.commit(); c.close()

            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                server.init_db()
            finally:
                server.CFG = previous

            cols = columns(path, "notes")
            c = sqlite3.connect(path); c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM notes").fetchone(); c.close()

        for gone in ("question_html", "answer_html", "css"):
            self.assertNotIn(gone, cols, f"{gone} should have been dropped")
        self.assertEqual({"Text": "keep me"}, json.loads(row["fields_json"]))
        self.assertEqual(3, row["version"], "version must survive the migration")

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "twice.db")
            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                server.init_db(); server.init_db()
            finally:
                server.CFG = previous


class RegisterStoresFieldsOnlyTest(unittest.TestCase):
    def test_rendered_html_in_the_payload_is_accepted_but_not_stored(self):
        with running_server() as base:
            # Older DEV builds still send these keys; they must not break.
            register_note(base, "6944" + "1" * 20)
            cols = columns(server.CFG.db_path, "notes")
            with server.db() as conn:
                row = conn.execute("SELECT * FROM notes").fetchone()
        self.assertNotIn("question_html", cols)
        self.assertEqual({"Front": "Q", "Back": "A"}, json.loads(row["fields_json"]))

    def test_a_fields_only_payload_registers(self):
        with running_server() as base:
            payload = {"notes": [{"doctrine_id": "6944" + "2" * 20,
                                  "note_type": "AnKingOverhaul", "deck": "Doctrine",
                                  "fields": {"Text": "{{c1::Aorta}}", "Extra": "x"},
                                  "tags": ["a", "b"]}]}
            req = urllib.request.Request(
                base + "/api/master/sync", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {TEST_PIPELINE_KEY}"})
            with urllib.request.urlopen(req) as r:
                body = json.loads(r.read())
        self.assertEqual(1, body["registered"])


class MasterPaneTest(unittest.TestCase):
    def _queue_html(self, base, fields):
        did = "6944" + "3" * 20
        register_note(base, did, fields=fields)
        req = urllib.request.Request(
            base + "/api/suggestions",
            data=json.dumps({"doctrine_id": did, "text": "typo",
                             "snapshot": {"fields": fields,
                                          "question_html": "<b>seen</b>"}}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
        return server.render_reviewer({"id": 1, "username": "r", "role": "reviewer"},
                                      "secret", "sess")

    def test_master_pane_shows_field_names_and_values(self):
        with running_server() as base:
            html = self._queue_html(base, {"Text": "The {{c1::aorta}} is big",
                                           "Extra": "note here", "Empty": ""})
        self.assertIn("Text", html)
        self.assertIn("note here", html)
        self.assertIn("Current master", html)

    def test_cloze_markers_are_highlighted_not_rendered_away(self):
        with running_server() as base:
            html = self._queue_html(base, {"Text": "The {{c1::aorta}} is big"})
        self.assertIn("c1::", html, "reviewers need to see the cloze source")
        self.assertIn("<mark", html)

    def test_field_html_is_shown_as_source_not_executed(self):
        with running_server() as base:
            html = self._queue_html(base, {"Text": "<img src=x onerror=alert(1)>"})
        self.assertNotIn("<img src=x onerror", html)
        self.assertIn("&lt;img", html)

    def test_empty_fields_are_omitted(self):
        with running_server() as base:
            html = self._queue_html(base, {"Text": "t", "Lecture Notes": ""})
        self.assertNotIn("Lecture Notes", html)

    def test_snapshot_pane_still_renders_what_the_student_saw(self):
        with running_server() as base:
            html = self._queue_html(base, {"Text": "t"})
        self.assertIn("seen", html)
        self.assertIn("iframe", html)
