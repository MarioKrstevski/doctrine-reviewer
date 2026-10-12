# Review feedback pass — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix everything Mario found testing phases 1–6: naming, the edit page,
release safety and overview, copyable links, direct card editing, deck-name spelling.

**Architecture:** Server-rendered pages as before. One new page (Cards), one renamed
and redesigned (Next release), one confirm page per build. Revision rows gain a
`kind` derived at cut time from their source (suggestion type, staff, upload, added).
Cutting takes an explicit set of revision ids.

**Tech Stack:** stdlib.

---

## Task 1: queue actions
- [ ] Remove the Resolve button and publish fields from the queue item; keep Accept
      proposal, Edit and accept, Decline, Mark expired. Publish fields live on the
      edit page only. Server action `resolve` stays for API compatibility.
- [ ] Tests updated; commit.

## Task 2: edit page
- [ ] Layout: left column snapshot + student text + proposed diff; right column
      stacked editors (label above, full width, Text 14 rows, Extra 8 rows), live
      rendered preview under each with cloze marks highlighted, then publish fields,
      then Save and accept / Cancel. Used by `/edit/<sid>` and `/cards/<guid>/edit`.
- [ ] Commit.

## Task 3: Next release (was Pending)
- [ ] `revisions.kind` column: `typo|incorrect|confusing|media|other` from the
      suggestion, `staff`, `upload`, `added`, `retired`. Backfilled on read when NULL.
- [ ] Page groups by kind with counts, each note shows diff, author, source link;
      checkboxes per note, "select group" toggles; "Review selection" → confirm page
      listing the chosen set → version field → Cut.
- [ ] `cut_release(version, revision_ids, user_id)`; unselected stay pending.
- [ ] Tests; commit.

## Task 4: releases page
- [ ] Draft card: "Build" opens `/releases/<v>/confirm` showing the changelog and a
      field that must equal the version to enable Build; "Discard draft" unstamps
      its revisions (back to Next release) and deletes the draft row.
- [ ] Published card: Download, "Rebuild package" when none on disk.
- [ ] Download links: full URL per token in the table with a Copy button; the new
      one highlighted with Copy; Revoke per token.
- [ ] Sections separated; empty changelog hidden.
- [ ] Tests; commit.

## Task 5: Cards
- [ ] `/cards?q=` (login): search guid exact, nid exact, else text LIKE over Text/
      Extra, max 50 rows with snippet, tags, link to edit. `/cards/<guid>/edit` GET/POST
      reuse the editor; save = `apply_edit(..., source="staff")`, no suggestion.
- [ ] Nav: Cards. Tests; commit.

## Task 6: add-on spelling + packages
- [ ] Accept Doctrin/Doctrine/Doctorine in identity fallback and sync deck pick;
      version 1.6.2; rebuild DEV + student.

## Task 7: deploy and hand back
- [ ] Full suites; deploy once; verify pages; send packages; list what to retest.
