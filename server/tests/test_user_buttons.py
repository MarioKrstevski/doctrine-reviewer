"""The user-management buttons, pressed the way the page renders them (throwaway database only)."""

import re
import unittest

import auth
import server
from tests.helpers import running_server
from tests.test_auth_routes import login, request
from tests.test_buttons_crawl import forms_of

PW = "adminpassword1"


def press(base, cookie, path, button, target_username=None, extra=None):
    """Find the rendered form whose button is `button` (optionally for one user's row) and submit it."""
    _, _, html = request(base, "GET", path, cookie=cookie)
    csrf = auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())
    rows = html.split("<tr")
    chunk = html
    if target_username:
        chunk = next(r for r in rows if f"<td>{target_username}</td>" in r)
    for form in forms_of("<form " + "<form ".join(chunk.split("<form ")[1:])):
        for name, value in form["buttons"]:
            if name and f"{name}={value}" == button:
                data = {k: (csrf if k == "csrf" else v) for k, v in form["fields"]}
                data[name] = value
                data.update(extra or {})
                return request(base, "POST", form["action"], data, cookie=cookie)
    raise AssertionError(f"no {button!r} button for {target_username} on {path}")


class UserButtonsTest(unittest.TestCase):
    def test_create_reset_deactivate_activate(self):
        with running_server() as base:
            server.create_user("boss", PW, "admin")
            server.create_user("second", PW, "admin")
            cookie = login(base, "boss", PW)[1]

            # Create: the new password is shown once, and it works
            _, _, html = request(base, "GET", "/admin/users", cookie=cookie)
            create = [f for f in forms_of(html) if ("do", "create") in f["fields"]][0]
            csrf = auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())
            status, _, html = request(base, "POST", create["action"],
                                      {"csrf": csrf, "do": "create", "username": "newbie"}, cookie=cookie)
            self.assertEqual(200, status)
            pw1 = re.search(r"<code>([^<]+)</code>", html).group(1)
            self.assertIn("Password for newbie", html)
            self.assertEqual(303, login(base, "newbie", pw1)[0])
            _, _, again = request(base, "GET", "/admin/users", cookie=cookie)
            self.assertNotIn(pw1, again, "the password is shown once")

            # Reset: a new password, the old one stops working
            _, _, html = press(base, cookie, "/admin/users", "do=reset", "newbie")
            pw2 = re.search(r"<code>([^<]+)</code>", html).group(1)
            self.assertNotEqual(pw1, pw2)
            self.assertNotEqual(303, login(base, "newbie", pw1)[0])
            self.assertEqual(303, login(base, "newbie", pw2)[0])

            # Deactivate: signed out and locked out, and the button becomes Activate
            live = login(base, "newbie", pw2)[1]
            status, _, html = press(base, cookie, "/admin/users", "do=deactivate", "newbie")
            self.assertIn("Deactivated newbie", html)
            self.assertEqual(303, request(base, "GET", "/reviewer", cookie=live)[0])   # session killed
            self.assertNotEqual(303, login(base, "newbie", pw2)[0], "a deactivated user cannot sign in")
            self.assertIn("do=activate", "".join(f"{n}={v}" for f in forms_of(html) for n, v in f["buttons"] if n))

            # Activate: they can sign in again
            _, _, html = press(base, cookie, "/admin/users", "do=activate", "newbie")
            self.assertIn("Reactivated newbie", html)
            self.assertEqual(303, login(base, "newbie", pw2)[0])

    def test_the_last_admin_cannot_be_deactivated_from_the_page(self):
        with running_server() as base:
            server.create_user("only", PW, "admin")
            cookie = login(base, "only", PW)[1]
            _, _, html = press(base, cookie, "/admin/users", "do=deactivate", "only")
            self.assertIn("only active administrator", html)
            self.assertEqual(200, request(base, "GET", "/admin/users", cookie=cookie)[0])

    def test_buyer_link_days_field_accepts_junk(self):
        with running_server() as base:
            server.create_user("boss", PW, "admin")
            cookie = login(base, "boss", PW)[1]
            csrf = auth.csrf_token(cookie.split("=", 1)[1], server.session_secret())
            for days in ("7 days", "abc", "", "-5", "9999999999999999999999", "0"):
                status, _, html = request(base, "POST", "/releases/token", {"csrf": csrf, "note": "b", "days": days}, cookie=cookie)
                self.assertEqual(200, status, days)
                self.assertIn("/dl/", html)


if __name__ == "__main__":
    unittest.main()
