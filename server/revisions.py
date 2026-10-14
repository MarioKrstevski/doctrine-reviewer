"""Edits to the online deck: apply to `notes`, record a revision. No HTTP.

`notes` always holds the current text, so the reviewer's master pane and the
staleness hash keep working. Each changed field writes one `revisions` row
with before/after, who, source ("suggestion:<id>", "upload:<id>", "staff")
and the release it shipped in (NULL while pending). Tags are edited too, as one
"tags" revision holding the before and after lists as JSON. The other fields
and the note type are never edited here: a note type change is a one-off
event, not an edit.
"""

import json
import secrets

EDITABLE_FIELDS = ("Text", "Extra")
# Anki's own guids use base91, which includes < > & " ` and other characters that
# need escaping in HTML and URLs. Cards created on the platform use letters and
# digits only. Existing guids keep their characters: they are what matches each card
# in students' collections, so they are escaped wherever they are shown instead.
GUID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _content_hash(fields):
    import server  # late import: server imports this module
    return server.content_hash(fields)


def _now():
    import server
    return server.now()


def apply_edit(conn, guid, new_values, user_id, source):
    """Set editable fields on a note. Returns {"changed": [field, ...]}.

    Raises ValueError for a non-editable field, LookupError for an unknown
    or retired guid. Identical values change nothing and record nothing.
    """
    bad = [f for f in new_values if f not in EDITABLE_FIELDS]
    if bad:
        raise ValueError(f"not editable: {', '.join(bad)}")
    row = conn.execute(
        "SELECT fields_json, version FROM notes WHERE doctrine_id=? AND retired=0",
        (guid,)).fetchone()
    if row is None:
        raise LookupError(f"unknown note {guid!r}")
    fields = json.loads(row["fields_json"] or "{}")
    changed = []
    for name, after in new_values.items():
        after = after if isinstance(after, str) else ""
        before = fields.get(name, "")
        if after == before:
            continue
        fields[name] = after
        conn.execute(
            """INSERT INTO revisions (guid, field, before, after, user_id, source, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (guid, name, before, after, user_id, source, _now()))
        changed.append(name)
    if changed:
        conn.execute(
            """UPDATE notes SET fields_json=?, content_hash=?, version=version+1, updated_at=?
               WHERE doctrine_id=?""",
            (json.dumps(fields), _content_hash(fields), _now(), guid))
    return {"changed": changed}


def clean_tags(tags):
    """Anki tags: no spaces inside a tag, no duplicates (case-insensitive, first kept), order kept."""
    if isinstance(tags, str):
        tags = tags.split()
    out, seen = [], set()
    for t in tags or []:
        if not isinstance(t, str):
            continue
        for part in t.split():
            if part.lower() not in seen:
                seen.add(part.lower())
                out.append(part)
    return out


def apply_tags(conn, guid, tags, user_id, source):
    """Replace a note's tags. Records one "tags" revision with the before and after
    lists as JSON. Returns True when something changed."""
    row = conn.execute("SELECT tags_json FROM notes WHERE doctrine_id=? AND retired=0", (guid,)).fetchone()
    if row is None:
        raise LookupError(f"unknown note {guid!r}")
    before = clean_tags(json.loads(row["tags_json"] or "[]"))
    after = clean_tags(tags)
    if {t.lower() for t in after} == {t.lower() for t in before}:
        return False   # Anki tags ignore order and case
    conn.execute(
        """INSERT INTO revisions (guid, field, before, after, user_id, source, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (guid, "tags", json.dumps(before), json.dumps(after), user_id, source, _now()))
    conn.execute("UPDATE notes SET tags_json=?, version=version+1, updated_at=? WHERE doctrine_id=?",
                 (json.dumps(after), _now(), guid))
    return True


def tags_at(conn, guid, included, current=None):
    """A note's tags once the releases in `included` shipped, like text_at."""
    rows = conn.execute("SELECT release, before, after FROM revisions WHERE guid=? AND field='tags' ORDER BY id",
                        (guid,)).fetchall()
    done = [r for r in rows if r["release"] in included]
    if done:
        return json.loads(done[-1]["after"] or "[]")
    if rows:
        return json.loads(rows[0]["before"] or "[]")
    return list(current or [])


def retire_note(conn, guid, user_id, source):
    """Take a note out of the deck at the next release. It stays in the database."""
    row = conn.execute("SELECT 1 FROM notes WHERE doctrine_id=? AND retired=0", (guid,)).fetchone()
    if row is None:
        raise LookupError(f"unknown note {guid!r}")
    conn.execute("UPDATE notes SET retired=1, version=version+1, updated_at=? WHERE doctrine_id=?", (_now(), guid))
    conn.execute(
        """INSERT INTO revisions (guid, field, before, after, user_id, source, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (guid, "retired", "0", "1", user_id, source, _now()))


def revert_pending(conn, guid):
    """Undo every unreleased change to one note: field and tag edits, a removal,
    or the note's creation. Returns what happened: "reverted", "deleted" or None."""
    revs = conn.execute("SELECT * FROM revisions WHERE guid=? AND release IS NULL ORDER BY id", (guid,)).fetchall()
    if not revs:
        return None
    if any(r["field"] == "added" for r in revs):
        conn.execute("DELETE FROM revisions WHERE guid=?", (guid,))
        conn.execute("DELETE FROM notes WHERE doctrine_id=?", (guid,))
        return "deleted"
    row = conn.execute("SELECT fields_json, tags_json FROM notes WHERE doctrine_id=?", (guid,)).fetchone()
    if row is None:
        return None
    fields = json.loads(row["fields_json"] or "{}")
    tags = json.loads(row["tags_json"] or "[]")
    first = {}
    for r in revs:
        first.setdefault(r["field"], r["before"])
    for name in EDITABLE_FIELDS:
        if name in first:
            fields[name] = first[name] or ""
    if "tags" in first:
        tags = json.loads(first["tags"] or "[]")
    conn.execute(
        """UPDATE notes SET fields_json=?, tags_json=?, content_hash=?, retired=0,
                  version=version+1, updated_at=? WHERE doctrine_id=?""",
        (json.dumps(fields), json.dumps(tags), _content_hash(fields), _now(), guid))
    conn.execute("DELETE FROM revisions WHERE guid=? AND release IS NULL", (guid,))
    return "reverted"


def new_guid(conn):
    """A 10-character base91 guid, the shape Anki itself generates."""
    while True:
        g = "".join(secrets.choice(GUID_ALPHABET) for _ in range(10))
        if conn.execute("SELECT 1 FROM notes WHERE doctrine_id=?", (g,)).fetchone() is None:
            return g


def add_note(conn, note_type, fields, tags, user_id, deck="Doctorine v2", platform_deck=1):
    """Create an official note. Returns its guid."""
    fields = {k: (v if isinstance(v, str) else "") for k, v in (fields or {}).items()}
    if not fields.get("Text", "").strip():
        raise ValueError("Text is required")
    guid = new_guid(conn)
    conn.execute(
        """INSERT INTO notes (doctrine_id, guid, doctorine_id, note_type, deck, fields_json,
                              tags_json, content_hash, version, updated_at, retired, platform_deck)
           VALUES (?,?,?,?,?,?,?,?,1,?,0,?)""",
        (guid, guid, "", note_type, deck, json.dumps(fields), json.dumps(list(tags or [])),
         _content_hash(fields), _now(), platform_deck))
    conn.execute(
        """INSERT INTO revisions (guid, field, before, after, user_id, source, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (guid, "added", None, json.dumps(fields), user_id, "staff", _now()))
    return guid


def pending(conn, deck_id=None):
    """Unreleased revisions grouped by note, oldest note first. deck_id limits to one deck."""
    rows = conn.execute(
        """SELECT r.*, u.username FROM revisions r LEFT JOIN users u ON u.id = r.user_id
           LEFT JOIN notes n ON n.doctrine_id = r.guid
           WHERE r.release IS NULL AND (? IS NULL OR COALESCE(n.platform_deck,1) = ?)
           ORDER BY r.id""", (deck_id, deck_id)).fetchall()
    groups, order = {}, []
    for r in rows:
        if r["guid"] not in groups:
            groups[r["guid"]] = {"guid": r["guid"], "revisions": []}
            order.append(r["guid"])
        groups[r["guid"]]["revisions"].append(dict(r))
    return [groups[g] for g in order]


def stamp_release(conn, version):
    """Mark every pending revision as shipped in `version`. Returns the count."""
    return conn.execute("UPDATE revisions SET release=? WHERE release IS NULL", (version,)).rowcount


KIND_LABELS = {
    "typo": "Typo / spelling",
    "incorrect": "Incorrect information",
    "confusing": "Confusing / unclear",
    "media": "Image / video",
    "other": "Other suggestions",
    "staff": "Direct edits",
    "upload": "Re-upload",
    "tags": "Tags",
    "added": "New notes",
    "retired": "Retired notes",
}
KIND_ORDER = ["incorrect", "typo", "confusing", "tags", "media", "other", "staff", "upload", "added", "retired"]


def kind_of(conn, rev):
    """Group key for a revision, derived from its source."""
    if rev["field"] in ("added", "retired"):
        return rev["field"]
    src = rev["source"] or ""
    if src.startswith("suggestion:"):
        row = conn.execute("SELECT suggestion_type FROM suggestions WHERE id=?",
                           (src.split(":", 1)[1],)).fetchone()
        return (row["suggestion_type"] if row and row["suggestion_type"] in KIND_LABELS else "other")
    if src.startswith("upload:"):
        return "upload"
    return "staff"


def pending_by_kind(conn, only_guids=None, deck_id=None):
    """[{kind, label, notes: [{guid, revisions, sources, users}]}] in KIND_ORDER."""
    groups = {}
    for g in pending(conn, deck_id):
        if only_guids is not None and g["guid"] not in only_guids:
            continue
        kinds = [kind_of(conn, rv) for rv in g["revisions"]]
        # a note touched by several sources is filed under its first revision's kind
        kind = kinds[0]
        entry = groups.setdefault(kind, {"kind": kind, "label": KIND_LABELS.get(kind, kind), "notes": []})
        entry["notes"].append({"guid": g["guid"], "revisions": g["revisions"],
                               "sources": sorted({rv["source"] or "" for rv in g["revisions"]}),
                               "users": sorted({rv["username"] or "?" for rv in g["revisions"]})})
    return [groups[k] for k in KIND_ORDER if k in groups] + [v for k, v in groups.items() if k not in KIND_ORDER]


def stamp_guids(conn, version, guids):
    """Stamp pending revisions of these notes only. Returns the count."""
    if not guids:
        return 0
    marks = ",".join("?" * len(guids))
    return conn.execute(
        f"UPDATE revisions SET release=? WHERE release IS NULL AND guid IN ({marks})",
        [version, *guids]).rowcount


def unstamp(conn, version):
    return conn.execute("UPDATE revisions SET release=NULL WHERE release=?", (version,)).rowcount


def text_at(conn, guid, included, row_fields=None):
    """Editable fields of `guid` as they were once the releases in `included`
    (a set of version strings) shipped. Edits not stamped to one of them, whether
    still pending or cut later, are ignored. `row_fields` is the note's current
    fields, used for fields that have no revision at all."""
    out = {}
    for field in EDITABLE_FIELDS:
        rows = conn.execute(
            "SELECT release, before, after FROM revisions WHERE guid=? AND field=? ORDER BY id",
            (guid, field)).fetchall()
        done = [r for r in rows if r["release"] in included]
        if done:
            out[field] = done[-1]["after"]
        elif rows:
            out[field] = rows[0]["before"]
        elif row_fields is not None and field in row_fields:
            out[field] = row_fields[field]
    return out
