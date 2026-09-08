#!/usr/bin/env python3
"""
Doctrine Editor platform — prototype server (stdlib only).

Run:  python3 server.py           (listens on http://127.0.0.1:8787)

Pages:
  /reviewer   internal queue: snapshot | current master | suggestion,
              with an "outdated" badge when the master card has changed
              since the student submitted.
  /updates    public changelog ("errata ledger") of accepted suggestions.
  /s/<token>  public status page for one suggestion (link the student gets).

API:
  POST /api/suggestions    from the Anki add-on
  POST /api/dev/register   from the add-on's "Stamp & register a deck" tool
  POST /reviewer/action    resolve / decline / expire (+ optional publish)
"""

import hashlib
import html
import json
import sqlite3
import uuid
from datetime import datetime, timezone
import hmac
import os
import re
import socketserver

import auth
import email_out
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config

CFG = config.load()
MIN_ADDON_VERSION = "1.0"
# Identity is the note guid (the deck pipeline sets it to the card's
# database id). Nothing is stamped. This legacy field name is only
# excluded from content hashing, mirroring the add-on, so a profile
# stamped by an older build hashes identically to a clean import.
ID_FIELD = "DoctrineID"

TYPE_LABELS = {
    "typo": "Typo",
    "incorrect": "Incorrect",
    "confusing": "Confusing",
    "other": "Other",
}


# ---------------------------------------------------------------- db

class _Connection(sqlite3.Connection):
    """sqlite3's context manager commits or rolls back but never closes.

    A lingering connection keeps its lock until garbage collection, which
    under a threaded server means writers stall on ghosts. Close on exit.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


# How long a connection waits for a lock before giving up. The default is
# 5s; a 100-note registration batch can hold the write lock longer than
# that, which is exactly what took the login path down in production.
DB_BUSY_TIMEOUT = 30


def db():
    conn = sqlite3.connect(CFG.db_path, timeout=DB_BUSY_TIMEOUT,
                           factory=_Connection)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn, table, wanted):
    """ALTER TABLE ... ADD COLUMN for each column in `wanted` that is absent."""
    present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, kind in wanted.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def init_db():
    # A container whose volume has not attached yet has no /data, and
    # SQLite cannot create a file in a directory that does not exist.
    # Create it rather than crash-loop -- but say so loudly, because a
    # mounted volume always exists already: if we had to create it, this
    # process is probably writing to ephemeral storage that will be lost.
    parent = os.path.dirname(os.path.abspath(CFG.db_path))
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        print(
            f"WARNING: created {parent} -- it did not exist. If this path "
            f"should be a mounted volume, DATA WILL BE LOST on restart.",
            flush=True,
        )

    # WAL: readers never wait for the writer and the writer never waits
    # for readers. Persistent in the file, so setting it once here covers
    # every later connection. synchronous=NORMAL is durable under WAL for
    # everything except power loss mid-checkpoint, which is acceptable
    # for a suggestion queue.
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            doctrine_id TEXT PRIMARY KEY,
            anki_note_id INTEGER,
            note_type TEXT,
            deck TEXT,
            fields_json TEXT,
            tags_json TEXT,
            content_hash TEXT,
            version INTEGER DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE,
            doctrine_id TEXT,
            anki_note_id INTEGER,
            note_type TEXT,
            deck TEXT,
            suggestion_type TEXT,
            text TEXT,
            email TEXT,
            snap_question TEXT,
            snap_answer TEXT,
            snap_css TEXT,
            snap_fields_json TEXT,
            snap_hash TEXT,
            status TEXT DEFAULT 'open',     -- open|resolved|declined|expired
            resolution_note TEXT,
            published INTEGER DEFAULT 0,
            publish_summary TEXT,
            credit_name TEXT,
            created_at TEXT,
            closed_at TEXT
        );
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
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_id INTEGER NOT NULL UNIQUE,
            email TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'thank_you',
            status TEXT NOT NULL DEFAULT 'pending',   -- pending|thanked|skipped
            created_at TEXT,
            actioned_at TEXT,
            actioned_by INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_username
            ON login_attempts(username, attempted_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)

        # suggestions predates several features in production, so columns
        # are added in place when missing rather than recreating the table.
        _ensure_columns(conn, "suggestions", {
            "resolved_by": "INTEGER",
            # P3 trace data
            "tags_json": "TEXT",
            "note_mod": "INTEGER",
            "card_id": "INTEGER",
            "card_ord": "INTEGER",
            "template_name": "TEXT",
            "deck_id": "INTEGER",
            "original_deck_id": "INTEGER",
            "install_id": "TEXT",
            "addon_version": "TEXT",
            "anki_version": "TEXT",
        })

        # Master copies were once stored as rendered card HTML plus a per-row
        # copy of the note-type CSS: 47 KB per note against ~500 bytes of
        # content, 2 GB for the real deck. Drop those columns in place; the
        # fields are the record now.
        note_cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
        rebuilt = False
        if note_cols & {"question_html", "answer_html", "css"}:
            # Rebuild rather than DROP COLUMN: each DROP rewrites the whole
            # table, and at 2 GB three of them plus a VACUUM blocked boot
            # for minutes. One SELECT of the surviving columns into a fresh
            # table is a single pass.
            print("Migrating master storage to fields-only...", flush=True)
            tags_src = "tags_json" if "tags_json" in note_cols else "NULL"
            conn.executescript(f"""
                CREATE TABLE notes_new (
                    doctrine_id TEXT PRIMARY KEY,
                    anki_note_id INTEGER,
                    note_type TEXT,
                    deck TEXT,
                    fields_json TEXT,
                    tags_json TEXT,
                    content_hash TEXT,
                    version INTEGER DEFAULT 1,
                    updated_at TEXT
                );
                INSERT INTO notes_new
                    SELECT doctrine_id, anki_note_id, note_type, deck,
                           fields_json, {tags_src}, content_hash, version,
                           updated_at
                    FROM notes;
                DROP TABLE notes;
                ALTER TABLE notes_new RENAME TO notes;
            """)
            rebuilt = True
        elif "tags_json" not in note_cols:
            conn.execute("ALTER TABLE notes ADD COLUMN tags_json TEXT")

    if rebuilt:
        # Reclaim the space the dropped table occupied. One-off, at boot.
        with db() as conn:
            conn.execute("VACUUM")
        print("  done: %.0f MB" % (os.path.getsize(CFG.db_path) / 1e6), flush=True)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def content_hash(fields: dict, id_field: str = ID_FIELD) -> str:
    parts = []
    for name in sorted(fields.keys()):
        if name == id_field:
            continue
        parts.append(f"{name}\x1f{fields[name]}")
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- html helpers

def esc(s):
    return html.escape(str(s or ""), quote=True)


_CLOZE = re.compile(r"\{\{c\d+::.*?\}\}", re.S)


def tag_chips(tags_json):
    try:
        tags = json.loads(tags_json or "[]")
    except ValueError:
        tags = []
    if not tags:
        return ""
    chips = "".join(f'<span class="chip" title="{esc(t)}">{esc(t.split("::")[-1])}</span>'
                    for t in tags)
    return f'<div class="chips">{chips}</div>'


def trace_block(r):
    """Collapsible identifiers for finding and reproducing the card.

    Only present values are listed, so an older add-on that sent none of
    this produces an empty block rather than a row of 'None'.
    """
    ord_ = r["card_ord"]
    is_cloze = (r["template_name"] or "").lower() == "cloze"
    if ord_ is not None and is_cloze:
        ord_label, ord_value = "cloze", f"c{ord_ + 1}"   # Anki ords are 0-based
    else:
        ord_label, ord_value = "card no.", ord_
    modified = r["note_mod"]
    if isinstance(modified, (int, float)):
        modified = datetime.fromtimestamp(modified, timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    pairs = [
        ("card", r["card_id"]),
        (ord_label, ord_value),
        ("template", r["template_name"]),
        ("deck id", r["deck_id"]),
        ("original deck", r["original_deck_id"]),
        ("note modified", modified),
        ("install", (r["install_id"] or "")[:8] or None),
        ("add-on", r["addon_version"]),
        ("anki", r["anki_version"]),
    ]
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{esc(str(v))}</dd>"
                   for k, v in pairs if v not in (None, ""))
    if not rows:
        return ""
    return (f'<details class="trace"><summary>Trace</summary>'
            f'<dl>{rows}</dl></details>')


def fields_pane(fields_json, label):
    """The master copy as field source, which is what a reviewer edits.

    Values are escaped and shown verbatim -- HTML tags, cloze markers and
    all -- so a reviewer sees exactly the text they will change in Anki.
    Cloze deletions are highlighted; empty fields are omitted.
    """
    try:
        fields = json.loads(fields_json or "{}")
    except ValueError:
        fields = {}
    rows = []
    for name, value in fields.items():
        if not (value or "").strip():
            continue
        safe = esc(value)
        safe = _CLOZE.sub(lambda m: f"<mark>{m.group(0)}</mark>", safe)
        rows.append(f'<dt>{esc(name)}</dt><dd>{safe}</dd>')
    body = "".join(rows) or "<i>(no content)</i>"
    return (f'<div class="pane"><div class="pane-label">{esc(label)}</div>'
            f'<dl class="fields">{body}</dl></div>')


def card_iframe(q_html, a_html, css, label):
    """Render a card snapshot inside a sandboxed iframe via srcdoc."""
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<style>body{{font-family:system-ui,sans-serif;padding:18px;margin:0;
background:#fff;color:#15201B;font-size:15px;line-height:1.5}}
hr{{border:none;border-top:1px solid #E4E1D8;margin:16px 0}}
img{{max-width:100%}}
::-webkit-scrollbar{{width:10px;height:10px}}
::-webkit-scrollbar-thumb{{background:#D6D2C6;border-radius:5px;
border:2px solid #fff}}
::-webkit-scrollbar-track{{background:transparent}}
{css or ""}</style></head><body>
{q_html or "<i>(empty)</i>"}<hr>{a_html or "<i>(empty)</i>"}
</body></html>"""
    return (f'<div class="pane"><div class="pane-label">{esc(label)}</div>'
            f'<iframe sandbox="" srcdoc="{esc(doc)}"></iframe></div>')


