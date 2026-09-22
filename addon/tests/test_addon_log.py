import os
import tempfile
import unittest

import addon_log


class LogTest(unittest.TestCase):
    def setUp(self):
        addon_log._logger = None

    def test_setup_creates_the_file_and_tail_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lg = addon_log.setup(tmp)
            lg.info("hello %s", "world")
            self.assertTrue(os.path.exists(os.path.join(tmp, addon_log.FILENAME)))
            self.assertIn("hello world", "".join(addon_log.tail(tmp)))

    def test_guarded_swallows_and_logs_the_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            addon_log.setup(tmp)

            @addon_log.guarded("boom-site")
            def boom():
                raise ValueError("kaboom")

            self.assertIsNone(boom())
            text = "".join(addon_log.tail(tmp, 50))
            self.assertIn("boom-site", text)
            self.assertIn("ValueError: kaboom", text)

    def test_guarded_passes_return_values_through(self):
        @addon_log.guarded("ok")
        def fine(x):
            return x * 2
        self.assertEqual(4, fine(2))

    def test_unwritable_directory_does_not_raise(self):
        addon_log.setup("/proc/nope/cannot")
        addon_log.log().info("still fine")
        self.assertEqual([], addon_log.tail("/proc/nope/cannot"))
