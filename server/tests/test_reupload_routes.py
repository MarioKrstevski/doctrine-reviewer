"""Re-upload over HTTP: submit notes with the pipeline key, review in the
browser, apply with decisions."""

import json
import unittest
import urllib.request

import auth
import revisions
import server
from tests.helpers import TEST_PIPELINE_KEY, register_note, running_server
from tests.test_auth_routes import login, make_user, request

A, B, C = "P]%rwtghL]", "s.MBeTB!D.", "j,^]wFzg[,"


def submit(base, notes, key=TEST_PIPELINE_KEY):
    req = urllib.request.Request(base + "/api/reupload", data=json.dumps({"notes": notes, "label": "test.apkg"}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def note(guid, text, extra="", tags=None):
    return {"doctrine_id": guid, "guid": guid, "note_type": "AnKingOverhaul++++++",
            "fields": {"Text": text, "Extra": extra}, "tags": tags or []}


def csrf_for(cookie):
    return auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())


class SubmitTest(unittest.TestCase):
    def test_requires_key(self):
        with running_server() as base:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                submit(base, [note(A, "a")], key="wrong")
        self.assertEqual(401, ctx.exception.code)

    def test_stores_an_upload_with_a_summary(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "a", "Extra": ""}); register_note(base, B, fields={"Text": "b", "Extra": ""})
            status, body = submit(base, [note(A, "a2"), note(C, "c")])
            with server.db() as conn:
                row = dict(conn.execute("SELECT * FROM uploads ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(200, status)
        self.assertEqual({"new": 1, "changed": 1, "conflicts": 0, "missing": 1, "unchanged": 0,
                          "tag_changes": 0, "other_field_changes": 0}, body["summary"])
        self.assertIn(f"/uploads/{row['id']}", body["review_url"])
        self.assertEqual("pending", row["status"])

    def test_id_mismatch_is_refused_and_not_stored(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "a"}); register_note(base, B, fields={"Text": "b"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                submit(base, [note("x1", "a"), note("x2", "b"), note("x3", "c")])
            with server.db() as conn:
                n = conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        self.assertEqual(400, ctx.exception.code)
        self.assertIn("did not preserve", json.loads(ctx.exception.read())["error"])
        self.assertEqual(0, n)


class ReviewPageTest(unittest.TestCase):
    def _upload(self, base):
        register_note(base, A, fields={"Text": "a", "Extra": ""}); register_note(base, B, fields={"Text": "b", "Extra": ""})
        with server.db() as conn:
            revisions.apply_edit(conn, B, {"Text": "platform"}, 1, "staff")
        _, body = submit(base, [note(A, "a2"), note(B, "file"), note(C, "c")])
        return body["review_url"].rsplit("/", 1)[1]

    def test_requires_login(self):
        with running_server() as base:
            uid = self._upload(base)
            self.assertEqual(303, request(base, "GET", f"/uploads/{uid}")[0])

    def test_shows_sections_and_diffs(self):
        with running_server() as base:
            uid = self._upload(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, _, html = request(base, "GET", f"/uploads/{uid}", cookie=cookie)
        self.assertEqual(200, status)
        for word in ("Changed", "Conflicts", "New notes", "0 missing from the file", "<del>a</del>", "<ins>a2</ins>",
                     "platform", "file", "Apply"):
            self.assertIn(word, html)

    def test_apply_with_decisions(self):
        with running_server() as base:
            uid = self._upload(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, headers, _ = request(base, "POST", f"/uploads/{uid}/apply",
                                         {"csrf": csrf_for(cookie), f"d:{A}": "file", f"d:{B}": "platform",
                                          f"d:{C}": "add"}, cookie=cookie)
            self.assertEqual(303, status)
            with server.db() as conn:
                a = json.loads(conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (A,)).fetchone()["fields_json"])["Text"]
                b = json.loads(conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (B,)).fetchone()["fields_json"])["Text"]
                c = conn.execute("SELECT 1 FROM notes WHERE doctrine_id=?", (C,)).fetchone()
                up = dict(conn.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone())
        self.assertEqual("a2", a)
        self.assertEqual("platform", b)
        self.assertIsNotNone(c)
        self.assertEqual("applied", up["status"])
        self.assertIn("/releases/pending", headers.get("Location", ""))

    def test_apply_twice_is_refused(self):
        with running_server() as base:
            uid = self._upload(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            request(base, "POST", f"/uploads/{uid}/apply", {"csrf": csrf_for(cookie), f"d:{A}": "file"}, cookie=cookie)
            status, _, html = request(base, "POST", f"/uploads/{uid}/apply", {"csrf": csrf_for(cookie), f"d:{A}": "file"}, cookie=cookie)
            with server.db() as conn:
                n = conn.execute("SELECT COUNT(*) FROM revisions WHERE source LIKE 'upload:%'").fetchone()[0]
        self.assertEqual(1, n)

    def test_apply_needs_csrf(self):
        with running_server() as base:
            uid = self._upload(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, _, _ = request(base, "POST", f"/uploads/{uid}/apply", {f"d:{A}": "file"}, cookie=cookie)
        self.assertEqual(403, status)
