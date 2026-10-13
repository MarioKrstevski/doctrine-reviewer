"""An edit made after a release is cut must not leak into that release or sync."""

import unittest

import revisions
import server
from tests.helpers import register_note, running_server
from tests.test_sync_api import publish, stamp, sync

A = "P]%rwtghL]"


class TextAtTest(unittest.TestCase):
    def test_pending_and_later_edits_do_not_leak(self):
        with running_server() as base:
            publish("1.0")
            register_note(base, A, fields={"Text": "t0", "Extra": "e0"})
            with server.db() as conn:
                revisions.apply_edit(conn, A, {"Text": "t1"}, 1, "staff")
            stamp("1.1"); publish("1.1")
            with server.db() as conn:
                revisions.apply_edit(conn, A, {"Text": "t2 pending", "Extra": "e1 pending"}, 1, "staff")
            _, body = sync(base, "1.0")
            ch = body["changed"][0]
            self.assertEqual("t1", ch["fields"]["Text"])
            self.assertEqual("e0", ch["fields"]["Extra"])
            self.assertEqual("t0", ch["before"]["Text"])
            with server.db() as conn:
                self.assertEqual({"Text": "t1", "Extra": "e0"},
                                 revisions.text_at(conn, A, {"1.1"}, {"Text": "t2 pending", "Extra": "e1 pending"}))
                self.assertEqual({"Text": "t0", "Extra": "e0"},
                                 revisions.text_at(conn, A, set(), {"Text": "t2 pending", "Extra": "e1 pending"}))


if __name__ == "__main__":
    unittest.main()
