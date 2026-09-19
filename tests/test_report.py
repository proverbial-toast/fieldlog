"""`fieldlog report`: Markdown rendering of a target's session.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog.cli import build_parser, dispatch_argv, handle_report

LONG_LOG = "\n".join(f"line {n}" for n in range(1, 123)) + "\n"


@pytest.fixture
def target_dir(tmp_workspace: Path) -> Path:
    """A target with two runs: one written from the repo root with a cwd-relative
    artifact_log, one newer with an absolute path, a binary artifact and exit 130."""
    target = tmp_workspace / "10.0.0.1"
    raw = target / "raw"
    raw.mkdir(parents=True)

    (raw / "20260912T120049_ping_quick_01.log").write_text(LONG_LOG, encoding="utf-8")
    (raw / "20260913T133333_tcpdump_capture_02.log").write_text("capturing\n", encoding="utf-8")
    (raw / "20260913T133333.pcap").write_bytes(b"\xd4\xc3\xb2\xa1\x00\x00")

    records = [
        {
            "id": "01",
            "recipe": "ping/quick",
            "command": "ping -c 4 10.0.0.1 | tee out.txt",
            "environment": {
                "TARGET": "10.0.0.1", "TARGET_IP": "10.0.0.1", "TARGET_HOST": "",
                "LHOST": "", "IFACE": "eth0",
                "OUT_DIR": "targets/10.0.0.1/raw/20260912T120049", "RUN_ID": "01",
            },
            # Older runs stored this relative to the cwd they were launched from.
            "artifact_log": "targets/10.0.0.1/raw/20260912T120049_ping_quick_01.log",
            "out_dir": "targets/10.0.0.1/raw/20260912T120049",
            "start_time": "2026-09-12T12:00:49.734145",
            "end_time": "2026-09-12T12:00:49.745039",
            "duration_sec": 0.01,
            "exit_code": 0,
            "summary": "4 replies · 0% loss",
            "artifacts": [
                {"path": "raw/20260912T120049_ping_quick_01.log", "lines": 122, "bytes": 4422}
            ],
        },
        {
            "id": "02",
            "recipe": "tcpdump/capture",
            "command": "tcpdump -i eth0 -w $OUTDIR/x.pcap",
            "environment": {
                "TARGET": "10.0.0.1", "TARGET_IP": "10.0.0.1", "TARGET_HOST": "router1",
                "LHOST": "10.0.0.9", "IFACE": "eth0",
                "OUT_DIR": str(raw / "20260913T133333"), "RUN_ID": "02",
            },
            "artifact_log": str(raw / "20260913T133333_tcpdump_capture_02.log"),
            "out_dir": str(raw / "20260913T133333"),
            "start_time": "2026-09-13T13:33:33.215199",
            "end_time": "2026-09-13T13:33:40.226243",
            "duration_sec": 7.01,
            "exit_code": 130,
            "artifacts": [
                {"path": "raw/20260913T133333_tcpdump_capture_02.log", "lines": 1, "bytes": 10},
                {"path": "raw/20260913T133333.pcap", "lines": None, "bytes": 2097152, "binary": True},
            ],
        },
    ]
    (target / "session.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return target


def _report(tmp_workspace: Path, capsys, *extra: str) -> str:
    args = build_parser().parse_args(["report", "10.0.0.1", "-w", str(tmp_workspace), *extra])
    assert handle_report(args) == 0
    return capsys.readouterr().out


def test_dispatch_does_not_treat_report_as_a_recipe():
    assert dispatch_argv(["report", "x"]) == ("cli", ["report", "x"])


def test_table_has_one_row_per_run(target_dir: Path, tmp_workspace: Path, capsys):
    out = _report(tmp_workspace, capsys)
    rows = [ln for ln in out.splitlines() if ln.startswith("| ") and not ln.startswith("| # ")]
    assert len(rows) == 2
    assert out.startswith("# 10.0.0.1\n")
    assert "| # | Recipe | Started | Duration | Exit | Files |" in out
    assert "· 2 runs · 2026-09-12 12:00:49 to 2026-09-13 13:33:40\n" in out
    assert "Scope (from the latest run): target `10.0.0.1` · dns `router1` · interface `eth0` · lhost `10.0.0.9`" in out
    # A pipe in the command must not split the summary row.
    assert "| 01 | ping/quick | 2026-09-12 12:00:49 | 0.01s | 0 | 1 |" in out


def test_exit_codes_are_annotated(target_dir: Path, tmp_workspace: Path, capsys):
    out = _report(tmp_workspace, capsys)
    assert "| **130 (interrupted)** | 2 |" in out
    assert "## #02 · tcpdump/capture · exit 130 (interrupted)" in out
    assert "## #01 · ping/quick · exit 0" in out


def test_binary_artifact_is_listed_but_never_quoted(target_dir: Path, tmp_workspace: Path, capsys):
    out = _report(tmp_workspace, capsys)
    assert "- `raw/20260913T133333.pcap` · binary · 2.0 MB" in out
    assert "- `raw/20260912T120049_ping_quick_01.log` · 122 lines · 4.3 KB" in out
    assert "\xd4" not in out


def test_tail_truncation_reports_omitted_lines(target_dir: Path, tmp_workspace: Path, capsys):
    out = _report(tmp_workspace, capsys)
    assert "Output (last 40 of 122 lines, 82 omitted):" in out
    assert "line 83" in out and "line 82" not in out

    full = _report(tmp_workspace, capsys, "--full")
    assert "Output (122 lines):" in full
    assert "line 1\n" in full


def test_missing_log_says_so_instead_of_a_code_block(target_dir: Path, tmp_workspace: Path, capsys):
    (target_dir / "raw" / "20260912T120049_ping_quick_01.log").unlink()
    out = _report(tmp_workspace, capsys)
    assert "log not found: `targets/10.0.0.1/raw/20260912T120049_ping_quick_01.log`" in out
    assert "line 122" not in out


def test_since_filters_by_run_number(target_dir: Path, tmp_workspace: Path, capsys):
    out = _report(tmp_workspace, capsys, "--since", "2")
    assert "· 1 run ·" in out
    assert "tcpdump/capture" in out
    assert "ping/quick" not in out


def test_output_file_keeps_stdout_clean(target_dir: Path, tmp_workspace: Path, tmp_path: Path, capsys):
    destination = tmp_path / "out" / "nested" / "report.md"
    args = build_parser().parse_args(
        ["report", "10.0.0.1", "-w", str(tmp_workspace), "-o", str(destination)]
    )
    assert handle_report(args) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"wrote {destination} (2 runs)\n"
    assert destination.read_text(encoding="utf-8").startswith("# 10.0.0.1\n")


def test_missing_target_and_manifest_exit_1(tmp_workspace: Path, capsys):
    args = build_parser().parse_args(["report", "nope", "-w", str(tmp_workspace)])
    assert handle_report(args) == 1
    assert "No target folders with runs" in capsys.readouterr().err

    (tmp_workspace / "bare").mkdir()
    args = build_parser().parse_args(["report", "bare", "-w", str(tmp_workspace)])
    assert handle_report(args) == 1
    # The folder is the right one; it just has nothing in it yet.
    assert "No runs recorded in" in capsys.readouterr().err

    # With a real target present, a miss says what is there instead.
    (tmp_workspace / "10.0.0.9").mkdir()
    (tmp_workspace / "10.0.0.9" / "session.json").write_text("[]", encoding="utf-8")
    args = build_parser().parse_args(["report", "nope", "-w", str(tmp_workspace)])
    assert handle_report(args) == 1
    assert "Target folders: 10.0.0.9" in capsys.readouterr().err


def test_a_corrupt_manifest_is_reported_not_rendered_as_empty(tmp_workspace: Path, capsys):
    """`No runs recorded.` over a damaged archive is the one wrong answer.

    It used to be the one given: exit 0 and a clean, empty document, which
    reads as "nothing was ever run here" and pipes on as a finished report.
    See tests/test_manifest_problems.py for the whole contract.
    """
    target = tmp_workspace / "broken"
    target.mkdir()
    (target / "session.json").write_text("{not json", encoding="utf-8")
    args = build_parser().parse_args(["report", "broken", "-w", str(tmp_workspace)])
    assert handle_report(args) == 2
    captured = capsys.readouterr()
    assert "No runs recorded." not in captured.out
    assert "is not valid JSON" in captured.err


def test_a_summary_is_shown_only_for_the_run_that_has_one(target_dir: Path, tmp_workspace: Path, capsys):
    out = _report(tmp_workspace, capsys)
    assert "Summary: `4 replies · 0% loss`" in out
    # Run #02 has no parse rule, so it gets no summary line rather than an empty one.
    assert out.count("Summary: `") == 1
