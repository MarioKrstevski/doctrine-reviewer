#!/usr/bin/env python3
"""Submit a deck .apkg for re-upload review: the platform compares it with
the online deck and gives back a review link. Nothing is applied here.

    PIPELINE_API_KEY=... python3 tools/reupload_apkg.py "Doctorine v2.1.apkg"
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apkg  # noqa: E402
from import_apkg import read_notes  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("apkg")
    ap.add_argument("--server", default="https://doctrine-editor-production.up.railway.app")
    args = ap.parse_args()
    key = os.environ.get("PIPELINE_API_KEY", "").strip()
    if not key:
        sys.exit("PIPELINE_API_KEY is not set")
    pkg = apkg.Package(args.apkg)
    notes = list(read_notes(pkg.collection_sqlite()))
    pkg.close()
    body = json.dumps({"label": os.path.basename(args.apkg), "notes": notes}).encode("utf-8")
    print(f"submitting {len(notes)} notes ({len(body)/1e6:.1f} MB)...")
    req = urllib.request.Request(args.server.rstrip("/") + "/api/reupload", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"refused: {json.loads(e.read().decode('utf-8')).get('error')}")
    s = out["summary"]
    print(f"unchanged {s['unchanged']} | changed {s['changed']} | conflicts {s['conflicts']} | "
          f"new {s['new']} | missing {s['missing']} | tag changes {s['tag_changes']} | "
          f"other-field changes {s['other_field_changes']}")
    print("review and apply here:", out["review_url"])


if __name__ == "__main__":
    main()
