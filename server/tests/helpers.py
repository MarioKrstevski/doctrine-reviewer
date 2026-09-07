"""Run the real server on an ephemeral port against a throwaway DB.

Each `running_server()` gets its own temp directory, so tests never share
state and never touch a developer's real doctrine.db.
"""

import contextlib
import tempfile
import threading
from pathlib import Path

import json
import urllib.request

import config
import server

# Registering master state needs the pipeline key; tests that only read
# do not care, but setting it by default keeps fixtures simple.
TEST_PIPELINE_KEY = "test-pipeline-key"


@contextlib.contextmanager
def running_server(**env):
    """Yield the base URL of a freshly started server.

    Extra keyword arguments override environment values, e.g.
    `running_server(PUBLIC_BASE_URL="https://example.test")`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        settings = {"DB_PATH": str(Path(tmp) / "test.db"), "PORT": "0",
                    "PIPELINE_API_KEY": TEST_PIPELINE_KEY}
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


def register_note(base, doctrine_id="doc-test000001", fields=None,
                  key=TEST_PIPELINE_KEY):
    """Register one master note the way the pipeline would."""
    payload = {"notes": [{
        "doctrine_id": doctrine_id,
        "anki_note_id": 1,
        "note_type": "Basic",
        "deck": "Test",
        "fields": fields or {"Front": "Q", "Back": "A"},
        "question_html": "Q",
        "answer_html": "A",
        "css": "",
    }]}
    req = urllib.request.Request(
        base + "/api/master/sync",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())
