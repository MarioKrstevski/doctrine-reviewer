import json
import unittest
import urllib.error
import urllib.request

from tests.helpers import running_server

PUBLIC = "https://doctrine.example"


def post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def register_one(base, doctrine_id="doc-test000001"):
    return post(base, "/api/dev/register", {"notes": [{
        "doctrine_id": doctrine_id,
        "anki_note_id": 1,
        "note_type": "Basic",
        "deck": "Test",
        "fields": {"Front": "Q", "Back": "A"},
        "question_html": "Q",
        "answer_html": "A",
        "css": "",
    }]})


class TrackingUrlTest(unittest.TestCase):
    def test_tracking_url_uses_public_base_url(self):
        with running_server(PUBLIC_BASE_URL=PUBLIC) as base:
            register_one(base)
            _, body = post(base, "/api/suggestions", {
                "doctrine_id": "doc-test000001",
                "text": "The dose is wrong.",
                "suggestion_type": "incorrect",
                "snapshot": {"fields": {"Front": "Q", "Back": "A"}},
            })
            self.assertTrue(
                body["tracking_url"].startswith(PUBLIC + "/s/"),
                f"leaked a local URL: {body['tracking_url']}",
            )
            self.assertNotIn("127.0.0.1", body["tracking_url"])


class UnknownIdTest(unittest.TestCase):
    def test_unregistered_id_is_rejected(self):
        with running_server() as base:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post(base, "/api/suggestions", {
                    "doctrine_id": "doc-spoofed0001",
                    "text": "hello",
                    "snapshot": {},
                })
            self.assertEqual(404, ctx.exception.code)
