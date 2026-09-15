from __future__ import annotations

import pytest
from pathlib import Path
from fieldlog.state import TargetSession
from fieldlog.recipes import is_blocked

def test_empty_target_blocked():
    session = TargetSession(target="")
    assert session.target == ""

    tool = {"id": "ping", "bin": "ping"}
    preset_with_target = {"id": "quick", "flags": "-c 1 $TARGET"}
    blocked, reason = is_blocked(tool, preset_with_target, session)
    assert blocked is True
    assert "needs a target" in reason

    preset_without_target = {"id": "localnet", "flags": "--interface=$IFACE"}
    blocked_no_target, _ = is_blocked(tool, preset_without_target, session)
    # Ping without target in flags shouldn't block on target
    assert blocked_no_target is False


def test_empty_lhost_blocked():
    """An interface with no IPv4 leaves $LHOST empty, so a preset using it must not run."""
    tool = {"id": "true", "bin": "true"}
    preset = {"id": "serve", "flags": "-s -1 -B ${LHOST}"}

    blocked, reason = is_blocked(tool, preset, TargetSession(interface="nosuch0"))
    assert blocked is True
    assert "needs a local address" in reason and "nosuch0" in reason

    assert is_blocked(tool, preset, TargetSession(interface="nosuch0", lhost="10.9.9.9"))[0] is False
    blocked, reason = is_blocked(tool, preset, TargetSession(interface="nosuch0", lhost="10.9.9.9;id"))
    assert blocked and "local address has unsafe characters (;)" in reason
    assert is_blocked(tool, {"id": "i", "flags": "-I $IFACE"}, TargetSession(interface="nosuch0"))[0] is False


def test_dry_run_creates_no_files(tmp_workspace: Path):
    from fieldlog.launch import plan_launch

    session = TargetSession(target="10.20.30.40", workspace_dir=tmp_workspace)
    tool = {"id": "ping", "bin": "ping"}
    preset = {"id": "quick", "flags": "-c 1 $TARGET"}

    plan = plan_launch(session, tool, preset, dry_run=True)
    assert plan.command == "ping -c 1 10.20.30.40"
    # Ensure neither log nor target dir was created on disk
    assert not plan.job.log_path.exists()
    assert not (tmp_workspace / "10.20.30.40").exists()


def test_format_command_wrappers():
    from fieldlog.recipes import format_command

    # Normal flags
    assert format_command("ping", "-c 4 127.0.0.1") == "ping -c 4 127.0.0.1"
    # Redundant bin
    assert format_command("ping", "ping -c 4 127.0.0.1") == "ping -c 4 127.0.0.1"
    # Wrappers: sudo, doas, timeout, env, nice
    assert format_command("tcpdump", "sudo tcpdump -i eth0") == "sudo tcpdump -i eth0"
    assert format_command("tcpdump", "doas tcpdump -i eth0") == "doas tcpdump -i eth0"
    assert format_command("nmap", "sudo nmap -sS 10.0.0.1") == "sudo nmap -sS 10.0.0.1"
    assert format_command("ping", "timeout 5 ping 127.0.0.1") == "timeout 5 ping 127.0.0.1"
    assert format_command("ping", "nice -n 10 ping 127.0.0.1") == "nice -n 10 ping 127.0.0.1"


def test_history_scope_dir(tmp_workspace: Path):
    import json
    import argparse
    from fieldlog.cli import handle_history

    # A target like fe80::1 has scope_dir fe80--1
    target_dir = tmp_workspace / "fe80--1"
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = target_dir / "session.json"
    manifest.write_text(json.dumps([{"id": "01", "recipe": "ping/quick", "exit_code": 0}]))

    args = argparse.Namespace(target="fe80::1", target_flag="", workspace=str(tmp_workspace), json=True)
    # If handle_history uses raw target.replace('/', '_').replace(' ', '_'),
    # it looks for fe80::1 instead of fe80--1 and exits 1.
    res = handle_history(args)
    assert res == 0


def test_get_interface_ip_lifecycle_and_cache(monkeypatch):
    import fcntl
    from fieldlog import state

    # Reset cache
    state._clear_ip_cache()

    ioctl_calls = 0

    def mock_ioctl(fd, req, arg):
        nonlocal ioctl_calls
        ioctl_calls += 1
        # 20 bytes padding + 4 bytes for 192.168.12.34
        return b"\x00" * 20 + bytes([192, 168, 12, 34]) + b"\x00" * 20

    monkeypatch.setattr(fcntl, "ioctl", mock_ioctl)

    # First call triggers ioctl
    ip1 = state.get_interface_ip("eth0")
    assert ip1 == "192.168.12.34"
    assert ioctl_calls == 1

    # Second immediate call should hit cache without extra ioctl call
    ip2 = state.get_interface_ip("eth0")
    assert ip2 == "192.168.12.34"
    assert ioctl_calls == 1

    # Clearing cache should cause a new ioctl call
    state._clear_ip_cache()
    ip3 = state.get_interface_ip("eth0")
    assert ip3 == "192.168.12.34"
    assert ioctl_calls == 2


