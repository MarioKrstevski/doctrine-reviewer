import json
import os
import tempfile
import unittest

import ping


class PingTest(unittest.TestCase):
    def test_reads_license_from_media_folder(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual("", ping.read_license(d))
            with open(os.path.join(d, ping.LICENSE_FILE), "w") as fh:
                json.dump({"license": "abc123", "release": "1.0"}, fh)
            self.assertEqual("abc123", ping.read_license(d))
            p = ping.payload("install-1", "1.7.0", "1.0", d)
        self.assertEqual({"install_id": "install-1", "addon_version": "1.7.0",
                          "release": "1.0", "license": "abc123"}, p)

    def test_corrupt_license_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, ping.LICENSE_FILE), "w").write("not json")
            self.assertNotIn("license", ping.payload("i", "1", "", d))

    def test_daily_gate(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(ping.due(d, now=10**6))
            ping.mark_sent(d, now=10**6)
            self.assertFalse(ping.due(d, now=10**6 + 3600))
            self.assertTrue(ping.due(d, now=10**6 + 25 * 3600))
            self.assertTrue(ping.due(d, now=10**6 + 1, force=True))


if __name__ == "__main__":
    unittest.main()
