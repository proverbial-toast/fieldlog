"""macOS support: what differs per platform, and how each difference is checked.

The work was done on Linux, so every case that would otherwise need a Mac is
driven by a platform name passed in — `load_catalog(platform="darwin")` reads the
shipped catalog exactly as a Mac would — or by monkeypatching the one lookup that
answers for the box. The two that cannot be faked, the `SIOCGIFADDR` ioctl and
`sys.platform` itself, are asserted against whichever system is running the suite.
"""

from __future__ import annotations

import socket
import sys
import textwrap
from pathlib import Path

import pytest

from fieldlog import app as app_mod
from fieldlog import recipes as recipes_mod
from fieldlog.app import default_interface
from fieldlog.cli import build_parser, handle_run
from fieldlog.recipes import (
    Catalog,
    load_catalog,
    normalize_recipe,
    parse_rule,
    parse_summary,
    timeout_binary,
)
from fieldlog.state import DEFAULT_INTERFACE, LOOPBACK_NAMES, TargetSession, _clear_ip_cache, get_interface_ip

# ---- `platform:` on a catalog entry ----------------------------------------


def _catalog(tmp_path: Path, base: str, platform: str) -> Catalog:
    """A catalog from one base file, read as `platform` would read it."""
    (tmp_path / "base.yaml").write_text(textwrap.dedent(base), encoding="utf-8")
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(base=tmp_path / "base.yaml", dropin_dir=dropins, platform=platform)


def _ids(cat: Catalog) -> list:
    return [f"{t['id']}/{p['id']}" for t in cat.tools for p in t["presets"]]


def _flags(cat: Catalog, tool_id: str, preset_id: str) -> str:
    tool = next(t for t in cat.tools if t["id"] == tool_id)
    return next(p for p in tool["presets"] if p["id"] == preset_id)["flags"]


PRESET_LEVEL = """
    recipes:
      - id: ping
        bin: true
        presets:
          - id: here
            platform: {written}
            flags: "-c 1"
          - id: always
            flags: "-c 2"
    """


@pytest.mark.parametrize("written", ["linux", "[linux]", "[linux, freebsd]"])
def test_a_preset_named_for_this_platform_is_there_and_no_other(tmp_path: Path, written):
    kept = _catalog(tmp_path, PRESET_LEVEL.format(written=written), "linux")
    assert _ids(kept) == ["ping/here", "ping/always"]
    assert kept.errors == []

    gone = _catalog(tmp_path, PRESET_LEVEL.format(written=written), "darwin")
    assert _ids(gone) == ["ping/always"]
    assert gone.errors == []


def test_a_tool_level_platform_carries_its_presets_with_it(tmp_path: Path):
    text = """
        recipes:
          - id: ss
            bin: true
            platform: linux
            presets:
              - id: listen
                flags: "-tulpn"
              - id: estab
                flags: "-tn"
          - id: dig
            bin: true
            presets:
              - id: a
                flags: "+short $HOST"
        """
    assert _ids(_catalog(tmp_path, text, "linux")) == ["ss/listen", "ss/estab", "dig/a"]
    assert _ids(_catalog(tmp_path, text, "darwin")) == ["dig/a"]


def test_a_tool_left_with_no_presets_goes_too(tmp_path: Path):
    text = """
        recipes:
          - id: ethtool
            bin: true
            presets:
              - id: link
                platform: linux
                flags: "$IFACE"
              - id: stats
                platform: linux
                flags: "-S $IFACE"
        """
    cat = _catalog(tmp_path, text, "darwin")
    assert [t["id"] for t in cat.tools] == []
    assert cat.errors == []          # silently, not as a tool with nothing in it


def test_one_id_written_once_per_platform_is_one_entry_not_a_repeat(tmp_path: Path):
    text = """
        recipes:
          - id: ping
            bin: true
            presets:
              - id: quick
                platform: linux
                flags: "-c 4 -W 1 $TARGET"
              - id: quick
                platform: darwin
                flags: "-c 4 -t 6 $TARGET"
        """
    for platform, flags in (("linux", "-c 4 -W 1 $TARGET"), ("darwin", "-c 4 -t 6 $TARGET")):
        cat = _catalog(tmp_path, text, platform)
        assert _ids(cat) == ["ping/quick"]
        assert _flags(cat, "ping", "quick") == flags
        assert cat.errors == []      # the other entry was never there to collide with


