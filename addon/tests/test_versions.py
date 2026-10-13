import unittest

import versions


class VersionsTest(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(versions.newer("1.7.1", "1.7.0"))
        self.assertTrue(versions.newer("1.10.0", "1.9.9"))
        self.assertTrue(versions.newer("2.0", "1.9.9"))
        self.assertFalse(versions.newer("1.7.0", "1.7.0"))
        self.assertFalse(versions.newer("1.7", "1.7.0"))
        self.assertFalse(versions.newer("1.6.9", "1.7.0"))
        self.assertFalse(versions.newer("", "1.7.0"))
        self.assertFalse(versions.newer(None, "1.7.0"))


if __name__ == "__main__":
    unittest.main()
