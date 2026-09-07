"""Suggestions carry trace data: tags, card/deck ids, install id, versions."""

import json
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path

import config
import server
from tests.helpers import register_note, running_server

TRACE = {
    "tags": ["#AK_Step1_v11::^Systems::Cardio", "#AK_Step2_v11::!Shelf::IM"],
    "note_mod": 1788816928,
    "card_id": 1368291918470,
    "card_ord": 1,
    "template_name": "Cloze",
    "deck_id": 1700000000001,
    "original_deck_id": None,
    "install_id": "0123456789abcdef0123456789abcdef",
    "addon_version": "1.1",
    "anki_version": "25.02",
}


def suggest(base, extra=None, did="6944" + "9" * 20):
    body = {"doctrine_id": did, "text": "dose wrong",
            "snapshot": {"fields": {"Front": "Q", "Back": "A"}}}
    body.update(extra or {})
    req = urllib.request.Request(
        base + "/api/suggestions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def last_row():
    with server.db() as conn:
        return dict(conn.execute("SELECT * FROM suggestions ORDER BY id DESC LIMIT 1").fetchone())


class StorageTest(unittest.TestCase):
    def test_trace_fields_are_stored(self):
        with running_server() as base:
            register_note(base, "6944" + "9" * 20)
            suggest(base, TRACE)
            row = last_row()
        self.assertEqual(TRACE["tags"], json.loads(row["tags_json"]))
        for key in ("note_mod", "card_id", "card_ord", "template_name", "deck_id",
                    "install_id", "addon_version", "anki_version"):
            self.assertEqual(TRACE[key], row[key], key)
        self.assertIsNone(row["original_deck_id"])

    def test_an_older_addon_without_trace_still_succeeds(self):
        with running_server() as base:
            register_note(base, "6944" + "9" * 20)
            body = suggest(base)
            row = last_row()
        self.assertTrue(body["ok"])
        self.assertEqual([], json.loads(row["tags_json"] or "[]"))
        self.assertIsNone(row["card_id"])

    def test_tags_do_not_affect_the_content_hash(self):
        with running_server() as base:
            register_note(base, "6944" + "9" * 20)
            suggest(base, TRACE)
            row = last_row()
            with server.db() as conn:
                master = conn.execute("SELECT content_hash FROM notes").fetchone()
        # Snapshot had no content_hash; the server does not compute one
        # from tags. The master hash covers fields only.
        self.assertEqual(server.content_hash({"Front": "Q", "Back": "A"}),
                         master["content_hash"])


class MigrationTest(unittest.TestCase):
    def test_columns_are_added_to_an_existing_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            c = sqlite3.connect(path)
            c.executescript("""CREATE TABLE suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE,
                doctrine_id TEXT, text TEXT, status TEXT DEFAULT 'open');""")
            c.execute("INSERT INTO suggestions (token, doctrine_id, text) VALUES ('t','d','x')")
            c.commit(); c.close()
            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                server.init_db()
                with server.db() as conn:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(suggestions)")}
                    row = conn.execute("SELECT text FROM suggestions").fetchone()
            finally:
                server.CFG = previous
        for col in ("tags_json", "card_id", "install_id", "anki_version"):
            self.assertIn(col, cols)
        self.assertEqual("x", row["text"])


class RenderTest(unittest.TestCase):
    def _queue(self, base, extra):
        register_note(base, "6944" + "9" * 20)
        suggest(base, extra)
        return server.render_reviewer({"id": 1, "username": "r", "role": "reviewer"},
                                      "secret", "sess")

    def test_tags_render_as_chips(self):
        with running_server() as base:
            html = self._queue(base, TRACE)
        self.assertIn('class="chip"', html)
        self.assertIn("Cardio", html)

    def test_trace_block_shows_ids_and_versions(self):
        with running_server() as base:
            html = self._queue(base, TRACE)
        self.assertIn("<details", html)
        self.assertIn("1368291918470", html)   # card id
        self.assertIn("1700000000001", html)   # deck id
        self.assertIn("01234567", html)         # shortened install id
        self.assertIn("25.02", html)
        self.assertIn("Cloze", html)

    def test_absent_values_do_not_render_as_none(self):
        with running_server() as base:
            html = self._queue(base, {})
        self.assertNotIn(">None<", html)
        self.assertNotIn("None</", html)

    def test_a_filtered_deck_shows_the_original_deck(self):
        with running_server() as base:
            html = self._queue(base, dict(TRACE, original_deck_id=555))
        self.assertIn("555", html)
