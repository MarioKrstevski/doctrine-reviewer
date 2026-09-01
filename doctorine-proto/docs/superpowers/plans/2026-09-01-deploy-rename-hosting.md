# Doctrine Editor — Deploy (rename + hosting) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the working local prototype to a publicly reachable HTTPS
host under the name "Doctrine Editor", with a bootstrap URL that lets the
add-on follow a future domain change without being reinstalled.

**Architecture:** Extract configuration out of module-level constants into
an env-driven `config.py` so the same code runs locally and in a
container. Add `GET /where`, a tiny public endpoint returning the current
API base. The add-on gains a pure, Anki-free `resolver.py` that decides
which base URL to POST to, so that logic is unit-testable without running
Anki. Deploy as a Docker container on Northflank with SQLite on a
persistent volume.

**Tech Stack:** Python 3.12 stdlib only (`http.server`, `sqlite3`,
`unittest`, `urllib`). No third-party packages, in the server or the
tests. Docker for deployment. PyQt6/aqt in the add-on only.

**Covers spec phases:** PV (version control), P0 (rename), P1 (hosting +
bootstrap). Spec: `docs/superpowers/specs/2026-09-01-doctrine-editor-production-design.md`

**Does NOT cover:** authentication (P2), richer payload (P3), pipeline
sync (P4), outbox (P5), button positioning (P6). Those are separate plans.
Until P2 lands, the deployed `/reviewer` queue is **publicly readable** —
see Task 12 for the interim mitigation.

---

## File Structure

After Task 1 the repository root is the project root. Final layout:

| Path | Responsibility |
| --- | --- |
| `server/config.py` | **new** — read env into a frozen `Config`. No I/O, no DB. |
| `server/server.py` | existing — HTTP handling, DB, rendering. Loses its hardcoded constants. |
| `server/tests/helpers.py` | **new** — spin the server on an ephemeral port against a temp DB. |
| `server/tests/test_config.py` | **new** — `config.load()` behaviour. |
| `server/tests/test_where.py` | **new** — the bootstrap endpoint. |
| `server/tests/test_api.py` | **new** — suggestion API, incl. the tracking-URL fix. |
| `addon/resolver.py` | **new** — pure API-base resolution. Imports nothing from `aqt`. |
| `addon/tests/test_resolver.py` | **new** — resolution and cache logic. |
| `addon/__init__.py` | existing — Anki glue. Delegates URL choice to `resolver`. |
| `Dockerfile` | **new** — stdlib-only image. |
| `.dockerignore` | **new** — keep the DB and git metadata out of the image. |

`resolver.py` is deliberately separate from `__init__.py`: `__init__.py`
cannot be imported without Anki installed, so any logic left inside it is
untestable. `resolver.py` takes its clock and its HTTP fetcher as
arguments, so tests inject fakes and never touch the network.

---

## Task 1: Flatten the repository and rename `platform/`

No tests — this is a pure file move, verified by the tree and by the
existing server still starting.

**Files:**
- Move: `doctorine-proto/*` → repository root
- Rename: `platform/` → `server/`

> **Why rename `platform/`:** `platform` is a Python **standard library
> module**. Once `server/` is on `sys.path` (which Task 2 requires so
> tests can import it), `import platform` anywhere in the process would
> resolve to this directory instead of the stdlib. This is a latent
> import-shadowing bug; fix it before adding tests, not after.

- [ ] **Step 1: Move the project up one level and rename**

```bash
cd /Users/mario/Documents/work/frex-solutions/client-projects/doctrine-review-proto
git mv doctorine-proto/platform doctorine-proto/server
for f in CLAUDE.md README.md addon docs server doctorine_suggestions.ankiaddon; do
  git mv "doctorine-proto/$f" "$f" 2>/dev/null || mv "doctorine-proto/$f" "$f"
done
rmdir doctorine-proto
ls
```

Expected: `CLAUDE.md  README.md  addon  docs  server` at the root.
`doctorine_suggestions.ankiaddon` is gitignored, so it moves with `mv`,
not `git mv` — that is expected and not an error.

