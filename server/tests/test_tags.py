"""Tags are edited on the platform, ship in releases and reach students as a diff.
Cards can be removed and pending changes undone."""

import json
import os
import tempfile
import unittest
import urllib.parse

import release_build as rb
import reupload
import revisions
import server
import tagindex
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request
from tests.test_cards import csrf_for
from tests.test_release_build import CLOZE, T0, make_baseline, rows
from tests.test_sync_api import publish, stamp, sync

A, B = "P]%rwtghL]", "s.MBeTB!D."


def set_tags(guid, tags):
    with server.db() as conn:
        conn.execute("UPDATE notes SET tags_json=? WHERE doctrine_id=?", (json.dumps(tags), guid))


def tags_of(guid):
    with server.db() as conn:
        return json.loads(conn.execute("SELECT tags_json FROM notes WHERE doctrine_id=?", (guid,)).fetchone()[0])


class CleanTagsTest(unittest.TestCase):
    def test_splits_dedupes_case_insensitively_and_keeps_order(self):
        self.assertEqual(["B", "a::x", "c"], revisions.clean_tags("B a::x  b c A::X"))
        self.assertEqual(["x"], revisions.clean_tags(["x", None, 3, "X"]))


class ApplyTagsTest(unittest.TestCase):
    def test_records_one_revision_and_ignores_no_ops(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "t", "Extra": ""})
            set_tags(A, ["Old", "Keep"])
            with server.db() as conn:
                self.assertTrue(revisions.apply_tags(conn, A, "Keep New", 1, "staff"))
                self.assertFalse(revisions.apply_tags(conn, A, ["keep", "new"], 1, "staff"), "case and order only")
                revs = conn.execute("SELECT before, after FROM revisions WHERE guid=? AND field='tags'", (A,)).fetchall()
            self.assertEqual(["Keep", "New"], tags_of(A))
        self.assertEqual(1, len(revs))
        self.assertEqual((["Old", "Keep"], ["Keep", "New"]), (json.loads(revs[0][0]), json.loads(revs[0][1])))


class CardPageTest(unittest.TestCase):
    def test_edit_page_shows_the_tag_editor_and_saving_tags_records_a_revision(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "t {{c1::x}}", "Extra": ""})
            set_tags(A, ["Doctorine::Shelf::Micro"])
            user, pw = make_user(); _, cookie = login(base, user, pw)
            q = urllib.parse.quote(A, safe="")
            _, _, html = request(base, "GET", f"/cards/edit?guid={q}", cookie=cookie)
            self.assertIn("data-tagbox", html)
            self.assertIn('value="Doctorine::Shelf::Micro"', html, "current tags prefill the editor")
            status, _, _ = request(base, "POST", f"/cards/edit?guid={q}",
                                   {"csrf": csrf_for(cookie), "Text": "t {{c1::x}}", "Extra": "",
                                    "tags": "Doctorine::Shelf::Micro Doctorine::Shelf::Path"}, cookie=cookie)
            self.assertEqual(303, status)
            self.assertEqual(["Doctorine::Shelf::Micro", "Doctorine::Shelf::Path"], tags_of(A))
            _, _, html = request(base, "GET", f"/cards/edit?guid={q}", cookie=cookie)
        self.assertIn("+ Doctorine::Shelf::Path", html, "history shows the added tag")

    def test_remove_and_undo(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "t", "Extra": ""})
            user, pw = make_user(); _, cookie = login(base, user, pw)
            q = urllib.parse.quote(A, safe="")
            request(base, "POST", f"/cards/retire?guid={q}", {"csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                self.assertEqual(1, conn.execute("SELECT retired FROM notes WHERE doctrine_id=?", (A,)).fetchone()[0])
            _, _, html = request(base, "GET", f"/cards/edit?guid={q}", cookie=cookie)
            self.assertIn("leaves the deck in the next release", html)
            request(base, "POST", f"/cards/revert?guid={q}", {"csrf": csrf_for(cookie)}, cookie=cookie)
            with server.db() as conn:
                self.assertEqual(0, conn.execute("SELECT retired FROM notes WHERE doctrine_id=?", (A,)).fetchone()[0])
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM revisions WHERE guid=? AND release IS NULL", (A,)).fetchone()[0])

    def test_undo_restores_text_and_tags_and_deletes_an_unreleased_new_note(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "old", "Extra": "e"})
            set_tags(A, ["T1"])
            with server.db() as conn:
                revisions.apply_edit(conn, A, {"Text": "new"}, 1, "staff")
                revisions.apply_tags(conn, A, ["T2"], 1, "staff")
                self.assertEqual("reverted", revisions.revert_pending(conn, A))
                row = conn.execute("SELECT fields_json, tags_json FROM notes WHERE doctrine_id=?", (A,)).fetchone()
                self.assertEqual("old", json.loads(row[0])["Text"])
                self.assertEqual(["T1"], json.loads(row[1]))
                g = revisions.add_note(conn, "Cloze", {"Text": "x {{c1::y}}"}, [], 1)
                self.assertEqual("deleted", revisions.revert_pending(conn, g))
                self.assertIsNone(conn.execute("SELECT 1 FROM notes WHERE doctrine_id=?", (g,)).fetchone())

    def test_new_note_form_takes_tags(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "t", "Extra": ""})
            user, pw = make_user(); _, cookie = login(base, user, pw)
            _, _, html = request(base, "GET", "/notes/new", cookie=cookie)
            self.assertIn("data-tagbox", html)
            request(base, "POST", "/notes/new", {"csrf": csrf_for(cookie), "note_type": "Cloze",
                                                 "Text": "n {{c1::x}}", "Extra": "", "tags": "A::b A::B c"}, cookie=cookie)
            with server.db() as conn:
                row = conn.execute("SELECT tags_json FROM notes WHERE fields_json LIKE '%n {{c1::x}}%'").fetchone()
        self.assertEqual(["A::b", "c"], json.loads(row[0]))


