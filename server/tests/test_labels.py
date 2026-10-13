"""Labels belong to cards. They are created on the fly, shared by every report on the card,
and removed when no card uses them."""

import unittest

import labels
import server
from tests.helpers import register_note, running_server


class LabelStorageTest(unittest.TestCase):
    def test_normalize_and_parse(self):
        self.assertEqual("ask anna", labels.normalize("  Ask   ANNA "))
        self.assertEqual(40, len(labels.normalize("x" * 99)))
        self.assertEqual(["ask anna", "needs review"], labels.parse_new("Ask Anna, needs review,, ASK anna"))
        self.assertEqual("", labels.normalize("   "))

    def test_set_replace_list_find_and_cleanup(self):
        with running_server() as base:
            register_note(base, "guidAAAAAA"); register_note(base, "guidBBBBBB")
            with server.db() as conn:
                labels.set_card_labels(conn, "guidAAAAAA", ["Ask Anna", "wording"], 1, "t")
                labels.set_card_labels(conn, "guidBBBBBB", ["wording"], 1, "t")
                self.assertEqual({"guidAAAAAA": ["ask anna", "wording"], "guidBBBBBB": ["wording"]},
                                 labels.labels_for(conn, ["guidAAAAAA", "guidBBBBBB"]))
                self.assertEqual([{"name": "wording", "cards": 2}, {"name": "ask anna", "cards": 1}],
                                 labels.all_labels(conn, 1))
                self.assertEqual({"guidAAAAAA", "guidBBBBBB"}, labels.guids_with(conn, ["wording"], 1))
                self.assertEqual({"guidAAAAAA"}, labels.guids_with(conn, ["ASK anna"], 1))
                # replacing drops the old labels, and an unused label disappears
                labels.set_card_labels(conn, "guidAAAAAA", ["wording"], 1, "t")
                self.assertEqual(["wording"], [x["name"] for x in labels.all_labels(conn, 1)])
                labels.set_card_labels(conn, "guidAAAAAA", [], 1, "t")
                labels.set_card_labels(conn, "guidBBBBBB", [], 1, "t")
                self.assertEqual([], labels.all_labels(conn, 1))

    def test_labels_are_per_deck(self):
        with running_server() as base:
            register_note(base, "guidAAAAAA")
            second = server.add_deck("Other")
            with server.db() as conn:
                labels.set_card_labels(conn, "guidAAAAAA", ["x"], 1, "t", deck_id=1)
                self.assertEqual([], labels.all_labels(conn, second))
                self.assertEqual(set(), labels.guids_with(conn, ["x"], second))


class LabelRoutesTest(unittest.TestCase):
    def _suggest(self, base, guid, text):
        import json, urllib.request
        body = {"doctrine_id": guid, "guid": guid, "text": text,
                "snapshot": {"fields": {"Text": "alpha text", "Extra": "e"}}}
        req = urllib.request.Request(base + "/api/suggestions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()

    def test_labeling_a_card_moves_its_open_reports_from_new_to_triaged(self):
        from tests.test_cards import csrf_for
        from tests.test_release_routes import staff
        from tests.test_auth_routes import request
        with running_server() as base:
            register_note(base, "guidAAAAAA", fields={"Text": "alpha text", "Extra": "e"})
            register_note(base, "guidBBBBBB", fields={"Text": "alpha text", "Extra": "e"})
            self._suggest(base, "guidAAAAAA", "report on A"); self._suggest(base, "guidBBBBBB", "report on B")
            cookie = staff(base, "reviewer")
            request(base, "POST", "/labels/set", {"guid": "guidAAAAAA", "new": "Ask Anna, wording", "csrf": csrf_for(cookie),
                                                  "next": "/reviewer"}, cookie=cookie)
            _, _, html = request(base, "GET", "/reviewer", cookie=cookie)
            new_pane = html.split('id="new-pane"')[1].split('id="triaged-pane"')[0]
            triaged = html.split('id="triaged-pane"')[1].split('id="closed-pane"')[0]
            self.assertIn("report on B", new_pane); self.assertNotIn("report on A", new_pane)
            self.assertIn("report on A", triaged)
            self.assertRegex(html, r'Triaged <span class="count">1</span>')
            self.assertIn("ask anna", triaged)
            # filtering by a label that the card does not carry hides it
            _, _, filtered = request(base, "GET", "/reviewer?label=nothing", cookie=cookie)
            self.assertNotIn("report on A", filtered.split('id="triaged-pane"')[1].split('id="closed-pane"')[0])
            # it is still open: it stays out of Closed
            closed = html.split('id="closed-pane"')[1]
            self.assertNotIn("report on A", closed)

    def test_cards_search_by_label_and_next_release_filter(self):
        import revisions
        from tests.test_cards import csrf_for
        from tests.test_release_routes import staff
        from tests.test_auth_routes import request
        with running_server() as base:
            register_note(base, "guidAAAAAA", fields={"Text": "first card text", "Extra": ""})
            register_note(base, "guidBBBBBB", fields={"Text": "second card text", "Extra": ""})
            cookie = staff(base, "reviewer")
            request(base, "POST", "/labels/set", {"guid": "guidAAAAAA", "new": "batch one", "csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                revisions.apply_edit(conn, "guidAAAAAA", {"Text": "first card edited"}, 1, "staff")
                revisions.apply_edit(conn, "guidBBBBBB", {"Text": "second card edited"}, 1, "staff")
            _, _, found = request(base, "GET", "/cards?q=label:batch+one", cookie=cookie)
            self.assertIn("first card", found); self.assertNotIn("second card", found)
            _, _, everything = request(base, "GET", "/releases/next", cookie=cookie)
            self.assertIn("first card edited", everything); self.assertIn("second card edited", everything)
            _, _, only = request(base, "GET", "/releases/next?label=batch+one", cookie=cookie)
            self.assertIn("first card edited", only); self.assertNotIn("second card edited", only)
            self.assertIn("Show everything", only)


if __name__ == "__main__":
    unittest.main()