- [ ] **Step 2: Verify the server still starts from its new location**

```bash
cd server && timeout 3 python3 server.py; echo "exit=$?"
```

Expected: the three startup lines print, then `exit=124` (timeout killed
it). Any traceback is a failure.

- [ ] **Step 3: Update the two path references in the docs**

In `README.md`, `cd platform` becomes `cd server`. In `CLAUDE.md`,
`` `platform/server.py` `` becomes `` `server/server.py` `` (2 occurrences).

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor: flatten repo to root, rename platform/ to server/

platform/ shadowed the Python stdlib module of the same name, which
would break imports once the directory landed on sys.path for tests."
```

- [ ] **Step 5: Push to GitHub (owner: Mario)**

```bash
git remote add origin git@github.com:<you>/<repo>.git && git push -u origin main
```

Make the repository **private**. Do not continue to Task 10 until this
succeeds — Northflank builds from the remote.

---

## Task 2: Test harness

**Files:**
- Create: `server/tests/__init__.py` (empty)
- Create: `server/tests/helpers.py`
- Create: `server/tests/test_smoke.py`

- [ ] **Step 1: Write the failing smoke test**

`server/tests/test_smoke.py`:

```python
import unittest
import urllib.request

from tests.helpers import running_server


class SmokeTest(unittest.TestCase):
    def test_updates_page_is_reachable(self):
        with running_server() as base:
            with urllib.request.urlopen(base + "/updates") as r:
                self.assertEqual(200, r.status)

    def test_unknown_path_is_404(self):
        with running_server() as base:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/nope")
            self.assertEqual(404, ctx.exception.code)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd server && python3 -m unittest discover -s tests -t . -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tests.helpers'`.

- [ ] **Step 3: Write the harness**

`server/tests/helpers.py`:

```python
"""Run the real server on an ephemeral port against a throwaway DB.

Each `running_server()` gets its own temp directory, so tests never share
state and never touch a developer's real doctrine.db.
"""

import contextlib
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import config
import server


@contextlib.contextmanager
def running_server(**env):
    """Yield the base URL of a freshly started server.

    Extra keyword arguments override environment values, e.g.
    `running_server(PUBLIC_BASE_URL="https://example.test")`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        settings = {"DB_PATH": str(Path(tmp) / "test.db"), "PORT": "0"}
        settings.update(env)

        previous = server.CFG
        server.CFG = config.load(settings)
        server.init_db()

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            server.CFG = previous
```

Also create an empty `server/tests/__init__.py`.

- [ ] **Step 4: Run again — still failing, but for a new reason**

```bash
cd server && python3 -m unittest discover -s tests -t . -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'config'`. This is
correct; Task 3 creates it. Do not proceed until the error is *this* one,
not the previous one.

- [ ] **Step 5: Commit**

```bash
git add server/tests && git commit -m "test: add server test harness (ephemeral port, temp DB)"
```

---

## Task 3: Env-driven configuration

**Files:**
- Create: `server/config.py`
- Create: `server/tests/test_config.py`
- Modify: `server/server.py` (replace the `HOST, PORT, DB_PATH` constants)

- [ ] **Step 1: Write the failing test**

`server/tests/test_config.py`:

```python
import unittest

import config


class LoadTest(unittest.TestCase):
    def test_defaults_are_local(self):
        cfg = config.load({})
        self.assertEqual("127.0.0.1", cfg.host)
        self.assertEqual(8787, cfg.port)
        self.assertEqual("doctrine.db", cfg.db_path)
        self.assertEqual("http://127.0.0.1:8787", cfg.public_base_url)

    def test_port_is_an_int(self):
        self.assertEqual(9000, config.load({"PORT": "9000"}).port)

    def test_public_base_url_defaults_to_host_and_port(self):
        cfg = config.load({"HOST": "0.0.0.0", "PORT": "80"})
        self.assertEqual("http://0.0.0.0:80", cfg.public_base_url)

    def test_explicit_public_base_url_wins_and_loses_trailing_slash(self):
        cfg = config.load({"PUBLIC_BASE_URL": "https://example.test/"})
        self.assertEqual("https://example.test", cfg.public_base_url)
```

