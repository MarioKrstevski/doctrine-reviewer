"""Shared UI pieces for the staff and public pages: components CSS, a little
JavaScript, and small HTML helpers. Plain strings, no framework.

The look follows shadcn/ui's sidebar, dropdown, dialog, tabs and table
patterns, drawn with the Study Navigator brand tokens from BASE_CSS.
"""

import html
import re
from datetime import datetime, timezone

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def parse_utc(value):
    """datetime for the formats stored in the database, else None."""
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def human_time(value, with_time=True):
    """'13 Oct 2026, 18:33'. A script upgrades it to the viewer's local time."""
    d = parse_utc(value)
    if d is None:
        return _esc(value or "")
    label = f"{d.day} {MONTHS[d.month - 1]} {d.year}" + (f", {d:%H:%M}" if with_time else "")
    return (f'<time data-utc="{d.strftime("%Y-%m-%dT%H:%M:%SZ")}" '
            f'data-time="{1 if with_time else 0}" title="{d:%Y-%m-%d %H:%M} UTC">{label}</time>')


def plain_text(s, limit=0):
    """Field HTML as readable text, optionally cut with an ellipsis."""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", " ", s or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"\{\{c\d+::(.*?)(::[^}]*)?\}\}", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def infobox(title, body_html, open_=False):
    """A small collapsible tip box with an info icon."""
    return (f'<details class="infobox"{" open" if open_ else ""}><summary>'
            f'<span class="i">i</span><span class="t">{_esc(title)}</span></summary>'
            f'<div class="b">{body_html}</div></details>')


ICONS = {
    "chev": '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="m7 15 5 5 5-5M7 9l5-5 5 5"/></svg>',
    "check": '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" '
             'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    "plus": '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    "inbox": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
             'stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-6l-2 3h-4l-2-3H2"/>'
             '<path d="M5.5 5h13L22 12v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z"/></svg>',
    "cards": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
             'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="16" rx="2"/>'
             '<path d="M21 8v10a2 2 0 0 1-2 2"/></svg>',
    "next": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0-4-4m4 4 4-4M4 21h16"/></svg>',
    "release": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
               'stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg>',
    "mail": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/>'
            '<path d="m3 7 9 6 9-6"/></svg>',
    "deck": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 9 5-9 5-9-5z"/>'
            '<path d="m3 13 9 5 9-5"/></svg>',
    "shield": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
              'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/></svg>',
    "users": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
             'stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.2"/>'
             '<path d="M3 20c.5-3.3 3-5 6-5s5.5 1.7 6 5M16 5.2a3.2 3.2 0 0 1 0 5.6M21 20c-.3-2.4-1.7-4-3.8-4.7"/></svg>',
    "gear": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/>'
            '<path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h0a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5h0a1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v0a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    "globe": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
             'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>'
             '<path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
    "help": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>'
            '<path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 1-1 1.7M12 17h.01"/></svg>',
}


