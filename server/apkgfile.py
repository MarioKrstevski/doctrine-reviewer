"""Read an Anki .apkg without the anki package: collection + media manifest.

Newer packages (schema 18) carry collection.anki21b (zstd) and a media
manifest that is zstd-compressed protobuf:
    MediaEntries { repeated MediaEntry entries = 1 }
    MediaEntry   { string name = 1; uint32 size = 2; bytes sha1 = 3 }
Entry i corresponds to the zip member named str(i). Older packages carry a
plain JSON manifest {"0": "name", ...}. Both are handled. zstd is done via
the `zstd` CLI so this stays dependency-free.
"""

import json
import os
import shutil
import subprocess
import tempfile
import zipfile


def zstd_decompress(data: bytes) -> bytes:
    if shutil.which("zstd") is None:
        raise RuntimeError("zstd CLI not found (brew install zstd)")
    return subprocess.run(["zstd", "-d", "-c"], input=data, capture_output=True, check=True).stdout


def _varint(buf, i):
    shift = 0; val = 0
    while True:
        b = buf[i]; i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _fields(buf):
    """Yield (field_no, wire_type, value) over a protobuf message."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 2:
            ln, i = _varint(buf, i); v = buf[i:i + ln]; i += ln
        elif wt == 1:
            v = buf[i:i + 8]; i += 8
        elif wt == 5:
            v = buf[i:i + 4]; i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield fno, wt, v


def decode_media_manifest(raw: bytes) -> dict:
    """{zip member name: real filename}"""
    if raw[:1] in (b"{", b" ") or raw.strip().startswith(b"{"):
        return {k: v for k, v in json.loads(raw.decode("utf-8")).items()}
    buf = zstd_decompress(raw)
    names = {}
    idx = 0
    for fno, wt, v in _fields(buf):
        if fno == 1 and wt == 2:
            name = ""
            for f2, w2, v2 in _fields(v):
                if f2 == 1 and w2 == 2:
                    name = v2.decode("utf-8")
            names[str(idx)] = name
            idx += 1
    return names


class Package:
    def __init__(self, path):
        self.path = path
        self.zip = zipfile.ZipFile(path)
        self._tmp = None

    def collection_sqlite(self) -> bytes:
        names = self.zip.namelist()
        if "collection.anki21b" in names:
            return zstd_decompress(self.zip.read("collection.anki21b"))
        for n in ("collection.anki21", "collection.anki2"):
            if n in names:
                return self.zip.read(n)
        raise ValueError("no collection in package")

    def collection_to_file(self, out_path):
        """Write the raw collection SQLite to out_path without holding it in memory."""
        names = self.zip.namelist()
        if "collection.anki21b" in names:
            if shutil.which("zstd") is None:
                raise RuntimeError("zstd CLI not found")
            with self.zip.open("collection.anki21b") as src, open(out_path, "wb") as dst:
                proc = subprocess.Popen(["zstd", "-d", "-q", "-c"], stdin=subprocess.PIPE, stdout=dst)
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    proc.stdin.write(chunk)
                proc.stdin.close()
                if proc.wait() != 0:
                    raise RuntimeError("could not decompress the collection")
            return out_path
        for n in ("collection.anki21", "collection.anki2"):
            if n in names:
                with self.zip.open(n) as src, open(out_path, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                return out_path
        raise ValueError("no collection in package")

    def media_names(self) -> dict:
        return decode_media_manifest(self.zip.read("media"))

    def media_bytes(self, member) -> bytes:
        return self.zip.read(member)

    def close(self):
        self.zip.close()


# ---- notes + media classification used by the Deck page ----

import hashlib
import re
import sqlite3


def read_notes(sqlite_bytes):
    """Yield dicts shaped like /api/master/sync expects, from a raw collection."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(sqlite_bytes); path = f.name
    yield from read_notes_file(path, remove=True)


def read_notes_file(path, remove=False):
    """Same as read_notes, reading a collection file on disk row by row."""
    try:
        c = sqlite3.connect(path); c.row_factory = sqlite3.Row
        fields = {}
        for r in c.execute("SELECT ntid, ord, name FROM fields ORDER BY ntid, ord"):
            fields.setdefault(r["ntid"], []).append(r["name"])
        types = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM notetypes")}
        for r in c.execute("SELECT id, guid, mid, tags, flds FROM notes"):
            names = fields.get(r["mid"], [])
            values = r["flds"].split("\x1f")
            yield {"doctrine_id": r["guid"], "guid": r["guid"], "anki_note_id": r["id"],
                   "note_type": types.get(r["mid"], ""),
                   "fields": {n: (values[i] if i < len(values) else "") for i, n in enumerate(names)},
                   "tags": [t for t in r["tags"].split() if t]}
        c.close()
    finally:
        if remove:
            os.unlink(path)


_REF = re.compile(r'''(src=["']|\[sound:)([^"'\]]+)''')


def rename_refs(value, renames):
    """Point src= / [sound:] references at renamed media files."""
    if not renames or not value:
        return value
    return _REF.sub(lambda m: m.group(1) + renames.get(m.group(2), m.group(2)), value)


def renamed(name, sha256_hex):
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name}-{sha256_hex[:8]}"
    return f"{stem}-{sha256_hex[:8]}.{ext}"


def sha256_member(pkg, member):
    h = hashlib.sha256()
    with pkg.zip.open(member) as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
