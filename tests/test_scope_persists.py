"""A scope set in the [T] modal reaches disk when it is set.

`.last-scope.json` used to be written only at unmount, so a crash or a killed
terminal lost the target the operator had just typed and the next bare
`fieldlog` came up on the old one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import Input

from fieldlog.app import FieldlogApp, TargetModal
from fieldlog.state import TargetSession


@pytest.mark.asyncio
async def test_saving_the_scope_form_caches_it_before_unmount(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        modal = TargetModal(app.session)
        app.push_screen(modal)
        await pilot.pause()
        modal.query_one("#in-target", Input).value = "10.0.0.9"
        modal.action_save()
        await pilot.pause()

        saved = tmp_workspace / ".last-scope.json"
        assert saved.exists()                    # while the app is still running
        assert json.loads(saved.read_text())["target"] == "10.0.0.9"
