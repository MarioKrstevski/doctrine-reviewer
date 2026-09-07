"""
Doctrine Editor — Anki add-on prototype.

Adds a "Suggest" button to the reviewer bottom bar. Clicking it opens a
dialog where the student picks a suggestion type and writes a comment.
The add-on silently attaches the card's DoctrineID, note id, deck,
fields, rendered HTML and a content hash, and POSTs it to the platform.

Tools menu (Tools -> Doctrine Editor):
  - "Stamp & register a deck..." : dev helper that adds a DoctrineID
    field to every note type in a chosen deck, stamps unique IDs, and
    registers the notes as the "master" state on the platform server.

Config (config.json): server_url, id_field.
"""

import hashlib
import json
import urllib.request
import urllib.error
import uuid

from aqt import mw, gui_hooks
from aqt.qt import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QTextEdit,
    QLineEdit, QPushButton, QInputDialog, Qt
)
from aqt.utils import tooltip, showInfo, showWarning, openLink

ADDON_NAME = "Doctrine Editor"

SUGGESTION_TYPES = [
    ("typo", "Typo / spelling"),
    ("incorrect", "Incorrect information"),
    ("confusing", "Confusing / unclear"),
    ("other", "Other"),
]


# ---------------------------------------------------------------- config

def get_config():
    cfg = mw.addonManager.getConfig(__name__) or {}
    return {
        "server_url": cfg.get("server_url", "http://127.0.0.1:8787").rstrip("/"),
        "id_field": cfg.get("id_field", "DoctrineID"),
    }


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

def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
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

    if cfg["id_field"] not in field_names or not note[cfg["id_field"]].strip():
        showInfo(
            "This card is not part of a supported deck, so suggestions "
            "can't be sent for it."
        )
        return

    doctrine_id = note[cfg["id_field"]].strip()
    deck_name = mw.col.decks.name(card.did)

    try:
        note_type = note.note_type()
    except AttributeError:  # older Anki
        note_type = note.model()

    dlg = SuggestionDialog(mw, f"{note_type['name']} — {deck_name}")
    if not dlg.exec():
        return

    fields = {name: note[name] for name in field_names}
    payload = {
        "doctrine_id": doctrine_id,
        "anki_note_id": note.id,
        "note_type": note_type["name"],
        "deck": deck_name,
        "snapshot": {
            "question_html": card.question(),
            "answer_html": card.answer(),
            "css": note_type.get("css", ""),
            "fields": fields,
            "content_hash": content_hash(fields, cfg["id_field"]),
        },
        **dlg.result_data,
    }

    url = cfg["server_url"] + "/api/suggestions"

    def task():
        return post_json(url, payload)

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
                f"Is it running at {cfg['server_url']}?\n\n{e}"
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
    web_content.body += """
<style>
#doctrine-suggest-btn {
  position: fixed; right: 12px; top: 150px; z-index: 300;
  display: none;
  padding: 4px 10px; font-size: 12px; cursor: pointer;
  border: 1px solid #888; border-radius: 4px; background: transparent;
  color: inherit;
}
#doctrine-suggest-btn:hover { background: rgba(128,128,128,0.15); }
</style>
<button id="doctrine-suggest-btn" onclick="pycmd('doctrine_editor')"
        title="Suggest an edit to this card">&#9998; Suggest an edit</button>
"""


def on_reviewer_did_show_question(card):
    cfg = get_config()
    note = card.note()
    supported = (
        cfg["id_field"] in note.keys() and note[cfg["id_field"]].strip()
    )
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

def stamp_and_register_deck():
    cfg = get_config()
    id_field = cfg["id_field"]

    deck_names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
    if not deck_names:
        showInfo("No decks found.")
        return
    name, ok = QInputDialog.getItem(
        mw, ADDON_NAME, "Deck to stamp and register:", deck_names, 0, False
    )
    if not ok or not name:
        return

    note_ids = mw.col.find_notes(f'deck:"{name}"')
    if not note_ids:
        showInfo(f'No notes found in deck "{name}".')
        return

    models = mw.col.models
    patched_models = set()
    registered = []

    for nid in note_ids:
        note = mw.col.get_note(nid)
        try:
            m = note.note_type()
        except AttributeError:
            m = note.model()

        # Ensure the ID field exists on this note type.
        existing = [f["name"] for f in m["flds"]]
        if id_field not in existing:
            if m["id"] not in patched_models:
                fld = models.new_field(id_field)
                models.add_field(m, fld)
                try:
                    models.update_dict(m)
                except AttributeError:
                    models.save(m)
                patched_models.add(m["id"])
            note = mw.col.get_note(nid)  # reload with new field

        if not note[id_field].strip():
            note[id_field] = "doc-" + uuid.uuid4().hex[:12]
            mw.col.update_note(note)

        fields = {fname: note[fname] for fname in note.keys()}
        cards = note.cards()
        q_html, a_html, css = "", "", m.get("css", "")
        deck_of_card = name
        if cards:
            c = cards[0]
            q_html, a_html = c.question(), c.answer()
            deck_of_card = mw.col.decks.name(c.did)

        registered.append({
            "doctrine_id": note[id_field].strip(),
            "anki_note_id": note.id,
            "note_type": m["name"],
            "deck": deck_of_card,
            "fields": fields,
            "question_html": q_html,
            "answer_html": a_html,
            "css": css,
        })

    url = cfg["server_url"] + "/api/dev/register"

    def task():
        return post_json(url, {"notes": registered})

    def on_done(fut):
        try:
            result = fut.result()
        except Exception as e:
            showWarning(f"Registration failed:\n{e}")
            return
        showInfo(
            f"Registered {result.get('registered', 0)} notes "
            f"({result.get('updated', 0)} updated to a new version).\n\n"
            "Note: adding the ID field changed the note types, so Anki may "
            "ask for a full sync — that's expected on a test profile."
        )

    mw.taskman.run_in_background(task, on_done)


def open_reviewer_page():
    openLink(get_config()["server_url"] + "/reviewer")


def open_updates_page():
    openLink(get_config()["server_url"] + "/updates")


# ---------------------------------------------------------------- menu & hooks

def setup_menu():
    from aqt.qt import QMenu, QAction
    menu = QMenu(ADDON_NAME, mw)

    a1 = QAction("Suggest an edit for current card", mw)
    a1.triggered.connect(open_suggestion_dialog)
    menu.addAction(a1)

    menu.addSeparator()

    a2 = QAction("Stamp && register a deck… (dev)", mw)
    a2.triggered.connect(stamp_and_register_deck)
    menu.addAction(a2)

    a3 = QAction("Open reviewer queue (dev)", mw)
    a3.triggered.connect(open_reviewer_page)
    menu.addAction(a3)

    a4 = QAction("Open public updates page", mw)
    a4.triggered.connect(open_updates_page)
    menu.addAction(a4)

    mw.form.menuTools.addMenu(menu)


gui_hooks.webview_will_set_content.append(on_webview_will_set_content)
gui_hooks.reviewer_did_show_question.append(on_reviewer_did_show_question)
gui_hooks.webview_did_receive_js_message.append(on_js_message)
gui_hooks.main_window_did_init.append(setup_menu)
