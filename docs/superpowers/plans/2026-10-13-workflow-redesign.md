# Workflow redesign — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The platform becomes a Study Navigator–branded, Linear-style workflow:
Inbox (grouped by card) → Cards (history) → Next release → Release (notes, public or
silent) → Outbox, plus Deck (upload), Detection, Settings; downloads streamed and
tagged per buyer; multi-deck ready; a clean slate with v2 re-imported.

**Architecture:** Same stdlib server. Text-at-release reconstructed from revisions.
Packages never stored: built and streamed per download as a stored (uncompressed)
zip with exact Content-Length. A `decks` table scopes notes, releases, revisions,
uploads, suggestions and media. Staff pages share a sidebar shell; public pages a
minimal header.

**Spec:** `docs/superpowers/specs/2026-10-13-workflow-redesign.md`, decisions answered
2026-10-13: wipe test history; keep latest + previous collection only; browser
re-upload on the current volume with a free-space guard; retired cards untouched;
detection page with thresholds 20 (watch) / 100 (red), detection only; brand from
studynavigator.com (navy/blue/teal/cream, Fraunces + Plus Jakarta Sans).

---

- [x] **T1 Brand + shell.** (done 2026-10-13, commit on main) New tokens and fonts; public header (Updates, Install) with
      no staff links; staff sidebar (Inbox, Cards, Next release, Releases, Outbox,
      Deck, Detection, Settings) with counts; tests that anonymous pages contain no
      staff links.
- [x] **T2 Release text at version.** (done) `revisions.text_at(conn, guid, release_order)`;
      build and sync use it. Test: edit after cut does not leak.
- [x] **T3 Streamed downloads.** (done; HTTP/1.0 streamed zip, no Content-Length, keeps 2 newest collections) `release_build.stream_apkg(write, collection, media,
      license=None)` with ZIP_STORED and precomputed length; `/dl/<token>` and staff
      downloads stream; no package files; keep latest + previous collection, delete
      older. Tests: round-trip via `tools/apkg.py`, Content-Length exact, license file
      present when given.
- [x] **T4 Inbox by card.** (done 2026-10-13) Group open suggestions by note; card header with count;
      per-suggestion Duplicate / Decline / Expired; card-level Accept proposal (pick
      one) or Edit card; saving resolves the selected suggestions (default all open on
      that card) and queues a thank-you for each with an email. Banner shows target
      release.
- [x] **T5 Cards.** (done; image-rename rule moves to T8 where media is uploaded) Tag search; History timeline (revisions + suggestions, date, who,
      source, diff); "pending edit exists" notice on the editor; image-rename rule in
      the editor (warn when an `src` name exists with different content).
- [x] **T6 Release page.** (done) Version suggestion (minor/major radio), public notes
      (markdown-ish, plain) or silent; Updates shows release notes for public releases.
- [x] **T7 Add-on.** (done: prompt shows your version vs newest and releases behind; ping shipped with T10; install page warning) Prompt: "Your deck: 1.0 · Newest: 1.3 (3 releases behind)" side by
      side; ping `/api/ping` with install_id, add-on version, deck release, license id
      (read from `_doctrine_license.json` in collection.media); install page warning.
- [x] **T8 Deck page.** (done: raw-body upload with progress, admin only; media/claim not needed) Streamed browser upload to disk with free-space guard; choose
      "replace this deck (re-upload)" or "new deck"; id-overlap check refuses a
      replacement whose ids mostly differ; re-upload → diff review (existing);
      `/api/media/claim` so the CLI skips uploading blobs already on disk.
- [x] **T9 Multi-deck.** (done as foundation: decks table, platform_deck columns, per-deck official/sync/where, add-on pinned by DECK_SLUG, Add deck on Deck page. Not yet: per-deck release building UI and import into a second deck; release versions must be unique across decks) `decks` table; `deck_id` on scoped tables (default 1);
      queries filter by active deck; `/where` returns deck slug; add-on pinned to it.
- [x] **T10 Detection.** (done: /api/ping, installs table, Detection page 20/100, add-on 1.7.0 daily ping reads _doctrine_license.json) Licenses table; per-download license token; installs table
      from pings; page listing licenses by install count with thresholds.
- [x] **T11 Clean slate.** (done 2026-10-13: test data wiped in place; v2 deck and 1.0 baseline kept, so no re-import and no media/claim needed) Wipe test data on production, re-import v2 as 1.0 (media
      claimed, not re-uploaded), deploy, verify end to end.

## Progress log

- 2026-10-13: production volume filled by a stored 2.15 GB package; package and a test
  row deleted, DB vacuumed, writes restored. 1.1 collection kept; 1.1 content verified.
- 2026-10-13: T1 done. Order agreed with Mario because of usage limits: T1, T4 (Inbox
  by card), T2+T3 (release text fix + streamed downloads), T11 (clean slate), then
  T5, T6, T7, T8, T9, T10 if budget remains. Mario confirmed: wipe all test data,
  volume stays 4.6 GB unless more is needed, detection thresholds 20/100.
- 2026-10-13: Mario: the add-on automatic update prompt (T7) goes last (hardest). Order now:
  T4, T2+T3, T11, T5, T6, T8, T9, T10, T7. Deploy each task separately so he sees changes early.
- 2026-10-13: T4, T2, T3, T11 done and deployed. Next: T5, T6, T8, T9, T10, then T7 (last).
- 2026-10-13: T5, T6, T8, T9, T10 done and deployed. Only T7 (add-on update prompt, last by Mario's choice) remains.
- 2026-10-13: T7 done. All of T1-T11 shipped. Remaining ideas: per-deck release UI, student-facing real-device test of 1.7.0.
- 2026-10-14: UI feedback round shipped. New: ui.py (shared components), cardview.py (Anki-like rendering with sanitizer),
  shadcn-style sidebar with deck switcher and user menu, global active deck scoping every page, public pages always public,
  Add-on and testing staff page, Inbox with Rendered/HTML/Differences panes and stale-proposal rule, Cards tiles,
  Edit page tabs (Edit, History, Anki view), Releases redesign, Deck page picker with triple-confirm delete,
  Detection info box, auto-refresh while building, add-on 1.7.1 (stale card fix, check on open, add-on self-update).
- 2026-10-14: Labels (per card, per deck, created on the fly; Inbox New/Triaged/Closed; Cards label: search; Next release filter)
  and release approval (admin only, not the preparer unless Users setting allows; build locked until approved; remove card from
  draft resets approval) shipped. Release detail page lists what changed, who, source, student words, thank-you status.