def sidebar(user, decks, active_deck, active, counts, csrf, esc=_esc):
    """The staff sidebar: deck switcher, grouped navigation, user menu."""
    inbox, nxt, thanks = counts
    admin = user.get("role") == "admin"

    def item(href, label, key, icon, n=0):
        cls = ' class="active"' if key == active else ""
        c = f'<span class="count">{n}</span>' if n else ""
        return f'<a href="{href}"{cls}>{ICONS[icon]}<span>{label}</span>{c}</a>'

    cur = next((d for d in decks if d["id"] == active_deck), decks[0] if decks else None)
    deck_name = cur["name"] if cur else "No deck"
    initial = (deck_name[:1] or "D").upper()
    options = "".join(
        f'<form method="post" action="/deck/select"><input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="deck" value="{esc(d["slug"])}">'
        f'<button type="submit" class="menu-item">'
        f'<span class="av sm">{esc(d["name"][:1].upper())}</span><span class="grow">{esc(d["name"])}</span>'
        f'{ICONS["check"] if d["id"] == active_deck else ""}</button></form>'
        for d in decks)
    switcher = f"""
<div class="menu-wrap">
  <button type="button" class="switcher" data-menu="deckmenu">
    <span class="av">{esc(initial)}</span>
    <span class="two"><b>{esc(deck_name)}</b><small>Deck workspace</small></span>{ICONS["chev"]}
  </button>
  <div class="menu" id="deckmenu" hidden>
    <div class="menu-label">Decks</div>{options}
    <a class="menu-item" href="/deck">{ICONS["plus"]}<span class="grow">Manage decks</span></a>
  </div>
</div>"""
    uname = user["username"]
    role = "Administrator" if admin else "Reviewer"
    usermenu = f"""
<div class="menu-wrap bottom">
  <div class="menu up" id="usermenu" hidden>
    <a class="menu-item" href="/account"><span class="grow">Account</span></a>
    <form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}">
      <button type="submit" class="menu-item"><span class="grow">Sign out</span></button></form>
  </div>
  <button type="button" class="switcher user" data-menu="usermenu">
    <span class="av round">{esc(uname[:2].upper())}</span>
    <span class="two"><b>{esc(uname)}</b><small>{role}</small></span>{ICONS["chev"]}
  </button>
</div>"""
    return f"""
<aside class="sidebar">
  {switcher}
  <nav>
  <span class="group">Review</span>
  {item("/reviewer", "Inbox", "reviewer", "inbox", inbox)}
  {item("/cards", "Cards", "cards", "cards")}
  <span class="group">Ship</span>
  {item("/releases/next", "Next release", "pending", "next", nxt)}
  {item("/releases", "Releases", "releases", "release")}
  {item("/outbox", "Outbox", "outbox", "mail", thanks)}
  <span class="group">Deck</span>
  {item("/deck", "Deck", "deck", "deck")}
  {item("/detection", "Detection", "detection", "shield") if admin else ""}
  {item("/admin/users", "Users", "admin", "users") if admin else ""}
  <span class="group">Help</span>
  {item("/addon", "Add-on and testing", "addon", "help")}
  <a href="/updates">{ICONS["globe"]}<span>Public site</span></a>
  </nav>
  {usermenu}
</aside>"""


