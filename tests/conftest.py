from __future__ import annotations

import sys
from pathlib import Path
import pytest

# Ensure local project is at the head of sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "targets"
    ws.mkdir(parents=True, exist_ok=True)
    return ws
