# Doctrine Editor — project context for Claude Code

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
    with doctrine_id, anki_note_id, deck, note fields, rendered Q/A
    HTML, css, and a content hash (sha256 over sorted fields, excluding
    the ID field). Runs network in `mw.taskman.run_in_background`.
  - Dev tool (Tools → Doctrine Editor → "Register a deck as master…",
    DEV_MODE builds only): uploads a deck's notes keyed by guid to
    /api/master/sync in batches of 100 with the pipeline bearer key.
    Read-only against the collection. Per release: import the new .apkg
    into a throwaway profile, run this once.
  - `resolver.py`: pure, Anki-free API-base resolution (override →
    fresh cache → GET {bootstrap_url}/where → stale cache → bootstrap).
    Unit-tested in `addon/tests/`.
  - `config.json`: `bootstrap_url`, `api_base_override`, `id_field`,
    `button_top_offset`, `button_right_offset`.
- `server/server.py` — stdlib-only Python server + SQLite
  (`doctrine.db`, auto-created). Config via env, see `server/config.py`.
  - `Server` subclasses ThreadingHTTPServer to skip `socket.getfqdn()`,
    which otherwise blocks startup ~35s on networks with no reverse zone.
  - GET /where — bootstrap endpoint returning the current `api_base`.
  - POST /api/suggestions — rejects unknown DoctrineID (404). This is
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
  - GET/POST /outbox (login) — thank-you outbox. Resolving a suggestion
    that carries an email queues one `notifications` row with a rendered
    plain-text subject/body; reviewers copy it into their mail client and
    mark it thanked or skipped. Declines/expirations queue nothing.
    Nothing sends mail: `email_out.send()` is a stub, `SEND_EMAIL` is off,
    and `SEND_EMAIL=true` without `SMTP_URL` refuses to boot.
  - GET /s/<token> — submitter tracking page (open/accepted/declined/
    already-fixed).
- Hash function is duplicated in both files (`content_hash`) and MUST
  stay identical. Enforced by `server/tests/test_hash_parity.py`, which
  extracts both implementations and compares their output.

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
   - Rule: ONE open suggestion per (install_id, doctrine_id) — a
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
4. Dedupe across sources: same doctrine_id + near-identical text from
   different people → collapse with counter (also merges legit pile-ons).
5. Real deployment: swap HOST/PORT for a domain (tracking URLs are
   currently localhost); HTTPS; consider porting server to the existing
   Next.js/Neon stack if it should live with the other products —
   stdlib server is prototype scaffolding, not an architecture choice.
6. Nice-to-haves: implement `email_out.send()` behind SEND_EMAIL (the
   outbox and queueing already exist); keyboard shortcut for the button;
   payload size caps.

## Decisions already made (don't relitigate)

- Separate add-on (no access to the Study Navigator add-on codebase);
  two installs is acceptable.
- No template-embedded button (visual clutter in study content).
- No forum. Public proof = curated changelog of accepted fixes only.
- No login/CAPTCHA/email-verification for students. Identity =
  install_id (+ optional purchase-time submission key later).
- Reviewers apply edits manually; suggestions are input, never merged.
- Identity = the Anki note **guid**. The deck pipeline already sets it to
  the card's MongoDB ObjectId (24 lowercase hex), verified 100% preserved
  across export -> import on Doctrine 0.1 (34,704/34,704). Nothing is
  stamped, no field is added, the add-on never writes to the collection,
  so students are never forced into a full sync. The server column is
  still called `doctrine_id`; its value is the guid. Client-side button
  gate = ObjectId-shaped guid OR top-level deck named "Doctrine"
  (`addon/identity.py`); the server's "is this guid registered?" check is
  the real gate.

## Test loop

1. `cd server && python3 server.py`
2. Install the DEV build into a test profile that has Doctrine X.Y.apkg
   imported, restart Anki.
3. Tools → Doctrine Editor → Register a deck as master (DEV build).
4. Review a card → ✎ Suggest → send → check /reviewer.
5. Edit the note in Anki, re-register → badge flips to outdated.
   (`DoctrineID` is excluded from the content hash on both sides only as
   a legacy no-op, so a profile stamped by an older build still matches.)
