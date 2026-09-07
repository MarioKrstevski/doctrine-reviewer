# Doctorine Suggestions — local prototype

Two pieces:

- `addon/` — Anki add-on: "✎ Suggest" button in the reviewer bottom bar, plus a
  dev tool that stamps `DoctorineID` fields onto a deck and registers it as the
  master state on the platform.
- `server/server.py` — zero-dependency Python server (stdlib + SQLite):
  suggestion API, reviewer queue, public updates ledger, tracking pages.

## Setup (5 minutes)

1. **Start the server**
   ```
   cd server
   python3 server.py
   ```
   Runs at http://127.0.0.1:8787 and creates `doctorine.db` next to it.

2. **Install the add-on** — use a *test Anki profile*, since stamping adds a
   field to note types. Copy the `addon/` folder into your Anki add-ons dir
   and rename it, e.g.:
   - Windows: `%APPDATA%\Anki2\addons21\doctorine_suggest\`
   - macOS: `~/Library/Application Support/Anki2/addons21/doctorine_suggest/`
   - Linux: `~/.local/share/Anki2/addons21/doctorine_suggest/`

   Restart Anki. You'll see **Tools → Doctorine Suggestions**.

3. **Register a fake deck** — Tools → Doctorine Suggestions →
   **Stamp & register a deck…**, pick one of your own decks. This adds a
   `DoctorineID` field where missing, stamps unique IDs, and uploads all notes
   as master v1. (Anki will warn about a full sync — expected on a test
   profile.)

## Test scenario

1. Review a card in the registered deck → click **✎ Suggest** (bottom-right)
   → pick a type, write a comment, optionally add an email → Send.
   You get back a tracking link (`/s/<token>`).
2. Open http://127.0.0.1:8787/reviewer — the suggestion sits in the queue with
   three panes: *what the student saw* | *current master* | *the suggestion*,
   badged **Matches current version**.
3. Now simulate a deck update: edit that note in Anki (fix the "issue"), then
   run **Stamp & register a deck…** again. The master bumps to v2.
4. Refresh /reviewer — the same suggestion now shows
   **Card changed since submission — now v2**, and the two card panes visibly
   differ. Mark it **expired**, or **Resolve** it with *publish to updates*
   checked, a public one-liner, and a credit name.
5. Open http://127.0.0.1:8787/updates — the public ledger shows the accepted
   change with credit. The student's tracking link now shows the outcome.
6. Review a card from a *non-registered* deck and try to suggest — the add-on
   blocks it; and even a hand-crafted POST with a fake ID gets a 404 from the
   server (server-side validation is the real gate).

## What's deliberately out of scope (prototype)

- No auth on /reviewer (production: put it behind login or the internal tool).
- Media files aren't rehosted — images in snapshots won't load in the iframes
  (production: serve deck media from the platform and rewrite paths).
- No rate limiting / spam protection on the API.
- Tracking URLs are `http://127.0.0.1` — swap `HOST/PORT` for a real domain.

Config: `addon/config.json` → `server_url`, `id_field`.
