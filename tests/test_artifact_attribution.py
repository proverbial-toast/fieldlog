"""A run records the files it owns — its primary log and its own `$OUTDIR`.

The old model diffed the whole target folder against a snapshot taken at spawn,
which cannot tell "changed while this ran" from "this run wrote it": two runs in
flight each claimed the other's output. A recipe that genuinely writes outside
its `$OUTDIR` opts back into the scan with `scan: true`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fieldlog.archive import recorded_path
from fieldlog.launch import plan_launch
from fieldlog.recipes import scans_workspace
from fieldlog.runner import run_job
from fieldlog.state import TargetSession

SH = {"id": "sh", "bin": "sh"}


async def _run(session: TargetSession, preset: dict):
    plan = plan_launch(session, SH, preset)
    code = await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    return plan.job, code


def _paths(job) -> set:
    return {a.path for a in job.artifact_delta.artifacts}


def _log(session: TargetSession, job) -> str:
    """The primary log as the record names it: relative inside the target
    folder, absolute when a log destination puts it outside."""
    return recorded_path(session.target_dir, job.log_path)


# ---- The rule --------------------------------------------------------------


def test_scan_is_off_unless_the_recipe_asks_for_it():
    assert scans_workspace({"id": "x"}) is False
    assert scans_workspace({"id": "x", "scan": True}) is True
    # `is True`, like `outdir:` — a truthy string is a mistake, not an opt-in.
    assert scans_workspace({"id": "x", "scan": "yes"}) is False


# ---- Concurrency -----------------------------------------------------------


@pytest.mark.asyncio
async def test_overlapping_runs_do_not_claim_each_others_files(tmp_workspace: Path):
    """The regression this replaces: both runs used to list both logs."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    slow = {"id": "slow", "flags": "-c 'echo one; sleep 0.4'"}
    fast = {"id": "fast", "flags": "-c 'echo two'"}

    (job_a, code_a), (job_b, code_b) = await asyncio.gather(_run(session, slow), _run(session, fast))
    assert code_a == 0 and code_b == 0

    assert _paths(job_a) == {_log(session, job_a)}
    assert _paths(job_b) == {_log(session, job_b)}
    assert _paths(job_a).isdisjoint(_paths(job_b))

    # And the archive agrees: one file each, not two.
    records = json.loads((session.target_dir / "session.json").read_text(encoding="utf-8"))
    assert [len(r["artifacts"]) for r in records] == [1, 1]


# ---- What each mode records ------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_owns_its_outdir(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    job, code = await _run(session, {"id": "w", "flags": """-c 'echo hi; echo x > "$OUTDIR/x.txt"'"""})
    assert code == 0

    paths = [a.path for a in job.artifact_delta.artifacts]
    assert paths[0] == _log(session, job)                         # primary log sorts first
    assert set(paths) == {_log(session, job), f"raw/{job.out_dir.name}/x.txt"}


