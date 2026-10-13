"""A prepared release needs an administrator's approval, from someone other than the preparer,
before it can be built. Any change to the draft resets the approval."""

import json
import time
import unittest
import urllib.request

import revisions
import server
from tests.helpers import register_note, running_server
from tests.test_cards import csrf_for
from tests.test_release_routes import seed_baseline
from tests.test_auth_routes import login, make_user, request

A, B = "guidAAAAAA", "guidBBBBBB"


def person(base, name, role):
    user, pw = make_user(name, "staffpassword1", role)
    return login(base, user, pw)[1]


def status_of(version):
    with server.db() as conn:
        return dict(conn.execute("SELECT * FROM releases WHERE version=?", (version,)).fetchone())


class ApprovalTest(unittest.TestCase):
    def _draft(self, base, cutter):
        seed_baseline()
        register_note(base, A, fields={"Text": "first old", "Extra": "e"})
        register_note(base, B, fields={"Text": "second old", "Extra": "e"})
        with server.db() as conn:
            revisions.apply_edit(conn, A, {"Text": "first new"}, 1, "staff")
            revisions.apply_edit(conn, B, {"Text": "second new"}, 1, "staff")
        request(base, "POST", "/releases/cut", {"version": "1.1", "csrf": csrf_for(cutter), f"rev:{A}": "1", f"rev:{B}": "1"},
                cookie=cutter)

    def test_build_is_locked_until_an_admin_other_than_the_preparer_approves(self):
        with running_server() as base:
            prep = person(base, "preparer", "reviewer")
            self._draft(base, prep)
            self.assertIsNotNone(status_of("1.1")["prepared_by"])
            # locked: the confirm page offers no build form, and a direct POST does nothing
            _, _, page_ = request(base, "GET", "/releases/1.1/confirm", cookie=prep)
            self.assertIn("building is locked", page_)
            request(base, "POST", "/releases/1.1/build", {"csrf": csrf_for(prep), "confirm": "1.1"}, cookie=prep)
            self.assertEqual("draft", status_of("1.1")["status"])
            # a reviewer cannot approve
            request(base, "POST", "/releases/1.1/approve", {"csrf": csrf_for(prep)}, cookie=prep)
            self.assertIsNone(status_of("1.1")["approved_by"])
            # an admin can
            admin = person(base, "second_admin", "admin")
            _, _, html = request(base, "POST", "/releases/1.1/approve", {"csrf": csrf_for(admin)}, cookie=admin)
            self.assertIn("approved", html.lower())
            self.assertIsNotNone(status_of("1.1")["approved_by"])
            request(base, "POST", "/releases/1.1/build", {"csrf": csrf_for(prep), "confirm": "1.1"}, cookie=prep)
            for _ in range(100):
                if status_of("1.1")["status"] in ("published", "failed"):
                    break
                time.sleep(0.1)
            self.assertEqual("published", status_of("1.1")["status"], status_of("1.1")["build_log"])

    def test_the_preparer_cannot_approve_their_own_draft_unless_allowed(self):
        with running_server() as base:
            admin = person(base, "solo_admin", "admin")
            self._draft(base, admin)
            _, _, html = request(base, "POST", "/releases/1.1/approve", {"csrf": csrf_for(admin)}, cookie=admin)
            self.assertIn("another administrator", html)
            self.assertIsNone(status_of("1.1")["approved_by"])
            request(base, "POST", "/admin/self-approval", {"allow": "1", "csrf": csrf_for(admin)}, cookie=admin)
            request(base, "POST", "/releases/1.1/approve", {"csrf": csrf_for(admin)}, cookie=admin)
            self.assertIsNotNone(status_of("1.1")["approved_by"])
            _, _, users = request(base, "GET", "/admin/users", cookie=admin)
            self.assertIn("checked", users.split("Let an administrator approve")[0].rsplit("<input", 1)[1])

    def test_removing_a_card_resets_approval_and_sends_it_back_to_next_release(self):
        with running_server() as base:
            prep = person(base, "preparer", "reviewer")
            self._draft(base, prep)
            admin = person(base, "second_admin", "admin")
            request(base, "POST", "/releases/1.1/approve", {"csrf": csrf_for(admin)}, cookie=admin)
            self.assertIsNotNone(status_of("1.1")["approved_by"])
            _, _, html = request(base, "POST", "/releases/1.1/remove", {"guid": B, "csrf": csrf_for(admin)}, cookie=admin)
            self.assertIn("approval was reset", html)
            self.assertIsNone(status_of("1.1")["approved_by"])
            with server.db() as conn:
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM revisions WHERE guid=? AND release IS NULL", (B,)).fetchone()[0])
                self.assertEqual(1, conn.execute("SELECT COUNT(DISTINCT guid) FROM revisions WHERE release='1.1'").fetchone()[0])
            _, _, nxt = request(base, "GET", "/releases/next", cookie=admin)
            self.assertIn("second new", nxt)

    def test_releases_page_shows_the_approval_state(self):
        with running_server() as base:
            prep = person(base, "preparer", "reviewer")
            self._draft(base, prep)
            _, _, html = request(base, "GET", "/releases", cookie=prep)
            self.assertIn("waiting for approval", html)
            self.assertIn("Review and approve", html)
            self.assertNotIn(">Build and publish<", html)
            admin = person(base, "second_admin", "admin")
            request(base, "POST", "/releases/1.1/approve", {"csrf": csrf_for(admin)}, cookie=admin)
            _, _, html = request(base, "GET", "/releases", cookie=prep)
            self.assertIn("Approved draft", html)
            self.assertIn(">Build and publish<", html)


if __name__ == "__main__":
    unittest.main()
