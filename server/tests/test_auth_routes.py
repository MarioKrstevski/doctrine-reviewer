"""HTTP-level tests for login, guards and the public/private boundary."""

import http.client
import json
import unittest
import urllib.parse

import auth
import server
from tests.helpers import running_server


def request(base, method, path, body=None, cookie=None, follow=False):
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    headers = {}
    payload = None
    if body is not None:
        payload = urllib.parse.urlencode(body)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    result = (resp.status, dict(resp.getheaders()), data)
    conn.close()
    return result


def session_cookie(headers):
    raw = headers.get("Set-Cookie", "")
    return raw.split(";")[0] if raw else None


def make_user(username="rev", password="hunter2hunter2", role="reviewer"):
    ok, msg = server.create_user(username, password, role)
    assert ok, msg
    return username, password


def login(base, username, password):
    status, headers, _ = request(base, "POST", "/login",
                                 {"username": username, "password": password})
    return status, session_cookie(headers)


class PublicRoutesStayPublicTest(unittest.TestCase):
    def test_public_routes_need_no_cookie(self):
        with running_server() as base:
            for path in ["/updates", "/where"]:
                with self.subTest(path=path):
                    status, _, _ = request(base, "GET", path)
                    self.assertEqual(200, status)

    def test_the_suggestion_api_needs_no_cookie(self):
        with running_server() as base:
            parsed = urllib.parse.urlparse(base)
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
            conn.request("POST", "/api/dev/register",
                         body=json.dumps({"notes": [{
                             "doctrine_id": "doc-pub0000001", "anki_note_id": 1,
                             "note_type": "Basic", "deck": "D",
                             "fields": {"Front": "Q"}, "question_html": "Q",
                             "answer_html": "A", "css": ""}]}),
                         headers={"Content-Type": "application/json"})
            self.assertEqual(200, conn.getresponse().status)
            conn.close()

            conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
            conn.request("POST", "/api/suggestions",
                         body=json.dumps({"doctrine_id": "doc-pub0000001",
                                          "text": "anon", "snapshot": {}}),
                         headers={"Content-Type": "application/json"})
            self.assertEqual(200, conn.getresponse().status)
            conn.close()


