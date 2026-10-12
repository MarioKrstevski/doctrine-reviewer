"""Editor: accept a proposal in one click, edit before accepting, add a note."""

import json
import unittest
import urllib.request

import auth
import revisions
import server
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request

GUID = "P]%rwtghL]"
CURRENT = {"Text": "HPV11 has {{c1::low}} cancer risk", "Extra": "e"}


def suggest(base, proposed=None, email=None):
    body = {"doctrine_id": GUID, "guid": GUID, "text": "wording", "email": email,
            "snapshot": {"fields": CURRENT}}
    if proposed:
        body["proposed_fields"] = proposed
    req = urllib.request.Request(base + "/api/suggestions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()
    with server.db() as conn:
        return conn.execute("SELECT id FROM suggestions ORDER BY id DESC LIMIT 1").fetchone()["id"]


def csrf_for(cookie):
    return auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())


def note_fields():
    with server.db() as conn:
        return json.loads(conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (GUID,)).fetchone()["fields_json"])


def suggestion(sid):
    with server.db() as conn:
        return dict(conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone())


class AcceptProposalTest(unittest.TestCase):
    def test_one_click_applies_the_proposal_and_resolves(self):
        with running_server() as base:
            register_note(base, GUID, fields=CURRENT)
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = suggest(base, {"Text": "HPV11 has {{c1::low}} malignancy risk"}, email="s@x.test")
            status, _, _ = request(base, "POST", "/reviewer/action",
                                   {"id": sid, "do": "accept_proposal", "csrf": csrf_for(cookie)}, cookie=cookie)
            self.assertEqual(303, status)
            fields = note_fields(); sug = suggestion(sid)
            with server.db() as conn:
                rev = dict(conn.execute("SELECT * FROM revisions").fetchone())
                thanks = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        self.assertEqual("HPV11 has {{c1::low}} malignancy risk", fields["Text"])
        self.assertEqual("resolved", sug["status"])
        self.assertEqual(f"suggestion:{sid}", rev["source"])
        self.assertEqual(1, thanks)

    def test_without_a_proposal_it_is_refused(self):
        with running_server() as base:
            register_note(base, GUID, fields=CURRENT)
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = suggest(base)
            status, _, _ = request(base, "POST", "/reviewer/action",
                                   {"id": sid, "do": "accept_proposal", "csrf": csrf_for(cookie)}, cookie=cookie)
            st = suggestion(sid)["status"]
        self.assertEqual(303, status)
        self.assertEqual("open", st)


class EditPageTest(unittest.TestCase):
    def test_requires_login(self):
        with running_server() as base:
            register_note(base, GUID, fields=CURRENT)
            sid = suggest(base)
            status, headers, _ = request(base, "GET", f"/edit/{sid}")
        self.assertEqual(303, status)
        self.assertIn("/login", headers.get("Location", ""))

    def test_prefills_current_fields_or_the_proposal(self):
        with running_server() as base:
            register_note(base, GUID, fields=CURRENT)
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = suggest(base, {"Text": "PROPOSED"})
            _, _, html = request(base, "GET", f"/edit/{sid}", cookie=cookie)
            _, _, html2 = request(base, "GET", f"/edit/{sid}?from=proposal", cookie=cookie)
        self.assertIn("cancer risk", html)
        self.assertNotIn(">PROPOSED<", html)
        self.assertIn("PROPOSED", html2)

    def test_post_applies_the_edit_and_resolves(self):
        with running_server() as base:
            register_note(base, GUID, fields=CURRENT)
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = suggest(base)
            status, _, _ = request(base, "POST", f"/edit/{sid}",
                                   {"Text": "EDITED", "Extra": "e", "csrf": csrf_for(cookie),
                                    "publish": "1", "publish_summary": "Fixed wording", "credit_name": "M."},
                                   cookie=cookie)
            self.assertEqual(303, status)
            fields = note_fields(); sug = suggestion(sid)
        self.assertEqual("EDITED", fields["Text"])
        self.assertEqual("resolved", sug["status"])
        self.assertEqual(1, sug["published"])
        self.assertEqual("Fixed wording", sug["publish_summary"])

    def test_post_needs_csrf(self):
        with running_server() as base:
            register_note(base, GUID, fields=CURRENT)
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = suggest(base)
            status, _, _ = request(base, "POST", f"/edit/{sid}", {"Text": "X"}, cookie=cookie)
            text = note_fields()["Text"]
        self.assertEqual(403, status)
        self.assertEqual(CURRENT["Text"], text)

    def test_queue_marks_edited_items(self):
        with running_server() as base:
            register_note(base, GUID, fields=CURRENT)
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = suggest(base)
            request(base, "POST", f"/edit/{sid}", {"Text": "EDITED", "Extra": "e", "csrf": csrf_for(cookie)}, cookie=cookie)
            html = server.render_reviewer({"id": 1, "username": "r", "role": "reviewer"}, "s", "t")
        self.assertIn("Edited", html)


class AddNoteTest(unittest.TestCase):
    def test_form_and_create(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            status, _, html = request(base, "GET", "/notes/new", cookie=cookie)
            self.assertEqual(200, status)
            self.assertIn("Text", html)
            status, headers, _ = request(base, "POST", "/notes/new",
                                         {"note_type": "AnKingOverhaul++++++", "Text": "{{c1::new}}",
                                          "Extra": "x", "tags": "Doctorine::Yield::Step1::Gold  Doctorine::Shelf::IM",
                                          "csrf": csrf_for(cookie)}, cookie=cookie)
            self.assertEqual(303, status)
            with server.db() as conn:
                row = dict(conn.execute("SELECT * FROM notes ORDER BY updated_at DESC LIMIT 1").fetchone())
        self.assertEqual("{{c1::new}}", json.loads(row["fields_json"])["Text"])
        self.assertEqual(["Doctorine::Yield::Step1::Gold", "Doctorine::Shelf::IM"], json.loads(row["tags_json"]))
        self.assertIn("/releases/pending", headers.get("Location", ""))

    def test_requires_login(self):
        with running_server() as base:
            self.assertEqual(303, request(base, "GET", "/notes/new")[0])
            self.assertEqual(303, request(base, "POST", "/notes/new", {"Text": "x"})[0])


class PendingPageTest(unittest.TestCase):
    def test_lists_pending_revisions(self):
        with running_server() as base:
            register_note(base, GUID, fields=CURRENT)
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            with server.db() as conn:
                revisions.apply_edit(conn, GUID, {"Text": "changed"}, 1, "staff")
            status, _, html = request(base, "GET", "/releases/pending", cookie=cookie)
        self.assertEqual(200, status)
        self.assertIn(GUID.replace("%", "%"), html.replace("&amp;", "&"))
        self.assertIn("<del>", html)
        self.assertIn("1 note", html)

    def test_requires_login(self):
        with running_server() as base:
            self.assertEqual(303, request(base, "GET", "/releases/pending")[0])
