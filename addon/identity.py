"""Client-side check: is this note one of ours, and what identifies it?

The gate is the note type: Doctrine notes carry a field named exactly
"Doctorine ID" (their spelling, with the space). Other decks do not have
that field, so its *presence* is the signal -- the value is often empty
today and may stay empty for a while.

Identity, in order of preference:
  1. the "Doctorine ID" value, if filled -- their database id
  2. the note guid -- Anki's own, unique, survives export/import

Both are opaque strings. Native Anki guids contain characters such as
] % ! : so anything that puts one in a URL must encode it. The add-on
only ever sends them in JSON bodies.

This is a courtesy gate only. The server decides what it accepts.
"""

ID_FIELD = "Doctorine ID"
DECK_NAME = "doctrine"


def is_doctrine_card(field_names, deck_name=None) -> bool:
    if field_names and ID_FIELD in field_names:
        return True
    if isinstance(deck_name, str):
        top = deck_name.strip().split("::", 1)[0].strip().lower()
        if top in (DECK_NAME, "doctorine"):
            return True
    return False


def identity_of(fields, guid):
    """(doctorine_id or '', guid or '') -- always both, never None."""
    doc = ""
    if isinstance(fields, dict):
        doc = (fields.get(ID_FIELD) or "").strip()
    return doc, (guid or "")


def primary_id(fields, guid) -> str:
    """The key a suggestion is filed under: Doctorine ID if set, else guid."""
    doc, g = identity_of(fields, guid)
    return doc or g
