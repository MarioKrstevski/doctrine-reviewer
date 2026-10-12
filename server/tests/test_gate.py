"""Only official notes are accepted, and the add-on can fetch the official set.

Official = a guid present in the notes table and not retired. Students' own
notes, local copies (new guid) and other decks are refused at submit time.
"""

import json
import unittest
import urllib.error
import urllib.request

import server
from tests.helpers import register_note, running_server

GUID = "P]%rwtghL]"


def suggest(base, guid):
    body = {"doctrine_id": guid, "guid": guid, "text": "typo",
            "snapshot": {"fields": {"Text": "Q"}}}
    req = urllib.request.Request(base + "/api/suggestions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    with urllib.request.urlopen(req) as r:
        return r.status, dict(r.headers), r.read()


class SubmitGateTest(unittest.TestCase):
    def test_official_guid_is_accepted(self):
        with running_server() as base:
            register_note(base, GUID)
            status, body = suggest(base, GUID)
        self.assertEqual(200, status)
        self.assertIn("/s/", body["tracking_url"])

    def test_unknown_guid_is_refused(self):
        with running_server() as base:
            register_note(base, GUID)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                suggest(base, "s.MBeTB!D.")
        self.assertEqual(404, ctx.exception.code)
        self.assertIn("not part of", json.loads(ctx.exception.read())["error"])

    def test_retired_guid_is_refused(self):
        with running_server() as base:
            register_note(base, GUID)
            with server.db() as conn:
                conn.execute("UPDATE notes SET retired=1 WHERE doctrine_id=?", (GUID,))
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                suggest(base, GUID)
        self.assertEqual(404, ctx.exception.code)

    def test_nothing_is_written_on_refusal(self):
        with running_server() as base:
            try:
                suggest(base, "nope")
            except urllib.error.HTTPError:
                pass
            with server.db() as conn:
                n = conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]
        self.assertEqual(0, n)


class OfficialListTest(unittest.TestCase):
    def test_lists_active_guids_with_etag(self):
        with running_server() as base:
            register_note(base, GUID)
            register_note(base, "s.MBeTB!D.")
            with server.db() as conn:
                conn.execute("UPDATE notes SET retired=1 WHERE doctrine_id=?", ("s.MBeTB!D.",))
            status, headers, raw = get(base, "/api/official")
        body = json.loads(raw)
        self.assertEqual(200, status)
        self.assertEqual([GUID], body["guids"])
        self.assertEqual(1, body["count"])
        self.assertIn("release", body)
        self.assertTrue(headers.get("ETag"))

    def test_if_none_match_returns_304(self):
        with running_server() as base:
            register_note(base, GUID)
            _, headers, _ = get(base, "/api/official")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                get(base, "/api/official", {"If-None-Match": headers["ETag"]})
        self.assertEqual(304, ctx.exception.code)

    def test_etag_changes_when_the_set_changes(self):
        with running_server() as base:
            register_note(base, GUID)
            _, h1, _ = get(base, "/api/official")
            register_note(base, "j,^]wFzg[,")
            _, h2, _ = get(base, "/api/official")
        self.assertNotEqual(h1["ETag"], h2["ETag"])

    def test_is_public(self):
        with running_server() as base:
            status, _, _ = get(base, "/api/official")
        self.assertEqual(200, status)
