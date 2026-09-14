"""Which interface the TUI starts on, and what $LHOST follows.

A named interface is kept and only an unset one is picked. `session.lhost` is only
ever an address the operator set; otherwise $LHOST is read from the interface when
it is used, so a remembered scope never pins yesterday's address.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog import app as app_mod
from fieldlog import state as state_mod
from fieldlog.app import FieldlogApp, parse_iface_field
from fieldlog.cli import build_parser, tui_session
from fieldlog.state import TargetSession, save_last_scope


@pytest.fixture
def box(monkeypatch):
    """Pretend the box has exactly these interfaces and IPv4 addresses."""

    def install(addrs: dict[str, str]) -> None:
        def fake_ip(name: str) -> str:
            return addrs.get(name, "")

        monkeypatch.setattr(app_mod, "get_interface_ip", fake_ip)
        monkeypatch.setattr(state_mod, "get_interface_ip", fake_ip)
        monkeypatch.setattr(
            app_mod, "list_box_interfaces",
            lambda: sorted(addrs.items(), key=lambda kv: (kv[0] == "lo", not kv[1], kv[0])),
        )

    return install


@pytest.mark.parametrize(
    "addrs, interface, lhost, expected",
    [
        ({"lo": "127.0.0.1", "eth0": "10.0.0.2", "wlan0": "192.168.1.5"}, "", "", ("eth0", "", "10.0.0.2")),
        ({"lo": "127.0.0.1", "eth0": "", "wlan0": "192.168.1.5"}, "", "", ("wlan0", "", "192.168.1.5")),
        ({"lo": "127.0.0.1", "eth0": ""}, "", "", ("eth0", "", "")),                      # nothing has an address
        ({"lo": "127.0.0.1", "eth0": "", "wlan0": "192.168.1.5"}, "", "10.9.9.9", ("wlan0", "10.9.9.9", "10.9.9.9")),
        ({"lo": "127.0.0.1", "eth0": "", "wlan0": "192.168.1.5"}, "eth0", "", ("eth0", "", "")),  # named eth0 kept
        ({"lo": "127.0.0.1", "wlan0": "192.168.1.5"}, "tun0", "", ("tun0", "", "")),       # VPN not up yet
    ],
)
def test_tui_start_interface(box, tmp_workspace: Path, addrs, interface, lhost, expected):
    box(addrs)
    s = FieldlogApp(TargetSession(interface=interface, lhost=lhost, workspace_dir=tmp_workspace)).session
    assert (s.interface, s.lhost, s.effective_lhost()) == expected


def test_lhost_follows_the_interface_after_start(box, tmp_workspace: Path):
    box({"eth0": "10.0.0.2"})
    session = FieldlogApp(TargetSession(interface="eth0", workspace_dir=tmp_workspace)).session
    box({"eth0": "10.0.0.7"})                      # DHCP hands out a new address
    assert session.effective_lhost() == "10.0.0.7"


def test_saved_scope_keeps_only_an_address_you_set(box, tmp_workspace: Path):
    box({"eth0": "10.0.0.2"})
    saved = tmp_workspace / ".last-scope.json"

    save_last_scope(FieldlogApp(TargetSession(interface="", workspace_dir=tmp_workspace)).session)
    assert json.loads(saved.read_text())["lhost"] == ""

    session = TargetSession(interface="tun0", lhost="10.8.0.2", workspace_dir=tmp_workspace)
    save_last_scope(FieldlogApp(session).session)
    assert json.loads(saved.read_text())["lhost"] == "10.8.0.2"


@pytest.mark.parametrize(
    "raw, current, expected",
    [
        ("tun0 / 10.8.0.2", "eth0", ("tun0", "10.8.0.2")),
        ("tun0/10.8.0.2", "eth0", ("tun0", "10.8.0.2")),
        ("eth0", "wlan0", ("eth0", "")),               # a bare name goes back to the interface's own
        ("", "wlan0", ("wlan0", "")),
        (" / 10.8.0.2", "wlan0", ("wlan0", "10.8.0.2")),
    ],
)
def test_scope_form_interface_field(raw, current, expected):
    assert parse_iface_field(raw, current) == expected


@pytest.mark.asyncio
async def test_scope_form_saves_a_typed_address_and_clears_it(box, tmp_workspace: Path):
    from textual.widgets import Input

    from fieldlog.app import TargetModal

    box({"eth0": "10.0.0.2", "tun0": ""})
    app = FieldlogApp(TargetSession(target="10.0.0.1", interface="eth0", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        for raw, expected in (("tun0 / 10.8.0.2", ("tun0", "10.8.0.2")), ("eth0", ("eth0", ""))):
            modal = TargetModal(app.session)
            app.push_screen(modal)
            await pilot.pause()
            modal.query_one("#in-iface", Input).value = raw
            modal.action_save()
            await pilot.pause()
            assert (app.session.interface, app.session.lhost) == expected


def _tui_args(workspace: Path, *argv: str):
    return build_parser().parse_args(["tui", "-w", str(workspace), *argv])


def test_tui_session_leaves_an_unnamed_interface_unset(tmp_workspace: Path):
    assert tui_session(_tui_args(tmp_workspace)).interface == ""
    assert tui_session(_tui_args(tmp_workspace, "-i", "eth0")).interface == "eth0"


def test_tui_session_keeps_a_remembered_interface(tmp_workspace: Path):
    (tmp_workspace / ".last-scope.json").write_text('{"interface": "tun0"}')
    assert tui_session(_tui_args(tmp_workspace)).interface == "tun0"
