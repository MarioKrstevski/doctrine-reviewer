"""Releases, the baseline collection blob, and media files. No HTTP here.

Blobs live under BLOB_ROOT (default /data/blobs, the Railway volume):
  <root>/media/<sha256>              one file per distinct content
  <root>/releases/<version>/collection.sqlite   the deck's own collection DB

A release .apkg is later produced by copying that collection and patching
changed notes, so it is kept byte-for-byte: note type ids, templates, CSS,
deck ids and guids survive without re-encoding Anki's protobuf configs.
"""

import hashlib
import mimetypes
import os
import re

SQLITE_MAGIC = b"SQLite format 3\x00"
_SAFE_NAME = re.compile(r"^[^/\\\x00]{1,255}$")


def blob_root():
    return os.environ.get("BLOB_ROOT", "/data/blobs")


def safe_filename(name) -> bool:
    return bool(name) and _SAFE_NAME.match(name) is not None and name not in (".", "..")


def media_dir():
    return os.path.join(blob_root(), "media")


def sha_path(sha256):
    return os.path.join(media_dir(), sha256)


def store_media(conn, filename, data):
    """Write the bytes once (by sha256) and record the filename. Idempotent."""
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute("SELECT sha256 FROM media WHERE filename=?", (filename,)).fetchone()
    existing = row is not None and row["sha256"] == sha
    path = sha_path(sha)
    if not os.path.exists(path):
        os.makedirs(media_dir(), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    if not existing:
        conn.execute("INSERT OR REPLACE INTO media (filename, sha256, size) VALUES (?,?,?)",
                     (filename, sha, len(data)))
    return {"filename": filename, "sha256": sha, "size": len(data), "existing": existing}


def media_file(conn, filename):
    """(path, content_type) for a known filename, else None."""
    row = conn.execute("SELECT sha256 FROM media WHERE filename=?", (filename,)).fetchone()
    if row is None:
        return None
    path = sha_path(row["sha256"])
    if not os.path.isfile(path):
        return None
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return path, ctype


def release_dir(version):
    return os.path.join(blob_root(), "releases", version)


def store_baseline(conn, version, note_count, data, now):
    if not data.startswith(SQLITE_MAGIC):
        raise ValueError("not a SQLite database")
    d = release_dir(version)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "collection.sqlite")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    conn.execute(
        """INSERT OR REPLACE INTO releases
           (version, status, note_count, collection_path, created_at)
           VALUES (?,'published',?,?,?)""",
        (version, note_count, path, now))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_release', ?)",
                 (version,))
    return {"version": version, "path": path, "size": len(data)}


def current_release_key(deck_id=1):
    return "current_release" if deck_id == 1 else f"current_release:{deck_id}"


def current_release(conn, deck_id=1):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (current_release_key(deck_id),)).fetchone()
    return row["value"] if row else None


def list_releases(conn):
    return [dict(r) for r in conn.execute(
        "SELECT version, status, note_count, created_at FROM releases ORDER BY created_at DESC")]
