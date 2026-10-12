# Release builder and download (phase 5) — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "Cut release" turns pending changes into version N: a new `.apkg` that
students can import over their existing deck, a changelog on `/updates`, and a
download link.

**Architecture:** Copy the previous release's collection SQLite, patch it in place
(fields, sort field, checksum, mod time for changed notes; insert notes + cards for
new ones; delete retired ones), bump the collection mod time, zstd-compress it, and
zip it with the media manifest and every media file, streaming from the media store.
Note type ids, templates, CSS, deck ids and guids are never touched, so a student's
import updates in place. Builds run in a background thread; the page polls status.

**Tech Stack:** stdlib + `zstd` CLI in the container (added to the Dockerfile).

**Spec:** `docs/superpowers/specs/2026-10-01-online-deck-and-sync-design.md`, §6–7.

---

## File structure

| path | responsibility |
| --- | --- |
| `server/release_build.py` | **new** — patch a collection SQLite from revisions; write the .apkg; pure functions over paths and a DB connection |
| `server/tests/test_release_build.py` | **new** — synthetic baseline round-trips |
| `server/server.py` | `/releases` page: cut (draft), confirm (build), status; download routes; changelog grouping on `/updates` |
| `server/tests/test_release_routes.py` | **new** |
| `Dockerfile` | install `zstd` |

## Task 1: collection patcher
- [ ] Tests on a synthetic baseline (two note types, cloze + basic, 3 notes, cards):
      changed Text updates `flds`, `sfld`, `csum`, `mod`; `usn` set to -1; other
      notes byte-identical; new cloze note gets one card per cloze number in the
      deck of the baseline; retired note and its cards removed; `col.mod` bumped;
      note type / template / deck tables unchanged (compared row-for-row).
- [ ] Implement `patch_collection(src_path, dst_path, notes_by_guid, new_notes,
      retired_guids, now)`.
- [ ] Commit.

## Task 2: package writer
- [ ] Tests: `write_apkg(out, collection_path, media_iter)` produces a zip readable
      by `tools/apkg.py` with `meta` (version 3), `collection.anki21b` (zstd),
      a `collection.anki2` stub, a decodable media manifest, members `0..N-1`.
- [ ] Implement, streaming media; protobuf encoder for the manifest.
- [ ] Commit.

## Task 3: release lifecycle
- [ ] Tests: `POST /releases/cut` (login) with version creates a draft release,
      stamps pending revisions with that version, builds the changelog from them;
      `POST /releases/<v>/build` starts a build; status page shows
      building/published; `releases.current` only moves on publish; old built
      packages are removed when a new one is published; `/updates` groups published
      entries by release.
- [ ] Implement `/releases` (list, cut form, status), build in a thread with a
      `build_status` column, delete older `.apkg` files on publish.
- [ ] Commit.

## Task 4: downloads
- [ ] Tests: `/download/release/latest` needs login (staff); `/dl/<token>` serves
      the latest published package for a valid unexpired token and 404s otherwise;
      tokens are minted at `/releases` by an admin with an expiry; `Content-Length`
      and `Content-Disposition` set; served by streaming, not read into memory.
- [ ] Implement; `download_tokens` table. Stripe hook left as a documented TODO
      route (`/api/purchases/stripe`) that mints a token from a checkout session id
      once a secret is configured.
- [ ] Commit.

## Task 5: verify on production
- [ ] Cut release 1.1 from the pending edits Mario made (or a test edit), build,
      download, import into a throwaway Anki profile over Doctorine v2: Anki must
      report updated notes and **zero** new notes/duplicates; the edited card shows
      the new text; the scheduler's tags are untouched.
