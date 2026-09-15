"""A target may be an ssh `user@host`; a dns name may not, and neither takes shell metacharacters."""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.recipes import is_blocked
from fieldlog.state import TargetSession

NOOP_TOOL = {"id": "true", "bin": "true"}
TARGET_PRESET = {"id": "t", "flags": "$TARGET"}
HOST_PRESET = {"id": "h", "flags": "$HOST"}
IFACE_PRESET = {"id": "i", "flags": "-I $IFACE"}


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
