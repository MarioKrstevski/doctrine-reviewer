"""Build a concrete update plan from a sync payload and the local collection.

Pure, so the rules are unit-tested without Anki:
- only Text and Extra are ever written; tags and other fields are untouched
- a note the student does not have is skipped (not added) unless it is in
  `added`; an added note the student already has is updated instead
- retired notes are reported, never deleted
- a local edit (current text != payload `before`) is flagged and overwritten;
  the UI says so before anything is written
- media to fetch = referenced media the student's folder lacks
"""

EDITABLE = ("Text", "Extra")


def plan(payload, local, media_have):
    updates, adds, skipped, retired_present = [], [], [], []
    local_edits = 0

    def wanted(fields):
        return {f: fields.get(f, "") for f in EDITABLE if f in fields}

    for ch in payload.get("changed") or []:
        guid = ch["guid"]
        if guid not in local:
            skipped.append({"guid": guid, "reason": "not in collection"})
            continue
        new = wanted(ch.get("fields") or {})
        cur = local[guid].get("fields") or {}
        if all(cur.get(f, "") == v for f, v in new.items()):
            continue
        before = ch.get("before") or {}
        edited = any(cur.get(f, "") != before.get(f, cur.get(f, "")) for f in new)
        if edited:
            local_edits += 1
        updates.append({"guid": guid, "nid": local[guid]["nid"], "fields": new, "local_edit": edited})

    for ad in payload.get("added") or []:
        guid = ad["guid"]
        if guid in local:
            new = wanted(ad.get("fields") or {})
            cur = local[guid].get("fields") or {}
            if any(cur.get(f, "") != v for f, v in new.items()):
                updates.append({"guid": guid, "nid": local[guid]["nid"], "fields": new, "local_edit": False})
            continue
        adds.append({"guid": guid, "note_type": ad.get("note_type") or "",
                     "fields": dict(ad.get("fields") or {}), "tags": list(ad.get("tags") or [])})

    for guid in payload.get("retired") or []:
        if guid in local:
            retired_present.append(guid)

    media = [m for m in (payload.get("media") or []) if m not in media_have]
    counts = {"updates": len(updates), "adds": len(adds), "skipped": len(skipped),
              "retired_present": len(retired_present), "media": len(media), "local_edits": local_edits}
    return {"updates": updates, "adds": adds, "skipped": skipped, "retired_present": retired_present,
            "media": media, "counts": counts}
