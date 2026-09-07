# Doctrine Editor — local prototype

Two pieces:

- `addon/` — Anki add-on: "✎ Suggest" button in the reviewer bottom bar, plus a
  dev tool that stamps `DoctrineID` fields onto a deck and registers it as the
  master state on the platform.
- `server/server.py` — zero-dependency Python server (stdlib + SQLite):
  suggestion API, reviewer queue, public updates ledger, tracking pages.

## Setup (5 minutes)

1. **Start the server**
   ```
   cd server
   python3 server.py
   ```
   Runs at http://127.0.0.1:8787 and creates `doctrine.db` next to it.

2. **Install the add-on** — use a *test Anki profile*, since stamping adds a
   field to note types. Copy the `addon/` folder into your Anki add-ons dir
   and rename it, e.g.:
   - Windows: `%APPDATA%\Anki2\addons21\doctrine_editor\`
   - macOS: `~/Library/Application Support/Anki2/addons21/doctrine_editor/`
   - Linux: `~/.local/share/Anki2/addons21/doctrine_editor/`

   Restart Anki. You'll see **Tools → Doctrine Editor**.

3. **Register a fake deck** — Tools → Doctrine Editor →
   **Stamp & register a deck…**, pick one of your own decks. This adds a
   `DoctrineID` field where missing, stamps unique IDs, and uploads all notes
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

- **No auth on /reviewer** (spec phase P2). Anyone with the URL can read
  the queue and act on it.
- Media files aren't rehosted — images in snapshots won't load in the iframes
  (production: serve deck media from the platform and rewrite paths).
- No rate limiting / spam protection on the API.

## Configuration

Server (environment variables):

| Var | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | bind address (`0.0.0.0` in the container) |
| `PORT` | `8787` | bind port (`8080` in the container) |
| `DB_PATH` | `doctrine.db` | SQLite file; mount a volume in production |
| `PUBLIC_BASE_URL` | `http://HOST:PORT` | the origin used to build tracking links |

`PUBLIC_BASE_URL` must be set in production. Tracking links are built
from it, so leaving it unset hands every student a link to their own
localhost.

Add-on (`addon/config.json`):

| Key | Purpose |
| --- | --- |
| `bootstrap_url` | permanent URL; the add-on GETs `/where` here to find the API |
| `api_base_override` | set to bypass the bootstrap entirely (local development) |
| `id_field` | note field holding the DoctrineID |
| `button_top_offset` / `button_right_offset` | reviewer button position, in px |

The add-on caches the resolved API base for 24h, so changing
`PUBLIC_BASE_URL` server-side moves where suggestions are posted without
anyone reinstalling.

## Tests

```
cd server && python3.12 -m unittest discover -s tests -t .
cd addon  && python3.12 -m unittest discover -s tests -t .
```

No third-party packages. `python3.12` matters: the container is 3.12
while macOS system `python3` is 3.9.

## Docker

```
docker build -t doctrine-editor .
docker run --rm -p 8080:8080 -e PUBLIC_BASE_URL=https://your.host \
  -v "$(pwd)/.localdata:/data" doctrine-editor
```