The trailing-slash case matters: every URL in the codebase is built as
`base + "/s/" + token`, so a stray slash would produce `//s/`.

- [ ] **Step 2: Run to verify it fails**

```bash
cd server && python3 -m unittest tests.test_config -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'config'`.

- [ ] **Step 3: Write the implementation**

`server/config.py`:

```python
"""Runtime configuration, read from the environment.

Pure: no I/O beyond reading a mapping. `load()` takes the mapping as an
argument so tests can pass a dict instead of mutating os.environ.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    public_base_url: str


def load(env=None) -> Config:
    env = os.environ if env is None else env
    host = env.get("HOST", "127.0.0.1")
    port = int(env.get("PORT", "8787"))
    public = env.get("PUBLIC_BASE_URL") or f"http://{host}:{port}"
    return Config(
        host=host,
        port=port,
        db_path=env.get("DB_PATH", "doctrine.db"),
        public_base_url=public.rstrip("/"),
    )
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd server && python3 -m unittest tests.test_config -v
```

Expected: 4 tests, OK.

- [ ] **Step 5: Wire it into the server**

In `server/server.py`, replace lines 29-30:

```python
HOST, PORT = "127.0.0.1", 8787
DB_PATH = "doctorine.db"
```

with:

```python
import config

CFG = config.load()
```

Then update the three places that used the old constants:

- `db()` — `sqlite3.connect(DB_PATH)` becomes `sqlite3.connect(CFG.db_path)`
- the `__main__` block — `HOST`/`PORT` become `CFG.host`/`CFG.port`, and
  the printed URLs become `CFG.public_base_url`
- `api_suggestion` — handled in Task 4, leave it for now

- [ ] **Step 6: Run the whole suite**

```bash
cd server && python3 -m unittest discover -s tests -t . -v
```

Expected: 6 tests, OK. The smoke tests from Task 2 now pass.

- [ ] **Step 7: Commit**

```bash
git add server/config.py server/server.py server/tests/test_config.py
git commit -m "feat: read host, port, db path and public base URL from env"
```

---

## Task 4: Fix the hardcoded tracking URL

`api_suggestion` currently returns `f"http://{HOST}:{PORT}/s/{token}"`.
On a hosted server that hands every student a link to **their own**
`127.0.0.1`. This is a bug, not just a config gap.

**Files:**
- Create: `server/tests/test_api.py`
- Modify: `server/server.py` (`api_suggestion`, ~line 553)

- [ ] **Step 1: Write the failing test**

`server/tests/test_api.py`:

```python
import json
import unittest
import urllib.error
import urllib.request

from tests.helpers import running_server

PUBLIC = "https://doctrine.example"


def post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def register_one(base, doctrine_id="doc-test000001"):
    return post(base, "/api/dev/register", {"notes": [{
        "doctrine_id": doctrine_id,
        "anki_note_id": 1,
        "note_type": "Basic",
        "deck": "Test",
        "fields": {"Front": "Q", "Back": "A"},
        "question_html": "Q",
        "answer_html": "A",
        "css": "",
    }]})


class TrackingUrlTest(unittest.TestCase):
    def test_tracking_url_uses_public_base_url(self):
        with running_server(PUBLIC_BASE_URL=PUBLIC) as base:
            register_one(base)
            _, body = post(base, "/api/suggestions", {
                "doctrine_id": "doc-test000001",
                "text": "The dose is wrong.",
                "suggestion_type": "incorrect",
                "snapshot": {"fields": {"Front": "Q", "Back": "A"}},
            })
            self.assertTrue(
                body["tracking_url"].startswith(PUBLIC + "/s/"),
                f"leaked a local URL: {body['tracking_url']}",
            )
            self.assertNotIn("127.0.0.1", body["tracking_url"])


class UnknownIdTest(unittest.TestCase):
    def test_unregistered_id_is_rejected(self):
        with running_server() as base:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post(base, "/api/suggestions", {
                    "doctrine_id": "doc-spoofed0001",
                    "text": "hello",
                    "snapshot": {},
                })
            self.assertEqual(404, ctx.exception.code)
```

