"""The add-on and the server must produce identical content hashes.

Staleness detection compares a hash computed inside Anki against one
computed on the server. If the two implementations ever drift, every
suggestion silently looks "changed since submission" and the reviewer
queue becomes untrustworthy. CLAUDE.md states they must stay identical;
this test is what enforces it.

The add-on cannot be imported directly (it needs aqt), so its function is
extracted from source and executed in an isolated namespace.
"""

import hashlib
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ID_FIELD = "DoctrineID"


def _extract(path):
    src = (ROOT / path).read_text()
    match = re.search(r"^def content_hash.*?return hashlib[^\n]*\n", src, re.S | re.M)
    if match is None:
        raise AssertionError(f"no content_hash() found in {path}")
    namespace = {"hashlib": hashlib, "ID_FIELD": ID_FIELD}
    exec(match.group(0), namespace)
    return namespace["content_hash"]


CASES = [
    {"Front": "Q", "Back": "A", ID_FIELD: "doc-1"},
    {"Back": "A", "Front": "Q", ID_FIELD: "doc-2"},
    {"Extra": "text with \x1f and \x1e separators", "Front": "x", ID_FIELD: "d"},
    {"Front": "unicode: café — 日本語", ID_FIELD: "d"},
    {"Front": "", "Back": "", ID_FIELD: "d"},
    {},
]


class HashParityTest(unittest.TestCase):
    def setUp(self):
        self.addon = _extract("addon/__init__.py")
        self.server = _extract("server/server.py")

    def test_both_sides_agree(self):
        for fields in CASES:
            with self.subTest(fields=sorted(fields)):
                self.assertEqual(
                    self.addon(fields, ID_FIELD),
                    self.server(fields, ID_FIELD),
                    "add-on and server content_hash have drifted apart",
                )

    def test_field_order_does_not_matter(self):
        self.assertEqual(
            self.addon({"A": "1", "B": "2", ID_FIELD: "d"}, ID_FIELD),
            self.addon({"B": "2", "A": "1", ID_FIELD: "d"}, ID_FIELD),
        )

    def test_the_id_field_is_excluded(self):
        self.assertEqual(
            self.addon({"F": "1", ID_FIELD: "x"}, ID_FIELD),
            self.addon({"F": "1", ID_FIELD: "y"}, ID_FIELD),
            "the ID field must not affect the hash, or every stamp would "
            "look like a content change",
        )

    def test_different_content_gives_different_hashes(self):
        self.assertNotEqual(
            self.addon({"F": "1"}, ID_FIELD),
            self.addon({"F": "2"}, ID_FIELD),
        )
