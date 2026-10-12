"""Edits to the online deck: apply to `notes`, record a revision. No HTTP.

`notes` always holds the current text, so the reviewer's master pane and the
staleness hash keep working. Each changed field writes one `revisions` row
with before/after, who, source ("suggestion:<id>", "upload:<id>", "staff")
and the release it shipped in (NULL while pending). Note type, tags and the
other fields are never edited here: tags are the scheduler's, and a note
type change is a one-off event, not an edit.
"""

import json
import secrets

EDITABLE_FIELDS = ("Text", "Extra")
BASE91 = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
          "!#$%&()*+,-./:;<=>?@[]^_`{|}~")


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


def new_guid(conn):
    """A 10-character base91 guid, the shape Anki itself generates."""
    while True:
        g = "".join(secrets.choice(BASE91) for _ in range(10))
        if conn.execute("SELECT 1 FROM notes WHERE doctrine_id=?", (g,)).fetchone() is None:
            return g


def add_note(conn, note_type, fields, tags, user_id, deck="Doctorine v2"):
    """Create an official note. Returns its guid."""
    fields = {k: (v if isinstance(v, str) else "") for k, v in (fields or {}).items()}
    if not fields.get("Text", "").strip():
        raise ValueError("Text is required")
    guid = new_guid(conn)
    conn.execute(
        """INSERT INTO notes (doctrine_id, guid, doctorine_id, note_type, deck, fields_json,
                              tags_json, content_hash, version, updated_at, retired)
           VALUES (?,?,?,?,?,?,?,?,1,?,0)""",
        (guid, guid, "", note_type, deck, json.dumps(fields), json.dumps(list(tags or [])),
         _content_hash(fields), _now()))
    conn.execute(
        """INSERT INTO revisions (guid, field, before, after, user_id, source, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (guid, "added", None, json.dumps(fields), user_id, "staff", _now()))
    return guid


def pending(conn):
    """Unreleased revisions grouped by note, oldest note first."""
    rows = conn.execute(
        """SELECT r.*, u.username FROM revisions r LEFT JOIN users u ON u.id = r.user_id
           WHERE r.release IS NULL ORDER BY r.id""").fetchall()
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
