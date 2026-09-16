"""`history` and `report` find a target by its folder name or by its target.

The archive folder is named for the dns name when one was set, so a run made
with `-t 10.10.11.50 -H box.htb` lands in `targets/box.htb/`. Asking for the
address used to report nothing there, though both commands spell the argument
`-t/--target`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog.cli import build_parser, find_target_dir, handle_history, handle_report


def _archive(workspace: Path, folder: str, target: str, host: str = "") -> Path:
    d = workspace / folder
    (d / "raw").mkdir(parents=True)
    (d / "session.json").write_text(json.dumps([{
        "id": "01", "recipe": "ping/quick", "command": "ping -c 1", "exit_code": 0,
        "duration_sec": 0.1, "start_time": "2026-09-16T09:00:00", "artifacts": [],
        "environment": {"TARGET": target, "TARGET_IP": target, "TARGET_HOST": host,
                        "LHOST": "", "IFACE": "eth0", "OUT_DIR": "", "RUN_ID": "01"},
    }]), encoding="utf-8")
    return d


def test_the_folder_name_still_wins(tmp_workspace: Path):
    box = _archive(tmp_workspace, "box.htb", "10.10.11.50", "box.htb")
    assert find_target_dir(tmp_workspace, "box.htb") == box


def test_the_target_finds_the_folder_named_for_its_host(tmp_workspace: Path):
    box = _archive(tmp_workspace, "box.htb", "10.10.11.50", "box.htb")
    assert find_target_dir(tmp_workspace, "10.10.11.50") == box


def test_a_subnet_folder_is_found_by_the_cidr_that_made_it(tmp_workspace: Path):
    """`192.168.1.0/24` becomes the folder `192.168.1.0_24`; both should work."""
    net = _archive(tmp_workspace, "192.168.1.0_24", "192.168.1.0/24")
    assert find_target_dir(tmp_workspace, "192.168.1.0/24") == net
    assert find_target_dir(tmp_workspace, "192.168.1.0_24") == net


def test_an_unknown_target_is_not_guessed_at(tmp_workspace: Path):
    _archive(tmp_workspace, "box.htb", "10.10.11.50", "box.htb")
    assert find_target_dir(tmp_workspace, "10.10.11.99") is None
    assert find_target_dir(tmp_workspace, "") is None


def test_the_right_folder_wins_when_two_targets_look_alike(tmp_workspace: Path):
    _archive(tmp_workspace, "box.htb", "10.10.11.50", "box.htb")
    other = _archive(tmp_workspace, "other.htb", "10.10.11.51", "other.htb")
    assert find_target_dir(tmp_workspace, "10.10.11.51") == other


def test_history_and_report_both_accept_the_target(tmp_workspace: Path, capsys):
    _archive(tmp_workspace, "box.htb", "10.10.11.50", "box.htb")

    assert handle_history(build_parser().parse_args(
        ["history", "10.10.11.50", "-w", str(tmp_workspace)])) == 0
    assert "box.htb" in capsys.readouterr().out

    assert handle_report(build_parser().parse_args(
        ["report", "10.10.11.50", "-w", str(tmp_workspace)])) == 0
    assert capsys.readouterr().out.startswith("# box.htb\n")


@pytest.mark.parametrize("command", ["history", "report"])
def test_an_unknown_target_lists_what_is_there(tmp_workspace: Path, capsys, command: str):
    _archive(tmp_workspace, "box.htb", "10.10.11.50", "box.htb")
    handler = {"history": handle_history, "report": handle_report}[command]

    assert handler(build_parser().parse_args([command, "nope", "-w", str(tmp_workspace)])) == 1
    err = capsys.readouterr().err
    assert "No runs for 'nope'" in err and "box.htb" in err


def test_a_real_folder_beats_an_inferred_match(tmp_workspace: Path):
    """Runs made both with and without `-H` leave two folders for one host. The
    name the operator typed is the folder they meant; the manifest scan is only
    a fallback for when no such folder exists."""
    _archive(tmp_workspace, "box.htb", "10.10.11.50", "box.htb")
    direct = _archive(tmp_workspace, "10.10.11.50", "10.10.11.50")

    assert find_target_dir(tmp_workspace, "10.10.11.50") == direct
    assert find_target_dir(tmp_workspace, "box.htb") == tmp_workspace / "box.htb"