class SyncTagsTest(unittest.TestCase):
    def _seed(self, base):
        publish("1.0")
        register_note(base, A, fields={"Text": "a0", "Extra": ""})
        register_note(base, B, fields={"Text": "b0", "Extra": ""})
        set_tags(A, ["Keep", "Gone"]); set_tags(B, ["X"])
        with server.db() as conn:
            revisions.apply_tags(conn, A, ["Keep", "Added"], 1, "staff")
            revisions.apply_edit(conn, B, {"Text": "b1"}, 1, "staff")
            revisions.apply_tags(conn, B, ["X", "Y"], 1, "staff")
        stamp("1.1"); publish("1.1")
        with server.db() as conn:
            revisions.apply_tags(conn, A, ["Pending"], 1, "staff")   # not released: must not leak

    def test_tag_only_change_has_no_fields_and_a_diff(self):
        with running_server() as base:
            self._seed(base)
            _, body = sync(base, "1.0")
        by = {c["guid"]: c for c in body["changed"]}
        self.assertEqual({}, by[A]["fields"], "old add-ons skip an entry with no fields")
        self.assertEqual({"add": ["Added"], "remove": ["Gone"]}, by[A]["tags"])
        self.assertEqual("b1", by[B]["fields"]["Text"])
        self.assertEqual({"add": ["Y"], "remove": []}, by[B]["tags"])

    def test_up_to_date_gets_nothing(self):
        with running_server() as base:
            self._seed(base)
            _, body = sync(base, "1.1")
        self.assertEqual([], body["changed"])


class PatchTagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src.sqlite")
        self.dst = os.path.join(self.tmp.name, "dst.sqlite")
        make_baseline(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tags_are_rewritten_and_registered(self):
        out = rb.patch_collection(self.src, self.dst, changed={}, new_notes=[], retired=[], now=T0 + 10,
                                  tags={"guidBBBBBB": ["Doctorine::New", "Other"]})
        self.assertEqual(1, out["tagged"])
        c = rb.connect(self.dst)
        self.assertEqual(" Doctorine::New Other ", c.execute("SELECT tags FROM notes WHERE guid='guidBBBBBB'").fetchone()[0])
        self.assertIn("Doctorine::New", {r[0] for r in c.execute("SELECT tag FROM tags")})
        c.close()

    def test_a_new_cloze_number_gets_a_card(self):
        out = rb.patch_collection(self.src, self.dst, changed={"guidAAAAAA": {"Text": "{{c1::a}} {{c3::b}}", "Extra": ""}},
                                  new_notes=[], retired=[], now=T0 + 10)
        self.assertEqual(1, out["new_cards"])
        ords = sorted(r[3] for r in rows(self.dst, "cards") if r[1] == 1001)
        self.assertEqual([0, 2], ords)


class ReuploadTagsTest(unittest.TestCase):
    def test_tag_changes_apply_only_when_chosen(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "a", "Extra": ""})
            register_note(base, B, fields={"Text": "b", "Extra": ""})
            set_tags(A, ["T"]); set_tags(B, ["T"])
            with server.db() as conn:
                incoming = {A: {"fields": {"Text": "a", "Extra": ""}, "tags": ["T", "U"]},
                            B: {"fields": {"Text": "b", "Extra": ""}, "tags": ["t"]}}
                diff = reupload.compare(reupload.snapshot(conn), incoming)
                self.assertEqual([A], list(diff["tag_changes"]), "case-only difference is not a change")
                out = reupload.apply(conn, diff, incoming, {}, 1, 7, tag_decisions={A: "file"})
            self.assertEqual(1, out["tagged"])
            self.assertEqual(["T", "U"], tags_of(A))


