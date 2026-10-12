"""
Doctrine Editor — Anki add-on prototype.

Adds a "Suggest" button to the reviewer bottom bar. Clicking it opens a
dialog where the student picks a suggestion type and writes a comment.
The add-on silently attaches the card's DoctrineID, note id, deck,
fields, rendered HTML and a content hash, and POSTs it to the platform.

Tools menu (Tools -> Doctrine Editor):
  - "Register a deck..." : dev helper that uploads a deck's notes as the
    master state on the platform. Read-only: it never modifies the
    collection. Identity is the note guid, which the deck pipeline sets
    to the card's database id.

No user-facing config: the endpoint, button position and ID field are
constants below, changed by shipping a new build.
"""

import hashlib
import json
import os
import time
import urllib.request
import urllib.error

from aqt import mw, gui_hooks
from aqt.qt import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QTextEdit, QLineEdit, QPushButton, QInputDialog, QCheckBox, QPlainTextEdit, Qt
)
from aqt.utils import tooltip, showInfo, showWarning, openLink

from . import addon_log, chunk_util, identity, official, payload, resolver, state, sync, sync_plan, sync_state

ADDON_NAME = "Doctrine Editor"
ADDON_VERSION = "1.6"

SUGGESTION_TYPES = [
    ("typo", "Typo / spelling"),
    ("incorrect", "Incorrect information"),
    ("confusing", "Confusing / unclear"),
    ("media", "Image or audio problem"),
    ("other", "Other"),
]


# ---------------------------------------------------------------- config

DEFAULT_BOOTSTRAP = "https://doctrine-editor-production.up.railway.app"


def user_files_dir():
    """Anki preserves user_files/ across add-on updates."""
    return os.path.join(os.path.dirname(__file__), "user_files")


_log = addon_log.setup(user_files_dir())


# Everything below is ours, not the student's. The add-on deliberately
# ships no config.json, so Anki shows no Config panel at all: the endpoint,
# the button position and the ID field are decisions we make and change by
# releasing a new build. API_BASE_OVERRIDE is a development escape hatch --
# set it in this file when testing against a local server, never in a
# shipped build.
# Dev tooling. A shipped build has DEV_MODE = False, so students never see
# the stamp/register menu at all -- in production the generation pipeline
# mints DoctrineIDs and the .apkg ships with them already in place.
# PIPELINE_API_KEY must stay empty in anything that leaves this machine;
# the server rejects the register call without it regardless.
DEV_MODE = False
PIPELINE_API_KEY = ""
# DEBUG_MODE: the button never hides. On unsupported cards it renders
# disabled with the reason, so "no button" can be told apart from
# "button hidden on purpose". On in DEV builds.
DEBUG_MODE = False
_log.info("loaded Doctrine Editor %s (dev=%s debug=%s)", ADDON_VERSION, DEV_MODE, DEBUG_MODE)

API_BASE_OVERRIDE = ""
# Identity is the note guid; nothing is stamped. LEGACY_ID_FIELD is only
# excluded from content hashing, so a profile that was stamped by an older
# build still hashes identically to a clean import of the same deck.
LEGACY_ID_FIELD = "DoctrineID"
BUTTON_TOP_OFFSET = 150
BUTTON_RIGHT_OFFSET = 12


def get_config():
    return {
        "bootstrap_url": DEFAULT_BOOTSTRAP,
        "api_base_override": API_BASE_OVERRIDE,
        "button_top_offset": BUTTON_TOP_OFFSET,
        "button_right_offset": BUTTON_RIGHT_OFFSET,
        "_cache": state.load(user_files_dir()),
    }


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
        state.save(user_files_dir(), cache)
    return base


def cached_api_base(cfg) -> str:
    """Resolve without touching the network — safe on the UI thread."""
    return resolver.resolve_api_base(
        cfg, dict(cfg.get("_cache") or {}),
        fetch=lambda url: None, now=time.time,
    )


# ---------------------------------------------------------------- hashing
# Must stay in sync with content_hash() on the server.

def content_hash(fields: dict, id_field: str) -> str:
    parts = []
    for name in sorted(fields.keys()):
        if name == id_field:
            continue
        parts.append(f"{name}\x1f{fields[name]}")
    blob = "\x1e".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- http

