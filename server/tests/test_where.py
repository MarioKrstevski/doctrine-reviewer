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


class AddonVersionTest(unittest.TestCase):
    def test_where_reports_the_packaged_addon_version(self):
        import json, os, tempfile, urllib.request, zipfile
        import server
        from tests.helpers import running_server
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.ankiaddon")
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("__init__.py", 'X = 1\nADDON_VERSION = "9.8.7"\n')
            old = server.ADDON_PACKAGE_PATH
            server.ADDON_PACKAGE_PATH = p
            try:
                with running_server() as base:
                    with urllib.request.urlopen(base + "/where") as r:
                        info = json.loads(r.read())
            finally:
                server.ADDON_PACKAGE_PATH = old
        self.assertEqual("9.8.7", info["addon_version"])
