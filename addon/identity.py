"""Client-side check: does this note look like one of ours?

Identity is the Anki note guid. The Doctrine pipeline sets guid to the
card's database ObjectId -- 24 lowercase hex characters -- whereas notes
created inside Anki get 10-character base91 guids. That shape is a
reliable, zero-configuration signal for showing the Suggest button.

The deck name is accepted as a second signal so that a future change to
the id format degrades to "button shows, server explains" rather than
"button silently vanishes everywhere".

This is a courtesy gate only. The server's check that the guid is
registered is what actually decides whether a suggestion is accepted.
"""

import re

_OBJECTID = re.compile(r"[0-9a-f]{24}")
DECK_NAME = "doctrine"


def is_doctrine_card(guid, deck_name) -> bool:
    # No guid means nothing to submit, whatever deck it sits in.
    if not isinstance(guid, str) or not guid:
        return False
    if _OBJECTID.fullmatch(guid):
        return True
    if isinstance(deck_name, str):
        top = deck_name.strip().split("::", 1)[0].strip().lower()
        if top == DECK_NAME:
            return True
    return False
