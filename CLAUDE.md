# Doctorine Suggestions — project context for Claude Code

Student-facing card suggestion system for a large medical Anki deck.
Students click a "Suggest" button while reviewing, describe an issue
(typo / incorrect / confusing / other), and it lands in a private
reviewer queue. Reviewers manually apply edits in the existing internal
update system (no auto-merge), then optionally publish the accepted fix
to a public "community updates" errata page with credit. Submitter gets
a tracking link. No user accounts/login — by design.

## Architecture

- `addon/` — Anki desktop add-on (PyQt6, Anki 23.10+).
  - "✎ Suggest" button injected into the reviewer bottom bar via
    `gui_hooks.webview_will_set_content` (ReviewerBottomBar) + `pycmd`.
  - Dialog: type dropdown, text, optional email → POST /api/suggestions
    with doctorine_id, anki_note_id, deck, note fields, rendered Q/A
    HTML, css, and a content hash (sha256 over sorted fields, excluding
    the ID field). Runs network in `mw.taskman.run_in_background`.
  - Dev tool (Tools → Doctorine Suggestions → "Stamp & register a
    deck…"): adds a `DoctorineID` field to note types, stamps
    `doc-<uuid12>`, uploads all notes as master state to
    /api/dev/register. In production the pipeline mints IDs instead.
  - `config.json`: `server_url`, `id_field`.
- `server/server.py` — stdlib-only Python server + SQLite
  (`doctorine.db`, auto-created). Port 8787.
  - POST /api/suggestions — rejects unknown DoctorineID (404). This is
    the real "only our deck" gate; the add-on check is courtesy.
  - POST /api/dev/register — upserts master notes; bumps `version` when
    the content hash changes.
  - GET /reviewer — queue. Three panes per item: student snapshot
    (sandboxed iframe srcdoc) | current master vN | suggestion text.
    Staleness: snapshot hash != master hash → "Card changed since
    submission" badge → reviewer marks expired. Actions: resolve
    (+ publish checkbox, public one-liner, credit name), decline,
    expire.
  - GET /updates — public errata ledger of published resolutions.
  - GET /s/<token> — submitter tracking page (open/accepted/declined/
    already-fixed).
- Hash function is duplicated in both files (`content_hash`) and MUST
  stay identical.

## Status: working end-to-end (tested)

register → suggest → queue shows "matches current version" → edit note
+ re-register (v2) → same suggestion flips to outdated → expire or
resolve+publish → /updates shows credited entry → tracking link updates.
Spoofed ID correctly 404s.

## TODO (rough priority)

1. Abuse protection (designed, not implemented — see decisions below):
   - Add-on: generate a random `install_id` UUID on first run, store in
     config/meta, send with every suggestion.
   - Server: log `install_id` + client IP on each suggestion row.
   - Rule: ONE open suggestion per (install_id, doctorine_id) — a
     repeat either gets a friendly "already pending" message or appends
     to the existing item with a counter. No new queue entries.
   - Per-install_id daily rate limit (~10/day). Per-IP burst limit only
     (campus/library NAT means many legit users share an IP — do NOT
     hard-limit per IP).
   - Reviewer UI: show short install_id + IP in item meta, "decline all
     open from this source" bulk action. Shadow-ban list for install_ids
     (accept + discard silently).
2. Reviewer auth — /reviewer must go behind a login or the internal
   tool. Prototype has none.
3. Media: snapshot HTML references local Anki media; images don't load.
   Production: host deck media on the platform, rewrite src paths.
   (Pipeline shipped the media, filenames match.)
4. Dedupe across sources: same doctorine_id + near-identical text from
   different people → collapse with counter (also merges legit pile-ons).
5. Real deployment: swap HOST/PORT for a domain (tracking URLs are
   currently localhost); HTTPS; consider porting server to the existing
   Next.js/Neon stack if it should live with the other products —
   stdlib server is prototype scaffolding, not an architecture choice.
6. Nice-to-haves: email notification on resolution (email field already
   captured); keyboard shortcut for the button; payload size caps.

## Decisions already made (don't relitigate)

- Separate add-on (no access to the Study Navigator add-on codebase);
  two installs is acceptable.
- No template-embedded button (visual clutter in study content).
- No forum. Public proof = curated changelog of accepted fixes only.
- No login/CAPTCHA/email-verification for students. Identity =
  install_id (+ optional purchase-time submission key later).
- Reviewers apply edits manually; suggestions are input, never merged.
- DoctorineID is minted by the generation pipeline in production and
  ships inside the .apkg; server validates existence on every submit.

## Test loop

1. `cd server && python3 server.py`
2. Copy `addon/` into Anki addons21 dir (TEST PROFILE — stamping alters
   note types and forces full sync), restart Anki.
3. Tools → Doctorine Suggestions → Stamp & register a deck.
4. Review a card → ✎ Suggest → send → check /reviewer.
5. Edit the note in Anki, re-register → badge flips to outdated.
