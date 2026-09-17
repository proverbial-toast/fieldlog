"""`$OUTDIR` belongs to one run, so two runs a second apart cannot overwrite
each other's side files. A chain is the one deliberate exception: its steps all
write into the first step's directory."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import List

import pytest

from fieldlog.chain import run_chain
from fieldlog.launch import LaunchPlan, plan_launch
from fieldlog.recipes import find_chain, load_catalog
from fieldlog.state import TargetSession, run_stamp

NOOP_TOOL = {"id": "true", "bin": "true"}
NOOP_PRESET = {"id": "noop", "flags": "-c 1"}

CHAIN_YAML = """
    recipes:
      - id: a
        bin: true
        presets:
          - id: x
            flags: ""
      - id: b
        bin: true
        presets:
          - id: x
            flags: ""
      - id: c
        bin: true
        presets:
          - id: x
            flags: ""
    chains:
      - id: three
        steps:
          - a/x
          - b/x
          - c/x
    """


def _catalog(tmp_path: Path, yaml_text: str):
    """A catalog from one base file, with nothing else on disk scanned."""
    base = tmp_path / "base.yaml"
    base.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(base=base, dropin_dir=dropins)


def test_two_runs_in_the_same_second_get_their_own_outdir(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)

    first = plan_launch(session, NOOP_TOOL, NOOP_PRESET)
    second = plan_launch(session, NOOP_TOOL, NOOP_PRESET)

    assert first.job.out_dir != second.job.out_dir
    assert first.job.out_dir.name.endswith(f"_{first.job.id}")
    assert second.job.out_dir.name.endswith(f"_{second.job.id}")


@pytest.mark.asyncio
async def test_a_chain_shares_its_first_steps_outdir(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CHAIN_YAML)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    seen: List[Path] = []

    async def run_step(plan: LaunchPlan) -> int:
        # Nothing has to run: what a step writes is not what is under test.
        seen.append(plan.job.out_dir)
        return 0

    result = await run_chain(session, cat, find_chain(cat, "three"), run_step=run_step)

    assert len(seen) == 3
    assert set(seen) == {seen[0]}
    assert result.record["out_dir"] == str(seen[0])


@pytest.mark.asyncio
async def test_a_run_after_a_chain_does_not_land_in_the_chains_outdir(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CHAIN_YAML)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    shared: List[Path] = []

    async def run_step(plan: LaunchPlan) -> int:
        shared.append(plan.job.out_dir)
        return 0

    await run_chain(session, cat, find_chain(cat, "three"), run_step=run_step)
    after = plan_launch(session, NOOP_TOOL, NOOP_PRESET)

    assert after.job.out_dir not in shared


def test_the_step_records_agree_with_the_chain_summary(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.cli import build_parser, handle_run

    cat = _catalog(tmp_path, CHAIN_YAML)
    args = build_parser().parse_args(
        ["run", "three", "-t", "10.0.0.1", "-w", str(tmp_workspace), "-q"]
    )
    assert handle_run(args, cat) == 0

    runs = json.loads((tmp_workspace / "10.0.0.1" / "session.json").read_text(encoding="utf-8"))
    steps = [r for r in runs if r["recipe"] != "chain/three"]
    summary = next(r for r in runs if r["recipe"] == "chain/three")
    assert {r["out_dir"] for r in steps} == {summary["out_dir"]}
    # The shared directory is the first step's, so it carries that step's number.
    assert summary["out_dir"].endswith(f"_{steps[0]['id']}")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through an unwritable directory")
def test_the_raw_fallback_keeps_a_chains_steps_in_one_dir(tmp_path: Path, tmp_workspace: Path):
    """An unwritable log destination sends both the log and `$OUTDIR` back to
    raw/. The fallback moves the shared directory, it does not rename it — or a
    chain's steps would scatter exactly where the archive is already degraded."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    session = TargetSession(
        target="10.0.0.1", workspace_dir=tmp_workspace, artifact_root=str(elsewhere)
    )
    elsewhere.chmod(0o500)
    try:
        stamp = run_stamp()
        first = plan_launch(session, NOOP_TOOL, NOOP_PRESET, stamp=stamp)
        second = plan_launch(
            session, NOOP_TOOL, NOOP_PRESET, stamp=stamp, out_dir=first.job.out_dir
        )
    finally:
        elsewhere.chmod(0o700)

    assert first.job.out_dir == second.job.out_dir
    assert first.job.out_dir.parent == session.raw_dir
