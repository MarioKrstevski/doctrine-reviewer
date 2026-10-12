"""Baseline collection blob, media files and the releases table.

A later release is produced by copying the baseline collection and patching
notes, so the baseline is stored byte-for-byte. Media is stored by sha256
and served under /media/<filename> for the reviewer panes.
"""

import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.parse
import urllib.request

import server
from tests.helpers import TEST_PIPELINE_KEY, running_server

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def raw(base, method, path, body=b"", key=TEST_PIPELINE_KEY, ctype="application/octet-stream"):
    u = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    conn.request(method, path, body=body, headers=headers)
    r = conn.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    conn.close()
    return out


def make_sqlite():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.db")
        c = sqlite3.connect(p); c.execute("CREATE TABLE col (id INTEGER)"); c.execute("INSERT INTO col VALUES (1)"); c.commit(); c.close()
        return open(p, "rb").read()


class MediaTest(unittest.TestCase):
    def test_upload_requires_the_key(self):
        with running_server() as base:
            status, _, _ = raw(base, "POST", "/api/media/x.png", PNG, key=None)
        self.assertEqual(401, status)

    def test_upload_stores_and_serves(self):
        with running_server() as base:
            status, _, body = raw(base, "POST", "/api/media/heart%20sounds.png", PNG)
            self.assertEqual(200, status)
            info = json.loads(body)
            self.assertEqual(hashlib.sha256(PNG).hexdigest(), info["sha256"])
            status, headers, served = raw(base, "GET", "/media/heart%20sounds.png", key=None)
        self.assertEqual(200, status)
        self.assertEqual(PNG, served)
        self.assertEqual("image/png", headers["Content-Type"])
        self.assertIn("immutable", headers.get("Cache-Control", ""))

    def test_duplicate_upload_is_idempotent(self):
        with running_server() as base:
            raw(base, "POST", "/api/media/a.png", PNG)
            status, _, body = raw(base, "POST", "/api/media/a.png", PNG)
            self.assertEqual(200, status)
            self.assertTrue(json.loads(body)["existing"])
            with server.db() as conn:
                n = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        self.assertEqual(1, n)

    def test_unknown_media_is_404(self):
        with running_server() as base:
            status, _, _ = raw(base, "GET", "/media/nope.png", key=None)
        self.assertEqual(404, status)

    def test_path_traversal_is_refused(self):
        with running_server() as base:
            status, _, _ = raw(base, "POST", "/api/media/..%2F..%2Fetc%2Fpasswd", PNG)
            self.assertEqual(400, status)
            status, _, _ = raw(base, "GET", "/media/..%2Fdoctrine-v1.db", key=None)
            self.assertEqual(404, status)

    def test_manifest_lists_filenames(self):
        with running_server() as base:
            raw(base, "POST", "/api/media/a.png", PNG)
            raw(base, "POST", "/api/media/b.mp3", b"ID3" + b"\x00" * 10)
            status, _, body = raw(base, "GET", "/api/media/manifest")
        self.assertEqual(200, status)
        self.assertEqual({"a.png", "b.mp3"}, set(json.loads(body)["files"]))

    def test_media_srcs_in_panes_are_rewritten(self):
        html = server.card_iframe('<img src="heart sounds.png"><img src="https://x/y.png">', "", "", "L")
        self.assertIn('src="/media/heart%20sounds.png"', html.replace("&quot;", '"'))
        self.assertIn('src="https://x/y.png"', html.replace("&quot;", '"'))


class BaselineTest(unittest.TestCase):
    def test_upload_creates_release_and_stores_blob(self):
        blob = make_sqlite()
        with running_server() as base:
            status, _, body = raw(base, "POST", "/api/baseline?version=1.0&note_count=3", blob)
            self.assertEqual(200, status)
            info = json.loads(body)
            with server.db() as conn:
                rel = dict(conn.execute("SELECT * FROM releases WHERE version='1.0'").fetchone())
                cur = conn.execute("SELECT value FROM settings WHERE key='current_release'").fetchone()[0]
            stored = open(info["path"], "rb").read()
        self.assertEqual(blob, stored)
        self.assertEqual("published", rel["status"])
        self.assertEqual(3, rel["note_count"])
        self.assertEqual("1.0", cur)

    def test_upload_requires_key_and_version(self):
        with running_server() as base:
            self.assertEqual(401, raw(base, "POST", "/api/baseline?version=1.0", b"x", key=None)[0])
            self.assertEqual(400, raw(base, "POST", "/api/baseline", b"x")[0])

    def test_rejects_a_non_sqlite_blob(self):
        with running_server() as base:
            status, _, _ = raw(base, "POST", "/api/baseline?version=1.0", b"this is not sqlite")
        self.assertEqual(400, status)

    def test_releases_list_is_public(self):
        with running_server() as base:
            raw(base, "POST", "/api/baseline?version=1.0&note_count=3", make_sqlite())
            status, _, body = raw(base, "GET", "/api/releases", key=None)
        self.assertEqual(200, status)
        self.assertEqual("1.0", json.loads(body)["current"])

    def test_official_list_reports_the_release(self):
        with running_server() as base:
            raw(base, "POST", "/api/baseline?version=1.0&note_count=0", make_sqlite())
            status, _, body = raw(base, "GET", "/api/official", key=None)
        self.assertEqual("1.0", json.loads(body)["release"])
