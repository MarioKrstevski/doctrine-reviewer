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
import threading
import os
import re
import socketserver

import auth
import email_out
import releases
import release_build
import reupload
import revisions
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

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
    "media": "Image / audio",
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
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS releases (
            version TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'draft',   -- draft|published
            note_count INTEGER,
            collection_path TEXT,
            changelog TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT NOT NULL,
            field TEXT NOT NULL,          -- Text | Extra | added
            before TEXT,
            after TEXT,
            user_id INTEGER,
            source TEXT,                  -- suggestion:<id> | upload:<id> | staff
            release TEXT,                 -- NULL while pending
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_revisions_release ON revisions(release);
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'pending',   -- pending|applied|discarded
            incoming_json TEXT NOT NULL,              -- {guid: {fields, tags, note_type}}
            diff_json TEXT NOT NULL,
            summary_json TEXT,
            created_at TEXT,
            applied_at TEXT,
            applied_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS download_tokens (
            token TEXT PRIMARY KEY,
            note TEXT,
            created_by INTEGER,
            created_at TEXT,
            expires_at TEXT,
            downloads INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS media (
            filename TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL,
            size INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_username
            ON login_attempts(username, attempted_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)

        # suggestions predates several features in production, so columns
        # are added in place when missing rather than recreating the table.
        _ensure_columns(conn, "notes", {"doctorine_id": "TEXT", "guid": "TEXT",
                                        "retired": "INTEGER DEFAULT 0"})
        _ensure_columns(conn, "releases", {"package_path": "TEXT", "build_log": "TEXT",
                                           "published_at": "TEXT", "built_by": "INTEGER"})
        _ensure_columns(conn, "suggestions", {"release": "TEXT"})
        _ensure_columns(conn, "suggestions", {
            "resolved_by": "INTEGER",
            # identity, both halves: their id (may be empty) and Anki's guid
            "doctorine_id": "TEXT",
            "guid": "TEXT",
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
            # P3+: the student's proposed exact change (editable fields only)
            "proposed_fields_json": "TEXT",
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
    def label(t):
        # "Doctorine::Yield::Step1::Gold" -> "Step1 · Gold". The last
        # segment alone (Gold, Silver, Screening) loses the meaning of the
        # deck's hierarchical tags; the brand prefix adds nothing.
        parts = [x for x in t.split("::") if x]
        if parts and parts[0].lower() in ("doctorine", "doctrine"):
            parts = parts[1:]
        return " · ".join(parts[-2:]) if parts else t
    chips = "".join(f'<span class="chip" title="{esc(t)}">{esc(label(t))}</span>'
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


def _words(text):
    return re.findall(r"\s+|<[^>]+>|\{\{c\d+::|\}\}|[^\s<{}]+|[{}]", text or "")


def diff_words(before, after):
    """Word-level HTML diff with <ins>/<del>, everything escaped."""
    import difflib
    a, b = _words(before), _words(after)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(esc("".join(a[i1:i2])))
        if op in ("replace", "delete"):
            out.append("<del>" + esc("".join(a[i1:i2])) + "</del>")
        if op in ("replace", "insert"):
            out.append("<ins>" + esc("".join(b[j1:j2])) + "</ins>")
    return "".join(out)


def diff_fields(current, proposed):
    """[(field, html_diff)] for proposed fields that differ from current."""
    out = []
    for name in revisions.EDITABLE_FIELDS:
        if name in proposed and proposed[name] != (current or {}).get(name, ""):
            out.append((name, diff_words((current or {}).get(name, ""), proposed[name])))
    return out


def proposal_block(r, master, csrf):
    """The student's proposed change as a diff against the current master."""
    if not r["proposed_fields_json"]:
        return ""
    try:
        proposed = json.loads(r["proposed_fields_json"])
    except ValueError:
        return ""
    current = json.loads(master["fields_json"]) if master is not None else {}
    diffs = diff_fields(current, proposed)
    if not diffs:
        return ('<div class="proposal"><div class="pane-label">Proposed change</div>'
                '<p class="small">Identical to the current card.</p></div>')
    rows = "".join(f'<dt>{esc(f)}</dt><dd class="diff">{h}</dd>' for f, h in diffs)
    return (f'<div class="proposal"><div class="pane-label">Proposed change '
            f'<span class="small">(against current master)</span></div>'
            f'<dl class="fields">{rows}</dl></div>')


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


_SRC = re.compile(r'(src=["\'])(?!https?://|data:|/)([^"\']+)(["\'])')


def rewrite_media(html):
    """Point relative src="file.png" at /media/file.png (served from the
    baseline's media). Absolute and data URLs are left alone."""
    if not html:
        return html
    return _SRC.sub(lambda m: m.group(1) + "/media/" + quote(m.group(2)) + m.group(3), html)


def card_iframe(q_html, a_html, css, label):
    """Render a card snapshot inside a sandboxed iframe via srcdoc."""
    q_html, a_html = rewrite_media(q_html), rewrite_media(a_html)
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
  /* Study Navigator brand (studynavigator.com) */
  --navy:#0F2A3D; --navy-mid:#14324A; --navy-soft:#1B3E59;
  --blue:#57B9FF; --blue-light:#90D5FF; --teal:#77D1D4; --slate:#517891;
  --paper:#FAF7F0; --card:#FFFFFF; --panel:#F6F2E8;
  --ink:#122031; --muted:#4C6270; --faint:#6E8494;
  --line:#E7DFCE; --line-strong:#D6CCB6;
  --accent:#2F86C9; --accent-ink:#14324A;
  --accent-soft:#E9F3FD; --accent-border:#90D5FF;
  --warn:#8A5800; --warn-soft:#F6EDD7; --warn-border:#E3D1A6;
  --bad:#943030; --bad-soft:#F5E4E1; --bad-border:#E1C1BB;
  --dim-soft:#EFEAE0; --code-bg:#F1ECE0;
  --serif:"Fraunces", Georgia, "Times New Roman", serif;
  --sans:"Plus Jakarta Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono:ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --shadow:0 12px 30px -14px rgba(15,42,61,.18);
  --radius:16px;
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
.copyrow { display:flex; gap:8px; align-items:center; }
.copyrow input, .linkcell input { flex:1; min-width:0; font-family:ui-monospace,Menlo,monospace; font-size:12px; }
.linkcell { display:flex; gap:6px; align-items:center; min-width:420px; }
.badge.substantial { background:#e5edf7; color:#2a4a6f; }
.badge.brief { background:#eee; color:#666; }
.proposal { padding:10px 16px 12px; border-top:1px solid var(--line); background:#fbfbf8; }
.proposal .fields { padding:8px 0 0; height:auto; max-height:260px; }
.diff del { background:#fde2e2; color:#8a1f1f; text-decoration:line-through; padding:0 2px; }
.diff ins { background:#dcf5e4; color:#145a32; text-decoration:none; padding:0 2px; }
.btn-link { margin-left:10px; font-size:13px; }
.btn-link-primary { display:inline-block; padding:7px 14px; border-radius:6px; font-size:13px;
  font-weight:500; text-decoration:none; color:var(--accent-ink); border:1px solid var(--accent-ink);
  background:transparent; }
.btn-link-primary:hover { background:var(--accent-soft); }
.actions .hint { color:var(--faint); font-size:12.5px; flex:1 1 260px; }
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
.release-head { font-family:var(--serif); font-size:17px; font-weight:600; margin:22px 6px 4px;
  color:var(--accent-ink); }
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


SHELL_CSS = """
header.site { background:var(--navy); color:#fff; border-bottom:none; }
header.site .wrap { display:flex; align-items:center; gap:18px; padding:16px 24px; }
header.site .brand { color:#fff; font-family:var(--serif); font-size:22px; font-weight:600; }
header.site .brand small { color:var(--blue-light); }
header.site nav { margin-left:auto; }
header.site nav a { color:#D6E6F2; margin-left:22px; font-size:14px; font-weight:500; text-decoration:none; }
header.site nav a:hover, header.site nav a.active { color:#fff; box-shadow:inset 0 -2px 0 var(--blue); }
.staff { display:grid; grid-template-columns:232px minmax(0,1fr); min-height:100vh; }
.sidebar { background:var(--navy); color:#D6E6F2; padding:20px 14px; position:sticky; top:0;
  height:100vh; display:flex; flex-direction:column; gap:4px; }
.sidebar .brand { color:#fff; font-family:var(--serif); font-size:20px; font-weight:600; padding:4px 10px 16px; }
.sidebar .brand small { display:block; color:var(--blue-light); font-family:var(--sans); font-size:10.5px;
  letter-spacing:.14em; text-transform:uppercase; margin:2px 0 0; }
.sidebar .group { font-size:10.5px; letter-spacing:.12em; text-transform:uppercase; color:var(--slate);
  padding:14px 10px 4px; }
.sidebar a { display:flex; align-items:center; gap:8px; padding:7px 10px; border-radius:10px;
  color:#D6E6F2; text-decoration:none; font-size:14px; font-weight:500; }
.sidebar a:hover { background:var(--navy-soft); color:#fff; }
.sidebar a.active { background:var(--navy-mid); color:#fff; box-shadow:inset 3px 0 0 var(--blue); }
.sidebar a .count { margin-left:auto; background:var(--blue); color:var(--navy); }
.sidebar .who { margin-top:auto; padding:12px 10px 0; border-top:1px solid var(--navy-soft); font-size:12.5px; }
.sidebar .who form { display:flex; align-items:center; gap:8px; }
.sidebar .who button { background:transparent; border:1px solid var(--slate); color:#D6E6F2;
  padding:3px 10px; font-size:12px; border-radius:8px; }
.staff main .wrap { max-width:1180px; }
button { background:var(--navy); border-color:var(--navy); border-radius:10px; }
button:hover { background:var(--navy-soft); border-color:var(--navy-soft); }
.card, .status-hero, .empty { border-radius:var(--radius); }
.count { background:var(--accent); }
@media (max-width:900px) { .staff { grid-template-columns:1fr; }
  .sidebar { position:static; height:auto; flex-direction:row; flex-wrap:wrap; }
  .sidebar .group, .sidebar .who { display:none; } }
"""

FONTS_LINK = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
              '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
              '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400..700'
              '&family=Plus+Jakarta+Sans:ital,wght@0,400..700;1,400&display=swap">')


def _staff_counts():
    with db() as conn:
        inbox = conn.execute("SELECT COUNT(DISTINCT doctrine_id) FROM suggestions WHERE status='open'").fetchone()[0]
        nxt = conn.execute("SELECT COUNT(DISTINCT guid) FROM revisions WHERE release IS NULL").fetchone()[0]
    return inbox, nxt, pending_thank_yous()


def page(title, body, active="", user=None, csrf=""):
    head = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — Doctrine</title>{FONTS_LINK}
<style>{BASE_CSS}{LEDGER_CSS}{SHELL_CSS}</style></head><body>"""
    script = """<script>
document.querySelectorAll('.actions input[name=publish]').forEach(cb => {
  const fields = cb.closest('form').querySelector('.pub-fields');
  if (!fields) return;
  const sync = () => fields.classList.toggle('off', !cb.checked);
  cb.addEventListener('change', sync); sync();
});
</script></body></html>"""

    if not user:
        def pub(href, label, key):
            cls = ' class="active"' if key == active else ""
            return f'<a href="{href}"{cls}>{label}</a>'
        return (head + f"""
<header class="site"><div class="wrap">
  <span class="brand">Doctrine<small>Card quality</small></span>
  <nav>{pub("/updates", "Updates", "updates")}{pub("/install", "Install the add-on", "install")}</nav>
</div></header>
<div class="wrap">{body}</div>""" + script)

    inbox, nxt, thanks = _staff_counts()

    def item(href, label, key, n=0):
        cls = ' class="active"' if key == active else ""
        c = f'<span class="count">{n}</span>' if n else ""
        return f'<a href="{href}"{cls}>{label}{c}</a>'

    admin = user.get("role") == "admin"
    side = f"""
<aside class="sidebar">
  <span class="brand">Doctrine<small>Deck workspace</small></span>
  <span class="group">Review</span>
  {item("/reviewer", "Inbox", "reviewer", inbox)}
  {item("/cards", "Cards", "cards")}
  <span class="group">Ship</span>
  {item("/releases/next", "Next release", "pending", nxt)}
  {item("/releases", "Releases", "releases")}
  {item("/outbox", "Outbox", "outbox", thanks)}
  <span class="group">Deck</span>
  {item("/deck", "Deck", "deck")}
  {item("/detection", "Detection", "detection") if admin else ""}
  {item("/admin/users", "Settings", "admin") if admin else item("/account", "Account", "account")}
  {item("/updates", "Public updates", "updates")}
  <div class="who"><form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}">
  <span>{esc(user["username"])}</span><button type="submit">Sign out</button></form>
  <a href="/account" style="padding:6px 0 0;font-size:12px">Account</a></div>
</aside>"""
    return head + f'<div class="staff">{side}<main><div class="wrap">{body}</div></main></div>' + script


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
            state_badge = '<span class="badge">No master on file</span>'
            master_pane = ('<div class="pane"><div class="pane-label">Current '
                           'master</div><div class="sugg-body sugg-empty">No '
                           'master copy on file for this card, so the '
                           'snapshot on the left is the only version we have. '
                           'Compare it against the deck yourself.</div></div>')
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
            has_proposal = bool(r["proposed_fields_json"]) and master is not None
            accept = (f'<button name="do" value="accept_proposal">Accept proposal</button>'
                      if has_proposal else "")
            edit_label = "Edit and accept" if not has_proposal else "Edit proposal, then accept"
            actions = f"""
<div class="actions">{expire_hint}
<form method="post" action="/reviewer/action">
  <input type="hidden" name="csrf" value="{csrf}">
  <input type="hidden" name="id" value="{r['id']}">
  <span class="hint">Accepting changes the card in the online deck and queues it for the next release.</span>
  <span class="btns">
    {accept}
    <a class="btn-link-primary" href="/edit/{r['id']}{'?from=proposal' if has_proposal else ''}">{edit_label}</a>
    <button name="do" value="decline" class="secondary">Decline</button>
    <button name="do" value="expire" class="ghost">Mark expired</button>
  </span>
</form></div>"""
        else:
            closed_bits = []
            with db() as conn:
                edited = conn.execute(
                    "SELECT COUNT(*) FROM revisions WHERE source=?",
                    (f"suggestion:{r['id']}",)).fetchone()[0]
            if edited:
                closed_bits.append(f'<span class="badge ok">Edited</span> '
                                   f'{edited} field change(s) pending release')
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
  {proposal_block(r, master, csrf)}
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
        def entry(r):
            return f"""
<div class="entry">
  <div class="stamp"><b>#{r['id']}</b>{esc((r['closed_at'] or '').split(' ')[0])}</div>
  <div class="what"><span class="kind">{esc(TYPE_LABELS.get(r['suggestion_type'], 'Fix'))}</span>
    {esc(r['publish_summary'] or r['text'][:120])}</div>
  <div class="credit">{('suggested by ' + esc(r['credit_name'])) if r['credit_name'] else 'community suggestion'}</div>
</div>"""
        rendered = [dict(r, html=entry(r)) for r in rows]
        groups, order = {}, []
        for r in rendered:
            key = r["release"] or ""
            if key not in groups:
                groups[key] = []; order.append(key)
            groups[key].append(r["html"])
        parts = []
        for key in order:
            title = f"Release {esc(key)}" if key else "Accepted, in the next release"
            parts.append(f'<h2 class="release-head">{title}</h2>' + "".join(groups[key]))
        ledger = f'<div class="ledger">{"".join(parts)}</div>'
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

def is_official(guid) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM notes WHERE doctrine_id=? AND retired=0", (guid,)
        ).fetchone() is not None


_official_cache = {"key": None, "body": b"", "etag": ""}


def official_list():
    """(etag, json bytes) for the current official guid set, cached in memory.

    Rebuilt only when the set changes: the cache key is (row count, newest
    updated_at, retired count), all cheap aggregates.
    """
    import hashlib as _h
    with db() as conn:
        key = tuple(conn.execute(
            "SELECT COUNT(*), MAX(updated_at), SUM(retired) FROM notes").fetchone())
        if key == _official_cache["key"]:
            return _official_cache["etag"], _official_cache["body"]
        guids = [r["doctrine_id"] for r in conn.execute(
            "SELECT doctrine_id FROM notes WHERE retired=0 ORDER BY doctrine_id")]
        release = conn.execute(
            "SELECT value FROM settings WHERE key='current_release'").fetchone()
    body = json.dumps({"release": release["value"] if release else "unreleased",
                       "count": len(guids), "guids": guids}).encode("utf-8")
    etag = '"' + _h.sha256(body).hexdigest()[:24] + '"'
    _official_cache.update(key=key, body=body, etag=etag)
    return etag, body


def fetch_master(doctorine_id, guid):
    """The client's current copy of a card, or None.

    Stub. When the client exposes a GET endpoint this calls it (server-side
    only; their key never ships in the add-on) and returns
    {"fields": {...}, "content_hash": ...}. Until then None: suggestions
    are accepted on the student snapshot alone and the reviewer sees
    "No master on file" rather than a comparison.
    """
    return None


def api_suggestion(data):
    doc_id = (data.get("doctrine_id") or "").strip()
    doctorine_id = (data.get("doctorine_id") or "").strip()
    guid = (data.get("guid") or "").strip()
    if not doc_id:
        doc_id = doctorine_id or guid
    if not doc_id:
        return 400, {"error": "Missing card identity."}

    text = (data.get("text") or "").strip()
    if not text:
        return 400, {"error": "Suggestion text is required."}
    snap = data.get("snapshot") or {}

    # Only official notes: a guid in the current release and not retired.
    # Students' own notes, local copies (new guid) and other decks stop here.
    if not is_official(doc_id):
        return 404, {"error": "This card is not part of the Doctorine deck, "
                              "so suggestions can't be sent for it."}
    fetch_master(doctorine_id or doc_id, guid)

    with db() as conn:

        token = uuid.uuid4().hex[:16]
        tags = data.get("tags")
        proposed = data.get("proposed_fields")
        if isinstance(proposed, dict):
            proposed = {k: v for k, v in proposed.items()
                        if k in revisions.EDITABLE_FIELDS and isinstance(v, str)}
            proposed_json = json.dumps(proposed) if proposed else None
        else:
            proposed_json = None
        conn.execute(
            """INSERT INTO suggestions
               (token, doctrine_id, anki_note_id, note_type, deck,
                suggestion_type, text, email,
                snap_question, snap_answer, snap_css, snap_fields_json,
                snap_hash, created_at,
                tags_json, note_mod, card_id, card_ord, template_name,
                deck_id, original_deck_id, install_id, addon_version,
                anki_version, doctorine_id, guid, proposed_fields_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?,?,?)""",
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
             data.get("addon_version"), data.get("anki_version"),
             doctorine_id, guid, proposed_json),
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


def _close_suggestion(conn, sid, status, form, user_id):
    """open -> status with the form's publish fields. Returns rows changed."""
    publish = 1 if (status == "resolved" and form.get("publish")) else 0
    return conn.execute(
        """UPDATE suggestions SET status=?, resolution_note=?, published=?,
           publish_summary=?, credit_name=?, closed_at=?, resolved_by=?
           WHERE id=? AND status='open'""",
        (status, form.get("resolution_note", [""])[0].strip() or None,
         publish, form.get("publish_summary", [""])[0].strip() or None,
         form.get("credit_name", [""])[0].strip() or None, now(),
         user_id, sid)).rowcount


def accept_proposal(conn, sid, user_id):
    """Apply the student's proposed edit to the note and resolve. False if none."""
    r = conn.execute("SELECT doctrine_id, proposed_fields_json FROM suggestions "
                     "WHERE id=? AND status='open'", (sid,)).fetchone()
    if r is None or not r["proposed_fields_json"]:
        return False
    try:
        proposed = json.loads(r["proposed_fields_json"])
        revisions.apply_edit(conn, r["doctrine_id"], proposed, user_id, f"suggestion:{sid}")
    except (ValueError, LookupError):
        return False
    if _close_suggestion(conn, sid, "resolved", {}, user_id) == 1:
        queue_thank_you(conn, sid)
    return True


def apply_edit_and_resolve(conn, sid, new_values, form, user_id):
    """Reviewer-edited fields: apply to the note, then resolve with publish fields."""
    r = conn.execute("SELECT doctrine_id FROM suggestions WHERE id=? AND status='open'",
                     (sid,)).fetchone()
    if r is None:
        raise LookupError("suggestion is not open")
    revisions.apply_edit(conn, r["doctrine_id"], new_values, user_id, f"suggestion:{sid}")
    if _close_suggestion(conn, sid, "resolved", form, user_id) == 1:
        queue_thank_you(conn, sid)


def reviewer_action(form, user_id=None):
    sid = form.get("id", [""])[0]
    action = form.get("do", [""])[0]
    if action == "accept_proposal" and sid:
        with db() as conn:
            accept_proposal(conn, sid, user_id)
        return
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

    def _send_file(self, path, filename, ctype="application/octet-stream"):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

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
        if path.startswith("/media/"):
            name = unquote(path[len("/media/"):])
            found = None
            if releases.safe_filename(name):
                with db() as conn:
                    found = releases.media_file(conn, name)
            if found is None:
                self._send(404, b"not found", "text/plain")
            else:
                fpath, ctype = found
                with open(fpath, "rb") as fh:
                    data = fh.read()
                self._send(200, data, ctype, {"Cache-Control": "public, max-age=31536000, immutable"})
        elif path == "/api/media/manifest":
            if not CFG.pipeline_api_key or not self._bearer_ok(CFG.pipeline_api_key):
                self._json(401, {"error": "Unauthorized."})
            else:
                with db() as conn:
                    files = [r["filename"] for r in conn.execute("SELECT filename FROM media")]
                self._json(200, {"files": files})
        elif path == "/api/sync/version":
            with db() as conn:
                cur = releases.current_release(conn)
                row = conn.execute("SELECT published_at, note_count FROM releases WHERE version=?", (cur,)).fetchone() if cur else None
            self._json(200, {"current": cur, "published_at": row["published_at"] if row else None,
                             "note_count": row["note_count"] if row else None})
        elif path == "/api/sync":
            since = parse_qs(urlparse(self.path).query).get("since", [""])[0]
            with db() as conn:
                payload = sync_payload(conn, since)
            self._json(200, payload)
        elif path == "/api/releases":
            with db() as conn:
                self._json(200, {"current": releases.current_release(conn),
                                 "releases": releases.list_releases(conn)})
        elif path == "/api/official":
            etag, body = official_list()
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(200, body, "application/json",
                           {"ETag": etag, "Cache-Control": "public, max-age=300"})
        elif path == "/where":
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
            self._send(200, render_install(self._current_user(), self._cookie_token()))
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
        elif path.startswith("/dl/"):
            token = path[len("/dl/"):]
            with db() as conn:
                row = conn.execute("SELECT * FROM download_tokens WHERE token=?", (token,)).fetchone()
                ok = row is not None and not auth.is_expired(
                    datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc), auth.utcnow())
                version, pkg = latest_package(conn) if ok else (None, None)
                if pkg:
                    conn.execute("UPDATE download_tokens SET downloads=downloads+1 WHERE token=?", (token,))
            if not pkg:
                self._send(404, page("Not found", "<h1>Link not valid</h1><p>This download link is "
                                     "unknown, expired, or no release is published yet.</p>"))
            else:
                self._send_file(pkg, f"Doctorine-{version}.apkg")
        elif path == "/releases" or path.startswith("/download/release/"):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            elif path == "/releases":
                self._send(200, render_releases(user, auth.csrf_token(self._cookie_token(), session_secret())))
            else:
                want = path[len("/download/release/"):]
                with db() as conn:
                    version, pkg = latest_package(conn) if want == "latest" else package_for_version(conn, want)
                if not pkg:
                    self._send(404, page("Not found", "<h1>No package for that release</h1>"))
                else:
                    self._send_file(pkg, f"Doctorine-{version}.apkg")
        elif path.startswith("/uploads/"):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                csrf = auth.csrf_token(self._cookie_token(), session_secret())
                self._send(200, render_upload(user, csrf, path[len("/uploads/"):].split("/")[0]))
        elif path == "/releases/pending":
            self._redirect("/releases/next")
        elif path in ("/cards", "/cards/edit"):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                csrf = auth.csrf_token(self._cookie_token(), session_secret())
                qs = parse_qs(urlparse(self.path).query)
                if path == "/cards":
                    self._send(200, render_cards(user, csrf, qs.get("q", [""])[0]))
                else:
                    html = render_card_edit(user, csrf, qs.get("guid", [""])[0])
                    if html is None:
                        self._send(404, page("Not found", "<h1>No such card</h1>", "cards", user, csrf))
                    else:
                        self._send(200, html)
        elif path.startswith("/edit/") or path in ("/notes/new", "/releases/next") or \
                (path.startswith("/releases/") and path.endswith("/confirm")):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                csrf = auth.csrf_token(self._cookie_token(), session_secret())
                if path == "/notes/new":
                    self._send(200, render_new_note(user, csrf))
                elif path == "/releases/next":
                    self._send(200, render_next_release(user, csrf))
                elif path.endswith("/confirm"):
                    self._send(200, render_build_confirm(user, csrf, path[len("/releases/"):-len("/confirm")]))
                else:
                    qs = parse_qs(urlparse(self.path).query)
                    self._send(200, render_edit(user, csrf, path[len("/edit/"):],
                                                qs.get("from", [""])[0] == "proposal"))
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
        elif path in ("/releases/cut", "/releases/token", "/releases/token/revoke", "/releases/confirm") or \
                (path.startswith("/releases/") and (path.endswith("/build") or path.endswith("/discard") or path.endswith("/rebuild"))):
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Invalid form token.</p>"))
            elif path == "/releases/confirm":
                self._send(200, render_confirm_selection(
                    user, auth.csrf_token(self._cookie_token(), session_secret()), _selected_guids(form)))
            elif path == "/releases/cut":
                guids = _selected_guids(form)
                ok, message = cut_release(form.get("version", [""])[0], user["id"], guids)
                if ok:
                    self._redirect("/releases")
                else:
                    self._send(200, render_confirm_selection(
                        user, auth.csrf_token(self._cookie_token(), session_secret()), guids, message))
            elif path.endswith("/discard"):
                discard_release(path[len("/releases/"):-len("/discard")])
                self._redirect("/releases/next")
            elif path.endswith("/rebuild"):
                start_rebuild(path[len("/releases/"):-len("/rebuild")], user["id"])
                self._redirect("/releases")
            elif path == "/releases/token/revoke":
                if user["role"] != "admin":
                    self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Administrators only.</p>"))
                else:
                    with db() as conn:
                        conn.execute("DELETE FROM download_tokens WHERE token=?", (form.get("token", [""])[0],))
                    self._redirect("/releases")
            elif path == "/releases/token":
                if user["role"] != "admin":
                    self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Administrators only.</p>"))
                else:
                    token = mint_download_token(form.get("note", [""])[0], form.get("days", ["7"])[0], user["id"])
                    self._send(200, render_releases(user, auth.csrf_token(self._cookie_token(), session_secret()),
                                                    token_link=f"{CFG.public_base_url}/dl/{token}"))
            else:
                version = path[len("/releases/"):-len("/build")]
                if form.get("confirm", [""])[0].strip() != version:
                    self._send(200, render_build_confirm(
                        user, auth.csrf_token(self._cookie_token(), session_secret()), version,
                        "Type the version exactly to confirm."))
                else:
                    start_build(version, user["id"])
                    self._redirect("/releases")
        elif path == "/api/reupload":
            if not CFG.pipeline_api_key or not self._bearer_ok(CFG.pipeline_api_key):
                self._json(401, {"error": "Unauthorized."})
            else:
                try:
                    data = json.loads(raw or b"{}")
                    upload_id, summary = create_upload(data.get("label") or "", data.get("notes") or [])
                    self._json(200, {"ok": True, "upload_id": upload_id, "summary": summary,
                                     "review_url": f"{CFG.public_base_url}/uploads/{upload_id}"})
                except json.JSONDecodeError:
                    self._json(400, {"error": "Invalid JSON."})
                except ValueError as e:
                    self._json(400, {"error": str(e)})
        elif path.startswith("/uploads/") and path.endswith("/apply"):
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Invalid form token.</p>"))
            else:
                upload_id = path[len("/uploads/"):-len("/apply")]
                apply_upload(upload_id, form, user["id"])
                self._redirect("/releases/pending")
        elif path.startswith("/api/media/") or path == "/api/baseline":
            if not CFG.pipeline_api_key or not self._bearer_ok(CFG.pipeline_api_key):
                self._json(401, {"error": "Unauthorized."})
            elif path == "/api/baseline":
                qs = parse_qs(urlparse(self.path).query)
                version = (qs.get("version", [""])[0]).strip()
                if not version or "/" in version or ".." in version:
                    self._json(400, {"error": "version is required."})
                else:
                    try:
                        count = int(qs.get("note_count", ["0"])[0])
                        with db() as conn:
                            info = releases.store_baseline(conn, version, count, raw, now())
                        _official_cache["key"] = None
                        self._json(200, info)
                    except ValueError as e:
                        self._json(400, {"error": str(e)})
            else:
                name = unquote(path[len("/api/media/"):])
                if not releases.safe_filename(name):
                    self._json(400, {"error": "Bad filename."})
                else:
                    with db() as conn:
                        self._json(200, releases.store_media(conn, name, raw))
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
        elif path == "/cards/edit":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            guid = parse_qs(urlparse(self.path).query).get("guid", [""])[0]
            if user is None:
                self._redirect("/login")
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Invalid form token.</p>"))
            else:
                values = {f: form.get(f, [""])[0] for f in revisions.EDITABLE_FIELDS if f in form}
                try:
                    with db() as conn:
                        revisions.apply_edit(conn, guid, values, user["id"], "staff")
                    self._redirect("/releases/next")
                except (ValueError, LookupError) as e:
                    csrf = auth.csrf_token(self._cookie_token(), session_secret())
                    html = render_card_edit(user, csrf, guid, str(e))
                    self._send(200 if html else 404, html or page("Not found", "<h1>No such card</h1>"))
        elif path.startswith("/edit/") or path == "/notes/new":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            if user is None:
                self._redirect("/login")
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Invalid form token.</p>"))
            elif path == "/notes/new":
                try:
                    new_note_from_form(form, user["id"])
                    self._redirect("/releases/pending")
                except ValueError as e:
                    csrf = auth.csrf_token(self._cookie_token(), session_secret())
                    self._send(200, render_new_note(user, csrf, str(e)))
            else:
                sid = path[len("/edit/"):]
                values = {f: form.get(f, [""])[0] for f in revisions.EDITABLE_FIELDS if f in form}
                try:
                    with db() as conn:
                        apply_edit_and_resolve(conn, sid, values, form, user["id"])
                    self._redirect("/reviewer")
                except (ValueError, LookupError) as e:
                    csrf = auth.csrf_token(self._cookie_token(), session_secret())
                    self._send(200, render_edit(user, csrf, sid, False, str(e)))
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


REVIEWER_INSTALL_HTML = """<h2 id="reviewers">Testing the full loop (reviewers)</h2>
<p class="sub">If you have a reviewer login, this walks the whole path in
about ten minutes &mdash; first as a student, then as a reviewer.</p>
<ol class="steps">
  <li><b>As a student:</b> install the add-on above, review a Doctrine card,
      click <b>&#9998; Suggest an edit</b>, describe something, add your email,
      send. Open the tracking link it gives you &mdash; it should say
      <i>In review</i>.</li>
  <li><b>As a reviewer:</b> <a href="/login">sign in</a>. Your suggestion is in
      the queue next to the card exactly as you saw it, the current master
      copy, and a <i>Trace</i> block with the card and deck identifiers.</li>
  <li><b>Resolve it.</b> Tick <i>Publish to updates</i>, write a one-line
      public summary, put a name in <i>Credit as</i>, click <b>Resolve</b>.</li>
  <li>Open <a href="/updates">Community updates</a> &mdash; your fix is listed
      with the credit. Reload your tracking link &mdash; it now says
      <i>Accepted</i>.</li>
  <li>Because you left an email, <b>Outbox</b> (top nav) has a thank-you
      note ready to copy. Copy it, then <b>Mark as thanked</b>.</li>
  <li>Try the other outcomes on a second suggestion: <b>Decline</b> and
      <b>Mark expired</b>. Neither publishes anything and neither queues a
      thank-you.</li>
</ol>

<h2>Known gaps in this test build</h2>
<ul class="steps">
  <li>Images and audio inside the card snapshot do not load &mdash; the
      deck's media is not hosted yet. Text, cloze markers and layout are
      intact.</li>
  <li>The web address is temporary. When the final domain is chosen you
      will install the add-on once more; nothing else changes.</li>
  <li>Reviewer passwords are handed over directly by an administrator.
      There is no email reset by design &mdash; ask an admin.</li>
</ul>
<p class="sub">Everything else is fair game for feedback: wording, layout,
what's missing from the queue, what a reviewer would want to see.</p>
"""


def render_install(user=None, session_token=None):
    # Reviewer walkthrough and known-gaps list only for signed-in staff;
    # the page is public and must not confuse students.
    reviewer_block = REVIEWER_INSTALL_HTML if user else ""
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

{reviewer_block}
<style>
.install-box {{ margin:18px 0 26px; padding:18px 20px; border:1px solid var(--line);
  border-radius:8px; background:#fafaf7; }}
.btn-primary {{ display:inline-block; padding:10px 16px; border-radius:6px;
  background:#2f6f4f; color:#fff !important; text-decoration:none; font-weight:600; }}
.steps li {{ margin:6px 0; }}
h2 {{ font-size:16px; margin-top:26px; }}
</style>"""
    return page("Install", body, "install", user,
                auth.csrf_token(session_token, session_secret()) if session_token else "")


# ---------------------------------------------------------------- editor

def _editor_field(name, value, rows):
    return f"""
<div class="ed-field">
  <label for="f-{esc(name)}">{esc(name)}</label>
  <textarea id="f-{esc(name)}" name="{esc(name)}" rows="{rows}" data-preview="p-{esc(name)}">{esc(value or "")}</textarea>
  <div class="ed-preview" id="p-{esc(name)}" aria-live="polite"></div>
</div>"""


EDITOR_SCRIPT = """
<script>
(function () {
  function render(src) {
    var html = src.replace(/\\{\\{c(\\d+)::(.*?)(::[^}]*)?\\}\\}/g,
      function (_, n, ans) { return '<mark title="cloze c' + n + '">' + ans + '</mark>'; });
    return html.replace(/src="([^"]+)"/g, function (_, f) {
      return /^(https?:|data:|\\/)/.test(f) ? 'src="' + f + '"' : 'src="/media/' + encodeURIComponent(f) + '"';
    });
  }
  document.querySelectorAll('textarea[data-preview]').forEach(function (ta) {
    var out = document.getElementById(ta.dataset.preview);
    var sync = function () { out.innerHTML = render(ta.value); };
    ta.addEventListener('input', sync); sync();
  });
})();
</script>"""

EDITOR_CSS = """
<style>
.ed-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.15fr); gap:28px; align-items:start; }
.ed-left blockquote { margin:8px 0 16px; padding:10px 12px; background:#f7f7f4; border-left:3px solid #cfd6cf; white-space:pre-wrap; }
.ed-left .pane { border:1px solid var(--line); border-radius:6px; }
.ed-field { margin:0 0 18px; }
.ed-field label { display:block; font-size:11px; font-weight:600; letter-spacing:.06em; text-transform:uppercase;
  color:var(--muted); margin:0 0 6px; }
.ed-field textarea { width:100%; box-sizing:border-box; padding:10px 12px; border:1px solid var(--line-strong);
  border-radius:6px; font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; resize:vertical; background:#fff; }
.ed-field textarea:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
.ed-preview { margin-top:6px; padding:10px 12px; border:1px dashed var(--line-strong); border-radius:6px;
  background:#fff; font-size:14.5px; line-height:1.5; min-height:2.6em; }
.ed-preview:empty::before { content:"(preview)"; color:var(--faint); }
.ed-preview mark { background:#fff1a8; padding:0 3px; border-radius:2px; }
.ed-preview img { max-width:100%; }
.ed-publish { display:grid; gap:10px; padding:14px 16px; border:1px solid var(--line); border-radius:8px;
  background:var(--panel); margin:6px 0 16px; }
.ed-publish input[type=text] { width:100%; box-sizing:border-box; }
.ed-actions { display:flex; gap:12px; align-items:center; }
@media (max-width: 900px) { .ed-grid { grid-template-columns:minmax(0,1fr); } }
</style>"""


def editor_form(action, values, csrf, with_publish=True, submit_label="Save and accept"):
    areas = "".join(_editor_field(f, values.get(f, ""), 14 if f == "Text" else 8)
                    for f in revisions.EDITABLE_FIELDS)
    publish = ""
    if with_publish:
        publish = """
<div class="ed-publish">
  <label class="chk"><input type="checkbox" name="publish" value="1"> Publish to the community updates page</label>
  <input type="text" name="publish_summary" placeholder="Public one-liner, e.g. 'Clarified HPV types on the vulvar carcinoma card'">
  <input type="text" name="credit_name" placeholder="Credit as (optional, e.g. Marko S.)">
  <input type="text" name="resolution_note" placeholder="Internal note (optional)">
</div>"""
    return f"""
<form method="post" action="{action}">
  <input type="hidden" name="csrf" value="{csrf}">
  {areas}
  <p class="small">Cloze markers look like <code>{{{{c1::answer}}}}</code> and are highlighted in the preview. Only Text and Extra can be edited.</p>
  {publish}
  <div class="ed-actions"><button type="submit">{esc(submit_label)}</button>
  <a class="btn-link" href="/reviewer">Cancel</a></div>
</form>"""


def render_edit(user, csrf, sid, use_proposal=False, error=None):
    with db() as conn:
        r = conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
        master = None
        if r is not None:
            master = conn.execute("SELECT * FROM notes WHERE doctrine_id=? AND retired=0",
                                  (r["doctrine_id"],)).fetchone()
    if r is None or master is None:
        return page("Not found", "<h1>Suggestion or note not found</h1>", "reviewer", user, csrf)
    current = json.loads(master["fields_json"] or "{}")
    values = dict(current)
    proposal_html = ""
    if r["proposed_fields_json"]:
        try:
            proposed = json.loads(r["proposed_fields_json"])
            if use_proposal:
                values.update(proposed)
            diffs = diff_fields(current, proposed)
            if diffs:
                proposal_html = ('<div class="pane-label">Student\'s proposed change</div><dl class="fields" style="height:auto;max-height:240px">'
                                 + "".join(f'<dt>{esc(f)}</dt><dd class="diff">{h}</dd>' for f, h in diffs) + "</dl>")
        except ValueError:
            pass
    msg = f'<p class="login-error">{esc(error)}</p>' if error else ""
    body = f"""
<h1>Edit card</h1>
<p class="sub">Suggestion #{r['id']} &middot; <code>{esc(r['doctrine_id'])}</code> &middot; master v{master['version']}.
Saving changes the card in the online deck, queues it for the next release, and accepts the suggestion.</p>
{msg}
<div class="ed-grid">
  <div class="ed-left">
    <div class="pane-label">Student's suggestion</div>
    <blockquote>{esc(r['text'])}</blockquote>
    {proposal_html}
    <div class="pane-label">Card as the student saw it</div>
    {card_iframe(r['snap_question'], r['snap_answer'], r['snap_css'], 'Snapshot')}
  </div>
  <div>{editor_form(f"/edit/{r['id']}", values, csrf)}</div>
</div>{EDITOR_CSS}{EDITOR_SCRIPT}"""
    return page("Edit card", body, "reviewer", user, csrf)


def render_new_note(user, csrf, error=None):
    with db() as conn:
        types = [r["note_type"] for r in conn.execute(
            "SELECT note_type, COUNT(*) n FROM notes GROUP BY note_type ORDER BY n DESC")]
    opts = "".join(f'<option value="{esc(t)}">{esc(t)}</option>' for t in types)
    msg = f'<p class="login-error">{esc(error)}</p>' if error else ""
    body = f"""
<h1>Add a note</h1>
<p class="sub">Creates an official note in the online deck (pending until the next release).
Tags decide where the scheduler places it.</p>
{msg}
<form method="post" action="/notes/new" class="login-form" style="max-width:720px">
  <input type="hidden" name="csrf" value="{csrf}">
  <div class="ed-field"><label for="nt">Note type</label>
  <select id="nt" name="note_type">{opts}</select></div>
  {_editor_field("Text", "", 12)}
  {_editor_field("Extra", "", 8)}
  <div class="ed-field"><label for="tags">Tags (space separated; the scheduler uses these)</label>
  <input id="tags" type="text" name="tags" style="width:100%;box-sizing:border-box"
         placeholder="Doctorine::Yield::Step1::Gold Doctorine::Shelf::Internal_Medicine::Silver"></div>
  <div class="ed-actions"><button type="submit">Add note</button>
  <a class="btn-link" href="/releases/next">Cancel</a></div>
</form>{EDITOR_CSS}{EDITOR_SCRIPT}"""
    return page("Add a note", body, "reviewer", user, csrf)


def render_pending(user, csrf):
    with db() as conn:
        groups = revisions.pending(conn)
        current = releases.current_release(conn)
    items = []
    for g in groups:
        rows = ""
        for rv in g["revisions"]:
            if rv["field"] == "added":
                rows += f'<dt>added</dt><dd class="diff"><ins>{esc(rv["after"])}</ins></dd>'
            else:
                rows += f'<dt>{esc(rv["field"])}</dt><dd class="diff">{diff_words(rv["before"], rv["after"])}</dd>'
        who = ", ".join(sorted({(rv["username"] or "?") for rv in g["revisions"]}))
        src = ", ".join(sorted({rv["source"] or "" for rv in g["revisions"]}))
        items.append(f"""
<div class="card"><div class="card-head"><span class="meta"><code>{esc(g['guid'])}</code>
&middot; {esc(who)} &middot; {esc(src)}</span></div>
<div class="proposal"><dl class="fields">{rows}</dl></div></div>""")
    n = len(groups)
    body = (f'<h1>Pending changes</h1><p class="sub">{n} note{"s" if n != 1 else ""} changed since '
            f'release {esc(current or "—")}. These ship together in the next release.</p>'
            + ("".join(items) or '<div class="empty"><b>Nothing pending</b>Accepted edits and new notes appear here.</div>'))
    return page("Pending changes", body, "pending", user, csrf)


def new_note_from_form(form, user_id):
    tags = [t for t in (form.get("tags", [""])[0]).split() if t]
    fields = {f: form.get(f, [""])[0] for f in revisions.EDITABLE_FIELDS}
    with db() as conn:
        return revisions.add_note(conn, form.get("note_type", [""])[0] or "AnKingOverhaul++++++",
                                  fields, tags, user_id)


# ---------------------------------------------------------------- re-upload

def create_upload(label, notes):
    """Compare the submitted notes against the online deck and store the result.

    Raises reupload.IdMismatch / ValueError; nothing is stored in that case.
    """
    incoming = {}
    for n in notes:
        guid = (n.get("guid") or n.get("doctrine_id") or "").strip()
        if not guid:
            continue
        incoming[guid] = {"fields": n.get("fields") or {}, "tags": list(n.get("tags") or []),
                          "note_type": n.get("note_type") or ""}
    with db() as conn:
        diff = reupload.compare(reupload.snapshot(conn), incoming,
                                pending_guids=reupload.pending_guids(conn))
        summary = {k: (len(v) if isinstance(v, (dict, list)) else v) for k, v in diff.items()}
        cur = conn.execute(
            """INSERT INTO uploads (label, incoming_json, diff_json, summary_json, created_at)
               VALUES (?,?,?,?,?)""",
            (label, json.dumps(incoming), json.dumps(diff), json.dumps(summary), now()))
        return cur.lastrowid, summary


def _upload_row(conn, upload_id):
    r = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
    return dict(r) if r else None


def render_upload(user, csrf, upload_id):
    with db() as conn:
        up = _upload_row(conn, upload_id)
    if up is None:
        return page("Not found", "<h1>Upload not found</h1>", "pending", user, csrf)
    diff = json.loads(up["diff_json"]); incoming = json.loads(up["incoming_json"]); summ = json.loads(up["summary_json"] or "{}")
    applied = up["status"] != "pending"

    def radio(guid, options, default):
        if applied:
            return ""
        return " ".join(
            f'<label class="chk"><input type="radio" name="d:{esc(guid)}" value="{v}"'
            f'{" checked" if v == default else ""}> {label}</label>' for v, label in options)

    def section(title, guids, body_fn, options=None, default=None):
        if not guids:
            return ""
        items = "".join(
            f'<div class="card"><div class="card-head"><span class="meta"><code>{esc(g)}</code></span>'
            f'<span class="row-actions">{radio(g, options, default) if options else ""}</span></div>'
            f'<div class="proposal">{body_fn(g)}</div></div>' for g in guids)
        return f'<h2>{title} <span class="count">{len(guids)}</span></h2>{items}'

    def field_diffs(d):
        return '<dl class="fields">' + "".join(
            f'<dt>{esc(f)}</dt><dd class="diff">{diff_words(b, a)}</dd>' for f, (b, a) in d.items()) + "</dl>"

    body = f"""
<h1>Re-upload: {esc(up['label'] or '')}</h1>
<p class="sub">Uploaded {esc(up['created_at'])} &middot; status <b>{esc(up['status'])}</b> &middot;
{summ.get('unchanged', 0)} unchanged, {summ.get('changed', 0)} changed, {summ.get('conflicts', 0)} conflicts,
{summ.get('new', 0)} new, {summ.get('missing', 0)} missing from the file.
Tag changes ({summ.get('tag_changes', 0)}) and changes to non-editable fields ({summ.get('other_field_changes', 0)})
are listed but never applied.</p>
<form method="post" action="/uploads/{up['id']}/apply">
<input type="hidden" name="csrf" value="{csrf}">
{section("Changed", list(diff["changed"]), lambda g: field_diffs(diff["changed"][g]["fields"]),
         [("file", "Apply file version"), ("skip", "Skip")], "file")}
{section("Conflicts (edited on the platform since the file's base)", list(diff["conflicts"]),
         lambda g: field_diffs(diff["conflicts"][g]),
         [("platform", "Keep platform version"), ("file", "Take file version")], "platform")}
{section("New notes", list(diff["new"]),
         lambda g: '<dl class="fields">' + "".join(f'<dt>{esc(k)}</dt><dd>{esc(v)}</dd>' for k, v in (incoming[g].get("fields") or {}).items() if v) + "</dl>",
         [("add", "Add"), ("skip", "Skip")], "add")}
{section("Missing from the file", diff["missing"], lambda g: '<p class="small">Not in the uploaded deck. Kept unless retired.</p>',
         [("keep", "Keep"), ("retire", "Retire")], "keep")}
{section("Tag changes (not applied)", list(diff["tag_changes"]),
         lambda g: f'<p class="small">platform: {esc(" ".join(diff["tag_changes"][g][0]))}<br>file: {esc(" ".join(diff["tag_changes"][g][1]))}</p>')}
{section("Other field changes (not applied)", list(diff["other_field_changes"]),
         lambda g: f'<p class="small">{esc(", ".join(diff["other_field_changes"][g]))}</p>')}
{"" if applied else '<div class="actions"><button type="submit">Apply selected</button></div>'}
</form>"""
    return page("Re-upload", body, "pending", user, csrf)


def apply_upload(upload_id, form, user_id):
    with db() as conn:
        up = _upload_row(conn, upload_id)
        if up is None or up["status"] != "pending":
            return False
        diff = json.loads(up["diff_json"]); incoming = json.loads(up["incoming_json"])
        decisions = {}
        for key, vals in form.items():
            if key.startswith("d:"):
                decisions[key[2:]] = vals[0]
        reupload.apply(conn, diff, incoming, decisions, user_id, up["id"])
        conn.execute("UPDATE uploads SET status='applied', applied_at=?, applied_by=? WHERE id=?",
                     (now(), user_id, up["id"]))
    return True


# ---------------------------------------------------------------- releases

def _previous_published(conn, before_version):
    r = conn.execute(
        "SELECT * FROM releases WHERE status='published' AND version != ? "
        "ORDER BY published_at DESC, created_at DESC LIMIT 1", (before_version,)).fetchone()
    return dict(r) if r else None


def _selected_guids(form):
    return [k[4:] for k in form if k.startswith("rev:")]


def changelog_for(conn, guids):
    lines = []
    for g in revisions.pending_by_kind(conn, set(guids)):
        lines.append(f"## {g['label']} ({len(g['notes'])})")
        for n in g["notes"]:
            fields = sorted({rv["field"] for rv in n["revisions"]})
            lines.append(f"- {n['guid']}: {', '.join(fields)}  [{', '.join(n['sources'])}]")
    return "\n".join(lines)


def cut_release(version, user_id, guids):
    """Draft release from the selected notes' pending changes. Returns (ok, message)."""
    version = (version or "").strip()
    if not version or "/" in version or ".." in version:
        return False, "Version is required (e.g. 1.1)."
    if not guids:
        return False, "Nothing selected. Tick the changes to include, then review the selection."
    with db() as conn:
        if conn.execute("SELECT 1 FROM releases WHERE version=?", (version,)).fetchone():
            return False, f"Release {version} already exists."
        groups = [g for g in revisions.pending(conn) if g["guid"] in set(guids)]
        if not groups:
            return False, "Nothing is pending for the selected notes."
        changelog = changelog_for(conn, guids)
        n = revisions.stamp_guids(conn, version, [g["guid"] for g in groups])
        conn.execute("UPDATE suggestions SET release=? WHERE status='resolved' AND release IS NULL "
                     "AND id IN (SELECT CAST(substr(source, 12) AS INTEGER) FROM revisions "
                     "WHERE release=? AND source LIKE 'suggestion:%')", (version, version))
        conn.execute(
            "INSERT INTO releases (version, status, note_count, changelog, created_at, built_by) "
            "VALUES (?, 'draft', (SELECT COUNT(*) FROM notes WHERE retired=0), ?, ?, ?)",
            (version, changelog, now(), user_id))
    return True, f"Release {version} drafted with {n} field change(s) across {len(groups)} note(s)."


def discard_release(version):
    """Drop a draft: its revisions go back to Next release."""
    with db() as conn:
        rel = conn.execute("SELECT status FROM releases WHERE version=?", (version,)).fetchone()
        if rel is None or rel["status"] not in ("draft", "failed"):
            return False
        revisions.unstamp(conn, version)
        conn.execute("UPDATE suggestions SET release=NULL WHERE release=?", (version,))
        conn.execute("DELETE FROM releases WHERE version=?", (version,))
    return True


def render_next_release(user, csrf):
    with db() as conn:
        groups = revisions.pending_by_kind(conn)
        current = releases.current_release(conn)
    total = sum(len(g["notes"]) for g in groups)
    sections = []
    for g in groups:
        rows = ""
        for n in g["notes"]:
            parts = ""
            for rv in n["revisions"]:
                if rv["field"] == "added":
                    parts += f'<dt>added</dt><dd class="diff"><ins>{esc((rv["after"] or "")[:400])}</ins></dd>'
                elif rv["field"] == "retired":
                    parts += '<dt>retired</dt><dd class="diff"><del>removed from the deck</del></dd>'
                else:
                    parts += f'<dt>{esc(rv["field"])}</dt><dd class="diff">{diff_words(rv["before"], rv["after"])}</dd>'
            src = ", ".join(s.replace("suggestion:", "#") for s in n["sources"])
            rows += f"""
<div class="card nr-note"><div class="card-head">
  <label class="chk"><input type="checkbox" name="rev:{esc(n['guid'])}" value="1" checked data-group="{esc(g['kind'])}"> <code>{esc(n['guid'])}</code></label>
  <span class="meta">{esc(', '.join(n['users']))} &middot; {esc(src)}</span>
</div><div class="proposal"><dl class="fields">{parts}</dl></div></div>"""
        sections.append(f"""
<h2 class="nr-head"><label class="chk"><input type="checkbox" checked data-toggle="{esc(g['kind'])}"> {esc(g['label'])}</label>
<span class="count">{len(g['notes'])}</span></h2>{rows}""")
    body = f"""
<h1>Next release</h1>
<p class="sub">Accepted changes waiting to ship, since release <b>{esc(current or '—')}</b>:
<b>{total} note{'s' if total != 1 else ''}</b>. Untick anything that should wait. Nothing is built from this page.</p>
{'<form method="post" action="/releases/confirm"><input type="hidden" name="csrf" value="' + csrf + '">' if groups else ''}
{''.join(sections) or '<div class="empty"><b>Nothing waiting</b>Accepted suggestions, direct edits and new notes appear here.</div>'}
{'<div class="actions"><button type="submit">Review selection</button></div></form>' if groups else ''}
<script>
document.querySelectorAll('input[data-toggle]').forEach(function (t) {{
  t.addEventListener('change', function () {{
    document.querySelectorAll('input[data-group="' + t.dataset.toggle + '"]').forEach(function (c) {{ c.checked = t.checked; }});
  }});
}});
</script>
<style>.nr-head {{ font-family:var(--serif); font-size:18px; margin:26px 0 10px; display:flex; align-items:center; gap:10px; }}
.nr-note .card-head code {{ margin-left:6px; }}</style>"""
    return page("Next release", body, "pending", user, csrf)


def render_confirm_selection(user, csrf, guids, error=None):
    with db() as conn:
        groups = revisions.pending_by_kind(conn, set(guids))
        cur = releases.current_release(conn)
    total = sum(len(g["notes"]) for g in groups)
    hidden = "".join(f'<input type="hidden" name="rev:{esc(n["guid"])}" value="1">' for g in groups for n in g["notes"])
    lines = "".join(f'<h3>{esc(g["label"])} <span class="count">{len(g["notes"])}</span></h3><ul>'
                    + "".join(f'<li><code>{esc(n["guid"])}</code> &middot; {esc(", ".join(sorted({rv["field"] for rv in n["revisions"]})))}</li>' for n in g["notes"])
                    + "</ul>" for g in groups)
    msg = f'<p class="login-error">{esc(error)}</p>' if error else ""
    body = f"""
<h1>Confirm release contents</h1>
<p class="sub"><b>{total} note{'s' if total != 1 else ''}</b> will be stamped with the new version. Everything else stays in Next release.
Cutting only drafts the release; building it is a separate confirmed step.</p>
{msg}
<div class="card"><div class="proposal">{lines or '<p>Nothing selected.</p>'}</div></div>
<form method="post" action="/releases/cut" class="create-form">
  <input type="hidden" name="csrf" value="{csrf}">{hidden}
  <input type="text" name="version" placeholder="Version, e.g. {esc(_bump(cur))}" required>
  <button type="submit" {'disabled' if not total else ''}>Cut release</button>
  <a class="btn-link" href="/releases/next">Back</a>
</form>"""
    return page("Confirm release", body, "pending", user, csrf)


def _bump(version):
    try:
        major, minor = (version or "1.0").split(".")[:2]
        return f"{major}.{int(minor) + 1}"
    except ValueError:
        return "1.1"


def render_build_confirm(user, csrf, version, error=None):
    with db() as conn:
        rel = conn.execute("SELECT * FROM releases WHERE version=?", (version,)).fetchone()
    if rel is None:
        return page("Not found", "<h1>No such release</h1>", "releases", user, csrf)
    msg = f'<p class="login-error">{esc(error)}</p>' if error else ""
    body = f"""
<h1>Build and publish release {esc(version)}</h1>
<p class="sub">This writes the new deck package, makes {esc(version)} the current release, and starts offering it
to students through the add-on and the download links. It cannot be undone from this page.</p>
{msg}
<div class="card"><div class="card-head"><span class="meta"><b>Changelog</b></span></div>
<div class="proposal"><pre class="small" style="white-space:pre-wrap;margin:0">{esc(rel['changelog'] or '')}</pre></div></div>
<form method="post" action="/releases/{esc(version)}/build" class="create-form">
  <input type="hidden" name="csrf" value="{csrf}">
  <label class="small">To continue, type the version: <b>{esc(version)}</b></label>
  <input type="text" name="confirm" autocomplete="off" placeholder="{esc(version)}">
  <button type="submit">Build and publish</button>
  <a class="btn-link" href="/releases">Back</a>
</form>"""
    return page("Build release", body, "releases", user, csrf)


def _build_release(version):
    """Runs in a background thread. Patches the previous collection, writes
    the package, publishes, and removes older package files."""
    try:
        with db() as conn:
            rel = dict(conn.execute("SELECT * FROM releases WHERE version=?", (version,)).fetchone())
            prev = _previous_published(conn, version)
            if prev is None or not prev.get("collection_path"):
                raise RuntimeError("no previous published release with a collection file")
            revs = [dict(r) for r in conn.execute(
                "SELECT * FROM revisions WHERE release=? ORDER BY id", (version,))]
            changed_guids = {r["guid"] for r in revs if r["field"] in revisions.EDITABLE_FIELDS}
            added_guids = [r["guid"] for r in revs if r["field"] == "added"]
            retired = [r["guid"] for r in revs if r["field"] == "retired"]
            changed = {}
            for guid in changed_guids - set(added_guids):
                row = conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (guid,)).fetchone()
                fields = json.loads(row["fields_json"]) if row else {}
                changed[guid] = {f: fields.get(f, "") for f in revisions.EDITABLE_FIELDS if f in fields}
            new_notes = []
            for guid in added_guids:
                row = conn.execute("SELECT note_type, fields_json, tags_json FROM notes WHERE doctrine_id=?", (guid,)).fetchone()
                if row:
                    new_notes.append({"guid": guid, "note_type": row["note_type"],
                                      "fields": json.loads(row["fields_json"]),
                                      "tags": json.loads(row["tags_json"] or "[]")})
            media = [(r["filename"], releases.sha_path(r["sha256"]))
                     for r in conn.execute("SELECT filename, sha256 FROM media ORDER BY filename")]

        d = releases.release_dir(version)
        os.makedirs(d, exist_ok=True)
        col_path = os.path.join(d, "collection.sqlite")
        counts = release_build.patch_collection(prev["collection_path"], col_path, changed, new_notes,
                                                retired, now=int(datetime.now(timezone.utc).timestamp()))
        pkg_path = os.path.join(d, f"Doctorine-{version}.apkg")
        info = release_build.write_apkg(pkg_path, col_path, [m for m in media if os.path.exists(m[1])])

        with db() as conn:
            conn.execute(
                "UPDATE releases SET status='published', collection_path=?, package_path=?, "
                "published_at=?, build_log=? WHERE version=?",
                (col_path, pkg_path, now(), json.dumps({"patch": counts, "package": info}), version))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_release', ?)", (version,))
            # Keep only the newest package on disk; collections stay per version.
            for r in conn.execute("SELECT version, package_path FROM releases WHERE version != ? "
                                  "AND package_path IS NOT NULL", (version,)).fetchall():
                try:
                    if r["package_path"] and os.path.exists(r["package_path"]):
                        os.remove(r["package_path"])
                except OSError:
                    pass
                conn.execute("UPDATE releases SET package_path=NULL WHERE version=?", (r["version"],))
        _official_cache["key"] = None
    except Exception as e:  # recorded, never raised into the thread
        import traceback
        with db() as conn:
            conn.execute("UPDATE releases SET status='failed', build_log=? WHERE version=?",
                         (f"{e}\n{traceback.format_exc()}", version))


def start_rebuild(version, user_id):
    """Produce the package for an already-published release from its own
    collection file. Does not change what is current."""
    with db() as conn:
        rel = conn.execute("SELECT status, collection_path FROM releases WHERE version=?", (version,)).fetchone()
        if rel is None or rel["status"] != "published" or not rel["collection_path"]:
            return False

    def run():
        try:
            with db() as conn:
                col_path = conn.execute("SELECT collection_path FROM releases WHERE version=?", (version,)).fetchone()["collection_path"]
                media = [(r["filename"], releases.sha_path(r["sha256"]))
                         for r in conn.execute("SELECT filename, sha256 FROM media ORDER BY filename")]
            pkg_path = os.path.join(releases.release_dir(version), f"Doctorine-{version}.apkg")
            release_build.write_apkg(pkg_path, col_path, [m for m in media if os.path.exists(m[1])])
            with db() as conn:
                conn.execute("UPDATE releases SET package_path=? WHERE version=?", (pkg_path, version))
        except Exception as e:
            with db() as conn:
                conn.execute("UPDATE releases SET build_log=? WHERE version=?", (f"rebuild failed: {e!r}", version))
    threading.Thread(target=run, daemon=True).start()
    return True


def start_build(version, user_id):
    with db() as conn:
        rel = conn.execute("SELECT status FROM releases WHERE version=?", (version,)).fetchone()
        if rel is None or rel["status"] not in ("draft", "failed"):
            return False
        conn.execute("UPDATE releases SET status='building', built_by=? WHERE version=?", (user_id, version))
    threading.Thread(target=_build_release, args=(version,), daemon=True).start()
    return True


def render_releases(user, csrf, notice=None, token_link=None):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM releases ORDER BY created_at DESC")]
        cur = releases.current_release(conn)
        pending_n = len(revisions.pending(conn))
        tokens = [dict(r) for r in conn.execute(
            "SELECT * FROM download_tokens ORDER BY created_at DESC LIMIT 20")] if user["role"] == "admin" else []
    items = []
    for r in rows:
        act = ""
        if r["status"] in ("draft", "failed"):
            act = (f'<a class="btn-link-primary" href="/releases/{esc(r["version"])}/confirm">Build and publish…</a> '
                   f'<form method="post" action="/releases/{esc(r["version"])}/discard" class="inline">'
                   f'<input type="hidden" name="csrf" value="{csrf}"><button class="ghost">Discard draft</button></form>')
        elif r["status"] == "building":
            act = '<span class="badge warn">Building…</span>'
        elif r["status"] == "published":
            if r.get("package_path") and os.path.exists(r["package_path"]):
                act = f'<span class="badge ok">Published</span> <a class="btn-link" href="/download/release/{esc(r["version"])}">Download</a>'
            else:
                act = (f'<span class="badge ok">Published</span> '
                       f'<form method="post" action="/releases/{esc(r["version"])}/rebuild" class="inline">'
                       f'<input type="hidden" name="csrf" value="{csrf}"><button class="ghost">Rebuild package</button></form>')
        log = f'<details class="trace"><summary>Build log</summary><pre>{esc(r["build_log"] or "")}</pre></details>' if r.get("build_log") else ""
        items.append(f"""
<div class="card"><div class="card-head">
  <span class="meta"><b>Release {esc(r['version'])}</b> &middot; {esc(r['status'])} &middot; {r['note_count'] or 0} notes
  {"&middot; current" if r['version'] == cur else ""}</span>
  <span class="row-actions">{act}</span>
  <time>{esc(r.get('published_at') or r['created_at'] or '')}</time>
</div>
{('<div class="proposal"><pre class="small" style="white-space:pre-wrap;margin:0">' + esc(r['changelog']) + '</pre>' + log + '</div>') if (r['changelog'] or log) else ''}</div>""")
    msg = f'<p class="notice">{esc(notice)}</p>' if notice else ""
    if token_link:
        msg += (f'<div class="pw-reveal"><p><strong>New download link</strong></p>'
                f'<div class="copyrow"><input type="text" readonly value="{esc(token_link)}" class="copyable">'
                f'<button type="button" class="copy-btn ghost" data-copy="{esc(token_link)}">Copy</button></div>'
                f'<p class="pw-note">Give this to the buyer. It serves the latest published release until it expires.</p></div>')
    cut = (f'<p><a class="btn-link-primary" href="/releases/next">Next release: {pending_n} note(s) waiting</a></p>'
           if pending_n else '<p class="small">Nothing is waiting for the next release.</p>')
    tok = ""
    if user["role"] == "admin":
        rows_t = "".join(
            f'<tr><td class="linkcell"><input type="text" readonly value="{esc(CFG.public_base_url)}/dl/{esc(t["token"])}" class="copyable">'
            f'<button type="button" class="copy-btn ghost" data-copy="{esc(CFG.public_base_url)}/dl/{esc(t["token"])}">Copy</button></td>'
            f'<td>{esc(t["note"] or "")}</td><td>{esc(t["expires_at"])}</td><td>{t["downloads"]}</td>'
            f'<td><form method="post" action="/releases/token/revoke" class="inline"><input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="token" value="{esc(t["token"])}"><button class="ghost">Revoke</button></form></td></tr>'
            for t in tokens)
        tok = f"""
<h2>Download links for buyers</h2>
<form method="post" action="/releases/token" class="create-form">
  <input type="hidden" name="csrf" value="{csrf}">
  <input type="text" name="note" placeholder="Who is this for (optional)">
  <input type="text" name="days" value="7" style="width:70px"> days
  <button type="submit">Create link</button>
</form>
<table class="users"><thead><tr><th>Link</th><th>For</th><th>Expires</th><th>Downloads</th><th></th></tr></thead><tbody>{rows_t}</tbody></table>
<p class="admin-note">A link always serves the <em>current</em> published release. Stripe can mint these automatically once a webhook secret is configured.</p>
<script>
document.querySelectorAll('.copy-btn[data-copy]').forEach(function (b) {{
  b.addEventListener('click', function () {{
    navigator.clipboard.writeText(b.dataset.copy).then(function () {{ b.textContent = 'Copied'; setTimeout(function () {{ b.textContent = 'Copy'; }}, 1500); }});
  }});
}});
</script>"""
    body = (f'<h1>Releases</h1><p class="sub">Current release: <b>{esc(cur or "—")}</b>. A release is cut from '
            f'<a href="/releases/next">Next release</a>, then built and published here in a separate, confirmed step.</p>'
            + msg + cut + '<h2 style="margin-top:28px">All releases</h2>' + "".join(items)
            + ('<hr style="border:0;border-top:1px solid var(--line);margin:34px 0 10px">' + tok if tok else ''))
    return page("Releases", body, "releases", user, csrf)


def mint_download_token(note, days, user_id):
    token = auth.new_token()
    expires = (datetime.now(timezone.utc) + __import__("datetime").timedelta(days=max(1, min(int(days or 7), 90))))
    with db() as conn:
        conn.execute("INSERT INTO download_tokens (token, note, created_by, created_at, expires_at) VALUES (?,?,?,?,?)",
                     (token, (note or "").strip(), user_id, now(), expires.strftime("%Y-%m-%d %H:%M UTC")))
    return token


def latest_package(conn):
    r = conn.execute("SELECT version, package_path FROM releases WHERE status='published' AND package_path IS NOT NULL "
                     "ORDER BY published_at DESC LIMIT 1").fetchone()
    return (r["version"], r["package_path"]) if r and os.path.exists(r["package_path"]) else (None, None)


def package_for_version(conn, version):
    r = conn.execute("SELECT version, package_path FROM releases WHERE version=? AND status='published'", (version,)).fetchone()
    return (r["version"], r["package_path"]) if r and r["package_path"] and os.path.exists(r["package_path"]) else (None, None)


# ---------------------------------------------------------------- sync

_MEDIA_REF = re.compile(r'(?:src=["\']|\[sound:)([^"\'\]]+)')


def _release_order(conn):
    return [r["version"] for r in conn.execute(
        "SELECT version FROM releases WHERE status='published' ORDER BY published_at, created_at")]


def sync_payload(conn, since):
    """Changes a collection at release `since` needs to reach the current release.

    Collapses multiple revisions of one field to its final value and reports
    `before` (the field at `since`) so the add-on can tell a local edit from
    an untouched card. Unknown `since` → everything after the first release.
    """
    order = _release_order(conn)
    current = order[-1] if order else None
    baseline_missing = since not in order
    later = order[order.index(since) + 1:] if not baseline_missing else order[1:]
    if not current or not later:
        return {"from": since, "to": current, "changed": [], "added": [], "retired": [],
                "media": [], "baseline_missing": baseline_missing}
    marks = ",".join("?" * len(later))
    revs = conn.execute(
        f"SELECT * FROM revisions WHERE release IN ({marks}) ORDER BY id", later).fetchall()
    added, retired, before = {}, [], {}
    touched = set()
    for r in revs:
        if r["field"] == "added":
            added[r["guid"]] = True
        elif r["field"] == "retired":
            retired.append(r["guid"])
        elif r["field"] in revisions.EDITABLE_FIELDS:
            touched.add(r["guid"])
            before.setdefault(r["guid"], {}).setdefault(r["field"], r["before"])
    changed, added_out, media = [], [], set()
    for guid in sorted(touched - set(added) - set(retired)):
        row = conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=? AND retired=0", (guid,)).fetchone()
        if row is None:
            continue
        fields = json.loads(row["fields_json"] or "{}")
        now_fields = {f: fields.get(f, "") for f in revisions.EDITABLE_FIELDS}
        b = {f: before[guid].get(f, now_fields[f]) for f in revisions.EDITABLE_FIELDS}
        changed.append({"guid": guid, "fields": now_fields, "before": b})
        for f in now_fields.values():
            media.update(_MEDIA_REF.findall(f or ""))
    for guid in added:
        row = conn.execute("SELECT note_type, fields_json, tags_json FROM notes WHERE doctrine_id=? AND retired=0", (guid,)).fetchone()
        if row is None:
            continue
        fields = json.loads(row["fields_json"] or "{}")
        added_out.append({"guid": guid, "note_type": row["note_type"], "fields": fields,
                          "tags": json.loads(row["tags_json"] or "[]")})
        for f in fields.values():
            media.update(_MEDIA_REF.findall(f or ""))
    known = set()
    if media:
        m = ",".join("?" * len(media))
        known = {r["filename"] for r in conn.execute(f"SELECT filename FROM media WHERE filename IN ({m})", sorted(media))}
    return {"from": since, "to": current, "changed": changed, "added": added_out,
            "retired": sorted(set(retired)), "media": sorted(known),
            "baseline_missing": baseline_missing}


# ---------------------------------------------------------------- cards (direct edit)

CARDS_LIMIT = 50


def search_cards(conn, q):
    q = (q or "").strip()
    if not q:
        return []
    exact = conn.execute("SELECT * FROM notes WHERE doctrine_id=? AND retired=0", (q,)).fetchone()
    if exact:
        return [exact]
    if q.isdigit():
        rows = conn.execute("SELECT * FROM notes WHERE anki_note_id=? AND retired=0", (int(q),)).fetchall()
        if rows:
            return rows
    like = f"%{q}%"
    return conn.execute(
        "SELECT * FROM notes WHERE retired=0 AND fields_json LIKE ? ORDER BY doctrine_id LIMIT ?",
        (like, CARDS_LIMIT + 1)).fetchall()


def render_cards(user, csrf, q):
    with db() as conn:
        rows = search_cards(conn, q)
    capped = len(rows) > CARDS_LIMIT
    rows = rows[:CARDS_LIMIT]
    items = []
    for r in rows:
        fields = json.loads(r["fields_json"] or "{}")
        text = re.sub(r"<[^>]+>", "", fields.get("Text", ""))[:160]
        extra = re.sub(r"<[^>]+>", "", fields.get("Extra", ""))[:120]
        items.append(f"""
<div class="card"><div class="card-head">
  <span class="meta"><code>{esc(r['doctrine_id'])}</code> &middot; nid {r['anki_note_id'] or '—'} &middot; v{r['version']}</span>
  <span class="row-actions"><a class="btn-link-primary" href="/cards/edit?guid={quote(r['doctrine_id'], safe='')}">Edit</a></span>
</div><div class="proposal"><p style="margin:0 0 4px"><b>{esc(text)}</b></p>
<p class="small" style="margin:0">{esc(extra)}</p>{tag_chips(r['tags_json'])}</div></div>""")
    note = ""
    if capped:
        note = f'<p class="small">Showing the first {CARDS_LIMIT} matches; narrow the search.</p>'
    elif q and not rows:
        note = '<p class="small">No official card matches.</p>'
    body = f"""
<h1>Cards</h1>
<p class="sub">Find any card in the online deck and edit it directly. Edits go to Next release like accepted suggestions.</p>
<form method="get" action="/cards" class="create-form">
  <input type="text" name="q" value="{esc(q or '')}" placeholder="Search text, or paste a note id / guid" style="min-width:360px" autofocus>
  <button type="submit">Search</button>
</form>
{note}{''.join(items)}"""
    return page("Cards", body, "cards", user, csrf)


def render_card_edit(user, csrf, guid, error=None):
    with db() as conn:
        master = conn.execute("SELECT * FROM notes WHERE doctrine_id=? AND retired=0", (guid,)).fetchone()
    if master is None:
        return None
    values = json.loads(master["fields_json"] or "{}")
    msg = f'<p class="login-error">{esc(error)}</p>' if error else ""
    tags = tag_chips(master["tags_json"])
    body = f"""
<h1>Edit card</h1>
<p class="sub"><code>{esc(guid)}</code> &middot; {esc(master['note_type'])} &middot; v{master['version']}.
Saving changes the card in the online deck and queues it for the next release.</p>
{tags}{msg}
<div class="ed-grid">
  <div class="ed-left">
    <div class="pane-label">Current card</div>
    {fields_pane(master['fields_json'], 'Current master')}
  </div>
  <div>{editor_form(f"/cards/edit?guid={quote(guid, safe='')}", values, csrf, with_publish=False, submit_label="Save")}</div>
</div>{EDITOR_CSS}{EDITOR_SCRIPT}"""
    return page("Edit card", body, "cards", user, csrf)


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
