"""Local sync state: which release this collection is at, when we last asked.

Lives in user_files/sync.json. A missing or corrupt file reads as "unknown
release", which makes the server send everything since the first release
and the plan skip whatever already matches.
"""

import json
import os

FILENAME = "sync.json"
CHECK_INTERVAL = 12 * 3600


def _path(d):
    return os.path.join(d, FILENAME)


def load(d):
    try:
        with open(_path(d), encoding="utf-8") as fh:
            data = json.load(fh)
        return {"release": str(data.get("release", "") or ""), "last_check": float(data.get("last_check", 0) or 0)}
    except (OSError, ValueError, TypeError):
        return {"release": "", "last_check": 0.0}


def save(d, release, last_check):
    try:
        os.makedirs(d, exist_ok=True)
        tmp = _path(d) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"release": release, "last_check": last_check}, fh)
        os.replace(tmp, _path(d))
    except (OSError, ValueError, TypeError):
        pass


def due(state, now, force=False):
    return force or (now - state.get("last_check", 0)) >= CHECK_INTERVAL
