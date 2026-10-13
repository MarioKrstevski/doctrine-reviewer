"""Labels: names you invent to group cards, for triage and for preparing releases.

A label belongs to a card (a note), not to one report, so reports, edits and uploads
on that card all share it. Labels are per deck. A label with no cards is removed.
No HTTP here.
"""

import re

MAX_LEN = 40
# brand-friendly pairs (background, text): blue, teal, amber, rose, violet, slate
PALETTE = [("#E4F1FC", "#14324A"), ("#DDF3F3", "#17595C"), ("#F6EDD7", "#6B4400"),
           ("#F6E3E1", "#7A2626"), ("#EAE6F7", "#3F3480"), ("#E7ECF0", "#2F4757")]


def normalize(name):
    """Lowercase, collapse spaces, trim, cap the length. Empty means invalid."""
    text = re.sub(r"\s+", " ", (name or "").strip().lower())
    return text[:MAX_LEN].strip()


def color_for(name):
    return PALETTE[sum(ord(c) for c in name) % len(PALETTE)]


def parse_new(text):
    """'ask anna, needs review' -> ['ask anna', 'needs review']"""
    seen, out = set(), []
    for part in re.split(r"[,\n]", text or ""):
        n = normalize(part)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def labels_for(conn, guids):
    """{guid: [label names]} for exactly these cards."""
    out = {g: [] for g in guids}
    guids = list(guids)
    for start in range(0, len(guids), 500):
        chunk = guids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT cl.guid, l.name FROM card_labels cl JOIN labels l ON l.id=cl.label_id "
                f"WHERE cl.guid IN ({marks}) ORDER BY l.name", chunk):
            out[r["guid"]].append(r["name"])
    return out


def all_labels(conn, deck_id=1):
    """[{name, cards}] for a deck, most used first."""
    return [dict(r) for r in conn.execute(
        "SELECT l.name, COUNT(cl.guid) AS cards FROM labels l LEFT JOIN card_labels cl ON cl.label_id=l.id "
        "WHERE l.platform_deck=? GROUP BY l.id ORDER BY cards DESC, l.name", (deck_id,))]


def guids_with(conn, names, deck_id=1):
    """Guids of cards that carry any of these labels."""
    names = [normalize(n) for n in names if normalize(n)]
    if not names:
        return set()
    marks = ",".join("?" * len(names))
    return {r["guid"] for r in conn.execute(
        f"SELECT cl.guid FROM card_labels cl JOIN labels l ON l.id=cl.label_id "
        f"WHERE l.platform_deck=? AND l.name IN ({marks})", (deck_id, *names))}


def set_card_labels(conn, guid, names, user_id, now, deck_id=1):
    """Replace a card's labels with exactly `names`, creating labels as needed."""
    wanted = []
    for n in names:
        n = normalize(n)
        if n and n not in wanted:
            wanted.append(n)
    conn.execute("DELETE FROM card_labels WHERE guid=?", (guid,))
    for n in wanted:
        conn.execute("INSERT OR IGNORE INTO labels (platform_deck, name, created_at) VALUES (?,?,?)", (deck_id, n, now))
        lid = conn.execute("SELECT id FROM labels WHERE platform_deck=? AND name=?", (deck_id, n)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO card_labels (guid, label_id, added_by, added_at) VALUES (?,?,?,?)",
                     (guid, lid, user_id, now))
    cleanup(conn)
    return wanted


def cleanup(conn):
    """Remove labels that no card uses any more."""
    conn.execute("DELETE FROM labels WHERE id NOT IN (SELECT DISTINCT label_id FROM card_labels)")
