"""Add-on state lives in user_files/, not in the visible config.

Anki renders config.json in the add-on Config panel, so anything stored
there is shown to students and is editable by them. Internal state (the
resolved API base cache, the install id) must not appear there.
"""

import json
import tempfile
import unittest
from pathlib import Path

import state


class LoadTest(unittest.TestCase):
    def test_a_missing_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual({}, state.load(tmp))

    def test_it_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            state.save(tmp, {"api_base": "https://x.test", "fetched_at": 5})
            self.assertEqual({"api_base": "https://x.test", "fetched_at": 5},
                             state.load(tmp))

    def test_it_creates_the_directory_if_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = str(Path(tmp) / "user_files")
            state.save(nested, {"a": 1})
            self.assertEqual({"a": 1}, state.load(nested))

    def test_corrupt_json_reads_as_empty_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / state.FILENAME).write_text("{not json at all")
            self.assertEqual({}, state.load(tmp))

    def test_a_write_failure_does_not_raise(self):
        # A read-only location must not take down the suggestion flow.
        state.save("/proc/nonexistent-and-unwritable", {"a": 1})

    def test_install_id_is_created_once_and_then_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = state.install_id(tmp)
            second = state.install_id(tmp)
            self.assertEqual(first, second, "install id changed between calls")
            self.assertGreaterEqual(len(first), 16)

    def test_install_ids_differ_between_installs(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.assertNotEqual(state.install_id(a), state.install_id(b))
