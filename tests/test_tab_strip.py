"""The System tab takes the room its name needs, and no more.

Reported as: "the actual tab `|   [System]   |` seems wider than it needs to
be." It was 16 cells for a six-letter word. Four of them were decoration: the
brackets, which said "this is a tab" to something that is already drawn as one,
and a near-invisible `·` holding the place of a close button this tab does not
have. The × missing is what says a tab cannot be closed.

Job tabs are untouched — they are as wide as `tool/preset #NN` plus their ×.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession
from fieldlog.tui.models import TabDescriptor
from fieldlog.tui.widgets import TabClose, TabItem

# ` ▪  System  ` — icon cell, the word, and one space either side of it.
SYSTEM_TAB_CELLS = 12


def _app(workspace: Path) -> FieldlogApp:
    session = TargetSession(target="10.0.0.1", interface="eth0")
    session.workspace_dir = workspace
    return FieldlogApp(session)


def _item(app: FieldlogApp, tab_id: str) -> TabItem:
    return next(i for i in app.query(TabItem) if i.tab.id == tab_id)


async def test_the_system_tab_is_as_wide_as_its_name(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()                      # a width exists only after layout
        item = _item(app, "system")

        assert item.tab.label == "System"
        assert item.outer_size.width == SYSTEM_TAB_CELLS


async def test_the_system_tab_spends_nothing_on_a_close_button_it_has_not_got(
    tmp_workspace: Path,
):
    """The `·` placeholder is gone, not restyled: it cost two cells to say
    what the absent × says for free."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        item = _item(app, "system")

        assert item.closable is False
        assert not item.query(TabClose)
        assert not item.query(".tab-close")
        assert not item.query(".tab-locked"), "the spacer came back"


async def test_a_job_tab_still_carries_its_close_button(tmp_workspace: Path):
    """Narrowing System must not narrow the tabs that do close."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.tabs.append(TabDescriptor("job-1", "ping/sweep #01", "done", "ping", job_id="1"))
        app._refresh_tab_strip()
        await pilot.pause()

        item = _item(app, "job-1")
        assert item.closable is True
        assert len(item.query(TabClose)) == 1
        # icon (2) + label with its padding (2 + 14) + × (2) + the item's own (2)
        assert item.outer_size.width == 22


async def test_a_refresh_still_repaints_the_system_tab(tmp_workspace: Path):
    """`update_tab` walks icon, label, then close. Skipping the close for a tab
    that has none keeps that walk from ending early on an expected miss — the
    kind of thing the surrounding `except` would have swallowed in silence."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        item = _item(app, "system")
        app.tabs.append(TabDescriptor("job-1", "ping/sweep #01", "active", "ping", job_id="1"))
        app._refresh_tab_strip()
        await pilot.pause()
        app.action_select_tab("job-1")            # System is no longer the active tab
        await pilot.pause()

        assert item.is_active is False
        assert str(item.query_one(".tab-label").content) == "System"
        assert item.outer_size.width == SYSTEM_TAB_CELLS


@pytest.mark.parametrize(
    "tab_id, expected", [("system", "System"), ("job-1", "ping/sweep #01")]
)
async def test_the_copied_log_is_headed_with_the_tab_it_came_from(
    tmp_workspace: Path, tab_id, expected, monkeypatch
):
    """Ctrl+Shift+C stamps the clipboard with which tab the lines came from.

    The System branch spelled `[System]` by hand — a second copy of the label
    that this rename would have left behind, disagreeing with the tab itself.
    It reads `tab.label` now, like the branch beside it.
    """
    from fieldlog.tui import jobs as jobs_mod

    copied: list[str] = []
    monkeypatch.setattr(jobs_mod, "copy_text_to_clipboard", lambda text, app=None: copied.append(text))

    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.tabs.append(TabDescriptor("job-1", "ping/sweep #01", "done", "ping", job_id="1"))
        app._refresh_tab_strip()
        app.action_select_tab(tab_id)
        await pilot.pause()

        app.action_copy_log()

        assert copied, "nothing reached the clipboard"
        header = copied[0].splitlines()[0]
        assert f" · {expected} · " in header
        assert "[System]" not in header
