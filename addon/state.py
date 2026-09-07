"""Persistent add-on state, kept out of the visible config.

Anki renders config.json in the add-on's Config panel, so anything stored
there is shown to the student and is editable by them. The resolved API
base cache and the install id are internal, so they live in `user_files/`
instead -- the directory Anki preserves across add-on updates.

Every operation degrades quietly: a missing, unreadable or corrupt state
file reads as empty, and a failed write is swallowed. Losing the cache
costs one extra bootstrap request; raising here would surface as an error
in the middle of a student sending a suggestion.
"""

import json
import os
import uuid

FILENAME = "state.json"


def _path(directory):
    return os.path.join(directory, FILENAME)


def load(directory):
    try:
        with open(_path(directory), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(directory, data):
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = _path(directory) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, _path(directory))
    except (OSError, ValueError, TypeError):
        pass


def install_id(directory):
    """A random per-installation id, generated once and then stable.

    Used later for abuse protection: one open suggestion per install per
    card, and a per-install daily rate limit. Not tied to any identity.
    """
    data = load(directory)
    existing = data.get("install_id")
    if isinstance(existing, str) and existing:
        return existing
    value = uuid.uuid4().hex
    data["install_id"] = value
    save(directory, data)
    return value
