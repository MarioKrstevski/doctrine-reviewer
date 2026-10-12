"""The add-on keeps a cached copy of the official guid set.

Fetched from GET /api/official at most every few hours, revalidated with
If-None-Match, kept on failure. None (never fetched) means "unknown":
the button shows and the server decides.
"""

import json
import tempfile
import unittest
from pathlib import Path

import official

GUIDS = ["P]%rwtghL]", "s.MBeTB!D."]


def ok_fetch(payload, etag='"abc"'):
    def fetch(url, headers):
        return 200, {"ETag": etag}, json.dumps(payload).encode()
    return fetch


def not_modified(url, headers):
    return 304, {}, b""


def boom(url, headers):
    raise OSError("offline")


class LoadTest(unittest.TestCase):
    def test_nothing_cached_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(official.load(d))

    def test_refresh_then_load(self):
        with tempfile.TemporaryDirectory() as d:
            official.refresh(d, "https://x", ok_fetch({"release": "1.0", "guids": GUIDS}), now=lambda: 1000)
            got = official.load(d)
        self.assertEqual(set(GUIDS), got.guids)
        self.assertEqual("1.0", got.release)

    def test_corrupt_cache_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / official.FILENAME).write_text("{nope")
            self.assertIsNone(official.load(d))


class RefreshTest(unittest.TestCase):
    def test_fetches_the_official_endpoint(self):
        seen = []
        def fetch(url, headers):
            seen.append((url, dict(headers)))
            return 200, {"ETag": '"e"'}, json.dumps({"release": "1.0", "guids": GUIDS}).encode()
        with tempfile.TemporaryDirectory() as d:
            official.refresh(d, "https://api.test", fetch, now=lambda: 0)
        self.assertEqual("https://api.test/api/official", seen[0][0])

    def test_sends_if_none_match_and_keeps_set_on_304(self):
        seen = []
        def nm(url, headers):
            seen.append(dict(headers)); return 304, {}, b""
        with tempfile.TemporaryDirectory() as d:
            official.refresh(d, "https://x", ok_fetch({"release": "1.0", "guids": GUIDS}, '"v1"'), now=lambda: 0)
            official.refresh(d, "https://x", nm, now=lambda: official.MIN_INTERVAL + 1)
            got = official.load(d)
        self.assertEqual('"v1"', seen[0].get("If-None-Match"))
        self.assertEqual(set(GUIDS), got.guids)

    def test_failure_keeps_the_old_set(self):
        with tempfile.TemporaryDirectory() as d:
            official.refresh(d, "https://x", ok_fetch({"release": "1.0", "guids": GUIDS}), now=lambda: 0)
            official.refresh(d, "https://x", boom, now=lambda: official.MIN_INTERVAL + 1)
            self.assertEqual(set(GUIDS), official.load(d).guids)

    def test_does_not_refetch_within_the_interval(self):
        calls = []
        def fetch(url, headers):
            calls.append(1); return 200, {"ETag": '"e"'}, json.dumps({"release": "1.0", "guids": GUIDS}).encode()
        with tempfile.TemporaryDirectory() as d:
            official.refresh(d, "https://x", fetch, now=lambda: 0)
            official.refresh(d, "https://x", fetch, now=lambda: official.MIN_INTERVAL - 1)
        self.assertEqual(1, len(calls))

    def test_force_ignores_the_interval(self):
        calls = []
        def fetch(url, headers):
            calls.append(1); return 200, {"ETag": '"e"'}, json.dumps({"release": "1.0", "guids": GUIDS}).encode()
        with tempfile.TemporaryDirectory() as d:
            official.refresh(d, "https://x", fetch, now=lambda: 0)
            official.refresh(d, "https://x", fetch, now=lambda: 1, force=True)
        self.assertEqual(2, len(calls))

    def test_a_junk_payload_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            official.refresh(d, "https://x", ok_fetch({"release": "1.0", "guids": GUIDS}), now=lambda: 0)
            official.refresh(d, "https://x", ok_fetch({"surprise": 1}), now=lambda: official.MIN_INTERVAL + 1, force=True)
            self.assertEqual(set(GUIDS), official.load(d).guids)


class GateTest(unittest.TestCase):
    def test_in_set_is_official(self):
        self.assertTrue(official.is_official("P]%rwtghL]", set(GUIDS)))

    def test_not_in_set_is_not(self):
        self.assertFalse(official.is_official("j,^]wFzg[,", set(GUIDS)))

    def test_unknown_set_defers_to_the_server(self):
        self.assertTrue(official.is_official("anything", None))

    def test_empty_guid_is_never_official(self):
        self.assertFalse(official.is_official("", set(GUIDS)))
        self.assertFalse(official.is_official(None, None))
