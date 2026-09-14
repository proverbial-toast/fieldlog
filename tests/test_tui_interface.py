"""Which interface the TUI starts on: a named one is kept, only an unset one is picked."""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog import app as app_mod
from fieldlog.app import FieldlogApp
from fieldlog.cli import build_parser, tui_session
from fieldlog.state import TargetSession


@pytest.fixture
def box(monkeypatch):
    """Pretend the box has exactly these interfaces and IPv4 addresses."""

    def install(addrs: dict[str, str]) -> None:
        monkeypatch.setattr(app_mod, "get_interface_ip", lambda name: addrs.get(name, ""))
        monkeypatch.setattr(
            app_mod, "list_box_interfaces",
            lambda: sorted(addrs.items(), key=lambda kv: (kv[0] == "lo", not kv[1], kv[0])),
        )

    return install


@pytest.mark.parametrize(
    "addrs, interface, lhost, expected",
    [
        ({"lo": "127.0.0.1", "eth0": "10.0.0.2", "wlan0": "192.168.1.5"}, "", "", ("eth0", "10.0.0.2")),
        ({"lo": "127.0.0.1", "eth0": "", "wlan0": "192.168.1.5"}, "", "", ("wlan0", "192.168.1.5")),
        ({"lo": "127.0.0.1", "eth0": ""}, "", "", ("eth0", "")),                    # nothing has an address
        ({"lo": "127.0.0.1", "eth0": "", "wlan0": "192.168.1.5"}, "", "10.9.9.9", ("wlan0", "10.9.9.9")),
        ({"lo": "127.0.0.1", "eth0": "", "wlan0": "192.168.1.5"}, "eth0", "", ("eth0", "")),  # named eth0 kept
        ({"lo": "127.0.0.1", "wlan0": "192.168.1.5"}, "tun0", "", ("tun0", "")),     # VPN not up yet
    ],
)
def test_tui_start_interface(box, tmp_workspace: Path, addrs, interface, lhost, expected):
    box(addrs)
    app = FieldlogApp(TargetSession(interface=interface, lhost=lhost, workspace_dir=tmp_workspace))
    assert (app.session.interface, app.session.lhost) == expected


def _tui_args(workspace: Path, *argv: str):
    return build_parser().parse_args(["tui", "-w", str(workspace), *argv])


def test_tui_session_leaves_an_unnamed_interface_unset(tmp_workspace: Path):
    assert tui_session(_tui_args(tmp_workspace)).interface == ""
    assert tui_session(_tui_args(tmp_workspace, "-i", "eth0")).interface == "eth0"


def test_tui_session_keeps_a_remembered_interface(tmp_workspace: Path):
    tmp_workspace.mkdir(parents=True, exist_ok=True)
    (tmp_workspace / ".last-scope.json").write_text('{"interface": "tun0"}')
    assert tui_session(_tui_args(tmp_workspace)).interface == "tun0"
