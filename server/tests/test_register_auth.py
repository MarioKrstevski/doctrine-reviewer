"""/api/dev/register writes master card state and must not be public.

Master state drives the reviewer's "current version" pane and the
staleness badge, and registering an id makes it pass the "is this card
ours?" check on /api/suggestions. An unauthenticated writer could
therefore corrupt the queue or smuggle in cards that never shipped.

Fails closed: with no key configured the endpoint is disabled outright
rather than falling back to open.
"""

import http.client
import json
import unittest
import urllib.parse

from tests.helpers import running_server

KEY = "test-pipeline-key-123"

NOTE = {"notes": [{
    "doctrine_id": "doc-auth000001", "anki_note_id": 1, "note_type": "Basic",
    "deck": "D", "fields": {"Front": "Q"}, "question_html": "Q",
    "answer_html": "A", "css": "",
}]}


def register(base, key=None, path="/api/dev/register"):
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    conn.request("POST", path, body=json.dumps(NOTE), headers=headers)
    resp = conn.getresponse()
    status = resp.status
    resp.read()
    conn.close()
    return status


def note_count(base):
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    conn.request("POST", "/api/suggestions",
                 body=json.dumps({"doctrine_id": "doc-auth000001",
                                  "text": "probe", "snapshot": {}}),
                 headers={"Content-Type": "application/json"})
    status = conn.getresponse().status
    conn.close()
    return status


class RegisterAuthTest(unittest.TestCase):
    def test_no_key_configured_disables_the_endpoint(self):
        # Explicitly blank, overriding the harness default.
        with running_server(PIPELINE_API_KEY="") as base:
            self.assertEqual(503, register(base))
            self.assertEqual(503, register(base, "anything"))

    def test_a_correct_key_is_accepted(self):
        with running_server(PIPELINE_API_KEY=KEY) as base:
            self.assertEqual(200, register(base, KEY))

    def test_a_missing_key_is_rejected(self):
        with running_server(PIPELINE_API_KEY=KEY) as base:
            self.assertEqual(401, register(base))

    def test_a_wrong_key_is_rejected(self):
        with running_server(PIPELINE_API_KEY=KEY) as base:
            self.assertEqual(401, register(base, "wrong-key"))

    def test_a_rejected_call_writes_nothing(self):
        with running_server(PIPELINE_API_KEY=KEY) as base:
            register(base, "wrong-key")
            # The id must still be unknown, so suggestions for it 404.
            self.assertEqual(404, note_count(base),
                             "an unauthorised register created master state")

    def test_the_new_path_works_too(self):
        with running_server(PIPELINE_API_KEY=KEY) as base:
            self.assertEqual(200, register(base, KEY, "/api/master/sync"))

    def test_the_new_path_is_also_protected(self):
        with running_server(PIPELINE_API_KEY=KEY) as base:
            self.assertEqual(401, register(base, None, "/api/master/sync"))
