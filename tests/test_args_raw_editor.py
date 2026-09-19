"""The raw ARGS editor is as tall as the command it is editing.

`#args-raw-area` was `height: 3`, which is a border and one row of text. Every
template longer than one line — a soft-wrapped command, or the multi-line one
the editor's own hint invites — was edited through a one-row porthole, and the
band collapsed from nine rows to six on the way in, so the pane it opened over
jumped as well. `python-server/https` is the first shipped template long enough
to show it.

The cap is deliberate: the editor may take the room `#args-body-row` can spare
once the `✓ done` line below it has its own, and scrolls past that rather than
eating the panes above.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession
from fieldlog.tui.widgets import ArgsTextArea

# The shipped `python-server/https` template, which is one folded YAML line and
# wraps to four or five rows at the widths below.
LONG = (
    "-c \"import http.server,ssl,signal,sys; "
    "signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); "
    "print('Serving HTTPS on :8443', flush=True); "
    "ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); "
    "ctx.load_cert_chain('$OUTDIR/cert.pem', '$OUTDIR/key.pem'); "
    "srv = http.server.HTTPServer(('0.0.0.0',8443), http.server.SimpleHTTPRequestHandler); "
    "srv.socket = ctx.wrap_socket(srv.socket, server_side=True); srv.serve_forever()\""
)
SHORT = "-c 4 $TARGET"

# What `#args-raw-area`'s max-height leaves for text, once its border has two.
MAX_TEXT_ROWS = 5


def _app(workspace: Path, flags: str) -> FieldlogApp:
    session = TargetSession(target="10.0.0.1")
    session.workspace_dir = workspace
    app = FieldlogApp(session)
    tools = [{"id": "t", "bin": "true", "name": "T", "presets": [{"id": "p", "name": "p", "flags": flags}]}]
    app.catalog.tools = tools
    app.catalog.chains = []
    app._recipes = tools
    return app


async def _wrapped_height(pilot, area: ArgsTextArea) -> int:
    """The editor's wrapped height, measured once its width has settled.

    A TextArea wraps against a width it does not have until layout has run, so
    reading `wrapped_document.height` straight after the mode switch can catch
    it at 1 no matter how long the command is. That is a race, not a result: it
    passed here and on five CI legs and failed on the sixth. Waiting for two
    consecutive equal non-zero widths is what makes the measurement mean
    something.
    """
    width = -1
    for _ in range(20):
        await pilot.pause()
        if area.size.width and area.size.width == width:
            break
        width = area.size.width
    assert area.size.width, "the editor never got a width to wrap against"
    return area.wrapped_document.height


@pytest.mark.parametrize("size", [(140, 45), (120, 40), (80, 30)], ids=["wide", "split", "narrow"])
async def test_a_wrapped_command_is_edited_in_more_than_one_row(tmp_workspace: Path, size):
    """Every row the command wraps to is on screen, up to the cap."""
    app = _app(tmp_workspace, LONG)
    async with app.run_test(size=size) as pilot:
        app.action_toggle_args_mode(force_raw=True)
        area = app.query_one("#args-raw-area", ArgsTextArea)
        wrapped = await _wrapped_height(pilot, area)

        assert wrapped > 1, "the fixture no longer wraps; it cannot prove anything"
        assert area.scrollable_content_region.height == min(wrapped, MAX_TEXT_ROWS)


async def test_a_command_too_tall_for_the_band_scrolls_rather_than_grows(tmp_workspace: Path):
    """A narrow console wraps the same command to nine rows. The editor takes
    its five and scrolls; the band must not climb over the panes to fit it."""
    app = _app(tmp_workspace, LONG)
    async with app.run_test(size=(80, 30)) as pilot:
        app.action_toggle_args_mode(force_raw=True)
        area = app.query_one("#args-raw-area", ArgsTextArea)

        assert await _wrapped_height(pilot, area) > MAX_TEXT_ROWS, "fixture does not overflow at this width"
        assert area.scrollable_content_region.height == MAX_TEXT_ROWS
        assert app.query_one("#args-band").size.height <= 11   # the band's own max-height


async def test_a_one_line_command_still_takes_one_row(tmp_workspace: Path):
    """The editor grows to its content, so the common case is unchanged: a
    short template must not open a five-row box over the results pane.

    Six is the floor the band has always had in raw mode — its header, the
    bordered editor around one row of text, the `✓ done` line and the foot —
    and `min-height: 3` on the editor is what holds it there.
    """
    app = _app(tmp_workspace, SHORT)
    async with app.run_test(size=(140, 45)) as pilot:
        app.action_toggle_args_mode(force_raw=True)
        area = app.query_one("#args-raw-area", ArgsTextArea)

        assert await _wrapped_height(pilot, area) == 1
        assert area.scrollable_content_region.height == 1
        assert area.outer_size.height == 3                  # one row plus its border
        assert app.query_one("#args-band").size.height == 6
