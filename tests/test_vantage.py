"""Every run records where it was made from: interface, local address, gateway
and wireless network, read from the OS's own routing answer as it starts.

The same check from the switch port and from the guest Wi-Fi are two different
results, and a week later the command line alone cannot say which it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog import runner as runner_mod
from fieldlog.cli import build_parser, handle_history
from fieldlog.launch import plan_launch
from fieldlog.report import render_report
from fieldlog.runner import run_job
from fieldlog.state import TargetSession
from fieldlog.vantage import probe_address, vantage, vantage_line

LINUX = {
    ("ip", "-o", "route", "get", "10.0.0.1"):
        "10.0.0.1 via 192.168.4.1 dev wlan0 src 192.168.4.23 uid 1000 \\    cache \n",
    ("ip", "-o", "route", "show", "default"):
        "default via 192.168.4.1 dev wlan0 proto dhcp metric 600 \n",
    ("iw", "dev", "wlan0", "link"):
        "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: Office Guest\n\tfreq: 5180\n",
}

DARWIN = {
    ("route", "-n", "get", "10.0.0.1"):
        "   route to: 10.0.0.1\ndestination: default\n    gateway: 192.168.4.1\n  interface: en0\n",
    ("ipconfig", "getsummary", "en0"):
        "<dictionary> {\n  InterfaceType : WiFi\n  SSID : Office Guest\n}\n",
}


def _runner(table: dict):
    return lambda argv: table.get(tuple(argv), "")


@pytest.fixture(autouse=True)
def _no_ioctl(monkeypatch):
    """The local address comes from the route answer here, never this box."""
    monkeypatch.setattr("fieldlog.vantage.get_interface_ip", lambda iface: "192.168.4.23")


# ---- reading the route --------------------------------------------------------


def test_linux_reads_the_route_to_the_target():
    assert vantage("10.0.0.1", "linux", _runner(LINUX)) == {
        "iface": "wlan0", "local": "192.168.4.23", "gateway": "192.168.4.1",
        "ssid": "Office Guest", "route": "target",
    }


def test_a_hostname_is_never_resolved_the_default_route_stands_in():
    asked = []
    table = _runner(LINUX)
    block = vantage("router1.example", "linux", lambda argv: asked.append(argv) or table(argv))
    assert block["route"] == "default" and block["iface"] == "wlan0"
    assert ["ip", "-o", "route", "show", "default"] in asked


def test_darwin_reads_route_get_and_the_ssid():
    assert vantage("10.0.0.1", "darwin", _runner(DARWIN)) == {
        "iface": "en0", "local": "192.168.4.23", "gateway": "192.168.4.1",
        "ssid": "Office Guest", "route": "target",
    }


def test_a_redacted_darwin_ssid_is_left_out():
    table = dict(DARWIN)
    table[("ipconfig", "getsummary", "en0")] = "  SSID : <redacted>\n"
    assert "ssid" not in vantage("10.0.0.1", "darwin", _runner(table))


def test_an_on_link_darwin_route_names_no_gateway():
    table = {("route", "-n", "get", "10.0.0.1"): "  gateway: link#6\n  interface: en0\n"}
    assert "gateway" not in vantage("10.0.0.1", "darwin", _runner(table))


def test_a_wired_link_has_no_ssid_and_leaves_the_key_out():
    table = {k: v for k, v in LINUX.items() if k[0] != "iw"}
    assert "ssid" not in vantage("10.0.0.1", "linux", _runner(table))


def test_nothing_known_is_no_block_at_all():
    assert vantage("10.0.0.1", "linux", lambda argv: "") == {}


def test_a_probe_that_raises_is_no_block_not_a_crash():
    def boom(argv):
        raise RuntimeError("wedged")
    assert vantage("10.0.0.1", "linux", boom) == {}


@pytest.mark.parametrize(
    "target, expected",
    [
        ("10.0.0.1", ("10.0.0.1", "")),
        ("10.0.0.0/24", ("10.0.0.0", "")),
        ("ops@10.0.0.5", ("10.0.0.5", "")),
        ("fe80::1%eth0", ("fe80::1", "eth0")),
        ("router1.example", (None, "")),
        ("", (None, "")),
    ],
)
def test_the_address_asked_about(target, expected):
    assert probe_address(target) == expected


def test_an_ipv6_zone_names_the_interface_when_the_route_does_not():
    assert vantage("fe80::1%eth1", "linux", lambda argv: "")["iface"] == "eth1"


# ---- the record and its readers -------------------------------------------------


BLOCK = {"iface": "wlan0", "local": "192.168.4.23", "gateway": "192.168.4.1", "ssid": "Office Guest"}


async def test_a_run_record_carries_its_vantage(tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(runner_mod, "vantage", lambda target: dict(BLOCK, route="target"))
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, {"id": "sh", "bin": "sh"}, {"id": "t", "flags": "-c true"})
    await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)

    record = json.loads((session.target_dir / "session.json").read_text())[-1]
    assert record["vantage"] == dict(BLOCK, route="target")


async def test_a_failed_vantage_leaves_the_key_off_and_the_run_alone(tmp_workspace: Path, monkeypatch):
    def boom(target):
        raise OSError("no route")
    monkeypatch.setattr(runner_mod, "vantage", boom)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, {"id": "sh", "bin": "sh"}, {"id": "t", "flags": "-c true"})
    assert await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env) == 0

    record = json.loads((session.target_dir / "session.json").read_text())[-1]
    assert "vantage" not in record


async def test_a_wedged_probe_delays_the_run_by_the_deadline_at_most(tmp_workspace: Path, monkeypatch):
    import time

    def slow(target):
        time.sleep(1)
        return dict(BLOCK)
    monkeypatch.setattr(runner_mod, "vantage", slow)
    monkeypatch.setattr(runner_mod, "VANTAGE_DEADLINE", 0.2)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, {"id": "sh", "bin": "sh"}, {"id": "t", "flags": "-c true"})

    started = time.monotonic()
    assert await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env) == 0
    assert time.monotonic() - started < 0.8

    record = json.loads((session.target_dir / "session.json").read_text())[-1]
    assert "vantage" not in record


def test_the_line_readers_show():
    assert vantage_line(BLOCK) == "wlan0 · 192.168.4.23 · via 192.168.4.1 · ssid Office Guest"
    assert vantage_line({"iface": "lo"}) == "lo"
    assert vantage_line(None) == "" and vantage_line({}) == ""


def _record(rid: str, block: dict) -> dict:
    return {
        "id": rid, "recipe": "ping/quick", "command": "ping 10.0.0.1", "exit_code": 0,
        "start_time": f"2026-09-22T10:0{rid[-1]}:00", "duration_sec": 1.0, "artifacts": [],
        "vantage": block,
    }


WIRED = {"iface": "eth0", "local": "10.1.1.9", "gateway": "10.1.1.1"}


def test_history_says_the_vantage_once_and_again_only_when_it_changes(tmp_workspace: Path, capsys):
    folder = tmp_workspace / "10.0.0.1"
    folder.mkdir()
    runs = [_record("01", WIRED), _record("02", WIRED), _record("03", BLOCK), _record("04", BLOCK)]
    (folder / "session.json").write_text(json.dumps(runs))

    args = build_parser().parse_args(["history", "10.0.0.1", "-w", str(tmp_workspace)])
    assert handle_history(args) == 0

    out = capsys.readouterr().out
    assert out.count("from eth0") == 1
    assert out.count("from wlan0") == 1
    assert out.index("from eth0") < out.index("#02") < out.index("from wlan0")


def test_the_report_says_where_each_run_was_made_from(tmp_workspace: Path):
    folder = tmp_workspace / "10.0.0.1"
    folder.mkdir()
    out = render_report(folder, [_record("01", BLOCK)])
    assert "From: `wlan0 · 192.168.4.23 · via 192.168.4.1 · ssid Office Guest`" in out


# ---- live: the real `ip` / `route` on whatever box runs the suite ------------
# The tests above read canned output. These ask this machine's kernel, which is
# the only check the macOS parsing gets: CI's macos-latest leg runs them.


def test_live_loopback_routes_through_the_loopback_interface(monkeypatch):
    monkeypatch.undo()          # the real get_interface_ip, not the autouse stub
    block = vantage("127.0.0.1")
    assert block.get("iface") in ("lo", "lo0"), block
    assert block.get("local") == "127.0.0.1", block
    assert block.get("route") == "target"


def test_live_default_route_when_this_box_has_one(monkeypatch):
    monkeypatch.undo()
    block = vantage("")
    if not block:
        pytest.skip("no default route on this machine")
    assert block["route"] == "default"
    assert block["iface"] and block["iface"] not in ("lo", "lo0"), block
