# Editor and revisions (phase 3) — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reviewers can change a card (Text, Extra) from a suggestion or add a new
note, every change is recorded as a pending revision, and students can propose an
edit instead of describing one.

**Architecture:** The `notes` table keeps the *current* text of every card (baseline
plus applied edits), so the reviewer's master pane and the staleness check keep
working unchanged. Every applied edit also writes a `revisions` row (guid, field,
before, after, who, source, release NULL = pending). Cutting a release later stamps
the pending rows and patches the baseline collection from them. Student-proposed
edits travel in the suggestion as `proposed_fields` and are shown as a diff.

**Tech Stack:** stdlib. Diff rendering with `difflib` server-side. No JS editor
library; a textarea with cloze highlighting in a preview pane.

**Spec:** `docs/superpowers/specs/2026-10-01-online-deck-and-sync-design.md`, §5.

---

## File structure

| path | responsibility |
| --- | --- |
| `server/revisions.py` | **new** — apply an edit to `notes`, record the revision, list pending; pure DB functions |
| `server/tests/test_revisions.py` | **new** |
| `server/server.py` | routes `/edit/<sid>`, `/notes/new`, actions on the queue; diff and editor rendering |
| `server/tests/test_editor_routes.py` | **new** |
| `addon/__init__.py` | optional "Propose an edit" in the suggest dialog: editable Text/Extra, sent as `proposed_fields` |
| `addon/payload.py` | `proposed(before, after)` → only changed fields, pure |

Editable fields are a constant `EDITABLE_FIELDS = ("Text", "Extra")` on the server;
the editor refuses anything else.

## Task 1: revisions module
- [ ] Tests: `apply_edit(conn, guid, {"Text": "new"}, user_id, source)` updates
      `notes.fields_json`, bumps `version`, recomputes `content_hash`, inserts one
      revision per changed field with before/after; unchanged fields produce no row;
      non-editable field raises; unknown guid raises; `pending(conn)` lists rows with
      `release IS NULL`; `add_note(conn, note_type, fields, tags, user_id)` inserts a
      note with a fresh guid (Anki-style 10-char base91) and a revision with
      `before = NULL`.
- [ ] Implement; commit.

## Task 2: suggestions carry proposed edits
- [ ] Tests: `POST /api/suggestions` stores `proposed_fields_json` when present
      (only editable fields kept); queue shows a diff block for it; suggestions
      without it are unchanged.
- [ ] Implement storage + `diff_block(before, after)` (unified, HTML-escaped,
      `<ins>/<del>`); commit.

## Task 3: editor routes
- [ ] Tests: `GET /edit/<sid>` needs login; shows Text/Extra textareas prefilled
      from the current note (or from the proposal if `?from=proposal`); `POST
      /edit/<sid>` with csrf applies the edit via `apply_edit` with
      `source=suggestion:<sid>`, resolves the suggestion (`resolved_by`, publish
      fields as today), queues the thank-you if email; the queue item for an
      accepted-with-edit suggestion shows "Edited" and the revision; `POST
      /reviewer/action do=accept_proposal` applies the proposal in one click.
- [ ] `GET/POST /notes/new` (login): note type picker (from baseline note types),
      Text/Extra, tags; creates via `add_note`.
- [ ] `/releases/pending` (login): list of pending revisions grouped by note.
- [ ] Implement; commit.

## Task 4: add-on proposed edit
- [ ] `payload.proposed(before, after)` tests: returns only changed editable fields,
      strips nothing, empty when identical.
- [ ] Suggest dialog: a "Propose the exact change" expander with Text and Extra
      prefilled; on send, `proposed_fields` added when changed. Import smoke test
      passes.
- [ ] Commit; rebuild packages (no deploy during an upload).

## Task 5: verify
- [ ] Full suites green; deploy once the import has finished; open the queue on the
      live item, edit it, confirm `/releases/pending` lists it.
