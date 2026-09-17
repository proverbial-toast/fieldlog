"""The palette says why a row cannot run, in the words is_blocked used.

Every blocked row used to read "needs dns name" as long as the binary was
installed — a missing target, an unsafe one and a missing local address alike.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession
from fieldlog.tui.modals import PaletteModal

TOOL = {
    "id": "true", "bin": "true", "name": "noop",
    "presets": [
        {"id": "aim", "name": "aim at the scope", "flags": "$TARGET"},
        {"id": "resolve", "name": "ask about the name", "flags": "$HOST"},
    ],
}


def _rows(tmp_workspace: Path, session: TargetSession):
    """The palette's task rows for a session, blocked ones included."""
    app = FieldlogApp(session)
    app._recipes = [TOOL]
    app.hide_missing = False              # a blocked row is the one under test
    modal = PaletteModal(app)
    modal.query_text = "true"
    _groups, flat = modal.get_groups()
    return modal, {row["preset"]["id"]: row for row in flat if row["kind"] == "task"}


def _label(modal: PaletteModal, row: dict) -> str:
    text, _cls = modal._render_item(row, False)
    return text.plain


def test_a_row_carries_the_reason_it_is_blocked(tmp_workspace: Path):
    modal, rows = _rows(tmp_workspace, TargetSession(workspace_dir=tmp_workspace))
    assert rows["aim"]["blocked"] is True
    assert rows["aim"]["reason"] == "needs a target"


@pytest.mark.parametrize(
    ("preset_id", "session_kwargs", "label"),
    [
        ("aim", {}, "needs target"),
        ("aim", {"target": "10.0.0.1;id"}, "bad target"),
        ("resolve", {}, "needs dns name"),
        ("resolve", {"hostname": "-x"}, "bad dns name"),
    ],
)
def test_the_row_says_which_scope_value_is_wrong(
    tmp_workspace: Path, preset_id: str, session_kwargs: dict, label: str
):
    modal, rows = _rows(
        tmp_workspace, TargetSession(workspace_dir=tmp_workspace, **session_kwargs)
    )
    assert label in _label(modal, rows[preset_id])


def test_a_runnable_row_says_nothing_about_the_scope(tmp_workspace: Path):
    modal, rows = _rows(
        tmp_workspace, TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    )
    assert rows["aim"]["blocked"] is False
    assert "needs" not in _label(modal, rows["aim"])