@pytest.mark.asyncio
async def test_a_file_written_outside_outdir_needs_scan(tmp_workspace: Path):
    """The trade: a tool writing into the working directory is invisible by
    default, and `scan: true` is what finds it."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    # cwd is the target folder, so these land beside raw/, not in $OUTDIR. The
    # two runs write different names on purpose: same name and same length is a
    # file the mtime/size diff can miss when both writes fall in one clock tick,
    # which is the imprecision `scan` is documented to have.
    quiet, _ = await _run(session, {"id": "q", "flags": "-c 'echo hi > quiet.txt'"})
    assert _paths(quiet) == {_log(session, quiet)}

    scanned, _ = await _run(session, {"id": "s", "flags": "-c 'echo hi > scanned.txt'", "scan": True})
    assert "scanned.txt" in _paths(scanned)
    assert _log(session, scanned) in _paths(scanned)
    # The earlier run's files were already there and untouched, so they are not
    # claimed — a scan is bounded by the run, even if not by ownership.
    assert "quiet.txt" not in _paths(scanned)
    assert _log(session, quiet) not in _paths(scanned)


@pytest.mark.asyncio
async def test_scan_still_reaches_outside_the_archive(tmp_path: Path, tmp_workspace: Path):
    """`--artifact-root` puts the log and $OUTDIR outside the target folder;
    the scan path snapshots that root too, and records absolute paths."""
    elsewhere = tmp_path / "elsewhere"
    session = TargetSession(
        target="10.0.0.1", workspace_dir=tmp_workspace, artifact_root=str(elsewhere)
    )
    job, code = await _run(
        session, {"id": "w", "flags": """-c 'echo hi; echo x > "$OUTDIR/x.txt"'""", "scan": True}
    )
    assert code == 0

    scope = elsewhere / "10.0.0.1"
    assert job.out_dir.parent == scope                # the run dir sits under the log destination
    assert _paths(job) == {str(job.log_path), str(job.out_dir / "x.txt")}
    assert all(Path(p).is_absolute() for p in _paths(job))


# ---- The run always owns its own log ---------------------------------------


@pytest.mark.parametrize("scan", [False, True], ids=["default", "scan"])
@pytest.mark.asyncio
async def test_a_silent_run_still_records_its_log(tmp_workspace: Path, scan: bool):
    """A job that prints nothing leaves a 0-byte log with the mtime it was born
    with — it is created at plan time, before the scan's snapshot. The diff is
    right that nothing changed; the log is still this run's artifact."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    preset = {"id": "silent", "flags": "-c 'exit 0'", **({"scan": True} if scan else {})}

    job, code = await _run(session, preset)
    assert code == 0
    assert job.log_path.stat().st_size == 0
    assert _paths(job) == {_log(session, job)}


# ---- A shared $OUTDIR is still attributed per step -------------------------


@pytest.mark.asyncio
async def test_a_chain_step_does_not_claim_the_previous_steps_output(tmp_workspace: Path):
    """Chain steps deliberately share one `$OUTDIR` — that is how a step consumes
    what the one before it produced. Each still records only what it wrote."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    stamp = "20260916T120000"

    one = plan_launch(session, SH, {"id": "one", "flags": """-c 'echo one > "$OUTDIR/one.txt"'"""},
                      stamp=stamp)
    await run_job(one.command, one.job, session, lambda t, s: None, env=one.env)

    # Sharing is deliberate and explicit: the chain driver hands every later
    # step the first step's directory, which is what these two stand in for.
    two = plan_launch(session, SH, {"id": "two", "flags": """-c 'echo two > "$OUTDIR/two.txt"'"""},
                      stamp=stamp, out_dir=one.job.out_dir)
    await run_job(two.command, two.job, session, lambda t, s: None, env=two.env)

    assert one.job.out_dir == two.job.out_dir            # the shared dir, as designed
    assert (two.job.out_dir / "one.txt").is_file()       # step 2 can still read step 1's file

    shared = one.job.out_dir.name
    assert _paths(one.job) == {_log(session, one.job), f"raw/{shared}/one.txt"}
    assert _paths(two.job) == {_log(session, two.job), f"raw/{shared}/two.txt"}


@pytest.mark.asyncio
async def test_a_rewritten_file_is_claimed_by_the_run_that_rewrote_it(tmp_workspace: Path):
    """The flip side: a later step that overwrites an earlier one's file did
    write it, and records it."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    stamp = "20260916T130000"

    first = plan_launch(session, SH, {"id": "a", "flags": """-c 'echo short > "$OUTDIR/f.txt"'"""},
                        stamp=stamp)
    await run_job(first.command, first.job, session, lambda t, s: None, env=first.env)

    again = plan_launch(session, SH,
                        {"id": "b", "flags": """-c 'echo a-much-longer-line > "$OUTDIR/f.txt"'"""},
                        stamp=stamp, out_dir=first.job.out_dir)
    await run_job(again.command, again.job, session, lambda t, s: None, env=again.env)

    assert f"raw/{first.job.out_dir.name}/f.txt" in _paths(again.job)
