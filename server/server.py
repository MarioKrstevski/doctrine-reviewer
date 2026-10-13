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

import collections
import hashlib
import shutil
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
import apkgfile
import cardview
import ui
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
                                           "published_at": "TEXT", "built_by": "INTEGER",
                                           "public_notes": "TEXT", "visibility": "TEXT"})
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT
        );""")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS installs (
            install_id TEXT PRIMARY KEY,
            license TEXT,
            addon_version TEXT,
            release TEXT,
            first_seen TEXT,
            last_seen TEXT,
            pings INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_installs_license ON installs(license);""")
        if conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 0:
            conn.execute("INSERT INTO decks (id, slug, name, created_at) VALUES (1, 'doctorine', 'Doctorine', datetime('now'))")
        _ensure_columns(conn, "users", {"active_deck": "INTEGER"})
        _ensure_columns(conn, "installs", {"deck": "TEXT"})
        _ensure_columns(conn, "media", {"platform_deck": "INTEGER DEFAULT 1"})
        for t in ("notes", "releases", "uploads", "suggestions", "download_tokens"):
            _ensure_columns(conn, t, {"platform_deck": "INTEGER DEFAULT 1"})
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


def _card_doc(q_html, a_html, css):
    q_html, a_html = rewrite_media(q_html), rewrite_media(a_html)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>body{{font-family:system-ui,sans-serif;padding:18px;margin:0;
background:#fff;color:#122031;font-size:15px;line-height:1.5}}
hr{{border:none;border-top:1px solid #E4E1D8;margin:16px 0}}
img{{max-width:100%}}
::-webkit-scrollbar{{width:10px;height:10px}}
::-webkit-scrollbar-thumb{{background:#D6D2C6;border-radius:5px;
border:2px solid #fff}}
::-webkit-scrollbar-track{{background:transparent}}
{css or ""}</style></head><body>
{q_html or "<i>(empty)</i>"}<hr>{a_html or "<i>(empty)</i>"}
</body></html>"""


def card_iframe(q_html, a_html, css, label):
    """Render a card snapshot inside a sandboxed iframe via srcdoc."""
    doc = _card_doc(q_html, a_html, css)
    return (f'<div class="pane"><div class="pane-label">{esc(label)}</div>'
            f'<iframe sandbox="" srcdoc="{esc(doc)}"></iframe></div>')


def anki_frame(q_html, a_html, css=""):
    """Just the sandboxed iframe: question, a rule, then the answer side, the way Anki shows it."""
    return f'<iframe class="anki-frame" sandbox="" srcdoc="{esc(_card_doc(q_html, a_html, css))}"></iframe>'


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
  border-radius:9px; background:var(--accent); color:#fff;
  font-size:11px; text-align:center; line-height:18px; }
.outbox-body { display:grid; grid-template-columns:1fr 1fr; gap:0; }
.outbox-col { padding:12px 16px; min-width:0; }
.outbox-col + .outbox-col { border-left:1px solid var(--line); }
.outbox-col blockquote { margin:8px 0; padding:10px 12px; background:var(--panel);
  border-left:3px solid var(--line-strong); white-space:pre-wrap; }
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
.proposal { padding:10px 16px 12px; border-top:1px solid var(--line); background:var(--panel); }
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
.chip { font-size:11px; padding:2px 8px; border-radius:10px; background:var(--dim-soft);
  color:var(--muted); border:1px solid var(--line); white-space:nowrap; }
.trace { padding:6px 16px; font-size:12px; color:var(--muted); }
.trace summary { cursor:pointer; user-select:none; }
.trace dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 14px;
  margin:6px 0 0; }
.trace dt { color:var(--muted); }
.trace dd { margin:0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.pane iframe { width:100%; height:270px; border:none; display:block;
  background:#fff; }
.pane.suggestion { background:var(--panel); }
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


def _staff_counts(deck_id=1):
    with db() as conn:
        inbox = conn.execute("SELECT COUNT(DISTINCT doctrine_id) FROM suggestions WHERE status='open' "
                             "AND COALESCE(platform_deck,1)=?", (deck_id,)).fetchone()[0]
        nxt = conn.execute("SELECT COUNT(DISTINCT r.guid) FROM revisions r JOIN notes n ON n.doctrine_id=r.guid "
                           "WHERE r.release IS NULL AND COALESCE(n.platform_deck,1)=?", (deck_id,)).fetchone()[0]
    return inbox, nxt, pending_thank_yous(deck_id)


def page(title, body, active="", user=None, csrf="", public=False):
    """public=True always uses the public header, even for a signed-in staff member."""
    head = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} | Doctrine</title>{FONTS_LINK}
<style>{BASE_CSS}{LEDGER_CSS}{SHELL_CSS}{ui.UI_CSS}</style></head><body>"""
    script = """<script>
document.querySelectorAll('.actions input[name=publish]').forEach(cb => {
  const fields = cb.closest('form').querySelector('.pub-fields');
  if (!fields) return;
  const sync = () => fields.classList.toggle('off', !cb.checked);
  cb.addEventListener('change', sync); sync();
});
</script>""" + ui.UI_JS + "</body></html>"

    if not user or public:
        def pub(href, label, key):
            cls = ' class="active"' if key == active else ""
            return f'<a href="{href}"{cls}>{label}</a>'
        back = (f'<a href="/reviewer" class="back">Back to workspace</a>' if user else "")
        return (head + f"""
<header class="site"><div class="wrap">
  <span class="brand">Doctrine<small>Card quality</small></span>
  <nav>{pub("/updates", "Updates", "updates")}{pub("/install", "Install the add-on", "install")}{back}</nav>
</div></header>
<div class="wrap">{body}</div>""" + script)

    decks, deck_id = deck_context(user)
    side = ui.sidebar(user, decks, deck_id, active, _staff_counts(deck_id), csrf, esc)
    cur = next((d for d in decks if d["id"] == deck_id), decks[0] if decks else None)
    mbar = (f'<div class="mbar"><button type="button" id="navbtn" aria-label="Menu">Menu</button>'
            f'<span class="brand">Doctrine</span><span class="deckname">{esc(cur["name"] if cur else "")}</span></div>')
    return (head + f'<div class="staff">{mbar}<div class="scrim" id="scrim"></div>{side}'
            f'<main><div class="wrap">{body}</div></main></div>' + script)


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


def _next_version_hint():
    """Version the next release would get (minor bump of the newest cut)."""
    try:
        with db() as conn:
            vs = [r[0] for r in conn.execute("SELECT version FROM releases")]
        if not vs:
            return "1.0"
        def key(v):
            return tuple(int(p) if p.isdigit() else 0 for p in v.split("."))
        top = max(vs, key=key)
        parts = list(key(top)) + [0, 0]
        return f"{parts[0]}.{parts[1] + 1}"
    except Exception:
        return "the next one"


def _mark_clozes(escaped):
    return _CLOZE.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped)


def _field_rows(fields):
    return "".join(f'<dt>{esc(k)}</dt><dd>{_mark_clozes(esc(v))}</dd>' for k, v in (fields or {}).items()
                   if k in revisions.EDITABLE_FIELDS and (v or "").strip()) or '<p class="small">No text.</p>'


def _rendered(fields):
    f = cardview.faces(fields)
    extra = (f'<div class="sec extra" style="margin:8px 0 0"><div class="lab">Extra</div>'
             f'<div class="txt">{f["extra"]}</div></div>') if f["extra"].strip() else ""
    return (f'<div class="sec front" style="margin:0"><div class="lab">Front</div><div class="txt">{f["front"]}</div></div>'
            f'<div class="sec back" style="margin:8px 0 0"><div class="lab">Back</div><div class="txt">{f["back"]}</div></div>'
            f'{extra}')


def _faces_frame(fields, css=""):
    """A card built from fields, shown as Anki's answer side: front, rule, back, extra."""
    f = cardview.faces(fields)
    extra = f'<br><br>{f["extra"]}' if f["extra"].strip() else ""
    return anki_frame(f["front"], f["back"] + extra, css)


def _diff_rows(base, fields):
    diffs = diff_fields(base, fields)
    return ("".join(f'<dt>{esc(f)}</dt><dd class="diff">{h}</dd>' for f, h in diffs)
            or '<p class="small">No difference in Text or Extra.</p>')


def _seg(group, options):
    """A small switch. Buttons show/hide the elements with data-group=group and data-value=<value>."""
    buttons = "".join(
        '<button type="button" data-show="%s"%s>%s</button>' % (v, ' class="on"' if i == 0 else "", t)
        for i, (v, t) in enumerate(options))
    return f'<span class="seg" data-for="{group}">{buttons}</span>'


def _est_height(*parts):
    """Pixel height for a card pane, from how much text it holds, so short cards do not leave a big blank frame."""
    text = " ".join(ui.plain_text(p) for p in parts if p)
    breaks = sum((p or "").lower().count(t) for p in parts for t in ("<br", "</p>", "</li>", "</div>"))
    lines = -(-len(text) // 48) + breaks
    return max(150, min(380, 90 + 24 * lines))


def _view(group, value, inner, cls="", hidden=False, height=None):
    style = f' style="height:{height}px"' if height else ""
    return (f'<div class="viewpane {cls}" data-group="{group}" data-value="{value}"{style}{" hidden" if hidden else ""}>'
            f'{inner}</div>')


def _html_view(fields):
    return f'<dl class="fields" style="height:auto;border:0;padding:0">{_field_rows(fields)}</dl>'


def _diff_view(base, fields):
    return f'<dl class="fields" style="height:auto;border:0;padding:0">{_diff_rows(base, fields)}</dl>'


def _saw_section(r, theirs, current, master, stale, css):
    """What the student saw. One card when ours is the same, two columns when ours has moved on."""
    sid = r["id"]
    g = f"s{sid}"
    if r["snap_question"] or r["snap_answer"]:
        saw_rendered = anki_frame(r["snap_question"], r["snap_answer"], css)
    else:
        saw_rendered = _faces_frame(theirs, css)
    if master is None:
        badge = '<span class="badge">No copy of this card on file</span>'
    elif stale:
        badge = '<span class="badge warn">Card changed since submission</span>'
    else:
        badge = f'<span class="badge ok">Same as the current card (v{master["version"]})</span>'
    options = [("rendered", "Rendered"), ("html", "HTML")] + ([("diff", "Differences")] if stale else [])
    if r["snap_question"] or r["snap_answer"]:
        parts = [r["snap_question"], r["snap_answer"]]
    else:
        f_ = cardview.faces(theirs)
        parts = [f_["front"], f_["back"], f_["extra"]]
    if stale:
        f2 = cardview.faces(current)
        parts += [f2["front"], f2["back"], f2["extra"]]
    h = _est_height(*parts)

    def col(label, rendered, fields, base):
        diff = _view(g, "diff", _diff_view(base, fields), "scroll", True, h) if base is not None else ""
        return (f'<div class="col"><div class="collab">{label}</div>'
                f'{_view(g, "rendered", rendered, "", False, h)}'
                f'{_view(g, "html", _html_view(fields), "scroll", True, h)}{diff}</div>')

    if stale:
        cols = (col("What the student saw", saw_rendered, theirs, current)
                + col(f'What we have now, v{master["version"]}', _faces_frame(current, css), current, theirs))
        layout = "two"
    else:
        cols = col("", saw_rendered, theirs, None)
        layout = "one"
    return (f'<section class="saw" data-scope><div class="sechead"><span class="t">What the student saw</span>{badge}'
            f'<span class="sp">{_seg(g, options)}</span></div><div class="cols {layout}">{cols}</div></section>')


def _ask_section(r, current, master, theirs, stale, css):
    """Their words, and on the right what they want when they proposed an exact change."""
    sid = r["id"]
    proposed = None
    if r["proposed_fields_json"]:
        try:
            proposed = json.loads(r["proposed_fields_json"])
        except ValueError:
            proposed = None
    has_proposal = bool(proposed) and master is not None and bool(diff_fields(current, proposed))
    kind = esc(TYPE_LABELS.get(r["suggestion_type"], r["suggestion_type"]))
    words = (f'<div><div class="lab">What they wrote <span class="badge dim">{kind}</span></div>'
             f'<div class="words">{esc(r["text"])}</div></div>')
    if not has_proposal:
        return f'<section class="ask"><div class="askgrid one">{words}</div></section>', False
    g = f"w{sid}"
    wanted = _faces_frame({**current, **proposed}, css)
    pf = cardview.faces({**current, **proposed})
    wh = _est_height(pf["front"], pf["back"], pf["extra"])
    stale_note = ('<p class="stale-hint" style="margin:8px 0 0">The student wrote this against an older version of '
                  'the card, so it cannot be accepted as is. Use Edit proposal.</p>') if stale else ""
    want = (f'<div data-scope><div class="lab">What they want {_seg(g, [("card", "Card"), ("diff", "Show differences")])}</div>'
            f'{_view(g, "card", wanted, "", False, wh)}{_view(g, "diff", _diff_view(current, proposed), "scroll", True, wh)}'
            f'{stale_note}</div>')
    return f'<section class="ask"><div class="askgrid two">{words}{want}</div></section>', True


def render_inbox_item(r, master, csrf):
    """One report, top to bottom: header, what they wrote and want, what they saw, then the actions."""
    sid = r["id"]
    status = r["status"]
    current = json.loads(master["fields_json"]) if master is not None else {}
    try:
        theirs = json.loads(r["snap_fields_json"] or "{}")
    except ValueError:
        theirs = {}
    if master is None:
        stale = False
    elif theirs:
        # Compare what a reviewer can change. Their copy matching ours is what makes a proposal safe to accept.
        stale = any(theirs.get(f, "") != current.get(f, "") for f in revisions.EDITABLE_FIELDS if f in theirs)
    else:
        stale = master["content_hash"] != r["snap_hash"]
    status_badge = {"open": '<span class="badge ok">Open</span>', "resolved": '<span class="badge dim">Resolved</span>',
                    "declined": '<span class="badge dim">Declined</span>',
                    "expired": '<span class="badge dim">Expired</span>'}[status]
    css = r["snap_css"] or ""
    ask, has_proposal = _ask_section(r, current, master, theirs, stale, css)
    saw = _saw_section(r, theirs, current, master, stale, css)

    note_id = f'Note {r["anki_note_id"]}' if r["anki_note_id"] else "Note"
    contact = f'<dt>email</dt><dd>{esc(r["email"])}</dd>' if r["email"] else ""
    details = (f'<details class="trace"><summary>Details</summary><dl>'
               f'<dt>deck</dt><dd>{esc(r["deck"])}</dd><dt>note type</dt><dd>{esc(r["note_type"])}</dd>'
               f'<dt>internal id</dt><dd><code>{esc(r["doctrine_id"])}</code></dd>{contact}</dl>'
               f'{trace_block(r)}</details>')
    email_chip = '<span class="badge dim">Email left</span>' if r["email"] else ""

    if status == "open":
        can_accept = has_proposal and not stale
        accept = '<button name="do" value="accept_proposal">Accept proposal</button>' if can_accept else ""
        edit_label = "Edit proposal" if has_proposal else "Edit card"
        actions = f"""
<div class="actions">
<form method="post" action="/reviewer/action">
  <input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="id" value="{sid}">
  <span class="hint">Accepting changes the card in the online deck and queues it for the next release.</span>
  <span class="btns">
    {accept}
    <a class="btn-link-primary" href="/edit/{sid}{'?from=proposal' if has_proposal else ''}">{edit_label}</a>
    <span class="menu-wrap"><button type="button" class="secondary" data-menu="mo{sid}">Close without changing</button>
      <div class="menu up" id="mo{sid}" hidden>
        <button name="do" value="duplicate" class="menu-item" title="Same issue as another report on this card. The student still gets a thank-you.">Duplicate</button>
        <button name="do" value="decline" class="menu-item">Decline</button>
        <button name="do" value="expire" class="menu-item">Mark expired</button>
      </div></span>
  </span>
</form></div>"""
    else:
        bits = []
        with db() as conn:
            edited = conn.execute("SELECT COUNT(*) FROM revisions WHERE source=?", (f"suggestion:{sid}",)).fetchone()[0]
        if edited:
            bits.append(f'<span class="badge ok">Edited</span> {edited} field change(s) in the next release')
        if r["resolution_note"]:
            bits.append(f"Note: {esc(r['resolution_note'])}")
        if r["published"]:
            bits.append(f"Published: &ldquo;{esc(r['publish_summary'])}&rdquo;")
        if r["closed_at"]:
            bits.append(ui.human_time(r["closed_at"]))
        actions = f'<div class="actions">{" &middot; ".join(bits) or "Closed."}</div>'

    return f"""
<div class="card">
  <div class="card-head">
    {status_badge} {email_chip}
    <span class="meta"><b>{note_id}</b> &middot; report #{sid}</span>
    <time>{ui.human_time(r["created_at"])}</time>
  </div>
  {tag_chips(r["tags_json"])}
  <div style="padding:0 16px">{details}</div>
  {ask}
  {saw}
  {actions}
</div>"""


def render_reviewer(user=None, secret=None, session_token=None):
    csrf = auth.csrf_token(session_token, secret) if session_token and secret else ""
    deck_id = active_deck_id(user) if user else 1
    with db() as conn:
        # Every open item, plus a bounded tail of closed ones. Neither
        # query may scale with the size of the deck or of history.
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE status='open' AND COALESCE(platform_deck,1)=? ORDER BY id DESC",
            (deck_id,)).fetchall()
        rows += conn.execute(
            "SELECT * FROM suggestions WHERE status!='open' AND COALESCE(platform_deck,1)=? "
            "ORDER BY id DESC LIMIT ?", (deck_id, RECENT_CLOSED_SHOWN)
        ).fetchall()
        closed_total = conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE status!='open' AND COALESCE(platform_deck,1)=?", (deck_id,)).fetchone()[0]
        masters = fetch_masters(conn, {r["doctrine_id"] for r in rows})

    items = []
    item_card = []
    for r in rows:
        item_card.append((r["doctrine_id"], r["status"], r["id"]))
        items.append(render_inbox_item(r, masters.get(r["doctrine_id"]), csrf))

    # Group open items by card. A card with several open reports gets a header.
    order, groups, closed = [], {}, []
    for html, (did, st, sid) in zip(items, item_card):
        if st == "open":
            if did not in groups:
                groups[did] = []; order.append(did)
            groups[did].append((sid, html))
        else:
            closed.append(html)
    parts = []
    for did in order:
        members = groups[did]
        if len(members) > 1:
            parts.append(f'''<section class="card-group"><div class="group-head">
<b>{len(members)} reports on this card</b> <code>{esc(did)}</code>
<form method="post" action="/reviewer/action" style="display:inline">
<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="id" value="{members[0][0]}">
<button name="do" value="resolve_card" class="secondary" title="Resolve every open report on this card as fixed (each student with an email gets a thank-you)">Card fixed, resolve all</button>
</form></div>''' + "".join(h for _, h in members) + "</section>")
        else:
            parts.append(members[0][1])
    n_open = len(order) and sum(len(m) for m in groups.values())
    nxt = _next_version_hint()
    banner = ui.infobox("How to read this page", (
        "<p>Each report opens with <b>what the student wrote</b> and, if they proposed an exact change, <b>what they want</b>. "
        "Below that is <b>what the student saw</b>. If our card has changed since, you see ours next to it. "
        "A proposal can only be accepted directly when the student saw the same card we have now.</p>"
        f"<p>Accepting adds the change to the next release ({esc(nxt)}). You choose what ships on the Next release page.</p>"))
    new_empty = ('<div class="empty"><b>Nothing new</b>When a student clicks Suggest an edit inside Anki, their report '
                 'arrives here with the card they were looking at.</div>')
    closed_empty = '<div class="empty"><b>Nothing closed yet</b>Accepted, declined and expired reports are kept here.</div>'
    closed_note = (f'<p class="small">Showing the latest {RECENT_CLOSED_SHOWN} of {closed_total} closed reports.</p>'
                   if closed_total > len(closed) else "")
    tabs = (f'<div class="tabs" data-tabs><button type="button" data-tab="new-pane">New '
            f'<span class="count">{n_open}</span></button>'
            f'<button type="button" data-tab="closed-pane">Closed <span class="count dim">{closed_total}</span></button></div>')
    body = ('<h1>Inbox</h1>' + banner + tabs
            + f'<div class="tabpane" id="new-pane">{"".join(parts) or new_empty}</div>'
            + f'<div class="tabpane" id="closed-pane" hidden>{closed_note}{"".join(closed) or closed_empty}</div>')
    return page("Inbox", body, "reviewer", user, csrf)


def render_updates(user=None):
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

    with db() as conn:
        public = [dict(r) for r in conn.execute(
            "SELECT version, published_at, public_notes FROM releases WHERE status='published' "
            "AND COALESCE(visibility,'public')!='silent' ORDER BY rowid DESC")]
    def entry(r):
        return f"""
<div class="entry">
  <div class="stamp"><b>#{r['id']}</b>{esc((r['closed_at'] or '').split(' ')[0])}</div>
  <div class="what"><span class="kind">{esc(TYPE_LABELS.get(r['suggestion_type'], 'Fix'))}</span>
    {esc(r['publish_summary'] or r['text'][:120])}</div>
  <div class="credit">{('suggested by ' + esc(r['credit_name'])) if r['credit_name'] else 'community suggestion'}</div>
</div>"""
    by_release = {}
    for r in rows:
        by_release.setdefault(r["release"] or "", []).append(entry(r))
    parts = []
    if by_release.get(""):
        parts.append('<h2 class="release-head">Accepted, in the next release</h2>' + "".join(by_release[""]))
    for rel in public:
        notes = (rel["public_notes"] or "").strip()
        when = esc((rel["published_at"] or "").split(" ")[0])
        notes_html = (f'<div class="release-notes">{"".join("<p>" + esc(p) + "</p>" for p in notes.split(chr(10) + chr(10)))}</div>'
                      if notes else "")
        fixes = by_release.get(rel["version"], [])
        if notes or fixes:
            parts.append(f'<h2 class="release-head">Release {esc(rel["version"])} <small>{when}</small></h2>'
                         + notes_html + "".join(fixes))
    if parts:
        ledger = f'<div class="ledger">{"".join(parts)}</div>'
    else:
        ledger = ('<div class="ledger"><div class="empty">No published updates '
                  'yet. Release notes and accepted suggestions will appear here.</div></div>')

    body = f"""
<h1>Updates</h1>
<p class="sub">Every entry below started as a suggestion sent by a student from
inside Anki, was reviewed by our team, and shipped as a card update.
{accepted} of {total} suggestions accepted so far.</p>
{ledger}"""
    return page("Updates", body, "updates", user, public=True)


def render_status(token, user=None):
    with db() as conn:
        r = conn.execute(
            "SELECT * FROM suggestions WHERE token=?", (token,)
        ).fetchone()
    if r is None:
        return page("Not found", "<h1>Suggestion not found</h1>")

    status_text = {
        "open": ("In review", "Our reviewers haven't gotten to this one yet."),
        "resolved": ("Accepted", "Thank you. The card was updated based on your suggestion."),
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
    return page("Your suggestion", body, user=user, public=True)


# ---------------------------------------------------------------- api

def is_official(guid) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM notes WHERE doctrine_id=? AND retired=0", (guid,)
        ).fetchone() is not None


WATCH_AT, FLAG_AT = 20, 100   # installs per license: watch, red flag


def record_ping(data):
    """Upsert one install from an add-on ping. Returns False for junk."""
    iid = str(data.get("install_id") or "").strip()[:64]
    if len(iid) < 8:
        return False
    lic = str(data.get("license") or "").strip()[:64] or None
    deck = str(data.get("deck") or "").strip()[:64] or None
    with db() as conn:
        conn.execute(
            """INSERT INTO installs (install_id, license, addon_version, release, first_seen, last_seen, deck)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(install_id) DO UPDATE SET
                 license=COALESCE(excluded.license, installs.license),
                 addon_version=excluded.addon_version, release=excluded.release,
                 last_seen=excluded.last_seen, pings=installs.pings+1,
                 deck=COALESCE(excluded.deck, installs.deck)""",
            (iid, lic, str(data.get("addon_version") or "")[:20],
             str(data.get("release") or "")[:40], now(), now(), deck))
    return True


def detection_rows(deck_slug=None):
    """([{license, note, installs, last_seen, level}], untagged, total) for one deck, busiest first."""
    with db() as conn:
        cond, args = ("(deck=? OR deck IS NULL)", (deck_slug,)) if deck_slug else ("1=1", ())
        lic = [dict(r) for r in conn.execute(
            f"SELECT license, COUNT(*) AS installs, MAX(last_seen) AS last_seen FROM installs "
            f"WHERE license IS NOT NULL AND license!='staff' AND {cond} GROUP BY license "
            f"ORDER BY installs DESC, last_seen DESC", args)]
        untagged = conn.execute(f"SELECT COUNT(*) FROM installs WHERE license IS NULL AND {cond}", args).fetchone()[0]
        total = conn.execute(f"SELECT COUNT(*) FROM installs WHERE {cond}", args).fetchone()[0]
        notes = {hashlib.sha256(r["token"].encode()).hexdigest()[:16]: r["note"]
                 for r in conn.execute("SELECT token, note FROM download_tokens")}
    for r in lic:
        r["note"] = notes.get(r["license"], "")
        r["level"] = "flag" if r["installs"] >= FLAG_AT else "watch" if r["installs"] >= WATCH_AT else "ok"
    return lic, untagged, total


def render_detection(user, csrf):
    decks, deck_id = deck_context(user)
    cur = next((d for d in decks if d["id"] == deck_id), decks[0])
    lic, untagged, total = detection_rows(cur["slug"])
    badge = {"flag": '<span class="badge bad">Red flag</span>',
             "watch": '<span class="badge warn">Watch</span>', "ok": '<span class="badge dim">Normal</span>'}
    rows = "".join(
        f'<tr><td><code>{esc(r["license"])}</code></td><td>{esc(r["note"] or "Unknown buyer")}</td>'
        f'<td class="num"><b>{r["installs"]}</b></td><td>{ui.human_time(r["last_seen"])}</td>'
        f'<td>{badge[r["level"]]}</td></tr>' for r in lic[:200])
    empty = ('<tr><td colspan="5" class="empty-row"><b>Nothing to show yet.</b><br>Rows appear once an add-on that was '
             'installed after downloading a buyer link opens Anki. The add-on reports once a day.</td></tr>')
    how = (f"<p>Every buyer download carries a hidden license id. The add-on reports that id with its own install id "
           f"once a day. If one license shows up on many installs, the file was probably shared.</p>"
           f"<ul><li><b>Watch</b> from {WATCH_AT} installs on one license, <b>red flag</b> from {FLAG_AT}.</li>"
           f"<li><b>Detection only.</b> Nothing is blocked and nobody is notified.</li>"
           f"<li><b>Without a license</b> means an install whose deck did not come from a tagged download, for example "
           f"one imported before tagging or from a staff download. It is not a problem.</li>"
           f"<li>Deleting the hidden file defeats it, so treat a flag as a hint and not as proof.</li></ul>")
    body = f"""
<h1>Detection</h1>
<p class="sub">Spots a download being shared. {esc(cur["name"])}.</p>
{ui.infobox("How detection works", how, True)}
<p class="queue-count"><b>{total}</b> installs seen &middot; <b>{len(lic)}</b> licenses &middot; <b>{untagged}</b> without a license</p>
<table class="tbl"><thead><tr><th>License</th><th>Buyer</th><th class="num">Installs</th><th>Last seen</th><th>Status</th></tr></thead>
<tbody>{rows or empty}</tbody></table>"""
    return page("Detection", body, "detection", user, csrf)


def deck_context(user):
    """(decks, active deck id) for a signed-in user. Falls back to the first deck."""
    with db() as conn:
        decks = [dict(r) for r in conn.execute("SELECT id, slug, name FROM decks ORDER BY id")]
    want = (user or {}).get("active_deck") or 1
    ids = {d["id"] for d in decks}
    return decks, (want if want in ids else (decks[0]["id"] if decks else 1))


def active_deck_id(user):
    return deck_context(user)[1]


def select_deck(user_id, slug):
    with db() as conn:
        d = conn.execute("SELECT id FROM decks WHERE slug=?", (slug,)).fetchone()
        if d:
            conn.execute("UPDATE users SET active_deck=? WHERE id=?", (d["id"], user_id))
            return True
    return False


def deck_from_slug(conn, slug):
    """Row for a deck slug; the first deck when none is given; None if unknown."""
    if not slug:
        return conn.execute("SELECT * FROM decks ORDER BY id LIMIT 1").fetchone()
    return conn.execute("SELECT * FROM decks WHERE slug=?", (slug,)).fetchone()


_official_cache = {}


def official_list(deck_id=1):
    """(etag, json bytes) for a deck's official guid set, cached in memory.

    Rebuilt only when the set changes: the cache key is (row count, newest
    updated_at, retired count), all cheap aggregates.
    """
    import hashlib as _h
    with db() as conn:
        key = tuple(conn.execute(
            "SELECT COUNT(*), MAX(updated_at), SUM(retired) FROM notes WHERE platform_deck=?",
            (deck_id,)).fetchone()) + (releases.current_release(conn, deck_id),)
        hit = _official_cache.get(deck_id)
        if hit and key == hit["key"]:
            return hit["etag"], hit["body"]
        guids = [r["doctrine_id"] for r in conn.execute(
            "SELECT doctrine_id FROM notes WHERE retired=0 AND platform_deck=? ORDER BY doctrine_id",
            (deck_id,))]
        release = releases.current_release(conn, deck_id)
    body = json.dumps({"release": release or "unreleased",
                       "count": len(guids), "guids": guids}).encode("utf-8")
    etag = '"' + _h.sha256(body).hexdigest()[:24] + '"'
    _official_cache[deck_id] = {"key": key, "body": body, "etag": etag}
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
        conn.execute("UPDATE suggestions SET platform_deck=COALESCE("
                     "(SELECT platform_deck FROM notes WHERE doctrine_id=?),1) WHERE token=?",
                     (doc_id, token))
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

    subject = "Thank you, your Doctrine suggestion was accepted"
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
        "The Doctrine team",
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


def open_siblings(conn, sid):
    """Other open suggestions on the same card as `sid`."""
    r = conn.execute("SELECT doctrine_id FROM suggestions WHERE id=?", (sid,)).fetchone()
    if r is None:
        return []
    return conn.execute(
        "SELECT * FROM suggestions WHERE doctrine_id=? AND status='open' AND id!=? "
        "ORDER BY id", (r["doctrine_id"], sid)).fetchall()


def resolve_together(conn, sid, also_ids, user_id, note="Fixed together with another report on this card."):
    """Resolve each id in also_ids (only open ones on the same card as sid) and
    queue a thank-you for each that left an email."""
    allowed = {r["id"] for r in open_siblings(conn, sid)}
    for other in also_ids:
        try:
            other = int(other)
        except (TypeError, ValueError):
            continue
        if other in allowed:
            if _close_suggestion(conn, other, "resolved", {"resolution_note": [note]}, user_id) == 1:
                queue_thank_you(conn, other)


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
    also = form.get("also", [])
    """Reviewer-edited fields: apply to the note, then resolve with publish fields."""
    r = conn.execute("SELECT doctrine_id FROM suggestions WHERE id=? AND status='open'",
                     (sid,)).fetchone()
    if r is None:
        raise LookupError("suggestion is not open")
    revisions.apply_edit(conn, r["doctrine_id"], new_values, user_id, f"suggestion:{sid}")
    if _close_suggestion(conn, sid, "resolved", form, user_id) == 1:
        queue_thank_you(conn, sid)
    resolve_together(conn, sid, also, user_id)


def reviewer_action(form, user_id=None):
    sid = form.get("id", [""])[0]
    action = form.get("do", [""])[0]
    if action == "accept_proposal" and sid:
        with db() as conn:
            accept_proposal(conn, sid, user_id)
        return
    if action == "duplicate" and sid:
        with db() as conn:
            if _close_suggestion(conn, sid, "resolved", {"resolution_note": [
                    "Duplicate of another report on this card."]}, user_id) == 1:
                queue_thank_you(conn, sid)
        return
    if action == "resolve_card" and sid:
        # sid = any open suggestion on the card; resolve all open ones there.
        with db() as conn:
            ids = [sid] + [str(r["id"]) for r in open_siblings(conn, sid)]
            for one in ids:
                if _close_suggestion(conn, one, "resolved", {"resolution_note": [
                        "Card fixed. This report is covered by the fix."]}, user_id) == 1:
                    queue_thank_you(conn, one)
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

    def _send_package(self, collection_path, version, license_id):
        """Build the .apkg on the fly. No Content-Length (HTTP/1.0, closes at the end)."""
        with db() as conn:
            row = conn.execute("SELECT COALESCE(platform_deck,1) AS d FROM releases WHERE version=?", (version,)).fetchone()
            media = media_for_package(conn, row["d"] if row else 1)
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="Doctorine-{version}.apkg"')
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            release_build.stream_apkg(self.wfile.write, collection_path, media,
                                      license=release_build.license_member(license_id, version))
        except (BrokenPipeError, ConnectionResetError):
            pass

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
                deck = deck_from_slug(conn, parse_qs(urlparse(self.path).query).get("deck", [""])[0])
                if deck is None:
                    return self._json(404, {"error": "Unknown deck."})
                cur = releases.current_release(conn, deck["id"])
                row = conn.execute("SELECT published_at, note_count FROM releases WHERE version=?", (cur,)).fetchone() if cur else None
            self._json(200, {"current": cur, "published_at": row["published_at"] if row else None,
                             "note_count": row["note_count"] if row else None})
        elif path == "/api/sync":
            q = parse_qs(urlparse(self.path).query)
            since = q.get("since", [""])[0]
            with db() as conn:
                deck = deck_from_slug(conn, q.get("deck", [""])[0])
                if deck is None:
                    return self._json(404, {"error": "Unknown deck."})
                payload = sync_payload(conn, since, deck["id"])
            self._json(200, payload)
        elif path == "/api/releases":
            with db() as conn:
                self._json(200, {"current": releases.current_release(conn),
                                 "releases": releases.list_releases(conn)})
        elif path == "/api/official":
            with db() as conn:
                deck = deck_from_slug(conn, parse_qs(urlparse(self.path).query).get("deck", [""])[0])
            if deck is None:
                return self._json(404, {"error": "Unknown deck."})
            etag, body = official_list(deck["id"])
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(200, body, "application/json",
                           {"ETag": etag, "Cache-Control": "public, max-age=300"})
        elif path == "/where":
            with db() as conn:
                deck = deck_from_slug(conn, parse_qs(urlparse(self.path).query).get("deck", [""])[0])
            self._send(
                200,
                json.dumps({
                    "api_base": CFG.public_base_url,
                    "min_addon_version": MIN_ADDON_VERSION,
                    "deck": deck["slug"] if deck else None,
                    "addon_version": addon_version(),
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
                                     "server image. Check the deployment.</p>"))
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
                version, pkg = latest_package(conn, (row["platform_deck"] or 1) if ok else 1) if ok else (None, None)
                if pkg:
                    conn.execute("UPDATE download_tokens SET downloads=downloads+1 WHERE token=?", (token,))
            if not pkg:
                self._send(404, page("Not found", "<h1>Link not valid</h1><p>This download link is "
                                     "unknown, expired, or no release is published yet.</p>"))
            else:
                lic = hashlib.sha256(token.encode()).hexdigest()[:16]
                self._send_package(pkg, version, lic)
        elif (path.startswith("/releases/") and path.count("/") == 2
              and path[len("/releases/"):] not in ("next", "status", "pending", "token", "confirm", "cut")):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                html_ = render_release_detail(user, auth.csrf_token(self._cookie_token(), session_secret()),
                                              unquote(path[len("/releases/"):]))
                self._send(200 if html_ else 404, html_ or page("Not found", "<h1>No such release</h1>"))
        elif path == "/releases/status":
            if self._current_user() is None:
                self._json(401, {"error": "Sign in."})
            else:
                with db() as conn:
                    n = conn.execute("SELECT COUNT(*) FROM releases WHERE status='building'").fetchone()[0]
                self._json(200, {"building": n})
        elif path == "/releases" or path.startswith("/download/release/"):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            elif path == "/releases":
                self._send(200, render_releases(user, auth.csrf_token(self._cookie_token(), session_secret())))
            else:
                want = path[len("/download/release/"):]
                with db() as conn:
                    version, pkg = (latest_package(conn, active_deck_id(user)) if want == "latest"
                                    else package_for_version(conn, want))
                if not pkg:
                    self._send(404, page("Not found", "<h1>No package for that release</h1>"))
                else:
                    self._send_package(pkg, version, "staff")
        elif path.startswith("/uploads/"):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                csrf = auth.csrf_token(self._cookie_token(), session_secret())
                self._send(200, render_upload(user, csrf, path[len("/uploads/"):].split("/")[0]))
        elif path == "/releases/pending":
            self._redirect("/releases/next")
        elif path == "/addon":
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                self._send(200, render_addon(user, auth.csrf_token(self._cookie_token(), session_secret())))
        elif path == "/detection":
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            elif user["role"] != "admin":
                self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Administrators only.</p>"))
            else:
                self._send(200, render_detection(user, auth.csrf_token(self._cookie_token(), session_secret())))
        elif path == "/deck" or path.startswith("/deck/status/"):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            elif path == "/deck":
                self._send(200, render_deck(user, auth.csrf_token(self._cookie_token(), session_secret())))
            else:
                with db() as conn:
                    up = _upload_row(conn, path[len("/deck/status/"):])
                if up is None:
                    self._json(404, {"error": "No such upload."})
                else:
                    self._json(200, {"status": up["status"],
                                     "error": json.loads(up["summary_json"] or "{}").get("error", "")})
        elif path in ("/cards", "/cards/edit"):
            user = self._current_user()
            if user is None:
                self._redirect("/login")
            else:
                csrf = auth.csrf_token(self._cookie_token(), session_secret())
                qs = parse_qs(urlparse(self.path).query)
                if path == "/cards":
                    self._send(200, render_cards(user, csrf, qs.get("q", [""])[0], qs.get("n", [""])[0]))
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
            self._send(200, render_updates(self._current_user()))
        elif path.startswith("/s/"):
            self._send(200, render_status(path[3:], self._current_user()))
        else:
            self._send(404, page("Not found", "<h1>Not found</h1>"))

    def _deck_upload(self):
        user = self._current_user()
        if user is None or user["role"] != "admin":
            return self._json(403, {"error": "Administrators only."})
        token = self._cookie_token()
        if not token or not auth.csrf_ok(self.headers.get("X-CSRF", ""), token, session_secret()):
            return self._json(403, {"error": "Invalid form token. Reload the page."})
        size = int(self.headers.get("Content-Length", 0))
        if size <= 0 or size > DECK_MAX:
            return self._json(400, {"error": "Missing or oversized file."})
        ok, free, need = deck_space(size)
        if not ok:
            return self._json(507, {"error": f"Not enough free space: {free / 1e9:.1f} GB free, "
                                             f"{need / 1e9:.1f} GB needed. Grow the volume or free space first."})
        label = unquote(parse_qs(urlparse(self.path).query).get("name", ["deck.apkg"])[0])[:200]
        with db() as conn:
            upload_id = conn.execute(
                "INSERT INTO uploads (label, status, incoming_json, diff_json, created_at, platform_deck) VALUES (?,?,?,?,?,?)",
                (label, "processing", "{}", "{}", now(), active_deck_id(user))).lastrowid
        tmp_dir = os.path.join(releases.blob_root(), "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, f"deck-{upload_id}.apkg")
        try:
            left = size
            with open(path, "wb") as out:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise ConnectionError("upload ended early")
                    out.write(chunk); left -= len(chunk)
        except Exception as e:
            try:
                os.remove(path)
            except OSError:
                pass
            _set_upload(upload_id, "failed", error=f"Upload interrupted: {e}")
            return
        threading.Thread(target=process_deck_upload, args=(upload_id, path, active_deck_id(user)), daemon=True).start()
        self._json(200, {"upload_id": upload_id})

    def _do_POST(self):
        if urlparse(self.path).path == "/deck/upload":
            return self._deck_upload()
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
                (path.startswith("/releases/") and (path.endswith("/build") or path.endswith("/discard"))):
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
                ok, message = cut_release(form.get("version", [""])[0], user["id"], guids,
                                          form.get("notes", [""])[0], form.get("visibility", ["public"])[0])
                if ok:
                    self._redirect("/releases")
                else:
                    self._send(200, render_confirm_selection(
                        user, auth.csrf_token(self._cookie_token(), session_secret()), guids, message))
            elif path.endswith("/discard"):
                discard_release(path[len("/releases/"):-len("/discard")])
                self._redirect("/releases/next")
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
                    token = mint_download_token(form.get("note", [""])[0], form.get("days", ["7"])[0], user["id"],
                                            active_deck_id(user))
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
        elif path == "/api/ping":
            try:
                ok = record_ping(json.loads(raw or b"{}"))
                self._json(200 if ok else 400, {"ok": ok})
            except (json.JSONDecodeError, AttributeError):
                self._json(400, {"ok": False})
        elif path == "/deck/select":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Invalid form token.</p>"))
            else:
                select_deck(user["id"], form.get("deck", [""])[0])
                back = urlparse(self.headers.get("Referer", "")).path or "/reviewer"
                self._redirect(back if back.startswith("/") and not back.startswith("//") else "/reviewer")
        elif path == "/deck/delete":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif user["role"] != "admin" or not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Administrators only.</p>"))
            else:
                with db() as conn:
                    d = conn.execute("SELECT id FROM decks WHERE slug=?", (form.get("deck", [""])[0],)).fetchone()
                ok, message = delete_deck(d["id"], form.get("confirm_name", [""])[0]) if d else (False, "No such deck.")
                user = self._current_user()
                self._send(200, render_deck(user, auth.csrf_token(self._cookie_token(), session_secret()), message))
        elif path == "/deck/new":
            user = self._current_user()
            form = parse_qs(raw.decode("utf-8"))
            if user is None:
                self._redirect("/login")
            elif user["role"] != "admin" or not self._csrf_ok(form):
                self._send(403, page("Forbidden", "<h1>Forbidden</h1><p>Administrators only.</p>"))
            else:
                add_deck(form.get("name", [""])[0])
                self._redirect("/deck")
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
                        _official_cache.clear()
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
                    new_note_from_form(form, user["id"], active_deck_id(user))
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
"""
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
  <p class="pw-note">Shown once. Copy it now and send it to them.
  It cannot be retrieved later, only reset.</p>
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
<h1>Users</h1>
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
There is no email in this system, so hand credentials over directly.</p>
"""
    return page("Users", body, "admin", user, csrf)


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
<p class="admin-note">Forgot your password? An administrator can reset it.
There is no email recovery.</p>
"""
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
            return ("That is the only active administrator. Create another "
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
_addon_version_cache = {}


def addon_version():
    """Version of the add-on package this server hands out, read from the package itself."""
    path = ADDON_PACKAGE_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _addon_version_cache.get("key") != (path, mtime):
        import zipfile
        version = None
        try:
            with zipfile.ZipFile(path) as z:
                m = re.search(r'^ADDON_VERSION\s*=\s*"([^"]+)"', z.read("__init__.py").decode("utf-8"), re.M)
                version = m.group(1) if m else None
        except (OSError, KeyError, zipfile.BadZipFile):
            version = None
        _addon_version_cache.update(key=(path, mtime), value=version)
    return _addon_version_cache["value"]


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


def render_addon(user, csrf):
    """Staff page: the download plus the full test loop. The public /install page stays buyer-facing."""
    body = f"""
<h1>Add-on and testing</h1>
<p class="sub">Install the add-on in a test profile and walk the whole loop, first as a student, then as a reviewer.</p>
<div class="card"><div class="proposal" style="padding:16px 18px">
  <a class="btn-link-primary" href="/download/{ADDON_PACKAGE_NAME}">Download {ADDON_PACKAGE_NAME}</a>
  <p class="small" style="margin:10px 0 0">Version {esc(addon_version() or "unknown")}. Anki 23.10 or newer, desktop only.
  Delete any older copy first (Tools, Add-ons, select it, Delete) and restart Anki after installing. The public
  buyer page is <a href="/install">/install</a>.</p>
</div></div>
{ui.infobox("What the add-on does", "<p>One add-on, two jobs. <b>Suggest an edit</b> sends a report or an exact proposed change from the reviewer. <b>Deck updates</b> checks for a newer release every time Anki opens and offers it, showing your version next to the newest one.</p>", True)}
<h2>The full loop</h2>
<ol class="steps">
  <li><b>Install and check.</b> Open Tools, Doctrine Editor, Diagnostics. It should show the version, the server as reachable and the deck <code>doctorine</code>.</li>
  <li><b>Suggest.</b> Review any Doctorine card, click <b>Suggest an edit</b>, describe the problem or type the exact change, add an email if you want a thank-you, and send. Send two on the same card to see them grouped.</li>
  <li><b>Inbox.</b> Open <a href="/reviewer">Inbox</a>. Try Accept proposal, Edit and accept, Duplicate and Card fixed.</li>
  <li><b>Outbox.</b> Every report with an email gets a thank-you in the <a href="/outbox">Outbox</a>. Copy it, then mark it thanked.</li>
  <li><b>Next release.</b> Review the changes on <a href="/releases/next">Next release</a>, choose what ships, pick minor or major, write notes or make it silent, and cut.</li>
  <li><b>Releases.</b> Build and publish the draft. The page refreshes itself when the build is done.</li>
  <li><b>Update in Anki.</b> Reopen Anki. The update prompt appears on its own. You can also run Tools, Doctrine Editor, Check for deck updates.</li>
  <li><b>Public pages.</b> <a href="/updates">Updates</a> shows the release notes. The suggestion tracking link now says Accepted.</li>
  <li><b>Detection.</b> Your install appears on <a href="/detection">Detection</a> after the first daily ping.</li>
</ol>
{ui.infobox("Known limits of this test build", "<ul><li>A deck update replaces the card text on this computer, including any local edits.</li><li>The web address is temporary. The final domain means installing the add-on once more.</li><li>Reviewer passwords are handed over by an administrator. There is no email reset.</li></ul>")}"""
    return page("Add-on and testing", body, "addon", user, csrf)




def render_install(user=None, session_token=None):
    # Reviewer walkthrough and known-gaps list only for signed-in staff;
    # the page is public and must not confuse students.
    reviewer_block = ""
    body = f"""
<h1>Install the Doctrine Editor add-on</h1>
<p class="sub">Adds a <b>&#9998; Suggest an edit</b> button to Anki's reviewer
on Doctrine cards. Suggestions go straight to the review team, and you get a
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

<h2>Updating your deck</h2>
<p>When we release a new version of the deck, the add-on offers it the next time Anki
opens. It shows <b>your version next to the newest one</b> and how many releases you are behind,
and <b>Update now</b> is the default choice. Updating rewrites only the text of the cards that
changed (the Text and Extra fields), downloads new images, and adds new cards. Your study progress,
tags and your own cards are not touched.</p>
<p class="notice"><b>If you edited a card yourself, an update replaces your edit on that card.</b>
Send us the change with <b>&#9998; Suggest an edit</b> instead, and everyone gets it. Edit &rarr; Undo
reverts an update right after it runs.</p>

<h2>Already have an older version?</h2>
<p>Tools &rarr; Add-ons &rarr; select <b>Doctrine Editor</b> &rarr; Delete, then
install the new file. Your settings and pending suggestions are unaffected.</p>

{reviewer_block}
"""
    return page("Install", body, "install", user,
                auth.csrf_token(session_token, session_secret()) if session_token else "", public=True)


# ---------------------------------------------------------------- editor

def _editor_field(name, value, rows):
    return f"""
<div class="ed-field">
  <label for="f-{esc(name)}">{esc(name)}</label>
  <textarea id="f-{esc(name)}" name="{esc(name)}" rows="{rows}" data-preview="p-{esc(name)}">{esc(value or "")}</textarea>
  <div class="ed-preview" id="p-{esc(name)}" aria-live="polite"></div>
</div>"""


EDITOR_SCRIPT = r"""
<script>
(function () {
  var BAD = /^(script|style|iframe|object|embed|link|meta|form|input|button)$/i;
  function clean(html) {
    var doc = new DOMParser().parseFromString('<div>' + html + '</div>', 'text/html');
    var root = doc.body.firstChild;
    root.querySelectorAll('*').forEach(function (el) {
      if (BAD.test(el.tagName)) { el.remove(); return; }
      Array.from(el.attributes).forEach(function (a) {
        var n = a.name.toLowerCase(), v = a.value;
        if (n.indexOf('on') === 0 || n === 'style' || n === 'srcdoc') { el.removeAttribute(a.name); return; }
        if ((n === 'href' || n === 'src') && /^\s*(javascript|vbscript):/i.test(v)) { el.removeAttribute(a.name); return; }
        if (n === 'src' && !/^(https?:|data:image\/|\/)/i.test(v)) el.setAttribute('src', '/media/' + encodeURIComponent(v));
      });
      if (el.tagName === 'IMG') el.setAttribute('loading', 'lazy');
    });
    return root.innerHTML;
  }
  var CLOZE = /\{\{c(\d+)::([\s\S]*?)(?:::([\s\S]*?))?\}\}/g;
  function cloze(text, active, reveal) {
    return text.replace(CLOZE, function (_, n, ans, hint) {
      if (+n !== active) return ans;
      return reveal ? '<span class="cloze-show">' + ans + '</span>'
                    : '<span class="cloze-hide">[' + (hint || '...') + ']</span>';
    });
  }
  function numbers(text) {
    var out = [], m; CLOZE.lastIndex = 0;
    while ((m = CLOZE.exec(text))) { if (out.indexOf(+m[1]) < 0) out.push(+m[1]); }
    return out.sort(function (a, b) { return a - b; });
  }
  function highlight(src) {
    return src.replace(CLOZE, function (_, n, ans) { return '<mark title="cloze c' + n + '">' + ans + '</mark>'; });
  }
  var areas = Array.from(document.querySelectorAll('textarea[data-preview]'));
  var initial = {};
  areas.forEach(function (ta) {
    initial[ta.name] = ta.dataset.initial !== undefined ? ta.dataset.initial : ta.value;
    var out = document.getElementById(ta.dataset.preview);
    var sync = function () { out.innerHTML = clean(highlight(ta.value)); dirty(); anki(); };
    ta.addEventListener('input', sync);
    ta._sync = sync;
  });
  var save = document.getElementById('savebtn');
  var status = document.getElementById('savestate');
  function dirty() {
    if (!save) return;
    var changed = areas.some(function (ta) { return ta.value !== initial[ta.name]; });
    if (!save.dataset.always) save.disabled = !changed;
    if (status) status.textContent = changed ? 'Unsaved changes' : 'No changes yet';
  }
  // Anki view
  var box = document.getElementById('anki');
  var side = 'front', active = 1;
  function field(name) { var t = document.querySelector('textarea[name="' + name + '"]'); return t ? t.value : ''; }
  function anki() {
    if (!box) return;
    var text = field('Text'), extra = field('Extra'), nums = numbers(text);
    var tabs = document.getElementById('anki-clozes'), face = document.getElementById('anki-face'), bar = document.getElementById('anki-bar');
    if (nums.length && nums.indexOf(active) < 0) active = nums[0];
    tabs.innerHTML = '';
    nums.forEach(function (n) {
      var b = document.createElement('button'); b.type = 'button'; b.textContent = 'c' + n;
      if (n === active) b.className = 'on';
      b.addEventListener('click', function () { active = n; side = 'front'; anki(); });
      tabs.appendChild(b);
    });
    var front = clean(cloze(text, active, false)), back = clean(cloze(text, active, true));
    face.innerHTML = side === 'front' ? front : back + (extra.trim() ? '<hr>' + clean(extra) : '');
    bar.innerHTML = '';
    if (side === 'front') {
      var s = document.createElement('button'); s.type = 'button'; s.textContent = 'Show answer (Space)';
      s.addEventListener('click', function () { side = 'back'; anki(); }); bar.appendChild(s);
    } else {
      [['Again', 1], ['Hard', 2], ['Good', 3], ['Easy', 4]].forEach(function (r) {
        var b = document.createElement('button'); b.type = 'button'; b.textContent = r[0] + ' (' + r[1] + ')';
        b.addEventListener('click', function () { rate(r[0]); }); bar.appendChild(b);
      });
    }
  }
  function rate(name) {
    var note = document.getElementById('anki-note');
    if (note) note.textContent = 'Rated ' + name + '. This is a preview, nothing is saved.';
    side = 'front'; anki();
  }
  document.addEventListener('keydown', function (e) {
    var pane = document.getElementById('anki-pane');
    if (!box || !pane || pane.hidden || /^(TEXTAREA|INPUT)$/.test((e.target || {}).tagName)) return;
    if (e.key === ' ') { e.preventDefault(); if (side === 'front') { side = 'back'; anki(); } }
    var map = {'1': 'Again', '2': 'Hard', '3': 'Good', '4': 'Easy'};
    if (side === 'back' && map[e.key]) rate(map[e.key]);
  });
  areas.forEach(function (ta) { ta._sync(); });
  dirty(); anki();
})();
</script>"""

EDITOR_CSS = """
<style>
.ed-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.15fr); gap:28px; align-items:start; }
.ed-left blockquote { margin:8px 0 16px; padding:10px 12px; background:var(--panel); border-left:3px solid var(--line-strong); white-space:pre-wrap; }
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
.card-group { border:1px solid var(--line); border-left:3px solid var(--blue); border-radius:10px; padding:10px 12px; margin:14px 0; }
.group-head { display:flex; gap:12px; align-items:center; justify-content:space-between; margin-bottom:8px; }
.closed-h { margin-top:28px; }
.ed-publish { display:grid; gap:10px; padding:14px 16px; border:1px solid var(--line); border-radius:8px;
  background:var(--panel); margin:6px 0 16px; }
.ed-publish input[type=text] { width:100%; box-sizing:border-box; }
.ed-actions { display:flex; gap:12px; align-items:center; }
@media (max-width: 900px) { .ed-grid { grid-template-columns:minmax(0,1fr); } }
</style>"""


def editor_form(action, values, csrf, with_publish=True, submit_label="Save and accept", extra_html="",
                always_enabled=False):
    areas = "".join(_editor_field(f, values.get(f, ""), 5 if f == "Text" else 7)
                    for f in revisions.EDITABLE_FIELDS)
    publish = ""
    if with_publish:
        publish = """
<details class="infobox" style="max-width:none"><summary><span class="i">+</span><span class="t">Credit this student on the Updates page (optional)</span></summary>
<div class="b ed-publish" style="border:0;padding-top:0">
  <label class="chk"><input type="checkbox" name="publish" value="1"> Publish a line about this fix on the Updates page</label>
  <input type="text" name="publish_summary" placeholder="Public one-liner, e.g. Clarified HPV types on the vulvar carcinoma card">
  <input type="text" name="credit_name" placeholder="Credit as (optional, e.g. Marko S.)">
  <input type="text" name="resolution_note" placeholder="Internal note (optional)">
</div></details>"""
    return f"""
<form method="post" action="{action}" id="edform">
  <input type="hidden" name="csrf" value="{csrf}">
  {areas}
  {extra_html}
  {publish}
  <div class="ed-actions"><button type="submit" id="savebtn"{' data-always="1"' if always_enabled else ' disabled'}>{esc(submit_label)}</button>
  <span class="small" id="savestate"></span>
  <a class="btn-link" href="/reviewer">Cancel</a></div>
</form>"""


_EDITOR_HINT = ui.infobox("Writing cards", "<p>Cloze markers look like <code>{{c1::answer}}</code> and are highlighted in the preview. "
                          "Only <b>Text</b> and <b>Extra</b> can be edited here. Tags are never changed by edits, "
                          "because the scheduler uses them.</p>")


def _editor_tabs(edit_html, history_html):
    anki_html = """
<div class="anki" id="anki"><div class="seg" id="anki-clozes" style="align-self:center;margin-bottom:14px"></div>
  <div class="face" id="anki-face"></div><div class="bar" id="anki-bar"></div>
  <small id="anki-note" style="margin-top:12px">Preview of the text in the editor. Space shows the answer, keys 1 to 4 rate. Ratings are not saved.</small></div>"""
    return f"""
<div class="tabs" data-tabs><button type="button" data-tab="edit-pane">Edit</button>
<button type="button" data-tab="history-pane">History</button>
<button type="button" data-tab="anki-pane">Anki view</button></div>
<div class="tabpane" id="edit-pane">{edit_html}</div>
<div class="tabpane" id="history-pane" hidden><div class="ledger">{history_html}</div></div>
<div class="tabpane" id="anki-pane" hidden>{anki_html}</div>"""


def render_edit(user, csrf, sid, use_proposal=False, error=None):
    with db() as conn:
        r = conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
        master = None
        sibs = []
        history = ""
        if r is not None:
            master = conn.execute("SELECT * FROM notes WHERE doctrine_id=? AND retired=0",
                                  (r["doctrine_id"],)).fetchone()
            sibs = open_siblings(conn, sid)
            history = card_history_html(conn, r["doctrine_id"]) if master else ""
    if r is None or master is None:
        return page("Not found", "<h1>Suggestion or note not found</h1>", "reviewer", user, csrf)
    sib_html = ""
    if sibs:
        rows_ = "".join(
            f'<label class="chk" style="display:flex"><input type="checkbox" name="also" value="{x["id"]}" checked> '
            f'#{x["id"]} &middot; {esc(TYPE_LABELS.get(x["suggestion_type"], x["suggestion_type"]))}: '
            f'{esc((x["text"] or "")[:160])}</label>' for x in sibs)
        sib_html = ('<div class="ed-publish" style="margin-bottom:12px"><b>Also resolve these reports on the same card</b>'
                    '<span class="small">Each student with an email gets a thank-you. '
                    f'Untick any this edit does not cover.</span>{rows_}</div>')
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
                proposal_html = ('<div class="pane-label" style="margin-top:10px">Proposed change</div><dl class="fields" style="height:auto;max-height:200px">'
                                 + "".join(f'<dt>{esc(f)}</dt><dd class="diff">{h}</dd>' for f, h in diffs) + "</dl>")
        except ValueError:
            pass
    pend = 0
    with db() as conn:
        pend = conn.execute("SELECT COUNT(*) FROM revisions WHERE guid=? AND release IS NULL", (r["doctrine_id"],)).fetchone()[0]
    notice = (f'<p class="notice">This card already has {pend} pending edit(s) waiting for the next release. '
              'The fields below include them.</p>') if pend else ""
    msg = f'<p class="login-error">{esc(error)}</p>' if error else ""
    context = f"""
<div class="card" style="margin:0 0 16px"><div class="card-head"><span class="meta"><b>Student report #{r['id']}</b>
&middot; {esc(TYPE_LABELS.get(r['suggestion_type'], r['suggestion_type']))}</span></div>
<div class="proposal"><blockquote style="margin:0">{esc(r['text'])}</blockquote>{proposal_html}</div></div>"""
    edit = (context + notice + msg + _EDITOR_HINT
            + editor_form(f"/edit/{r['id']}", values, csrf, extra_html=sib_html,
                          always_enabled=bool(use_proposal)))
    body = (f'<h1>Edit card</h1><p class="sub">Report #{r["id"]}, master v{master["version"]}. '
            'Saving changes the card in the online deck, queues it for the next release, and accepts the report.</p>'
            + _editor_tabs(edit, history) + EDITOR_CSS + EDITOR_SCRIPT)
    return page("Edit card", body, "reviewer", user, csrf)


def render_card_edit(user, csrf, guid, error=None):
    with db() as conn:
        master = conn.execute("SELECT * FROM notes WHERE doctrine_id=? AND retired=0", (guid,)).fetchone()
        history = card_history_html(conn, guid) if master else ""
        pend = conn.execute("SELECT COUNT(*) FROM revisions WHERE guid=? AND release IS NULL",
                            (guid,)).fetchone()[0] if master else 0
    if master is None:
        return None
    notice = (f'<p class="notice">This card already has {pend} pending edit(s) waiting for the next '
              'release. The fields below show them, and saving builds on top.</p>') if pend else ""
    values = json.loads(master["fields_json"] or "{}")
    msg = f'<p class="login-error">{esc(error)}</p>' if error else ""
    edit = (notice + msg + _EDITOR_HINT + tag_chips(master["tags_json"])
            + editor_form(f"/cards/edit?guid={quote(guid, safe='')}", values, csrf, with_publish=False, submit_label="Save"))
    body = (f'<h1>Edit card</h1><p class="sub"><code>{esc(guid)}</code> &middot; version {master["version"]}. '
            'Saving changes the card in the online deck and queues it for the next release.</p>'
            + _editor_tabs(edit, history) + EDITOR_CSS + EDITOR_SCRIPT)
    return page("Edit card", body, "cards", user, csrf)


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
  <div class="ed-field"><label for="tags">Tags (space separated, the scheduler uses these)</label>
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
            f'release {esc(current or "none")}. These ship together in the next release.</p>'
            + ("".join(items) or '<div class="empty"><b>Nothing pending</b>Accepted edits and new notes appear here.</div>'))
    return page("Pending changes", body, "pending", user, csrf)


def new_note_from_form(form, user_id, deck_id=1):
    tags = [t for t in (form.get("tags", [""])[0]).split() if t]
    fields = {f: form.get(f, [""])[0] for f in revisions.EDITABLE_FIELDS}
    with db() as conn:
        return revisions.add_note(conn, form.get("note_type", [""])[0] or "AnKingOverhaul++++++",
                                  fields, tags, user_id, platform_deck=deck_id)


# ---------------------------------------------------------------- re-upload

def _incoming_map(notes):
    incoming = {}
    for n in notes:
        guid = (n.get("guid") or n.get("doctrine_id") or "").strip()
        if not guid:
            continue
        incoming[guid] = {"fields": n.get("fields") or {}, "tags": list(n.get("tags") or []),
                          "note_type": n.get("note_type") or ""}
    return incoming


def create_upload(label, notes):
    """Compare the submitted notes against the online deck and store the result.

    Raises reupload.IdMismatch / ValueError; nothing is stored in that case.
    """
    incoming = _incoming_map(notes)
    with db() as conn:
        diff = reupload.compare(reupload.snapshot(conn), incoming,
                                pending_guids=reupload.pending_guids(conn))
        summary = {k: (len(v) if isinstance(v, (dict, list)) else v) for k, v in diff.items()}
        cur = conn.execute(
            """INSERT INTO uploads (label, incoming_json, diff_json, summary_json, created_at)
               VALUES (?,?,?,?,?)""",
            (label, json.dumps(incoming), json.dumps(diff), json.dumps(summary), now()))
        return cur.lastrowid, summary


def add_deck(name):
    """Create an empty deck; its slug is derived from the name."""
    name = (name or "").strip()[:80]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not name or not slug:
        return None
    with db() as conn:
        if conn.execute("SELECT 1 FROM decks WHERE slug=?", (slug,)).fetchone():
            return None
        return conn.execute("INSERT INTO decks (slug, name, created_at) VALUES (?,?,?)",
                            (slug, name, now())).lastrowid


DECK_MARGIN = 400 * 1024 * 1024   # headroom beyond the file itself
DECK_MAX = 6 * 1024 ** 3


def deck_space(size):
    """(ok, free, needed) for accepting an upload of `size` bytes."""
    root = releases.blob_root()
    os.makedirs(root, exist_ok=True)
    free = shutil.disk_usage(root).free
    need = int(size * 1.3) + DECK_MARGIN
    return free >= need, free, need


def _set_upload(upload_id, status, **summary):
    with db() as conn:
        conn.execute("UPDATE uploads SET status=?, summary_json=COALESCE(?, summary_json) WHERE id=?",
                     (status, json.dumps(summary) if summary else None, upload_id))


def process_deck_upload(upload_id, path, deck_id=1):
    """Background: read the .apkg, store its media (renaming a changed image that
    reuses a filename), compare notes with the online deck, hand over to review."""
    try:
        pkg = apkgfile.Package(path)
        notes = list(apkgfile.read_notes(pkg.collection_sqlite()))
        manifest = pkg.media_names()
        with db() as conn:
            known = {r["filename"]: r["sha256"] for r in conn.execute("SELECT filename, sha256 FROM media")}
        renames, stats = {}, {"media_new": 0, "media_same": 0, "media_renamed": 0}
        items = list(manifest.items())
        for start in range(0, len(items), 100):
            with db() as conn:
                for member, name in items[start:start + 100]:
                    if not releases.safe_filename(name):
                        continue
                    data = pkg.media_bytes(member)
                    sha = hashlib.sha256(data).hexdigest()
                    if name not in known:
                        releases.store_media(conn, name, data, deck_id); known[name] = sha; stats["media_new"] += 1
                    elif known[name] == sha:
                        stats["media_same"] += 1
                    else:
                        new_name = apkgfile.renamed(name, sha)
                        releases.store_media(conn, new_name, data, deck_id); known[new_name] = sha
                        renames[name] = new_name; stats["media_renamed"] += 1
        pkg.close()
        if renames:
            for n in notes:
                n["fields"] = {k: apkgfile.rename_refs(v, renames) for k, v in n["fields"].items()}
        incoming = _incoming_map(notes)
        with db() as conn:
            diff = reupload.compare(reupload.snapshot(conn, deck_id), incoming,
                                    pending_guids=reupload.pending_guids(conn))
            summary = {k: (len(v) if isinstance(v, (dict, list)) else v) for k, v in diff.items()}
            summary.update(stats)
            summary["renamed_files"] = renames
            conn.execute("UPDATE uploads SET status='pending', incoming_json=?, diff_json=?, summary_json=? WHERE id=?",
                         (json.dumps(incoming), json.dumps(diff), json.dumps(summary), upload_id))
    except reupload.IdMismatch as e:
        _set_upload(upload_id, "failed", error=str(e), kind="id_mismatch")
    except Exception as e:
        _set_upload(upload_id, "failed", error=f"Could not read the file: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def delete_deck(deck_id, typed_name):
    """Permanently remove a deck and everything that belongs to it. Returns (ok, message).

    The first deck can never be deleted. The typed name must match the deck's name.
    Media files are shared by name, so they stay.
    """
    with db() as conn:
        deck = conn.execute("SELECT * FROM decks WHERE id=?", (deck_id,)).fetchone()
        if deck is None:
            return False, "No such deck."
        first = conn.execute("SELECT id FROM decks ORDER BY id LIMIT 1").fetchone()["id"]
        if deck["id"] == first:
            return False, "The first deck cannot be deleted."
        if (typed_name or "").strip() != deck["name"]:
            return False, "The name you typed does not match."
        paths = [r["collection_path"] for r in conn.execute(
            "SELECT collection_path FROM releases WHERE platform_deck=? AND collection_path IS NOT NULL", (deck_id,))]
        guids = [r["doctrine_id"] for r in conn.execute("SELECT doctrine_id FROM notes WHERE platform_deck=?", (deck_id,))]
        for start in range(0, len(guids), 500):
            chunk = guids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM revisions WHERE guid IN ({marks})", chunk)
        conn.execute("DELETE FROM notifications WHERE suggestion_id IN (SELECT id FROM suggestions WHERE platform_deck=?)",
                     (deck_id,))
        for table in ("suggestions", "uploads", "download_tokens", "releases", "notes"):
            conn.execute(f"DELETE FROM {table} WHERE platform_deck=?", (deck_id,))
        conn.execute("DELETE FROM settings WHERE key=?", (releases.current_release_key(deck_id),))
        conn.execute("UPDATE users SET active_deck=NULL WHERE active_deck=?", (deck_id,))
        conn.execute("DELETE FROM decks WHERE id=?", (deck_id,))
    for p in paths:
        try:
            os.remove(p)
            os.rmdir(os.path.dirname(p))
        except OSError:
            pass
    _official_cache.clear()
    return True, f"Deleted {deck['name']}."


def render_deck(user, csrf, notice=None):
    decks, deck_id = deck_context(user)
    admin = user["role"] == "admin"
    with db() as conn:
        stats = {}
        for d in decks:
            stats[d["id"]] = {
                "cards": conn.execute("SELECT COUNT(*) FROM notes WHERE platform_deck=? AND retired=0", (d["id"],)).fetchone()[0],
                "retired": conn.execute("SELECT COUNT(*) FROM notes WHERE platform_deck=? AND retired=1", (d["id"],)).fetchone()[0],
                "release": releases.current_release(conn, d["id"]),
            }
        media = {d["id"]: conn.execute("SELECT COUNT(*) FROM media WHERE COALESCE(platform_deck,1)=?",
                                       (d["id"],)).fetchone()[0] for d in decks}
        ups = [dict(r) for r in conn.execute(
            "SELECT id, label, status, created_at FROM uploads WHERE COALESCE(platform_deck,1)=? "
            "ORDER BY id DESC LIMIT 15", (deck_id,))]
    cur = next((d for d in decks if d["id"] == deck_id), decks[0])
    st = stats[cur["id"]]

    rows = ""
    dialogs = ""
    for i, d in enumerate(decks, start=1):
        sel = d["id"] == deck_id
        info = stats[d["id"]]
        delete = ""
        if admin and i > 1:
            delete = (f'<button type="button" class="btn-text" data-dialog="del1-{d["id"]}">Delete</button>')
            dialogs += f"""
<div class="dlg-back" id="del1-{d["id"]}"><div class="dlg"><h3>Delete {esc(d["name"])}?</h3>
  <p>This permanently removes the deck, its {info["cards"]:,} cards, its releases, reports, download links and uploads.
  Students whose add-on is pinned to it will stop receiving updates.</p>
  <div class="foot"><button type="button" class="btn-quiet" data-close>Cancel</button>
  <button type="button" class="btn-danger" data-close data-dialog="del2-{d["id"]}">Continue</button></div></div></div>
<div class="dlg-back" id="del2-{d["id"]}"><div class="dlg"><h3>Are you really sure?</h3>
  <p>There is no undo. Deleting <b>{esc(d["name"])}</b> cannot be reversed, and the only way back is to upload the deck again.</p>
  <div class="foot"><button type="button" class="btn-quiet" data-close>Cancel</button>
  <button type="button" class="btn-danger" data-close data-dialog="del3-{d["id"]}">Yes, continue</button></div></div></div>
<div class="dlg-back" id="del3-{d["id"]}"><div class="dlg"><h3>Type the deck name to confirm</h3>
  <form method="post" action="/deck/delete">
    <input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="deck" value="{esc(d["slug"])}">
    <p>Type <b>{esc(d["name"])}</b> to delete it for good.</p>
    <input type="text" name="confirm_name" autocomplete="off" style="width:100%" data-confirm="{esc(d["name"])}">
    <div class="foot"><button type="button" class="btn-quiet" data-close>Cancel</button>
    <button type="submit" class="btn-danger" disabled>Delete forever</button></div>
  </form></div></div>"""
        rows += (f'<tr class="{"sel" if sel else ""}"><td class="num">{i}</td>'
                 f'<td><div class="row" style="gap:8px;flex-wrap:nowrap"><form method="post" action="/deck/select" class="inline"><input type="hidden" name="csrf" value="{csrf}">'
                 f'<input type="hidden" name="deck" value="{esc(d["slug"])}">'
                 f'<button type="submit" class="deck-pick">{esc(d["name"])}</button></form>'
                 f'{"<span class=\"badge current\">Selected</span>" if sel else ""}</div></td>'
                 f'<td><code>{esc(d["slug"])}</code></td><td class="num">{info["cards"]:,}</td>'
                 f'<td>{esc(info["release"] or "none yet")}</td><td>{delete}</td></tr>')

    add = ""
    if admin:
        add = f"""
<form method="post" action="/deck/new" class="row" style="margin:14px 0 0">
  <input type="hidden" name="csrf" value="{csrf}">
  <input type="text" name="name" placeholder="New deck name, for example Pathoma Companion" style="min-width:320px" required>
  <button type="submit" class="secondary">Add deck</button>
</form>"""
    if admin:
        upload = f"""
<div class="card"><div class="proposal" style="padding:16px 18px">
  <h3 style="margin:0 0 6px">Re-upload {esc(cur["name"])}</h3>
  {ui.infobox("How a re-upload works", "<p>Choose the new <code>.apkg</code>. Nothing changes yet. You get a side-by-side review of what differs, and approved changes go to Next release. Cards are matched by note id, and a file whose ids mostly differ is refused. A changed image that reuses an old filename is saved under a new name automatically.</p><p>Large files need spare room on the server, about 1.3 times the file size plus 0.4 GB. If there is not enough, the upload is refused with a message before anything is sent.</p>")}
  <div class="row"><input type="file" id="deckfile" accept=".apkg"><button id="deckgo" type="button">Upload and compare</button></div>
  <div id="deckmsg" class="small" style="margin-top:8px"></div>
  <progress id="deckprog" value="0" max="100" style="width:100%;display:none"></progress>
</div></div>
<script>
(function(){{
var f=document.getElementById('deckfile'),b=document.getElementById('deckgo'),m=document.getElementById('deckmsg'),p=document.getElementById('deckprog');
function poll(id){{fetch('/deck/status/'+id).then(r=>r.json()).then(function(s){{
  if(s.status==='pending'){{location.href='/uploads/'+id;}}
  else if(s.status==='failed'){{m.textContent=s.error||'Failed.';b.disabled=false;}}
  else{{m.textContent='Reading the file and comparing cards.';setTimeout(function(){{poll(id);}},2500);}}}});}}
b.addEventListener('click',function(){{
  var file=f.files[0]; if(!file){{m.textContent='Choose a .apkg first.';return;}}
  b.disabled=true;p.style.display='block';m.textContent='Uploading.';
  var x=new XMLHttpRequest();
  x.open('POST','/deck/upload?name='+encodeURIComponent(file.name));
  x.setRequestHeader('X-CSRF',{json.dumps(csrf)});
  x.upload.onprogress=function(e){{if(e.lengthComputable)p.value=100*e.loaded/e.total;}};
  x.onload=function(){{var r={{}};try{{r=JSON.parse(x.responseText);}}catch(e){{}}
    if(x.status===200){{p.style.display='none';m.textContent='Uploaded. Comparing cards.';poll(r.upload_id);}}
    else{{m.textContent=r.error||('Upload failed ('+x.status+').');b.disabled=false;}}}};
  x.onerror=function(){{m.textContent='Upload failed (connection).';b.disabled=false;}};
  x.send(file);
}});}})();
</script>"""
    else:
        upload = '<p class="small">Only administrators can upload a deck file.</p>'
    up_rows = "".join(
        f'<tr><td class="num">#{u["id"]}</td><td>{esc(u["label"] or "")}</td><td>{esc(u["status"])}</td>'
        f'<td>{ui.human_time(u["created_at"])}</td><td><a href="/uploads/{u["id"]}">Open</a></td></tr>' for u in ups) or \
        '<tr><td colspan="5" class="empty-row">No uploads yet.</td></tr>'
    msg = f'<p class="notice">{esc(notice)}</p>' if notice else ""
    body = f"""
<h1>Deck</h1>
<p class="sub">The online deck is the master copy every release is cut from.</p>
{msg}
<h2>Decks</h2>
{ui.infobox("About decks", "<p>Each deck has its own cards, releases and download. The add-on is pinned to one deck, so students of one deck never see another. Choose a deck below to work on it. The whole workspace follows your choice. Release versions must be unique across decks, for example <code>pathoma-1.0</code>.</p>")}
<table class="tbl"><thead><tr><th class="num">#</th><th>Deck</th><th>Add-on id</th><th class="num">Cards</th><th>Current release</th><th></th></tr></thead>
<tbody>{rows}</tbody></table>
{add}
<h2 style="margin-top:32px">{esc(cur["name"])}</h2>
<div class="card"><div class="proposal">
  <p style="margin:0"><b>{st["cards"]:,}</b> active cards{f' &middot; {st["retired"]:,} retired' if st["retired"] else ''}
  &middot; <b>{media[cur["id"]]:,}</b> media files &middot; current release <b>{esc(st["release"] or "none yet")}</b></p>
</div></div>
{upload}
<h2>Recent uploads</h2>
<table class="tbl"><thead><tr><th class="num">#</th><th>File</th><th>Status</th><th>When</th><th></th></tr></thead><tbody>{up_rows}</tbody></table>
{dialogs}
<script>
document.querySelectorAll('input[data-confirm]').forEach(function (i) {{
  var btn = i.closest('form').querySelector('button[type=submit]');
  i.addEventListener('input', function () {{ btn.disabled = i.value.trim() !== i.dataset.confirm; }});
}});
</script>"""
    return page("Deck", body, "deck", user, csrf)


def _upload_row(conn, upload_id):
    r = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
    return dict(r) if r else None


def render_upload(user, csrf, upload_id):
    with db() as conn:
        up = _upload_row(conn, upload_id)
    if up is None:
        return page("Not found", "<h1>Upload not found</h1>", "pending", user, csrf)
    if up["status"] in ("processing", "failed"):
        err = json.loads(up["summary_json"] or "{}").get("error", "")
        msg = "Still reading the file…" if up["status"] == "processing" else f"This upload failed. {err}"
        return page("Upload", f'<h1>Upload #{up["id"]}</h1><p class="sub">{esc(msg)}</p>'
                    '<p><a href="/deck">Back to Deck</a></p>', "deck", user, csrf)
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
        reupload.apply(conn, diff, incoming, decisions, user_id, up["id"], up.get("platform_deck") or 1)
        conn.execute("UPDATE uploads SET status='applied', applied_at=?, applied_by=? WHERE id=?",
                     (now(), user_id, up["id"]))
    return True


# ---------------------------------------------------------------- releases

def _previous_published(conn, before_version, deck_id=1):
    r = conn.execute(
        "SELECT * FROM releases WHERE status='published' AND version != ? AND COALESCE(platform_deck,1)=? "
        "ORDER BY published_at DESC, created_at DESC LIMIT 1", (before_version, deck_id)).fetchone()
    return dict(r) if r else None


def _selected_guids(form):
    return [k[4:] for k in form if k.startswith("rev:")]


def changelog_for(conn, guids):
    lines = []
    for g in revisions.pending_by_kind(conn, set(guids)):
        lines.append(f"{g['label']} ({len(g['notes'])})")
        for n in g["notes"]:
            fields = sorted({rv["field"] for rv in n["revisions"] if rv["field"] in revisions.EDITABLE_FIELDS})
            row = conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (n["guid"],)).fetchone()
            text = ui.plain_text((json.loads(row["fields_json"] or "{}") if row else {}).get("Text", ""), 90) or n["guid"]
            lines.append(f"- {text} ({', '.join(fields) or 'new card'}) by {', '.join(n['users'])}")
    return "\n".join(lines)


def cut_release(version, user_id, guids, notes="", visibility="public"):
    """Draft release from the selected notes' pending changes. Returns (ok, message)."""
    version = (version or "").strip()
    if not version or "/" in version or ".." in version:
        return False, "Version is required (e.g. 1.1)."
    if not guids:
        return False, "Nothing selected. Tick the changes to include, then review the selection."
    with db() as conn:
        if conn.execute("SELECT 1 FROM releases WHERE version=?", (version,)).fetchone():
            return False, f"Release {version} already exists."
        first = conn.execute("SELECT COALESCE(platform_deck,1) AS d FROM notes WHERE doctrine_id=?",
                             (next(iter(guids)),)).fetchone()
        deck_id = first["d"] if first else 1
        groups = [g for g in revisions.pending(conn, deck_id) if g["guid"] in set(guids)]
        if not groups:
            return False, "Nothing is pending for the selected notes."
        changelog = changelog_for(conn, guids)
        n = revisions.stamp_guids(conn, version, [g["guid"] for g in groups])
        conn.execute("UPDATE suggestions SET release=? WHERE status='resolved' AND release IS NULL "
                     "AND id IN (SELECT CAST(substr(source, 12) AS INTEGER) FROM revisions "
                     "WHERE release=? AND source LIKE 'suggestion:%')", (version, version))
        conn.execute(
            "INSERT INTO releases (version, status, note_count, changelog, created_at, built_by, "
            "public_notes, visibility, platform_deck) "
            "VALUES (?, 'draft', (SELECT COUNT(*) FROM notes WHERE retired=0 AND COALESCE(platform_deck,1)=?), ?, ?, ?, ?, ?, ?)",
            (version, deck_id, changelog, now(), user_id, (notes or "").strip() or None,
             "silent" if visibility == "silent" else "public", deck_id))
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
    deck_id = active_deck_id(user)
    with db() as conn:
        groups = revisions.pending_by_kind(conn, None, deck_id)
        current = releases.current_release(conn, deck_id)
    total = sum(len(g["notes"]) for g in groups)
    edits = sum(len(n["revisions"]) for g in groups for n in g["notes"])
    with db() as conn:
        snippets = {n["guid"]: ui.plain_text(((json.loads(row["fields_json"] or "{}")).get("Text", "")), 100)
                    for g in groups for n in g["notes"]
                    for row in [conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (n["guid"],)).fetchone()]
                    if row}
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
  <label class="chk"><input type="checkbox" name="rev:{esc(n['guid'])}" value="1" checked data-group="{esc(g['kind'])}"> <b>{esc(snippets.get(n['guid']) or n['guid'])}</b></label>
  <span class="badge dim">{len(n['revisions'])} edit{'s' if len(n['revisions']) != 1 else ''}</span>
  <span class="meta">by {esc(', '.join(n['users']))} &middot; from {esc(src or 'direct edit')} &middot; <code>{esc(n['guid'])}</code></span>
</div><div class="proposal"><dl class="fields">{parts}</dl></div></div>"""
        sections.append(f"""
<h2 class="nr-head"><label class="chk"><input type="checkbox" checked data-toggle="{esc(g['kind'])}"> {esc(g['label'])}</label>
<span class="count">{len(g['notes'])}</span></h2>{rows}""")
    body = f"""
<h1>Next release</h1>
<p class="sub">Changes waiting to ship since release <b>{esc(current or 'none')}</b>.</p>
{ui.infobox("How this page works", "<p>Everything accepted since the last release is listed here and ticked. Untick a card to hold it back for a later release. A card with two edits ships both or neither. Nothing is built from this page. Review selection is the next step.</p>")}
<p class="queue-count"><b>{edits}</b> edit{'s' if edits != 1 else ''} on <b>{total}</b> card{'s' if total != 1 else ''}</p>
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
    deck_id = active_deck_id(user)
    with db() as conn:
        groups = revisions.pending_by_kind(conn, set(guids), deck_id)
        cur = releases.current_release(conn, deck_id)
    total = sum(len(g["notes"]) for g in groups)
    hidden = "".join(f'<input type="hidden" name="rev:{esc(n["guid"])}" value="1">' for g in groups for n in g["notes"])
    lines = "".join(f'<h3>{esc(g["label"])} <span class="count">{len(g["notes"])}</span></h3><ul>'
                    + "".join(f'<li><code>{esc(n["guid"])}</code> &middot; {esc(", ".join(sorted({rv["field"] for rv in n["revisions"]})))}</li>' for n in g["notes"])
                    + "</ul>" for g in groups)
    msg = f'<p class="login-error">{esc(error)}</p>' if error else ""
    body = f"""
<h1>Confirm release contents</h1>
<p class="sub"><b>{total} note{'s' if total != 1 else ''}</b> will be stamped with the new version. Everything else stays in Next release.
Cutting only drafts the release. Building it is a separate confirmed step.</p>
{msg}
<div class="card"><div class="proposal">{lines or '<p>Nothing selected.</p>'}</div></div>
<form method="post" action="/releases/cut" class="create-form" style="display:grid;gap:14px;max-width:640px">
  <input type="hidden" name="csrf" value="{csrf}">{hidden}
  <div><b>Size of this release</b><br>
    <label class="chk"><input type="radio" name="size" value="minor" checked> Minor, fixes and small additions &rarr; {esc(_bump(cur))}</label><br>
    <label class="chk"><input type="radio" name="size" value="major"> Major, new content or restructuring &rarr; {esc(_bump_major(cur))}</label></div>
  <div><label for="ver"><b>Version</b></label><br>
    <input id="ver" type="text" name="version" value="{esc(_bump(cur))}" required></div>
  <div><b>Who sees this release</b><br>
    <label class="chk"><input type="radio" name="visibility" value="public" checked> Public: listed on the Updates page with the notes below</label><br>
    <label class="chk"><input type="radio" name="visibility" value="silent"> Silent: students get it through the add-on, nothing is announced</label></div>
  <div><label for="notes"><b>Release notes</b> (shown on the Updates page, a blank line starts a new paragraph)</label><br>
    <textarea id="notes" name="notes" rows="6" style="width:100%;box-sizing:border-box"></textarea></div>
  <div><button type="submit" {'disabled' if not total else ''}>Cut release</button>
  <a class="btn-link" href="/releases/next">Back</a></div>
</form>
<script>
(function(){{var v=document.getElementById('ver');
var m={{minor:{json.dumps(_bump(cur))},major:{json.dumps(_bump_major(cur))}}};
document.querySelectorAll('input[name=size]').forEach(function(r){{r.addEventListener('change',function(){{v.value=m[r.value];}});}});}})();
</script>"""
    return page("Confirm release", body, "pending", user, csrf)


def _bump(version):
    try:
        major, minor = (version or "1.0").split(".")[:2]
        return f"{major}.{int(minor) + 1}"
    except ValueError:
        return "1.1"


def _bump_major(version):
    try:
        return f"{int((version or '1.0').split('.')[0]) + 1}.0"
    except ValueError:
        return "2.0"


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


def _versions_through(conn, version):
    """Versions of every release cut up to and including `version`, in cut order."""
    out = []
    for r in conn.execute("SELECT version FROM releases ORDER BY rowid"):
        out.append(r["version"])
        if r["version"] == version:
            return out
    return out


def _build_release(version):
    """Runs in a background thread. Patches the previous collection, writes
    the package, publishes, and removes older package files."""
    try:
        with db() as conn:
            rel = dict(conn.execute("SELECT * FROM releases WHERE version=?", (version,)).fetchone())
            deck_id = rel.get("platform_deck") or 1
            prev = _previous_published(conn, version, deck_id)
            if prev is None or not prev.get("collection_path"):
                raise RuntimeError("no previous published release with a collection file")
            revs = [dict(r) for r in conn.execute(
                "SELECT * FROM revisions WHERE release=? ORDER BY id", (version,))]
            changed_guids = {r["guid"] for r in revs if r["field"] in revisions.EDITABLE_FIELDS}
            added_guids = [r["guid"] for r in revs if r["field"] == "added"]
            retired = [r["guid"] for r in revs if r["field"] == "retired"]
            included = set(_versions_through(conn, version))
            changed = {}
            for guid in changed_guids - set(added_guids):
                row = conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (guid,)).fetchone()
                fields = json.loads(row["fields_json"]) if row else {}
                at = revisions.text_at(conn, guid, included, fields)
                changed[guid] = {f: at.get(f, "") for f in revisions.EDITABLE_FIELDS if f in fields}
            new_notes = []
            for guid in added_guids:
                row = conn.execute("SELECT note_type, fields_json, tags_json FROM notes WHERE doctrine_id=?", (guid,)).fetchone()
                if row:
                    nf = json.loads(row["fields_json"])
                    nf.update(revisions.text_at(conn, guid, included, nf))
                    new_notes.append({"guid": guid, "note_type": row["note_type"],
                                      "fields": nf,
                                      "tags": json.loads(row["tags_json"] or "[]")})
            media = media_for_package(conn, deck_id)

        d = releases.release_dir(version)
        os.makedirs(d, exist_ok=True)
        col_path = os.path.join(d, "collection.sqlite")
        counts = release_build.patch_collection(prev["collection_path"], col_path, changed, new_notes,
                                                retired, now=int(datetime.now(timezone.utc).timestamp()))
        info = {"media": len(media), "stored_package": False}

        with db() as conn:
            conn.execute(
                "UPDATE releases SET status='published', collection_path=?, package_path=NULL, "
                "published_at=?, build_log=? WHERE version=?",
                (col_path, now(), json.dumps({"patch": counts, "package": info}), version))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         (releases.current_release_key(deck_id), version))
        _prune_collections(deck_id)
        _official_cache.clear()
    except Exception as e:  # recorded, never raised into the thread
        import traceback
        with db() as conn:
            conn.execute("UPDATE releases SET status='failed', build_log=? WHERE version=?",
                         (f"{e}\n{traceback.format_exc()}", version))


def _prune_collections(deck_id=1, keep=2):
    """Keep the newest `keep` release collections on disk; older ones are not
    needed to build the next release or to serve downloads."""
    with db() as conn:
        rows = conn.execute("SELECT version, collection_path FROM releases WHERE status='published' "
                            "AND collection_path IS NOT NULL AND COALESCE(platform_deck,1)=? "
                            "ORDER BY published_at DESC, rowid DESC", (deck_id,)).fetchall()
        for r in rows[keep:]:
            try:
                if os.path.exists(r["collection_path"]):
                    os.remove(r["collection_path"])
            except OSError:
                continue
            conn.execute("UPDATE releases SET collection_path=NULL WHERE version=?", (r["version"],))


def start_build(version, user_id):
    with db() as conn:
        rel = conn.execute("SELECT status FROM releases WHERE version=?", (version,)).fetchone()
        if rel is None or rel["status"] not in ("draft", "failed"):
            return False
        conn.execute("UPDATE releases SET status='building', built_by=? WHERE version=?", (user_id, version))
    threading.Thread(target=_build_release, args=(version,), daemon=True).start()
    return True


def release_changes(conn, version):
    """Readable lines for what a release changed: [{kind, label, snippet, fields, who, guid}]."""
    out, seen = [], {}
    for rv in conn.execute(
            "SELECT r.*, u.username FROM revisions r LEFT JOIN users u ON u.id=r.user_id "
            "WHERE r.release=? ORDER BY r.id", (version,)):
        rv = dict(rv)
        e = seen.get(rv["guid"])
        if e is None:
            row = conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=?", (rv["guid"],)).fetchone()
            text = ui.plain_text((json.loads(row["fields_json"] or "{}") if row else {}).get("Text", ""), 90)
            kind = revisions.kind_of(conn, rv)
            e = {"kind": kind, "label": revisions.KIND_LABELS.get(kind, kind), "snippet": text or rv["guid"],
                 "fields": set(), "who": set(), "guid": rv["guid"]}
            seen[rv["guid"]] = e
            out.append(e)
        if rv["field"] in revisions.EDITABLE_FIELDS:
            e["fields"].add(rv["field"])
        if rv["username"]:
            e["who"].add(rv["username"])
    for e in out:
        e["fields"], e["who"] = sorted(e["fields"]), sorted(e["who"])
    return out


def _changes_html(changes):
    if not changes:
        return ""
    lis = "".join(
        f'<li><span class="k">{esc(c["label"])}</span>{esc(c["snippet"])}'
        f'<span class="by">{esc(", ".join(c["fields"]))}{" by " + esc(", ".join(c["who"])) if c["who"] else ""}</span></li>'
        for c in changes[:200])
    more = f'<li class="by">and {len(changes) - 200} more</li>' if len(changes) > 200 else ""
    return f'<ul class="chg">{lis}{more}</ul>'


def release_detail_rows(conn, version):
    """One row per card changed in a release, with who, why and what the student said."""
    order, cards = [], {}
    for rv in conn.execute(
            "SELECT r.*, u.username FROM revisions r LEFT JOIN users u ON u.id=r.user_id "
            "WHERE r.release=? ORDER BY r.id", (version,)):
        rv = dict(rv)
        c = cards.get(rv["guid"])
        if c is None:
            note = conn.execute("SELECT anki_note_id, fields_json FROM notes WHERE doctrine_id=?", (rv["guid"],)).fetchone()
            fields = json.loads(note["fields_json"] or "{}") if note else {}
            c = {"guid": rv["guid"], "nid": note["anki_note_id"] if note else None,
                 "snippet": ui.plain_text(fields.get("Text", ""), 90) or rv["guid"],
                 "kind": revisions.kind_of(conn, rv), "changes": [], "who": set(), "sources": [], "when": rv["created_at"]}
            cards[rv["guid"]] = c
            order.append(rv["guid"])
        c["who"].add(rv["username"] or "unknown")
        if rv["source"] and rv["source"] not in c["sources"]:
            c["sources"].append(rv["source"])
        c["changes"].append({"field": rv["field"], "before": rv["before"] or "", "after": rv["after"] or ""})
    out = []
    for g in order:
        c = cards[g]
        c["reports"] = []
        for src in c["sources"]:
            if src.startswith("suggestion:"):
                sg = conn.execute("SELECT * FROM suggestions WHERE id=?", (src.split(":", 1)[1],)).fetchone()
                if sg:
                    nt = conn.execute("SELECT status, actioned_at FROM notifications WHERE suggestion_id=?", (sg["id"],)).fetchone()
                    c["reports"].append({"id": sg["id"], "text": sg["text"], "email": sg["email"],
                                         "proposed": bool(sg["proposed_fields_json"]), "when": sg["created_at"],
                                         "thanks": (nt["status"] if nt else ("none" if not (sg["email"] or "").strip() else "pending"))})
        out.append(c)
    return out


def render_release_detail(user, csrf, version):
    with db() as conn:
        rel = conn.execute("SELECT * FROM releases WHERE version=?", (version,)).fetchone()
        if rel is None:
            return None
        rel = dict(rel)
        rows = release_detail_rows(conn, version)
        builder = conn.execute("SELECT username FROM users WHERE id=?", (rel["built_by"],)).fetchone() if rel.get("built_by") else None
        deck = conn.execute("SELECT name FROM decks WHERE id=?", (rel.get("platform_deck") or 1,)).fetchone()
    n_edits = sum(len(c["changes"]) for c in rows)
    from_students = sum(1 for c in rows if c["reports"])
    thanks = collections.Counter(rp["thanks"] for c in rows for rp in c["reports"])
    kind_badge = lambda k: f'<span class="badge dim">{esc(revisions.KIND_LABELS.get(k, k))}</span>'

    def change_html(ch):
        if ch["field"] == "added":
            return '<div class="chgline"><span class="k">New card</span></div>'
        if ch["field"] == "retired":
            return '<div class="chgline"><span class="k">Retired</span> removed from the deck</div>'
        return f'<div class="chgline"><span class="k">{esc(ch["field"])}</span> <span class="diff">{diff_words(ch["before"], ch["after"])}</span></div>'

    def source_html(c):
        bits = []
        for rp in c["reports"]:
            mail = {"thanked": '<span class="badge ok">Thank-you sent</span>',
                    "pending": '<span class="badge warn">Thank-you waiting in Outbox</span>',
                    "skipped": '<span class="badge dim">Thank-you skipped</span>',
                    "none": '<span class="badge dim">No email left</span>'}[rp["thanks"]]
            bits.append(f'<div class="rpt"><b>Student report #{rp["id"]}</b>'
                        f'{" with an exact proposal" if rp["proposed"] else ""}<br>'
                        f'<span class="small">&ldquo;{esc((rp["text"] or "")[:200])}&rdquo;</span><br>{mail}</div>')
        for src in c["sources"]:
            if src.startswith("upload:"):
                bits.append(f'<div class="rpt"><b>Deck re-upload #{esc(src.split(":", 1)[1])}</b></div>')
            elif src == "staff":
                bits.append('<div class="rpt"><b>Direct edit</b> by staff</div>')
        return "".join(bits)

    body_rows = "".join(
        f'<tr><td><b>{esc(c["snippet"])}</b><br><span class="small">Note {c["nid"] or esc(c["guid"])}</span><br>'
        f'<a href="/cards/edit?guid={quote(c["guid"], safe="")}#edit-pane">Open card</a> &middot; '
        f'<a href="/cards/edit?guid={quote(c["guid"], safe="")}#history-pane">History</a></td>'
        f'<td>{kind_badge(c["kind"])}</td><td class="wide">{"".join(change_html(ch) for ch in c["changes"])}</td>'
        f'<td>{esc(", ".join(sorted(c["who"])))}</td><td class="wide">{source_html(c)}</td>'
        f'<td>{ui.human_time(c["when"])}</td></tr>' for c in rows) or \
        '<tr><td colspan="6" class="empty-row">This release has no recorded card changes. It is the original deck.</td></tr>'
    mail_bits = ", ".join(f"{n} {label}" for key, label in (("thanked", "thanked"), ("pending", "waiting in the Outbox"),
                          ("skipped", "skipped"), ("none", "without an email")) for n in [thanks.get(key, 0)] if n)
    summary = (f'<p class="sub">{esc(deck["name"] if deck else "Doctorine")} &middot; '
               f'{"published" if rel["status"] == "published" else esc(rel["status"])} {ui.human_time(rel.get("published_at") or rel["created_at"])}'
               f'{" by " + esc(builder["username"]) if builder else ""} &middot; '
               f'{"announced on the Updates page" if rel.get("visibility") != "silent" else "silent release"}</p>')
    notes = (f'<div class="infobox" style="display:block;padding:10px 14px"><b>Public notes</b><br>{esc(rel["public_notes"])}</div>'
             if rel.get("public_notes") else "")
    body = f"""
<p><a href="/releases">&larr; All releases</a></p>
<h1>Release {esc(version)}</h1>
{summary}{notes}
<p class="queue-count"><b>{len(rows)}</b> card{'s' if len(rows) != 1 else ''} changed &middot; <b>{n_edits}</b> edit{'s' if n_edits != 1 else ''}
&middot; <b>{from_students}</b> from student reports{(' &middot; ' + mail_bits) if mail_bits else ''}</p>
{ui.infobox("What this page is for", "<p>A record of what went into this release, for anyone double checking the work. Each row shows what changed, who did it, and where it came from. For a student report you can see their words, whether they proposed an exact change, and whether they were thanked. Open card goes to the card itself, and History shows every change it has ever had.</p>")}
<table class="tbl wrap"><thead><tr><th>Card</th><th>Type</th><th>What changed</th><th>Who</th><th>Where it came from</th><th>When</th></tr></thead>
<tbody>{body_rows}</tbody></table>"""
    return page(f"Release {version}", body, "releases", user, csrf)


def render_releases(user, csrf, notice=None, token_link=None):
    deck_id = active_deck_id(user)
    with db() as conn:
        deck = conn.execute("SELECT * FROM decks WHERE id=?", (deck_id,)).fetchone()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM releases WHERE COALESCE(platform_deck,1)=? ORDER BY rowid DESC", (deck_id,))]
        cur = releases.current_release(conn, deck_id)
        pending_n = len(revisions.pending(conn, deck_id))
        changes = {r["version"]: release_changes(conn, r["version"]) for r in rows}
        tokens = [dict(r) for r in conn.execute(
            "SELECT * FROM download_tokens WHERE COALESCE(platform_deck,1)=? ORDER BY created_at DESC LIMIT 30",
            (deck_id,))] if user["role"] == "admin" else []
    deck_name = deck["name"] if deck else "Doctorine"
    items = []
    for r in rows:
        v = r["version"]
        is_cur = v == cur
        when = ui.human_time(r.get("published_at") or r["created_at"])
        badge = {"draft": '<span class="badge warn">Draft</span>', "failed": '<span class="badge bad">Failed</span>',
                 "building": '<span class="badge warn">Building</span>',
                 "published": '<span class="badge current">Current</span>' if is_cur
                 else '<span class="badge dim">Published</span>'}.get(r["status"], "")
        public = '<span class="badge dim">Silent</span>' if r.get("visibility") == "silent" else ""
        acts = ""
        if r["status"] in ("draft", "failed"):
            acts = (f'<a class="btn-link-primary" href="/releases/{esc(v)}/confirm">Build and publish</a>'
                    f'<form method="post" action="/releases/{esc(v)}/discard" class="inline">'
                    f'<input type="hidden" name="csrf" value="{csrf}"><button class="btn-text">Discard draft</button></form>')
        elif r["status"] == "building":
            acts = '<span class="small">Building now. This page updates itself when it is done.</span>'
        elif r["status"] == "published":
            if r.get("collection_path") and os.path.exists(r["collection_path"]):
                acts = f'<a class="btn-link-primary" href="/download/release/{esc(v)}">Download</a>'
            else:
                acts = '<span class="small">An older release. It can no longer be downloaded.</span>'
        n_changes = len(changes.get(v, []))
        summary = (f'<a class="small" href="/releases/{quote(v, safe="")}">{n_changes} card{"s" if n_changes != 1 else ""} changed, see details</a>'
                   if n_changes else '<span class="small">The original deck</span>')
        notes = (f'<p class="small" style="margin:10px 0 0">{esc(r["public_notes"])}</p>'
                 if r.get("public_notes") else "")
        log = (f'<details><summary>Build log</summary><pre class="small">{esc(r["build_log"])}</pre></details>'
               if r.get("build_log") else "")
        cls = "rel current" if is_cur else ("rel" if r["status"] in ("draft", "building", "failed") else "rel muted")
        items.append(f"""
<div class="{cls}">
  <div class="rh"><a class="v" href="/releases/{quote(v, safe="")}" style="text-decoration:none">Release {esc(v)}</a>{badge}{public}{summary}<span class="when">{when}</span></div>
  {notes}{_changes_html(changes.get(v, []))}
  <div class="acts">{acts}</div>{log}
</div>""")
    msg = f'<p class="notice">{esc(notice)}</p>' if notice else ""
    if token_link:
        msg += (f'<div class="rel current"><b>New download link</b>'
                f'<div class="copyblock" style="margin-top:8px"><code>{esc(token_link)}</code>'
                f'<button type="button" data-copy="{esc(token_link)}">Copy</button></div>'
                f'<p class="small" style="margin:8px 0 0">Give this to the buyer. It always serves the newest '
                f'release of {esc(deck_name)}.</p></div>')
    cut = (f'<p><a class="btn-link-primary" href="/releases/next">{pending_n} card{"s" if pending_n != 1 else ""} '
           f'waiting in Next release</a></p>' if pending_n else
           '<p class="small">Nothing is waiting for the next release.</p>')
    tok = ""
    if user["role"] == "admin":
        def tlink(t):
            return f'{CFG.public_base_url}/dl/{t["token"]}'
        rows_t = "".join(
            f'<tr><td>{esc(t["note"] or "No name")}</td>'
            f'<td>{esc(deck_name)}</td>'
            f'<td>Newest, now {esc(cur or "none")}</td>'
            f'<td><div class="copyblock"><code>{esc(tlink(t))}</code>'
            f'<button type="button" class="btn-quiet" data-copy="{esc(tlink(t))}">Copy</button></div></td>'
            f'<td>{ui.human_time(t["expires_at"], False)}</td><td class="num">{t["downloads"]}</td>'
            f'<td><form method="post" action="/releases/token/revoke" class="inline">'
            f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="token" value="{esc(t["token"])}">'
            f'<button class="btn-text">Revoke</button></form></td></tr>'
            for t in tokens) or '<tr><td colspan="7" class="empty-row">No download links yet. Create one above.</td></tr>'
        tok = f"""
<h2 style="margin-top:34px">Download links for buyers</h2>
{ui.infobox("How buyer links work", f"<p>A link always serves the <b>newest published release</b> of {esc(deck_name)}. "
            "If you publish a new release later, the same link downloads the new one. Revoking a link stops new "
            "downloads but does not remove a deck already imported. Each download carries a hidden license id so "
            "sharing can be spotted on the Detection page.</p>")}
<form method="post" action="/releases/token" class="row" style="margin:0 0 16px">
  <input type="hidden" name="csrf" value="{csrf}">
  <div><label class="field-label" for="tn">Who is it for</label>
    <input id="tn" type="text" name="note" placeholder="Optional, e.g. a buyer's name" style="min-width:260px"></div>
  <div><label class="field-label" for="td">Valid for (days)</label>
    <input id="td" type="text" name="days" value="7" style="width:80px"></div>
  <div style="align-self:flex-end"><button type="submit">Create link</button></div>
</form>
<table class="tbl"><thead><tr><th>Buyer</th><th>Deck</th><th>Serves</th><th>Link</th><th>Expires</th>
<th class="num">Downloads</th><th></th></tr></thead><tbody>{rows_t}</tbody></table>"""
    body = (f'<h1>Releases</h1><p class="sub">{esc(deck_name)}. Current release: <b>{esc(cur or "none")}</b>.</p>'
            + ui.infobox("How releases work",
                         "<p>A release is cut from <a href=\"/releases/next\">Next release</a>, then built and published here "
                         "as a separate, confirmed step. The current release is what the add-on offers to students and what "
                         "buyer links download. Older releases stay listed for the record.</p>")
            + msg + cut + '<h2 style="margin-top:26px">All releases</h2>' + "".join(items) + tok)
    if any(r["status"] == "building" for r in rows):
        body += """<script>
(function poll(){
  fetch('/releases/status').then(function(r){return r.json();}).then(function(s){
    if (s.building) { setTimeout(poll, 3000); } else { location.reload(); }
  }).catch(function(){ setTimeout(poll, 6000); });
})();
</script>"""
    return page("Releases", body, "releases", user, csrf)


def mint_download_token(note, days, user_id, deck_id=1):
    token = auth.new_token()
    expires = (datetime.now(timezone.utc) + __import__("datetime").timedelta(days=max(1, min(int(days or 7), 90))))
    with db() as conn:
        conn.execute("INSERT INTO download_tokens (token, note, created_by, created_at, expires_at, platform_deck) "
                     "VALUES (?,?,?,?,?,?)",
                     (token, (note or "").strip(), user_id, now(), expires.strftime("%Y-%m-%d %H:%M UTC"), deck_id))
    return token


def latest_package(conn, deck_id=1):
    """(version, collection_path) of the current release, or (None, None).
    Packages are never stored; they are built from this file on download."""
    r = conn.execute("SELECT version, collection_path FROM releases WHERE status='published' "
                     "AND collection_path IS NOT NULL AND COALESCE(platform_deck,1)=? "
                     "ORDER BY published_at DESC, rowid DESC LIMIT 1", (deck_id,)).fetchone()
    return (r["version"], r["collection_path"]) if r and os.path.exists(r["collection_path"]) else (None, None)


def package_for_version(conn, version):
    r = conn.execute("SELECT version, collection_path FROM releases WHERE version=? AND status='published'",
                     (version,)).fetchone()
    return (r["version"], r["collection_path"]) if r and r["collection_path"] and os.path.exists(r["collection_path"]) else (None, None)


def media_for_package(conn, deck_id=1):
    """Media files a deck's package carries: only the ones that deck owns."""
    return [(r["filename"], releases.sha_path(r["sha256"]))
            for r in conn.execute("SELECT filename, sha256 FROM media WHERE COALESCE(platform_deck,1)=? "
                                  "ORDER BY filename", (deck_id,))
            if os.path.exists(releases.sha_path(r["sha256"]))]


# ---------------------------------------------------------------- sync

_MEDIA_REF = re.compile(r'(?:src=["\']|\[sound:)([^"\'\]]+)')


def _release_order(conn, deck_id=1):
    return [r["version"] for r in conn.execute(
        "SELECT version FROM releases WHERE status='published' AND COALESCE(platform_deck,1)=? "
        "ORDER BY published_at, created_at", (deck_id,))]


def sync_payload(conn, since, deck_id=1):
    """Changes a collection at release `since` needs to reach the current release.

    Collapses multiple revisions of one field to its final value and reports
    `before` (the field at `since`) so the add-on can tell a local edit from
    an untouched card. Unknown `since` → everything after the first release.
    """
    order = _release_order(conn, deck_id)
    current = order[-1] if order else None
    baseline_missing = since not in order
    later = order[order.index(since) + 1:] if not baseline_missing else order[1:]
    if not current or not later:
        return {"from": since, "to": current, "behind": 0, "changed": [], "added": [], "retired": [],
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
    shipped = set(order)
    changed, added_out, media = [], [], set()
    for guid in sorted(touched - set(added) - set(retired)):
        row = conn.execute("SELECT fields_json FROM notes WHERE doctrine_id=? AND retired=0", (guid,)).fetchone()
        if row is None:
            continue
        fields = json.loads(row["fields_json"] or "{}")
        at = revisions.text_at(conn, guid, shipped, fields)
        now_fields = {f: at.get(f, "") for f in revisions.EDITABLE_FIELDS}
        b = {f: before[guid].get(f, now_fields[f]) for f in revisions.EDITABLE_FIELDS}
        changed.append({"guid": guid, "fields": now_fields, "before": b})
        for f in now_fields.values():
            media.update(_MEDIA_REF.findall(f or ""))
    for guid in added:
        row = conn.execute("SELECT note_type, fields_json, tags_json FROM notes WHERE doctrine_id=? AND retired=0", (guid,)).fetchone()
        if row is None:
            continue
        fields = json.loads(row["fields_json"] or "{}")
        fields.update(revisions.text_at(conn, guid, shipped, fields))
        added_out.append({"guid": guid, "note_type": row["note_type"], "fields": fields,
                          "tags": json.loads(row["tags_json"] or "[]")})
        for f in fields.values():
            media.update(_MEDIA_REF.findall(f or ""))
    known = set()
    if media:
        m = ",".join("?" * len(media))
        known = {r["filename"] for r in conn.execute(f"SELECT filename FROM media WHERE filename IN ({m})", sorted(media))}
    return {"from": since, "to": current, "behind": len(later), "changed": changed, "added": added_out,
            "retired": sorted(set(retired)), "media": sorted(known),
            "baseline_missing": baseline_missing}


# ---------------------------------------------------------------- cards (direct edit)

CARDS_LIMIT = 50


def search_cards_page(conn, q, deck_id, limit):
    """Up to `limit` matches for the Cards page. An exact internal id or note id comes first."""
    if not (q or "").strip():
        return []
    where, args = _card_filter(q, deck_id)
    return conn.execute(
        f"SELECT * FROM notes WHERE {where} ORDER BY (doctrine_id=? OR CAST(anki_note_id AS TEXT)=?) DESC, doctrine_id LIMIT ?",
        (*args, (q or "").strip(), re.sub(r"\D", "", q or ""), limit)).fetchall()


def search_cards(conn, q, deck_id=1):
    q = (q or "").strip()
    if not q:
        return []
    D = "COALESCE(platform_deck,1)=?"
    exact = conn.execute(f"SELECT * FROM notes WHERE doctrine_id=? AND retired=0 AND {D}", (q, deck_id)).fetchone()
    if exact:
        return [exact]
    if q.isdigit():
        rows = conn.execute(f"SELECT * FROM notes WHERE anki_note_id=? AND retired=0 AND {D}",
                            (int(q), deck_id)).fetchall()
        if rows:
            return rows
    if q.lower().startswith("tag:"):
        like = f"%{q[4:].strip()}%"
        return conn.execute(
            f"SELECT * FROM notes WHERE retired=0 AND {D} AND tags_json LIKE ? ORDER BY doctrine_id LIMIT ?",
            (deck_id, like, CARDS_LIMIT + 1)).fetchall()
    like = f"%{q}%"
    return conn.execute(
        f"SELECT * FROM notes WHERE retired=0 AND {D} AND (fields_json LIKE ? OR tags_json LIKE ?) "
        "ORDER BY doctrine_id LIMIT ?", (deck_id, like, like, CARDS_LIMIT + 1)).fetchall()


CARDS_PAGE = 48


def _card_filter(q, deck_id):
    """(where_sql, args) for a Cards search. Accepts text, tag:..., a note id, an internal id, or part of one.

    Pasted forms like 'Note 1788834703288', 'nid:1788834703288' and '#1788834703288' work too.
    """
    q = (q or "").strip()
    D = "retired=0 AND COALESCE(platform_deck,1)=?"
    if q.lower().startswith("tag:"):
        return f"{D} AND tags_json LIKE ?", (deck_id, f"%{q[4:].strip()}%")
    bare = re.sub(r"^(note\s*id|note|nid|id|#)[\s:#]*", "", q, flags=re.I).strip()
    if bare.isdigit() and len(bare) >= 5:
        return f"{D} AND (anki_note_id=? OR CAST(anki_note_id AS TEXT) LIKE ? OR doctrine_id LIKE ?)", \
               (deck_id, int(bare), f"{bare}%", f"%{bare}%")
    like = f"%{q}%"
    return (f"{D} AND (doctrine_id=? OR doctrine_id LIKE ? OR fields_json LIKE ? OR tags_json LIKE ?)",
            (deck_id, bare or q, f"%{bare or q}%" if len(bare or q) >= 4 else "\x00", like, like))


def count_cards(conn, q, deck_id):
    """How many official cards a search matches in total."""
    if not (q or "").strip():
        return 0
    where, args = _card_filter(q, deck_id)
    return conn.execute(f"SELECT COUNT(*) FROM notes WHERE {where}", args).fetchone()[0]


def _tag_label(t):
    parts = [x for x in t.split("::") if x]
    if parts and parts[0].lower() in ("doctorine", "doctrine"):
        parts = parts[1:]
    return " · ".join(parts[-2:]) if parts else t


def tile_tags(tags_json, uid, show=3):
    try:
        tags = json.loads(tags_json or "[]")
    except ValueError:
        tags = []
    if not tags:
        return ""
    chip = lambda t: f'<span class="chip" title="{esc(t)}">{esc(_tag_label(t))}</span>'
    out = "".join(chip(t) for t in tags[:show])
    if len(tags) > show:
        out += (f'<button type="button" class="chip btn-quiet" data-pop="tp{uid}">+{len(tags) - show} more</button>'
                f'<div class="pop" id="tp{uid}" hidden>{"".join(chip(t) for t in tags)}</div>')
    return f'<div class="tg">{out}</div>'


def render_cards(user, csrf, q, n=CARDS_PAGE):
    deck_id = active_deck_id(user)
    try:
        n = max(CARDS_PAGE, min(int(n or CARDS_PAGE), 960))
    except ValueError:
        n = CARDS_PAGE
    with db() as conn:
        total = count_cards(conn, q, deck_id)
        rows = search_cards_page(conn, q, deck_id, n)
    tiles, fulls = [], []
    for i, r in enumerate(rows):
        fields = json.loads(r["fields_json"] or "{}")
        f = cardview.faces(fields)
        guid = quote(r["doctrine_id"], safe="")

        def sec(kind, label, content):
            if not content.strip():
                return ""
            long_ = len(ui.plain_text(content)) > 240
            more = (f'<button type="button" class="more" data-full="full{i}">Show all</button>' if long_ else "")
            return (f'<div class="sec {kind}"><div class="lab">{label}</div>'
                    f'<div class="txt{" clamp" if long_ else ""}">{content}</div>{more}</div>')
        tiles.append(f"""
<div class="tile">
  <div class="th"><code>{esc(r['doctrine_id'])}</code><span>nid {r['anki_note_id'] or 'none'}</span><span>v{r['version']}</span>
    <span class="sp"><a class="btn-link-primary" href="/cards/edit?guid={guid}">Edit</a></span></div>
  {sec("front", "Front", f["front"])}{sec("back", "Back", f["back"])}{sec("extra", "Extra", f["extra"])}
  {tile_tags(r['tags_json'], i)}
  <template id="full{i}"><h3>{esc(ui.plain_text(fields.get("Text", ""), 80))}</h3>
    <div class="sec front"><div class="lab">Front</div><div class="txt">{f["front"]}</div></div>
    <div class="sec back"><div class="lab">Back</div><div class="txt">{f["back"]}</div></div>
    <div class="sec extra"><div class="lab">Extra</div><div class="txt">{f["extra"]}</div></div></template>
</div>""")
    shown = len(rows)
    if not (q or "").strip():
        status = ""
    elif total == 0:
        status = '<p class="small">No official card matches.</p>'
    else:
        status = f'<p class="queue-count">Showing <b>{shown}</b> of <b>{total}</b> matches</p>'
    more_link = ""
    if shown < total:
        more_link = (f'<p style="margin-top:18px"><a class="btn-link-primary" href="/cards?q={quote(q or "")}'
                     f'&n={n + CARDS_PAGE}">Show {min(CARDS_PAGE, total - shown)} more</a></p>')
    body = f"""
<h1>Cards</h1>
{ui.infobox("What you can search", "<p>Search any text on a card, a tag with <code>tag:Step1::Gold</code>, or an ID. An ID can be the Anki note ID or the internal one, whole or partial. Edits go to Next release like accepted suggestions do. Cards are shown the way Anki shows them, with the answer hidden on the front.</p>")}
<form method="get" action="/cards" class="row" style="margin:0 0 14px">
  <input type="text" name="q" value="{esc(q or '')}" placeholder="Search text, a tag, or an ID" style="min-width:380px" autofocus>
  <button type="submit">Search</button>
</form>
{status}
<div class="tiles">{"".join(tiles)}</div>{more_link}
<div class="dlg-back" id="fulldlg"><div class="dlg wide"><div id="fullbody"></div>
  <div class="foot"><button type="button" class="btn-quiet" data-close>Close</button></div></div></div>
<script>
document.querySelectorAll('.more[data-full]').forEach(function (b) {{
  b.addEventListener('click', function () {{
    document.getElementById('fullbody').innerHTML = document.getElementById(b.dataset.full).innerHTML;
    document.getElementById('fulldlg').classList.add('on');
  }});
}});
</script>"""
    return page("Cards", body, "cards", user, csrf)


def card_history_html(conn, guid):
    """Timeline of every change and every suggestion on one card, newest first."""
    events = []
    for r in conn.execute(
            "SELECT r.*, u.username FROM revisions r LEFT JOIN users u ON u.id=r.user_id "
            "WHERE r.guid=? ORDER BY r.id DESC", (guid,)):
        src = r["source"] or ""
        if src.startswith("suggestion:"):
            who = f'accepted suggestion #{esc(src.split(":")[1])}'
        elif src.startswith("upload:"):
            who = f"deck re-upload #{esc(src.split(':')[1])}"
        else:
            who = "direct edit"
        by = f" by {esc(r['username'])}" if r["username"] else ""
        state = (f'shipped in {esc(r["release"])}' if r["release"]
                 else '<span class="badge warn">pending, next release</span>')
        if r["field"] in revisions.EDITABLE_FIELDS:
            diff = diff_words(r["before"] or "", r["after"] or "")
            detail = f'<dl class="fields" style="height:auto"><dt>{esc(r["field"])}</dt><dd class="diff">{diff}</dd></dl>'
        else:
            detail = f'<p class="small">{esc(r["field"])}</p>'
        events.append((r["created_at"] or "", r["id"], f"""
<div class="entry"><div class="stamp"><b>Edit</b>{esc((r["created_at"] or "")[:16])}</div>
<div class="what">{who}{by} &middot; {state}{detail}</div></div>"""))
    for r in conn.execute("SELECT * FROM suggestions WHERE doctrine_id=? ORDER BY id DESC", (guid,)):
        events.append((r["created_at"] or "", r["id"] + 10**9, f"""
<div class="entry"><div class="stamp"><b>Report #{r["id"]}</b>{esc((r["created_at"] or "")[:16])}</div>
<div class="what"><span class="badge dim">{esc(r["status"])}</span>
{esc(TYPE_LABELS.get(r["suggestion_type"], r["suggestion_type"]))}: {esc((r["text"] or "")[:300])}</div></div>"""))
    if not events:
        return '<p class="small">No changes or reports on this card yet.</p>'
    events.sort(key=lambda e: (e[0], e[1]), reverse=True)
    return "".join(e[2] for e in events)




# ---------------------------------------------------------------- outbox

def pending_thank_yous(deck_id=None):
    with db() as conn:
        if deck_id is None:
            return conn.execute("SELECT COUNT(*) c FROM notifications WHERE status='pending'").fetchone()["c"]
        return conn.execute(
            "SELECT COUNT(*) c FROM notifications n JOIN suggestions s ON s.id=n.suggestion_id "
            "WHERE n.status='pending' AND COALESCE(s.platform_deck,1)=?", (deck_id,)).fetchone()["c"]


def render_outbox(user, csrf):
    with db() as conn:
        rows = conn.execute(
            """SELECT n.*, s.text AS suggestion_text, s.deck, s.token,
                      s.doctrine_id, s.suggestion_type
               FROM notifications n JOIN suggestions s ON s.id = n.suggestion_id
               WHERE n.status='pending' AND COALESCE(s.platform_deck,1)=? ORDER BY n.id""",
            (active_deck_id(user),)).fetchall()

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
            """SELECT s.token, s.expires_at, u.id, u.username, u.role, u.active, u.active_deck
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
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "active_deck": row["active_deck"] or 1}


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
