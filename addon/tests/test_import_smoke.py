"""Import the add-on package with Anki stubbed out.

Anki reports any exception at import time as 'Add-on Startup Failed' with
no traceback shown to the user. This catches that class of failure in CI.
"""

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ADDON_DIR = Path(__file__).resolve().parents[1]


def _stub_anki():
    aqt = types.ModuleType("aqt")
    mw = mock.MagicMock()
    mw.addonManager.getConfig.return_value = {}
    aqt.mw = mw
    aqt.gui_hooks = mock.MagicMock()
    qt = types.ModuleType("aqt.qt")
    for name in ("QApplication", "QDialog", "QVBoxLayout", "QHBoxLayout", "QLabel",
                 "QComboBox", "QTextEdit", "QLineEdit", "QPushButton",
                 "QInputDialog", "Qt", "QMenu", "QAction"):
        setattr(qt, name, mock.MagicMock(name=name))
    utils = types.ModuleType("aqt.utils")
    for name in ("tooltip", "showInfo", "showWarning", "openLink"):
        setattr(utils, name, mock.MagicMock(name=name))
    reviewer = types.ModuleType("aqt.reviewer"); reviewer.Reviewer = type("Reviewer", (), {})
    anki = types.ModuleType("anki"); bi = types.ModuleType("anki.buildinfo"); bi.version = "25.09.5"
    return {"aqt": aqt, "aqt.qt": qt, "aqt.utils": utils, "aqt.reviewer": reviewer,
            "anki": anki, "anki.buildinfo": bi}


class ImportSmokeTest(unittest.TestCase):
    def test_the_package_imports_under_anki(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_parent = Path(tmp)
            (pkg_parent / "doctrine_editor").symlink_to(ADDON_DIR)
            with mock.patch.dict(sys.modules, _stub_anki()):
                sys.path.insert(0, str(pkg_parent))
                try:
                    for m in [m for m in sys.modules if m.startswith("doctrine_editor")]:
                        del sys.modules[m]
                    mod = importlib.import_module("doctrine_editor")
                finally:
                    sys.path.remove(str(pkg_parent))
        for name in ("open_suggestion_dialog", "on_reviewer_did_show_question",
                     "on_js_message", "open_diagnostics", "register_deck", "setup_menu"):
            self.assertTrue(callable(getattr(mod, name, None)), name)
        # filter hook contract: returns `handled` untouched when not ours
        self.assertEqual("h", mod.on_js_message("h", "something_else", None))
