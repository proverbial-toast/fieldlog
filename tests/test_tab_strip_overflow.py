"""More tabs than the strip is wide: the labels stay on screen.

The strip is two rows, a top border and the labels. Its horizontal scrollbar
took the second row as soon as the tabs outgrew the width, so an operator with
a handful of finished runs saw a teal bar where every label should be. The bar
is gone; the strip scrolls to the active tab instead.
"""

from __future__ import annotations

from pathlib import Path

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession
from fieldlog.tui.models import TabDescriptor


def _app(workspace: Path) -> FieldlogApp:
    session = TargetSession(target="127.0.0.1", interface="eth0")
    session.workspace_dir = workspace
    return FieldlogApp(session)


def _label(app: FieldlogApp, tab_id: str):
    for item in app.query_one("#tabs-list").children:
        if item.tab.id == tab_id:
            return item.query_one(".tab-label").region
    raise AssertionError(tab_id)


async def test_an_overflowing_strip_keeps_its_labels_and_shows_the_active_tab(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(168, 45)) as pilot:
        for i in range(1, 7):
            app.tabs.append(TabDescriptor(f"job-{i}", f"ip/route #{i:02d}", "done", "ip", job_id=str(i)))
        app.active_tab_id = "job-6"
        app._refresh_tab_strip()
        await pilot.pause()
        await pilot.pause()

        strip = app.query_one("#tabs-scroll")
        assert strip.virtual_size.width > strip.content_region.width, "the tabs must overflow for this test"
        # No row goes to a scrollbar: the label row is inside the viewport.
        assert strip.scrollbar_size_horizontal == 0
        assert strip.content_region.contains_region(_label(app, "job-6"))

        # Back to the first tab: the strip scrolls back with it.
        app.action_select_tab("system")
        await pilot.pause()
        await pilot.pause()
        assert strip.scroll_x == 0
        assert strip.content_region.contains_region(_label(app, "system"))
