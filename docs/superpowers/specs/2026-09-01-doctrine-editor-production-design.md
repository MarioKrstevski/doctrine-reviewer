# Doctrine Editor — production readiness design

Date: 2026-09-01
Status: approved (brainstorm), not yet implemented

Takes the working local prototype (`addon/` + `platform/server.py`) to a
hosted, authenticated, rebranded system that a remote team can review.

## Goal

A tester installs the plugin, suggests an edit from Anki, and it lands in
a login-protected reviewer queue on a public HTTPS host. Reviewers resolve
or decline; accepted fixes appear on a public ledger; submitters who left
an email land in a thank-you outbox that a human works by hand.

## Out of scope (still open, deliberately not in this pass)

- Abuse protection / rate limiting (README TODO 1).
- Media rehosting — images in snapshot iframes still will not load
  (README TODO 3).
- Cross-source dedupe of near-identical suggestions (README TODO 4).
- Any outbound email. Nothing in this system sends mail.

---

## PV — Version control (done / in progress)

Prerequisite for everything else: P1 deploys from a Git repository, so
the repo must exist before hosting is configured.

**Done:** `git init` at the workspace root, `main` branch, initial commit
of the prototype and this spec. `.gitignore` excludes `*.db`, `*.ankiaddon`,
`__pycache__/`, `.env*`, and `.DS_Store`.

The database exclusion is a privacy requirement, not tidiness — the
suggestions table stores submitter email addresses. It must never reach a
remote, public or private.

**Remaining (owner: Mario):**

1. Create the GitHub repository. Private until the public `/updates`
   ledger and the code are both ready to be seen.
2. `git remote add origin <url>` and `git push -u origin main`.
3. Connect the repository to Northflank so P1 builds from it.

**Repository layout note.** The workspace currently nests
`doctrine-review-proto/doctorine-proto/`. Two levels for one project is
redundant and will read badly as a GitHub repo root. P0 flattens this:
the inner folder's contents move up to the repository root as part of the
rename, so the tree becomes `addon/`, `platform/`, `docs/`, `README.md`.

**Secrets.** No credential is ever committed. `PIPELINE_API_KEY` (P4) and
any future SMTP settings (P5) are set as Northflank environment
variables. Generated reviewer passwords (P2) are displayed once in the
browser and never written to a file.

---

## P0 — Rename to Doctrine Editor

Blocks everything else; every later phase touches these strings.

| From | To |
| --- | --- |
| package `doctorine_suggest` | `doctrine_editor` |
| display name `Doctorine Suggestions` | `Doctrine Editor` |
| note field `DoctorineID` | `DoctrineID` |
| DB column `doctorine_id` | `doctrine_id` |
| `doctorine.db` | `doctrine.db` |
| folder `doctorine-proto` | `doctrine-editor` |
| button label `✎ Suggest Edit on Doctrine` | `✎ Suggest an edit` |

The `doc-<uuid12>` ID prefix is unchanged — it is opaque and carries no
branding.

No migration is written. The prototype DB is dropped and the test deck is
re-stamped and re-registered. This is only safe because no real deck has
shipped yet; doing the rename after a deck reaches students would require
a real migration on both the DB and every installed collection.

`CLAUDE.md` and `README.md` are updated in the same pass.

---

## P1 — Hosting and the bootstrap URL

### Server changes

- Bind `0.0.0.0`; read `PORT` from env (default 8787).
- `PUBLIC_BASE_URL` env replaces the hardcoded `127.0.0.1:8787` used to
  build `/s/<token>` tracking links.
- SQLite path from `DB_PATH` env, pointed at a mounted volume in
  production so the database survives restarts and redeploys.
- Dockerfile: `python:3.12-slim`, copy `platform/`, no dependency install
  (the server is stdlib-only).
- Northflank service, built from the Dockerfile, with a persistent
  volume mounted at `/data`. The free tier runs two services with no
  forced sleep and provides a generated HTTPS subdomain; a custom domain
  later gets a managed Let's Encrypt certificate.

### Host selection (decided 2026-09-01)

Fly.io was the original choice and was dropped: its free allowance now
applies only to legacy accounts.

Render's free tier was rejected outright. It has no persistent disk, and
separately it spins services down after 15 minutes with a 30-60s cold
start while the add-on's HTTP timeout is 15s (`addon/__init__.py`) — a
tester's first suggestion against a cold server would fail rather than
merely be slow.

Vercel was considered, since a Hobby account and deploy tooling already
exist, but it is serverless: no disk, so it forces the SQLite -> Postgres
port and a rewrite of `server.py`'s request handling before anything can
be shown to anyone. Deferred as the likely *eventual* home (CLAUDE.md
already names the Next.js/Neon stack), not the review deployment.

**Open risk — verify at signup.** The free tier's persistent *volume*
allowance is not confirmed. If volumes turn out to be paid-only, the
fallback is Northflank's one free managed Postgres, which pulls the
SQLite -> Postgres port forward into P1 rather than leaving it for later.
Confirm this before writing the Dockerfile.

