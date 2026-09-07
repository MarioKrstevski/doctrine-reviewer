"""init_db() must upgrade an existing database without losing data.

Production already has a suggestions table created before auth existed.
Adding resolved_by has to be an ALTER, not a recreate.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import config
import server

OLD_SCHEMA = """
CREATE TABLE suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE,
    doctrine_id TEXT,
    anki_note_id INTEGER,
    note_type TEXT,
    deck TEXT,
    suggestion_type TEXT,
    text TEXT,
    email TEXT,
    snap_question TEXT,
    snap_answer TEXT,
    snap_css TEXT,
    snap_fields_json TEXT,
    snap_hash TEXT,
    status TEXT DEFAULT 'open',
    resolution_note TEXT,
    published INTEGER DEFAULT 0,
    publish_summary TEXT,
    credit_name TEXT,
    created_at TEXT,
    closed_at TEXT
);
"""


class MigrationTest(unittest.TestCase):
    def test_existing_suggestions_survive_the_auth_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO suggestions (token, doctrine_id, text, email, status)"
                " VALUES (?,?,?,?,?)",
                ("tok123", "doc-old0000001", "pre-existing", "s@example.com", "open"),
            )
            conn.commit()
            conn.close()

            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                server.init_db()

                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM suggestions WHERE token='tok123'"
                ).fetchone()
                cols = {r[1] for r in conn.execute("PRAGMA table_info(suggestions)")}
                users = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
                ).fetchone()
                conn.close()
            finally:
                server.CFG = previous

        self.assertIsNotNone(row, "the pre-existing suggestion was lost")
        self.assertEqual("pre-existing", row["text"])
        self.assertEqual("s@example.com", row["email"], "submitter email was lost")
        self.assertIn("resolved_by", cols, "resolved_by was not added")
        self.assertIsNone(row["resolved_by"])
        self.assertIsNotNone(users, "users table was not created")

    def test_running_init_db_twice_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "twice.db")
            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                server.init_db()
                server.init_db()
            finally:
                server.CFG = previous
