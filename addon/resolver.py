"""Decide which API base the add-on should POST to.

Pure and Anki-free on purpose: the clock and the HTTP fetcher are
injected, so this is unit-testable without Anki and without a network.

Resolution order:
  1. api_base_override from config (development escape hatch)
  2. a cached value fetched less than a day ago
  3. a fresh GET {bootstrap_url}/where
  4. the cached value, however old, if that fetch fails
  5. the bootstrap URL itself
"""

DAY = 86400


def _valid(base):
    return isinstance(base, str) and base.startswith(("http://", "https://"))


def resolve_api_base(cfg, cache, fetch, now):
    override = (cfg.get("api_base_override") or "").strip()
    if override:
        return override.rstrip("/")

    bootstrap = (cfg.get("bootstrap_url") or "").rstrip("/")
    cached = cache.get("api_base")
    fetched_at = cache.get("fetched_at", 0)

    if _valid(cached) and now() - fetched_at < DAY:
        return cached

    try:
        payload = fetch(bootstrap + "/where")
        base = (payload or {}).get("api_base")
        if _valid(base):
            base = base.rstrip("/")
            cache["api_base"] = base
            cache["fetched_at"] = now()
            return base
    except Exception:
        # Any failure to resolve must degrade to a usable URL rather than
        # raise into Anki's UI thread.
        pass

    if _valid(cached):
        return cached
    return bootstrap
