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


def load(env=None) -> Config:
    env = os.environ if env is None else env
    host = env.get("HOST", "127.0.0.1")
    port = int(env.get("PORT", "8787"))
    public = env.get("PUBLIC_BASE_URL") or f"http://{host}:{port}"
    return Config(
        host=host,
        port=port,
        db_path=env.get("DB_PATH", "doctrine.db"),
        public_base_url=public.rstrip("/"),
    )
