"""Daily usage ping: install id, add-on version, deck release, license id.

The license id is the hidden `_doctrine_license.json` that ships inside the
deck package a buyer downloads. It lets the platform notice one download
being installed in very many places. It never blocks anything. Pure and
Anki-free so it can be tested.
"""

import json
import os
import time

LICENSE_FILE = "_doctrine_license.json"
INTERVAL = 24 * 3600
STATE_FILE = "ping.json"


def read_license(media_dir):
    """The license id from the collection's media folder, or ""."""
    try:
        with open(os.path.join(media_dir, LICENSE_FILE), encoding="utf-8") as fh:
            return str(json.load(fh).get("license", "") or "")[:64]
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def due(directory, now=None, force=False):
    now = time.time() if now is None else now
    try:
        with open(os.path.join(directory, STATE_FILE), encoding="utf-8") as fh:
            last = float(json.load(fh).get("last", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        last = 0.0
    return force or now - last >= INTERVAL


def mark_sent(directory, now=None):
    try:
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, STATE_FILE), "w", encoding="utf-8") as fh:
            json.dump({"last": time.time() if now is None else now}, fh)
    except OSError:
        pass


def payload(install_id, addon_version, release, media_dir, deck=""):
    out = {"install_id": install_id, "addon_version": addon_version, "release": release or ""}
    if deck:
        out["deck"] = deck
    lic = read_license(media_dir)
    if lic:
        out["license"] = lic
    return out
