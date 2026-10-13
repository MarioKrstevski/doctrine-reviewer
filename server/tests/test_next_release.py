"""Next release: accepted changes grouped by kind, selectable, cut with a confirm."""

import json
import unittest
import urllib.request

import auth
import releases
import revisions
import server
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request

A, B, C = "P]%rwtghL]", "s.MBeTB!D.", "j,^]wFzg[,"


def csrf_for(cookie):
    return auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())


def seed(base):
    """Three accepted changes of different kinds: a typo suggestion, a staff
    edit, and a new note; plus a published 1.0 so cutting is allowed."""
    with server.db() as conn:
        conn.execute("INSERT OR REPLACE INTO releases (version, status, note_count, created_at, published_at) "
                     "VALUES ('1.0','published',3,?,?)", (server.now(), server.now()))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_release','1.0')")
    register_note(base, A, fields={"Text": "a", "Extra": ""})
    register_note(base, B, fields={"Text": "b", "Extra": ""})
    body = {"doctrine_id": A, "guid": A, "text": "typo here", "suggestion_type": "typo",
            "snapshot": {"fields": {"Text": "a"}}, "proposed_fields": {"Text": "a fixed"}}
    req = urllib.request.Request(base + "/api/suggestions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()
    with server.db() as conn:
        sid = conn.execute("SELECT id FROM suggestions ORDER BY id DESC LIMIT 1").fetchone()["id"]
        server.accept_proposal(conn, sid, 1)
        revisions.apply_edit(conn, B, {"Text": "b staff"}, 1, "staff")
        revisions.add_note(conn, "AnKingOverhaul++++++", {"Text": "new {{c1::x}}", "Extra": ""}, [], 1)


class KindTest(unittest.TestCase):
    def test_kinds_are_derived_from_source(self):
        with running_server() as base:
            seed(base)
            with server.db() as conn:
                groups = revisions.pending_by_kind(conn)
        kinds = {g["kind"]: [n["guid"] for n in g["notes"]] for g in groups}
        self.assertEqual([A], kinds["typo"])
        self.assertEqual([B], kinds["staff"])
        self.assertEqual(1, len(kinds["added"]))


class PageTest(unittest.TestCase):
    def test_renders_groups_with_checkboxes_and_counts(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, _, html = request(base, "GET", "/releases/next", cookie=cookie)
        self.assertEqual(200, status)
        for label in ("Typo / spelling", "Direct edits", "New notes", "Review selection"):
            self.assertIn(label, html)
        self.assertEqual(3, html.count('name="rev:'))
        self.assertIn("<b>3</b> cards", html)

    def test_old_pending_url_redirects(self):
        with running_server() as base:
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, headers, _ = request(base, "GET", "/releases/pending", cookie=cookie)
        self.assertEqual(303, status); self.assertIn("/releases/next", headers.get("Location", ""))


class CutTest(unittest.TestCase):
    def test_confirm_shows_only_the_selection(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, _, html = request(base, "POST", "/releases/confirm",
                                      {"csrf": csrf_for(cookie), f"rev:{A}": "1", f"rev:{B}": "1"}, cookie=cookie)
        self.assertEqual(200, status)
        self.assertIn(A.replace("%", "%"), html.replace("&amp;", "&"))
        self.assertIn("2 notes", html)
        self.assertNotIn("New notes", html)

    def test_cut_takes_only_selected_and_leaves_the_rest_pending(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, headers, _ = request(base, "POST", "/releases/cut",
                                         {"csrf": csrf_for(cookie), "version": "1.1", f"rev:{A}": "1"}, cookie=cookie)
            with server.db() as conn:
                stamped = {r["guid"] for r in conn.execute("SELECT guid FROM revisions WHERE release='1.1'")}
                still = {g["guid"] for g in revisions.pending(conn)}
                rel = dict(conn.execute("SELECT * FROM releases WHERE version='1.1'").fetchone())
        self.assertEqual(303, status)
        self.assertEqual({A}, stamped)
        self.assertEqual(2, len(still))
        self.assertEqual("draft", rel["status"])
        self.assertIn("typo", rel["changelog"].lower())

    def test_cut_with_no_selection_is_refused(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, _, html = request(base, "POST", "/releases/cut",
                                      {"csrf": csrf_for(cookie), "version": "1.1"}, cookie=cookie)
            with server.db() as conn:
                n = conn.execute("SELECT COUNT(*) FROM releases WHERE version='1.1'").fetchone()[0]
        self.assertEqual(0, n)
        self.assertIn("Nothing selected", html)


class DiscardTest(unittest.TestCase):
    def test_discarding_a_draft_returns_its_changes_to_next_release(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            request(base, "POST", "/releases/cut", {"csrf": csrf_for(cookie), "version": "1.1", f"rev:{A}": "1", f"rev:{B}": "1"}, cookie=cookie)
            status, _, _ = request(base, "POST", "/releases/1.1/discard", {"csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                gone = conn.execute("SELECT 1 FROM releases WHERE version='1.1'").fetchone()
                pending = {g["guid"] for g in revisions.pending(conn)}
        self.assertEqual(303, status)
        self.assertIsNone(gone)
        self.assertEqual(3, len(pending))


class BuildConfirmTest(unittest.TestCase):
    def test_build_requires_typing_the_version(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            request(base, "POST", "/releases/cut", {"csrf": csrf_for(cookie), "version": "1.1", f"rev:{A}": "1"}, cookie=cookie)
            from tests.test_release_routes import approve
            approve(base, "1.1")
            status, _, html = request(base, "GET", "/releases/1.1/confirm", cookie=cookie)
            self.assertEqual(200, status); self.assertIn("type the version", html.lower())
            status, _, _ = request(base, "POST", "/releases/1.1/build", {"csrf": csrf_for(cookie), "confirm": "nope"}, cookie=cookie)
            with server.db() as conn:
                st = conn.execute("SELECT status FROM releases WHERE version='1.1'").fetchone()["status"]
        self.assertEqual("draft", st, "a wrong confirmation must not start a build")
