"""Press every button.

Signs in as an administrator against a throwaway database, opens each staff page, finds
every form and submit button as rendered, and submits it. A button must never reach a
missing route (404), crash the server (5xx) or be refused for a bad form token. Nothing
here touches a real database: running_server() makes a fresh one in a temp folder.
"""

import json
import re
import unittest
import urllib.request
from html.parser import HTMLParser

import auth
import labels
import revisions
import server
from tests.helpers import register_note, running_server
from tests.test_auth_routes import login, make_user, request
from tests.test_release_routes import seed_baseline

A, B, C = "guidAAAAAA", "guidBBBBBB", "guidCCCCCC"
ADMIN_PW = "crawlpassword1"


class _Forms(HTMLParser):
    """Collects forms with their fields and submit buttons."""

    def __init__(self):
        super().__init__()
        self.forms, self.cur, self.select, self.area = [], None, None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.cur = {"method": (a.get("method") or "get").lower(), "action": a.get("action") or "",
                        "fields": [], "buttons": [], "file": False}
            self.forms.append(self.cur)
        elif self.cur is None:
            return
        elif tag == "input":
            t = (a.get("type") or "text").lower()
            name = a.get("name")
            if t == "file":
                self.cur["file"] = True
            elif t in ("submit", "button"):
                if name and t == "submit":
                    self.cur["buttons"].append((name, a.get("value") or ""))
            elif name:
                if t in ("checkbox", "radio") and "checked" not in a:
                    return
                default = {"password": "Abcdefgh1234", "text": "x1"}.get(t, a.get("value") or "")
                value = a.get("value") if t in ("hidden", "checkbox", "radio") else default
                self.cur["fields"].append((name, value if value is not None else ""))
        elif tag == "button":
            if (a.get("type") or "submit") == "submit" and a.get("name"):
                self.cur["buttons"].append((a["name"], a.get("value") or ""))
            elif (a.get("type") or "submit") == "submit":
                self.cur["buttons"].append((None, None))
        elif tag == "textarea" and a.get("name"):
            self.cur["fields"].append((a["name"], "some text"))
        elif tag == "select" and a.get("name"):
            self.select = a["name"]
        elif tag == "option" and self.select:
            self.cur["fields"].append((self.select, a.get("value") or ""))
            self.select = None

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur = None


def forms_of(html):
    p = _Forms()
    p.feed(html)
    return [f for f in p.forms if f["method"] == "post" and not f["file"] and f["action"] and f["action"] != "/logout"]


def seed(base):
    """Enough state that every page has buttons: reports, a proposal, a label, a draft release, a link, a thank-you."""
    seed_baseline()
    for g, t in ((A, "alpha card text"), (B, "beta card text"), (C, "gamma card text")):
        register_note(base, g, fields={"Text": t, "Extra": "e"})

    def suggest(guid, text, email=None, proposed=None):
        body = {"doctrine_id": guid, "guid": guid, "text": text, "email": email, "suggestion_type": "typo",
                "snapshot": {"fields": {"Text": {A: "alpha card text", B: "beta card text", C: "gamma card text"}[guid], "Extra": "e"}}}
        if proposed:
            body["proposed_fields"] = proposed
        req = urllib.request.Request(base + "/api/suggestions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
    suggest(A, "first report on alpha", "a@example.org", {"Text": "alpha card text fixed"})
    suggest(A, "second report on alpha", None)
    suggest(B, "report on beta", "b@example.org")
    suggest(C, "report on gamma", None)
    with server.db() as conn:
        labels.set_card_labels(conn, C, ["ask anna"], 1, "t")          # gamma becomes triaged
        server.accept_proposal(conn, "1", 1)                            # queues a thank-you, pends an edit on A
        revisions.apply_edit(conn, B, {"Text": "beta card text edited"}, 1, "staff")
    # a pending re-upload review: one changed card, one new card, one card missing from the file
    server.create_upload("crawl.apkg", [
        {"guid": A, "fields": {"Text": "alpha card text from the file", "Extra": "e"}, "tags": ["t"]},
        {"guid": "guidNEW0001", "fields": {"Text": "a brand new card", "Extra": ""}, "tags": []},
        {"guid": B, "fields": {"Text": "beta card text", "Extra": "e"}, "tags": []}])
    return None


class ButtonCrawlTest(unittest.TestCase):
    PAGES = ["/reviewer", "/cards?q=card", "/cards/edit?guid=" + B, "/edit/3", "/releases/next", "/releases",
             "/releases/1.1", "/uploads/1", "/deck", "/outbox", "/admin/users", "/account", "/detection", "/addon", "/notes/new"]

    def _session(self, base):
        ok, msg = server.create_user("crawler", ADMIN_PW, "admin")
        make = server.create_user("second_admin", ADMIN_PW, "admin")
        return login(base, "crawler", ADMIN_PW)[1]

    def test_every_rendered_button_is_wired_up(self):
        self._crawl(None)

    def test_every_button_survives_blank_values(self):
        self._crawl("")

    def test_every_button_survives_hostile_values(self):
        self._crawl("<b>'\"; DROP TABLE users;-- 99999999999999999999 -1 \x00 %s{0}")

    def _crawl(self, override):
        problems, pressed = [], 0
        for page_path in self.PAGES:
            with running_server() as base:
                seed(base)
                server.create_user("a_reviewer", "reviewerpassword1", "reviewer")
                cookie = self._session(base)
                # a draft release and a download link so Releases has their buttons
                with server.db() as conn:
                    first = conn.execute("SELECT id FROM users WHERE username='crawler'").fetchone()["id"]
                server.cut_release("1.1", first, [B])
                server.mint_download_token("Buyer", 7, first)
                server.add_deck("Scratch Deck")
                status, _, html = request(base, "GET", page_path, cookie=cookie)
                if status != 200:
                    problems.append(f"GET {page_path} -> {status}")
                    continue
                forms = forms_of(html)
                for idx, form in enumerate(forms):
                    for (bname, bvalue) in (form["buttons"] or [(None, None)]):
                        # a fresh session per press keeps one destructive button from hiding the next
                        current = login(base, "crawler", ADMIN_PW)[1] or cookie
                        csrf = auth.csrf_token(current.split("=", 1)[1], server.session_secret())
                        data = {k: (csrf if k == "csrf" else (v if override is None else override))
                                for k, v in form["fields"]}
                        if bname:
                            data[bname] = bvalue
                        label = f'{page_path} form {idx} {form["action"]} {bname}={bvalue}'
                        try:
                            status, _, body = request(base, "POST", form["action"], data, cookie=current)
                        except Exception as exc:   # the server dropped the connection: an unhandled error
                            problems.append(f"{label} -> {type(exc).__name__}")
                            continue
                        pressed += 1
                        if status == 404 or status >= 500:
                            problems.append(f"{label} -> {status}")
                        elif status == 403 and "Invalid form token" in body:
                            problems.append(f"{label} -> bad token")
        self.assertGreater(pressed, 30, "the crawler found too few buttons to be meaningful")
        self.assertEqual([], problems)


if __name__ == "__main__":
    unittest.main()
