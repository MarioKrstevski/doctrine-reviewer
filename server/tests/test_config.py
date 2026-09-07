import unittest

import config


class LoadTest(unittest.TestCase):
    def test_defaults_are_local(self):
        cfg = config.load({})
        self.assertEqual("127.0.0.1", cfg.host)
        self.assertEqual(8787, cfg.port)
        self.assertEqual("doctrine.db", cfg.db_path)
        self.assertEqual("http://127.0.0.1:8787", cfg.public_base_url)

    def test_port_is_an_int(self):
        self.assertEqual(9000, config.load({"PORT": "9000"}).port)

    def test_public_base_url_defaults_to_host_and_port(self):
        cfg = config.load({"HOST": "0.0.0.0", "PORT": "80"})
        self.assertEqual("http://0.0.0.0:80", cfg.public_base_url)

    def test_explicit_public_base_url_wins_and_loses_trailing_slash(self):
        cfg = config.load({"PUBLIC_BASE_URL": "https://example.test/"})
        self.assertEqual("https://example.test", cfg.public_base_url)
