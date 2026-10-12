"""Patch a deck's own collection file from revisions, without Anki.

Synthetic baseline using the real schema (tests/fixtures/anki_schema.sql):
one cloze note type, one basic note type, three notes, their cards.
Everything the student's import matches on (guids, note type ids, deck ids,
templates) must survive byte-for-byte.
"""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import release_build as rb

SCHEMA = (Path(__file__).parent / "fixtures" / "anki_schema.sql").read_text()
CLOZE, BASIC, DECK = 1790015250461, 1790098374507, 1788959386773
T0 = 1_700_000_000


def make_baseline(path):
    c = rb.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO col (id, crt, mod, scm, ver, dty, usn, ls, conf, models, decks, dconf, tags) "
              "VALUES (1, ?, ?, ?, 18, 0, 0, 0, '', '', '', '', '')", (T0 - 1000, T0 * 1000, T0 * 1000))
    c.execute("INSERT INTO notetypes VALUES (?, 'Cloze', ?, 0, x'00')", (CLOZE, T0))
    c.execute("INSERT INTO notetypes VALUES (?, 'Basic', ?, 0, x'00')", (BASIC, T0))
    for ntid, names in ((CLOZE, ["Text", "Extra"]), (BASIC, ["Front", "Back"])):
        for i, n in enumerate(names):
            c.execute("INSERT INTO fields VALUES (?, ?, ?, x'00')", (ntid, i, n))
    c.execute("INSERT INTO templates VALUES (?, 0, 'Cloze', ?, 0, x'00')", (CLOZE, T0))
    c.execute("INSERT INTO templates VALUES (?, 0, 'Card 1', ?, 0, x'00')", (BASIC, T0))
    c.execute("INSERT INTO templates VALUES (?, 1, 'Card 2', ?, 0, x'00')", (BASIC, T0))
    c.execute("INSERT INTO decks VALUES (1, 'Default', ?, 0, x'00', x'00')", (T0,))
    c.execute("INSERT INTO decks VALUES (?, 'Doctorine v2', ?, 0, x'00', x'00')", (DECK, T0))
    notes = [(1001, "guidAAAAAA", CLOZE, "Doctorine::Yield::Step1::Gold", ["<b>p53</b> blocks {{c1::G1 to S}}", "extra one"]),
             (1002, "guidBBBBBB", CLOZE, "", ["{{c1::a}} and {{c2::b}}", ""]),
             (1003, "guidCCCCCC", BASIC, "", ["front", "back"])]
    for nid, guid, mid, tags, flds in notes:
        first = re.sub(r"<[^>]+>", "", flds[0])
        csum = int(hashlib.sha1(first.encode()).hexdigest()[:8], 16)
        c.execute("INSERT INTO notes VALUES (?,?,?,?,0,?,?,?,?,0,'')",
                  (nid, guid, mid, T0, f" {tags} " if tags else "", "\x1f".join(flds), first, csum))
    cards = [(2001, 1001, 0), (2002, 1002, 0), (2003, 1002, 1), (2004, 1003, 0), (2005, 1003, 1)]
    for cid, nid, ord_ in cards:
        c.execute("INSERT INTO cards VALUES (?,?,?,?,?,0,0,0,0,0,0,0,0,0,0,0,0,'')", (cid, nid, DECK, ord_, T0))
    c.commit(); c.close()


def rows(path, table):
    c = rb.connect(path); out = [tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]; c.close(); return out


class PatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "base.sqlite")
        self.dst = os.path.join(self.tmp.name, "out.sqlite")
        make_baseline(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_changed_text_updates_fields_sortfield_checksum_mod(self):
        rb.patch_collection(self.src, self.dst,
                            changed={"guidAAAAAA": {"Text": "<b>p53</b> blocks {{c1::G1/S}}", "Extra": "extra one"}},
                            new_notes=[], retired=[], now=T0 + 500)
        c = rb.connect(self.dst)
        n = c.execute("SELECT * FROM notes WHERE guid='guidAAAAAA'").fetchone()
        self.assertEqual("<b>p53</b> blocks {{c1::G1/S}}\x1fextra one", n["flds"])
        self.assertEqual("p53 blocks {{c1::G1/S}}", n["sfld"])
        self.assertEqual(int(hashlib.sha1(b"p53 blocks {{c1::G1/S}}").hexdigest()[:8], 16), n["csum"])
        self.assertEqual(T0 + 500, n["mod"])
        self.assertEqual(-1, n["usn"])
        self.assertEqual(1001, n["id"], "note id must not change")
        self.assertEqual(CLOZE, n["mid"], "note type id must not change")

    def test_untouched_rows_are_identical_and_structure_tables_unchanged(self):
        rb.patch_collection(self.src, self.dst, changed={"guidAAAAAA": {"Text": "x", "Extra": ""}},
                            new_notes=[], retired=[], now=T0 + 1)
        for t in ("notetypes", "fields", "templates", "decks"):
            self.assertEqual(rows(self.src, t), rows(self.dst, t), t)
        src_b = [r for r in rows(self.src, "notes") if r[1] != "guidAAAAAA"]
        dst_b = [r for r in rows(self.dst, "notes") if r[1] != "guidAAAAAA"]
        self.assertEqual(src_b, dst_b)
        self.assertEqual(rows(self.src, "cards"), rows(self.dst, "cards"), "cards untouched by a text edit")

    def test_new_cloze_note_gets_one_card_per_cloze_in_the_main_deck(self):
        rb.patch_collection(self.src, self.dst, changed={},
                            new_notes=[{"guid": "guidNEW001", "note_type": "Cloze",
                                        "fields": {"Text": "{{c1::x}} {{c2::y}} {{c2::z}}", "Extra": "e"},
                                        "tags": ["Doctorine::Yield::Step1::Gold"]}],
                            retired=[], now=T0 + 9)
        c = rb.connect(self.dst)
        n = c.execute("SELECT * FROM notes WHERE guid='guidNEW001'").fetchone()
        self.assertEqual(CLOZE, n["mid"])
        self.assertEqual(" Doctorine::Yield::Step1::Gold ", n["tags"])
        cards = c.execute("SELECT ord, did FROM cards WHERE nid=? ORDER BY ord", (n["id"],)).fetchall()
        self.assertEqual([(0, DECK), (1, DECK)], [tuple(r) for r in cards])
        self.assertGreater(n["id"], 1003)

    def test_new_basic_note_gets_one_card_per_template(self):
        rb.patch_collection(self.src, self.dst, changed={},
                            new_notes=[{"guid": "guidNEW002", "note_type": "Basic",
                                        "fields": {"Front": "f", "Back": "b"}, "tags": []}],
                            retired=[], now=T0 + 9)
        c = rb.connect(self.dst)
        nid = c.execute("SELECT id FROM notes WHERE guid='guidNEW002'").fetchone()[0]
        self.assertEqual([0, 1], [r[0] for r in c.execute("SELECT ord FROM cards WHERE nid=? ORDER BY ord", (nid,))])

    def test_unknown_note_type_is_refused(self):
        with self.assertRaises(ValueError):
            rb.patch_collection(self.src, self.dst, changed={},
                                new_notes=[{"guid": "g", "note_type": "Nope", "fields": {"Text": "t"}, "tags": []}],
                                retired=[], now=T0)

    def test_retired_note_and_its_cards_are_removed(self):
        rb.patch_collection(self.src, self.dst, changed={}, new_notes=[], retired=["guidBBBBBB"], now=T0)
        c = rb.connect(self.dst)
        self.assertIsNone(c.execute("SELECT 1 FROM notes WHERE guid='guidBBBBBB'").fetchone())
        self.assertEqual(0, c.execute("SELECT COUNT(*) FROM cards WHERE nid=1002").fetchone()[0])
        self.assertEqual(3, c.execute("SELECT COUNT(*) FROM graves").fetchone()[0],
                         "one grave for the note and one per card, so a sync propagates the deletion")

    def test_collection_mod_is_bumped_and_source_untouched(self):
        before = open(self.src, "rb").read()
        out = rb.patch_collection(self.src, self.dst, changed={"guidAAAAAA": {"Text": "x", "Extra": ""}},
                                  new_notes=[], retired=[], now=T0 + 7)
        self.assertEqual(before, open(self.src, "rb").read())
        c = rb.connect(self.dst)
        self.assertEqual((T0 + 7) * 1000, c.execute("SELECT mod FROM col").fetchone()[0])
        self.assertEqual({"changed": 1, "added": 0, "retired": 0}, out)

    def test_changing_a_field_the_note_type_lacks_is_refused(self):
        with self.assertRaises(ValueError):
            rb.patch_collection(self.src, self.dst, changed={"guidCCCCCC": {"Text": "x"}},
                                new_notes=[], retired=[], now=T0)


class WriteApkgTest(unittest.TestCase):
    def test_package_round_trips_through_the_reader(self):
        import sys, zipfile
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import apkg
        with tempfile.TemporaryDirectory() as d:
            col = os.path.join(d, "col.sqlite"); make_baseline(col)
            media = [("heart sounds.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20), ("a.mp3", b"ID3" + b"\x01" * 5)]
            paths = []
            for name, data in media:
                p = os.path.join(d, hashlib.sha256(data).hexdigest()); open(p, "wb").write(data); paths.append((name, p))
            out = os.path.join(d, "out.apkg")
            info = rb.write_apkg(out, col, paths)
            pkg = apkg.Package(out)
            names = pkg.media_names()
            self.assertEqual({"0": "heart sounds.png", "1": "a.mp3"}, names)
            self.assertEqual(media[0][1], pkg.media_bytes("0"))
            got = pkg.collection_sqlite()
            self.assertEqual(open(col, "rb").read(), got)
            members = set(zipfile.ZipFile(out).namelist())
            self.assertTrue({"meta", "media", "collection.anki21b", "collection.anki2", "0", "1"} <= members)
            self.assertEqual(2, info["media"]); self.assertGreater(info["bytes"], 0)
            pkg.close()

    def test_meta_declares_the_version_anki_expects(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            col = os.path.join(d, "col.sqlite"); make_baseline(col)
            out = os.path.join(d, "out.apkg"); rb.write_apkg(out, col, [])
            self.assertEqual(b"\x08\x03", zipfile.ZipFile(out).read("meta"))

    def test_media_manifest_encoder_matches_the_decoder(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import apkg
        raw = rb.encode_media_manifest([("x.png", 5, b"\x00" * 20), ("y with space.jpg", 7, b"\x01" * 20)])
        self.assertEqual({"0": "x.png", "1": "y with space.jpg"}, apkg.decode_media_manifest(raw))
