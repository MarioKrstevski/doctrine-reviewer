# Doctrine Editor — Reviewer Authentication (P2) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the reviewer queue behind a login, with an admin who creates
and resets reviewer accounts. No email anywhere.

**Architecture:** A new `auth.py` holds everything that can be tested
without HTTP — password hashing, token minting, session validity, the
login throttle. `server.py` gains a thin guard layer that maps a request
cookie to a user row and refuses or redirects. Public routes are
unchanged.

**Tech Stack:** Python 3.12 stdlib only — `hashlib.scrypt`, `hmac`,
`secrets`, `sqlite3`.

**Covers spec phase:** P2. Spec:
`docs/superpowers/specs/2026-09-01-doctrine-editor-production-design.md`

**Not in scope:** the thank-you outbox (P5) and anything that sends mail.
Submitter emails continue to be captured on the suggestion row and are
untouched by this plan.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `server/auth.py` | **new** — hashing, tokens, session/throttle rules. No HTTP, no rendering. |
| `server/tests/test_auth.py` | **new** — unit tests for the above. |
| `server/tests/test_auth_routes.py` | **new** — end-to-end tests over HTTP. |
| `server/server.py` | schema, guards, login/account/admin routes. |

`auth.py` is separate so the security-critical logic is testable without
a running server. `server.py` keeps only the HTTP wiring.

---

## Task 1: Password hashing and tokens

**Files:** Create `server/auth.py`, `server/tests/test_auth.py`

- [ ] **Step 1: Write failing tests**

Cover: a hash verifies against its own password; a wrong password fails;
the same password hashed twice gives different stored values (per-user
salt); tokens are unique and long; `verify` is total (never raises on
malformed stored values).

- [ ] **Step 2: Run, confirm ModuleNotFoundError**

```bash
cd server && python3.12 -m unittest tests.test_auth -v
```

- [ ] **Step 3: Implement `hash_password`, `verify_password`, `new_token`**

`hashlib.scrypt(n=2**14, r=8, p=1)`, 16-byte salt from `secrets.token_bytes`,
comparison via `hmac.compare_digest`. Store as `scrypt$<salt_hex>$<hash_hex>`.

- [ ] **Step 4: Run, confirm pass. Step 5: Commit.**

---

## Task 2: Session and throttle rules

**Files:** Modify `server/auth.py`, `server/tests/test_auth.py`

- [ ] **Step 1: Write failing tests**

Cover: a session inside its window is valid; one past `expires_at` is not;
the throttle allows the 5th attempt and blocks the 6th; attempts older
than the window do not count; a successful login clears the count.

- [ ] **Step 2-4: Run / implement `session_expiry`, `is_expired`, `too_many_attempts` / run.**

Pure functions taking an explicit `now`, so no test sleeps.

- [ ] **Step 5: Commit.**

---

## Task 3: Schema and the `adduser` CLI

**Files:** Modify `server/server.py`

- [ ] **Step 1: Add tables to `init_db()`**

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'reviewer',   -- admin|reviewer
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    password_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    ok INTEGER,
    attempted_at TEXT
);
```

Plus `ALTER TABLE suggestions ADD COLUMN resolved_by INTEGER` guarded by a
check against `PRAGMA table_info`, since the table already exists in
production.

- [ ] **Step 2: Add the CLI**

`python3 server.py adduser <name> [--admin]` prompts twice for a password
via `getpass`, or generates one with `secrets.token_urlsafe(12)` when run
with `--generate`. Prints the password once. Refuses duplicate usernames.

- [ ] **Step 3: Verify against a temp DB. Step 4: Commit.**

---

## Task 4: Login, logout, and the guard

**Files:** Modify `server/server.py`, create `server/tests/test_auth_routes.py`

- [ ] **Step 1: Write failing HTTP tests**

Cover: `/reviewer` unauthenticated redirects to `/login`; a good login
sets a cookie and reaches `/reviewer`; a bad password does not; a
deactivated user cannot log in; logout invalidates the cookie; the
public routes (`/updates`, `/s/<token>`, `/api/suggestions`, `/where`)
still work with no cookie at all.

- [ ] **Step 2-4: Run / implement / run.**

Cookie: `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` whenever
`CFG.public_base_url` starts with `https://` (so local HTTP testing still
works). Failed logins return one generic message.

- [ ] **Step 5: Commit.**

---

## Task 5: CSRF on state-changing forms

**Files:** Modify `server/server.py`, `server/tests/test_auth_routes.py`

- [ ] **Step 1: Write failing tests** — a POST to `/reviewer/action`
      without a valid token is rejected even with a valid session cookie.
- [ ] **Step 2-4: Run / implement / run.**

Token derived from the session token via HMAC, embedded as a hidden field,
compared with `hmac.compare_digest`.

- [ ] **Step 5: Commit.**

---

## Task 6: `/account` and `/admin/users`

**Files:** Modify `server/server.py`, `server/tests/test_auth_routes.py`

- [ ] **Step 1: Write failing tests**

Cover: a reviewer cannot reach `/admin/users` (403); an admin can; creating
a reviewer returns a password shown once; a password reset invalidates that
user's existing sessions immediately; deactivation invalidates them too;
`/account` changes own password only with the correct current password;
the last active admin cannot deactivate themselves.

- [ ] **Step 2-4: Run / implement / run.**

- [ ] **Step 5: Commit.**

---

## Task 7: Record who acted

**Files:** Modify `server/server.py`

- [ ] **Step 1: Test** that resolving a suggestion stores the acting user's id.
- [ ] **Step 2-4: Run / implement / run.** Show the username in the reviewer
      queue item meta. The public `/updates` ledger keeps crediting the
      *submitter* and must not name the reviewer.
- [ ] **Step 5: Commit.**

---

## Task 8: Deploy and verify

- [ ] Run the full suite.
- [ ] Deploy; create the admin account against the live DB.
- [ ] Verify `/reviewer` redirects to `/login` when logged out, that login
      works, and that `/updates`, `/s/<token>` and `/api/suggestions` are
      still reachable with no cookie.

---

## Definition of done

- [ ] `/reviewer`, `/reviewer/action`, `/account`, `/admin/users` require a session
- [ ] `/updates`, `/s/<token>`, `/api/suggestions`, `/where` remain public
- [ ] Admin can create, reset and deactivate reviewers; reviewers can only
      change their own password
- [ ] Reset or deactivation kills live sessions immediately
- [ ] Deactivated users keep their history; nothing is deleted
- [ ] No email is sent anywhere