BASE_CSS = """
:root {
  --paper:#F6F4EE; --card:#FFFFFF; --panel:#FAF9F4;
  --ink:#1E2A25; --muted:#5F6B66; --faint:#89938D;
  --line:#E4E1D8; --line-strong:#CDC9BD;
  --accent:#0E5F50; --accent-ink:#0A4A3F;
  --accent-soft:#E3EFE9; --accent-border:#C2DAD1;
  --warn:#8A5800; --warn-soft:#F6EDD7; --warn-border:#E3D1A6;
  --bad:#943030; --bad-soft:#F5E4E1; --bad-border:#E1C1BB;
  --dim-soft:#ECEAE2; --code-bg:#F1EFE8;
  --serif:"Source Serif 4", Georgia, "Iowan Old Style", serif;
  --sans:"Source Sans 3", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono:ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --shadow:0 1px 2px rgba(30,42,37,.05), 0 6px 18px rgba(30,42,37,.05);
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--paper); color:var(--ink);
  font:15px/1.55 var(--sans);
  -webkit-font-smoothing:antialiased; }
::selection { background:var(--accent-soft); color:var(--accent-ink); }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px;
  border-radius:3px; }
a { color:var(--accent-ink); text-decoration-thickness:1px;
  text-underline-offset:3px; }
a:hover { color:var(--accent); }
code { font-family:var(--mono); font-size:12px; background:var(--code-bg);
  padding:2px 6px; border-radius:4px; color:var(--ink);
  letter-spacing:-.01em; }
.wrap { max-width:1240px; margin:0 auto; padding:30px 24px 72px; }
header.site { background:var(--card); border-bottom:2px solid var(--ink); }
header.site .wrap { display:flex; align-items:baseline; flex-wrap:wrap;
  gap:10px 16px; padding:20px 24px 15px; }
.brand { font-family:var(--serif); font-size:23px; font-weight:600;
  letter-spacing:.005em; }
.brand small { color:var(--faint); font-family:var(--sans); font-weight:500;
  font-size:11px; margin-left:10px; letter-spacing:.14em;
  text-transform:uppercase; }
nav { margin-left:auto; }
nav a { margin-left:22px; font-size:13.5px; font-weight:500;
  color:var(--muted); text-decoration:none; padding-bottom:5px;
  transition:color .15s ease-out; }
nav a:hover { color:var(--ink); }
nav a.active { color:var(--accent-ink);
  box-shadow:inset 0 -2px 0 var(--accent); }
h1 { font-family:var(--serif); font-weight:600; font-size:31px;
  letter-spacing:-.01em; line-height:1.2; margin:22px 0 8px;
  text-wrap:balance; }
.sub { color:var(--muted); margin:0 0 8px; max-width:62ch; font-size:15px; }
.queue-count { color:var(--faint); font-size:13px; margin:0 0 26px;
  font-variant-numeric:tabular-nums; }
.queue-count b { color:var(--accent-ink); font-weight:600; }
.badge { display:inline-flex; align-items:center; padding:3px 10px;
  border-radius:999px; font-size:11px; font-weight:600;
  letter-spacing:.05em; text-transform:uppercase;
  border:1px solid transparent; white-space:nowrap; }
.badge.ok   { background:var(--accent-soft); color:var(--accent-ink);
  border-color:var(--accent-border); }
.badge.warn { background:var(--warn-soft); color:var(--warn);
  border-color:var(--warn-border); }
.badge.bad  { background:var(--bad-soft); color:var(--bad);
  border-color:var(--bad-border); }
.badge.dim  { background:var(--dim-soft); color:var(--muted);
  border-color:var(--line-strong); }
.card { background:var(--card); border:1px solid var(--line);
  border-radius:10px; margin:0 0 26px; overflow:hidden;
  box-shadow:var(--shadow); }
.card-head { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center;
  padding:13px 18px; border-bottom:1px solid var(--line); font-size:13px; }
.card-head .meta { color:var(--muted); overflow-wrap:anywhere; }
.card-head .meta b { color:var(--ink); font-weight:600; }
.card-head time { margin-left:auto; color:var(--faint); font-size:12.5px;
  font-variant-numeric:tabular-nums; white-space:nowrap; }
.panes { display:grid; grid-template-columns:1.05fr 1.05fr .9fr; gap:0; }
.pane { border-right:1px solid var(--line); min-width:0;
  display:flex; flex-direction:column; }
.pane:last-child { border-right:none; }
.pane-label { display:flex; align-items:center; gap:10px;
  font-size:10.5px; font-weight:600; text-transform:uppercase;
  letter-spacing:.11em; color:var(--faint); padding:9px 14px;
  border-bottom:1px solid var(--line); background:var(--panel); }
.pane-label .badge { margin-left:auto; letter-spacing:.04em; }
.pane.stale .pane-label { background:var(--warn-soft); color:var(--warn); }
.pane .fields { margin:0; padding:14px 16px; height:270px; overflow:auto;
  font-size:13px; line-height:1.5; background:#fff; }
.pane .fields dt { font-weight:600; color:var(--muted); font-size:11px;
  letter-spacing:.04em; text-transform:uppercase; margin-top:10px; }
.pane .fields dt:first-child { margin-top:0; }
.pane .fields dd { margin:2px 0 0; white-space:pre-wrap; word-break:break-word;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.pane .fields mark { background:#fff1a8; padding:0 2px; border-radius:2px; }
.count { display:inline-block; min-width:18px; padding:0 6px; margin-left:4px;
  border-radius:9px; background:var(--accent, #2f6f4f); color:#fff;
  font-size:11px; text-align:center; line-height:18px; }
.outbox-body { display:grid; grid-template-columns:1fr 1fr; gap:0; }
.outbox-col { padding:12px 16px; min-width:0; }
.outbox-col + .outbox-col { border-left:1px solid var(--line); }
.outbox-col blockquote { margin:8px 0; padding:10px 12px; background:#f7f7f4;
  border-left:3px solid #cfd6cf; white-space:pre-wrap; }
.outbox-col label.small { display:block; font-size:11px; color:var(--muted);
  margin-top:8px; text-transform:uppercase; letter-spacing:.04em; }
.outbox-col .copyable { width:100%; box-sizing:border-box; font-size:13px;
  font-family:inherit; padding:6px 8px; border:1px solid #ddd; border-radius:4px;
  background:#fff; }
.outbox-col textarea.copyable { resize:vertical; }
.copy-btn { margin-top:6px; }
.badge.substantial { background:#e5edf7; color:#2a4a6f; }
.badge.brief { background:#eee; color:#666; }
.chips { display:flex; flex-wrap:wrap; gap:6px; padding:8px 16px 0; }
.chip { font-size:11px; padding:2px 8px; border-radius:10px; background:#eef1ee;
  color:#3a4a40; border:1px solid #dfe5df; white-space:nowrap; }
.trace { padding:6px 16px; font-size:12px; color:var(--muted); }
.trace summary { cursor:pointer; user-select:none; }
.trace dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 14px;
  margin:6px 0 0; }
.trace dt { color:var(--muted); }
.trace dd { margin:0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.pane iframe { width:100%; height:270px; border:none; display:block;
  background:#fff; }
.pane.suggestion { background:#F8FAF7; }
.pane .sugg-body { padding:15px 16px; font-size:14.5px; line-height:1.6;
  white-space:pre-wrap; overflow-wrap:anywhere; flex:1; }
.pane .sugg-empty { color:var(--faint); font-style:italic; }
.actions { padding:13px 18px; border-top:1px solid var(--line);
  background:var(--panel); font-size:13px; color:var(--muted); }
.actions form { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.actions .grow { flex:1 1 200px; }
.actions .pub-fields { display:flex; flex-wrap:wrap; gap:10px;
  align-items:center; }
.actions .pub-fields.off { display:none; }
.actions .btns { display:flex; gap:8px; margin-left:auto; }
.stale-hint { margin:0 0 10px; color:var(--warn); font-size:13px;
  font-style:italic; }
input[type=text], input[type=email] { border:1px solid var(--line-strong);
  border-radius:6px; padding:7px 10px; font-size:13px; font-family:var(--sans);
  color:var(--ink); background:var(--card); min-width:0;
  transition:border-color .15s ease-out, box-shadow .15s ease-out; }
input[type=text]::placeholder, input[type=email]::placeholder {
  color:var(--faint); }
input[type=text]:hover, input[type=email]:hover {
  border-color:var(--faint); }
input[type=text]:focus, input[type=email]:focus { outline:none;
  border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
input[type=checkbox] { accent-color:var(--accent); width:15px; height:15px; }
button { border:1px solid var(--accent-ink); background:var(--accent-ink);
  color:#fff; border-radius:6px; padding:7px 14px; font-size:13px;
  font-weight:500; font-family:var(--sans); cursor:pointer;
  transition:background .15s ease-out, border-color .15s ease-out,
    color .15s ease-out; }
button:hover { background:var(--accent); border-color:var(--accent); }
button.secondary { background:transparent; color:var(--ink);
  border-color:var(--line-strong); }
button.secondary:hover { border-color:var(--ink); background:transparent;
  color:var(--ink); }
button.ghost { background:transparent; color:var(--muted);
  border-color:transparent; }
button.ghost:hover { background:var(--dim-soft); border-color:transparent;
  color:var(--ink); }
label.chk { display:inline-flex; gap:7px; align-items:center;
  color:var(--muted); cursor:pointer; user-select:none; }
.empty { padding:56px 32px; color:var(--muted); text-align:center;
  background:var(--card); border:1px dashed var(--line-strong);
  border-radius:10px; }
.empty b { display:block; font-family:var(--serif); font-size:18px;
  font-weight:600; color:var(--ink); margin-bottom:6px; }
@media (max-width:960px){
  .panes { grid-template-columns:1fr; }
  .pane { border-right:none; border-bottom:1px solid var(--line); }
  .pane:last-child { border-bottom:none; }
  .card-head time { margin-left:0; width:100%; }
  .actions .btns { margin-left:0; width:100%; }
  .actions .btns button { flex:1; }
}
"""

