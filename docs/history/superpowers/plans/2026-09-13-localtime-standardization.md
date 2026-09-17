# Localtime Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Standardize fieldlog on local time for session manifest records, report generation, and documentation, matching the existing local time filenames.

**Architecture:** Update `fieldlog/runner.py` to serialize timestamps with naive local ISO 8601 strings (`datetime.fromtimestamp(ts).isoformat()`). Update `fieldlog/report.py` to render markdown without "UTC" labels. Update existing tests and documentation.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio

---

### Task 1: Runner Manifest Timestamps

**Files:**
- Modify: `fieldlog/runner.py:10-25` (imports) and `fieldlog/runner.py:364-370`
- Test: `tests/test_runner_clock.py`

- [ ] **Step 1: Write failing test for local timestamp generation in manifest**

Create `tests/test_runner_clock.py`:
```python
"""Verify session manifest records use naive local ISO 8601 timestamps."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from fieldlog.launch import plan_launch
from fieldlog.runner import run_job
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}


@pytest.mark.asyncio
async def test_session_records_use_naive_localtime(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    preset = {"id": "echo", "flags": "-c 'echo hello'"}
    plan = plan_launch(session, SH_TOOL, preset)

    code = await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    assert code == 0

    record = json.loads((session.target_dir / "session.json").read_text())[-1]
    start_time = record["start_time"]
    end_time = record["end_time"]

    # Must not contain timezone offset or Z
    assert "+" not in start_time and "Z" not in start_time
    assert "+" not in end_time and "Z" not in end_time
    # Must be valid ISO format with date and time
    assert "T" in start_time and "T" in end_time
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_runner_clock.py -v`
Expected: FAIL with `AssertionError: assert '+' not in '...T...+00:00'`

- [ ] **Step 3: Update `fieldlog/runner.py` to write naive local ISO 8601 timestamps**

In `fieldlog/runner.py`:
1. Change import:
```python
from datetime import datetime
```
(remove `timezone`)

2. In `_append_manifest` (around lines 366-367):
```python
        "start_time": datetime.fromtimestamp(start).isoformat(),
        "end_time": datetime.fromtimestamp(end).isoformat(),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_runner_clock.py -v`
Expected: PASS

- [ ] **Step 5: Commit changes**

```bash
git add tests/test_runner_clock.py fieldlog/runner.py
git commit -m "runner: record start_time and end_time as naive local ISO 8601"
```

---

### Task 2: Report Rendering Without UTC Labels

**Files:**
- Modify: `fieldlog/report.py:75-86, 250-300`
- Modify: `tests/test_report.py:15-100`

- [ ] **Step 1: Update test fixture and assertions in `tests/test_report.py`**

In `tests/test_report.py`:
1. Update `target_dir` fixture records to use local timestamps (aligning with their filenames):
   - Record `01`:
     `"start_time": "2026-09-12T12:00:49.734145",`
     `"end_time": "2026-09-12T12:00:49.745039",`
   - Record `02`:
     `"start_time": "2026-09-13T13:33:33.215199",`
     `"end_time": "2026-09-13T13:33:40.226243",`
2. Update assertions:
   - In `test_table_has_one_row_per_run`:
     Assert `"| Started |"` in table header instead of `"| Started (UTC) |"`.
     Assert summary line has `2026-09-12 12:00:49 to 2026-09-13 13:33:40` without trailing `UTC`.
   - In `test_binary_artifact_is_listed`:
     Check metadata line contains `Started 2026-09-13 13:33:33 ·` without `UTC`.

- [ ] **Step 2: Run `test_report.py` to verify failure**

Run: `.venv/bin/pytest tests/test_report.py -v`
Expected: FAIL due to `"UTC"` present in `fieldlog/report.py` output.

- [ ] **Step 3: Update `fieldlog/report.py`**

In `fieldlog/report.py`:
1. In `format_time`:
   Update docstring:
   ```python
   def format_time(stamp: object) -> str:
       """`2026-09-12T12:00:49.734145` -> `2026-09-12 12:00:49`, as stored (localtime)."""
   ```
2. In `render_markdown`:
   - Line 256:
     ```python
     summary += f" · {first} to {last}"
     ```
   - Line 268:
     ```python
     "| # | Recipe | Started | Duration | Exit | Files |",
     ```
   - Line 296:
     ```python
     meta = f"Started {started} · {duration}"
     ```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit changes**

```bash
git add fieldlog/report.py tests/test_report.py
git commit -m "report: remove UTC labels and use localtime in markdown output"
```

---

### Task 3: Documentation and Full Verification

**Files:**
- Modify: `README.md:380-381, 408`

- [ ] **Step 1: Update `README.md`**

1. Lines 380-381:
```json
  "start_time": "2026-09-12T18:01:56.734145",
  "end_time": "2026-09-12T18:01:59.801200",
```
(matching the filename `20260912T180156_ping_quick_01.log` in local time)

2. Line 408:
Change:
`- Log filenames use local time; the record's timestamps are UTC.`
To:
`- Log filenames and record timestamps use local time.`

- [ ] **Step 2: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: All tests pass.

- [ ] **Step 3: Commit changes**

```bash
git add README.md
git commit -m "docs: update run record example and clock description to localtime"
```
