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


# ---------------------------------------------------------------- .apkg writer

import hashlib as _hashlib
import os as _os
import shutil as _shutil
import subprocess as _subprocess
import tempfile as _tempfile
import zipfile as _zipfile

# Anki's newer package format: meta = PackageMetadata{version=3} (field 1,
# varint 3); collection.anki21b = zstd(SQLite); media = zstd(MediaEntries);
# a legacy collection.anki2 stub so old clients show a clear error.
META_V3 = b"\x08\x03"
LEGACY_STUB_SQL = (
    "CREATE TABLE col (id integer primary key, crt integer, mod integer, scm integer, "
    "ver integer, dty integer, usn integer, ls integer, conf text, models text, decks text, "
    "dconf text, tags text);"
    "INSERT INTO col VALUES (1, 0, 0, 0, 11, 0, 0, 0, '{}', '{}', '{}', '{}', '{}');"
    "CREATE TABLE notes (id integer primary key, guid text, mid integer, mod integer, usn integer, "
    "tags text, flds text, sfld text, csum integer, flags integer, data text);"
    "INSERT INTO notes VALUES (1, 'legacy', 0, 0, 0, '', 'This deck requires a newer Anki version. "
    "Please update to Anki 2.1.50 or later.', '', 0, 0, '');")


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b); return bytes(out)


def _field(no, wire, payload):
    return _varint((no << 3) | wire) + (_varint(len(payload)) + payload if wire == 2 else payload)


def encode_media_manifest(entries):
    """entries: [(name, size, sha1_bytes)] in zip-member order. Returns zstd bytes."""
    body = b""
    for name, size, sha1 in entries:
        entry = _field(1, 2, name.encode("utf-8")) + _field(2, 0, _varint(size)) + _field(3, 2, sha1)
        body += _field(1, 2, entry)
    return zstd_compress(body)


def zstd_compress(data, level=3):
    if _shutil.which("zstd") is None:
        raise RuntimeError("zstd CLI not found")
    return _subprocess.run(["zstd", "-q", f"-{level}", "-c"], input=data, capture_output=True, check=True).stdout


def _fill_zip(z, collection_path, media, level):
    """Write the package members into open zip `z`. media items are
    (filename, path_on_disk) or (filename, bytes)."""
    entries = []
    nbytes = 0
    z.writestr("meta", META_V3)
    with open(collection_path, "rb") as fh:
        z.writestr("collection.anki21b", zstd_compress(fh.read(), level))
    with _tempfile.NamedTemporaryFile(suffix=".anki2", delete=False) as stub:
        stub_path = stub.name
    try:
        c = sqlite3.connect(stub_path); c.executescript(LEGACY_STUB_SQL); c.commit(); c.close()
        z.write(stub_path, "collection.anki2")
    finally:
        _os.unlink(stub_path)
    for i, (name, src) in enumerate(media):
        h = _hashlib.sha1()
        size = 0
        if isinstance(src, (bytes, bytearray)):
            with z.open(str(i), "w", force_zip64=True) as dst:
                dst.write(src)
            h.update(src); size = len(src)
        else:
            with open(src, "rb") as fh, z.open(str(i), "w", force_zip64=True) as dst:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk); size += len(chunk); dst.write(chunk)
        entries.append((name, size, h.digest()))
        nbytes += size
    z.writestr("media", encode_media_manifest(entries))
    return {"media": len(entries), "bytes": nbytes + _os.path.getsize(collection_path)}


def write_apkg(out_path, collection_path, media, level=3):
    """media: iterable of (filename, path_on_disk). Streams every file into
    the zip, so a 2 GB package never sits in memory. Returns counts."""
    tmp = out_path + ".tmp"
    with _zipfile.ZipFile(tmp, "w", _zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        info = _fill_zip(z, collection_path, media, level)
    _os.replace(tmp, out_path)
    return info


class _Sink:
    """Write-only, non-seekable file object; zipfile then uses data descriptors."""
    def __init__(self, write):
        self._write = write

    def write(self, data):
        self._write(data)
        return len(data)

    def flush(self):
        pass


def license_member(token, version):
    import json as _json
    return ("_doctrine_license.json",
            _json.dumps({"license": token, "release": version}).encode())


def stream_apkg(write, collection_path, media, license=None, level=3):
    """Build the package straight into write(bytes); nothing touches disk.
    Media is already compressed, so members are stored. `license` is an
    optional (filename, bytes) extra media member (see license_member)."""
    items = list(media)
    if license:
        items.append(license)
    sink = _Sink(write)
    with _zipfile.ZipFile(sink, "w", _zipfile.ZIP_STORED, allowZip64=True) as z:
        return _fill_zip(z, collection_path, items, level)
