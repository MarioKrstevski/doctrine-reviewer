"""Cards: find any official card and edit it directly, without a suggestion."""

import json
import unittest

import auth
import server
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request

A, B = "P]%rwtghL]", "s.MBeTB!D."


def csrf_for(cookie):
    return auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())


def seed(base):
    register_note(base, A, fields={"Text": "<b>Vulvar carcinoma</b> is caused by {{c1::HPV}}", "Extra": "HPV E6 and E7"})
    register_note(base, B, fields={"Text": "p53 blocks {{c1::G1 to S}}", "Extra": ""})
    with server.db() as conn:
        conn.execute("UPDATE notes SET anki_note_id=1368292407340 WHERE doctrine_id=?", (A,))


class SearchTest(unittest.TestCase):
    def test_requires_login(self):
        with running_server() as base:
            self.assertEqual(303, request(base, "GET", "/cards?q=p53")[0])

    def test_finds_by_text(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            _, _, html = request(base, "GET", "/cards?q=vulvar", cookie=cookie)
        self.assertIn("Vulvar carcinoma", html)
        self.assertNotIn("p53 blocks", html)
        self.assertIn(f"/cards/edit?guid=", html)

    def test_finds_by_guid_and_note_id(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            import urllib.parse
            _, _, by_guid = request(base, "GET", "/cards?q=" + urllib.parse.quote(B, safe=""), cookie=cookie)
            _, _, by_nid = request(base, "GET", "/cards?q=1368292407340", cookie=cookie)
        self.assertIn("p53 blocks", by_guid); self.assertNotIn("Vulvar", by_guid)
        self.assertIn("Vulvar carcinoma", by_nid)

    def test_empty_query_shows_the_form_only(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            status, _, html = request(base, "GET", "/cards", cookie=cookie)
        self.assertEqual(200, status); self.assertIn('name="q"', html); self.assertNotIn("Vulvar", html)

    def test_results_are_capped(self):
        with running_server() as base:
            for i in range(60):
                register_note(base, f"guid{i:06d}", fields={"Text": f"common word {i}", "Extra": ""})
            user, pw = make_user(); _, cookie = login(base, user, pw)
            _, _, html = request(base, "GET", "/cards?q=common+word", cookie=cookie)
        self.assertIn("first 50", html)


class DirectEditTest(unittest.TestCase):
    def test_edit_page_prefills_and_save_records_a_staff_revision(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            import urllib.parse
            q = urllib.parse.quote(A, safe="")
            status, _, html = request(base, "GET", f"/cards/edit?guid={q}", cookie=cookie)
            self.assertEqual(200, status); self.assertIn("Vulvar carcinoma", html)
            status, headers, _ = request(base, "POST", f"/cards/edit?guid={q}",
                                         {"Text": "<b>Vulvar carcinoma</b> is caused by {{c1::high-risk HPV}}",
                                          "Extra": "HPV E6 and E7", "csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                text = json.loads(conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (A,)).fetchone()["fields_json"])["Text"]
                rev = dict(conn.execute("SELECT * FROM revisions ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(303, status); self.assertIn("/releases/next", headers.get("Location", ""))
        self.assertIn("high-risk HPV", text)
        self.assertEqual("staff", rev["source"])

    def test_unknown_guid_is_404(self):
        with running_server() as base:
            user, pw = make_user(); _, cookie = login(base, user, pw)
            self.assertEqual(404, request(base, "GET", "/cards/edit?guid=nope", cookie=cookie)[0])

    def test_save_needs_csrf(self):
        with running_server() as base:
            seed(base)
            user, pw = make_user(); _, cookie = login(base, user, pw)
            import urllib.parse
            status, _, _ = request(base, "POST", "/cards/edit?guid=" + urllib.parse.quote(A, safe=""), {"Text": "x"}, cookie=cookie)
        self.assertEqual(403, status)


class HistoryAndTagsTest(unittest.TestCase):
    def test_tag_search(self):
        with running_server() as base:
            seed(base)
            with server.db() as conn:
                conn.execute("UPDATE notes SET tags_json=? WHERE doctrine_id=?",
                             (json.dumps(["Doctorine::Yield::Step1::Gold"]), A))
            user, pw = make_user(); _, cookie = login(base, user, pw)
            _, _, html = request(base, "GET", "/cards?q=tag:Yield::Step1", cookie=cookie)
            _, _, html2 = request(base, "GET", "/cards?q=Step1::Gold", cookie=cookie)
        for h in (html, html2):
            self.assertIn("Vulvar carcinoma", h)
            self.assertNotIn("p53 blocks", h)

    def test_history_and_pending_notice(self):
        import revisions
        with running_server() as base:
            seed(base)
            with server.db() as conn:
                revisions.apply_edit(conn, A, {"Extra": "HPV E6/E7 degrade p53"}, 1, "staff")
            user, pw = make_user(); _, cookie = login(base, user, pw)
            from urllib.parse import quote
            _, _, html = request(base, "GET", f"/cards/edit?guid={quote(A, safe='')}", cookie=cookie)
        self.assertIn("History", html)
        self.assertIn("direct edit", html)
        self.assertIn("pending, next release", html)
        self.assertIn("already has 1 pending edit", html)