def test_an_unknown_platform_name_simply_hides_the_entry(tmp_path: Path):
    text = """
        recipes:
          - id: ping
            bin: true
            presets:
              - id: plan9
                platform: plan9
                flags: "-c 1"
        """
    cat = _catalog(tmp_path, text, "linux")
    assert _ids(cat) == [] and cat.errors == []
    # The vocabulary is not fixed: a box that calls itself plan9 gets the recipe.
    assert _ids(_catalog(tmp_path, text, "plan9")) == ["ping/plan9"]


def test_a_platform_value_that_names_no_platform_is_reported_and_the_entry_kept(tmp_path: Path):
    text = """
        recipes:
          - id: ping
            bin: true
            presets:
              - id: junk
                platform: 3
                flags: "-c 1"
        """
    for platform in ("linux", "darwin"):
        cat = _catalog(tmp_path, text, platform)
        assert _ids(cat) == ["ping/junk"]
        assert len(cat.errors) == 1 and "platform:" in cat.errors[0]


@pytest.mark.parametrize("value", ["", "[]", "null", "[\"\"]"])
def test_a_platform_left_empty_is_the_same_as_absent(tmp_path: Path, value: str):
    """`platform:` written and left empty says nothing, as an empty `presets:` or
    `expect:` does: the entry exists on every platform and nothing is reported."""
    text = f"""
        recipes:
          - id: ping
            bin: true
            presets:
              - id: any
                platform: {value}
                flags: "-c 1"
        """
    for platform in ("linux", "darwin"):
        cat = _catalog(tmp_path, text, platform)
        assert _ids(cat) == ["ping/any"]
        assert cat.errors == []


# ---- The shipped catalog, read as each platform reads it --------------------


def _shipped(tmp_path: Path, platform: str) -> Catalog:
    """The shipped catalog alone — no drop-in on this machine is scanned."""
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(dropin_dir=dropins, platform=platform)


# What a Linux box has had all along. Spelled out rather than derived, so a tool
# that disappears behind a mistyped `platform:` is caught here and not in the
# README, which counts them.
LINUX_TOOLS = [
    "ping", "traceroute", "mtr", "arp-scan", "dig", "resolvectl", "curl",
    "openssl", "wrk", "ss", "tcpdump", "iperf3", "ethtool",
]


def test_the_shipped_catalog_is_unchanged_on_linux(tmp_path: Path):
    cat = _shipped(tmp_path, "linux")
    assert cat.errors == []
    assert [t["id"] for t in cat.tools] == LINUX_TOOLS
    assert "-W 1" in _flags(cat, "ping", "quick")
    assert [p["id"] for p in next(t for t in cat.tools if t["id"] == "traceroute")["presets"]] == [
        "icmp", "mtu", "tcp80",
    ]


def test_the_shipped_catalog_loads_as_a_mac_reads_it(tmp_path: Path):
    cat = _shipped(tmp_path, "darwin")
    assert cat.errors == []

    quick = _flags(cat, "ping", "quick")
    assert "-t 6" in quick and "-W" not in quick
    v6 = next(p for p in next(t for t in cat.tools if t["id"] == "ping")["presets"] if p["id"] == "v6")
    assert v6["bin"] == "ping6"

    traceroute = next(t for t in cat.tools if t["id"] == "traceroute")
    assert [p["id"] for p in traceroute["presets"]] == ["icmp"]

    ids = [t["id"] for t in cat.tools]
    assert not {"ss", "ethtool", "resolvectl"} & set(ids)
    assert {"scutil", "lsof"} <= set(ids)

    # The stand-ins keep the chain honest: `reach` still resolves to real presets.
    assert [s["recipe"] for s in cat.chains[0]["steps"]] == ["ping/quick", "traceroute/icmp", "dig/ptr"]


