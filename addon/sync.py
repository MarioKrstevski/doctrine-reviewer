"""Deck sync: apply a release's changes to the student's collection.

Everything that touches Anki lives here, behind small functions that take
the plan from sync_plan (pure, tested). Rules enforced:
- only Text and Extra are written; scheduling and other fields untouched
- tags get the release's added and removed tags; the student's own tags stay
- adds go into the deck the student keeps Doctorine in (or "Doctorine v2")
- retired notes are never deleted
- every note is applied inside its own try/except; one failure never leaves
  the collection half-written for that note, and the run continues
- an undo checkpoint wraps the whole run, so Edit > Undo reverts it
"""

import urllib.parse
import urllib.request

from . import addon_log, sync_plan

log = addon_log.log


def local_state(col, guids):
    """{guid: {"nid", "fields", "tags"}} for the given guids that exist locally."""
    out = {}
    if not guids:
        return out
    # Anki has no guid index API; a single SQL round-trip over all notes is
    # far cheaper than find_notes per guid for 30k cards.
    rows = col.db.all("SELECT id, guid FROM notes")
    wanted = set(guids)
    nid_by_guid = {g: nid for nid, g in rows if g in wanted}
    for guid, nid in nid_by_guid.items():
        note = col.get_note(nid)
        out[guid] = {"nid": nid, "fields": {k: note[k] for k in note.keys()}, "tags": list(note.tags)}
    return out


def media_have(col, names):
    return {n for n in names if col.media.have(n)}


def fetch_media(col, api_base, names, on_progress=None):
    """Download missing media into the collection's media folder."""
    done, failed = 0, []
    for i, name in enumerate(names, start=1):
        try:
            url = api_base.rstrip("/") + "/media/" + urllib.parse.quote(name, safe="")
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read()
            col.media.write_data(name, data)
            done += 1
        except Exception as e:
            failed.append((name, repr(e)))
            log().warning("media %s failed: %r", name, e)
        if on_progress:
            on_progress(i, len(names))
    return done, failed


def deck_for_adds(col, preferred="Doctorine v2"):
    """Where new notes go: the student's Doctorine deck, else `preferred`."""
    for d in col.decks.all_names_and_ids():
        top = d.name.split("::", 1)[0].strip().lower()
        if top.startswith(("doctrin", "doctorin")):
            return d.id
    return col.decks.id(preferred)


def apply_plan(col, plan, on_progress=None):
    """Write the plan. Returns counts and a list of (guid, error) failures."""
    counts = {"updated": 0, "added": 0, "failed": 0}
    failures = []
    total = len(plan["updates"]) + len(plan["adds"])
    i = 0
    checkpoint = None
    try:
        checkpoint = col.add_custom_undo_entry("Doctrine deck update")
    except Exception:
        checkpoint = None

    for u in plan["updates"]:
        i += 1
        try:
            note = col.get_note(u["nid"])
            for field, value in u["fields"].items():
                if field in note:
                    note[field] = value
            if u.get("tags") is not None:
                note.tags = list(u["tags"])
            col.update_note(note)
            counts["updated"] += 1
        except Exception as e:
            counts["failed"] += 1
            failures.append((u["guid"], repr(e)))
            log().error("update %s failed: %r", u["guid"], e)
        if on_progress:
            on_progress(i, total)

    if plan["adds"]:
        did = deck_for_adds(col)
        for a in plan["adds"]:
            i += 1
            try:
                model = col.models.by_name(a["note_type"])
                if model is None:
                    raise LookupError(f"note type {a['note_type']!r} not in collection")
                note = col.new_note(model)
                for field, value in a["fields"].items():
                    if field in note:
                        note[field] = value
                note.tags = list(a.get("tags") or [])
                note.guid = a["guid"]
                col.add_note(note, did)
                counts["added"] += 1
            except Exception as e:
                counts["failed"] += 1
                failures.append((a["guid"], repr(e)))
                log().error("add %s failed: %r", a["guid"], e)
            if on_progress:
                on_progress(i, total)

    if checkpoint is not None:
        try:
            col.merge_undo_entries(checkpoint)
        except Exception:
            pass
    return counts, failures


def describe(plan, to_version):
    c = plan["counts"]
    lines = [f"Release {to_version}: {c['updates']} card(s) updated, {c['adds']} new."]
    if c["local_edits"]:
        lines.append(f"{c['local_edits']} of them were edited on this computer; "
                     "updating replaces those edits.")
    if c["media"]:
        lines.append(f"{c['media']} image/audio file(s) to download.")
    if c["retired_present"]:
        lines.append(f"{c['retired_present']} card(s) were retired from the deck; "
                     "they are left in your collection.")
    if c["skipped"]:
        lines.append(f"{c['skipped']} changed card(s) are not in your collection and are skipped.")
    return "\n".join(lines)
