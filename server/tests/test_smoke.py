import unittest
import urllib.error
import urllib.request

from tests.helpers import running_server


class SmokeTest(unittest.TestCase):
    def test_updates_page_is_reachable(self):
        with running_server() as base:
            with urllib.request.urlopen(base + "/updates") as r:
                self.assertEqual(200, r.status)

    def test_unknown_path_is_404(self):
        with running_server() as base:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/nope")
            self.assertEqual(404, ctx.exception.code)
