# Doctrine Editor — Richer Payload (P3) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every suggestion carries enough trace data to find, reproduce and
route it: tags, which card of the note, which deck, which install, which
versions.

**Architecture:** A pure `addon/payload.py` assembles the trace dict from
plain values so the logic is testable without Anki; `__init__.py` only
gathers the values. The server adds nullable columns via the existing
"add column if missing" migration pattern, stores them verbatim, and
renders tags as chips plus a collapsible Trace block in the queue.

**Tech Stack:** stdlib only, both sides.

**Covers spec phase:** P3. `note_guid` from the spec is dropped: the guid
*is* `doctrine_id` now.

---

## Task 1: Trace assembly (add-on, pure)

**Files:** Create `addon/payload.py`, `addon/tests/test_payload.py`

- [ ] Tests: template name for a cloze type (one template, any ord) and a
      standard type (template per ord); `original_deck_id` is `None` when
      `odid == 0`; tags pass through as a list; unknown ord does not raise.
- [ ] Implement `trace(...) -> dict`.
- [ ] Commit.

## Task 2: Server columns and storage

**Files:** Modify `server/server.py`; create `server/tests/test_trace.py`

- [ ] Tests: a suggestion posted with the new keys is stored; one posted
      without them (older add-on) still succeeds with NULLs; migration adds
      the columns to an existing table.
- [ ] Generalise the PRAGMA-guarded ALTER into `_ensure_columns(conn,
      table, {name: type})`; add the ten columns; store in `api_suggestion`.
- [ ] Commit.

## Task 3: Reviewer rendering

**Files:** Modify `server/server.py`; extend `server/tests/test_trace.py`

- [ ] Tests: tags render as chips; the Trace block contains card id,
      deck id, install id (shortened) and versions; absent values do not
      render as the string "None".
- [ ] Implement `tag_chips()` and `trace_block()`; wire into the item meta.
- [ ] Commit.

## Task 4: Add-on wiring

**Files:** Modify `addon/__init__.py`, `addon/manifest.json`

- [ ] `ADDON_VERSION` constant; gather `note.tags`, `note.mod`, `card.id`,
      `card.ord`, template names, `card.did`, `card.odid`, `install_id`,
      Anki version; merge `payload.trace(...)` into the POST body.
- [ ] `py_compile`; rebuild DEV and student packages.
- [ ] Commit.

## Task 5: Deploy and verify

- [ ] Deploy; POST a suggestion with the new keys via curl; confirm the
      row and the rendered chips/trace on the live queue.
- [ ] Ask Mario to reinstall the DEV build and send one real suggestion.

## Definition of done

- [ ] Suggestions from the new add-on carry tags, card/deck ids, ord,
      template, install id, add-on and Anki versions
- [ ] Suggestions from older add-ons still succeed
- [ ] Queue shows tags as chips and a collapsible Trace block
- [ ] `content_hash` unchanged — tags are not part of it
