"""Runtime configuration, read from the environment.

Pure: no I/O beyond reading a mapping. `load()` takes the mapping as an
argument so tests can pass a dict instead of mutating os.environ.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    public_base_url: str
    pipeline_api_key: str
    send_email: bool
    smtp_url: str


def _platform_url(env):
    """Public URL supplied by the hosting platform, if any.

    Railway injects RAILWAY_PUBLIC_DOMAIN as a bare hostname once a domain
    is generated, which lets the service know its own URL without a second
    deploy to set PUBLIC_BASE_URL by hand. Setting PUBLIC_BASE_URL
    explicitly always wins.
    """
    domain = (env.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if not domain:
        return None
    if domain.startswith(("http://", "https://")):
        return domain
    return "https://" + domain


def load(env=None) -> Config:
    env = os.environ if env is None else env
    host = env.get("HOST", "127.0.0.1")
    port = int(env.get("PORT", "8787"))
    public = env.get("PUBLIC_BASE_URL") or _platform_url(env) or f"http://{host}:{port}"
    return Config(
        host=host,
        port=port,
        db_path=env.get("DB_PATH", "doctrine.db"),
        public_base_url=public.rstrip("/"),
        pipeline_api_key=env.get("PIPELINE_API_KEY", "").strip(),
        send_email=(env.get("SEND_EMAIL", "") or "").strip().lower()
                   in ("1", "true", "yes", "on"),
        smtp_url=(env.get("SMTP_URL", "") or "").strip(),
    )
