# Official-card gate and baseline deck import — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Only official Doctorine notes get a Suggest button and are accepted by the
server; the platform holds a complete copy of release 1.0 (notes, note types,
templates, the baseline collection file, media) from which later releases are built.

**Architecture:** The notes table already holds all 32,279 v2 notes keyed by guid. The
gate reads that table. For the baseline, the platform stores the deck's own
`collection.anki21b` (decompressed SQLite, ~57 MB) as a blob plus every media file,
uploaded from Mario's machine by a local tool using the pipeline key. A later release
is produced by copying that baseline and patching changed notes, so note type ids,
templates, deck ids and guids are preserved without re-encoding Anki's protobuf.

**Tech Stack:** stdlib Python (server, tool, add-on tests); `zstd` CLI on the
importing machine; SQLite; Railway volume at `/data` for blobs and media.

**Spec:** `docs/superpowers/specs/2026-10-01-online-deck-and-sync-design.md`, phases 1–2.

---

## File structure

| path | responsibility |
| --- | --- |
| `server/server.py` | routes: `/api/official`, media upload/serve, baseline upload, releases table |
| `server/releases.py` | **new** — release/baseline/media bookkeeping, blob paths, no HTTP |
| `server/tests/test_gate.py` | **new** — submit gate + official list |
| `server/tests/test_baseline.py` | **new** — baseline/media upload and serving |
| `tools/import_apkg.py` | **new** — local: unpack .apkg, upload baseline + media + notes |
| `addon/official.py` | **new** — pure: cache/refresh of the official guid set |
| `addon/identity.py` | gate becomes "guid in official set" with a fallback |
| `addon/__init__.py` | fetch the list in background; button uses it |

---

## Task 1: Server submit gate (official guids only)

**Files:** `server/tests/test_gate.py`, `server/server.py`

- [ ] Write failing tests: unknown guid → 404; known guid → 200; retired guid → 404;
      `GET /api/official` returns `{"release": "...", "count": N, "guids": [...]}` with
      `ETag`, and `If-None-Match` → 304.
- [ ] Implement: `notes.retired INTEGER DEFAULT 0` via `_ensure_columns`;
      `api_suggestion` checks `SELECT 1 FROM notes WHERE doctrine_id=? AND retired=0`
      before insert (restores the 404 with the original message); `/api/official`
      builds the list once per `(release, max(updated_at))` and caches it in memory.
- [ ] Delete the 34,704 legacy ObjectId rows (`deck='Doctrine'`) from production in
      the same deploy; they are not official.
- [ ] Commit.

## Task 2: Add-on official set

**Files:** `addon/official.py`, `addon/tests/test_official.py`, `addon/identity.py`,
`addon/tests/test_identity.py`, `addon/__init__.py`

- [ ] Tests for `official.py`: `load(dir)` → set or None; `refresh(dir, fetch, now)`
      stores guids+release+etag+fetched_at, honours 304, keeps old set on failure,
      refreshes at most once per 6h.
- [ ] Tests for identity: `is_official(guid, official_set)` — in set → True; not in
      set → False; set is None (never fetched) → True (server decides).
- [ ] Wire: on profile load, `mw.taskman.run_in_background(refresh)`;
      `on_reviewer_did_show_question` uses the cached set; DEBUG_MODE tooltip says
      "not in official release N".
- [ ] `py_compile`, smoke test, commit.

## Task 3: Releases, baseline blob and media on the server

**Files:** `server/releases.py`, `server/tests/test_baseline.py`, `server/server.py`

- [ ] Tests: `POST /api/baseline` (bearer) stores the uploaded SQLite as
      `/data/releases/<version>/collection.sqlite` and records release row; rejects
      without key; `POST /api/media/<name>` (bearer, raw body) stores to
      `/data/media/<sha256>` and records `(filename, sha256, size)`; duplicate upload
      is idempotent; `GET /media/<name>` serves with the right content type and 404
      for unknown names; `/api/releases` lists versions.
- [ ] Implement `releases.py`: `blob_root()` from `BLOB_ROOT` env (default
      `/data/blobs`), `store_media(name, data)`, `media_path(name)`,
      `create_release(version, note_count, collection_blob)`, `current_release()`.
- [ ] Rewrite `src="file.jpg"` to `/media/file.jpg` in `card_iframe` for both panes
      when the file is known; leave unknown refs alone.
- [ ] Commit.

## Task 4: Local import tool

**Files:** `tools/import_apkg.py`

- [ ] Reads an .apkg: unzip, `zstd -d` `collection.anki21b` → SQLite; decode the
      media manifest (zstd + protobuf: repeated `(index, filename, sha1)`) — parse with
      a minimal varint reader, no protobuf dependency.
- [ ] Uploads in this order, all with `Authorization: Bearer $PIPELINE_API_KEY`:
      1. notes via `/api/master/sync` in batches of 500 (existing; adds
         `note_type_id`, `anki_deck_id`), with `--release 1.0`
      2. each media file to `/api/media/<name>` (skips names the server already has,
         via `GET /api/media/manifest`)
      3. the SQLite collection to `/api/baseline?version=1.0`
- [ ] Prints a summary and exits non-zero on any failure. Resumable: re-running skips
      what is already uploaded.
- [ ] Run against `Doctorine v2.apkg`; verify counts on the server; commit.

## Task 5: Verify end to end

- [ ] `/api/official` count = 32,279; a suggestion from the DEV add-on on a v2 card is
      accepted; one on a student-created note is refused with the gate message.
- [ ] A reviewer item for a card with an image shows the image.
- [ ] Deploy; update CLAUDE.md (identity + gate + baseline sections).