LEDGER_CSS = """
.ledger { border-top:2px solid var(--ink); margin-top:10px; }
.entry { display:grid; grid-template-columns:130px 1fr auto;
  gap:20px; padding:18px 6px; border-bottom:1px solid var(--line);
  align-items:baseline; }
.entry .stamp { font-family:var(--serif); font-size:13px;
  color:var(--faint); font-variant-numeric:tabular-nums; }
.entry .stamp b { display:block; color:var(--ink); font-size:15px;
  font-weight:600; }
.entry .what { font-size:15.5px; line-height:1.55; }
.entry .what .kind { font-size:10.5px; font-weight:600;
  letter-spacing:.09em; text-transform:uppercase; color:var(--accent-ink);
  margin-right:10px; }
.entry .credit { font-size:13px; color:var(--muted); font-style:italic;
  white-space:nowrap; }
.ledger .empty { border:none; background:transparent; text-align:left;
  padding:44px 6px; font-style:italic; }
.status-hero { background:var(--card); border:1px solid var(--line);
  border-radius:10px; box-shadow:var(--shadow); padding:26px 28px;
  margin:0 0 20px; }
.status-hero .verdict { font-family:var(--serif); font-size:24px;
  font-weight:600; margin:0 0 4px; letter-spacing:-.01em; }
.status-hero .verdict.ok { color:var(--accent-ink); }
.status-hero .explain { color:var(--muted); margin:0 0 18px; max-width:56ch; }
.status-hero blockquote { margin:0; padding:14px 18px;
  background:var(--panel); border:1px solid var(--line); border-radius:8px;
  font-size:14.5px; line-height:1.6; white-space:pre-wrap;
  overflow-wrap:anywhere; }
@media (max-width:700px){ .entry { grid-template-columns:1fr; gap:4px; }
  .entry .credit { white-space:normal; } }
"""


