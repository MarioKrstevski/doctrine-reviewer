"""Multi-deck foundation: decks table, per-deck official list and sync, pinning via /where."""

import json
import unittest
import urllib.error
import urllib.request

import server
from tests.helpers import register_note, running_server
from tests.test_cards import csrf_for
from tests.test_release_routes import staff
from tests.test_auth_routes import request


def get(base, path):
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class DeckApiTest(unittest.TestCase):
    def test_where_names_the_default_deck_and_unknown_decks_404(self):
        with running_server() as base:
            self.assertEqual("doctorine", get(base, "/where")[1]["deck"])
            self.assertEqual(404, get(base, "/api/official?deck=nope")[0])
            self.assertEqual(404, get(base, "/api/sync?since=1.0&deck=nope")[0])
            self.assertEqual(404, get(base, "/api/sync/version?deck=nope")[0])

    def test_official_list_is_per_deck(self):
        with running_server() as base:
            register_note(base, "guidAAAAAA")
            register_note(base, "guidBBBBBB")
            second = server.add_deck("Pathoma Companion")
            self.assertIsNotNone(second)
            with server.db() as conn:
                conn.execute("UPDATE notes SET platform_deck=? WHERE doctrine_id='guidBBBBBB'", (second,))
            _, a = get(base, "/api/official")
            _, b = get(base, "/api/official?deck=pathoma-companion")
        self.assertEqual(["guidAAAAAA"], a["guids"])
        self.assertEqual(["guidBBBBBB"], b["guids"])

    def test_admin_adds_a_deck_and_duplicates_are_ignored(self):
        with running_server() as base:
            cookie = staff(base, "admin")
            request(base, "POST", "/deck/new", {"name": "Pathoma Companion", "csrf": csrf_for(cookie)}, cookie=cookie)
            request(base, "POST", "/deck/new", {"name": "Pathoma Companion", "csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                slugs = [r[0] for r in conn.execute("SELECT slug FROM decks ORDER BY id")]
            _, _, html = request(base, "GET", "/deck", cookie=cookie)
        self.assertEqual(["doctorine", "pathoma-companion"], slugs)
        self.assertIn("pathoma-companion", html)

    def test_a_suggestion_takes_its_notes_deck(self):
        with running_server() as base:
            register_note(base, "guidAAAAAA")
            second = server.add_deck("Other")
            with server.db() as conn:
                conn.execute("UPDATE notes SET platform_deck=? WHERE doctrine_id='guidAAAAAA'", (second,))
            req = urllib.request.Request(base + "/api/suggestions", data=json.dumps(
                {"doctrine_id": "guidAAAAAA", "guid": "guidAAAAAA", "text": "x",
                 "snapshot": {"fields": {"Front": "Q"}}}).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            with server.db() as conn:
                pd = conn.execute("SELECT platform_deck FROM suggestions").fetchone()[0]
        self.assertEqual(second, pd)


class DeckWorkspaceTest(unittest.TestCase):
    def _login(self, base, role="admin"):
        return staff(base, role)

    def test_selecting_a_deck_scopes_cards_and_the_inbox(self):
        with running_server() as base:
            register_note(base, "guidAAAAAA", fields={"Text": "first deck card", "Extra": ""})
            register_note(base, "guidBBBBBB", fields={"Text": "second deck card", "Extra": ""})
            second = server.add_deck("Pathoma Companion")
            with server.db() as conn:
                conn.execute("UPDATE notes SET platform_deck=? WHERE doctrine_id='guidBBBBBB'", (second,))
            cookie = self._login(base)
            _, _, html = request(base, "GET", "/cards?q=deck+card", cookie=cookie)
            self.assertIn("first deck card", html)
            self.assertNotIn("second deck card", html)
            request(base, "POST", "/deck/select", {"deck": "pathoma-companion", "csrf": csrf_for(cookie)}, cookie=cookie)
            _, _, html = request(base, "GET", "/cards?q=deck+card", cookie=cookie)
            self.assertIn("second deck card", html)
            self.assertNotIn("first deck card", html)
            self.assertIn("Pathoma Companion", html)

    def test_public_pages_stay_public_when_signed_in(self):
        with running_server() as base:
            cookie = self._login(base)
            for path in ("/updates", "/install"):
                _, _, html = request(base, "GET", path, cookie=cookie)
                self.assertNotIn('class="sidebar"', html, path)
                self.assertIn("Back to workspace", html, path)
            _, _, html = request(base, "GET", "/reviewer", cookie=cookie)
            self.assertIn('class="sidebar"', html)

    def test_triple_confirm_delete_needs_the_typed_name_and_protects_the_first_deck(self):
        with running_server() as base:
            register_note(base, "guidBBBBBB")
            second = server.add_deck("Scratch Deck")
            with server.db() as conn:
                conn.execute("UPDATE notes SET platform_deck=? WHERE doctrine_id='guidBBBBBB'", (second,))
            cookie = self._login(base)
            _, _, html = request(base, "GET", "/deck", cookie=cookie)
            self.assertIn("Are you really sure?", html)
            self.assertIn("Type the deck name to confirm", html)
            self.assertEqual(1, html.count('data-dialog="del1-'))   # no delete button on the first deck
            request(base, "POST", "/deck/delete", {"deck": "scratch-deck", "confirm_name": "wrong", "csrf": csrf_for(cookie)}, cookie=cookie)
            request(base, "POST", "/deck/delete", {"deck": "doctorine", "confirm_name": "Doctorine", "csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0])
            request(base, "POST", "/deck/delete", {"deck": "scratch-deck", "confirm_name": "Scratch Deck", "csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                self.assertEqual(["doctorine"], [r[0] for r in conn.execute("SELECT slug FROM decks")])
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM notes WHERE doctrine_id='guidBBBBBB'").fetchone()[0])

    def test_reviewers_cannot_delete(self):
        with running_server() as base:
            server.add_deck("Scratch Deck")
            cookie = self._login(base, "reviewer")
            status, _, _ = request(base, "POST", "/deck/delete", {"deck": "scratch-deck", "confirm_name": "Scratch Deck", "csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                n = conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0]
        self.assertEqual((403, 2), (status, n))


class DeckMediaTest(unittest.TestCase):
    def test_media_belongs_to_a_deck_and_the_deck_page_counts_only_its_own(self):
        import releases
        with running_server() as base:
            second = server.add_deck("Scratch Deck")
            with server.db() as conn:
                releases.store_media(conn, "a.png", b"AAAA")
                releases.store_media(conn, "b.png", b"BBBB")
                releases.store_media(conn, "c.png", b"CCCC", second)
                self.assertEqual(["a.png", "b.png"], [m[0] for m in server.media_for_package(conn, 1)])
                self.assertEqual(["c.png"], [m[0] for m in server.media_for_package(conn, second)])
            admin = staff(base, "admin")
            _, _, first = request(base, "GET", "/deck", cookie=admin)
            request(base, "POST", "/deck/select", {"deck": "scratch-deck", "csrf": csrf_for(admin)}, cookie=admin)
            _, _, other = request(base, "GET", "/deck", cookie=admin)
        self.assertIn("<b>2</b> media files", first)
        self.assertIn("<b>1</b> media files", other)
        self.assertNotIn("Free space on the server", other)

    def test_sidebar_says_users_not_settings(self):
        with running_server() as base:
            admin = staff(base, "admin")
            _, _, html = request(base, "GET", "/reviewer", cookie=admin)
            _, _, users = request(base, "GET", "/admin/users", cookie=admin)
        self.assertIn("<span>Users</span>", html)
        self.assertNotIn("<span>Settings</span>", html)
        self.assertIn("<h1>Users</h1>", users)


if __name__ == "__main__":
    unittest.main()
