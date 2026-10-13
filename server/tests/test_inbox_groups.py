"""Inbox groups open suggestions per card; fixing a card thanks every reporter."""

import json
import unittest
import urllib.request

import server
from tests.helpers import register_note, running_server

GUID = "G]%rwtghL]"


def suggest(base, text, email=None):
    body = {"doctrine_id": GUID, "guid": GUID, "text": text, "email": email,
            "snapshot": {"fields": {"Text": "HPV11 {{c1::low}}", "Extra": "e"}}}
    req = urllib.request.Request(base + "/api/suggestions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def statuses():
    with server.db() as conn:
        return [(r["id"], r["status"]) for r in conn.execute("SELECT id,status FROM suggestions ORDER BY id")]


def thanks():
    with server.db() as conn:
        return conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]


def queue():
    return server.render_reviewer({"id": 1, "username": "r", "role": "reviewer"}, "s", "t")


class GroupTest(unittest.TestCase):
    def test_two_reports_one_card_share_a_group(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "HPV11 {{c1::low}}", "Extra": "e"})
            suggest(base, "typo one"); suggest(base, "typo two")
            html = queue()
        self.assertIn("2 reports on this card", html)
        self.assertIn("Card fixed", html)

    def test_resolve_card_thanks_everyone(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "HPV11 {{c1::low}}", "Extra": "e"})
            suggest(base, "a", "a@x.org"); suggest(base, "b", "b@x.org"); suggest(base, "c")
            server.reviewer_action({"id": ["1"], "do": ["resolve_card"]}, 1)
            self.assertEqual({"resolved"}, {s for _, s in statuses()})
            self.assertEqual(2, thanks())

    def test_duplicate_resolves_one_and_thanks_it(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "HPV11 {{c1::low}}", "Extra": "e"})
            suggest(base, "a", "a@x.org"); suggest(base, "b", "b@x.org")
            server.reviewer_action({"id": ["2"], "do": ["duplicate"]}, 1)
            self.assertEqual([(1, "open"), (2, "resolved")], statuses())
            self.assertEqual(1, thanks())

    def test_edit_resolves_ticked_siblings_only(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "HPV11 {{c1::low}}", "Extra": "e"})
            for t in "abc":
                suggest(base, t, f"{t}@x.org")
            with server.db() as conn:
                server.apply_edit_and_resolve(conn, "1", {"Text": "HPV11 {{c1::lower}}"},
                                              {"also": ["2"]}, 1)
            self.assertEqual([(1, "resolved"), (2, "resolved"), (3, "open")], statuses())
            self.assertEqual(2, thanks())

    def test_edit_page_lists_siblings_ticked(self):
        with running_server() as base:
            register_note(base, GUID, fields={"Text": "HPV11 {{c1::low}}", "Extra": "e"})
            suggest(base, "a"); suggest(base, "b")
            html = server.render_edit({"id": 1, "username": "r", "role": "reviewer"}, "t", "1")
        self.assertIn('name="also" value="2" checked', html)


if __name__ == "__main__":
    unittest.main()