class GuardTest(unittest.TestCase):
    def test_reviewer_redirects_to_login_when_logged_out(self):
        with running_server() as base:
            status, headers, _ = request(base, "GET", "/reviewer")
            self.assertEqual(303, status)
            self.assertIn("/login", headers.get("Location", ""))

    def test_root_redirects_to_login_when_logged_out(self):
        with running_server() as base:
            status, _, _ = request(base, "GET", "/")
            self.assertEqual(303, status)

    def test_a_valid_login_reaches_the_queue(self):
        with running_server() as base:
            user, pw = make_user()
            status, cookie = login(base, user, pw)
            self.assertEqual(303, status)
            self.assertIsNotNone(cookie, "no session cookie was set")
            status, _, _ = request(base, "GET", "/reviewer", cookie=cookie)
            self.assertEqual(200, status)

    def test_a_bad_password_does_not_authenticate(self):
        with running_server() as base:
            user, _ = make_user()
            status, cookie = login(base, user, "wrong-password")
            self.assertIsNone(cookie)
            self.assertEqual(200, status)

    def test_an_unknown_user_does_not_authenticate(self):
        with running_server() as base:
            _, cookie = login(base, "nobody", "whatever")
            self.assertIsNone(cookie)

    def test_a_deactivated_user_cannot_log_in(self):
        with running_server() as base:
            user, pw = make_user()
            with server.db() as conn:
                uid = conn.execute("SELECT id FROM users WHERE username=?",
                                   (user,)).fetchone()["id"]
            server.set_active(uid, False)
            _, cookie = login(base, user, pw)
            self.assertIsNone(cookie)

    def test_deactivation_kills_a_live_session_immediately(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            self.assertEqual(200, request(base, "GET", "/reviewer", cookie=cookie)[0])

            with server.db() as conn:
                uid = conn.execute("SELECT id FROM users WHERE username=?",
                                   (user,)).fetchone()["id"]
            server.set_active(uid, False)

            status, _, _ = request(base, "GET", "/reviewer", cookie=cookie)
            self.assertEqual(303, status, "a deactivated user kept access")

    def test_a_password_reset_kills_live_sessions(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            with server.db() as conn:
                uid = conn.execute("SELECT id FROM users WHERE username=?",
                                   (user,)).fetchone()["id"]
            server.set_password(uid, "a-brand-new-password")
            status, _, _ = request(base, "GET", "/reviewer", cookie=cookie)
            self.assertEqual(303, status, "old session survived a password reset")

    def test_logout_invalidates_the_cookie(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            csrf = auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())
            request(base, "POST", "/logout", {"csrf": csrf}, cookie=cookie)
            status, _, _ = request(base, "GET", "/reviewer", cookie=cookie)
            self.assertEqual(303, status)

    def test_a_forged_cookie_is_rejected(self):
        with running_server() as base:
            make_user()
            status, _, _ = request(base, "GET", "/reviewer",
                                   cookie="doctrine_session=not-a-real-token")
            self.assertEqual(303, status)

    def test_the_login_page_renders(self):
        with running_server() as base:
            status, _, body = request(base, "GET", "/login")
            self.assertEqual(200, status)
            self.assertIn("password", body.lower())


class ThrottleRouteTest(unittest.TestCase):
    def test_repeated_failures_lock_the_account_out(self):
        with running_server() as base:
            user, pw = make_user()
            for _ in range(auth.THROTTLE_MAX_ATTEMPTS):
                login(base, user, "wrong")
            _, cookie = login(base, user, pw)
            self.assertIsNone(cookie, "correct password accepted while throttled")


class CsrfRouteTest(unittest.TestCase):
    def _open_suggestion(self, base):
        import urllib.request
        for path, payload in [
            ("/api/dev/register", {"notes": [{
                "doctrine_id": "doc-csrf000001", "anki_note_id": 1,
                "note_type": "Basic", "deck": "D", "fields": {"Front": "Q"},
                "question_html": "Q", "answer_html": "A", "css": ""}]}),
            ("/api/suggestions", {"doctrine_id": "doc-csrf000001",
                                  "text": "csrf test", "snapshot": {}}),
        ]:
            req = urllib.request.Request(
                base + path, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
        with server.db() as conn:
            return conn.execute(
                "SELECT id FROM suggestions ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

    def test_an_action_without_a_csrf_token_is_refused(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = self._open_suggestion(base)

            status, _, _ = request(base, "POST", "/reviewer/action",
                                   {"id": sid, "do": "resolve"}, cookie=cookie)
            self.assertEqual(403, status)

            with server.db() as conn:
                row = conn.execute("SELECT status FROM suggestions WHERE id=?",
                                   (sid,)).fetchone()
            self.assertEqual("open", row["status"],
                             "a CSRF-less POST changed the suggestion anyway")

    def test_a_csrf_token_from_another_session_is_refused(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = self._open_suggestion(base)
            forged = auth.csrf_token("some-other-session", server.session_secret())

            status, _, _ = request(base, "POST", "/reviewer/action",
                                   {"id": sid, "do": "resolve", "csrf": forged},
                                   cookie=cookie)
            self.assertEqual(403, status)

    def test_a_valid_action_records_who_did_it(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            sid = self._open_suggestion(base)
            csrf = auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())

            status, _, _ = request(base, "POST", "/reviewer/action",
                                   {"id": sid, "do": "resolve", "csrf": csrf},
                                   cookie=cookie)
            self.assertEqual(303, status)

            with server.db() as conn:
                row = conn.execute(
                    "SELECT s.status, u.username FROM suggestions s"
                    " LEFT JOIN users u ON u.id = s.resolved_by WHERE s.id=?",
                    (sid,)).fetchone()
            self.assertEqual("resolved", row["status"])
            self.assertEqual(user, row["username"], "resolved_by was not recorded")

    def test_an_anonymous_action_is_refused(self):
        with running_server() as base:
            sid = self._open_suggestion(base)
            status, headers, _ = request(base, "POST", "/reviewer/action",
                                         {"id": sid, "do": "resolve"})
            self.assertEqual(303, status)
            self.assertIn("/login", headers.get("Location", ""))
            with server.db() as conn:
                row = conn.execute("SELECT status FROM suggestions WHERE id=?",
                                   (sid,)).fetchone()
            self.assertEqual("open", row["status"])


def admin(base, username="boss"):
    make_user(username, "adminpassword1", "admin")
    return login(base, username, "adminpassword1")


def csrf_for(cookie):
    return auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())


class AdminAccessTest(unittest.TestCase):
    def test_a_reviewer_cannot_reach_the_admin_page(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            status, _, _ = request(base, "GET", "/admin/users", cookie=cookie)
            self.assertEqual(403, status)

    def test_an_admin_can_reach_the_admin_page(self):
        with running_server() as base:
            _, cookie = admin(base)
            status, _, body = request(base, "GET", "/admin/users", cookie=cookie)
            self.assertEqual(200, status)
            self.assertIn("boss", body)

    def test_logged_out_admin_page_redirects(self):
        with running_server() as base:
            status, _, _ = request(base, "GET", "/admin/users")
            self.assertEqual(303, status)

    def test_a_reviewer_cannot_create_users(self):
        with running_server() as base:
            user, pw = make_user()
            _, cookie = login(base, user, pw)
            status, _, _ = request(base, "POST", "/admin/users",
                                   {"do": "create", "username": "sneaky",
                                    "csrf": csrf_for(cookie)}, cookie=cookie)
            self.assertEqual(403, status)
            with server.db() as conn:
                self.assertIsNone(conn.execute(
                    "SELECT id FROM users WHERE username='sneaky'").fetchone())

    def test_an_admin_creates_a_reviewer_and_sees_the_password_once(self):
        with running_server() as base:
            _, cookie = admin(base)
            status, _, body = request(base, "POST", "/admin/users",
                                      {"do": "create", "username": "newbie",
                                       "csrf": csrf_for(cookie)}, cookie=cookie)
            self.assertEqual(200, status)
            with server.db() as conn:
                row = conn.execute(
                    "SELECT role, active FROM users WHERE username='newbie'"
                ).fetchone()
            self.assertIsNotNone(row, "user was not created")
            self.assertEqual("reviewer", row["role"])
            self.assertIn("newbie", body)

    def test_creating_a_duplicate_username_is_refused(self):
        with running_server() as base:
            _, cookie = admin(base)
            make_user("taken", "somepassword12")
            request(base, "POST", "/admin/users",
                    {"do": "create", "username": "taken", "csrf": csrf_for(cookie)},
                    cookie=cookie)
            with server.db() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) c FROM users WHERE username='taken'"
                ).fetchone()["c"]
            self.assertEqual(1, count)

    def test_an_admin_reset_kills_the_target_session(self):
        with running_server() as base:
            _, admin_cookie = admin(base)
            user, pw = make_user("victim", "victimpassword1")
            _, victim_cookie = login(base, "victim", "victimpassword1")
            self.assertEqual(200, request(base, "GET", "/reviewer",
                                          cookie=victim_cookie)[0])

            with server.db() as conn:
                uid = conn.execute("SELECT id FROM users WHERE username='victim'"
                                   ).fetchone()["id"]
            request(base, "POST", "/admin/users",
                    {"do": "reset", "user_id": uid, "csrf": csrf_for(admin_cookie)},
                    cookie=admin_cookie)

            self.assertEqual(303, request(base, "GET", "/reviewer",
                                          cookie=victim_cookie)[0])

    def test_an_admin_cannot_deactivate_the_last_admin(self):
        with running_server() as base:
            _, cookie = admin(base)
            with server.db() as conn:
                uid = conn.execute("SELECT id FROM users WHERE username='boss'"
                                   ).fetchone()["id"]
            request(base, "POST", "/admin/users",
                    {"do": "deactivate", "user_id": uid, "csrf": csrf_for(cookie)},
                    cookie=cookie)
            with server.db() as conn:
                still = conn.execute("SELECT active FROM users WHERE id=?",
                                     (uid,)).fetchone()["active"]
            self.assertEqual(1, still, "locked out the only admin")

    def test_deactivating_a_user_keeps_their_history(self):
        with running_server() as base:
            _, cookie = admin(base)
            make_user("leaver", "leaverpassword1")
            with server.db() as conn:
                uid = conn.execute("SELECT id FROM users WHERE username='leaver'"
                                   ).fetchone()["id"]
            request(base, "POST", "/admin/users",
                    {"do": "deactivate", "user_id": uid, "csrf": csrf_for(cookie)},
                    cookie=cookie)
            with server.db() as conn:
                row = conn.execute("SELECT active FROM users WHERE id=?",
                                   (uid,)).fetchone()
            self.assertIsNotNone(row, "the user row was deleted, losing history")
            self.assertEqual(0, row["active"])


class AccountTest(unittest.TestCase):
    def test_a_user_changes_their_own_password(self):
        with running_server() as base:
            user, pw = make_user("selfserve", "oldpassword123")
            _, cookie = login(base, "selfserve", "oldpassword123")
            status, _, _ = request(base, "POST", "/account",
                                   {"current": "oldpassword123",
                                    "new": "newpassword456",
                                    "confirm": "newpassword456",
                                    "csrf": csrf_for(cookie)}, cookie=cookie)
            self.assertIn(status, (200, 303))
            self.assertIsNone(login(base, "selfserve", "oldpassword123")[1])
            self.assertIsNotNone(login(base, "selfserve", "newpassword456")[1])

    def test_the_current_password_is_required(self):
        with running_server() as base:
            make_user("careful", "realpassword123")
            _, cookie = login(base, "careful", "realpassword123")
            request(base, "POST", "/account",
                    {"current": "wrongpassword", "new": "newpassword456",
                     "confirm": "newpassword456", "csrf": csrf_for(cookie)},
                    cookie=cookie)
            self.assertIsNotNone(login(base, "careful", "realpassword123")[1],
                                 "password changed without the current one")

    def test_mismatched_confirmation_is_refused(self):
        with running_server() as base:
            make_user("typo", "realpassword123")
            _, cookie = login(base, "typo", "realpassword123")
            request(base, "POST", "/account",
                    {"current": "realpassword123", "new": "newpassword456",
                     "confirm": "different999", "csrf": csrf_for(cookie)},
                    cookie=cookie)
            self.assertIsNotNone(login(base, "typo", "realpassword123")[1])

    def test_account_page_requires_login(self):
        with running_server() as base:
            self.assertEqual(303, request(base, "GET", "/account")[0])
