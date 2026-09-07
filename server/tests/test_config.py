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

    def test_railway_domain_is_used_when_no_explicit_url(self):
        cfg = config.load({"RAILWAY_PUBLIC_DOMAIN": "app.up.railway.app"})
        self.assertEqual("https://app.up.railway.app", cfg.public_base_url)

    def test_explicit_url_beats_the_railway_domain(self):
        cfg = config.load({
            "RAILWAY_PUBLIC_DOMAIN": "app.up.railway.app",
            "PUBLIC_BASE_URL": "https://doctrine.example",
        })
        self.assertEqual("https://doctrine.example", cfg.public_base_url)

    def test_railway_domain_with_a_scheme_is_not_double_prefixed(self):
        cfg = config.load({"RAILWAY_PUBLIC_DOMAIN": "https://app.up.railway.app"})
        self.assertEqual("https://app.up.railway.app", cfg.public_base_url)


class DatabaseDirectoryTest(unittest.TestCase):
    """init_db() must not crash when the DB's parent directory is absent.

    A container whose volume has not attached yet has no /data, and SQLite
    cannot create a file in a directory that does not exist. Crash-looping
    there is worse than creating the directory and logging the path.
    """

    def test_init_db_creates_a_missing_parent_directory(self):
        import tempfile
        from pathlib import Path

        import config
        import server

        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "not" / "created" / "yet" / "doctrine.db"
            previous = server.CFG
            try:
                server.CFG = config.load({"DB_PATH": str(nested)})
                server.init_db()
                self.assertTrue(nested.exists(), "database file was not created")
            finally:
                server.CFG = previous
