"""GET /api/sync?since=<version>: what a student's collection must change to
reach the current release. Public: it exposes only text every student has."""

import json
import unittest
import urllib.request

import releases
import revisions
import server
from tests.helpers import register_note, running_server

A, B, C = "P]%rwtghL]", "s.MBeTB!D.", "j,^]wFzg[,"


def publish(version):
    with server.db() as conn:
        conn.execute("INSERT OR REPLACE INTO releases (version, status, note_count, created_at, published_at) "
                     "VALUES (?,'published',0,?,?)", (version, server.now(), server.now()))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_release', ?)", (version,))


def stamp(version):
    with server.db() as conn:
        revisions.stamp_release(conn, version)


def sync(base, since):
    with urllib.request.urlopen(base + f"/api/sync?since={urllib.parse.quote(since)}") as r:
        return r.status, json.loads(r.read())


import urllib.parse


class SyncPayloadTest(unittest.TestCase):
    def _seed(self, base):
        publish("1.0")
        register_note(base, A, fields={"Text": "a0", "Extra": "ea"})
        register_note(base, B, fields={"Text": "b0", "Extra": "eb"})
        with server.db() as conn:
            releases.store_media(conn, "pic.png", b"\x89PNG" + b"\x00" * 9)
            revisions.apply_edit(conn, A, {"Text": 'a1 <img src="pic.png">'}, 1, "staff")
        stamp("1.1"); publish("1.1")
        with server.db() as conn:
            revisions.apply_edit(conn, A, {"Text": 'a2 <img src="pic.png"> <img src="missing.png">'}, 1, "staff")
            revisions.add_note(conn, "AnKingOverhaul++++++", {"Text": "new {{c1::x}}", "Extra": ""}, ["Doctorine::Yield::Step1::Gold"], 1)
            conn.execute("UPDATE notes SET retired=1 WHERE doctrine_id=?", (B,))
            conn.execute("INSERT INTO revisions (guid, field, before, after, user_id, source, created_at) VALUES (?,?,?,?,?,?,?)",
                         (B, "retired", "0", "1", 1, "staff", server.now()))
        stamp("1.2"); publish("1.2")

    def test_changes_since_an_older_release_collapse_to_final_text(self):
        with running_server() as base:
            self._seed(base)
            status, body = sync(base, "1.0")
        self.assertEqual(200, status)
        self.assertEqual(("1.0", "1.2"), (body["from"], body["to"]))
        self.assertEqual(1, len(body["changed"]))
        ch = body["changed"][0]
        self.assertEqual(A, ch["guid"])
        self.assertEqual('a2 <img src="pic.png"> <img src="missing.png">', ch["fields"]["Text"])
        self.assertEqual("ea", ch["fields"]["Extra"])
        self.assertEqual("a0", ch["before"]["Text"], "before = text at the student's release, for local-edit detection")
        self.assertEqual(1, len(body["added"]))
        self.assertEqual("new {{c1::x}}", body["added"][0]["fields"]["Text"])
        self.assertEqual(["Doctorine::Yield::Step1::Gold"], body["added"][0]["tags"])
        self.assertEqual([B], body["retired"])
        self.assertEqual(["pic.png"], body["media"], "only referenced media that exists on the server")
        self.assertFalse(body["baseline_missing"])

    def test_up_to_date_is_empty(self):
        with running_server() as base:
            self._seed(base)
            _, body = sync(base, "1.2")
        self.assertEqual([], body["changed"]); self.assertEqual([], body["added"]); self.assertEqual([], body["retired"])

    def test_intermediate_release_only_gets_later_changes(self):
        with running_server() as base:
            self._seed(base)
            _, body = sync(base, "1.1")
        self.assertEqual('a1 <img src="pic.png">', body["changed"][0]["before"]["Text"])

    def test_unknown_since_returns_everything_since_the_first_release(self):
        with running_server() as base:
            self._seed(base)
            _, body = sync(base, "0.9-unknown")
        self.assertTrue(body["baseline_missing"])
        self.assertEqual(1, len(body["changed"]))

    def test_version_endpoint(self):
        with running_server() as base:
            self._seed(base)
            with urllib.request.urlopen(base + "/api/sync/version") as r:
                body = json.loads(r.read())
        self.assertEqual("1.2", body["current"])
        self.assertIn("published_at", body)
