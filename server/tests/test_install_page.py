"""Public install page and add-on download served by the platform itself.

The student build is packaged at container build time from addon/ (never
the DEV build, which carries the pipeline key), so 'where do I get the
add-on' has one answer: the platform.
"""

import io
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path

import server
from tests.helpers import running_server


class InstallPageTest(unittest.TestCase):
    def test_install_page_is_public_and_links_the_download(self):
        with running_server() as base:
            with urllib.request.urlopen(base + "/install") as r:
                self.assertEqual(200, r.status)
                html = r.read().decode()
        self.assertIn("/download/doctrine_editor.ankiaddon", html)
        self.assertIn("Install from file", html)
        self.assertIn("Testing the full loop", html)
        self.assertIn("Known gaps", html)

    def test_download_serves_a_valid_addon_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "doctrine_editor.ankiaddon"
            server.build_student_package(Path(__file__).resolve().parents[2] / "addon", pkg)
            previous = server.ADDON_PACKAGE_PATH
            server.ADDON_PACKAGE_PATH = str(pkg)
            try:
                with running_server() as base:
                    with urllib.request.urlopen(base + "/download/doctrine_editor.ankiaddon") as r:
                        self.assertEqual(200, r.status)
                        self.assertIn("application/octet-stream", r.headers["Content-Type"])
                        self.assertIn("doctrine_editor.ankiaddon", r.headers.get("Content-Disposition", ""))
                        data = r.read()
            finally:
                server.ADDON_PACKAGE_PATH = previous
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        self.assertIn("__init__.py", names)
        self.assertIn("manifest.json", names)
        self.assertFalse(any(n.startswith("tests/") for n in names))
        src = zipfile.ZipFile(io.BytesIO(data)).read("__init__.py").decode()
        self.assertIn('DEV_MODE = False', src)
        self.assertIn('PIPELINE_API_KEY = ""', src, "a served package must never carry the key")

    def test_missing_package_is_a_clear_404_not_a_crash(self):
        previous = server.ADDON_PACKAGE_PATH
        server.ADDON_PACKAGE_PATH = "/nonexistent/x.ankiaddon"
        try:
            with running_server() as base:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(base + "/download/doctrine_editor.ankiaddon")
                self.assertEqual(404, ctx.exception.code)
        finally:
            server.ADDON_PACKAGE_PATH = previous
