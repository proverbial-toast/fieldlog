"""The TUI's `$OUTDIR` preview is redrawn on every keypress, so it reads the run
counter rather than parsing the whole manifest — and it reserves nothing."""

from __future__ import annotations

import json
from pathlib import Path

from fieldlog.app import FieldlogApp
from fieldlog.archive import RUN_COUNTER, next_run_number, peek_run_number, reserve_run_number
from fieldlog.state import TargetSession


def test_peeking_agrees_with_the_number_a_reservation_would_get(tmp_path: Path):
    target = tmp_path / "10.0.0.1"
    target.mkdir()
    assert peek_run_number(target) == next_run_number(target) == 1

    reserve_run_number(target)
    reserve_run_number(target)
    assert peek_run_number(target) == next_run_number(target) == 3


def test_an_archive_from_before_the_counter_still_answers(tmp_path: Path):
    """No counter file: the runs themselves are the only record of the number."""
    target = tmp_path / "10.0.0.1"
    target.mkdir()
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "ping/quick", "exit_code": 0, "artifacts": []},
        {"id": "02", "recipe": "ping/quick", "exit_code": 0, "artifacts": []},
    ]), encoding="utf-8")

    assert not (target / RUN_COUNTER).exists()
    assert peek_run_number(target) == next_run_number(target) == 3


def test_the_preview_names_the_next_run_and_claims_nothing(tmp_workspace: Path):
    # No run_test: pending_out_dir reads session state and the archive only.
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    counter = app.session.target_dir / RUN_COUNTER

    first = app.pending_out_dir()
    second = app.pending_out_dir()

    assert first == second
    assert first.endswith("_01")
    assert not counter.exists()                 # a preview reserves nothing
