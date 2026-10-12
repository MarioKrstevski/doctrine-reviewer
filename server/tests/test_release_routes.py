"""Release lifecycle: cut a draft from pending revisions, build, publish,
changelog; staff download and token downloads."""

import json
import os
import time
import unittest

import auth
import releases
import revisions
import server
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request
from tests.test_release_build import make_baseline

A = "P]%rwtghL]"


def csrf_for(cookie):
    return auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())


def seed_baseline(version="1.0"):
    """A synthetic release 1.0 whose collection matches a registered note."""
    with server.db() as conn:
        d = releases.release_dir(version); os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "collection.sqlite"); make_baseline(path)
        conn.execute("INSERT OR REPLACE INTO releases (version, status, note_count, collection_path, created_at) "
                     "VALUES (?,'published',3,?,?)", (version, path, server.now()))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_release', ?)", (version,))


def staff(base, role="reviewer"):
    user, pw = make_user("staff_" + role, "staffpassword1", role)
    return login(base, user, pw)[1]


class CutTest(unittest.TestCase):
    def test_cut_creates_a_draft_and_stamps_pending_revisions(self):
        with running_server() as base:
            seed_baseline()
            register_note(base, "guidAAAAAA", fields={"Text": "old", "Extra": "e"})
            with server.db() as conn:
                revisions.apply_edit(conn, "guidAAAAAA", {"Text": "new"}, 1, "staff")
            cookie = staff(base)
            status, headers, _ = request(base, "POST", "/releases/cut",
                                         {"version": "1.1", "csrf": csrf_for(cookie), "rev:guidAAAAAA": "1"}, cookie=cookie)
            with server.db() as conn:
                rel = dict(conn.execute("SELECT * FROM releases WHERE version='1.1'").fetchone())
                stamped = conn.execute("SELECT COUNT(*) FROM revisions WHERE release='1.1'").fetchone()[0]
                cur = releases.current_release(conn)
        self.assertEqual(303, status)
        self.assertEqual("draft", rel["status"])
        self.assertEqual(1, stamped)
        self.assertIn("guidAAAAAA", rel["changelog"])
        self.assertEqual("1.0", cur, "current release moves only on publish")

    def test_cut_with_nothing_pending_is_refused(self):
        with running_server() as base:
            seed_baseline()
            cookie = staff(base)
            status, _, html = request(base, "POST", "/releases/cut", {"version": "1.1", "csrf": csrf_for(cookie), "rev:guidAAAAAA": "1"}, cookie=cookie)
            with server.db() as conn:
                n = conn.execute("SELECT COUNT(*) FROM releases WHERE version='1.1'").fetchone()[0]
        self.assertEqual(0, n)

    def test_duplicate_version_is_refused(self):
        with running_server() as base:
            seed_baseline()
            cookie = staff(base)
            status, _, html = request(base, "POST", "/releases/cut", {"version": "1.0", "csrf": csrf_for(cookie), "rev:guidAAAAAA": "1"}, cookie=cookie)
        self.assertEqual(200, status)
        self.assertIn("already exists", html)

    def test_page_requires_login(self):
        with running_server() as base:
            self.assertEqual(303, request(base, "GET", "/releases")[0])


