"""Build a concrete update plan from a sync payload and the local collection.

Pure, so the rules are unit-tested without Anki:
- only Text and Extra are ever written; other fields are untouched
- tags change only as the release says: its added tags are added and its
  removed tags removed (case-insensitive), so tags the student made stay
- a note the student does not have is skipped (not added) unless it is in
  `added`; an added note the student already has is updated instead
- retired notes are reported, never deleted
- a local edit (current text != payload `before`) is flagged and overwritten;
  the UI says so before anything is written
- media to fetch = referenced media the student's folder lacks
"""

EDITABLE = ("Text", "Extra")


def merge_tags(local_tags, ops):
    """The student's tags after a release's tag changes. None when nothing changes."""
    local_tags = list(local_tags or [])
    remove = {t.lower() for t in (ops.get("remove") or []) if isinstance(t, str)}
    out = [t for t in local_tags if t.lower() not in remove]
    have = {t.lower() for t in out}
    for t in ops.get("add") or []:
        if isinstance(t, str) and t.strip() and t.lower() not in have:
            have.add(t.lower())
            out.append(t)
    return None if out == local_tags else out


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
        text_changes = any(cur.get(f, "") != v for f, v in new.items())
        new_tags = merge_tags(local[guid].get("tags"), ch["tags"]) if isinstance(ch.get("tags"), dict) else None
        if not text_changes and new_tags is None:
            continue
        before = ch.get("before") or {}
        edited = text_changes and any(cur.get(f, "") != before.get(f, cur.get(f, "")) for f in new)
        if edited:
            local_edits += 1
        u = {"guid": guid, "nid": local[guid]["nid"], "fields": new if text_changes else {}, "local_edit": edited}
        if new_tags is not None:
            u["tags"] = new_tags
        updates.append(u)

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


def version_summary(frm, to, behind):
    """('Your deck: 1.0', 'Newest: 1.3', '3 releases behind') for the update prompt."""
    mine = f"Your deck: {frm}" if frm else "Your deck: not recorded yet"
    newest = f"Newest: {to}"
    try:
        n = int(behind)
    except (TypeError, ValueError):
        n = 0
    gap = "" if n <= 0 else ("1 release behind" if n == 1 else f"{n} releases behind")
    return mine, newest, gap
