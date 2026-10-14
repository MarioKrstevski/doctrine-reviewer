"""Administrators choose which decks a reviewer works on, and can change roles."""

import unittest
import urllib.parse

import server
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request
from tests.test_cards import csrf_for

A, B = "guidAAAAAA", "guidBBBBBB"


def setup(base):
    register_note(base, A, fields={"Text": "deck one card", "Extra": ""})
    register_note(base, B, fields={"Text": "deck two card", "Extra": ""})
    second = server.add_deck("Pathoma Companion")
    with server.db() as conn:
        conn.execute("UPDATE notes SET platform_deck=? WHERE doctrine_id=?", (second, B))
    make_user("boss", "bosspassword1", "admin")
    _, admin = login(base, "boss", "bosspassword1")
    make_user("rev", "revpassword1", "reviewer")
    with server.db() as conn:
        rev_id = conn.execute("SELECT id FROM users WHERE username='rev'").fetchone()[0]
    return second, admin, rev_id


class DeckAccessTest(unittest.TestCase):
    def test_limited_reviewer_sees_only_their_deck_and_is_refused_elsewhere(self):
        with running_server() as base:
            second, admin, rev_id = setup(base)
            request(base, "POST", "/admin/users", {"csrf": csrf_for(admin), "do": "decks", "user_id": str(rev_id),
                                                    "scope": "some", "deck": str(second)}, cookie=admin)
            _, rev = login(base, "rev", "revpassword1")
            _, _, cards = request(base, "GET", "/cards?q=card", cookie=rev)
            self.assertIn("deck two card", cards, "falls back to the one deck they may see")
            self.assertNotIn("deck one card", cards)
            self.assertNotIn('name="deck" value="doctorine"', cards, "the switcher hides decks they may not see")
            self.assertIn('name="deck" value="pathoma-companion"', cards)
            q = urllib.parse.quote(A, safe="")
            status, _, html = request(base, "GET", f"/cards/edit?guid={q}", cookie=rev)
            self.assertEqual(403, status)
            self.assertIn("deck you do not have access to", html)
            status, _, _ = request(base, "POST", f"/cards/edit?guid={q}",
                                   {"csrf": csrf_for(rev), "Text": "hacked", "Extra": ""}, cookie=rev)
            self.assertEqual(403, status)
            with server.db() as conn:
                self.assertIn("deck one card", conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (A,)).fetchone()[0])
            # switching to a deck they may not see does nothing
            request(base, "POST", "/deck/select", {"csrf": csrf_for(rev), "deck": "doctorine"}, cookie=rev)
            _, _, cards = request(base, "GET", "/cards?q=card", cookie=rev)
            self.assertNotIn("deck one card", cards)

    def test_all_decks_again_and_admins_are_never_limited(self):
        with running_server() as base:
            second, admin, rev_id = setup(base)
            server.set_user_decks(rev_id, [second])
            request(base, "POST", "/admin/users", {"csrf": csrf_for(admin), "do": "decks", "user_id": str(rev_id),
                                                    "scope": "all"}, cookie=admin)
            with server.db() as conn:
                self.assertIsNone(server.user_deck_ids(conn, rev_id))
            _, _, html = request(base, "POST", "/admin/users", {"csrf": csrf_for(admin), "do": "decks", "user_id": str(rev_id),
                                                                 "scope": "some"}, cookie=admin)
            self.assertIn("Pick at least one deck", html)
            q = urllib.parse.quote(B, safe="")
            self.assertEqual(200, request(base, "GET", f"/cards/edit?guid={q}", cookie=admin)[0])

    def test_create_with_decks_and_change_role(self):
        with running_server() as base:
            second, admin, _ = setup(base)
            request(base, "POST", "/admin/users", {"csrf": csrf_for(admin), "do": "create", "username": "solo",
                                                    "scope": "some", "deck": str(second)}, cookie=admin)
            with server.db() as conn:
                solo = conn.execute("SELECT id FROM users WHERE username='solo'").fetchone()[0]
                self.assertEqual({second}, server.user_deck_ids(conn, solo))
            _, _, page = request(base, "GET", "/admin/users", cookie=admin)
            self.assertIn("Pathoma Companion", page)
            request(base, "POST", "/admin/users", {"csrf": csrf_for(admin), "do": "make_admin", "user_id": str(solo)}, cookie=admin)
            with server.db() as conn:
                self.assertEqual("admin", conn.execute("SELECT role FROM users WHERE id=?", (solo,)).fetchone()[0])
            boss = None
            with server.db() as conn:
                boss = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            request(base, "POST", "/admin/users", {"csrf": csrf_for(admin), "do": "make_reviewer", "user_id": str(solo)}, cookie=admin)
            _, _, html = request(base, "POST", "/admin/users", {"csrf": csrf_for(admin), "do": "make_reviewer",
                                                                 "user_id": str(boss)}, cookie=admin)
            self.assertIn("only active administrator", html)


if __name__ == "__main__":
    unittest.main()