The second test pins the security property the whole design rests on:
the server, not the add-on, is the gate that rejects cards from decks
that are not ours.

> **Note:** this test already uses the post-rename key `doctrine_id`.
> It will fail on the key name until Task 6. That is intended — Task 6's
> verification is that these tests go green without being edited.

- [ ] **Step 2: Run to verify it fails**

```bash
cd server && python3 -m unittest tests.test_api -v
```

Expected: both FAIL — the tracking URL contains `127.0.0.1`, and the
register call does not recognise `doctrine_id`.

- [ ] **Step 3: Fix the tracking URL**

In `api_suggestion`, replace:

```python
    return 200, {"ok": True,
                 "tracking_url": f"http://{HOST}:{PORT}/s/{token}"}
```

with:

```python
    return 200, {"ok": True,
                 "tracking_url": f"{CFG.public_base_url}/s/{token}"}
```

- [ ] **Step 4: Run again**

```bash
cd server && python3 -m unittest tests.test_api -v
```

Expected: still FAIL, but now only on the `doctrine_id` key — the
`127.0.0.1` assertion passes. Task 6 closes the rest.

- [ ] **Step 5: Commit**

```bash
git add server/server.py server/tests/test_api.py
git commit -m "fix: build tracking URLs from PUBLIC_BASE_URL, not the bind address

A hosted server was handing every student a link to their own localhost."
```

---

## Task 5: The `/where` bootstrap endpoint

**Files:**
- Create: `server/tests/test_where.py`
- Modify: `server/server.py` (`do_GET`, ~line 630)

- [ ] **Step 1: Write the failing test**

`server/tests/test_where.py`:

```python
import json
import unittest
import urllib.request

from tests.helpers import running_server


class WhereTest(unittest.TestCase):
    def test_reports_the_public_base_url(self):
        with running_server(PUBLIC_BASE_URL="https://doctrine.example") as base:
            with urllib.request.urlopen(base + "/where") as r:
                self.assertEqual(200, r.status)
                self.assertEqual("application/json", r.headers["Content-Type"])
                body = json.loads(r.read())
        self.assertEqual("https://doctrine.example", body["api_base"])
        self.assertIn("min_addon_version", body)

    def test_is_cacheable(self):
        with running_server() as base:
            with urllib.request.urlopen(base + "/where") as r:
                self.assertIn("max-age", r.headers.get("Cache-Control", ""))
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd server && python3 -m unittest tests.test_where -v
```

Expected: FAIL — HTTP 404.

- [ ] **Step 3: Implement**

Add near the other constants in `server/server.py`:

```python
MIN_ADDON_VERSION = "1.0"
```

Extend `Handler._send` to accept extra headers:

```python
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)
```

Add the route as the **first** branch of `do_GET`, before `/`:

```python
        if path == "/where":
            self._send(
                200,
                json.dumps({
                    "api_base": CFG.public_base_url,
                    "min_addon_version": MIN_ADDON_VERSION,
                }),
                "application/json",
                {"Cache-Control": "public, max-age=3600"},
            )
        elif path in ("/", "/reviewer"):
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd server && python3 -m unittest tests.test_where -v
```

Expected: 2 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add server/server.py server/tests/test_where.py
git commit -m "feat: add GET /where bootstrap endpoint"
```

---

## Task 6: Rename — server side

**Files:**
- Modify: `server/server.py` (identifiers, schema, page titles)

The prototype database is **dropped**, not migrated. No real deck has
shipped, so there is nothing to preserve. Doing this after a deck reaches
students would require a genuine migration on both the DB and every
installed collection.

- [ ] **Step 1: Confirm the tests currently fail on the old names**

```bash
cd server && python3 -m unittest discover -s tests -t . -v
```

Expected: the `test_api` tests fail on `doctrine_id`. Everything else passes.

- [ ] **Step 2: Apply the rename**

```bash
cd server
sed -i '' \
  -e 's/doctorine_id/doctrine_id/g' \
  -e 's/DoctorineID/DoctrineID/g' \
  -e 's/doctorine\.db/doctrine.db/g' \
  -e 's/Doctorine Suggestions/Doctrine Editor/g' \
  -e 's/Doctorine/Doctrine/g' \
  server.py