def page(title, body, active="", user=None, csrf=""):
    def nav_link(href, label, key):
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'
    admin_link = (nav_link("/admin/users", "Reviewers", "admin")
                  if user and user.get("role") == "admin" else "")
    outbox_link = ""
    if user:
        n = pending_thank_yous()
        count = f' <span class="count">{n}</span>' if n else ""
        cls = ' class="active"' if active == "outbox" else ""
        outbox_link = f'<a href="/outbox"{cls}>Outbox{count}</a>'
    account_link = nav_link("/account", "Account", "account") if user else ""
    signed_in = ""
    if user:
        signed_in = (
            f'<form method="post" action="/logout" class="signout">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            f'<span>{esc(user["username"])}</span>'
            f'<button type="submit">Sign out</button></form>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — Doctrine</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Sans+3:ital,wght@0,400..600;1,400&family=Source+Serif+4:ital,opsz,wght@0,8..60,400..700;1,8..60,400..700&display=swap">
<style>{BASE_CSS}{LEDGER_CSS}
.signout {{ display: inline-flex; align-items: center; gap: 8px;
  margin-left: auto; font-size: 12px; color: #777; }}
.signout button {{ padding: 3px 9px; font-size: 12px; cursor: pointer;
  background: transparent; border: 1px solid #bbb; border-radius: 3px;
  color: inherit; }}
header.site .wrap {{ display: flex; align-items: center; gap: 18px; }}
</style></head><body>
<header class="site"><div class="wrap">
  <span class="brand">Doctrine<small>Card quality</small></span>
  <nav>
    {nav_link("/updates", "Community updates", "updates")}
    {nav_link("/install", "Install add-on", "install")}
    {nav_link("/reviewer", "Reviewer queue", "reviewer")}
    {outbox_link}{admin_link}{account_link}
  </nav>{signed_in}
</div></header>
<div class="wrap">{body}</div>
<script>
document.querySelectorAll('.actions input[name=publish]').forEach(cb => {{
  const fields = cb.closest('form').querySelector('.pub-fields');
  if (!fields) return;
  const sync = () => fields.classList.toggle('off', !cb.checked);
  cb.addEventListener('change', sync);
  sync();
}});
</script></body></html>"""


# ---------------------------------------------------------------- pages

RECENT_CLOSED_SHOWN = 50
_IN_CHUNK = 500   # stay well under SQLite's bound-parameter limit


def fetch_masters(conn, doctrine_ids):
    """Master rows for exactly these ids, never the whole table.

    The reviewer page once did SELECT * FROM notes to build this dict:
    every note's fields, rendered HTML and a copy of the note-type CSS,
    on every page view. At 34,704 real notes that was hundreds of MB per
    request and the container was OOM-killed.
    """
    ids = sorted(i for i in doctrine_ids if i)
    masters = {}
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start:start + _IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT * FROM notes WHERE doctrine_id IN ({marks})", chunk):
            masters[r["doctrine_id"]] = r
    return masters


def render_reviewer(user=None, secret=None, session_token=None):
    csrf = auth.csrf_token(session_token, secret) if session_token and secret else ""
    with db() as conn:
        # Every open item, plus a bounded tail of closed ones. Neither
        # query may scale with the size of the deck or of history.
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE status='open' ORDER BY id DESC"
        ).fetchall()
        rows += conn.execute(
            "SELECT * FROM suggestions WHERE status!='open' "
            "ORDER BY id DESC LIMIT ?", (RECENT_CLOSED_SHOWN,)
        ).fetchall()
        masters = fetch_masters(conn, {r["doctrine_id"] for r in rows})

    if not rows:
        body = ('<h1>Reviewer queue</h1><p class="sub">Suggestions from students '
                'land here, next to the card they saw and its current master '
                'version.</p><div class="empty"><b>The queue is clear</b>'
                'When a student clicks &#9998; Suggest inside Anki, their note '
                'arrives here with the exact card they were looking at.</div>')
        return page("Reviewer queue", body, "reviewer", user, csrf)

    items = []
    for r in rows:
        master = masters.get(r["doctrine_id"])
        stale_cls = ""
        if master is None:
            state_badge = '<span class="badge bad">Unknown card</span>'
            master_pane = ('<div class="pane"><div class="pane-label">Current '
                           'master</div><div class="sugg-body sugg-empty">No '
                           'master record for this ID.</div></div>')
            stale = False
        else:
            stale = master["content_hash"] != r["snap_hash"]
            if stale:
                state_badge = (f'<span class="badge warn">Card changed since '
                               f'submission — now v{master["version"]}</span>')
                stale_cls = " stale"
            else:
                state_badge = '<span class="badge ok">Matches current version</span>'
            master_pane = fields_pane(
                master["fields_json"], f'Current master (v{master["version"]})')
            if stale_cls:
                master_pane = master_pane.replace(
                    '<div class="pane">', f'<div class="pane{stale_cls}">', 1)

        status = r["status"]
        status_badge = {
            "open": '<span class="badge ok">Open</span>',
            "resolved": '<span class="badge dim">Resolved</span>',
            "declined": '<span class="badge dim">Declined</span>',
            "expired": '<span class="badge dim">Expired</span>',
        }[status]

        snapshot_pane = card_iframe(
            r["snap_question"], r["snap_answer"], r["snap_css"],
            "What the student saw")

        contact = f' &middot; {esc(r["email"])}' if r["email"] else ""
        sugg_pane = (
            '<div class="pane suggestion"><div class="pane-label">Suggestion'
            f'<span class="badge dim">'
            f'{esc(TYPE_LABELS.get(r["suggestion_type"], r["suggestion_type"]))}'
            f'</span></div>'
            f'<div class="sugg-body">{esc(r["text"])}</div></div>'
        )

        if status == "open":
            expire_hint = ""
            if stale:
                expire_hint = ('<p class="stale-hint">The card already '
                               'changed — if the issue is gone, mark it '
                               'expired.</p>')
            actions = f"""
<div class="actions">{expire_hint}
<form method="post" action="/reviewer/action">
  <input type="hidden" name="csrf" value="{csrf}">
  <input type="hidden" name="id" value="{r['id']}">
  <input type="text" name="resolution_note" class="grow" placeholder="Internal note (optional)">
  <label class="chk"><input type="checkbox" name="publish" value="1"> Publish to updates</label>
  <span class="pub-fields">
    <input type="text" name="publish_summary" style="min-width:250px" placeholder="Public one-liner, e.g. 'Fixed dose on beta-blocker card'">
    <input type="text" name="credit_name" placeholder="Credit as (e.g. Marko S.)">
  </span>
  <span class="btns">
    <button name="do" value="resolve">Resolve</button>
    <button name="do" value="decline" class="secondary">Decline</button>
    <button name="do" value="expire" class="ghost">Mark expired</button>
  </span>
</form></div>"""
        else:
            closed_bits = []
            if r["resolution_note"]:
                closed_bits.append(f"Note: {esc(r['resolution_note'])}")
            if r["published"]:
                closed_bits.append(f"Published: &ldquo;{esc(r['publish_summary'])}&rdquo;")
            if r["closed_at"]:
                closed_bits.append(esc(r["closed_at"]))
            actions = (f'<div class="actions">{" &middot; ".join(closed_bits) or "Closed."}'
                       '</div>')

        items.append(f"""
<div class="card">
  <div class="card-head">
    {status_badge} {state_badge}
    <span class="meta"><b>#{r['id']}</b> &middot; {esc(r['deck'])} &middot;
    {esc(r['note_type'])} &middot; <code>{esc(r['doctrine_id'])}</code>
    {f"&middot; <code>nid:{r['anki_note_id']}</code>" if r['anki_note_id'] else ""}{contact}</span>
    <time>{esc(r['created_at'])}</time>
  </div>
  {tag_chips(r["tags_json"])}
  <div class="panes">{snapshot_pane}{master_pane}{sugg_pane}</div>
  {trace_block(r)}
  {actions}
</div>""")

    n_open = sum(1 for r in rows if r["status"] == "open")
    body = ('<h1>Reviewer queue</h1><p class="sub">Each item pairs the exact '
            'card the student saw with its current master version. If the two '
            'panes differ, the card has already moved on.</p>'
            f'<p class="queue-count"><b>{n_open} open</b> &middot; '
            f'{len(rows) - n_open} closed</p>'
            + "".join(items))
    return page("Reviewer queue", body, "reviewer", user, csrf)


def render_updates():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE published=1 AND status='resolved' "
            "ORDER BY closed_at DESC, id DESC"
        ).fetchall()
        counts = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='resolved' THEN 1 ELSE 0 END) AS accepted "
            "FROM suggestions"
        ).fetchone()

    total = counts["total"] or 0
    accepted = counts["accepted"] or 0

    if rows:
        entries = "".join(f"""
<div class="entry">
  <div class="stamp"><b>#{r['id']}</b>{esc((r['closed_at'] or '').split(' ')[0])}</div>
  <div class="what"><span class="kind">{esc(TYPE_LABELS.get(r['suggestion_type'], 'Fix'))}</span>
    {esc(r['publish_summary'] or r['text'][:120])}</div>
  <div class="credit">{('suggested by ' + esc(r['credit_name'])) if r['credit_name'] else 'community suggestion'}</div>
</div>""" for r in rows)
        ledger = f'<div class="ledger">{entries}</div>'
    else:
        ledger = ('<div class="ledger"><div class="empty">No published updates '
                  'yet — accepted suggestions will appear here.</div></div>')

    body = f"""
<h1>Community updates</h1>
<p class="sub">Every entry below started as a suggestion sent by a student from
inside Anki, was reviewed by our team, and shipped as a card update.
{accepted} of {total} suggestions accepted so far.</p>
{ledger}"""
    return page("Community updates", body, "updates")


def render_status(token):
    with db() as conn:
        r = conn.execute(
            "SELECT * FROM suggestions WHERE token=?", (token,)
        ).fetchone()
    if r is None:
        return page("Not found", "<h1>Suggestion not found</h1>")

    status_text = {
        "open": ("In review", "Our reviewers haven't gotten to this one yet."),
        "resolved": ("Accepted", "Thank you — the card was updated based on your suggestion."),
        "declined": ("Reviewed, not applied", "A reviewer looked at this but decided not to change the card."),
        "expired": ("Already fixed", "The card had already been updated by the time we reviewed this."),
    }[r["status"]]

    verdict_cls = " ok" if r["status"] == "resolved" else ""
    body = f"""
<h1>Your suggestion</h1>
<p class="sub">Sent {esc(r['created_at'])} &middot;
{esc(TYPE_LABELS.get(r['suggestion_type'], ''))} on a card in
&ldquo;{esc(r['deck'])}&rdquo;</p>
<div class="status-hero">
  <p class="verdict{verdict_cls}">{status_text[0]}</p>
  <p class="explain">{status_text[1]}</p>
  <blockquote>{esc(r['text'])}</blockquote>
</div>
<p><a href="/updates">See all community updates &rarr;</a></p>"""
    return page("Your suggestion", body)


# ---------------------------------------------------------------- api

def api_suggestion(data):
    doc_id = (data.get("doctrine_id") or "").strip()
    if not doc_id:
        return 400, {"error": "Missing doctrine_id."}

    with db() as conn:
        master = conn.execute(
            "SELECT doctrine_id FROM notes WHERE doctrine_id=?", (doc_id,)
        ).fetchone()
        if master is None:
            return 404, {"error": "This card is not part of a registered deck."}

        snap = data.get("snapshot") or {}
        text = (data.get("text") or "").strip()
        if not text:
            return 400, {"error": "Suggestion text is required."}

        token = uuid.uuid4().hex[:16]
        tags = data.get("tags")
        conn.execute(
            """INSERT INTO suggestions
               (token, doctrine_id, anki_note_id, note_type, deck,
                suggestion_type, text, email,
                snap_question, snap_answer, snap_css, snap_fields_json,
                snap_hash, created_at,
                tags_json, note_mod, card_id, card_ord, template_name,
                deck_id, original_deck_id, install_id, addon_version,
                anki_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?)""",
            (token, doc_id, data.get("anki_note_id"),
             data.get("note_type"), data.get("deck"),
             data.get("suggestion_type", "other"), text, data.get("email"),
             snap.get("question_html"), snap.get("answer_html"),
             snap.get("css"), json.dumps(snap.get("fields") or {}),
             snap.get("content_hash"), now(),
             json.dumps(list(tags) if isinstance(tags, (list, tuple)) else []),
             data.get("note_mod"), data.get("card_id"), data.get("card_ord"),
             data.get("template_name"), data.get("deck_id"),
             data.get("original_deck_id") or None, data.get("install_id"),
             data.get("addon_version"), data.get("anki_version")),
        )
    return 200, {"ok": True,
                 "tracking_url": f"{CFG.public_base_url}/s/{token}"}


def api_register(data):
    notes = data.get("notes") or []
    registered = updated = 0
    with db() as conn:
        for n in notes:
            doc_id = (n.get("doctrine_id") or "").strip()
            if not doc_id:
                continue
            fields = n.get("fields") or {}
            h = content_hash(fields)
            existing = conn.execute(
                "SELECT content_hash, version FROM notes WHERE doctrine_id=?",
                (doc_id,)
            ).fetchone()
            tags = json.dumps(list(n.get("tags") or []))
            if existing is None:
                conn.execute(
                    """INSERT INTO notes (doctrine_id, anki_note_id, note_type,
                       deck, fields_json, tags_json, content_hash, version,
                       updated_at)
                       VALUES (?,?,?,?,?,?,?,1,?)""",
                    (doc_id, n.get("anki_note_id"), n.get("note_type"),
                     n.get("deck"), json.dumps(fields), tags, h, now()))
                registered += 1
            else:
                bump = existing["content_hash"] != h
                conn.execute(
                    """UPDATE notes SET anki_note_id=?, note_type=?, deck=?,
                       fields_json=?, tags_json=?, content_hash=?,
                       version=version+?, updated_at=?
                       WHERE doctrine_id=?""",
                    (n.get("anki_note_id"), n.get("note_type"), n.get("deck"),
                     json.dumps(fields), tags, h, 1 if bump else 0, now(),
                     doc_id))
                registered += 1
                if bump:
                    updated += 1
    return 200, {"ok": True, "registered": registered, "updated": updated}


THANK_YOU_QUOTE_CHARS = 160


def render_thank_you(deck, text, tracking_url, credit_name, published):
    """Plain-text subject and body for a human to paste into an email.

    Plain text on purpose: it is copied into whatever mail client the
    reviewer uses, so no HTML and no escaping.
    """
    quote = (text or "").strip().replace("\n", " ")
    if len(quote) > THANK_YOU_QUOTE_CHARS:
        quote = quote[:THANK_YOU_QUOTE_CHARS].rstrip() + "…"

    subject = "Thank you — your Doctrine suggestion was accepted"
    lines = [
        "Hi,",
        "",
        f"Thank you for your suggestion on a card in {deck or 'the deck'}:",
        f'"{quote}"',
        "",
        "A reviewer accepted it, and the fix ships in the next deck update.",
    ]
    if published:
        credited = f" It is credited to {credit_name}." if credit_name else ""
        lines += [f"We have listed it on the community updates page.{credited}",
                  f"{CFG.public_base_url}/updates"]
    lines += [
        "",
        "You can see its status any time here:",
        tracking_url,
        "",
        "— The Doctrine team",
    ]
    return subject, "\n".join(lines)


def queue_thank_you(conn, sid):
    """Queue a thank-you for a just-resolved suggestion, if it left an email.

    Called inside the resolving transaction. The UNIQUE(suggestion_id)
    constraint plus INSERT OR IGNORE make this safe to call more than once.
    """
    r = conn.execute(
        "SELECT deck, text, email, token, credit_name, published "
        "FROM suggestions WHERE id=?", (sid,)).fetchone()
    if r is None or not (r["email"] or "").strip():
        return
    subject, body = render_thank_you(
        r["deck"], r["text"], f"{CFG.public_base_url}/s/{r['token']}",
        r["credit_name"], bool(r["published"]))
    conn.execute(
        """INSERT OR IGNORE INTO notifications
           (suggestion_id, email, subject, body, created_at)
           VALUES (?,?,?,?,?)""",
        (sid, r["email"].strip(), subject, body, now()))


def reviewer_action(form, user_id=None):
    sid = form.get("id", [""])[0]
    action = form.get("do", [""])[0]
    status = {"resolve": "resolved", "decline": "declined",
              "expire": "expired"}.get(action)
    if not sid or not status:
        return
    publish = 1 if (status == "resolved" and form.get("publish")) else 0
    with db() as conn:
        changed = conn.execute(
            """UPDATE suggestions SET status=?, resolution_note=?, published=?,
               publish_summary=?, credit_name=?, closed_at=?, resolved_by=?
               WHERE id=? AND status='open'""",
            (status, form.get("resolution_note", [""])[0].strip() or None,
             publish, form.get("publish_summary", [""])[0].strip() or None,
             form.get("credit_name", [""])[0].strip() or None, now(),
             user_id, sid)).rowcount
        # Only a genuine open -> resolved transition earns a thank-you;
        # declines, expirations and repeat submits of the form do not.
        if changed == 1 and status == "resolved":
            queue_thank_you(conn, sid)


# ---------------------------------------------------------------- http handler

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie_token(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return None

    def _set_cookie(self, token, clear=False):
        attrs = [f"{COOKIE_NAME}={'' if clear else token}",
                 "Path=/", "HttpOnly", "SameSite=Lax"]
        if CFG.public_base_url.startswith("https://"):
            attrs.append("Secure")
        attrs.append("Max-Age=0" if clear else f"Max-Age={auth.SESSION_DAYS * 86400}")
        return "; ".join(attrs)

    def _current_user(self):
        return user_for_token(self._cookie_token())

    def _bearer_ok(self, expected):
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), expected)

    def _csrf_ok(self, form):
        token = self._cookie_token()
        supplied = form.get("csrf", [""])[0]
        return bool(token) and auth.csrf_ok(supplied, token, session_secret())

    def _db_unavailable(self):
        body = page("Busy", "<h1>Busy</h1><p>The database is momentarily "
                            "locked by another request. Please retry.</p>")
        self._send(503, body, headers={"Retry-After": "2"})

    def do_GET(self):
        try:
            self._do_GET()
        except sqlite3.OperationalError as e:
            print(f"[db] {e} on GET {self.path}", flush=True)
            self._db_unavailable()

    def do_POST(self):
        try:
            self._do_POST()
        except sqlite3.OperationalError as e:
            print(f"[db] {e} on POST {self.path}", flush=True)
            self._db_unavailable()

    def _do_GET(self):
        path = urlparse(self.path).path
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
        elif path == "/install":
            self._send(200, render_install())
        elif path == f"/download/{ADDON_PACKAGE_NAME}":
            try:
                with open(ADDON_PACKAGE_PATH, "rb") as fh:
                    data = fh.read()
            except OSError:
                self._send(404, page("Not found", "<h1>Add-on package not "
                                     "available</h1><p>It is built with the "
                                     "server image; check the deployment.</p>"))
            else:
                self._send(200, data, "application/octet-stream", {
                    "Content-Disposition":
                        f'attachment; filename="{ADDON_PACKAGE_NAME}"',
                    "Cache-Control": "no-cache",
                })
        elif path == "/login":
            if self._current_user():
                self._redirect("/reviewer")
            else:
                self._send(200, render_login())
        elif path == "/outbox":
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                self._send(200, render_outbox(
                    user, auth.csrf_token(self._cookie_token(), session_secret())))
        elif path == "/account":
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                self._send(200, render_account(
                    user, auth.csrf_token(self._cookie_token(), session_secret())))
        elif path == "/admin/users":
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            elif user["role"] != "admin":
                self._send(403, page("Forbidden", "<h1>Forbidden</h1>"
                                     "<p>Administrators only.</p>"))
            else:
                self._send(200, render_admin_users(
                    user, auth.csrf_token(self._cookie_token(), session_secret())))
        elif path in ("/", "/reviewer"):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                self._send(200, render_reviewer(user, session_secret(),
                                                self._cookie_token()))
        elif path == "/updates":
            self._send(200, render_updates())
        elif path.startswith("/s/"):
            self._send(200, render_status(path[3:]))
        else:
            self._send(404, page("Not found", "<h1>Not found</h1>"))

    def _do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path

        if path == "/api/suggestions":
            try:
                code, obj = api_suggestion(json.loads(raw or b"{}"))
            except json.JSONDecodeError:
                code, obj = 400, {"error": "Invalid JSON."}
            self._json(code, obj)
        elif path in ("/api/dev/register", "/api/master/sync"):
            # Writes master card state: never public. Fails closed when no
            # key is configured rather than falling back to open.
            if not CFG.pipeline_api_key:
                self._json(503, {"error": "Master sync is not enabled on "
                                          "this server."})
            elif not self._bearer_ok(CFG.pipeline_api_key):
                self._json(401, {"error": "Unauthorized."})
            else:
                try:
                    code, obj = api_register(json.loads(raw or b"{}"))
                except json.JSONDecodeError:
                    code, obj = 400, {"error": "Invalid JSON."}
                self._json(code, obj)
        elif path == "/login":
            form = parse_qs(raw.decode("utf-8"))
            username = form.get("username", [""])[0]
            user_id, error = authenticate(username, form.get("password", [""])[0])
            if user_id is None:
                self._send(200, render_login(error, username))
            else:
                token = start_session(user_id)
                self._redirect("/reviewer", self._set_cookie(token))
        elif path == "/logout":
            form = parse_qs(raw.decode("utf-8"))
            if self._csrf_ok(form):
                end_session(self._cookie_token())
            self._redirect("/login", self._set_cookie(None, clear=True))
        elif path == "/admin/users":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif user["role"] != "admin":
                self._send(403, page("Forbidden", "<h1>Forbidden</h1>"
                                     "<p>Administrators only.</p>"))
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1>"
                                     "<p>Invalid form token.</p>"))
            else:
                notice, new_password = admin_action(form)
                self._send(200, render_admin_users(
                    user, auth.csrf_token(self._cookie_token(), session_secret()),
                    notice, new_password))
        elif path == "/outbox":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1>"
                                     "<p>Invalid form token.</p>"))
            else:
                outbox_action(form, user["id"])
                self._redirect("/outbox")
        elif path == "/account":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1>"
                                     "<p>Invalid form token.</p>"))
            else:
                ok, message = change_own_password(
                    user["id"], form.get("current", [""])[0],
                    form.get("new", [""])[0], form.get("confirm", [""])[0])
                if ok:
                    # set_password cleared every session, this one included.
                    self._redirect("/login", self._set_cookie(None, clear=True))
                else:
                    self._send(200, render_account(
                        user,
                        auth.csrf_token(self._cookie_token(), session_secret()),
                        error=message))
        elif path == "/reviewer/action":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1>"
                                     "<p>Invalid form token. Reload and retry.</p>"))
            else:
                reviewer_action(form, user["id"])
                self._redirect("/reviewer")
        else:
            self._json(404, {"error": "Not found."})

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not reverse-resolve its own address.

    HTTPServer.server_bind() calls socket.getfqdn(), a reverse DNS lookup
    on the bind address. On a network with no reverse zone that blocks for
    tens of seconds before the server accepts its first connection — long
    enough for a container health check to fail. server_name is only used
    by the CGI handlers, which this server does not use.
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


# ---------------------------------------------------------------- auth pages

def render_login(error=None, username=""):
    err = f'<p class="login-error">{esc(error)}</p>' if error else ""
    body = f"""
<div class="login-wrap">
  <h1>Doctrine Editor</h1>
  <p class="sub">Reviewer sign-in</p>
  {err}
  <form method="post" action="/login" class="login-form">
    <label for="username">Username</label>
    <input id="username" name="username" autocomplete="username"
           value="{esc(username)}" autofocus required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password"
           autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
  <p class="login-note">No account? Ask an administrator to create one.
  Forgot your password? An administrator can reset it.</p>
</div>
<style>
.login-wrap {{ max-width: 340px; margin: 12vh auto; }}
.login-form {{ display: flex; flex-direction: column; gap: 6px; }}
.login-form label {{ font-size: 13px; font-weight: 600; margin-top: 8px; }}
.login-form input {{ padding: 8px; font-size: 15px; border: 1px solid #ccc;
  border-radius: 4px; }}
.login-form button {{ margin-top: 16px; padding: 9px; font-size: 15px;
  cursor: pointer; border: 0; border-radius: 4px; background: #2f6f4f;
  color: #fff; }}
.login-error {{ padding: 9px 12px; border-radius: 4px; background: #fdeaea;
  color: #8a1f1f; font-size: 14px; }}
.login-note {{ margin-top: 18px; font-size: 12px; color: #777;
  line-height: 1.5; }}
</style>"""
    return page("Sign in", body)


def render_admin_users(user, csrf, notice=None, new_password=None):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, username, role, active, created_at FROM users"
            " ORDER BY role='reviewer', username").fetchall()

    banner = ""
    if new_password:
        banner = f"""
<div class="pw-reveal">
  <p><strong>Password for {esc(new_password[0])}</strong></p>
  <code>{esc(new_password[1])}</code>
  <p class="pw-note">Shown once. Copy it now and send it to them —
  it cannot be retrieved later, only reset.</p>
</div>"""
    elif notice:
        banner = f'<p class="notice">{esc(notice)}</p>'

    items = []
    for r in rows:
        state = "active" if r["active"] else "inactive"
        toggle = "deactivate" if r["active"] else "activate"
        items.append(f"""
<tr class="{state}">
  <td>{esc(r['username'])}</td>
  <td><span class="role role-{r['role']}">{r['role']}</span></td>
  <td>{state}</td>
  <td class="row-actions">
    <form method="post" action="/admin/users" class="inline">
      <input type="hidden" name="csrf" value="{csrf}">
      <input type="hidden" name="user_id" value="{r['id']}">
      <button name="do" value="reset" class="ghost">Reset password</button>
    </form>
    <form method="post" action="/admin/users" class="inline">
      <input type="hidden" name="csrf" value="{csrf}">
      <input type="hidden" name="user_id" value="{r['id']}">
      <button name="do" value="{toggle}" class="ghost">{toggle.title()}</button>
    </form>
  </td>
</tr>""")

    body = f"""
<h1>Reviewers</h1>
<p class="sub">Signed in as {esc(user['username'])} (admin)</p>
{banner}
<form method="post" action="/admin/users" class="create-form">
  <input type="hidden" name="csrf" value="{csrf}">
  <input type="hidden" name="do" value="create">
  <input type="text" name="username" placeholder="New reviewer username" required>
  <label class="chk"><input type="checkbox" name="admin" value="1"> Administrator</label>
  <button type="submit">Create</button>
</form>
<table class="users">
  <thead><tr><th>User</th><th>Role</th><th>Status</th><th></th></tr></thead>
  <tbody>{''.join(items)}</tbody>
</table>
<p class="admin-note">Deactivating keeps a person's review history intact
and signs them out immediately. Resetting a password also signs them out.
There is no email in this system — hand credentials over directly.</p>
<style>
.pw-reveal {{ margin: 16px 0; padding: 14px 16px; border-radius: 6px;
  background: #eef7f0; border: 1px solid #bcdcc6; }}
.pw-reveal code {{ display: inline-block; margin: 6px 0; padding: 6px 10px;
  font-size: 17px; background: #fff; border: 1px solid #ccc; border-radius: 4px; }}
.pw-note {{ font-size: 12px; color: #55694d; margin: 4px 0 0; }}
.notice {{ padding: 9px 12px; border-radius: 4px; background: #f4f4f4; }}
.create-form {{ display: flex; gap: 8px; align-items: center; margin: 18px 0; }}
.create-form input[type=text] {{ padding: 7px; min-width: 240px;
  border: 1px solid #ccc; border-radius: 4px; }}
table.users {{ width: 100%; border-collapse: collapse; }}
table.users th, table.users td {{ text-align: left; padding: 8px 6px;
  border-bottom: 1px solid #eee; font-size: 14px; }}
tr.inactive {{ opacity: 0.5; }}
.role {{ font-size: 11px; padding: 2px 7px; border-radius: 10px;
  background: #eee; }}
.role-admin {{ background: #e5edf7; }}
form.inline {{ display: inline; }}
.row-actions {{ text-align: right; }}
.admin-note {{ margin-top: 22px; font-size: 12px; color: #777; line-height: 1.6; }}
</style>"""
    return page("Reviewers", body, "admin", user, csrf)


def render_account(user, csrf, notice=None, error=None):
    msg = ""
    if error:
        msg = f'<p class="login-error">{esc(error)}</p>'
    elif notice:
        msg = f'<p class="notice">{esc(notice)}</p>'
    body = f"""
<h1>Your account</h1>
<p class="sub">Signed in as {esc(user['username'])} ({esc(user['role'])})</p>
{msg}
<form method="post" action="/account" class="login-form" style="max-width:340px">
  <input type="hidden" name="csrf" value="{csrf}">
  <label for="current">Current password</label>
  <input id="current" name="current" type="password" required>
  <label for="new">New password</label>
  <input id="new" name="new" type="password" required>
  <label for="confirm">Repeat new password</label>
  <input id="confirm" name="confirm" type="password" required>
  <button type="submit">Change password</button>
</form>
<p class="admin-note">Forgot your password? An administrator can reset it —
there is no email recovery.</p>
<style>
.login-form {{ display: flex; flex-direction: column; gap: 6px; }}
.login-form label {{ font-size: 13px; font-weight: 600; margin-top: 8px; }}
.login-form input {{ padding: 8px; font-size: 15px; border: 1px solid #ccc;
  border-radius: 4px; }}
.login-form button {{ margin-top: 16px; padding: 9px; cursor: pointer;
  border: 0; border-radius: 4px; background: #2f6f4f; color: #fff; }}
.login-error {{ padding: 9px 12px; border-radius: 4px; background: #fdeaea;
  color: #8a1f1f; }}
.notice {{ padding: 9px 12px; border-radius: 4px; background: #eef7f0; }}
.admin-note {{ margin-top: 22px; font-size: 12px; color: #777; }}
</style>"""
    return page("Your account", body, "account", user, csrf)


def count_active_admins(exclude_id=None):
    query = "SELECT COUNT(*) c FROM users WHERE role='admin' AND active=1"
    params = ()
    if exclude_id is not None:
        query += " AND id != ?"
        params = (exclude_id,)
    with db() as conn:
        return conn.execute(query, params).fetchone()["c"]


def admin_action(form):
    """Apply an admin action. Returns (notice, new_password_tuple_or_None)."""
    action = form.get("do", [""])[0]

    if action == "create":
        username = form.get("username", [""])[0].strip()
        role = "admin" if form.get("admin") else "reviewer"
        password = auth.generate_password()
        ok, message = create_user(username, password, role)
        return (None, (username, password)) if ok else (message, None)

    try:
        user_id = int(form.get("user_id", [""])[0])
    except (TypeError, ValueError):
        return "Unknown user.", None

    with db() as conn:
        target = conn.execute("SELECT username, role, active FROM users WHERE id=?",
                              (user_id,)).fetchone()
    if target is None:
        return "Unknown user.", None

    if action == "reset":
        password = auth.generate_password()
        set_password(user_id, password)
        return None, (target["username"], password)

    if action == "deactivate":
        # Refuse to remove the last way into the admin page.
        if target["role"] == "admin" and count_active_admins(exclude_id=user_id) == 0:
            return ("That is the only active administrator — create another "
                    "one before deactivating this account."), None
        set_active(user_id, False)
        return f"Deactivated {target['username']}.", None

    if action == "activate":
        set_active(user_id, True)
        return (f"Reactivated {target['username']}. Reset their password to "
                "give them a way back in."), None

    return "Unknown action.", None


def change_own_password(user_id, current, new, confirm):
    """Returns (ok, message)."""
    if not new or len(new) < 8:
        return False, "New password must be at least 8 characters."
    if new != confirm:
        return False, "The new passwords did not match."
    with db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id=?",
                           (user_id,)).fetchone()
    if row is None or not auth.verify_password(current, row["password_hash"]):
        return False, "Current password is incorrect."
    set_password(user_id, new)
    return True, "Password changed. Your other sessions were signed out."


# ---------------------------------------------------------------- install page

# Built at container build time from addon/ (see Dockerfile). Always the
# student build: DEV_MODE off, no pipeline key.
ADDON_PACKAGE_PATH = os.environ.get("ADDON_PACKAGE_PATH",
                                    "/app/doctrine_editor.ankiaddon")
ADDON_PACKAGE_NAME = "doctrine_editor.ankiaddon"


def build_student_package(src_dir, out_path):
    """Zip addon/ into an .ankiaddon, refusing to ship a DEV build."""
    import zipfile
    src_dir = os.fspath(src_dir)
    init = open(os.path.join(src_dir, "__init__.py"), encoding="utf-8").read()
    if re.search(r"^DEV_MODE = True$", init, re.M) or \
       not re.search(r'^PIPELINE_API_KEY = ""$', init, re.M):
        raise RuntimeError("refusing to package a DEV build for students")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "user_files")]
            for name in files:
                if name.endswith((".pyc", ".DS_Store")):
                    continue
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, src_dir))


def render_install():
    body = f"""
<h1>Install the Doctrine Editor add-on</h1>
<p class="sub">Adds a <b>&#9998; Suggest an edit</b> button to Anki's reviewer
on Doctrine cards. Suggestions go straight to the review team; you get a
link to follow what happens to yours.</p>

<div class="install-box">
  <a class="btn-primary" href="/download/{ADDON_PACKAGE_NAME}">Download {ADDON_PACKAGE_NAME}</a>
  <p class="small">Anki 23.10 or newer, desktop only. The add-on never modifies
  your cards or note types.</p>
</div>

<h2>Steps</h2>
<ol class="steps">
  <li>Download the file above.</li>
  <li>In Anki: <b>Tools &rarr; Add-ons &rarr; Install from file&hellip;</b> and pick it.</li>
  <li>Restart Anki when prompted.</li>
  <li>Review any card in the <b>Doctrine</b> deck. The <b>&#9998; Suggest an edit</b>
      button appears top-right of the card.</li>
  <li>Click it, describe the issue, add your email if you'd like to hear back, send.</li>
</ol>

<h2>What happens next</h2>
<p>You get a tracking link straight away. A reviewer looks at every
suggestion next to the current version of the card. Accepted fixes ship
in the next deck update and can appear on the
<a href="/updates">community updates</a> page with credit.</p>

<h2>Already have an older version?</h2>
<p>Tools &rarr; Add-ons &rarr; select <b>Doctrine Editor</b> &rarr; Delete, then
install the new file. Your settings and pending suggestions are unaffected.</p>
<style>
.install-box {{ margin:18px 0 26px; padding:18px 20px; border:1px solid var(--line);
  border-radius:8px; background:#fafaf7; }}
.btn-primary {{ display:inline-block; padding:10px 16px; border-radius:6px;
  background:#2f6f4f; color:#fff !important; text-decoration:none; font-weight:600; }}
.steps li {{ margin:6px 0; }}
h2 {{ font-size:16px; margin-top:26px; }}
</style>"""
    return page("Install", body, "install")


# ---------------------------------------------------------------- outbox

def pending_thank_yous():
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE status='pending'"
        ).fetchone()["c"]


def render_outbox(user, csrf):
    with db() as conn:
        rows = conn.execute(
            """SELECT n.*, s.text AS suggestion_text, s.deck, s.token,
                      s.doctrine_id, s.suggestion_type
               FROM notifications n JOIN suggestions s ON s.id = n.suggestion_id
               WHERE n.status='pending' ORDER BY n.id"""
        ).fetchall()

    if not rows:
        body = ('<h1>Thank-you outbox</h1><p class="sub">Accepted suggestions '
                'whose author left an email land here, with a note ready to '
                'send.</p><div class="empty"><b>Nothing to send</b>Resolve a '
                'suggestion that came with an email and it will appear here.</div>')
        return page("Outbox", body, "outbox", user, csrf)

    items = []
    for r in rows:
        length = len((r["suggestion_text"] or "").strip())
        weight = "substantial" if length >= 120 else "brief"
        items.append(f"""
<div class="card outbox-item">
  <div class="card-head">
    <span class="badge {weight}">{weight}</span>
    <span class="meta"><b>#{r['suggestion_id']}</b> &middot; {esc(r['deck'])}
    &middot; {esc(TYPE_LABELS.get(r['suggestion_type'], ''))} &middot;
    <a href="mailto:{esc(r['email'])}">{esc(r['email'])}</a>
    &middot; {length} chars</span>
    <time>{esc(r['created_at'])}</time>
  </div>
  <div class="outbox-body">
    <div class="outbox-col">
      <div class="pane-label">Their suggestion</div>
      <blockquote>{esc(r['suggestion_text'])}</blockquote>
      <p class="small"><a href="/s/{esc(r['token'])}">Tracking page</a></p>
    </div>
    <div class="outbox-col">
      <div class="pane-label">Ready to send</div>
      <label class="small">Subject</label>
      <input type="text" readonly value="{esc(r['subject'])}" class="copyable">
      <label class="small">Body</label>
      <textarea readonly rows="11" class="copyable">{esc(r['body'])}</textarea>
      <button type="button" class="ghost copy-btn">Copy body</button>
    </div>
  </div>
  <div class="actions">
    <form method="post" action="/outbox" class="inline">
      <input type="hidden" name="csrf" value="{csrf}">
      <input type="hidden" name="id" value="{r['id']}">
      <button name="do" value="thanked">Mark as thanked</button>
      <button name="do" value="skipped" class="ghost">Skip</button>
    </form>
  </div>
</div>""")

    body = (f'<h1>Thank-you outbox</h1><p class="sub">{len(rows)} to send. '
            'Copy the note into your mail client, send it, then mark it as '
            'thanked. Skip the ones not worth a personal reply.</p>'
            + "".join(items) + """
<script>
document.querySelectorAll('.copy-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const ta = btn.parentElement.querySelector('textarea');
    navigator.clipboard.writeText(ta.value).then(() => {
      btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy body', 1500);
    });
  });
});
</script>""")
    return page("Outbox", body, "outbox", user, csrf)


def outbox_action(form, user_id):
    try:
        nid = int(form.get("id", [""])[0])
    except (TypeError, ValueError):
        return
    status = {"thanked": "thanked", "skipped": "skipped"}.get(form.get("do", [""])[0])
    if not status:
        return
    with db() as conn:
        conn.execute(
            """UPDATE notifications SET status=?, actioned_at=?, actioned_by=?
               WHERE id=? AND status='pending'""",
            (status, now(), user_id, nid))


# ---------------------------------------------------------------- sessions

COOKIE_NAME = "doctrine_session"
_SECRET_KEY = "session_secret"


def session_secret():
    """A per-installation secret, generated once and stored in the DB.

    Used only to derive CSRF tokens from session tokens. Kept out of the
    environment so a fresh deploy cannot silently fall back to a default.
    """
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings ("
                     "key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM settings WHERE key=?",
                           (_SECRET_KEY,)).fetchone()
        if row:
            return row["value"]
        secret = auth.new_token()
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?)",
                     (_SECRET_KEY, secret))
    return secret


def start_session(user_id):
    token = auth.new_token()
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at)"
            " VALUES (?,?,?,?)",
            (token, user_id, now(), auth.session_expiry(auth.utcnow())),
        )
    return token


def end_session(token):
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def user_for_token(token):
    """The active user behind a session token, or None.

    Expired rows are deleted lazily here, so a stale cookie cannot be
    reused and the table does not grow without bound.
    """
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            """SELECT s.token, s.expires_at, u.id, u.username, u.role, u.active
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token=?""", (token,)).fetchone()
        if row is None:
            return None
        if auth.is_expired(row["expires_at"], auth.utcnow()):
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            return None
        if not row["active"]:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
            return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def record_attempt(username, ok):
    with db() as conn:
        conn.execute(
            "INSERT INTO login_attempts (username, ok, attempted_at)"
            " VALUES (?,?,?)", (username, 1 if ok else 0, auth.utcnow().isoformat()))
        if ok:
            conn.execute("DELETE FROM login_attempts WHERE username=? AND ok=0",
                         (username,))


def recent_failures(username):
    with db() as conn:
        rows = conn.execute(
            "SELECT attempted_at FROM login_attempts"
            " WHERE username=? AND ok=0 ORDER BY id DESC LIMIT 50",
            (username,)).fetchall()
    return [r["attempted_at"] for r in rows]


def authenticate(username, password):
    """Returns (user_id, error_message). Never says which half was wrong."""
    username = (username or "").strip()
    generic = "Incorrect username or password."
    if not username or not password:
        return None, generic
    if auth.too_many_attempts(recent_failures(username), auth.utcnow()):
        return None, ("Too many failed attempts. Wait a few minutes and "
                      "try again.")
    with db() as conn:
        row = conn.execute(
            "SELECT id, password_hash, active FROM users WHERE username=?",
            (username,)).fetchone()
    ok = row is not None and row["active"] and auth.verify_password(
        password, row["password_hash"])
    record_attempt(username, ok)
    if not ok:
        return None, generic
    return row["id"], None


# ---------------------------------------------------------------- users

def create_user(username, password, role="reviewer"):
    """Insert a user. Returns (ok, message)."""
    username = (username or "").strip()
    if not username:
        return False, "Username is required."
    if role not in ("admin", "reviewer"):
        return False, f"Unknown role: {role}"
    if not password:
        return False, "Password is required."
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username=?", (username,)
        ).fetchone()
        if existing:
            return False, f"User {username!r} already exists."
        conn.execute(
            """INSERT INTO users
               (username, password_hash, role, active, created_at, password_changed_at)
               VALUES (?,?,?,1,?,?)""",
            (username, auth.hash_password(password), role, now(), now()),
        )
    return True, f"Created {role} {username!r}."


def set_password(user_id, password):
    """Change a password and invalidate that user's live sessions.

    Killing the sessions is the point: otherwise "this person is out"
    would not take effect until their cookie expired days later.
    """
    with db() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
            (auth.hash_password(password), now(), user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def set_active(user_id, active):
    with db() as conn:
        conn.execute("UPDATE users SET active=? WHERE id=?",
                     (1 if active else 0, user_id))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def bootstrap_admin(username):
    """Create the very first admin, once, from BOOTSTRAP_ADMIN.

    A fresh deploy has no users at all, so there is no way in and no way
    to create one. This closes that gap without needing shell access.

    It acts ONLY when zero admins exist. Leaving the variable set is
    therefore harmless: it cannot reset an existing admin's password and
    cannot mint a second admin if someone changes the value.
    """
    username = (username or "").strip()
    if not username:
        return
    if count_active_admins() > 0:
        return
    with db() as conn:
        any_admin = conn.execute(
            "SELECT id FROM users WHERE role='admin'").fetchone()
    if any_admin:
        return

    password = auth.generate_password()
    ok, message = create_user(username, password, "admin")
    if not ok:
        print(f"BOOTSTRAP_ADMIN: {message}", flush=True)
        return
    print("=" * 62, flush=True)
    print(f"  Created first admin {username!r}", flush=True)
    print(f"  Password: {password}", flush=True)
    print("  Shown once. Sign in, change it at /account, then remove", flush=True)
    print("  the BOOTSTRAP_ADMIN variable.", flush=True)
    print("=" * 62, flush=True)


def _cli_adduser(argv):
    import getpass

    if not argv:
        print("usage: server.py adduser <username> [--admin] [--generate]")
        return 2
    username = argv[0]
    role = "admin" if "--admin" in argv else "reviewer"

    if "--generate" in argv:
        password = auth.generate_password()
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords did not match.")
            return 1
        if len(password) < 8:
            print("Password must be at least 8 characters.")
            return 1

    ok, message = create_user(username, password, role)
    print(message)
    if ok and "--generate" in argv:
        print(f"\n  Password: {password}\n\nShown once. Copy it now.")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    init_db()

    if len(sys.argv) > 1 and sys.argv[1] == "adduser":
        raise SystemExit(_cli_adduser(sys.argv[2:]))

    bootstrap_admin(os.environ.get("BOOTSTRAP_ADMIN", ""))
    email_out.check_config(CFG)   # refuse to boot with a half-configured sender

    print(f"Doctrine Editor platform — {CFG.public_base_url}", flush=True)
    print(f"  Reviewer queue:  {CFG.public_base_url}/reviewer", flush=True)
    print(f"  Public updates:  {CFG.public_base_url}/updates", flush=True)
    print(f"  Listening on {CFG.host}:{CFG.port}, db={CFG.db_path}", flush=True)
    Server((CFG.host, CFG.port), Handler).serve_forever()
