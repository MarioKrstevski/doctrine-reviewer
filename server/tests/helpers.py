"""Run the real server on an ephemeral port against a throwaway DB.

Each `running_server()` gets its own temp directory, so tests never share
state and never touch a developer's real doctrine.db.
"""

import contextlib
import tempfile
import threading
from pathlib import Path

import config
import server


@contextlib.contextmanager
def running_server(**env):
    """Yield the base URL of a freshly started server.

    Extra keyword arguments override environment values, e.g.
    `running_server(PUBLIC_BASE_URL="https://example.test")`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        settings = {"DB_PATH": str(Path(tmp) / "test.db"), "PORT": "0"}
        settings.update(env)

        previous = server.CFG
        server.CFG = config.load(settings)
        server.init_db()

        httpd = server.Server(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            server.CFG = previous
