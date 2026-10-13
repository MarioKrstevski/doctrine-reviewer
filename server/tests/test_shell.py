"""Public pages show no staff navigation; staff pages get the sidebar."""

import unittest

import server
from tests.helpers import running_server
from tests.test_auth_routes import login, make_user, request

STAFF_HREFS = ("/reviewer", "/releases", "/cards", "/outbox", "/admin/users", "/deck", "/detection")


class PublicShellTest(unittest.TestCase):
    def test_public_pages_link_only_updates_and_install(self):
        with running_server() as base:
            for path in ("/updates", "/install"):
                _, _, html = request(base, "GET", path)
                with self.subTest(path=path):
                    for href in STAFF_HREFS:
                        self.assertNotIn(f'href="{href}"', html)
                    self.assertIn('href="/updates"', html)
                    self.assertIn('href="/install"', html)

    def test_brand_fonts_and_tokens(self):
        html = server.page("x", "<p>y</p>")
        self.assertIn("Fraunces", html)
        self.assertIn("Plus+Jakarta+Sans", html)
        self.assertIn("#0F2A3D", html)


class StaffShellTest(unittest.TestCase):
    def test_staff_pages_have_the_sidebar_in_work_order(self):
        with running_server() as base:
            user, pw = make_user(); _, cookie = login(base, user, pw)
            _, _, html = request(base, "GET", "/reviewer", cookie=cookie)
        order = [html.find(f'href="{h}"') for h in ("/reviewer", "/cards", "/releases/next", "/releases", "/outbox")]
        self.assertTrue(all(i > 0 for i in order), order)
        self.assertEqual(sorted(order), order)
        self.assertIn('class="sidebar"', html)
        self.assertIn("Inbox", html)