def test_pinned_recent_persistence(tmp_workspace: Path):
    from fieldlog.state import load_pinned_recent, save_pinned_recent

    # 1. Non-existent file starts empty
    pinned, recent = load_pinned_recent(tmp_workspace)
    assert pinned == []
    assert recent == []

    # 2. Saving and loading valid keys
    save_pinned_recent(tmp_workspace, ["ping/quick"], ["curl/timing443", "wrk/smoke"])
    p, r = load_pinned_recent(tmp_workspace)
    assert p == ["ping/quick"]
    assert r == ["curl/timing443", "wrk/smoke"]

    # 3. Filtering against valid catalog keys drops removed/unknown keys
    valid_keys = {"ping/quick", "curl/timing443"}
    p_filtered, r_filtered = load_pinned_recent(tmp_workspace, valid_keys=valid_keys)
    assert p_filtered == ["ping/quick"]
    assert r_filtered == ["curl/timing443"]  # wrk/smoke was filtered out

    # 4. Corrupt JSON file returns empty lists safely
    (tmp_workspace / ".pinned-recent.json").write_text("{not valid json")
    p_corrupt, r_corrupt = load_pinned_recent(tmp_workspace)
    assert p_corrupt == []
    assert r_corrupt == []


def test_clipboard_deduplication(monkeypatch):
    import sys
    from fieldlog.app import copy_text_to_clipboard

    stdout_writes = []
    monkeypatch.setattr(sys.stdout, "write", lambda s: stdout_writes.append(s))

    class MockApp:
        def __init__(self):
            self.copied = []
        def copy_to_clipboard(self, text):
            self.copied.append(text)

    app = MockApp()
    # When app is passed, Textual clipboard is used and sys.stdout.write is NOT called
    res = copy_text_to_clipboard("hello world", app=app)
    assert res is True
    assert app.copied == ["hello world"]
    assert stdout_writes == []

    # When app is None, stdout OSC 52 write is called
    res_no_app = copy_text_to_clipboard("test", app=None)
    assert res_no_app is True
    assert len(stdout_writes) > 0
    assert "\033]52;c;" in stdout_writes[0]


@pytest.mark.asyncio
async def test_action_copy_log_system_tab(tmp_path):
    from fieldlog.app import FieldlogApp
    from fieldlog.state import TargetSession

    session = TargetSession(workspace_dir=tmp_path)
    app = FieldlogApp(session=session)
    async with app.run_test():
        assert app.active_tab().id == "system"
        app.action_copy_log()


@pytest.mark.asyncio
async def test_action_copy_log_job_tab(tmp_path, monkeypatch):
    from fieldlog.app import FieldlogApp, TabDescriptor
    from fieldlog.state import TargetSession, ActiveJob

    session = TargetSession(workspace_dir=tmp_path)
    app = FieldlogApp(session=session)

    copied_texts = []
    monkeypatch.setattr(app, "copy_to_clipboard", lambda text: copied_texts.append(text))

    log_file = tmp_path / "test_job.log"
    log_content = "\n".join(f"line {i}" for i in range(1000))
    log_file.write_text(log_content, encoding="utf-8")

    job = ActiveJob(
        id="01",
        recipe_id="ping/quick",
        name="ping/quick #01",
        log_path=log_file,
        log_lines=[f"line {i}" for i in range(500)],
    )

    tab = TabDescriptor(
        id="job-01",
        label="ping/quick #01",
        status="done",
        tool_id="ping",
        job_id="01",
        cmd="ping -c 4 127.0.0.1",
        artifact=str(log_file),
    )

    async with app.run_test():
        app.jobs["01"] = job
        app.tabs.append(tab)
        app.active_tab_id = "job-01"

        app.action_copy_log()
        assert len(copied_texts) == 1
        assert "line 999" in copied_texts[0]
        assert "line 0" in copied_texts[0]


@pytest.mark.asyncio
async def test_action_copy_log_truncated(tmp_path, monkeypatch):
    from fieldlog.app import FieldlogApp, TabDescriptor
    from fieldlog.state import TargetSession, ActiveJob

    session = TargetSession(workspace_dir=tmp_path)
    app = FieldlogApp(session=session)

    copied_texts = []
    monkeypatch.setattr(app, "copy_to_clipboard", lambda text: copied_texts.append(text))

    log_file = tmp_path / "large_job.log"
    chunk = "A" * 100 + "\n"
    log_file.write_text(chunk * 6000, encoding="utf-8")

    job = ActiveJob(
        id="02",
        recipe_id="tcpdump/icmp",
        name="tcpdump #02",
        log_path=log_file,
    )
    tab = TabDescriptor(
        id="job-02",
        label="tcpdump #02",
        status="done",
        tool_id="tcpdump",
        job_id="02",
    )

    async with app.run_test():
        app.jobs["02"] = job
        app.tabs.append(tab)
        app.active_tab_id = "job-02"

        app.action_copy_log()
        assert len(copied_texts) == 1
        assert len(copied_texts[0].encode("utf-8")) <= 550 * 1024
        assert "capped at 500 KB" in app.system_log_lines[-1]






