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


if __name__ == "__main__":
    unittest.main()
