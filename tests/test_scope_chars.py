"""A target may be an ssh `user@host`; a dns name may not, and neither takes shell metacharacters."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.recipes import is_blocked, load_catalog
from fieldlog.state import TargetSession

NOOP_TOOL = {"id": "true", "bin": "true"}
TARGET_PRESET = {"id": "t", "flags": "$TARGET"}
HOST_PRESET = {"id": "h", "flags": "$HOST"}
IFACE_PRESET = {"id": "i", "flags": "-I $IFACE"}
LHOST_PRESET = {"id": "l", "flags": "-B $LHOST"}


def _catalog(tmp_path: Path, yaml_text: str):
    """A catalog from one base file, with nothing else on disk scanned."""
    base = tmp_path / "base.yaml"
    base.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(base=base, dropin_dir=dropins)


@pytest.mark.parametrize(
    "target", ["10.0.0.1", "10.0.0.0/24", "fe80::1", "jump1.example", "chris@jump1", "chris@10.0.0.1"]
)
def test_safe_targets_are_runnable(target):
    assert is_blocked(NOOP_TOOL, TARGET_PRESET, TargetSession(target=target))[0] is False


@pytest.mark.parametrize("target", ["a;b", "a b", "a|b", "$(id)", "`id`", "@(x)"])
def test_shell_metacharacters_block_a_target(target):
    blocked, reason = is_blocked(NOOP_TOOL, TARGET_PRESET, TargetSession(target=target))
    assert blocked and "target has unsafe characters" in reason


@pytest.mark.parametrize("iface", ["eth0", "eth0:0", "eth0.100", "tun0", "wlan0", ""])
def test_safe_interfaces_are_runnable(iface):
    # An empty interface stays allowed — same command as before the check.
    assert is_blocked(NOOP_TOOL, IFACE_PRESET, TargetSession(interface=iface))[0] is False


@pytest.mark.parametrize("iface", ["eth0;id", "a b", "a|b", "$(id)", "`id`", "eth0&"])
def test_shell_metacharacters_block_an_interface(iface):
    # $IFACE reaches the shell like the scope above, so it takes the same allowlist.
    blocked, reason = is_blocked(NOOP_TOOL, IFACE_PRESET, TargetSession(interface=iface))
    assert blocked and "interface has unsafe characters" in reason


def test_dns_name_still_refuses_at():
    blocked, reason = is_blocked(NOOP_TOOL, HOST_PRESET, TargetSession(hostname="chris@jump1"))
    assert blocked and "dns name has unsafe characters (@)" in reason


def test_user_at_host_does_not_stand_in_for_a_dns_name():
    session = TargetSession(target="chris@jump1")
    assert session.target_kind == "user@host"
    assert session.dns_name == ""
    blocked, reason = is_blocked(NOOP_TOOL, HOST_PRESET, session)
    assert blocked and "needs a dns name" in reason


def test_user_at_host_reaches_the_command(tmp_workspace: Path):
    session = TargetSession(target="chris@jump1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, NOOP_TOOL, {"id": "ssh", "flags": "env ssh $TARGET true"}, dry_run=True)
    assert "env ssh chris@jump1 true" in plan.command


# ---- A leading `-` is a flag, not a value ----------------------------------


@pytest.mark.parametrize("target", ["-f", "-c", "--help"])
def test_a_target_starting_with_a_dash_is_refused(target: str):
    # `--target=-f` after ping's own flags is a flood ping, not a target.
    blocked, reason = is_blocked(NOOP_TOOL, TARGET_PRESET, TargetSession(target=target))
    assert blocked and reason == "target must not start with -"


@pytest.mark.parametrize("target", ["a-b", "10.0.0.1", "x-1.example"])
def test_a_dash_inside_a_target_is_still_fine(target: str):
    assert is_blocked(NOOP_TOOL, TARGET_PRESET, TargetSession(target=target))[0] is False


def test_a_dns_name_starting_with_a_dash_is_refused():
    blocked, reason = is_blocked(NOOP_TOOL, HOST_PRESET, TargetSession(hostname="-x"))
    assert blocked and reason == "dns name must not start with -"


def test_an_interface_starting_with_a_dash_is_refused():
    blocked, reason = is_blocked(NOOP_TOOL, IFACE_PRESET, TargetSession(interface="-i"))
    assert blocked and reason == "interface must not start with -"


def test_a_local_address_starting_with_a_dash_is_refused():
    blocked, reason = is_blocked(NOOP_TOOL, LHOST_PRESET, TargetSession(lhost="-l"))
    assert blocked and reason == "local address must not start with -"


def test_the_cli_refuses_a_target_that_would_become_a_flag(tmp_path: Path, tmp_workspace: Path, capsys):
    from fieldlog.cli import build_parser, handle_run

    cat = _catalog(tmp_path, """
        recipes:
          - id: ping-like
            bin: true
            presets:
              - id: t
                flags: "-c 4 $TARGET"
        """)
    args = build_parser().parse_args(
        ["run", "ping-like/t", "--target=-f", "-w", str(tmp_workspace), "--dry-run"]
    )
    assert handle_run(args, cat) == 1
    assert "must not start with -" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["1.2.3", "10.0.0.256", "192.168.001.020"])
def test_a_malformed_address_is_refused(target: str):
    blocked, reason = is_blocked(NOOP_TOOL, TARGET_PRESET, TargetSession(target=target))
    assert blocked and reason == "target is not a valid address"


@pytest.mark.parametrize("target", ["10.0.0.1", "10.0.0.0/24", "fe80::1"])
def test_a_sound_address_or_subnet_still_runs(target: str):
    assert is_blocked(NOOP_TOOL, TARGET_PRESET, TargetSession(target=target))[0] is False