class TagIndexTest(unittest.TestCase):
    def test_counts_each_note_once_per_node(self):
        data = tagindex.build([json.dumps(["Shelf::Micro::Virus", "Shelf::Micro::Bact"]),
                               json.dumps(["Shelf::Path"]), json.dumps(["shelf::micro::virus"])])
        top = tagindex.children(data)
        self.assertEqual([{"path": "Shelf", "name": "Shelf", "count": 3, "has_children": True}], top)
        micro = tagindex.children(data, "Shelf")
        self.assertEqual(["Micro", "Path"], [n["name"] for n in micro])
        self.assertEqual(2, micro[0]["count"])
        self.assertEqual(["Shelf::Micro::Bact", "Shelf::Micro::Virus"],
                         [n["path"] for n in tagindex.children(data, "Shelf::Micro")])

    def test_sql_match_is_exact_or_below_and_escapes_underscores(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE notes (tags_json TEXT)")
        for t in (["Internal_Medicine"], ["InternalXMedicine"], ["Internal_Medicine::Cardio"], ["Internal_Medicine2"],
                  ["Shelf::Internal_Medicine"]):
            c.execute("INSERT INTO notes VALUES (?)", (json.dumps(t),))
        sql, args = tagindex.sql_match("Internal_Medicine")
        got = [json.loads(r[0])[0] for r in c.execute(f"SELECT tags_json FROM notes WHERE {sql}", args)]
        self.assertEqual(["Internal_Medicine", "Internal_Medicine::Cardio", "Shelf::Internal_Medicine"], got)


if __name__ == "__main__":
    unittest.main()


class TagTreePageTest(unittest.TestCase):
    def test_tree_endpoint_and_exact_tag_search(self):
        with running_server() as base:
            register_note(base, A, fields={"Text": "virus card", "Extra": ""})
            register_note(base, B, fields={"Text": "bacteria card", "Extra": ""})
            set_tags(A, ["Shelf::Micro::Virus"]); set_tags(B, ["Shelf::Micro::Virus_X"])
            self.assertEqual(401, request(base, "GET", "/cards/tags")[0])
            user, pw = make_user(); _, cookie = login(base, user, pw)
            _, _, top = request(base, "GET", "/cards/tags", cookie=cookie)
            self.assertEqual([{"path": "Shelf", "name": "Shelf", "count": 2, "has_children": True}], json.loads(top)["children"])
            _, _, hits = request(base, "GET", "/cards/tags?find=virus", cookie=cookie)
            self.assertEqual({"Shelf::Micro::Virus", "Shelf::Micro::Virus_X"}, {h["path"] for h in json.loads(hits)["hits"]})
            _, _, html = request(base, "GET", "/cards?q=" + urllib.parse.quote("tag:Shelf::Micro::Virus"), cookie=cookie)
            self.assertIn("virus card", html)
            self.assertNotIn("bacteria card", html, "exact tag, not every tag that starts with the same text")
            self.assertIn('id="tagtree"', html)
            _, _, html = request(base, "GET", "/cards?q=" + urllib.parse.quote("tag:Shelf::Micro"), cookie=cookie)
            self.assertIn("virus card", html); self.assertIn("bacteria card", html)
            _, _, html = request(base, "GET", "/cards?q=" + urllib.parse.quote("tag:*Virus_X"), cookie=cookie)
            self.assertIn("bacteria card", html); self.assertNotIn("virus card", html)
