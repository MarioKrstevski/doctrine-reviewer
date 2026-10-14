"""Tag counts and the Anki-style tag tree for one deck. No HTTP.

Anki nests tags with "::" (Doctorine::Shelf::Micro::Viruses). A node's count is
the number of notes carrying that tag or any tag below it, counted once per note,
the way Anki's browser sidebar finds cards. Built from `notes.tags_json` and
cached per deck until a note in it changes.
"""

import json
import threading

_lock = threading.Lock()
_cache = {}


def _key(conn, deck_id):
    r = conn.execute("SELECT COUNT(*), MAX(updated_at), SUM(version) FROM notes "
                     "WHERE COALESCE(platform_deck,1)=? AND retired=0", (deck_id,)).fetchone()
    return (r[0], r[1], r[2])


def build(rows):
    """rows: iterable of tags_json strings. Returns {"tags": {tag: n}, "nodes": {path: n}}.

    `nodes` keys are lower-cased paths (Anki tags are case-insensitive), and
    `names` keeps the first spelling seen for display."""
    tags, nodes, names = {}, {}, {}
    for raw in rows:
        try:
            note_tags = json.loads(raw or "[]")
        except ValueError:
            continue
        seen_tags, seen_nodes = set(), set()
        for t in note_tags:
            if not isinstance(t, str) or not t.strip():
                continue
            low = t.lower()
            if low not in seen_tags:
                seen_tags.add(low)
                names.setdefault(low, t)
                tags[low] = tags.get(low, 0) + 1
            parts = [p for p in t.split("::")]
            for i in range(1, len(parts) + 1):
                path = "::".join(parts[:i])
                lp = path.lower()
                names.setdefault(lp, path)
                if lp not in seen_nodes:
                    seen_nodes.add(lp)
                    nodes[lp] = nodes.get(lp, 0) + 1
    return {"tags": tags, "nodes": nodes, "names": names}


def index(conn, deck_id):
    key = _key(conn, deck_id)
    with _lock:
        hit = _cache.get(deck_id)
        if hit and hit[0] == key:
            return hit[1]
    rows = (r[0] for r in conn.execute(
        "SELECT tags_json FROM notes WHERE COALESCE(platform_deck,1)=? AND retired=0", (deck_id,)))
    data = build(rows)
    with _lock:
        _cache[deck_id] = (key, data)
    return data


def children(data, prefix=""):
    """Direct children of `prefix` ("" for the top level), sorted by name:
    [{"path", "name", "count", "has_children"}]."""
    nodes, names = data["nodes"], data["names"]
    lp = prefix.lower()
    depth = 0 if not prefix else lp.count("::") + 1
    out = []
    for path, n in nodes.items():
        if path.count("::") != depth:
            continue
        if prefix and not path.startswith(lp + "::"):
            continue
        out.append(path)
    kids_of = set()
    for path in nodes:
        if path.count("::") == depth + 1:
            kids_of.add(path.rsplit("::", 1)[0])
    shown = []
    for path in sorted(out, key=lambda p: names[p].lower()):
        full = names[path]
        shown.append({"path": full, "name": full.rsplit("::", 1)[-1], "count": nodes[path],
                      "has_children": path in kids_of})
    return shown


def all_tags(data):
    """Every full tag with its note count, most used first."""
    return sorted(((data["names"][t], n) for t, n in data["tags"].items()), key=lambda x: (-x[1], x[0].lower()))


def search(data, q, limit=60):
    """Tree paths whose name contains `q`, most notes first."""
    q = (q or "").strip().lower()
    if not q:
        return []
    hits = [(p, n) for p, n in data["nodes"].items() if q in p]
    hits.sort(key=lambda x: (-x[1], x[0]))
    return [{"path": data["names"][p], "count": n} for p, n in hits[:limit]]


def sql_match(tag):
    """(sql, args) matching notes with a tag that contains `tag` as whole parts:
    tag:Yield::Step1 finds Doctorine::Yield::Step1 and Doctorine::Yield::Step1::Gold,
    but not Yield::Step10. Like Anki, "*" stands for any text (tag:*Gold)."""
    t = (tag or "").strip()
    body = _like(json.dumps(t)[1:-1]).replace("*", "%")
    patterns = [f'%"{body}"%', f'%"{body}::%', f'%::{body}"%', f'%::{body}::%']
    return ("(" + " OR ".join("tags_json LIKE ? ESCAPE '\\'" for _ in patterns) + ")", tuple(patterns))


def _like(s):
    """Escape LIKE wildcards: tags often contain "_" (Internal_Medicine)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