### `GET /where` — the bootstrap endpoint

Public, unauthenticated, no side effects.

```json
{ "api_base": "https://<host>", "min_addon_version": "1.0" }
```

Served from env (`PUBLIC_BASE_URL`), `Cache-Control: public, max-age=3600`.

### Plugin resolution order

1. `api_base_override` in `config.json` — if set, used verbatim, nothing
   is fetched. This is the localhost development path.
2. Cached `api_base` from a previous `/where` fetch, if under 24h old.
3. Fresh `GET {bootstrap_url}/where`, result cached with a timestamp.
4. On any failure, the last known good cached value regardless of age.
5. If there has never been a successful fetch, `bootstrap_url` itself.

Resolution runs inside `mw.taskman.run_in_background` alongside the
existing POST, so a slow or unreachable bootstrap never blocks the Anki
UI. Cache lives in the add-on's writable config.

`config.json` becomes:

```json
{
  "bootstrap_url": "https://<generated-northflank-host>",
  "api_base_override": "",
  "id_field": "DoctrineID",
  "button_top_offset": 150,
  "button_right_offset": 12
}
```

### Accepted trade-off

The generated Northflank host is disposable — it exists so the team can
review the system. Testers will need **one** plugin reinstall when the
permanent host is chosen. From that point the bootstrap URL is fixed
forever and a domain change requires only an env var change server-side,
never a reinstall. `bootstrap_url` stays user-editable so a mid-test
switch can be handled by editing one line instead.

---

## P2 — Authentication

### Roles

`users.role` is `admin` or `reviewer`. The admin (one person) manages
accounts. Reviewers only review.

An admin can be created **only** via CLI:
`python3 server.py adduser <name> --admin`. There is no signup route and
no way to escalate a reviewer to admin through the web UI.

### Schema

```
users(id, username UNIQUE, password_hash, salt, role, active,
      created_at, password_changed_at)
sessions(token PRIMARY KEY, user_id, created_at, expires_at)
login_attempts(username, ip, attempted_at, ok)
```

Passwords: `hashlib.scrypt` with a per-user 16-byte salt. Comparison via
`hmac.compare_digest`.

### Sessions

256-bit `secrets.token_urlsafe` token. Cookie is `HttpOnly`, `Secure`,
`SameSite=Lax`, 14-day expiry. Server-side expiry is authoritative;
expired rows are deleted lazily on lookup.

### Routes

| Route | Access |
| --- | --- |
| `GET/POST /login`, `POST /logout` | public |
| `/reviewer` and its resolve/decline/expire actions | authenticated |
| `/outbox` and its actions | authenticated |
| `/account` | authenticated (self only) |
| `/admin/users` and its actions | admin only |
| `/updates`, `/s/<token>`, `POST /api/suggestions`, `GET /where` | public |

### `/admin/users` (admin only)

- **Create reviewer** — enter a username; the server generates a strong
  password and displays it **once**. The admin copies it and delivers it
  out-of-band. An explicit password may be typed instead.
- **Reset password** — same one-time display.
- **Deactivate / reactivate**.

### `/account` (any authenticated user)

Change own password; current password required. That is the entire page.
A reviewer cannot see other users, create anyone, or reset anyone —
including themselves. A forgotten password is recovered by asking the
admin. This is the only recovery path, by design.

### Two behavioural decisions

1. **A password reset or deactivation immediately invalidates that
   user's sessions.** Deleting their `sessions` rows is what makes
   "remove this person" take effect now rather than whenever their
   14-day cookie happens to expire.
2. **Deactivate, never delete.** Deleting a user would orphan
   `resolved_by` on every suggestion they handled, destroying the record
   of who approved what. Deactivation blocks login and hides them from
   reviewer-facing lists while history stays intact. A returning
   reviewer is reactivated with a new password on the same account.

### Hardening

- Login throttle: 5 failed attempts per username per 15 minutes, counted
  in `login_attempts`. Applied per username, not per IP — campus and
  library NAT means many legitimate users share an IP.
- Failed login returns one generic message regardless of whether the
  username exists.
- CSRF: a per-session token in a hidden field on every state-changing
  form, checked server-side. `SameSite=Lax` alone is not relied on.

### Audit

Every resolve / decline / expire records `resolved_by` (user id) and a
timestamp. Surfaced in the reviewer UI only. The public ledger continues
to credit the *submitter*, never the reviewer.

---

## P3 — Richer suggestion payload

Already sent today: all note fields (including `Extra`), `anki_note_id`,
deck name, note type name, rendered question/answer HTML, note type CSS,
`content_hash`.

Added by this phase:

