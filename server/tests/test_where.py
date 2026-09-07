import json
import unittest
import urllib.request

from tests.helpers import running_server


class WhereTest(unittest.TestCase):
    def test_reports_the_public_base_url(self):
        with running_server(PUBLIC_BASE_URL="https://doctrine.example") as base:
            with urllib.request.urlopen(base + "/where") as r:
                self.assertEqual(200, r.status)
                self.assertEqual("application/json", r.headers["Content-Type"])
                body = json.loads(r.read())
        self.assertEqual("https://doctrine.example", body["api_base"])
        self.assertIn("min_addon_version", body)

    def test_is_cacheable(self):
        with running_server() as base:
            with urllib.request.urlopen(base + "/where") as r:
                self.assertIn("max-age", r.headers.get("Cache-Control", ""))