grep -in "doctorine" server.py; echo "remaining=$?"
```

Expected: no output from `grep`, `remaining=1`.

- [ ] **Step 3: Drop the prototype database**

```bash
rm -f server/doctrine.db server/doctorine.db
```

- [ ] **Step 4: Run the full suite**

```bash
cd server && python3 -m unittest discover -s tests -t . -v
```

Expected: **all tests pass, with no test file edited.** The Task 4 tests
were written against the new names precisely so this step proves the
rename is complete rather than merely plausible.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: rename Doctorine -> Doctrine across the server

Drops the prototype DB rather than migrating it; no deck has shipped."
```

---

## Task 7: Rename — add-on side

**Files:**
- Modify: `addon/__init__.py`, `addon/config.json`, `addon/manifest.json`

- [ ] **Step 1: Apply the rename**

```bash
sed -i '' \
  -e 's/DoctorineID/DoctrineID/g' \
  -e 's/doctorine_id/doctrine_id/g' \
  -e 's/doctorine-suggest-btn/doctrine-suggest-btn/g' \
  -e 's/doctorine_suggest/doctrine_editor/g' \
  -e 's/Doctorine Suggestions/Doctrine Editor/g' \
  -e 's/Doctorine/Doctrine/g' \
  addon/__init__.py addon/config.json addon/manifest.json
grep -rin "doctorine" addon/; echo "remaining=$?"
```

Expected: no output, `remaining=1`.

- [ ] **Step 2: Update the button label**

In `addon/__init__.py`, the button text `&#9998; Suggest Edit on Doctrine`
becomes `&#9998; Suggest an edit`.

- [ ] **Step 3: Verify the file still parses**

The add-on cannot be imported without Anki, so check syntax only:

```bash
python3 -m py_compile addon/__init__.py && echo "syntax ok"
```

Expected: `syntax ok`.

- [ ] **Step 4: Verify the hash functions are still identical**

```bash
diff <(sed -n '/^def content_hash/,/return hashlib/p' addon/__init__.py) \
     <(sed -n '/^def content_hash/,/return hashlib/p' server/server.py)
```

Expected: only the signature line differs (the server's has a default
argument). **Any difference in the body is a bug** — staleness detection
depends on both sides producing byte-identical hashes.

- [ ] **Step 5: Commit**

```bash
git add addon && git commit -m "refactor: rename add-on to Doctrine Editor, DoctorineID -> DoctrineID"
```

---

## Task 8: The add-on's API-base resolver

**Files:**
- Create: `addon/resolver.py`
- Create: `addon/tests/__init__.py` (empty)
- Create: `addon/tests/test_resolver.py`

- [ ] **Step 1: Write the failing test**

`addon/tests/test_resolver.py`:

