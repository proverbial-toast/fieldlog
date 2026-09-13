"""Run a catalogued chain: its recipes in order, against one scope.

Every step shares the chain's `$OUTDIR` and is planned lazily, so a chain that
halts never reserves a run number for a step that did not run. The chain's own
summary number is claimed last, which keeps the step numbers contiguous. How a
step actually runs is the front-end's business: the CLI streams it to the
terminal, the TUI opens a tab for it, and both hand that in as `run_step`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from fieldlog.launch import LaunchPlan, next_run_id, plan_launch
from fieldlog.recipes import Catalog, chain_arrow, chain_steps
from fieldlog.runner import _write_record, build_env, manifest_environment
from fieldlog.state import TargetSession, run_stamp

# Given a planned step, run it and come back with its exit code.
RunStep = Callable[[LaunchPlan], Awaitable[int]]


@dataclass
class ChainResult:
    record: dict            # the chain summary record, as archived
    exit_code: int


async def run_chain(
    session: TargetSession,
    catalog: Catalog,
    chain: dict,
    *,
    run_step: RunStep,
    timeout: Optional[float] = None,
    flags_overrides: Optional[Dict[str, str]] = None,
) -> ChainResult:
    """Run `chain` step by step, stopping at the first failure unless that step
    says `continue: true`. An interrupt always stops it, whatever the step says.

    `flags_overrides` maps a recipe key to an unresolved template, so the
    operator's per-recipe args edits apply inside a chain too.
    """
    steps = chain_steps(catalog, chain)
    stamp = run_stamp()
    start = time.time()
    overrides = flags_overrides or {}

    out_dir = session.log_dir() + stamp
    records: List[dict] = []
    stopped_at: Optional[str] = None
    exit_code = 0

    for index, (tool, preset, keep_going) in enumerate(steps, start=1):
        key = f"{tool['id']}/{preset.get('id', 'default')}"
        plan = plan_launch(
            session, tool, preset,
            flags_override=overrides.get(key),
            timeout=timeout,
            stamp=stamp,
            chain={"id": chain["id"], "step": index, "of": len(steps)},
        )
        out_dir = str(plan.job.out_dir)
        code = await run_step(plan)
        records.append({"id": plan.job.id, "recipe": key, "exit_code": code})

        if plan.job.interrupted:
            # Ctrl+C is about the chain, not just the step it landed on.
            exit_code, stopped_at = 130, key
            break
        if code != 0:
            if exit_code == 0:
                exit_code = code
            if not keep_going:
                stopped_at = key
                break

    end = time.time()
    run_id = next_run_id(session)
    record = {
        "id": run_id,
        "recipe": f"chain/{chain['id']}",
        "command": f"chain {chain['id']}: {chain_arrow(chain, mark_continue=False)}",
        "steps": records,
        "stopped_at": stopped_at,
        "exit_code": exit_code,
        "environment": manifest_environment(build_env(session, Path(out_dir), run_id)),
        "artifact_log": "",
        "out_dir": out_dir,
        "start_time": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "end_time": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "duration_sec": round(end - start, 2),
        "artifacts": [],
    }
    _write_record(session, record)
    return ChainResult(record=record, exit_code=exit_code)