| Field | Source | Why |
| --- | --- | --- |
| `tags` | `note.tags` | requested; useful for routing and filtering |
| `note_guid` | `note.guid` | stable across export/import — more reliable than note id |
| `note_mod` | `note.mod` | when the note last changed in that collection |
| `card_id` | `card.id` | a card id *does* exist and is distinct from the note id |
| `card_ord`, `template_name` | `card.ord`, note type templates | which side/template the student actually saw |
| `deck_id` | `card.did` | requested |
| `original_deck_id` | `card.odid` | non-zero only inside filtered decks; `did` is misleading there |
| `install_id` | uuid4, generated once, persisted in add-on config | needed later for rate limiting and "decline all from this source" |
| `addon_version` | manifest | reproducing what the student ran |
| `anki_version` | `anki.buildinfo.version` | same |

Server stores each as a column on the suggestion row. The reviewer UI
shows tags as chips and puts the identifiers in a collapsible **Trace**
block so the three-pane layout stays readable.

**`content_hash` is unchanged — fields only, still excluding the ID
field.** Tags are deliberately excluded: including them would make a
routine re-tagging pass falsely mark every pending suggestion as stale.
If tag drift ever needs tracking it gets a separate `tags_hash`.

The hash function remains duplicated across `addon/__init__.py` and
`platform/server.py` and MUST stay byte-identical.

---

## P4 — Master state sync from the pipeline

The queue's middle pane ("current master vN") is only trustworthy if the
server knows the genuinely shipped state of each card.

`POST /api/master/sync`, `Authorization: Bearer $PIPELINE_API_KEY`.

Body is the existing `/api/dev/register` shape plus a `release_version`
string. Behaviour is unchanged: upsert by `doctrine_id`, bump `version`
when the content hash differs.

The deck generation pipeline calls this on every release. The Anki
"Stamp & register a deck…" dev tool is kept for local testing but is
gated behind the same bearer key, so an unauthenticated caller can no
longer rewrite master state.

A missing or wrong key returns 401 and writes nothing.

---

## P5 — Thank-you outbox (built, sends nothing)

```
notifications(id, suggestion_id, email, subject, body, kind,
              status, created_at, actioned_at, actioned_by)
```

`status` is `pending` | `thanked` | `skipped`.

A row is created automatically when a suggestion is **resolved** and the
submitter left an email. Declines and expirations create nothing.

`/outbox` (authenticated) lists pending rows with:

- the card and a link to the suggestion,
- the submitter's suggestion text and its length, so substantial
  contributions are easy to spot among one-word typo reports,
- pre-rendered thank-you subject and body,
- a copy button, and **Mark as thanked** / **Skip**.

`SEND_EMAIL` env defaults to `false`. A single `send(notification)`
function is stubbed; enabling the flag without SMTP configuration raises
at startup rather than silently dropping mail. Wiring a real sender later
requires filling in that one function — no restructuring.

---

## P6 — Button positioning

`button_top_offset` and `button_right_offset` from `config.json` are
interpolated into the injected CSS, so the button can be nudged against
the other add-on's UI by editing config and restarting Anki, with no code
change. The final measured values are then written back as the defaults.

---

## Sequencing

PV → P0 → P1 → P2 → P3 → P4 → P5 → P6.

PV first because P1 deploys from a Git remote, so the repository has to
exist and be connected before hosting can be set up. P0 next because
every later phase edits the same identifiers. P1 before
P2 because `Secure` cookies require the HTTPS that hosting provides. P6
last because it needs the other add-on running side by side to measure
against.

## Verification

Each phase ends with the README test loop re-run end to end against the
*hosted* server rather than localhost: register → suggest → queue shows
"matches current version" → edit and re-sync → same suggestion flips to
outdated → expire or resolve+publish → `/updates` shows the credited
entry → tracking link reflects the outcome. A spoofed `doctrine_id` must
still 404.

Additional per-phase checks:

- P2: an unauthenticated request to `/reviewer` redirects to `/login`; a
  deactivated user's existing session stops working immediately; a
  reviewer cannot reach `/admin/users`.
- P4: `/api/master/sync` without a bearer key returns 401 and leaves the
  master table untouched.
- P5: resolving a suggestion with an email creates exactly one pending
  notification; resolving one without an email creates none.


---

## Addendum 2026-09-08 — identity is the note guid, not a stamped field

Inspection of `Doctrine 0.1.apkg` (34,704 notes) showed every note guid
is a MongoDB ObjectId minted by the deck pipeline, and 100% of them
survived export -> import unchanged. The pipeline therefore already ships
a database-owned stable id in the guid slot.

Consequences, superseding P0's `DoctrineID` field and the stamp tool:

- The add-on sends `note.guid` as `doctrine_id`. It never writes to the
  collection: no field, no note-type change, no forced full sync.
- The stamp tool is removed. Registration (DEV builds only) is read-only
  and keyed by guid; the deck team changes nothing.
- Client-side button gate: ObjectId-shaped guid or top-level deck
  "Doctrine" (`addon/identity.py`). Server registration remains the real
  gate.
- `DoctrineID` stays excluded from content hashing on both sides purely
  so profiles stamped by earlier builds hash like clean imports.
- Residual risk: guid stability across *releases* is a property of the
  pipeline keeping database ids stable. Verified for export -> import;
  release -> release cannot be measured until a second release exists.