```python
import unittest

from resolver import DAY, resolve_api_base

BOOTSTRAP = "https://bootstrap.example"
LIVE = "https://api.example"


def never_called(url):
    raise AssertionError(f"should not have fetched {url}")


class ResolveTest(unittest.TestCase):
    def test_override_wins_and_skips_the_network(self):
        cache = {}
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP, "api_base_override": LIVE},
            cache, fetch=never_called, now=lambda: 0,
        )
        self.assertEqual(LIVE, base)
        self.assertEqual({}, cache, "override must not populate the cache")

    def test_fresh_cache_skips_the_network(self):
        cache = {"api_base": LIVE, "fetched_at": 100}
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, cache,
            fetch=never_called, now=lambda: 100 + DAY - 1,
        )
        self.assertEqual(LIVE, base)

    def test_stale_cache_refetches_and_updates(self):
        cache = {"api_base": "https://old.example", "fetched_at": 0}
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, cache,
            fetch=lambda url: {"api_base": LIVE}, now=lambda: DAY + 1,
        )
        self.assertEqual(LIVE, base)
        self.assertEqual(LIVE, cache["api_base"])
        self.assertEqual(DAY + 1, cache["fetched_at"])

    def test_fetch_failure_falls_back_to_stale_cache(self):
        cache = {"api_base": LIVE, "fetched_at": 0}

        def boom(url):
            raise OSError("network down")

        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, cache,
            fetch=boom, now=lambda: DAY * 365,
        )
        self.assertEqual(LIVE, base, "a stale cache beats no service at all")

    def test_fetch_failure_with_no_cache_falls_back_to_bootstrap(self):
        def boom(url):
            raise OSError("network down")

        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, {}, fetch=boom, now=lambda: 0,
        )
        self.assertEqual(BOOTSTRAP, base)

    def test_it_fetches_the_where_path(self):
        seen = []
        resolve_api_base(
            {"bootstrap_url": BOOTSTRAP + "/"}, {},
            fetch=lambda url: seen.append(url) or {"api_base": LIVE},
            now=lambda: 0,
        )
        self.assertEqual([BOOTSTRAP + "/where"], seen)

    def test_a_junk_response_falls_back_rather_than_returning_junk(self):
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, {},
            fetch=lambda url: {"unexpected": "shape"}, now=lambda: 0,
        )
        self.assertEqual(BOOTSTRAP, base)
```

The last case matters: a malformed `/where` response must not be able to
redirect student submissions to an empty or attacker-chosen string.

- [ ] **Step 2: Run to verify it fails**

```bash
cd addon && python3 -m unittest discover -s tests -t . -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'resolver'`.

- [ ] **Step 3: Implement**

`addon/resolver.py`:

```python
"""Decide which API base the add-on should POST to.

Pure and Anki-free on purpose: the clock and the HTTP fetcher are
injected, so this is unit-testable without Anki and without a network.

Resolution order:
  1. api_base_override from config (development escape hatch)
  2. a cached value fetched less than a day ago
  3. a fresh GET {bootstrap_url}/where
  4. the cached value, however old, if that fetch fails
  5. the bootstrap URL itself
"""

DAY = 86400


def _valid(base):
    return isinstance(base, str) and base.startswith(("http://", "https://"))


def resolve_api_base(cfg, cache, fetch, now):
    override = (cfg.get("api_base_override") or "").strip()
    if override:
        return override.rstrip("/")

    bootstrap = (cfg.get("bootstrap_url") or "").rstrip("/")
    cached = cache.get("api_base")
    fetched_at = cache.get("fetched_at", 0)

    if _valid(cached) and now() - fetched_at < DAY:
        return cached

    try:
        payload = fetch(bootstrap + "/where")
        base = (payload or {}).get("api_base")
        if _valid(base):
            base = base.rstrip("/")
            cache["api_base"] = base
            cache["fetched_at"] = now()
            return base
    except Exception:
        pass

    if _valid(cached):
        return cached
    return bootstrap
```

The bare `except Exception` is deliberate: any failure to resolve must
degrade to a usable URL rather than raise into Anki's UI thread.

- [ ] **Step 4: Run to verify it passes**

```bash
cd addon && python3 -m unittest discover -s tests -t . -v
```

Expected: 7 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add addon/resolver.py addon/tests
git commit -m "feat: add pure, testable API-base resolver to the add-on"
```

---

## Task 9: Wire the resolver into the add-on

Not unit-testable (needs Anki); verified manually in Task 12.

**Files:**
- Modify: `addon/config.json`, `addon/__init__.py`

- [ ] **Step 1: Rewrite `addon/config.json`**

```json
{
  "bootstrap_url": "http://127.0.0.1:8787",
  "api_base_override": "",
  "id_field": "DoctrineID",
  "button_top_offset": 150,
  "button_right_offset": 12
}
```

`bootstrap_url` becomes the real host in Task 11, once it exists.

- [ ] **Step 2: Update `get_config()` in `addon/__init__.py`**

```python
def get_config():
    cfg = mw.addonManager.getConfig(__name__) or {}
    return {
        "bootstrap_url": cfg.get("bootstrap_url", "http://127.0.0.1:8787").rstrip("/"),
        "api_base_override": cfg.get("api_base_override", ""),
        "id_field": cfg.get("id_field", "DoctrineID"),
        "button_top_offset": cfg.get("button_top_offset", 150),
        "button_right_offset": cfg.get("button_right_offset", 12),
        "_cache": cfg.get("_cache", {}),
    }
