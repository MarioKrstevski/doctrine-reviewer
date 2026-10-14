"""Junk in, no crash out: API bodies and URL parameters that no real client would send.

A handler that raises closes the connection without a reply, and a 5xx is a bug too.
Both count as failures here. Everything runs against a throwaway database.
"""

import http.client
import json
import unittest
import urllib.parse

import server
from tests.helpers import TEST_PIPELINE_KEY, register_note, running_server
from tests.test_auth_routes import login, make_user, request
from tests.test_buttons_crawl import seed

JUNK_BODIES = [
    b"", b"{", b"null", b"[]", b"123", b'"text"', b'{"notes": "not a list"}', b'{"notes": [1, 2, 3]}',
    b'{"notes": [{"doctrine_id": null}]}', b'{"text": ["x"], "snapshot": 5}', b'{"doctrine_id": {"a": 1}}',
    json.dumps({"doctrine_id": "guidAAAAAA", "text": "x" * 200000, "snapshot": {"fields": {"Text": "y" * 200000}}}).encode(),
    json.dumps({"doctrine_id": "guidAAAAAA", "text": "t", "proposed_fields": ["not", "a", "dict"]}).encode(),
    json.dumps({"doctrine_id": "guidAAAAAA", "text": "t", "snapshot": {"fields": "str"}}).encode(),
    json.dumps({"install_id": 123, "license": ["x"], "release": {"a": 1}}).encode(),
    "{\"text\": \"\u0000‮😀\"}".encode(),
]

POST_API = ["/api/suggestions", "/api/ping", "/api/master/sync", "/api/dev/register", "/api/reupload"]


def raw(base, method, path, body=None, headers=None):
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        r.read()
        return r.status
    finally:
        conn.close()


class ApiJunkTest(unittest.TestCase):
    def test_api_endpoints_never_crash_on_junk_bodies(self):
        with running_server() as base:
            seed(base)
            problems = []
            for path in POST_API:
                for headers in ({"Content-Type": "application/json"},
                                {"Content-Type": "application/json", "Authorization": f"Bearer {TEST_PIPELINE_KEY}"}):
                    for body in JUNK_BODIES:
                        try:
                            status = raw(base, "POST", path, body, headers)
                        except Exception as exc:
                            problems.append(f"{path} {body[:30]!r} -> {type(exc).__name__}")
                            continue
                        if status >= 500:
                            problems.append(f"{path} {body[:30]!r} -> {status}")
            self.assertEqual([], problems)

    def test_baseline_and_media_endpoints_reject_junk_cleanly(self):
        with running_server() as base:
            auth = {"Authorization": f"Bearer {TEST_PIPELINE_KEY}"}
            problems = []
            for method, path, body in [
                    ("POST", "/api/baseline?version=1.0&note_count=abc", b"not sqlite"),
                    ("POST", "/api/baseline?version=../x&note_count=1", b"SQLite format 3\x00" + b"x" * 50),
                    ("POST", "/api/baseline", b""),
                    ("POST", "/api/media/..%2F..%2Fetc%2Fpasswd", b"x"),
                    ("POST", "/api/media/%00", b"x"),
                    ("POST", "/api/media/" + "a" * 400, b"x"),
                    ("GET", "/api/media/manifest", None)]:
                try:
                    status = raw(base, method, path, body, auth)
                except Exception as exc:
                    problems.append(f"{method} {path} -> {type(exc).__name__}")
                    continue
                if status >= 500:
                    problems.append(f"{method} {path} -> {status}")
            self.assertEqual([], problems)


