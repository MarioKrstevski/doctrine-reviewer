"""Regression tests for the issues found in the review of the Sonnet-era changes."""

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

import release_build
import releases
import revisions
import server
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request
from tests.test_cards import csrf_for
from tests.test_release_build import make_baseline
from tests.test_release_routes import seed_baseline


def post_json(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def person(base, name, role):
    user, pw = make_user(name, "staffpassword1", role)
    return login(base, user, pw)[1]


class StaleProposalServerSideTest(unittest.TestCase):
    def test_accept_refuses_a_proposal_written_against_an_older_card(self):
        with running_server() as base:
            register_note(base, "guidAAAAAA", fields={"Text": "v1 text", "Extra": "e"})
            post_json(base, "/api/suggestions", {"doctrine_id": "guidAAAAAA", "guid": "guidAAAAAA", "text": "typo",
                                                  "snapshot": {"fields": {"Text": "v1 text", "Extra": "e"}},
                                                  "proposed_fields": {"Text": "v1 text fixed"}})
            with server.db() as conn:
                revisions.apply_edit(conn, "guidAAAAAA", {"Text": "v2 text by staff"}, 1, "staff")
            server.reviewer_action({"id": ["1"], "do": ["accept_proposal"]}, 1)
            with server.db() as conn:
                text = json.loads(conn.execute("SELECT fields_json FROM notes").fetchone()[0])["Text"]
                status = conn.execute("SELECT status FROM suggestions").fetchone()[0]
        self.assertEqual("v2 text by staff", text)
        self.assertEqual("open", status)


class SharedMediaTest(unittest.TestCase):
    def test_identical_file_in_two_decks_is_in_both_packages_and_survives_deleting_one(self):
        with running_server() as base:
            second = server.add_deck("Scratch Deck")
            with server.db() as conn:
                releases.store_media(conn, "shared.png", b"SAME")
                releases.store_media(conn, "only2.png", b"ONLY2", second)
                releases.link_media(conn, "shared.png", second)   # what an upload of the same file does
                self.assertIn("shared.png", [m[0] for m in server.media_for_package(conn, second)])
                self.assertIn("shared.png", [m[0] for m in server.media_for_package(conn, 1)])
                only2 = releases.sha_path(conn.execute("SELECT sha256 FROM media WHERE filename='only2.png'").fetchone()[0])
            ok, _ = server.delete_deck(second, "Scratch Deck")
            self.assertTrue(ok)
            with server.db() as conn:
                names = {r[0] for r in conn.execute("SELECT filename FROM media")}
            self.assertEqual({"shared.png"}, names)
            self.assertFalse(os.path.exists(only2), "a file only the deleted deck used is freed")


class CompressedCollectionTest(unittest.TestCase):
    def test_compressed_once_and_reused(self):
        with tempfile.TemporaryDirectory() as d:
            col = os.path.join(d, "collection.sqlite"); make_baseline(col)
            first = release_build.compressed_collection(col)
            mtime = os.path.getmtime(first)
            self.assertEqual(first, release_build.compressed_collection(col))
            self.assertEqual(mtime, os.path.getmtime(first))


class StuckUploadTest(unittest.TestCase):
    def test_restart_marks_processing_uploads_failed(self):
        with running_server() as base:
            with server.db() as conn:
                conn.execute("INSERT INTO uploads (label, status, incoming_json, diff_json) VALUES ('x','processing','{}','{}')")
            server.init_db()
            with server.db() as conn:
                row = conn.execute("SELECT status, summary_json FROM uploads").fetchone()
        self.assertEqual("failed", row[0])
        self.assertIn("restarted", row[1])


class PingLimitTest(unittest.TestCase):
    def test_rate_limit_and_unknown_license_shapes(self):
        with running_server() as base:
            codes = [post_json(base, "/api/ping", {"install_id": f"install-{i:04d}"}) for i in range(server.PING_PER_ADDRESS_PER_HOUR + 1)]
            self.assertEqual(429, codes[-1])
            self.assertTrue(all(c == 200 for c in codes[:-1]))
            server._ping_seen.clear()
            post_json(base, "/api/ping", {"install_id": "install-lic1", "license": "not-a-real-id"})
            post_json(base, "/api/ping", {"install_id": "install-lic2", "license": "0123456789abcdef"})
            with server.db() as conn:
                lic = dict(conn.execute("SELECT install_id, license FROM installs WHERE install_id LIKE 'install-lic%'").fetchall())
        self.assertEqual({"install-lic1": None, "install-lic2": "0123456789abcdef"}, lic)


class ReleaseAuditTest(unittest.TestCase):
    def _cut(self, base, cookie, version, guids):
        request(base, "POST", "/releases/cut", {"version": version, "csrf": csrf_for(cookie),
                                                **{f"rev:{g}": "1" for g in guids}}, cookie=cookie)

    def test_snippet_is_text_as_of_release_and_list_page_previews(self):
        with running_server() as base:
            seed_baseline()
            guids = [f"guid{i:06d}" for i in range(7)]
            for g in guids:
                register_note(base, g, fields={"Text": f"{g} old", "Extra": ""})
            with server.db() as conn:
                for g in guids:
                    revisions.apply_edit(conn, g, {"Text": f"{g} as shipped"}, 1, "staff")
            admin = person(base, "boss", "admin")
            self._cut(base, admin, "1.1", guids)
            with server.db() as conn:
                revisions.apply_edit(conn, guids[0], {"Text": f"{guids[0]} rewritten later"}, 1, "staff")
            _, _, detail = request(base, "GET", "/releases/1.1", cookie=admin)
            _, _, listing = request(base, "GET", "/releases", cookie=admin)
        self.assertIn(f"{guids[0]} as shipped", detail)
        self.assertNotIn("rewritten later", detail)
        self.assertIn("and 2 more, see details", listing)

    def test_only_admin_or_preparer_can_discard(self):
        with running_server() as base:
            seed_baseline()
            register_note(base, "guidAAAAAA", fields={"Text": "x", "Extra": ""})
            with server.db() as conn:
                revisions.apply_edit(conn, "guidAAAAAA", {"Text": "y"}, 1, "staff")
            prep = person(base, "prep", "reviewer")
            other = person(base, "other", "reviewer")
            self._cut(base, prep, "1.1", ["guidAAAAAA"])
            status, _, _ = request(base, "POST", "/releases/1.1/discard", {"csrf": csrf_for(other)}, cookie=other)
            self.assertEqual(403, status)
            with server.db() as conn:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM releases WHERE version='1.1'").fetchone())
            status, _, _ = request(base, "POST", "/releases/1.1/discard", {"csrf": csrf_for(prep)}, cookie=prep)
            self.assertEqual(303, status)


class IdSearchWordsTest(unittest.TestCase):
    def test_words_starting_with_id_or_note_are_searched_as_typed(self):
        with running_server() as base:
            register_note(base, "guidAAAAAA", fields={"Text": "idiopathic thrombocytopenic purpura", "Extra": ""})
            register_note(base, "guidBBBBBB", fields={"Text": "something else", "Extra": ""})
            cookie = person(base, "rev", "reviewer")
            _, _, html = request(base, "GET", "/cards?q=idiopathic", cookie=cookie)
        self.assertIn("idiopathic thrombocytopenic", html)
        self.assertNotIn("something else", html)


if __name__ == "__main__":
    unittest.main()
