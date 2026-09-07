"""Readers must not block behind writers, and DB errors must not crash requests.

Seen in production: a 348-batch registration ran while a reviewer logged
in. Under SQLite's default rollback journal every reader waits for the
writer; the default 5s wait expired, threads raised 'database is locked'
mid-request, and Railway reported 'Application failed to respond'.
"""

import sqlite3
import threading
import time
import unittest
import urllib.request
from unittest import mock

import server
from tests.helpers import running_server

HOLD_SECONDS = 3.0


class WalModeTest(unittest.TestCase):
    def test_init_db_switches_the_file_to_wal(self):
        with running_server():
            with server.db() as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual("wal", mode.lower())

    def test_a_reader_returns_immediately_while_a_writer_holds_the_lock(self):
        with running_server() as base:
            writer_ready = threading.Event()
            release = threading.Event()

            def hold_write_lock():
                raw = sqlite3.connect(server.CFG.db_path, timeout=30)
                raw.execute("BEGIN IMMEDIATE")
                raw.execute("INSERT INTO login_attempts (username, ok, attempted_at)"
                            " VALUES ('holder', 0, 'x')")
                writer_ready.set()
                release.wait(HOLD_SECONDS + 5)
                raw.commit()
                raw.close()

            t = threading.Thread(target=hold_write_lock, daemon=True)
            t.start()
            self.assertTrue(writer_ready.wait(5), "writer never took the lock")
            try:
                started = time.monotonic()
                with urllib.request.urlopen(base + "/updates", timeout=30) as r:
                    status = r.status
                elapsed = time.monotonic() - started
            finally:
                release.set()
                t.join(5)

        self.assertEqual(200, status)
        self.assertLess(elapsed, 1.0,
                        f"reader waited {elapsed:.1f}s behind a writer -- not WAL")

    def test_connections_are_closed_on_context_exit(self):
        with running_server():
            with server.db() as conn:
                pass
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


class BusyTimeoutTest(unittest.TestCase):
    def test_a_writer_waits_for_a_held_lock_rather_than_failing_at_once(self):
        with running_server():
            # Released from another thread, so allow cross-thread use.
            raw = sqlite3.connect(server.CFG.db_path, timeout=30,
                                  check_same_thread=False)
            raw.execute("BEGIN IMMEDIATE")
            raw.execute("INSERT INTO login_attempts (username, ok, attempted_at)"
                        " VALUES ('holder', 0, 'x')")

            def release_later():
                time.sleep(1.0)
                raw.commit()
                raw.close()

            threading.Thread(target=release_later, daemon=True).start()
            started = time.monotonic()
            server.record_attempt("someone", False)   # a write; must wait, not raise
            self.assertGreaterEqual(time.monotonic() - started, 0.9)


class GracefulDbErrorTest(unittest.TestCase):
    def test_a_locked_database_yields_503_not_a_dropped_connection(self):
        with running_server() as base:
            with mock.patch.object(server, "render_updates",
                                   side_effect=sqlite3.OperationalError("database is locked")):
                try:
                    urllib.request.urlopen(base + "/updates", timeout=10)
                    self.fail("expected an HTTP error")
                except urllib.error.HTTPError as e:
                    self.assertEqual(503, e.code)
                    self.assertIn("Retry-After", e.headers)