class BrowserUploadJunkTest(unittest.TestCase):
    def test_deck_upload_endpoint_rejects_bad_requests_cleanly(self):
        import auth
        with running_server() as base:
            seed(base)
            server.create_user("uploader", "uploadpassword1", "admin")
            server.create_user("plain", "plainpassword1", "reviewer")
            cookie = login(base, "uploader", "uploadpassword1")[1]
            csrf = auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())
            plain = login(base, "plain", "plainpassword1")[1]
            plain_csrf = auth.csrf_token(plain.split("=", 1)[1], server.session_secret())

            def post(headers, body=b"", declared=None):
                parsed = urllib.parse.urlparse(base)
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
                h = dict(headers)
                h["Content-Length"] = str(len(body) if declared is None else declared)
                conn.request("POST", "/deck/upload?name=x.apkg", body=body, headers=h)
                r = conn.getresponse(); r.read(); conn.close()
                return r.status
            self.assertEqual(403, post({}))                                             # signed out
            self.assertEqual(403, post({"Cookie": plain, "X-CSRF": plain_csrf}, b"x"))   # reviewer
            self.assertEqual(403, post({"Cookie": cookie, "X-CSRF": "wrong"}, b"x"))     # bad token
            self.assertEqual(400, post({"Cookie": cookie, "X-CSRF": csrf}, b"", 0))     # empty
            self.assertEqual(400, post({"Cookie": cookie, "X-CSRF": csrf}, b"", 10 ** 13))  # absurd size
            # a file that is not an .apkg is accepted for reading, then reported as failed, not crashed
            self.assertEqual(200, post({"Cookie": cookie, "X-CSRF": csrf}, b"this is not a zip file"))
            import time
            for _ in range(100):
                with server.db() as conn:
                    st = conn.execute("SELECT status FROM uploads ORDER BY id DESC LIMIT 1").fetchone()[0]
                if st != "processing":
                    break
                time.sleep(0.1)
            self.assertEqual("failed", st)


class UrlJunkTest(unittest.TestCase):
    PATHS = [
        "/s/", "/s/nope", "/s/%00", "/dl/", "/dl/nope", "/dl/%00" , "/media/", "/media/..%2F..%2Fetc%2Fpasswd", "/media/%00",
        "/api/sync", "/api/sync?since=%00&deck=%00", "/api/sync?since=" + "9" * 300, "/api/sync/version?deck=%27", "/api/official?deck=%27",
        "/where?deck=%27", "/updates", "/install", "/login", "/download/release/", "/download/release/zzz", "/download/doctrine_editor.ankiaddon",
    ]
    STAFF_PATHS = [
        "/edit/abc", "/edit/999999999999999999999", "/edit/", "/edit/%00", "/cards/edit", "/cards/edit?guid=", "/cards/edit?guid=%00",
        "/cards/edit?guid=" + "z" * 400, "/cards?q=%00", "/cards?q=%25", "/cards?q=%27%22%3B--", "/cards?q=" + "q" * 5000,
        "/cards?q=label:", "/cards?q=tag:", "/cards?q=nid:", "/cards?q=%23", "/cards?q=x&n=abc", "/cards?q=x&n=-5", "/cards?q=x&n=99999999999",
        "/reviewer?label=%00&label=%27", "/releases/next?label=%00&label=" + "l" * 300, "/releases/", "/releases/%00", "/releases/..%2F..",
        "/releases/zzz", "/releases/zzz/confirm", "/releases/%00/confirm", "/releases/status", "/uploads/abc", "/uploads/99999999999999999999",
        "/uploads/%00", "/deck/status/abc", "/deck/status/%00", "/deck?x=%00", "/outbox?x=%00", "/detection", "/addon", "/admin/users",
        "/account", "/notes/new", "/s/zzz", "/nonexistent", "/%00", "/" + "a" * 3000,
    ]

    def test_public_urls_never_crash(self):
        with running_server() as base:
            seed(base)
            problems = []
            for path in self.PATHS:
                try:
                    status = raw(base, "GET", path)
                except Exception as exc:
                    problems.append(f"{path} -> {type(exc).__name__}")
                    continue
                if status >= 500:
                    problems.append(f"{path} -> {status}")
            self.assertEqual([], problems)

    def test_staff_urls_never_crash(self):
        with running_server() as base:
            seed(base)
            server.create_user("fuzzer", "fuzzpassword1", "admin")
            cookie = login(base, "fuzzer", "fuzzpassword1")[1]
            problems = []
            for path in self.STAFF_PATHS:
                try:
                    status = raw(base, "GET", path, None, {"Cookie": cookie})
                except Exception as exc:
                    problems.append(f"{path[:80]} -> {type(exc).__name__}")
                    continue
                if status >= 500:
                    problems.append(f"{path[:80]} -> {status}")
            self.assertEqual([], problems)


if __name__ == "__main__":
    unittest.main()
