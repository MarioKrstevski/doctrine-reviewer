"""Cached set of official note guids, fetched from the platform.

Pure: the HTTP call and the clock are injected. State lives in
user_files/ next to the API-base cache. Any failure keeps the previous set;
having no set at all means "unknown", and the button is shown so the
server can decide.
"""

import json
import os
from collections import namedtuple

FILENAME = "official.json"
MIN_INTERVAL = 6 * 3600   # seconds between refresh attempts

Official = namedtuple("Official", "guids release etag fetched_at")


def _path(directory):
    return os.path.join(directory, FILENAME)


def load(directory):
    try:
        with open(_path(directory), encoding="utf-8") as fh:
            data = json.load(fh)
        guids = data["guids"]
        if not isinstance(guids, list):
            return None
        return Official(set(guids), data.get("release", ""), data.get("etag", ""),
                        float(data.get("fetched_at", 0)))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save(directory, guids, release, etag, fetched_at):
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = _path(directory) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"guids": sorted(guids), "release": release, "etag": etag,
                       "fetched_at": fetched_at}, fh)
        os.replace(tmp, _path(directory))
    except (OSError, ValueError, TypeError):
        pass


def refresh(directory, api_base, fetch, now, force=False, deck=""):
    """fetch(url, headers) -> (status, headers_dict, body_bytes). Never raises."""
    current = load(directory)
    if current and not force and now() - current.fetched_at < MIN_INTERVAL:
        return current
    headers = {"Accept": "application/json"}
    if current and current.etag:
        headers["If-None-Match"] = current.etag
    try:
        status, resp_headers, body = fetch(api_base.rstrip("/") + "/api/official" + (f"?deck={deck}" if deck else ""), headers)
        if status == 304 and current:
            _save(directory, current.guids, current.release, current.etag, now())
            return current
        if status != 200:
            return current
        data = json.loads(body.decode("utf-8"))
        guids = data.get("guids")
        if not isinstance(guids, list) or not all(isinstance(g, str) for g in guids):
            return current
        etag = ""
        for k, v in (resp_headers or {}).items():
            if k.lower() == "etag":
                etag = v
        fresh = Official(set(guids), str(data.get("release", "")), etag, now())
        _save(directory, fresh.guids, fresh.release, fresh.etag, fresh.fetched_at)
        return fresh
    except Exception:
        return current


def is_official(guid, guids) -> bool:
    if not isinstance(guid, str) or not guid:
        return False
    if guids is None:
        return True   # never fetched: let the server decide
    return guid in guids
