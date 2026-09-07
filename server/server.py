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
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config

CFG = config.load()
MIN_ADDON_VERSION = "1.0"
ID_FIELD = "DoctrineID"   # excluded from content hashing, same as add-on

TYPE_LABELS = {
    "typo": "Typo",
    "incorrect": "Incorrect",
    "confusing": "Confusing",
    "other": "Other",
}


# ---------------------------------------------------------------- db

def db():
    conn = sqlite3.connect(CFG.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            doctrine_id TEXT PRIMARY KEY,
            anki_note_id INTEGER,
            note_type TEXT,
            deck TEXT,
            fields_json TEXT,
            question_html TEXT,
            answer_html TEXT,
            css TEXT,
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
        """)


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


def page(title, body, active=""):
    def nav_link(href, label, key):
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — Doctrine</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Sans+3:ital,wght@0,400..600;1,400&family=Source+Serif+4:ital,opsz,wght@0,8..60,400..700;1,8..60,400..700&display=swap">
<style>{BASE_CSS}{LEDGER_CSS}</style></head><body>
<header class="site"><div class="wrap">
  <span class="brand">Doctrine<small>Card quality</small></span>
  <nav>
    {nav_link("/updates", "Community updates", "updates")}
    {nav_link("/reviewer", "Reviewer queue", "reviewer")}
  </nav>
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

def render_reviewer():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM suggestions ORDER BY "
            "CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC"
        ).fetchall()
        masters = {r["doctrine_id"]: r for r in conn.execute("SELECT * FROM notes")}

    if not rows:
        body = ('<h1>Reviewer queue</h1><p class="sub">Suggestions from students '
                'land here, next to the card they saw and its current master '
                'version.</p><div class="empty"><b>The queue is clear</b>'
                'When a student clicks &#9998; Suggest inside Anki, their note '
                'arrives here with the exact card they were looking at.</div>')
        return page("Reviewer queue", body, "reviewer")

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
            master_pane = card_iframe(
                master["question_html"], master["answer_html"], master["css"],
                f'Current master (v{master["version"]})')
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
  <div class="panes">{snapshot_pane}{master_pane}{sugg_pane}</div>
  {actions}
</div>""")

    n_open = sum(1 for r in rows if r["status"] == "open")
    body = ('<h1>Reviewer queue</h1><p class="sub">Each item pairs the exact '
            'card the student saw with its current master version. If the two '
            'panes differ, the card has already moved on.</p>'
            f'<p class="queue-count"><b>{n_open} open</b> &middot; '
            f'{len(rows) - n_open} closed</p>'
            + "".join(items))
    return page("Reviewer queue", body, "reviewer")


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
        conn.execute(
            """INSERT INTO suggestions
               (token, doctrine_id, anki_note_id, note_type, deck,
                suggestion_type, text, email,
                snap_question, snap_answer, snap_css, snap_fields_json,
                snap_hash, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (token, doc_id, data.get("anki_note_id"),
             data.get("note_type"), data.get("deck"),
             data.get("suggestion_type", "other"), text, data.get("email"),
             snap.get("question_html"), snap.get("answer_html"),
             snap.get("css"), json.dumps(snap.get("fields") or {}),
             snap.get("content_hash"), now()),
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
            if existing is None:
                conn.execute(
                    """INSERT INTO notes (doctrine_id, anki_note_id, note_type,
                       deck, fields_json, question_html, answer_html, css,
                       content_hash, version, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,1,?)""",
                    (doc_id, n.get("anki_note_id"), n.get("note_type"),
                     n.get("deck"), json.dumps(fields), n.get("question_html"),
                     n.get("answer_html"), n.get("css"), h, now()))
                registered += 1
            else:
                bump = existing["content_hash"] != h
                conn.execute(
                    """UPDATE notes SET anki_note_id=?, note_type=?, deck=?,
                       fields_json=?, question_html=?, answer_html=?, css=?,
                       content_hash=?, version=version+?, updated_at=?
                       WHERE doctrine_id=?""",
                    (n.get("anki_note_id"), n.get("note_type"), n.get("deck"),
                     json.dumps(fields), n.get("question_html"),
                     n.get("answer_html"), n.get("css"), h,
                     1 if bump else 0, now(), doc_id))
                registered += 1
                if bump:
                    updated += 1
    return 200, {"ok": True, "registered": registered, "updated": updated}


def reviewer_action(form):
    sid = form.get("id", [""])[0]
    action = form.get("do", [""])[0]
    status = {"resolve": "resolved", "decline": "declined",
              "expire": "expired"}.get(action)
    if not sid or not status:
        return
    publish = 1 if (status == "resolved" and form.get("publish")) else 0
    with db() as conn:
        conn.execute(
            """UPDATE suggestions SET status=?, resolution_note=?, published=?,
               publish_summary=?, credit_name=?, closed_at=?
               WHERE id=? AND status='open'""",
            (status, form.get("resolution_note", [""])[0].strip() or None,
             publish, form.get("publish_summary", [""])[0].strip() or None,
             form.get("credit_name", [""])[0].strip() or None, now(), sid))


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

    def do_GET(self):
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
        elif path in ("/", "/reviewer"):
            self._send(200, render_reviewer())
        elif path == "/updates":
            self._send(200, render_updates())
        elif path.startswith("/s/"):
            self._send(200, render_status(path[3:]))
        else:
            self._send(404, page("Not found", "<h1>Not found</h1>"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path

        if path == "/api/suggestions":
            try:
                code, obj = api_suggestion(json.loads(raw or b"{}"))
            except json.JSONDecodeError:
                code, obj = 400, {"error": "Invalid JSON."}
            self._json(code, obj)
        elif path == "/api/dev/register":
            try:
                code, obj = api_register(json.loads(raw or b"{}"))
            except json.JSONDecodeError:
                code, obj = 400, {"error": "Invalid JSON."}
            self._json(code, obj)
        elif path == "/reviewer/action":
            reviewer_action(parse_qs(raw.decode("utf-8")))
            self.send_response(303)
            self.send_header("Location", "/reviewer")
            self.end_headers()
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


if __name__ == "__main__":
    init_db()
    print(f"Doctrine Editor platform — {CFG.public_base_url}", flush=True)
    print(f"  Reviewer queue:  {CFG.public_base_url}/reviewer", flush=True)
    print(f"  Public updates:  {CFG.public_base_url}/updates", flush=True)
    print(f"  Listening on {CFG.host}:{CFG.port}, db={CFG.db_path}", flush=True)
    Server((CFG.host, CFG.port), Handler).serve_forever()