UI_CSS = """
/* ---- sidebar (shadcn-style) ---- */
.staff { grid-template-columns:248px minmax(0,1fr); }
.sidebar { padding:12px; gap:2px; overflow-y:auto; }
.sidebar nav { display:flex; flex-direction:column; gap:2px; flex:1; padding-top:6px; margin-left:0; }
.sidebar nav a { margin-left:0; padding-bottom:8px; }
.sidebar .group { padding:16px 10px 6px; font-size:11px; letter-spacing:.1em; color:#7FA0B6; }
.sidebar a { gap:10px; border-radius:8px; padding:8px 10px; color:#C9DCEA; font-size:14px; font-weight:500; }
.sidebar a svg { flex:none; opacity:.85; }
.sidebar a:hover { background:rgba(255,255,255,.06); color:#fff; box-shadow:none; }
.sidebar a.active { background:#24506F; color:#fff; font-weight:600; box-shadow:none; }
.sidebar a.active svg { opacity:1; color:var(--blue-light); }
.sidebar a .count { margin-left:auto; background:var(--blue); color:var(--navy); font-weight:700; }
.menu-wrap { position:relative; }
.switcher { width:100%; display:flex; align-items:center; gap:10px; text-align:left; background:transparent;
  border:1px solid transparent; border-radius:10px; padding:8px; color:#fff; cursor:pointer; }
.switcher:hover { background:rgba(255,255,255,.07); border-color:transparent; }
.switcher .two { display:flex; flex-direction:column; min-width:0; flex:1; line-height:1.25; }
.switcher .two b { font-size:14px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.switcher .two small { font-size:12px; color:#8FB0C6; font-weight:500; }
.switcher svg { color:#8FB0C6; flex:none; }
.av { width:32px; height:32px; border-radius:8px; background:var(--blue); color:var(--navy); display:inline-flex;
  align-items:center; justify-content:center; font-weight:700; font-family:var(--serif); flex:none; }
.av.sm { width:22px; height:22px; font-size:12px; border-radius:6px; }
.av.round { border-radius:50%; background:#2F6286; color:#fff; font-family:var(--sans); font-size:12px; }
.menu { position:absolute; left:0; right:0; top:calc(100% + 4px); background:#fff; color:var(--ink);
  border:1px solid var(--line); border-radius:10px; box-shadow:0 12px 30px -10px rgba(15,42,61,.35);
  padding:4px; z-index:30; min-width:200px; }
.menu[hidden] { display:none; }
.menu.up { top:auto; bottom:calc(100% + 4px); }
.menu-label { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--faint); padding:8px 10px 4px; }
.menu .menu-item, .menu a.menu-item { display:flex; align-items:center; gap:8px; width:100%; text-align:left;
  padding:8px 10px; border-radius:6px; font-size:14px; font-weight:500; color:var(--ink); background:transparent;
  border:0; cursor:pointer; text-decoration:none; box-shadow:none; }
.menu .menu-item:hover, .menu a.menu-item:hover { background:var(--accent-soft); color:var(--ink); }
.menu .grow { flex:1; }
.menu form { margin:0; }
.menu-wrap.bottom { margin-top:8px; border-top:1px solid rgba(255,255,255,.1); padding-top:8px; }

header.site nav a.back { border:1px solid #3a6482; border-radius:8px; padding:5px 10px; color:#fff; }

/* ---- sign in, account, users, install: one brand look ---- */
.login-wrap { max-width:400px; margin:9vh auto 0; background:#fff; border:1px solid var(--line); border-radius:18px;
  padding:30px 30px 26px; box-shadow:var(--shadow); }
.login-wrap h1 { margin:0 0 4px; font-size:26px; }
.login-form { display:flex; flex-direction:column; gap:6px; max-width:420px; }
.login-wrap .login-form { max-width:none; }
.login-form label { font-size:12px; font-weight:600; color:var(--muted); letter-spacing:.02em; margin-top:10px; }
.login-form input { padding:10px 12px; font-size:15px; border:1px solid var(--line-strong); border-radius:10px;
  background:#fff; font-family:var(--sans); color:var(--ink); }
.login-form input:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
.login-form button { margin-top:18px; padding:11px; font-size:15px; font-weight:600; border-radius:10px; }
.login-error { padding:10px 14px; border-radius:10px; background:var(--bad-soft); color:var(--bad);
  border:1px solid var(--bad-border); font-size:14px; }
.notice { padding:10px 14px; border-radius:10px; background:var(--accent-soft); border:1px solid var(--accent-border);
  color:var(--accent-ink); font-size:14px; }
.login-note, .admin-note { margin-top:18px; font-size:12.5px; color:var(--muted); line-height:1.55; max-width:62ch; }
.pw-reveal { margin:16px 0; padding:14px 16px; border-radius:12px; background:var(--accent-soft);
  border:1px solid var(--accent-border); }
.pw-reveal code { display:inline-block; margin:6px 0; padding:6px 10px; font-size:17px; background:#fff;
  border:1px solid var(--line-strong); border-radius:8px; }
.pw-note { font-size:12.5px; color:var(--muted); margin:4px 0 0; }
.create-form { display:flex; gap:8px; align-items:center; margin:18px 0; flex-wrap:wrap; }
.create-form input[type=text], .create-form input[type=password] { padding:9px 12px; min-width:240px; }
table.users { width:100%; border-collapse:collapse; font-size:14px; background:var(--card); border:1px solid var(--line);
  border-radius:12px; overflow:hidden; }
table.users th { text-align:left; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted);
  font-weight:600; padding:10px 14px; background:var(--panel); border-bottom:1px solid var(--line); }
table.users td { text-align:left; padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }
table.users tr:last-child td { border-bottom:0; }
tr.inactive { opacity:.5; }
.install-box { margin:18px 0 26px; padding:18px 20px; border:1px solid var(--line); border-radius:14px; background:var(--card);
  box-shadow:var(--shadow); }
.btn-primary { display:inline-block; padding:11px 18px; border-radius:10px; background:var(--navy); color:#fff !important;
  text-decoration:none; font-weight:600; }
.btn-primary:hover { background:var(--navy-soft); }
.steps li { margin:6px 0; }

/* ---- info box ---- */
.infobox { border:1px solid var(--accent-border); background:var(--accent-soft); border-radius:12px;
  margin:14px 0 18px; font-size:13px; color:var(--muted); max-width:78ch; }
.infobox summary { display:flex; align-items:center; gap:10px; padding:9px 12px; cursor:pointer; list-style:none;
  color:var(--accent-ink); font-weight:600; user-select:none; }
.infobox summary::-webkit-details-marker { display:none; }
.infobox .i { width:18px; height:18px; border-radius:50%; background:var(--accent); color:#fff; font-size:11px;
  font-style:italic; font-family:var(--serif); display:inline-flex; align-items:center; justify-content:center; }
.infobox .b { padding:0 14px 12px 40px; line-height:1.55; }
.infobox .b p { margin:0 0 6px; }
.infobox .b ul { margin:4px 0 0; padding-left:18px; }

/* ---- inbox panes ---- */
.ask { border-top:1px solid var(--line); }
.ask .pane-label { display:flex; align-items:center; }
.ask-body { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.5fr); gap:20px; padding:14px 16px; font-size:14.5px; }
.ask-body .lab, .viewbar .lab { font-size:10.5px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--faint);
  margin-bottom:4px; }
.viewbar { display:flex; align-items:center; gap:12px; padding:10px 16px; border-top:1px solid var(--line); background:var(--panel); }
.viewbar .lab { margin:0; }
.panes.two { display:grid; grid-template-columns:1fr 1fr; align-items:stretch; border-top:1px solid var(--line); }
.panes.two .pane { display:flex; flex-direction:column; border-right:1px solid var(--line); min-width:0; }
.panes.two .pane:last-child { border-right:0; }
.panes.two .pane-label { height:40px; box-sizing:border-box; display:flex; align-items:center; }
.viewpane { height:340px; box-sizing:border-box; }
.viewpane[hidden] { display:none; }
.viewpane.scroll { overflow:auto; padding:14px 16px; font-size:14px; }
.anki-frame { width:100%; height:100%; border:0; display:block; background:#fff; }
.panes.eq { align-items:stretch; }
.panes.eq .pane { display:flex; flex-direction:column; }
.panes.eq .pane-label { min-height:42px; }
.pane-body { padding:14px 16px; max-height:340px; overflow:auto; flex:1; font-size:14px; }
.pane-body .rendered .txt { margin-left:10px; }
.pane-label { color:var(--muted); }
.pane-label { flex-wrap:wrap; row-gap:6px; }
.pane-label .seg { margin-left:auto; flex-wrap:wrap; }
.pane-label .seg button { white-space:nowrap; }
.card .chips { padding:10px 16px 6px; }
.card .trace { padding:4px 0 8px; }
.card .trace summary { font-size:12.5px; }

.deck-pick { background:transparent !important; border:0 !important; color:var(--ink) !important; font-weight:600;
  padding:2px 0 !important; font-size:15px; cursor:pointer; text-align:left; }
.deck-pick:hover { color:var(--accent) !important; background:transparent !important; }
table.tbl tr.sel td { background:var(--accent-soft); }
table.tbl tr.sel td:first-child { box-shadow:inset 3px 0 0 var(--accent); }

/* ---- tables ---- */
table.tbl { width:100%; border-collapse:collapse; font-size:14px; background:var(--card); border:1px solid var(--line);
  border-radius:12px; overflow:hidden; }
table.tbl th { text-align:left; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted);
  font-weight:600; padding:10px 14px; background:var(--panel); border-bottom:1px solid var(--line); white-space:nowrap; }
table.tbl td { padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }
table.tbl tr:last-child td { border-bottom:0; }
table.tbl td.num, table.tbl th.num { text-align:right; font-variant-numeric:tabular-nums; }
table.tbl td.empty-row { text-align:center; color:var(--muted); padding:26px 14px; }
.copyblock { display:flex; gap:8px; align-items:center; }
.copyblock code { flex:1; min-width:0; padding:7px 10px; border-radius:8px; background:var(--code-bg); font-size:12.5px;
  overflow-wrap:anywhere; word-break:break-all; line-height:1.4; }
.btn-quiet { background:transparent !important; color:var(--muted) !important; border:1px solid var(--line-strong) !important;
  padding:5px 10px !important; font-size:12.5px !important; }
.btn-quiet:hover { color:var(--ink) !important; border-color:var(--ink) !important; background:transparent !important; }
.btn-danger { background:#943030 !important; border-color:#943030 !important; color:#fff !important; }
.btn-danger:hover { background:#7a2626 !important; }
.btn-danger[disabled] { opacity:.45; cursor:not-allowed; }
.btn-text { background:transparent !important; border:0 !important; color:var(--muted) !important; padding:4px 6px !important;
  text-decoration:underline; text-underline-offset:3px; }
.btn-text:hover { color:var(--ink) !important; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.field-label { font-size:12px; font-weight:600; color:var(--muted); display:block; margin-bottom:4px; }
.badge.current { background:var(--accent); color:#fff; border-color:var(--accent); }
.rel { border:1px solid var(--line); border-radius:14px; background:var(--card); padding:16px 18px; margin:0 0 12px; }
.rel.current { border:1.5px solid var(--accent); box-shadow:0 10px 26px -16px rgba(47,134,201,.6); background:#fff; }
.rel.muted { background:transparent; box-shadow:none; color:var(--muted); }
.rel .rh { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.rel .rh .v { font-family:var(--serif); font-size:20px; font-weight:600; color:var(--ink); }
.rel.muted .rh .v { font-size:16px; color:var(--muted); }
.rel .when { margin-left:auto; color:var(--faint); font-size:12.5px; }
.rel .acts { display:flex; gap:10px; align-items:center; margin-top:12px; }
.rel .chg { margin:12px 0 0; padding:0; list-style:none; font-size:13.5px; color:var(--ink); }
.rel .chg li { padding:5px 0; border-top:1px solid var(--line); }
.rel .chg li:first-child { border-top:0; }
.rel .chg .k { font-size:10.5px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--accent-ink);
  margin-right:8px; }
.rel .chg .by { color:var(--muted); font-size:12.5px; margin-left:6px; }
.rel details summary { cursor:pointer; color:var(--muted); font-size:12.5px; margin-top:10px; }

/* ---- dialog ---- */
.dlg-back { position:fixed; inset:0; background:rgba(15,42,61,.55); display:none; align-items:center; justify-content:center;
  z-index:50; padding:20px; }
.dlg-back.on { display:flex; }
.dlg { background:#fff; border-radius:16px; max-width:560px; width:100%; max-height:86vh; overflow:auto; padding:22px 24px;
  box-shadow:0 30px 70px -20px rgba(15,42,61,.55); }
.dlg.wide { max-width:820px; }
.dlg h3 { font-family:var(--serif); font-size:20px; margin:0 0 8px; }
.dlg .foot { display:flex; gap:10px; justify-content:flex-end; margin-top:18px; }

/* ---- tabs and segmented toggle ---- */
.tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin:14px 0 18px; }
.tabs button { background:transparent; color:var(--muted); border:0; border-bottom:2px solid transparent; border-radius:0;
  padding:9px 14px; font-size:14px; font-weight:600; cursor:pointer; }
.tabs button:hover { color:var(--ink); background:transparent; }
.tabs button.on { color:var(--accent-ink); border-bottom-color:var(--accent); background:transparent; }
.tabpane[hidden] { display:none; }
.seg { display:inline-flex; background:var(--dim-soft); border-radius:8px; padding:2px; gap:2px; }
.seg button { background:transparent; color:var(--muted); border:0; border-radius:6px; padding:3px 10px; font-size:12px;
  font-weight:600; cursor:pointer; }
.seg button:hover { background:transparent; color:var(--ink); }
.seg button.on { background:#fff; color:var(--ink); box-shadow:0 1px 2px rgba(15,42,61,.15); }

/* ---- tiles (Cards) ---- */
.tiles { display:grid; grid-template-columns:repeat(auto-fill,minmax(380px,1fr)); gap:16px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:14px; display:flex; flex-direction:column;
  box-shadow:var(--shadow); overflow:hidden; min-width:0; }
.tile .th { display:flex; align-items:center; gap:8px; padding:10px 14px; border-bottom:1px solid var(--line);
  font-size:12px; color:var(--muted); }
.tile .th .sp { margin-left:auto; }
.sec { padding:10px 14px 10px 12px; border-left:3px solid transparent; margin:8px 0 0 0; }
.sec .lab { font-size:10.5px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--faint);
  margin-bottom:3px; }
.sec .txt { margin-left:10px; font-size:14.5px; line-height:1.5; overflow-wrap:anywhere; }
.sec.front { border-left-color:var(--accent); }
.sec.back { border-left-color:var(--teal); }
.sec.extra { border-left-color:var(--line-strong); color:var(--muted); }
.sec .txt.clamp { display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }
.tile .more { background:transparent !important; border:0 !important; color:var(--accent-ink) !important;
  padding:0 0 0 10px !important; font-size:12.5px; text-decoration:underline; cursor:pointer; }
.tile .tg { display:flex; gap:6px; flex-wrap:wrap; padding:10px 14px 14px; margin-top:auto; position:relative; }
.cloze-hide { background:var(--accent-soft); color:var(--accent); font-weight:700; border-radius:4px; padding:0 4px; }
.cloze-show { background:#fff1a8; font-weight:600; border-radius:4px; padding:0 3px; }
.pop { position:absolute; left:14px; bottom:calc(100% - 6px); background:#fff; border:1px solid var(--line);
  border-radius:10px; padding:10px; box-shadow:0 12px 30px -10px rgba(15,42,61,.35); z-index:20; display:flex; gap:6px;
  flex-wrap:wrap; max-width:340px; }
.pop[hidden] { display:none; }

/* ---- Anki view ---- */
.anki { background:#2c2c2c; color:#e4e4e4; border-radius:14px; padding:26px 28px; text-align:center; min-height:300px;
  display:flex; flex-direction:column; }
.anki .face { flex:1; font-size:20px; line-height:1.5; text-align:center; }
.anki .face img { max-width:100%; }
.anki hr { border:0; border-top:1px solid #555; margin:18px 0; }
.anki .bar { margin-top:18px; display:flex; gap:10px; justify-content:center; flex-wrap:wrap; }
.anki .bar button { background:#3a3a3a; color:#eee; border:1px solid #555; border-radius:8px; padding:8px 18px; }
.anki .bar button:hover { background:#4a4a4a; }
.anki small { color:#999; }
.anki .cloze-show { color:#1b1b1b; }
.anki .cloze-hide { background:#1f3a52; color:#8fd0ff; }
button[disabled] { opacity:.45; cursor:not-allowed; }

/* ---- phone and small screens ---- */
.mbar { display:none; }
.scrim { display:none; }
@media (max-width:900px) {
  .staff { display:block; }
  .mbar { display:flex; align-items:center; gap:12px; position:sticky; top:0; z-index:30; background:var(--navy); color:#fff;
    padding:10px 14px; }
  .mbar button { background:transparent; border:1px solid #3a6482; color:#fff; border-radius:8px; padding:6px 10px;
    font-size:14px; }
  .mbar .brand { font-family:var(--serif); font-size:18px; font-weight:600; color:#fff; }
  .mbar .deckname { margin-left:auto; font-size:12.5px; color:#8FB0C6; }
  .sidebar { position:fixed; left:0; top:0; bottom:0; width:284px; height:100vh; z-index:50; transform:translateX(-102%);
    transition:transform .2s ease-out; box-shadow:none; }
  body.nav-open .sidebar { transform:none; box-shadow:0 0 60px rgba(0,0,0,.45); }
  body.nav-open .scrim { display:block; position:fixed; inset:0; background:rgba(15,42,61,.5); z-index:40; }
  .staff main .wrap, .wrap { padding:18px 16px 56px; }
  h1 { font-size:26px; margin-top:8px; }
  .tiles { grid-template-columns:1fr; }
  .panes, .panes.eq, .panes.two { grid-template-columns:1fr; }
  .ask-body { grid-template-columns:1fr; }
  .viewpane { height:300px; }
  .pane { border-right:0; border-bottom:1px solid var(--line); }
  .pane-body { max-height:300px; }
  table.tbl, table.users { display:block; overflow-x:auto; -webkit-overflow-scrolling:touch; white-space:nowrap; }
  table.tbl td .copyblock code { white-space:normal; min-width:220px; }
  input[type=text], input[type=email], input[type=password], select { min-width:0 !important; max-width:100%; }
  .row { align-items:stretch; }
  .row > * { flex:1 1 100%; }
  .row button, .row .btn-link-primary { flex:0 0 auto; }
  .tabs { overflow-x:auto; white-space:nowrap; }
  .actions .btns { flex-wrap:wrap; }
  .actions .btns > * { flex:1 1 auto; text-align:center; }
  .card-head { gap:6px 10px; }
  .copyblock { flex-wrap:wrap; }
  .dlg { padding:18px 16px; max-height:90vh; }
  .anki { padding:18px 14px; }
  .anki .face { font-size:17px; }
  .rel .rh .when { margin-left:0; width:100%; }
  .infobox { max-width:none; }
  .login-wrap { margin-top:5vh; padding:24px 20px; }
  header.site .wrap { flex-wrap:wrap; gap:8px 14px; padding:12px 16px; }
  header.site nav { margin-left:0; width:100%; display:flex; flex-wrap:wrap; gap:6px 14px; }
  header.site nav a { margin-left:0; font-size:13.5px; }
  .entry { grid-template-columns:1fr; gap:4px; }
}
@media (max-width:420px) {
  h1 { font-size:23px; }
  .tile .th { flex-wrap:wrap; }
  .panes .pane-label { flex-wrap:wrap; }
}
"""


