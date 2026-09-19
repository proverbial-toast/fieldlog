from __future__ import annotations

import sys
from pathlib import Path
import pytest

# Ensure local project is at the head of sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from fieldlog import recipes as recipes_mod  # noqa: E402 — needs the sys.path fix above


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "targets"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture(autouse=True)
def _no_operator_themes(tmp_path_factory, monkeypatch):
    """No test reads the developer's own `themes.yaml`.

    `load_catalog()` falls back to the real config path, so without this a
    theme switched off on this machine would quietly shrink the catalog every
    test builds — the same reason the drop-in directory is pointed elsewhere.
    A test that is about themes passes `themes_path=` explicitly.
    """
    monkeypatch.setattr(
        recipes_mod, "THEMES_PATH", tmp_path_factory.mktemp("no-themes") / "themes.yaml"
    )
