"""First-admin bootstrap.

A fresh deploy has no users, so nobody can log in and nobody can create
anyone. BOOTSTRAP_ADMIN names a username to create on startup; the
password is generated and printed to the logs once.

The safety property: it acts ONLY when there are zero admins. Leaving the
variable set must never reset an existing admin's password or create a
second way in.
"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import config
import server


class BootstrapAdminTest(unittest.TestCase):
    def _run(self, db_path, username):
        previous = server.CFG
        try:
            server.CFG = config.load({"DB_PATH": db_path})
            server.init_db()
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                server.bootstrap_admin(username)
            return buffer.getvalue()
        finally:
            server.CFG = previous

    def test_it_creates_the_first_admin_and_prints_the_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "b.db")
            output = self._run(path, "mario")

            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                with server.db() as conn:
                    row = conn.execute(
                        "SELECT role, active FROM users WHERE username='mario'"
                    ).fetchone()
            finally:
                server.CFG = previous

            self.assertIsNotNone(row, "admin was not created")
            self.assertEqual("admin", row["role"])
            self.assertIn("mario", output)
            self.assertIn("Password:", output)

    def test_it_does_nothing_when_an_admin_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "b.db")
            self._run(path, "mario")

            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                with server.db() as conn:
                    before = conn.execute(
                        "SELECT password_hash FROM users WHERE username='mario'"
                    ).fetchone()["password_hash"]
            finally:
                server.CFG = previous

            second = self._run(path, "mario")

            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                with server.db() as conn:
                    after = conn.execute(
                        "SELECT password_hash FROM users WHERE username='mario'"
                    ).fetchone()["password_hash"]
                    count = conn.execute(
                        "SELECT COUNT(*) c FROM users").fetchone()["c"]
            finally:
                server.CFG = previous

            self.assertEqual(before, after, "re-running reset the password")
            self.assertEqual(1, count, "a duplicate user was created")
            self.assertNotIn("Password:", second)

    def test_it_does_nothing_for_an_empty_username(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "b.db")
            self._run(path, "")
            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                with server.db() as conn:
                    count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            finally:
                server.CFG = previous
            self.assertEqual(0, count)

    def test_a_different_name_still_does_nothing_once_an_admin_exists(self):
        """Changing the variable must not mint a second admin."""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "b.db")
            self._run(path, "mario")
            self._run(path, "attacker")

            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": path})
                with server.db() as conn:
                    names = [r["username"] for r in
                             conn.execute("SELECT username FROM users")]
            finally:
                server.CFG = previous
            self.assertEqual(["mario"], names)