UI_JS = """
<script>
(function () {
  // local time for <time data-utc>
  document.querySelectorAll('time[data-utc]').forEach(function (t) {
    var d = new Date(t.dataset.utc); if (isNaN(d)) return;
    var o = {day: 'numeric', month: 'short', year: 'numeric'};
    if (t.dataset.time === '1') { o.hour = '2-digit'; o.minute = '2-digit'; }
    t.textContent = d.toLocaleString(undefined, o);
  });
  // phone navigation
  var navBtn = document.getElementById('navbtn');
  if (navBtn) {
    navBtn.addEventListener('click', function (e) { e.stopPropagation(); document.body.classList.toggle('nav-open'); });
    var scrim = document.getElementById('scrim');
    if (scrim) scrim.addEventListener('click', function () { document.body.classList.remove('nav-open'); });
    document.querySelectorAll('.sidebar nav a').forEach(function (a) {
      a.addEventListener('click', function () { document.body.classList.remove('nav-open'); });
    });
  }
  // menus
  document.querySelectorAll('[data-menu]').forEach(function (b) {
    b.addEventListener('click', function (e) {
      e.stopPropagation();
      var m = document.getElementById(b.dataset.menu); if (m) m.hidden = !m.hidden;
    });
  });
  document.addEventListener('click', function (e) {
    document.querySelectorAll('.menu').forEach(function (m) { if (!m.contains(e.target)) m.hidden = true; });
    document.querySelectorAll('.pop').forEach(function (m) { if (!m.contains(e.target)) m.hidden = true; });
  });
  // copy buttons
  document.querySelectorAll('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      var label = b.textContent;
      navigator.clipboard.writeText(b.dataset.copy).then(function () {
        b.textContent = 'Copied'; setTimeout(function () { b.textContent = label; }, 1500);
      });
    });
  });
  // dialogs
  document.querySelectorAll('[data-dialog]').forEach(function (b) {
    b.addEventListener('click', function () {
      var d = document.getElementById(b.dataset.dialog); if (d) d.classList.add('on');
    });
  });
  document.querySelectorAll('[data-close]').forEach(function (b) {
    b.addEventListener('click', function () { var d = b.closest('.dlg-back'); if (d) d.classList.remove('on'); });
  });
  document.querySelectorAll('.dlg-back').forEach(function (d) {
    d.addEventListener('click', function (e) { if (e.target === d) d.classList.remove('on'); });
  });
  // tabs: [data-tabs] > button[data-tab=id]; panes .tabpane#id
  document.querySelectorAll('[data-tabs]').forEach(function (bar) {
    var btns = bar.querySelectorAll('button[data-tab]');
    function show(id) {
      btns.forEach(function (b) { b.classList.toggle('on', b.dataset.tab === id); });
      document.querySelectorAll('.tabpane').forEach(function (p) { p.hidden = p.id !== id; });
      try { history.replaceState(null, '', '#' + id); } catch (e) {}
    }
    btns.forEach(function (b) { b.addEventListener('click', function () { show(b.dataset.tab); }); });
    var start = (location.hash || '').slice(1);
    show(start && bar.querySelector('[data-tab="' + start + '"]') ? start : btns[0].dataset.tab);
  });
  // segmented toggles: .seg[data-for=groupname] > button[data-show=value]; targets [data-group][data-value]
  document.querySelectorAll('.seg[data-for]').forEach(function (seg) {
    var group = seg.dataset.for;
    seg.querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () {
        seg.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
        var scope = seg.closest('[data-scope]') || document;
        scope.querySelectorAll('[data-group="' + group + '"]').forEach(function (t) {
          t.hidden = t.dataset.value !== b.dataset.show;
        });
      });
    });
  });
  // popovers: button[data-pop=id]
  document.querySelectorAll('[data-pop]').forEach(function (b) {
    b.addEventListener('click', function (e) {
      e.stopPropagation();
      var p = document.getElementById(b.dataset.pop); if (p) p.hidden = !p.hidden;
    });
  });
})();
</script>
"""
