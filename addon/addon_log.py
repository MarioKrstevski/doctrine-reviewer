"""Rotating file log for the add-on. Never raises.

Anki swallows exceptions inside hooks, so a broken hook looks exactly
like "the button isn't there". Every hook and network call is wrapped
and logged here, and the Diagnostics dialog shows the tail, so a tester
can paste evidence instead of describing a symptom.
"""

import logging
import logging.handlers
import os
import traceback

NAME = "doctrine_editor"
FILENAME = "doctrine.log"
_logger = None


def setup(directory):
    global _logger
    if _logger is not None:
        return _logger
    lg = logging.getLogger(NAME)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        os.makedirs(directory, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(
            os.path.join(directory, FILENAME), maxBytes=256 * 1024,
            backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h)
    except Exception:
        lg.addHandler(logging.NullHandler())
    _logger = lg
    return lg


def log():
    return _logger or logging.getLogger(NAME)


def exception(where):
    """Record the current exception with its traceback; never raise."""
    try:
        log().error("%s\n%s", where, traceback.format_exc())
    except Exception:
        pass


def tail(directory, lines=20):
    try:
        with open(os.path.join(directory, FILENAME), encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-lines:]
    except OSError:
        return []


def guarded(where):
    """Decorator: log and swallow, so a hook can never kill the button."""
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception:
                exception(where)
                return None
        wrapper.__name__ = getattr(fn, "__name__", "guarded")
        return wrapper
    return deco
