"""Suggestions are accepted without a master on file.

The client's deck is not registered with us and may never be; master
copies will come from their API once it exists (fetch_master stub). Until
then the student snapshot is the record, and the reviewer sees that
explicitly instead of a 404 at submit time.
"""

import json
import unittest
import urllib.request

import server
from tests.helpers import register_note, running_server

GUID = "P]%rwtghL]"


def suggest(base, **over):
    body = {"doctrine_id": GUID, "doctorine_id": "", "guid": GUID,
            "text": "typo in the answer",
            "snapshot": {"fields": {"Text": "Q", "Doctorine ID": ""}}}
    body.update(over)
    req = urllib.request.Request(base + "/api/suggestions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def last():
    with server.db() as conn:
        return dict(conn.execute("SELECT * FROM suggestions ORDER BY id DESC LIMIT 1").fetchone())


class AcceptWithoutMasterTest(unittest.TestCase):
    def test_unknown_id_is_accepted_and_tracked(self):
        with running_server() as base:
            status, body = suggest(base)
            row = last()
        self.assertEqual(200, status)
        self.assertIn("/s/", body["tracking_url"])
        self.assertEqual(GUID, row["doctrine_id"])
        self.assertEqual(GUID, row["guid"])
        self.assertEqual("", row["doctorine_id"] or "")

    def test_filled_doctorine_id_is_the_filing_key(self):
        with running_server() as base:
            suggest(base, doctrine_id="db-77", doctorine_id="db-77")
            row = last()
        self.assertEqual("db-77", row["doctrine_id"])
        self.assertEqual("db-77", row["doctorine_id"])
        self.assertEqual(GUID, row["guid"])

    def test_empty_ids_are_still_rejected(self):
        with running_server() as base:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                suggest(base, doctrine_id="", guid="")
        self.assertEqual(400, ctx.exception.code)

    def test_text_is_still_required(self):
        with running_server() as base:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                suggest(base, text="")
        self.assertEqual(400, ctx.exception.code)

    def test_fetch_master_is_consulted_and_may_return_none(self):
        calls = []
        real = server.fetch_master
        server.fetch_master = lambda d, g: calls.append((d, g)) or None
        try:
            with running_server() as base:
                suggest(base, doctrine_id="db-1", doctorine_id="db-1")
        finally:
            server.fetch_master = real
        self.assertEqual([("db-1", GUID)], calls)


class ReviewerWithoutMasterTest(unittest.TestCase):
    def _html(self, base):
        return server.render_reviewer({"id": 1, "username": "r", "role": "reviewer"},
                                      "secret", "sess")

    def test_queue_says_no_master_on_file(self):
        with running_server() as base:
            suggest(base)
            html = self._html(base)
        self.assertIn("No master on file", html)
        self.assertNotIn("Card changed since", html)

    def test_registered_master_still_compares(self):
        with running_server() as base:
            register_note(base, "reg-1", fields={"Text": "Q"})
            suggest(base, doctrine_id="reg-1", doctorine_id="reg-1",
                    snapshot={"fields": {"Text": "Q"},
                              "content_hash": server.content_hash({"Text": "Q"})})
            html = self._html(base)
        self.assertIn("Matches current version", html)

    def test_guid_with_url_characters_is_escaped_in_html(self):
        with running_server() as base:
            suggest(base, guid="a<b>&\"c", doctrine_id="a<b>&\"c")
            html = self._html(base)
        self.assertNotIn('a<b>&"c', html)
        self.assertIn("a&lt;b&gt;&amp;", html)
