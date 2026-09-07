"""Assemble the trace part of a suggestion from plain values.

Kept free of Anki imports so the rules -- which template name applies,
when original_deck_id is meaningful -- are unit-tested; __init__.py only
gathers the raw values from the card and note.
"""


def trace(tags, note_mod, card_id, card_ord, template_names, deck_id,
          original_deck_id, install_id, addon_version, anki_version):
    names = list(template_names or [])
    if len(names) == 1:
        # Cloze note types have a single template; card.ord is the cloze
        # number, not an index into the template list.
        template = names[0]
    elif 0 <= (card_ord or 0) < len(names):
        template = names[card_ord]
    else:
        template = None

    return {
        "tags": list(tags or []),
        "note_mod": note_mod,
        "card_id": card_id,
        "card_ord": card_ord,
        "template_name": template,
        "deck_id": deck_id,
        # Anki sets odid only while a card sits in a filtered deck; 0
        # otherwise. Report the meaningful case and nothing else.
        "original_deck_id": original_deck_id or None,
        "install_id": install_id,
        "addon_version": addon_version,
        "anki_version": anki_version,
    }
