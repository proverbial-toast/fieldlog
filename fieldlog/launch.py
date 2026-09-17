"""Turn a recipe and the current scope into a job ready to run.

The CLI and the TUI both launch through `plan_launch`, so the run number,
paths, substituted command and child env are worked out in one place. Neither
front-end builds a command itself; a dry run just prints the plan.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from fieldlog.archive import next_run_number, reserve_run_number
from fieldlog.recipes import format_command, parse_rule, scans_workspace, success_codes, writes_outdir
from fieldlog.runner import build_env, exec_form
from fieldlog.state import ActiveJob, TargetSession, prepare_job_paths, resolve_flags


@dataclass
class LaunchPlan:
    job: ActiveJob
    command: str                              # exactly what runs, timeout wrapper included
    env: Dict[str, str]
    timeout: Optional[float] = None
    warnings: List[str] = field(default_factory=list)


def next_run_id(session: TargetSession, reserve: bool = True) -> str:
    """The next run number for this target, zero-padded.

    With `reserve` the number is claimed under the archive lock, so no other
    launch, in this process or another, can get it. Without, it is a preview.
    """
    target_dir = session.target_dir
    number = reserve_run_number(target_dir) if reserve else next_run_number(target_dir)
    return f"{number:02d}"


def plan_launch(
    session: TargetSession,
    tool: dict,
    preset: dict,
    *,
    flags_override: Optional[str] = None,
    extra_args: str = "",
    timeout: Optional[float] = None,
    dry_run: bool = False,
    stamp: Optional[str] = None,
    out_dir: Optional[Path] = None,
    chain: Optional[dict] = None,
    note: str = "",
) -> LaunchPlan:
    """Everything needed to run `tool`/`preset` against `session`.

    `flags_override` is a TUI args edit and is an unresolved template, exactly
    like a preset's flags, so an edit made under one scope still runs against
    the scope in force now. Unless `dry_run`, the run number is reserved and
    the log file and (when the command writes there) `$OUTDIR` are created; a
    dry run touches nothing on disk. `stamp`, `out_dir` and `chain` are set by
    the chain driver, so every step of one chain shares the first step's
    `$OUTDIR` and says so in the manifest. `note` is the operator's `--note`,
    carried onto the record.
    """
    tool_id = tool["id"]
    preset_id = preset.get("id", "default")
    run_id = next_run_id(session, reserve=not dry_run)
    log_path, out_dir, root, stamp = prepare_job_paths(
        session, tool_id, preset_id, run_id, create=not dry_run, stamp=stamp, out_dir=out_dir
    )

    template = preset.get("flags", "") if flags_override is None else flags_override
    flags = resolve_flags(session, template, out_dir=str(out_dir))
    if extra_args.strip():
        flags = f"{flags} {extra_args.strip()}".strip()
    command = format_command(preset.get("bin", tool.get("bin", tool_id)), flags)

    warnings: List[str] = []
    # $OUTDIR must exist before a command that writes into it starts. Extra args
    # reach the shell unsubstituted, so the template is what is checked.
    if not dry_run and writes_outdir(preset, f"{template} {extra_args}"):
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            warnings.append(f"could not create {out_dir}")

    if timeout:
        # ponytail: coreutils timeout, so the job exits 124 on its own and run_job records it
        # coreutils timeout signals the whole process group on expiry, so the
        # sh -c wrapper (kept for pipes / ${VAR:-default}) is cleaned up too.
        # The inner shell execs a simple command for the same reason run_job
        # does: the tool's own exit code is what timeout then reports.
        command = f"timeout -k 5 {timeout:g}s sh -c {shlex.quote(exec_form(command))}"

    job = ActiveJob(
        id=run_id,
        recipe_id=tool_id,
        name=f"{tool.get('bin', tool_id)}/{preset_id} #{run_id}",
        log_path=log_path,
        variant_id=preset_id,
        command=command,
        root=root,
        stamp=stamp,
        out_dir=out_dir,
        chain=chain,
        parse_rule=parse_rule(preset),
        success_codes=success_codes(preset),
        scan_workspace=scans_workspace(preset),
        note=note,
    )
    return LaunchPlan(job, command, build_env(session, out_dir, run_id), timeout, warnings)
