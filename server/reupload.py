"""Re-upload with diff: compare a deck file against the online deck, then
apply only what staff confirmed. No HTTP here.

Inputs are {guid: {"fields": {...}, "tags": [...]}} maps. Rules:
- editable fields (Text, Extra) that differ are "changed" and can be applied
- other field changes and tag changes are reported, never applied (tags are
  the scheduler's; other fields are not edited on the platform)
- notes with pending platform revisions are "conflicts": shown side by side,
  staff choose "file" or "platform" per note
- notes missing from the file are never deleted; staff may retire them
- if most guids in the file are unknown, the export broke the ids: refuse
"""

import json

from revisions import EDITABLE_FIELDS, add_note, apply_edit


class IdMismatch(ValueError):
    pass


def snapshot(conn, deck_id=1):
    """One deck as a comparison map (active notes only)."""
    out = {}
    for r in conn.execute("SELECT doctrine_id, fields_json, tags_json FROM notes "
                          "WHERE retired=0 AND COALESCE(platform_deck,1)=?", (deck_id,)):
        out[r["doctrine_id"]] = {"fields": json.loads(r["fields_json"] or "{}"),
                                 "tags": json.loads(r["tags_json"] or "[]")}
    return out


def pending_guids(conn):
    return {r["guid"] for r in conn.execute("SELECT DISTINCT guid FROM revisions WHERE release IS NULL")}


def compare(current, incoming, pending_guids=frozenset(), max_unknown_ratio=0.5):
    """Classify every note in `incoming` against `current`."""
    if not incoming:
        raise ValueError("the file contains no notes")
    unknown = [g for g in incoming if g not in current]
    if current and len(unknown) / len(incoming) > max_unknown_ratio:
        raise IdMismatch(
            f"{len(unknown)} of {len(incoming)} note ids are unknown. The export "
            "did not preserve note ids; importing it would duplicate every card for "
            "students. Nothing was applied.")

    changed, conflicts, tag_changes, other, new = {}, {}, {}, {}, {}
    unchanged = 0
    for guid, inc in incoming.items():
        if guid not in current:
            new[guid] = inc
            continue
        cur = current[guid]
        diffs = {}
        others = []
        for name, val in (inc.get("fields") or {}).items():
            before = (cur.get("fields") or {}).get(name, "")
            if val == before:
                continue
            if name in EDITABLE_FIELDS:
                diffs[name] = (before, val)
            else:
                others.append(name)
        if others:
            other[guid] = others
        if list(cur.get("tags") or []) != list(inc.get("tags") or []):
            tag_changes[guid] = (list(cur.get("tags") or []), list(inc.get("tags") or []))
        if diffs:
            if guid in pending_guids:
                conflicts[guid] = diffs
            else:
                changed[guid] = {"fields": diffs}
        else:
            unchanged += 1
    missing = [g for g in current if g not in incoming]
    return {"new": new, "changed": changed, "conflicts": conflicts, "missing": missing,
            "tag_changes": tag_changes, "other_field_changes": other, "unchanged": unchanged}


def apply(conn, diff, incoming, decisions, user_id, upload_id, deck_id=1):
    """Apply confirmed parts of a diff.

    decisions: {guid: "file" | "platform" | "add" | "retire"}. Changed notes
    default to "file"; conflicts and missing notes need an explicit decision;
    new notes need "add". Everything else is left alone.
    """
    source = f"upload:{upload_id}"
    out = {"changed": 0, "added": 0, "retired": 0, "kept": 0}
    for guid, info in list(diff["changed"].items()) + list(diff["conflicts"].items()):
        fields = info["fields"] if "fields" in info else info
        choice = decisions.get(guid, "file" if guid in diff["changed"] else "platform")
        if choice == "file":
            apply_edit(conn, guid, {name: after for name, (_, after) in fields.items()}, user_id, source)
            out["changed"] += 1
        else:
            out["kept"] += 1
    for guid in diff["new"]:
        if decisions.get(guid) == "add":
            inc = incoming[guid]
            fields = {k: v for k, v in (inc.get("fields") or {}).items()}
            row = conn.execute("SELECT note_type FROM notes WHERE COALESCE(platform_deck,1)=? LIMIT 1", (deck_id,)).fetchone()
            note_type = inc.get("note_type") or (row["note_type"] if row else "AnKingOverhaul++++++")
            _add_with_guid(conn, guid, note_type, fields, inc.get("tags") or [], user_id, source, deck_id)
            out["added"] += 1
    for guid in diff["missing"]:
        if decisions.get(guid) == "retire":
            conn.execute("UPDATE notes SET retired=1 WHERE doctrine_id=?", (guid,))
            conn.execute("INSERT INTO revisions (guid, field, before, after, user_id, source, created_at) "
                         "VALUES (?,?,?,?,?,?,datetime('now'))", (guid, "retired", "0", "1", user_id, source))
            out["retired"] += 1
    return out


def _add_with_guid(conn, guid, note_type, fields, tags, user_id, source, deck_id=1):
    """A new note whose guid comes from the file (so students' future imports match)."""
    import server
    conn.execute(
        """INSERT INTO notes (doctrine_id, guid, doctorine_id, note_type, deck, fields_json,
                              tags_json, content_hash, version, updated_at, retired, platform_deck)
           VALUES (?,?,?,?,?,?,?,?,1,?,0,?)""",
        (guid, guid, "", note_type, "Doctorine v2", json.dumps(fields), json.dumps(list(tags)),
         server.content_hash(fields), server.now(), deck_id))
    conn.execute(
        "INSERT INTO revisions (guid, field, before, after, user_id, source, created_at) VALUES (?,?,?,?,?,?,?)",
        (guid, "added", None, json.dumps(fields), user_id, source, server.now()))
