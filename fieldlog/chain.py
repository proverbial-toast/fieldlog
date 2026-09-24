"""Run a catalogued chain: its recipes in order, against one scope.

Every step shares the first step's `$OUTDIR` and is planned lazily, so a chain
that halts never reserves a run number for a step that did not run. The chain's own
summary number is claimed last, which keeps the step numbers contiguous. How a
step actually runs is the front-end's business: the CLI streams it to the
terminal, the TUI opens a tab for it, and both hand that in as `run_step`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from fieldlog.archive import append_record, manifest_environment
from fieldlog.launch import LaunchPlan, next_run_id, plan_launch
from fieldlog.recipes import Catalog, chain_arrow, chain_steps, run_passed
from fieldlog.runner import build_env
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
    note: str = "",
) -> ChainResult:
    """Run `chain` step by step, stopping at the first failure unless that step
    says `continue: true`. An interrupt always stops it, whatever the step says.

    `flags_overrides` maps a recipe key to an unresolved template, so the
    operator's per-recipe args edits apply inside a chain too. `note` is the
    operator's `--note`; it goes on the summary record, not on every step.
    """
    # The scope and the args edits as they stood at launch, which is what the
    # front-end checked the chain against. Both are copies: the TUI's live
    # session and its edits stay editable while a chain runs, and reading them
    # step by step moved later steps onto a target nobody launched them at,
    # with values the gate never saw.
    session = replace(session)
    overrides = dict(flags_overrides or {})
    steps = chain_steps(catalog, chain)
    stamp = run_stamp()
    start = time.time()

    # The first step's directory, which every later step then reuses. A chain
    # without steps is dropped at load, so the empty string is never archived.
    out_dir = ""
    shared: Optional[Path] = None
    records: List[dict] = []
    # Each step's own summary, prefixed with the tool that made it, for the
    # one line the chain amounted to.
    summaries: List[str] = []
    stopped_at: Optional[str] = None
    exit_code = 0

    for index, (tool, preset, keep_going) in enumerate(steps, start=1):
        key = f"{tool['id']}/{preset.get('id', 'default')}"
        plan = plan_launch(
            session, tool, preset,
            flags_override=overrides.get(key),
            timeout=timeout,
            stamp=stamp,
            out_dir=shared,
            chain={"id": chain["id"], "step": index, "of": len(steps)},
        )
        shared = plan.job.out_dir
        out_dir = str(plan.job.out_dir)
        code = await run_step(plan)
        # The step's own code, never a verdict — the same rule the per-run
        # record follows. `success` and `expect` ride along so a reader of the
        # summary can tell a declared success, or a missed expectation, from a
        # failure without opening the step.
        records.append({
            "id": plan.job.id,
            "recipe": key,
            "exit_code": code,
            **({"success": plan.job.success_codes} if plan.job.success_codes else {}),
            **({"summary": plan.job.summary} if plan.job.summary else {}),
            **(
                {"expect": {"pattern": plan.job.expect, "found": plan.job.expect_found}}
                if plan.job.expect else {}
            ),
        })
        if plan.job.summary:
            summaries.append(f"{tool['id']}: {plan.job.summary}")

        if plan.job.interrupted:
            # Ctrl+C is about the chain, not just the step it landed on.
            exit_code, stopped_at = 130, key
            break
        if not run_passed(code, plan.job.success_codes, plan.job.expect_found):
            if exit_code == 0:
                # A step that exited 0 and missed its expectation still did not
                # pass, and the chain must not claim 0 for a chain that failed.
                exit_code = code if code != 0 else 1
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
        # What the chain amounted to: its steps' summaries in order. A step
        # without a `parse:` rule contributes nothing. `fields` are not joined
        # here — two steps of one recipe would collide, and a trend over a
        # step's fields is `history --recipe <step>` on its own records.
        **({"summary": " → ".join(summaries)} if summaries else {}),
        **({"note": note} if note else {}),
        # Naive local time, as every step's own record is (see runner).
        "start_time": datetime.fromtimestamp(start).isoformat(),
        "end_time": datetime.fromtimestamp(end).isoformat(),
        "duration_sec": round(end - start, 2),
        "artifacts": [],
    }
    append_record(session.target_dir, record)
    return ChainResult(record=record, exit_code=exit_code)