```

- [ ] **Step 3: Add the resolution helper**

```python
import time

from . import resolver


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_base(cfg) -> str:
    """Resolve the API base, persisting any refreshed cache.

    Call only from a background thread — it may make a network request.
    """
    cache = dict(cfg.get("_cache") or {})
    base = resolver.resolve_api_base(cfg, cache, fetch=get_json, now=time.time)
    if cache != (cfg.get("_cache") or {}):
        stored = mw.addonManager.getConfig(__name__) or {}
        stored["_cache"] = cache
        mw.addonManager.writeConfig(__name__, stored)
    return base
```

- [ ] **Step 4: Use it at the three call sites**

`open_suggestion_dialog`, `stamp_and_register_deck`, `open_reviewer_page`
and `open_updates_page` currently build URLs from `cfg["server_url"]`.

In the two background tasks, resolve **inside** the task function so the
network call happens off the UI thread:

```python
    def task():
        return post_json(api_base(cfg) + "/api/suggestions", payload)
```

For the two menu items that just open a browser, resolve on the cached
value only — never block a menu click on a network call:

```python
def open_reviewer_page():
    cfg = get_config()
    openLink(resolver.resolve_api_base(
        cfg, dict(cfg.get("_cache") or {}),
        fetch=lambda url: None, now=time.time) + "/reviewer")
```

- [ ] **Step 5: Verify syntax and commit**

```bash
python3 -m py_compile addon/__init__.py && echo "syntax ok"
git add addon && git commit -m "feat: resolve the API base via the bootstrap endpoint"
```

---

## Task 10: Containerise

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY server/ /app/

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/doctrine.db

EXPOSE 8080
CMD ["python3", "server.py"]
```

No `pip install` — the server is stdlib-only. `DB_PATH` points at the
mounted volume so the database survives redeploys.

- [ ] **Step 2: Write `.dockerignore`**

```
.git
.gitignore
docs
addon
**/tests
**/__pycache__
*.db
*.ankiaddon
```

- [ ] **Step 3: Build and run locally**

```bash
docker build -t doctrine-editor . \
  && docker run --rm -d --name doctrine-test -p 8080:8080 \
       -e PUBLIC_BASE_URL=https://doctrine.example \
       -v "$(pwd)/.localdata:/data" doctrine-editor
sleep 2 && curl -s localhost:8080/where
```

Expected: `{"api_base": "https://doctrine.example", "min_addon_version": "1.0"}`

- [ ] **Step 4: Verify the volume actually persists**

```bash
docker restart doctrine-test && sleep 2
ls -la .localdata/
docker rm -f doctrine-test
```

Expected: `doctrine.db` present and non-empty. If the file is missing,
the mount is wrong — fix it now, not on the host.

- [ ] **Step 5: Commit**

```bash
echo ".localdata/" >> .gitignore
git add Dockerfile .dockerignore .gitignore
git commit -m "build: add stdlib-only Dockerfile with SQLite on a mounted volume"
```

---

## Task 11: Deploy to Northflank (owner: Mario)

> **BLOCKER — resolve before starting.** Confirm the free tier includes a
> **persistent volume**. If it does not, stop: the fallback is their free
> managed Postgres, which means porting SQLite → Postgres, and that is a
> separate plan, not a step in this one.

- [ ] **Step 1: Create the service**

Connect the GitHub repo from Task 1. Build from `Dockerfile`, port 8080,
publicly exposed with the generated HTTPS subdomain.

- [ ] **Step 2: Attach a volume**

Mount at `/data`, 1 GB. Without this the database is wiped on every
redeploy.

