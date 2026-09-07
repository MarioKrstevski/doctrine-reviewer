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
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QTextEdit,
    QLineEdit, QPushButton, QInputDialog, Qt
)
from aqt.utils import tooltip, showInfo, showWarning, openLink

from . import chunk_util, identity, payload, resolver, state

ADDON_NAME = "Doctrine Editor"
ADDON_VERSION = "1.1"

SUGGESTION_TYPES = [
    ("typo", "Typo / spelling"),
    ("incorrect", "Incorrect information"),
    ("confusing", "Confusing / unclear"),
    ("other", "Other"),
]


# ---------------------------------------------------------------- config

DEFAULT_BOOTSTRAP = "https://doctrine-editor-production.up.railway.app"


def user_files_dir():
    """Anki preserves user_files/ across add-on updates."""
    return os.path.join(os.path.dirname(__file__), "user_files")


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
    def __init__(self, parent, card_label: str):
        super().__init__(parent)
        self.setWindowTitle("Suggest an edit")
        self.setMinimumWidth(420)

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

    def _on_send(self):
        text = self.text_edit.toPlainText().strip()
        if not text:
            showWarning("Please describe the issue before sending.")
            return
        self.result_data = {
            "suggestion_type": self.type_box.currentData(),
            "text": text,
            "email": self.email_edit.text().strip() or None,
        }
        self.accept()


# ---------------------------------------------------------------- suggest flow

def open_suggestion_dialog():
    card = mw.reviewer.card if mw.reviewer else None
    if card is None:
        tooltip("No card is being reviewed.")
        return

    cfg = get_config()
    note = card.note()
    field_names = list(note.keys())
    deck_name = mw.col.decks.name(card.did)

    if not identity.is_doctrine_card(note.guid, deck_name):
        showInfo(
            "This card is not part of a supported deck, so suggestions "
            "can't be sent for it."
        )
        return

    doctrine_id = note.guid

    try:
        note_type = note.note_type()
    except AttributeError:  # older Anki
        note_type = note.model()

    dlg = SuggestionDialog(mw, f"{note_type['name']} — {deck_name}")
    if not dlg.exec():
        return

    fields = {name: note[name] for name in field_names}
    try:
        from anki.buildinfo import version as anki_version
    except Exception:
        anki_version = None
    body = {
        "doctrine_id": doctrine_id,
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
            showWarning(f"The platform rejected the suggestion:\n{msg}")
            return
        except Exception as e:
            showWarning(
                "Could not reach the suggestion server.\n"
                f"Is it running at {cached_api_base(cfg)}?\n\n{e}"
            )
            return
        tracking = result.get("tracking_url")
        if tracking:
            tooltip("Suggestion sent — thank you!", period=2500)
            # Offer the tracking link once, non-blocking style:
            showInfo(
                "Your suggestion was sent.\n\n"
                f"You can follow what happens to it here:\n{tracking}"
            )
        else:
            tooltip("Suggestion sent — thank you!", period=2500)

    mw.taskman.run_in_background(task, on_done)


# ---------------------------------------------------------------- bottom bar button

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
""" % (cfg["button_right_offset"], cfg["button_top_offset"])


def on_reviewer_did_show_question(card):
    note = card.note()
    supported = identity.is_doctrine_card(note.guid, mw.col.decks.name(card.did))
    mw.reviewer.web.eval(
        "var b = document.getElementById('doctrine-suggest-btn');"
        "if (b) b.style.display = '%s';" % ("block" if supported else "none")
    )


def on_js_message(handled, message, context):
    if message == "doctrine_editor":
        open_suggestion_dialog()
        return (True, None)
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
            "doctrine_id": note.guid,
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