class BuildTest(unittest.TestCase):
    def _cut(self, base):
        seed_baseline()
        register_note(base, "guidAAAAAA", fields={"Text": "old", "Extra": "e"})
        with server.db() as conn:
            revisions.apply_edit(conn, "guidAAAAAA", {"Text": "<b>p53</b> NEW", "Extra": "e"}, 1, "staff")
            releases.store_media(conn, "pic.png", b"\x89PNG" + b"\x00" * 10)
        cookie = staff(base)
        request(base, "POST", "/releases/cut", {"version": "1.1", "csrf": csrf_for(cookie), "rev:guidAAAAAA": "1"}, cookie=cookie)
        return cookie

    def test_build_publishes_and_writes_a_valid_package(self):
        with running_server() as base:
            cookie = self._cut(base)
            status, _, _ = request(base, "POST", "/releases/1.1/build", {"csrf": csrf_for(cookie), "confirm": "1.1"}, cookie=cookie)
            self.assertEqual(303, status)
            for _ in range(100):
                with server.db() as conn:
                    rel = dict(conn.execute("SELECT * FROM releases WHERE version='1.1'").fetchone())
                if rel["status"] in ("published", "failed"):
                    break
                time.sleep(0.1)
            with server.db() as conn:
                cur = releases.current_release(conn)
            self.assertEqual("published", rel["status"], rel.get("build_log"))
            self.assertEqual("1.1", cur)
            self.assertTrue(os.path.exists(rel["package_path"]))
            import sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
            import apkg, release_build as rb
            pkg = apkg.Package(rel["package_path"])
            self.assertEqual({"0": "pic.png"}, pkg.media_names())
            col = pkg.collection_sqlite()
            tmp = rel["package_path"] + ".col"; open(tmp, "wb").write(col)
            c = rb.connect(tmp)
            self.assertEqual("<b>p53</b> NEW\x1fe", c.execute("SELECT flds FROM notes WHERE guid='guidAAAAAA'").fetchone()[0])
            pkg.close()

    def test_a_second_publish_removes_the_older_package_file(self):
        with running_server() as base:
            cookie = self._cut(base)
            request(base, "POST", "/releases/1.1/build", {"csrf": csrf_for(cookie), "confirm": "1.1"}, cookie=cookie)
            for _ in range(100):
                with server.db() as conn:
                    r11 = dict(conn.execute("SELECT * FROM releases WHERE version='1.1'").fetchone())
                if r11["status"] == "published": break
                time.sleep(0.1)
            with server.db() as conn:
                revisions.apply_edit(conn, "guidAAAAAA", {"Text": "again"}, 1, "staff")
            request(base, "POST", "/releases/cut", {"version": "1.2", "csrf": csrf_for(cookie), "rev:guidAAAAAA": "1"}, cookie=cookie)
            request(base, "POST", "/releases/1.2/build", {"csrf": csrf_for(cookie), "confirm": "1.2"}, cookie=cookie)
            for _ in range(100):
                with server.db() as conn:
                    r12 = dict(conn.execute("SELECT * FROM releases WHERE version='1.2'").fetchone())
                if r12["status"] == "published": break
                time.sleep(0.1)
            self.assertFalse(os.path.exists(r11["package_path"]), "older .apkg should be removed to save space")
            self.assertTrue(os.path.exists(r12["package_path"]))
            with server.db() as conn:
                self.assertTrue(os.path.exists(conn.execute("SELECT collection_path FROM releases WHERE version='1.1'").fetchone()[0]),
                                "per-version collection stays so 1.1 can be rebuilt")

    def test_updates_page_groups_by_release(self):
        with running_server() as base:
            cookie = self._cut(base)
            with server.db() as conn:
                conn.execute("INSERT INTO suggestions (token, doctrine_id, text, status, published, publish_summary, credit_name, closed_at, created_at) "
                             "VALUES ('t1','guidAAAAAA','x','resolved',1,'Fixed p53 wording','M.',?,?)", (server.now(), server.now()))
                conn.execute("UPDATE suggestions SET release='1.1'")
            html = server.render_updates()
        self.assertIn("Release 1.1", html)
        self.assertIn("Fixed p53 wording", html)


class DownloadTest(unittest.TestCase):
    def _published(self, base):
        cookie = BuildTest._cut(self, base)
        request(base, "POST", "/releases/1.1/build", {"csrf": csrf_for(cookie), "confirm": "1.1"}, cookie=cookie)
        for _ in range(100):
            with server.db() as conn:
                rel = dict(conn.execute("SELECT * FROM releases WHERE version='1.1'").fetchone())
            if rel["status"] == "published": break
            time.sleep(0.1)
        return cookie, rel

    def test_staff_download_needs_login_and_streams_the_package(self):
        import http.client, urllib.parse
        with running_server() as base:
            cookie, rel = self._published(base)
            self.assertEqual(303, request(base, "GET", "/download/release/latest")[0])
            u = urllib.parse.urlparse(base)
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
            conn.request("GET", "/download/release/latest", headers={"Cookie": cookie})
            r = conn.getresponse(); body = r.read(); headers = dict(r.getheaders()); status = r.status; conn.close()
            expected = open(rel["package_path"], "rb").read()
        self.assertEqual(200, status)
        self.assertEqual(str(len(expected)), headers["Content-Length"])
        self.assertIn("Doctorine-1.1.apkg", headers["Content-Disposition"])
        self.assertEqual(expected, body)

    def test_token_download(self):
        with running_server() as base:
            cookie, rel = self._published(base)
            admin_cookie = staff(base, "admin")
            status, _, html = request(base, "POST", "/releases/token", {"days": "7", "note": "test buyer", "csrf": csrf_for(admin_cookie)}, cookie=admin_cookie)
            self.assertEqual(200, status)
            import re
            m = re.search(r"/dl/([A-Za-z0-9_-]{20,})", html)
            self.assertIsNotNone(m, "token link not shown")
            status, headers, _ = request(base, "GET", f"/dl/{m.group(1)}")
            self.assertEqual(200, status)
            self.assertEqual(404, request(base, "GET", "/dl/nope-nope-nope-nope-nope")[0])
            with server.db() as conn:
                conn.execute("UPDATE download_tokens SET expires_at='2000-01-01 00:00 UTC'")
            self.assertEqual(404, request(base, "GET", f"/dl/{m.group(1)}")[0])

    def test_reviewers_cannot_mint_tokens(self):
        with running_server() as base:
            cookie, _ = self._published(base)
            status, _, _ = request(base, "POST", "/releases/token", {"days": "7", "csrf": csrf_for(cookie)}, cookie=cookie)
        self.assertEqual(403, status)
