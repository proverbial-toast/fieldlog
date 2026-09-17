"""The TUI's colours cost nothing to read.

theme.py held one catalog path, for a palette hint, and that import pulled yaml
and the whole recipe module into anything that only wanted a hex code. The hint
moved to the palette; this pins the import back out.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PURE = (
    "import sys, fieldlog.tui.theme; "
    "assert 'yaml' not in sys.modules; "
    "assert 'fieldlog.recipes' not in sys.modules"
)


def test_importing_the_theme_loads_neither_yaml_nor_the_catalog():
    # A fresh interpreter, because pytest has long since imported both.
    subprocess.run(
        [sys.executable, "-c", _PURE],
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        check=True,
    )
