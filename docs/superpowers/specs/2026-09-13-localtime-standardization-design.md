# Design Specification: Localtime Standardization

## Problem
Fieldlog currently uses two different clocks:
- Filenames and directory paths use local time via `run_stamp()` (`fieldlog/state.py:136`), which calls `time.localtime()`.
- Manifest records in `session.json` use UTC via `runner.py:366-367` (`datetime.fromtimestamp(ts, timezone.utc).isoformat()`).
- The report renderer (`fieldlog/report.py`) hardcodes "UTC" in headers and summaries.
- The documentation (`README.md:408`) notes this discrepancy explicitly.

## Goal
Standardize fieldlog on a single clock — local time — across the runner, manifest records, report output, and documentation, leaving archive path handling as-is.

## Changes

### 1. Runner Manifest Generation (`fieldlog/runner.py`)
- Change `start_time` and `end_time` generation from `datetime.fromtimestamp(..., timezone.utc).isoformat()` to naive local `datetime.fromtimestamp(...).isoformat()`.
- Clean up unused `timezone` import from `datetime`.

### 2. Report Markdown Rendering (`fieldlog/report.py`)
- Update `format_time()` docstring to reflect local time storage.
- In `render_markdown()`:
  - Summary: change `f" · {first} to {last} UTC"` to `f" · {first} to {last}"`.
  - Table header: change `"| # | Recipe | Started (UTC) | Duration | Exit | Files |"` to `"| # | Recipe | Started | Duration | Exit | Files |"`.
  - Per-run meta header: change `f"Started {started} UTC · {duration}"` to `f"Started {started} · {duration}"`.

### 3. Tests (`tests/test_report.py`)
- Update test fixture records in `target_dir` to store naive local ISO 8601 timestamps without `+00:00`.
- Update assertions in `tests/test_report.py`:
  - Update table header assertion to check for `"| Started |"`.
  - Update summary assertion to check for `"to ..."` without trailing `"UTC"`.
  - Update per-run meta line assertion to check for `Started ... ·` without `"UTC"`.

### 4. Documentation (`README.md`)
- Update example `session.json` record (`start_time` / `end_time`) to show naive local ISO 8601 timestamps without `+00:00`.
- Update bullet in archive section: "Log filenames and record timestamps use local time." (replacing "Log filenames use local time; the record's timestamps are UTC.").