# A single suggestion is small and interactive, so it fails fast. A deck
# registration uploads every note and needs room to finish.
SUGGEST_TIMEOUT = 20
REGISTER_TIMEOUT = 120
REGISTER_BATCH_SIZE = 500


def post_json(url: str, payload: dict, bearer: str = "",
              timeout: int = SUGGEST_TIMEOUT) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- dialog

class SuggestionDialog(QDialog):
    def __init__(self, parent, card_label: str, fields=None):
        super().__init__(parent)
        self.setWindowTitle("Suggest an edit")
        self.setMinimumWidth(460)
        self._fields = dict(fields or {})

        layout = QVBoxLayout(self)

        lbl = QLabel(f"Suggestion for: <b>{card_label}</b>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        layout.addWidget(QLabel("What kind of issue is it?"))
        self.type_box = QComboBox()
        for key, label in SUGGESTION_TYPES:
            self.type_box.addItem(label, key)
        layout.addWidget(self.type_box)

        layout.addWidget(QLabel("Describe the issue or your suggestion:"))
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText(
            "e.g. The dose in the answer should be 5 mg, not 50 mg."
        )
        self.text_edit.setMinimumHeight(110)
        layout.addWidget(self.text_edit)

        # Optional: propose the exact change. Editable copies of Text and
        # Extra; only fields that differ are sent. Never touches the note.
        self.propose_box = QCheckBox("Propose the exact change (edit the card text)")
        self.propose_box.setChecked(False)
        layout.addWidget(self.propose_box)
        self.propose_area = QVBoxLayout()
        self.field_edits = {}
        for name in payload.EDITABLE_FIELDS:
            if name not in self._fields:
                continue
            lbl = QLabel(name)
            edit = QPlainTextEdit()
            edit.setPlainText(self._fields.get(name, ""))
            edit.setMinimumHeight(90 if name == "Text" else 60)
            lbl.setVisible(False); edit.setVisible(False)
            self.propose_area.addWidget(lbl); self.propose_area.addWidget(edit)
            self.field_edits[name] = (lbl, edit)
        layout.addLayout(self.propose_area)
        self.propose_box.toggled.connect(self._toggle_propose)
        if not self.field_edits:
            self.propose_box.setVisible(False)

        layout.addWidget(QLabel("Email (optional, to hear back about it):"))
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@example.com")
        layout.addWidget(self.email_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        send = QPushButton("Send suggestion")
        send.setDefault(True)
        send.clicked.connect(self._on_send)
        btn_row.addWidget(cancel)
        btn_row.addWidget(send)
        layout.addLayout(btn_row)

        self.result_data = None

    def _toggle_propose(self, on):
        for lbl, edit in self.field_edits.values():
            lbl.setVisible(on); edit.setVisible(on)
        self.adjustSize()

    def _on_send(self):
        text = self.text_edit.toPlainText().strip()
        proposed = {}
        if self.propose_box.isChecked():
            after = {name: edit.toPlainText() for name, (_, edit) in self.field_edits.items()}
            proposed = payload.proposed(self._fields, after)
        if not text and not proposed:
            showWarning("Please describe the issue, or propose a change, before sending.")
            return
        self.result_data = {
            "suggestion_type": self.type_box.currentData(),
            "text": text or "(proposed an edit, see the change)",
            "email": self.email_edit.text().strip() or None,
        }
        if proposed:
            self.result_data["proposed_fields"] = proposed
        self.accept()


class SentDialog(QDialog):
    """Confirmation with the tracking link as a link, not a wall of text."""

    def __init__(self, parent, url: str):
        super().__init__(parent)
        self.setWindowTitle("Suggestion sent")
        self.setMinimumWidth(440)
        self.url = url

        layout = QVBoxLayout(self)
        head = QLabel("<b>Thank you — your suggestion was sent.</b>")
        layout.addWidget(head)

        sub = QLabel("A reviewer will look at it. You can check what happened "
                     "to it any time at this link:")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        link = QLabel(f'<a href="{url}">{url}</a>')
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        link.setWordWrap(True)
        link.setStyleSheet("padding: 8px; background: rgba(128,128,128,0.12);"
                           " border-radius: 4px;")
        layout.addWidget(link)

        btns = QHBoxLayout()
        copy_btn = QPushButton("Copy link")
        copy_btn.clicked.connect(self._copy)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda: openLink(self.url))
        done = QPushButton("Done")
        done.setDefault(True)
        done.clicked.connect(self.accept)
        btns.addWidget(copy_btn)
        btns.addWidget(open_btn)
        btns.addStretch()
        btns.addWidget(done)
        layout.addLayout(btns)

    def _copy(self):
        QApplication.clipboard().setText(self.url)
        tooltip("Link copied", period=1500)


# ---------------------------------------------------------------- official set

_official = {"value": None}


def official_set():
    """The cached official guid set, or None if never fetched."""
    if _official["value"] is None:
        _official["value"] = official.load(user_files_dir())
    return _official["value"]


def card_is_official(note, deck_name) -> bool:
    """Button rule. With a cached list: guid must be in it. Without one
    (first run, offline): fall back to the deck-name heuristic so the button
    is not shown on every deck; the server still decides."""
    cached = official_set()
    if cached is not None:
        return official.is_official(note.guid, cached.guids)
    return identity.is_doctrine_card(list(note.keys()), deck_name)


def fetch_official(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


def refresh_official_in_background(force=False):
    def task():
        cfg = get_config()
        return official.refresh(user_files_dir(), api_base(cfg), fetch_official,
                                now=time.time, force=force)

    def done(fut):
        try:
            result = fut.result()
        except Exception:
            addon_log.exception("official refresh")
            return
        _official["value"] = result
        if result:
            _log.info("official set: release=%s guids=%d", result.release, len(result.guids))

    mw.taskman.run_in_background(task, done)


# ---------------------------------------------------------------- suggest flow

@addon_log.guarded("open_suggestion_dialog")
def open_suggestion_dialog():
    card = mw.reviewer.card if mw.reviewer else None
    if card is None:
        tooltip("No card is being reviewed.")
        return

    cfg = get_config()
    note = card.note()
    field_names = list(note.keys())
    deck_name = mw.col.decks.name(card.did)

    if not card_is_official(note, deck_name):
        showInfo(
            "This card is not part of the Doctorine deck, so suggestions "
            "can't be sent for it."
        )
        return

    fields_now = {name: note[name] for name in field_names}
    doctorine_id, guid = identity.identity_of(fields_now, note.guid)
    doctrine_id = doctorine_id or guid

    try:
        note_type = note.note_type()
    except AttributeError:  # older Anki
        note_type = note.model()

    dlg = SuggestionDialog(mw, f"{note_type['name']} — {deck_name}",
                           {name: note[name] for name in field_names})
    if not dlg.exec():
        return

    fields = {name: note[name] for name in field_names}
    try:
        from anki.buildinfo import version as anki_version
    except Exception:
        anki_version = None
    body = {
        "doctrine_id": doctrine_id,       # filing key: Doctorine ID if set, else guid
        "doctorine_id": doctorine_id,     # their id, may be empty
        "guid": guid,                     # Anki's, always present
        "anki_note_id": note.id,
        "note_type": note_type["name"],
        "deck": deck_name,
        "snapshot": {
            "question_html": card.question(),
            "answer_html": card.answer(),
            "css": note_type.get("css", ""),
            "fields": fields,
            "content_hash": content_hash(fields, LEGACY_ID_FIELD),
        },
        **payload.trace(
            tags=note.tags,
            note_mod=note.mod,
            card_id=card.id,
            card_ord=card.ord,
            template_names=[t.get("name") for t in note_type.get("tmpls", [])],
            deck_id=card.did,
            original_deck_id=card.odid,
            install_id=state.install_id(user_files_dir()),
            addon_version=ADDON_VERSION,
            anki_version=anki_version,
        ),
        **dlg.result_data,
    }

    def task():
        return post_json(api_base(cfg) + "/api/suggestions", body)

    def on_done(fut):
        try:
            result = fut.result()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
            except Exception:
                msg = str(e)
            _log.warning("suggestion rejected: %s %s", e.code, msg)
            showWarning(f"The platform rejected the suggestion:\n{msg}")
            return
        except Exception as e:
            _log.error("suggestion send failed: %r", e)
            showWarning(
                "Could not reach the suggestion server.\n"
                f"Is it running at {cached_api_base(cfg)}?\n\n{e}"
            )
            return
        tracking = result.get("tracking_url")
        _log.info("suggestion sent id=%s tracking=%s", doctrine_id, tracking)
        if tracking:
            SentDialog(mw, tracking).exec()
        else:
            tooltip("Suggestion sent — thank you!", period=2500)

    mw.taskman.run_in_background(task, on_done)


# ---------------------------------------------------------------- bottom bar button

@addon_log.guarded("webview_will_set_content")
def on_webview_will_set_content(web_content, context):
    try:
        from aqt.reviewer import Reviewer
    except ImportError:
        return
    if not isinstance(context, Reviewer):
        return
    # Hidden by default; shown per-card only when the note carries a
    # DoctrineID (see on_reviewer_did_show_question).
    cfg = get_config()
    web_content.body += """
<style>
#doctrine-suggest-btn {
  position: fixed; right: %dpx; top: %dpx; z-index: 300;
  display: none;
  padding: 4px 10px; font-size: 12px; cursor: pointer;
  border: 1px solid #888; border-radius: 4px; background: transparent;
  color: inherit;
}
#doctrine-suggest-btn:hover { background: rgba(128,128,128,0.15); }
</style>
<button id="doctrine-suggest-btn" onclick="pycmd('doctrine_editor')"
        title="Suggest an edit to this card">&#9998; Suggest an edit</button>
<style>#doctrine-suggest-btn:disabled { opacity:.45; cursor:not-allowed; }</style>
""" % (cfg["button_right_offset"], cfg["button_top_offset"])


@addon_log.guarded("reviewer_did_show_question")
def on_reviewer_did_show_question(card):
    note = card.note()
    field_names = list(note.keys())
    deck_name = mw.col.decks.name(card.did)
    supported = card_is_official(note, deck_name)
    if supported or not DEBUG_MODE:
        js = ("var b = document.getElementById('doctrine-suggest-btn');"
              "if (b) { b.style.display = '%s'; b.disabled = false; "
              "b.title = 'Suggest an edit to this card'; }"
              % ("block" if supported else "none"))
    else:
        cached = official_set()
        reason = ("guid not in official release %s" % cached.release if cached
                  else "no official list cached; deck %r is not Doctorine" % deck_name)
        js = ("var b = document.getElementById('doctrine-suggest-btn');"
              "if (b) { b.style.display = 'block'; b.disabled = true; "
              "b.title = %s; }" % json.dumps("Not a Doctrine card: " + reason))
    mw.reviewer.web.eval(js)
    _log.debug("card %s deck=%r type=%r supported=%s", card.id, deck_name,
               note.note_type()["name"], supported)


def on_js_message(handled, message, context):
    # Filter hook: must return a (handled, value) tuple even on failure,
    # or every other add-on's handler downstream breaks.
    try:
        if message == "doctrine_editor":
            open_suggestion_dialog()
            return (True, None)
    except Exception:
        addon_log.exception("js_message")
    return handled


# ---------------------------------------------------------------- dev: stamp & register

def register_deck():
    """Dev tool: upload a deck's notes as the master state. Read-only.

    Nothing here writes to the collection. Identity is note.guid, which
    the deck pipeline sets to the card's database id, so no field is
    added and no note type is modified -- students are never forced into
    a full sync by anything this add-on does.
    """
    cfg = get_config()

    deck_names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
    if not deck_names:
        showInfo("No decks found.")
        return
    name, ok = QInputDialog.getItem(
        mw, ADDON_NAME, "Deck to register as master state:", deck_names, 0, False
    )
    if not ok or not name:
        return

    note_ids = mw.col.find_notes(f'deck:"{name}"')
    if not note_ids:
        showInfo(f'No notes found in deck "{name}".')
        return

    registered = []
    for nid in note_ids:
        note = mw.col.get_note(nid)
        try:
            m = note.note_type()
        except AttributeError:
            m = note.model()

        # Fields and tags only. Rendered card HTML was 47 KB per note of
        # template boilerplate against ~500 bytes of content; the fields
        # are what a reviewer edits, and what the server keeps.
        fields = {fname: note[fname] for fname in note.keys()}
        cards = note.cards()
        deck_of_card = mw.col.decks.name(cards[0].did) if cards else name

        registered.append({
            "doctrine_id": identity.primary_id(fields, note.guid),
            "doctorine_id": identity.identity_of(fields, note.guid)[0],
            "guid": note.guid,
            "anki_note_id": note.id,
            "note_type": m["name"],
            "deck": deck_of_card,
            "fields": fields,
            "tags": list(note.tags),
        })

    def task():
        """Upload in batches: one request per deck exceeded the timeout."""
        url = api_base(cfg) + "/api/master/sync"
        totals = {"registered": 0, "updated": 0, "batches": 0}
        batches = list(chunk_util.chunked(registered, REGISTER_BATCH_SIZE))
        for index, batch in enumerate(batches, start=1):
            mw.taskman.run_on_main(
                lambda i=index, n=len(batches):
                    mw.progress.update(label=f"Registering batch {i} of {n}…"))
            result = post_json(url, {"notes": batch}, PIPELINE_API_KEY,
                               timeout=REGISTER_TIMEOUT)
            totals["registered"] += result.get("registered", 0)
            totals["updated"] += result.get("updated", 0)
            totals["batches"] += 1
        return totals

    def on_done(fut):
        mw.progress.finish()
        try:
            result = fut.result()
        except Exception as e:
            showWarning(
                f"Registration failed:\n{e}\n\n"
                "The upload did not finish. Re-run this to retry; it is safe "
                "to repeat.")
            return
        showInfo(
            f"Registered {result.get('registered', 0)} notes in "
            f"{result.get('batches', 0)} batch(es) "
            f"({result.get('updated', 0)} updated to a new version)."
        )

    mw.progress.start(label="Registering deck…", immediate=True)
    mw.taskman.run_in_background(task, on_done)


def open_reviewer_page():
    openLink(cached_api_base(get_config()) + "/reviewer")


def open_updates_page():
    openLink(cached_api_base(get_config()) + "/updates")


# ---------------------------------------------------------------- deck sync

class UpdatePromptDialog(QDialog):
    """Release N available: what will change, and the choice to apply it."""

    def __init__(self, parent, text, changelog_url):
        super().__init__(parent)
        self.setWindowTitle("Doctorine deck update")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        head = QLabel("<b>A deck update is available.</b>")
        layout.addWidget(head)
        body = QLabel(text)
        body.setWordWrap(True)
        layout.addWidget(body)
        link = QLabel(f'<a href="{changelog_url}">See what changed</a>')
        link.setOpenExternalLinks(True)
        layout.addWidget(link)
        note = QLabel("Only the card text is updated. Your study progress, tags and "
                      "your own cards are not touched. Edit > Undo reverts the update.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #777; font-size: 12px;")
        layout.addWidget(note)
        row = QHBoxLayout()
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        go = QPushButton("Update now")
        go.setDefault(True)
        go.clicked.connect(self.accept)
        row.addStretch(); row.addWidget(later); row.addWidget(go)
        layout.addLayout(row)


def _fetch_sync_payload(api_base_url, since):
    return get_json(api_base_url + "/api/sync?since=" + urllib.parse.quote(since or "", safe=""))


def check_for_deck_updates(force=False, silent=False):
    """Background: ask the server what changed since our release. On the main
    thread: build the plan against the collection, prompt, apply."""
    d = user_files_dir()
    st = sync_state.load(d)
    if not sync_state.due(st, time.time(), force):
        return

    def task():
        cfg = get_config()
        base = api_base(cfg)
        current = get_json(base + "/api/sync/version").get("current")
        if not current or current == st["release"]:
            return {"base": base, "current": current, "payload": None}
        return {"base": base, "current": current, "payload": _fetch_sync_payload(base, st["release"])}

    def done(fut):
        try:
            result = fut.result()
        except Exception as e:
            addon_log.exception("deck update check")
            if not silent:
                showWarning(f"Could not check for deck updates:\n{e!r}")
            return
        sync_state.save(d, st["release"], time.time())
        payload = result["payload"]
        if payload is None:
            if st["release"] == "" and result["current"]:
                # First run on a collection that already matches: adopt the version.
                sync_state.save(d, result["current"], time.time())
            if not silent:
                showInfo("Your Doctorine deck is up to date.")
            return
        _offer_update(result["base"], payload, silent)

    mw.taskman.run_in_background(task, done)


def _offer_update(base, payload, silent):
    d = user_files_dir()
    guids = [c["guid"] for c in payload.get("changed", [])] + [a["guid"] for a in payload.get("added", [])] \
            + list(payload.get("retired", []))
    local = sync.local_state(mw.col, guids)
    have = sync.media_have(mw.col, payload.get("media", []))
    plan = sync_plan.plan(payload, local, have)
    to_version = payload.get("to") or "?"
    if not plan["updates"] and not plan["adds"]:
        sync_state.save(d, to_version, time.time())
        _log.info("deck already matches release %s", to_version)
        if not silent:
            showInfo(f"Your Doctorine deck already matches release {to_version}.")
        return
    text = sync.describe(plan, to_version)
    dlg = UpdatePromptDialog(mw, text, base + "/updates")
    if not dlg.exec():
        _log.info("deck update to %s deferred", to_version)
        return
    _apply_update(base, plan, to_version)


def _apply_update(base, plan, to_version):
    d = user_files_dir()
    mw.progress.start(label="Updating Doctorine deck…", immediate=True)
    try:
        media_done, media_failed = sync.fetch_media(
            mw.col, base, plan["media"],
            on_progress=lambda i, n: mw.progress.update(label=f"Downloading media {i} of {n}…"))
        counts, failures = sync.apply_plan(
            mw.col, plan,
            on_progress=lambda i, n: mw.progress.update(label=f"Updating cards {i} of {n}…"))
    finally:
        mw.progress.finish()
    if counts["failed"] == 0:
        sync_state.save(d, to_version, time.time())
    _log.info("deck update to %s: %s media=%d/%d failures=%s", to_version, counts, media_done,
              len(plan["media"]), failures[:5])
    refresh_official_in_background(force=True)
    mw.reset()
    msg = (f"Updated to release {to_version}.\n\n{counts['updated']} card(s) updated, "
           f"{counts['added']} added, {media_done} media file(s) downloaded.")
    if counts["failed"] or media_failed:
        msg += (f"\n\n{counts['failed']} card(s) and {len(media_failed)} media file(s) failed; "
                "see Tools > Doctrine Editor > Diagnostics. Run the update again to retry.")
    showInfo(msg)


# ---------------------------------------------------------------- diagnostics

def _section(lines, title, fn):
    """Run one diagnostics probe; a failure becomes a line, never an abort."""
    try:
        out = fn()
        lines.extend(out if isinstance(out, list) else [out])
    except Exception as e:
        lines.append(f"{title}: ERROR {type(e).__name__}: {e}")
        addon_log.exception(f"diagnostics/{title}")


def diagnostics_text() -> str:
    import platform as _platform
    lines = []
    cfg = None

    def versions():
        try:
            from anki.buildinfo import version as anki_version
        except Exception:
            anki_version = "?"
        return [f"Doctrine Editor {ADDON_VERSION}  dev={DEV_MODE} debug={DEBUG_MODE}",
                f"Anki {anki_version}  {_platform.platform()}"]
    _section(lines, "versions", versions)

    def config():
        nonlocal cfg
        cfg = get_config()
        return [f"install_id: {state.install_id(user_files_dir())}",
                f"bootstrap: {cfg['bootstrap_url']}",
                f"api_base (cached): {cached_api_base(cfg)}"]
    _section(lines, "config", config)

    def server():
        # Short timeout: this runs on the UI thread while the dialog opens.
        base = cached_api_base(cfg or get_config())
        req = urllib.request.Request(base + "/where", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        return f"server: reachable, api_base={info.get('api_base')}"
    _section(lines, "server", server)

    def hooks():
        def has(hook, fn):
            for attr in ("_hooks", "hooks"):
                lst = getattr(hook, attr, None)
                if lst is not None:
                    return "on" if fn in lst else "OFF"
            return "?"
        return "hooks: " + ", ".join([
            f"webview_will_set_content={has(gui_hooks.webview_will_set_content, on_webview_will_set_content)}",
            f"reviewer_did_show_question={has(gui_hooks.reviewer_did_show_question, on_reviewer_did_show_question)}",
            f"js_message={has(gui_hooks.webview_did_receive_js_message, on_js_message)}",
        ])
    _section(lines, "hooks", hooks)

    def off():
        cached = official_set()
        if cached is None:
            return "official set: not cached (button falls back to deck-name rule)"
        return (f"official set: release={cached.release} guids={len(cached.guids)} "
                f"fetched={time.strftime('%Y-%m-%d %H:%M', time.gmtime(cached.fetched_at))} UTC")
    _section(lines, "official", off)
    _section(lines, "sync", lambda: "deck release: %s (last check %s)" % (
        sync_state.load(user_files_dir())["release"] or "unknown",
        time.strftime("%Y-%m-%d %H:%M", time.gmtime(sync_state.load(user_files_dir())["last_check"])) + " UTC"))

    def card():
        card = mw.reviewer.card if mw.reviewer else None
        if card is None:
            return "current card: none (open the reviewer first)"
        note = card.note()
        names = list(note.keys())
        fields = {n: note[n] for n in names}
        doc, guid = identity.identity_of(fields, note.guid)
        deck = mw.col.decks.name(card.did)
        return ["current card:",
                f"  deck: {deck!r} (did={card.did})",
                f"  nid={note.id} cid={card.id} ord={card.ord} guid={note.guid!r}",
                f"  note type: {note.note_type()['name']!r}",
                f"  fields: {names}",
                f"  {identity.ID_FIELD!r} value: {doc!r}",
                f"  official: {card_is_official(note, deck)}"]
    _section(lines, "card", card)

    lines.append("")
    lines.append("--- last 20 log lines ---")
    _section(lines, "log", lambda: [l.rstrip() for l in addon_log.tail(user_files_dir(), 20)])
    return "\n".join(lines)


class DiagnosticsDialog(QDialog):
    def __init__(self, parent, text):
        super().__init__(parent)
        self.setWindowTitle("Doctrine Editor — Diagnostics")
        self.setMinimumSize(640, 480)
        layout = QVBoxLayout(self)
        self.box = QTextEdit()
        self.box.setReadOnly(True)
        self.box.setPlainText(text)
        self.box.setStyleSheet("font-family: Menlo, monospace; font-size: 12px;")
        layout.addWidget(self.box)
        row = QHBoxLayout()
        copy = QPushButton("Copy to clipboard")
        copy.clicked.connect(self._copy)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(copy); row.addStretch(); row.addWidget(close)
        layout.addLayout(row)

    def _copy(self):
        QApplication.clipboard().setText(self.box.toPlainText())
        tooltip("Diagnostics copied", period=1500)


def open_diagnostics():
    # User-triggered: a failure must be visible, not swallowed.
    try:
        text = diagnostics_text()
    except Exception:
        import traceback
        addon_log.exception("diagnostics")
        text = "Diagnostics itself failed:\n\n" + traceback.format_exc()
    try:
        DiagnosticsDialog(mw, text).exec()
    except Exception:
        addon_log.exception("diagnostics dialog")
        showWarning(text)


# ---------------------------------------------------------------- menu & hooks

def setup_menu():
    from aqt.qt import QMenu, QAction
    menu = QMenu(ADDON_NAME, mw)

    a1 = QAction("Suggest an edit for current card", mw)
    a1.triggered.connect(open_suggestion_dialog)
    menu.addAction(a1)

    a4 = QAction("Open public updates page", mw)
    a4.triggered.connect(open_updates_page)
    menu.addAction(a4)

    a5 = QAction("Diagnostics…", mw)
    a5.triggered.connect(open_diagnostics)
    menu.addAction(a5)

    a6 = QAction("Check for deck updates…", mw)
    a6.triggered.connect(lambda: check_for_deck_updates(force=True))
    menu.addAction(a6)

    if DEV_MODE:
        menu.addSeparator()

        a2 = QAction("Register a deck as master… (dev)", mw)
        a2.triggered.connect(register_deck)
        menu.addAction(a2)

        a3 = QAction("Open reviewer queue (dev)", mw)
        a3.triggered.connect(open_reviewer_page)
        menu.addAction(a3)

    mw.form.menuTools.addMenu(menu)


gui_hooks.webview_will_set_content.append(on_webview_will_set_content)
gui_hooks.reviewer_did_show_question.append(on_reviewer_did_show_question)
gui_hooks.webview_did_receive_js_message.append(on_js_message)
gui_hooks.main_window_did_init.append(setup_menu)
gui_hooks.profile_did_open.append(lambda: refresh_official_in_background())
gui_hooks.profile_did_open.append(lambda: check_for_deck_updates(silent=True))
