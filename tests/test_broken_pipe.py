"""`fieldlog … | head`: the reader leaves early, and that is not a failure.

`run` piped into `head`, `grep -m1` or a `less` quit early used to take the
tool down mid-write and archive nothing, with a traceback. A closed pipe now
reads as the operator's "enough", like Ctrl+C: the tool is interrupted and the
run archived as interrupted. The readers used to exit 1 through Rich's own
SystemExit; they now exit 0, since nothing went wrong.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

DROPIN = """recipes:
  - id: s
    bin: seq
    presets:
      - id: many
        flags: "1 200000"
"""


@pytest.fixture
def box(tmp_path: Path) -> Path:
    (tmp_path / "recipes.d").mkdir()
    (tmp_path / "recipes.d" / "s.yaml").write_text(DROPIN)
    return tmp_path


def _read_then_leave(box: Path, *args: str, lines: int = 2) -> tuple[int, str]:
    """Run fieldlog, read `lines` lines of its stdout, close the pipe, and
    return (exit status, stderr) — what `| head -n <lines>` does."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "fieldlog", *args],
        cwd=box, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    for _ in range(lines):
        proc.stdout.readline()
    proc.stdout.close()
    _, err = proc.communicate(timeout=60)
    return proc.returncode, err.decode()


def _records(box: Path, target: str) -> list:
    return json.loads((box / "targets" / target / "session.json").read_text())


def test_a_run_whose_reader_leaves_is_archived_as_interrupted(box: Path):
    code, err = _read_then_leave(box, "run", "s/many", "10.0.0.1", "-q")

    assert "Traceback" not in err and "Broken pipe" not in err, err
    record = _records(box, "10.0.0.1")[-1]
    assert record["interrupted"] is True
    assert code == record["exit_code"] != 0


def test_a_reader_leaving_during_the_header_runs_nothing_and_says_so(box: Path):
    code, err = _read_then_leave(box, "run", "s/many", "10.0.0.2", lines=1)

    assert code == 130
    assert "nothing ran" in err
    raw = box / "targets" / "10.0.0.2" / "raw"
    assert not raw.exists() or not list(raw.iterdir())      # no empty log left behind


@pytest.mark.parametrize("command", [["list"], ["doctor"], ["history", "10.0.0.9"]])
def test_a_reader_leaving_a_listing_early_is_not_a_failure(box: Path, command):
    # A few runs, so `history` has more to print than the one line read.
    folder = box / "targets" / "10.0.0.9"
    folder.mkdir(parents=True)
    runs = [{"id": f"{i:02d}", "recipe": "note", "note": "x" * 80,
             "start_time": "2026-09-22T10:00:00"} for i in range(1, 400)]
    (folder / "session.json").write_text(json.dumps(runs))

    code, err = _read_then_leave(box, *command, lines=1)

    assert code == 0, err
    assert err == ""
