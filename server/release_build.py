"""Build a release from the previous release's collection file, without Anki.

patch_collection(): copy the SQLite, then for every revised note rewrite the
field blob, the sort field (first field stripped of HTML), the checksum
(first 8 hex of sha1 of that), mod and usn=-1; insert new notes with cards;
delete retired notes with their cards and add graves; bump col.mod.

Everything a student's import matches on -- note ids, guids, note type ids,
templates, deck ids -- is copied untouched. No protobuf config is re-encoded.
"""

import hashlib
import re
import shutil
import sqlite3
import time

_HTML = re.compile(r"<[^>]+>")
_CLOZE_N = re.compile(r"\{\{c(\d+)::")
SEP = "\x1f"


def _unicase(a, b):
    a, b = a.lower(), b.lower()
    return (a > b) - (a < b)


def connect(path):
    """Open an Anki collection: registers the `unicase` collation its schema
    declares on note type, deck and tag names, without which SQLite refuses
    to touch those tables."""
    conn = sqlite3.connect(path)
    conn.create_collation("unicase", _unicase)
    conn.row_factory = sqlite3.Row
    return conn


def strip_html(s):
    return _HTML.sub("", s or "")


def checksum(first_field):
    return int(hashlib.sha1(strip_html(first_field).encode("utf-8")).hexdigest()[:8], 16)


def _note_types(conn):
    types = {}
    for r in conn.execute("SELECT id, name FROM notetypes"):
        names = [f[0] for f in conn.execute("SELECT name FROM fields WHERE ntid=? ORDER BY ord", (r[0],))]
        tmpls = conn.execute("SELECT COUNT(*) FROM templates WHERE ntid=?", (r[0],)).fetchone()[0]
        types[r[1]] = {"id": r[0], "fields": names, "templates": tmpls,
                       "cloze": tmpls == 1 and any(n.lower() == "text" for n in names)}
    return types


def _main_deck(conn):
    r = conn.execute("SELECT did, COUNT(*) n FROM cards GROUP BY did ORDER BY n DESC LIMIT 1").fetchone()
    if r:
        return r[0]
    r = conn.execute("SELECT id FROM decks WHERE id != 1 ORDER BY id LIMIT 1").fetchone()
    return r[0] if r else 1


def _fresh_id(conn, table, start_ms):
    """Anki ids are epoch milliseconds; keep them unique and increasing."""
    cur = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0
    return max(cur + 1, start_ms)


def _tags_blob(tags):
    tags = [t for t in (tags or []) if t]
    return f" {' '.join(tags)} " if tags else ""


def patch_collection(src_path, dst_path, changed, new_notes, retired, now):
    """changed: {guid: {field: value}} (full editable values); new_notes:
    [{guid, note_type, fields, tags}]; retired: [guid]. Returns counts."""
    shutil.copyfile(src_path, dst_path)
    conn = connect(dst_path)
    try:
        types = _note_types(conn)
        by_id = {t["id"]: t for t in types.values()}
        out = {"changed": 0, "added": 0, "retired": 0}

        for guid, values in changed.items():
            n = conn.execute("SELECT id, mid, flds FROM notes WHERE guid=?", (guid,)).fetchone()
            if n is None:
                raise LookupError(f"note {guid!r} not in the collection")
            t = by_id[n["mid"]]
            for name in values:
                if name not in t["fields"]:
                    raise ValueError(f"note type {n['mid']} has no field {name!r}")
            flds = n["flds"].split(SEP)
            flds += [""] * (len(t["fields"]) - len(flds))
            for name, val in values.items():
                flds[t["fields"].index(name)] = val or ""
            conn.execute("UPDATE notes SET flds=?, sfld=?, csum=?, mod=?, usn=-1 WHERE id=?",
                         (SEP.join(flds), strip_html(flds[0]), checksum(flds[0]), int(now), n["id"]))
            out["changed"] += 1

        deck = _main_deck(conn)
        nid = _fresh_id(conn, "notes", int(now) * 1000)
        cid = _fresh_id(conn, "cards", int(now) * 1000)
        for note in new_notes:
            t = types.get(note.get("note_type") or "")
            if t is None:
                raise ValueError(f"unknown note type {note.get('note_type')!r}")
            flds = [(note.get("fields") or {}).get(name, "") or "" for name in t["fields"]]
            conn.execute("INSERT INTO notes (id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data) "
                         "VALUES (?,?,?,?,-1,?,?,?,?,0,'')",
                         (nid, note["guid"], t["id"], int(now), _tags_blob(note.get("tags")),
                          SEP.join(flds), strip_html(flds[0]), checksum(flds[0])))
            if t["cloze"]:
                ords = sorted({int(m) - 1 for m in _CLOZE_N.findall(flds[0])}) or [0]
            else:
                ords = list(range(t["templates"]))
            for o in ords:
                conn.execute("INSERT INTO cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps, "
                             "lapses, left, odue, odid, flags, data) VALUES (?,?,?,?,?,-1,0,0,?,0,0,0,0,0,0,0,0,'')",
                             (cid, nid, deck, o, int(now), nid))
                cid += 1
            nid += 1
            out["added"] += 1

        for guid in retired:
            n = conn.execute("SELECT id FROM notes WHERE guid=?", (guid,)).fetchone()
            if n is None:
                continue
            for c in conn.execute("SELECT id FROM cards WHERE nid=?", (n["id"],)).fetchall():
                conn.execute("INSERT INTO graves (oid, type, usn) VALUES (?, 0, -1)", (c["id"],))
            conn.execute("INSERT INTO graves (oid, type, usn) VALUES (?, 1, -1)", (n["id"],))
            conn.execute("DELETE FROM cards WHERE nid=?", (n["id"],))
            conn.execute("DELETE FROM notes WHERE id=?", (n["id"],))
            out["retired"] += 1

        conn.execute("UPDATE col SET mod=?", (int(now) * 1000,))
        conn.commit()
        return out
    finally:
        conn.close()