- [ ] **Step 3: Set the environment variable**

`PUBLIC_BASE_URL` = the generated `https://…` host, **no trailing slash**.
Leave `HOST`, `PORT` and `DB_PATH` at their Dockerfile values.

- [ ] **Step 4: Verify the deployment**

```bash
curl -s https://<host>/where
curl -s -o /dev/null -w "%{http_code}\n" https://<host>/updates
```

Expected: JSON whose `api_base` matches the host exactly, then `200`.

- [ ] **Step 5: Verify persistence on the real host**

Redeploy from the Northflank dashboard, then re-run the `/updates` check.
Any data registered before the redeploy must still be present.

- [ ] **Step 6: Point the add-on at it**

Set `bootstrap_url` in `addon/config.json` to the deployed host, then:

```bash
git add addon/config.json
git commit -m "chore: point the add-on bootstrap at the deployed host"
```

---

## Task 12: End-to-end verification and packaging

- [ ] **Step 1: Run every test**

```bash
(cd server && python3 -m unittest discover -s tests -t . -v) \
  && (cd addon && python3 -m unittest discover -s tests -t . -v)
```

Expected: all green. Do not package until this is true.

- [ ] **Step 2: Repackage the add-on**

```bash
rm -f doctrine_editor.ankiaddon doctorine_suggestions.ankiaddon
cd addon && zip -r ../doctrine_editor.ankiaddon . \
  -x "tests/*" "__pycache__/*" "*.pyc" && cd ..
unzip -l doctrine_editor.ankiaddon
```

Expected: `__init__.py`, `resolver.py`, `config.json`, `manifest.json`.
**No `tests/` directory** — Anki would try to import it.

- [ ] **Step 3: Install into a test Anki profile and re-run the README loop**

Against the **deployed** host, not localhost:

1. Tools → Doctrine Editor → Stamp & register a deck
2. Review a card → ✎ Suggest an edit → send
3. Confirm the returned tracking link starts with `https://<host>` —
   **not** `127.0.0.1`. This is the Task 4 fix proven end to end.
4. Open `https://<host>/reviewer` — the suggestion shows
   "Matches current version"
5. Edit the note, re-register → the badge flips to outdated
6. Resolve with *publish* checked → it appears on `/updates` with credit
7. The tracking link reflects the outcome
8. Suggest on an unregistered deck → the add-on blocks it

- [ ] **Step 4: Confirm the bootstrap indirection actually works**

This is the whole point of P1, so prove it rather than assume it:

1. Change `PUBLIC_BASE_URL` on Northflank to a different value and redeploy
2. In the test profile, clear `_cache` from the add-on's config
3. Send a suggestion — it must POST to the **new** base with no reinstall
4. Set `PUBLIC_BASE_URL` back

- [ ] **Step 5: Commit and push**

```bash
git add -A && git commit -m "chore: package Doctrine Editor add-on for hosted testing"
git push
```

- [ ] **Step 6: Brief the testers**

Send them `doctrine_editor.ankiaddon` and tell them explicitly:

- Install into a **test Anki profile** — stamping alters note types and
  forces a full sync.
- **`/reviewer` has no login yet (P2).** Anyone with the URL can read the
  queue and act on it. Share the host only with the review team, and do
  not put anything sensitive in a suggestion until P2 ships.
- Images inside the card snapshots will not load. Known, deferred (P3 of
  the README TODO list, not in this plan).

---

## Definition of done

- [ ] Repo flattened, `platform/` renamed to `server/`, pushed to GitHub
- [ ] `grep -rin doctorine .` returns nothing outside `docs/`
- [ ] All server and add-on tests pass
- [ ] `https://<host>/where` returns the deployed host
- [ ] The database survives a redeploy
- [ ] A suggestion sent from Anki reaches the hosted queue and its
      tracking link uses the public host
- [ ] Changing `PUBLIC_BASE_URL` server-side moves where the add-on posts,
      with no reinstall
- [ ] Testers have the `.ankiaddon` and know `/reviewer` is unauthenticated
