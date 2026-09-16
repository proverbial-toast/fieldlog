"""The bounded tail reader must answer exactly as reading the whole log did.

A log big enough to matter is a log too big to keep in a test, so the size
thresholds are lowered instead: every case here goes down the seek-and-count
path that a multi-MB log would take in the field.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog import report as R


@pytest.fixture
def tiny_thresholds(monkeypatch):
    """Force the bounded path for logs measured in bytes, not megabytes."""
    monkeypatch.setattr(R, "WHOLE_FILE_MAX", 8)
    monkeypatch.setattr(R, "TAIL_WINDOW", 8)


LOGS = {
    "plain": b"alpha\nbravo\ncharlie\ndelta\necho\n",
    "no final newline": b"alpha\nbravo\ncharlie",
    "blank final line": b"alpha\nbravo\n\n",
    "whitespace final line": b"alpha\nbravo\n   \n",
    "crlf": b"alpha\r\nbravo\r\ncharlie\r\n",
    "lone cr": b"alpha\rbravo\rcharlie\r",
    "exotic breaks": "alpha\nbravo\x0bcharlie\x0cdelta echo\n".encode(),
    "undecodable bytes": b"\xff\xfe garbage\nreadable\n",
    "one long line": b"x" * 5000 + b"\n",
    "many lines": b"".join(b"line %d\n" % i for i in range(2000)),
    "empty": b"",
}


@pytest.mark.parametrize("name", list(LOGS))
@pytest.mark.parametrize("tail", [1, 2, 3, 40])
def test_tail_matches_a_whole_read(tmp_path: Path, tiny_thresholds, name: str, tail: int):
    log = tmp_path / "run.log"
    log.write_bytes(LOGS[name])

    whole = R.read_log_lines(log)
    shown, total = R.read_log_tail(log, tail)

    assert total == len(whole), f"{name}: line count disagrees with a whole read"
    assert shown == (whole[-tail:] if 0 < tail < len(whole) else whole)


@pytest.mark.parametrize("name", list(LOGS))
def test_count_matches_a_whole_read(tmp_path: Path, name: str):
    log = tmp_path / "run.log"
    log.write_bytes(LOGS[name])
    assert R.count_log_lines(log) == len(R.read_log_lines(log))


def test_count_spans_chunk_boundaries(tmp_path: Path, monkeypatch):
    """A \\r\\n split across two reads is one line break, not two."""
    log = tmp_path / "run.log"
    log.write_bytes(b"".join(b"line %d\r\n" % i for i in range(5000)))
    assert R.count_log_lines(log) == 5000 == len(R.read_log_lines(log))


def test_tail_of_zero_means_every_line(tmp_path: Path, tiny_thresholds):
    """`--full` and `--tail 0` ask for all of it; a bounded read cannot serve that."""
    log = tmp_path / "run.log"
    log.write_bytes(b"".join(b"line %d\n" % i for i in range(500)))
    shown, total = R.read_log_tail(log, 0)
    assert total == 500
    assert shown == R.read_log_lines(log)


def test_window_grows_for_lines_longer_than_it(tmp_path: Path, monkeypatch):
    """A log of 4 KB lines still yields 3 of them through a 64-byte window."""
    monkeypatch.setattr(R, "WHOLE_FILE_MAX", 64)
    monkeypatch.setattr(R, "TAIL_WINDOW", 64)
    log = tmp_path / "run.log"
    log.write_bytes(b"".join((b"%d" % i) + b"y" * 4096 + b"\n" for i in range(10)))
    shown, total = R.read_log_tail(log, 3)
    assert total == 10
    assert len(shown) == 3
    assert shown == R.read_log_lines(log)[-3:]


def test_report_quotes_the_same_tail_either_way(tmp_path: Path, monkeypatch):
    """The rendered report is what the operator sees; it must not shift."""
    import json

    target_dir = tmp_path / "10.0.0.5"
    (target_dir / "raw").mkdir(parents=True)
    log = target_dir / "raw" / "01.log"
    log.write_bytes(b"".join(b"packet %d captured\n" % i for i in range(3000)))
    (target_dir / "session.json").write_text(json.dumps([{
        "id": "01", "recipe": "tcpdump/icmp", "command": "tcpdump -i eth0",
        "artifact_log": str(log), "exit_code": 0, "duration_sec": 3.0,
        "start_time": "2026-09-16T12:00:00", "artifacts": [],
    }]))
    runs = R.load_runs(target_dir)

    whole_file = R.render_report(target_dir, runs, tail=40)
    monkeypatch.setattr(R, "WHOLE_FILE_MAX", 8)
    monkeypatch.setattr(R, "TAIL_WINDOW", 8)
    bounded = R.render_report(target_dir, runs, tail=40)

    assert bounded == whole_file
    assert "last 40 of 3000 lines, 2960 omitted" in bounded
