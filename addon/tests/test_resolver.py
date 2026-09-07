import unittest

from resolver import DAY, resolve_api_base

BOOTSTRAP = "https://bootstrap.example"
LIVE = "https://api.example"


def never_called(url):
    raise AssertionError(f"should not have fetched {url}")


class ResolveTest(unittest.TestCase):
    def test_override_wins_and_skips_the_network(self):
        cache = {}
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP, "api_base_override": LIVE},
            cache, fetch=never_called, now=lambda: 0,
        )
        self.assertEqual(LIVE, base)
        self.assertEqual({}, cache, "override must not populate the cache")

    def test_fresh_cache_skips_the_network(self):
        cache = {"api_base": LIVE, "fetched_at": 100}
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, cache,
            fetch=never_called, now=lambda: 100 + DAY - 1,
        )
        self.assertEqual(LIVE, base)

    def test_stale_cache_refetches_and_updates(self):
        cache = {"api_base": "https://old.example", "fetched_at": 0}
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, cache,
            fetch=lambda url: {"api_base": LIVE}, now=lambda: DAY + 1,
        )
        self.assertEqual(LIVE, base)
        self.assertEqual(LIVE, cache["api_base"])
        self.assertEqual(DAY + 1, cache["fetched_at"])

    def test_fetch_failure_falls_back_to_stale_cache(self):
        cache = {"api_base": LIVE, "fetched_at": 0}

        def boom(url):
            raise OSError("network down")

        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, cache,
            fetch=boom, now=lambda: DAY * 365,
        )
        self.assertEqual(LIVE, base, "a stale cache beats no service at all")

    def test_fetch_failure_with_no_cache_falls_back_to_bootstrap(self):
        def boom(url):
            raise OSError("network down")

        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, {}, fetch=boom, now=lambda: 0,
        )
        self.assertEqual(BOOTSTRAP, base)

    def test_it_fetches_the_where_path(self):
        seen = []
        resolve_api_base(
            {"bootstrap_url": BOOTSTRAP + "/"}, {},
            fetch=lambda url: seen.append(url) or {"api_base": LIVE},
            now=lambda: 0,
        )
        self.assertEqual([BOOTSTRAP + "/where"], seen)

    def test_a_junk_response_falls_back_rather_than_returning_junk(self):
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, {},
            fetch=lambda url: {"unexpected": "shape"}, now=lambda: 0,
        )
        self.assertEqual(BOOTSTRAP, base)

    def test_a_non_http_scheme_is_rejected(self):
        base = resolve_api_base(
            {"bootstrap_url": BOOTSTRAP}, {},
            fetch=lambda url: {"api_base": "file:///etc/passwd"}, now=lambda: 0,
        )
        self.assertEqual(BOOTSTRAP, base)