@pytest.mark.parametrize(
    "line, expected",
    [
        ("4 packets transmitted, 4 received, 0% packet loss, time 3003ms", "4 replies · 0% loss"),
        ("4 packets transmitted, 4 packets received, 0.0% packet loss", "4 replies · 0.0% loss"),
    ],
)
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_ping_rule_reads_either_platforms_stats_line(tmp_path: Path, platform, line, expected):
    cat = _shipped(tmp_path, platform)
    preset = next(p for p in next(t for t in cat.tools if t["id"] == "ping")["presets"] if p["id"] == "quick")
    assert parse_summary(parse_rule(preset), line) == expected


# ---- `--timeout` and the coreutils binary behind it -------------------------


@pytest.mark.parametrize(
    "installed, expected",
    [
        (("timeout", "gtimeout"), "timeout"),
        (("gtimeout",), "gtimeout"),          # Homebrew coreutils on a Mac
        ((), None),
    ],
)
def test_timeout_binary_prefers_the_coreutils_name_then_the_homebrew_one(monkeypatch, installed, expected):
    monkeypatch.setattr(recipes_mod, "is_tool_installed", lambda name: name in installed)
    assert timeout_binary() == expected


TRUE = Catalog(tools=[normalize_recipe({"id": "sleeper", "bin": "true", "presets": [{"id": "t", "flags": ""}]})])


def _run_args(workspace: Path, *argv: str):
    return build_parser().parse_args(
        ["run", "sleeper/t", "-t", "10.0.0.1", "-w", str(workspace), *argv]
    )


def test_run_refuses_a_timeout_it_cannot_enforce_and_reserves_nothing(tmp_workspace: Path, monkeypatch, capsys):
    monkeypatch.setattr("fieldlog.cli.timeout_binary", lambda: None)
    assert handle_run(_run_args(tmp_workspace, "--timeout", "5"), TRUE) == 1

    err = capsys.readouterr().err
    assert err == "Error: --timeout needs coreutils timeout in $PATH (macOS: brew install coreutils).\n"
    assert not (tmp_workspace / "10.0.0.1" / ".run-counter").exists()


def test_a_dry_run_still_previews_the_timeout_wrapper_on_a_box_without_one(
    tmp_workspace: Path, monkeypatch, capsys
):
    monkeypatch.setattr("fieldlog.cli.timeout_binary", lambda: None)
    monkeypatch.setattr("fieldlog.launch.timeout_binary", lambda: None)
    assert handle_run(_run_args(tmp_workspace, "--timeout", "5", "--dry-run"), TRUE) == 0

    assert "timeout -k 5 5s" in capsys.readouterr().out
    assert not (tmp_workspace / "10.0.0.1" / ".run-counter").exists()


def test_the_plan_uses_whichever_timeout_the_box_has(tmp_workspace: Path, monkeypatch):
    from fieldlog.launch import plan_launch

    monkeypatch.setattr("fieldlog.launch.timeout_binary", lambda: "gtimeout")
    session = TargetSession(target="10.0.0.9", workspace_dir=tmp_workspace)
    plan = plan_launch(session, {"id": "true", "bin": "true"}, {"id": "t", "flags": ""}, timeout=5, dry_run=True)
    assert plan.command == "gtimeout -k 5 5s sh -c 'exec true'"


# ---- The default interface and $LHOST --------------------------------------


def test_the_default_interface_is_this_platforms_first_wired_name():
    assert DEFAULT_INTERFACE == ("en0" if sys.platform == "darwin" else "eth0")
    assert TargetSession().interface == DEFAULT_INTERFACE


def test_default_interface_skips_loopback_under_either_name(monkeypatch):
    box = [("lo0", "127.0.0.1"), ("wlan0", "10.0.0.2")]
    monkeypatch.setattr(app_mod, "list_box_interfaces", lambda: box)
    monkeypatch.setattr(app_mod, "get_interface_ip", lambda name: dict(box).get(name, ""))
    assert default_interface() == "wlan0"
    assert set(LOOPBACK_NAMES) == {"lo", "lo0"}


def test_the_ioctl_answers_for_the_loopback_interface_on_this_platform():
    """The one thing only the macOS runner can prove: SIOCGIFADDR's number."""
    name = "lo0" if sys.platform == "darwin" else "lo"
    try:
        present = any(n == name for _index, n in socket.if_nameindex())
    except OSError:
        present = False
    if not present:
        pytest.skip(f"this box has no {name}")

    _clear_ip_cache()
    assert get_interface_ip(name) == "127.0.0.1"
