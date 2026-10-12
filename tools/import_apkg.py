#!/usr/bin/env python3
"""Upload a release .apkg to the platform: notes, media, baseline collection.

Runs on a machine that has the file; nothing of this size goes through a
browser. Resumable: already-uploaded media is skipped, note sync is an
upsert, the baseline upload is idempotent.

    PIPELINE_API_KEY=... python3 tools/import_apkg.py "Doctorine v2.apkg" \\
        --version 1.0 --server https://doctrine-editor-production.up.railway.app

Steps, in order:
  1. notes  -> POST /api/master/sync   (batches of 500, keyed by guid)
  2. media  -> POST /api/media/<name>  (skips names already on the server)
  3. baseline collection -> POST /api/baseline?version=...&note_count=...
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apkg  # noqa: E402

BATCH = 500


def request(server, key, method, path, data=None, ctype="application/json", timeout=600):
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def read_notes(sqlite_bytes):
    """Yield dicts shaped like /api/master/sync expects, from a raw collection."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(sqlite_bytes); path = f.name
    try:
        c = sqlite3.connect(path); c.row_factory = sqlite3.Row
        fields = {}
        for r in c.execute("SELECT ntid, ord, name FROM fields ORDER BY ntid, ord"):
            fields.setdefault(r["ntid"], []).append(r["name"])
        types = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM notetypes")}
        decks = {r["id"]: r["name"].replace("\x1f", "::") for r in c.execute("SELECT id, name FROM decks")}
        deck_of = {}
        for r in c.execute("SELECT nid, MIN(did) AS did FROM cards GROUP BY nid"):
            deck_of[r["nid"]] = decks.get(r["did"], "")
        for r in c.execute("SELECT id, guid, mid, tags, flds FROM notes"):
            names = fields.get(r["mid"], [])
            values = r["flds"].split("\x1f")
            yield {
                "doctrine_id": r["guid"],
                "doctorine_id": "",
                "guid": r["guid"],
                "anki_note_id": r["id"],
                "note_type": types.get(r["mid"], ""),
                "deck": deck_of.get(r["id"], ""),
                "fields": {n: (values[i] if i < len(values) else "") for i, n in enumerate(names)},
                "tags": [t for t in r["tags"].split() if t],
            }
        c.close()
    finally:
        os.unlink(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("apkg")
    ap.add_argument("--version", required=True)
    ap.add_argument("--server", default="https://doctrine-editor-production.up.railway.app")
    ap.add_argument("--skip-notes", action="store_true")
    ap.add_argument("--skip-media", action="store_true")
    ap.add_argument("--skip-baseline", action="store_true")
    args = ap.parse_args()
    key = os.environ.get("PIPELINE_API_KEY", "").strip()
    if not key:
        sys.exit("PIPELINE_API_KEY is not set")

    pkg = apkg.Package(args.apkg)
    sqlite_bytes = pkg.collection_sqlite()
    t0 = time.time()

    # 1. notes
    notes = list(read_notes(sqlite_bytes))
    print(f"notes in package: {len(notes)}")
    if not args.skip_notes:
        reg = upd = 0
        for i in range(0, len(notes), BATCH):
            batch = notes[i:i + BATCH]
            out = request(args.server, key, "POST", "/api/master/sync",
                          json.dumps({"notes": batch}).encode("utf-8"))
            reg += out.get("registered", 0); upd += out.get("updated", 0)
            print(f"  notes {min(i + BATCH, len(notes))}/{len(notes)}  (registered={reg} updated={upd})", end="\r")
        print(f"\n  notes done: registered={reg} updated={upd}")

    # 2. media
    names = pkg.media_names()
    print(f"media in package: {len(names)}")
    if not args.skip_media:
        have = set(request(args.server, key, "GET", "/api/media/manifest")["files"])
        todo = [(m, n) for m, n in names.items() if n not in have]
        print(f"  already on server: {len(names) - len(todo)}, to upload: {len(todo)}")
        sent = 0; bytes_sent = 0; failed = []
        for member, name in todo:
            data = pkg.media_bytes(member)
            path = "/api/media/" + urllib.parse.quote(name, safe="")
            for attempt in range(3):
                try:
                    request(args.server, key, "POST", path, data, "application/octet-stream", timeout=300)
                    break
                except (urllib.error.URLError, OSError) as e:
                    if attempt == 2:
                        failed.append((name, str(e)))
                    else:
                        time.sleep(2 * (attempt + 1))
            sent += 1; bytes_sent += len(data)
            if sent % 25 == 0 or sent == len(todo):
                print(f"  media {sent}/{len(todo)}  {bytes_sent/1e6:.0f} MB  {time.time()-t0:.0f}s", end="\r")
        print(f"\n  media done: uploaded={sent - len(failed)} failed={len(failed)}")
        for name, err in failed[:10]:
            print("   FAILED", name, err)
        if failed:
            sys.exit(1)

    # 3. baseline
    if not args.skip_baseline:
        q = urllib.parse.urlencode({"version": args.version, "note_count": len(notes)})
        out = request(args.server, key, "POST", f"/api/baseline?{q}", sqlite_bytes,
                      "application/octet-stream", timeout=1800)
        print(f"baseline stored: {out}")
    pkg.close()
    print(f"all done in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
