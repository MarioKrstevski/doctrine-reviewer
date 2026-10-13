"""A release has a detail page: what changed, who did it, where it came from, was the student thanked."""

import json
import unittest
import urllib.request

import revisions
import server
from tests.helpers import register_note, running_server
from tests.test_release_routes import csrf_for, seed_baseline, staff
from tests.test_auth_routes import request

A, B = "guidAAAAAA", "guidBBBBBB"


def suggest(base, guid, text, email):
    body = {"doctrine_id": guid, "guid": guid, "text": text, "email": email, "suggestion_type": "typo",
            "snapshot": {"fields": {"Text": "p53 old text", "Extra": "e"}},
            "proposed_fields": {"Text": "p53 new text"}}
    req = urllib.request.Request(base + "/api/suggestions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()


class ReleaseDetailTest(unittest.TestCase):
    def _release(self, base):
        seed_baseline()
        register_note(base, A, fields={"Text": "p53 old text", "Extra": "e"})
        register_note(base, B, fields={"Text": "second card", "Extra": "e"})
        suggest(base, A, "p53 is spelled wrong", "student@example.org")
        with server.db() as conn:
            server.accept_proposal(conn, "1", 1)
            revisions.apply_edit(conn, B, {"Text": "second card fixed"}, 1, "staff")
        cookie = staff(base, "admin")
        request(base, "POST", "/releases/cut", {"version": "1.1", "csrf": csrf_for(cookie), f"rev:{A}": "1", f"rev:{B}": "1",
                                                "notes": "Fixed two cards.", "visibility": "public"}, cookie=cookie)
        return cookie

    def test_detail_table_shows_cards_who_source_student_words_and_thanks(self):
        with running_server() as base:
            cookie = self._release(base)
            status, _, html = request(base, "GET", "/releases/1.1", cookie=cookie)
        self.assertEqual(200, status)
        for expected in ("Release 1.1", "p53 new text", "second card fixed", "Student report #1", "with an exact proposal",
                         "p53 is spelled wrong", "Thank-you waiting in Outbox", "Direct edit", "Open card", "History",
                         "<b>2</b> cards changed", "<b>1</b> from student reports", "Fixed two cards."):
            self.assertIn(expected, html)
        self.assertIn("<ins>", html)

    def test_requires_login_and_unknown_release_is_404(self):
        with running_server() as base:
            cookie = self._release(base)
            self.assertEqual(303, request(base, "GET", "/releases/1.1")[0])
            self.assertEqual(404, request(base, "GET", "/releases/9.9", cookie=cookie)[0])
            _, _, listing = request(base, "GET", "/releases", cookie=cookie)
        self.assertIn('href="/releases/1.1"', listing)
        self.assertIn("see details", listing)


if __name__ == "__main__":
    unittest.main()
