"""Thank-you outbox: a human-worked queue of people to write back to.

A row is queued only when a suggestion is RESOLVED and the submitter left
an email. Declines and expirations queue nothing. Nothing sends mail.
"""

import json
import unittest
import urllib.parse
import urllib.request

import auth
import server
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request

DID = "6944" + "5" * 20


def open_suggestion(base, email="student@example.com", text="The dose is 5 mg, not 50."):
    register_note(base, DID)
    body = {"doctrine_id": DID, "text": text, "email": email,
            "snapshot": {"fields": {"Front": "Q", "Back": "A"}}}
    req = urllib.request.Request(base + "/api/suggestions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()
    with server.db() as conn:
        return conn.execute("SELECT id FROM suggestions ORDER BY id DESC LIMIT 1").fetchone()["id"]


def act(base, cookie, sid, do, **extra):
    csrf = auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())
    form = {"id": sid, "do": do, "csrf": csrf}
    form.update(extra)
    return request(base, "POST", "/reviewer/action", form, cookie=cookie)


def notifications():
    with server.db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM notifications ORDER BY id")]


class QueueOnResolveTest(unittest.TestCase):
    def test_resolve_with_email_queues_exactly_one_thank_you(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = open_suggestion(base)
            act(base, cookie, sid, "resolve")
            rows = notifications()
        self.assertEqual(1, len(rows))
        n = rows[0]
        self.assertEqual(sid, n["suggestion_id"])
        self.assertEqual("student@example.com", n["email"])
        self.assertEqual("pending", n["status"])
        self.assertIn("accepted", n["subject"].lower())
        self.assertIn("/s/", n["body"], "body must carry the tracking link")
        self.assertIn("5 mg", n["body"], "body should quote their suggestion")

    def test_resolve_without_email_queues_nothing(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = open_suggestion(base, email=None)
            act(base, cookie, sid, "resolve")
            self.assertEqual([], notifications())

    def test_decline_and_expire_queue_nothing_even_with_email(self):
        for do in ("decline", "expire"):
            with self.subTest(do=do), running_server() as base:
                user, pw = make_user()
                _, cookie = login(base, user, pw)
                sid = open_suggestion(base)
                act(base, cookie, sid, do)
                self.assertEqual([], notifications())

    def test_resolving_twice_cannot_duplicate(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = open_suggestion(base)
            act(base, cookie, sid, "resolve")
            act(base, cookie, sid, "resolve")
            self.assertEqual(1, len(notifications()))

    def test_published_credit_is_mentioned(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = open_suggestion(base)
            act(base, cookie, sid, "resolve", publish="1",
                publish_summary="Fixed dose", credit_name="Marko S.")
            body = notifications()[0]["body"]
        self.assertIn("Marko S.", body)
        self.assertIn("/updates", body)


class ThankYouTextTest(unittest.TestCase):
    def test_render_is_pure_and_escapes_nothing(self):
        subject, body = server.render_thank_you(
            deck="Doctrine", text="A < B & C", tracking_url="https://x/s/abc",
            credit_name=None, published=False)
        self.assertIn("A < B & C", body, "plain-text mail, no HTML escaping")
        self.assertIn("https://x/s/abc", body)
        self.assertTrue(subject)

    def test_long_suggestions_are_truncated_in_the_quote(self):
        _, body = server.render_thank_you(
            deck="D", text="x" * 500, tracking_url="u", credit_name=None,
            published=False)
        self.assertNotIn("x" * 200, body)
        self.assertIn("…", body)
