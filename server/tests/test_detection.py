"""Installs report a license id; Detection lists licenses seen on many installs."""

import hashlib
import json
import unittest
import urllib.error
import urllib.request

import server
from tests.helpers import running_server
from tests.test_release_routes import staff
from tests.test_auth_routes import request


def ping(base, **data):
    req = urllib.request.Request(base + "/api/ping", data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class PingTest(unittest.TestCase):
    def test_ping_upserts_and_rejects_junk(self):
        with running_server() as base:
            self.assertEqual(200, ping(base, install_id="install-aaaa", addon_version="1.6.3", release="1.0", license="00000000000000a1"))
            self.assertEqual(200, ping(base, install_id="install-aaaa", addon_version="1.6.3", release="1.1"))
            self.assertEqual(400, ping(base, install_id="x"))
            with server.db() as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM installs")]
        self.assertEqual(1, len(rows))
        self.assertEqual(("00000000000000a1", "1.1", 2), (rows[0]["license"], rows[0]["release"], rows[0]["pings"]))


class DetectionPageTest(unittest.TestCase):
    def test_levels_notes_and_access(self):
        with running_server() as base:
            token = "buyer-token-abcdefghijklmnop"
            lic = hashlib.sha256(token.encode()).hexdigest()[:16]
            with server.db() as conn:
                conn.execute("INSERT INTO download_tokens (token, note, created_by, created_at, expires_at) VALUES (?,?,?,?,?)",
                             (token, "Buyer Anna", 1, server.now(), "2099-01-01 00:00 UTC"))
            for i in range(21):
                ping(base, install_id=f"anna-install-{i:03d}", license=lic)
            for i in range(2):
                ping(base, install_id=f"bob-install-{i:03d}", license="otherlicense1234")
            ping(base, install_id="no-license-001")
            admin = staff(base, "admin")
            _, _, html = request(base, "GET", "/detection", cookie=admin)
            rev_status = request(base, "GET", "/detection", cookie=staff(base, "reviewer"))[0]
        self.assertIn("Buyer Anna", html)
        self.assertIn("Watch", html)
        self.assertNotIn("Red flag</span>", html)
        self.assertIn("24</b> installs seen", html)
        self.assertEqual(403, rev_status)


if __name__ == "__main__":
    unittest.main()
