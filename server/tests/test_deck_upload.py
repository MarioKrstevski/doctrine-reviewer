"""Deck page: browser upload of an .apkg, compared against the online deck."""

import http.client
import json
import os
import tempfile
import time
import unittest
import urllib.parse

import releases
import release_build as rb
import server
from tests.helpers import register_note, running_server
from tests.test_cards import csrf_for
from tests.test_release_build import make_baseline
from tests.test_release_routes import staff
from tests.test_auth_routes import request


def make_apkg(d, edit=None, media=(), rename_guids=False):
    col = os.path.join(d, "c.sqlite"); make_baseline(col)
    c = rb.connect(col)
    if edit:
        c.execute("UPDATE notes SET flds=? WHERE guid='guidAAAAAA'", (edit,))
    if rename_guids:
        c.execute("UPDATE notes SET guid='zz'||guid")
    c.commit(); c.close()
    paths = []
    for name, data in media:
        p = os.path.join(d, "m_" + name); open(p, "wb").write(data); paths.append((name, p))
    out = os.path.join(d, "up.apkg")
    rb.write_apkg(out, col, paths)
    return out


def post_file(base, path, cookie, csrf):
    u = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=60)
    body = open(path, "rb").read()
    conn.request("POST", "/deck/upload?name=up.apkg", body=body,
                 headers={"Cookie": cookie, "X-CSRF": csrf, "Content-Length": str(len(body))})
    r = conn.getresponse(); out = (r.status, json.loads(r.read())); conn.close()
    return out


def wait(upload_id):
    for _ in range(150):
        with server.db() as conn:
            row = dict(conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone())
        if row["status"] != "processing":
            return row
        time.sleep(0.1)
    raise AssertionError("still processing")


class DeckUploadTest(unittest.TestCase):
    def _register(self, base):
        register_note(base, "guidAAAAAA", fields={"Text": "<b>p53</b> blocks {{c1::G1 to S}}", "Extra": "extra one"})
        register_note(base, "guidBBBBBB", fields={"Text": "{{c1::a}} and {{c2::b}}", "Extra": ""})
        register_note(base, "guidCCCCCC", fields={"Front": "front", "Back": "back"})

    def test_upload_compares_and_leaves_the_deck_untouched(self):
        with running_server() as base, tempfile.TemporaryDirectory() as d:
            self._register(base)
            cookie = staff(base, "admin")
            f = make_apkg(d, edit="<b>p53</b> blocks {{c1::G1 to S}} NEW\x1fextra one",
                          media=[("pic.png", b"\x89PNG1")])
            status, out = post_file(base, f, cookie, csrf_for(cookie))
            self.assertEqual(200, status)
            row = wait(out["upload_id"])
            self.assertEqual("pending", row["status"], row["summary_json"])
            summary = json.loads(row["summary_json"])
            self.assertEqual(1, summary["changed"])
            self.assertEqual(1, summary["media_new"])
            with server.db() as conn:
                self.assertIn("p53", json.loads(conn.execute(
                    "SELECT fields_json FROM notes WHERE doctrine_id='guidAAAAAA'").fetchone()[0])["Text"])
                self.assertNotIn("NEW", conn.execute("SELECT fields_json FROM notes WHERE doctrine_id='guidAAAAAA'").fetchone()[0])
            self.assertEqual([], os.listdir(os.path.join(releases.blob_root(), "tmp")), "temp file removed")
            _, _, html = request(base, "GET", f"/uploads/{out['upload_id']}", cookie=cookie)
            self.assertIn("NEW", html)

    def test_changed_image_with_the_same_name_is_renamed_and_references_follow(self):
        with running_server() as base, tempfile.TemporaryDirectory() as d:
            self._register(base)
            with server.db() as conn:
                releases.store_media(conn, "pic.png", b"\x89PNG-old")
            cookie = staff(base, "admin")
            f = make_apkg(d, edit='<img src="pic.png"> p53\x1fextra one', media=[("pic.png", b"\x89PNG-new")])
            _, out = post_file(base, f, cookie, csrf_for(cookie))
            row = wait(out["upload_id"])
            summary = json.loads(row["summary_json"])
            self.assertEqual(1, summary["media_renamed"])
            new_name = summary["renamed_files"]["pic.png"]
            self.assertNotEqual("pic.png", new_name)
            self.assertIn(f'src="{new_name}"', json.loads(row["incoming_json"])["guidAAAAAA"]["fields"]["Text"])
            with server.db() as conn:
                self.assertIsNotNone(releases.media_file(conn, new_name))
                self.assertIsNotNone(releases.media_file(conn, "pic.png"))

    def test_a_file_with_different_ids_is_refused(self):
        with running_server() as base, tempfile.TemporaryDirectory() as d:
            self._register(base)
            cookie = staff(base, "admin")
            _, out = post_file(base, make_apkg(d, rename_guids=True), cookie, csrf_for(cookie))
            row = wait(out["upload_id"])
        self.assertEqual("failed", row["status"])
        self.assertIn("note ids are unknown", json.loads(row["summary_json"])["error"])

    def test_reviewers_cannot_upload_and_space_guard_refuses(self):
        with running_server() as base, tempfile.TemporaryDirectory() as d:
            self._register(base)
            f = make_apkg(d)
            rev = staff(base, "reviewer")
            self.assertEqual(403, post_file(base, f, rev, csrf_for(rev))[0])
            admin = staff(base, "admin")
            orig = server.deck_space
            server.deck_space = lambda size: (False, 1e9, 5e9)
            try:
                status, out = post_file(base, f, admin, csrf_for(admin))
            finally:
                server.deck_space = orig
            self.assertEqual(507, status)
            self.assertIn("Not enough free space", out["error"])
            _, _, html = request(base, "GET", "/deck", cookie=admin)
            self.assertIn("Upload and compare", html)


if __name__ == "__main__":
    unittest.main()
