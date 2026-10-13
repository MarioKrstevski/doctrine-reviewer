"""Turn a sync payload plus the local collection into a concrete plan.

Pure: no Anki. local = {guid: {"nid": int, "fields": {...}}}; media_have =
set of filenames already in the student's media folder.
"""

import unittest

from sync_plan import plan

PAYLOAD = {
    "from": "1.0", "to": "1.2", "baseline_missing": False,
    "changed": [
        {"guid": "A", "fields": {"Text": "a2", "Extra": "ea"}, "before": {"Text": "a0", "Extra": "ea"}},
        {"guid": "Z", "fields": {"Text": "z2", "Extra": ""}, "before": {"Text": "z0", "Extra": ""}},
    ],
    "added": [{"guid": "N", "note_type": "AnKingOverhaul++++++", "fields": {"Text": "new"}, "tags": ["t"]}],
    "retired": ["B"],
    "media": ["pic.png", "have.png"],
}
LOCAL = {"A": {"nid": 1, "fields": {"Text": "a0", "Extra": "ea", "Pathoma": ""}},
         "B": {"nid": 2, "fields": {"Text": "b", "Extra": ""}}}


class PlanTest(unittest.TestCase):
    def test_updates_only_editable_fields_of_notes_the_student_has(self):
        p = plan(PAYLOAD, LOCAL, media_have={"have.png"})
        self.assertEqual([{"guid": "A", "nid": 1, "fields": {"Text": "a2", "Extra": "ea"}, "local_edit": False}], p["updates"])

    def test_missing_notes_are_skipped_with_a_reason(self):
        p = plan(PAYLOAD, LOCAL, media_have=set())
        self.assertIn({"guid": "Z", "reason": "not in collection"}, p["skipped"])

    def test_adds_are_listed(self):
        p = plan(PAYLOAD, LOCAL, media_have=set())
        self.assertEqual(["N"], [a["guid"] for a in p["adds"]])

    def test_an_add_the_student_already_has_becomes_an_update(self):
        local = dict(LOCAL, N={"nid": 9, "fields": {"Text": "old new"}})
        p = plan(PAYLOAD, local, media_have=set())
        self.assertEqual([], p["adds"])
        self.assertIn("N", [u["guid"] for u in p["updates"]])

    def test_retired_notes_are_never_deleted_only_reported(self):
        p = plan(PAYLOAD, LOCAL, media_have=set())
        self.assertEqual(["B"], p["retired_present"])
        self.assertNotIn("B", [u["guid"] for u in p["updates"]])

    def test_local_edit_is_flagged_but_still_overwritten(self):
        local = dict(LOCAL, A={"nid": 1, "fields": {"Text": "student changed this", "Extra": "ea"}})
        p = plan(PAYLOAD, local, media_have=set())
        u = p["updates"][0]
        self.assertTrue(u["local_edit"])
        self.assertEqual("a2", u["fields"]["Text"])
        self.assertEqual(1, p["counts"]["local_edits"])

    def test_already_matching_text_is_not_an_update(self):
        local = dict(LOCAL, A={"nid": 1, "fields": {"Text": "a2", "Extra": "ea"}})
        p = plan(PAYLOAD, local, media_have=set())
        self.assertEqual([], [u for u in p["updates"] if u["guid"] == "A"])

    def test_media_to_fetch_excludes_what_the_student_has(self):
        p = plan(PAYLOAD, LOCAL, media_have={"have.png"})
        self.assertEqual(["pic.png"], p["media"])

    def test_counts(self):
        p = plan(PAYLOAD, LOCAL, media_have=set())
        self.assertEqual({"updates": 1, "adds": 1, "skipped": 1, "retired_present": 1, "media": 2, "local_edits": 0}, p["counts"])

    def test_empty_payload_is_a_noop(self):
        p = plan({"changed": [], "added": [], "retired": [], "media": []}, LOCAL, set())
        self.assertEqual(0, sum(p["counts"].values()))


class VersionSummaryTest(unittest.TestCase):
    def test_lines(self):
        import sync_plan as sp
        self.assertEqual(("Your deck: 1.0", "Newest: 1.3", "3 releases behind"), sp.version_summary("1.0", "1.3", 3))
        self.assertEqual("1 release behind", sp.version_summary("1.2", "1.3", 1)[2])
        self.assertEqual("", sp.version_summary("1.3", "1.3", 0)[2])
        self.assertEqual("Your deck: not recorded yet", sp.version_summary("", "1.3", None)[0])
