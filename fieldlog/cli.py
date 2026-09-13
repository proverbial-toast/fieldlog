"""Command-line interface for fieldlog: recipe execution, listing, and inspection."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import shutil
import signal
import struct
import sys
import termios
import time
import tty
from pathlib import Path
from typing import List, Optional, Tuple

from rich.console import Console
from rich.table import Table
from rich.text import Text

from fieldlog import __version__
from fieldlog.archive import load_target_history
from fieldlog.report import DEFAULT_TAIL, load_runs, render_report, run_number
from fieldlog.recipes import (
    Catalog,
    KNOWN_INSTALL,
    chain_arrow,
    chain_blocked,
    chain_matches,
    chain_steps,
    find_chain,
    find_recipe,
    is_blocked,
    format_command,
    is_tool_installed,
    load_catalog,
    score,
    search,
)
from fieldlog.chain import run_chain
from fieldlog.launch import LaunchPlan, plan_launch
from fieldlog.runner import interrupt_job, run_job
from fieldlog.state import (
    DEFAULT_ARTIFACT_ROOT,
    SCOPE_VARS,
    TargetSession,
    load_last_scope,
    resolve_flags,
    run_stamp,
    scope_dir,
    template_vars,
)

SUBCOMMANDS = {
    "list",
    "ls",
    "recipes",
    "show",
    "info",
    "run",
    "exec",
    "history",
    "log",
    "runs",
    "report",
}
ROOT_FLAGS = {"-h", "--help", "-v", "--version"}


def dispatch_argv(argv: Optional[List[str]] = None) -> Tuple[str, List[str]]:
    """Determine whether to launch TUI or CLI, normalizing aliases and shorthand.
    Returns (action, normalized_argv) where action is 'tui' or 'cli'.
    """
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    if not argv:
        return "tui", []

    first = argv[0]
    if first == "tui":
        return "tui", argv[1:]

    if first in ("recipes", "ls"):
        return "cli", ["list"] + argv[1:]

    if first in ("exec",):
        return "cli", ["run"] + argv[1:]

    if first in ("info",):
        return "cli", ["show"] + argv[1:]

    if first in ("log", "runs"):
        return "cli", ["history"] + argv[1:]

    if first in ("list", "show", "run", "history", "report") or first in ROOT_FLAGS:
        return "cli", argv

    # Any other bare token defaults to prepending 'run'
    return "cli", ["run"] + argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fieldlog",
        description="fieldlog: Operator-driven network diagnostic & enumeration archive",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    # list
    list_p = subparsers.add_parser("list", help="List available recipes and tools", aliases=["recipes", "ls"])
    list_p.add_argument("query", nargs="?", default="", help="Optional search query matching tool, preset, or flags")
    list_p.add_argument("-c", "--category", default="", help="Filter recipes by category")
    list_p.add_argument("-r", "--runnable", action="store_true", help="Only show tools installed in $PATH")
    list_p.add_argument("--tools", action="store_true", help="Show tools overview without presets")
    list_p.add_argument("-q", "--names", action="store_true", help="Output bare recipe IDs (one per line)")
    list_p.add_argument("--json", action="store_true", help="Output recipe catalog as JSON")
    list_p.add_argument("-V", "--verbose", action="store_true", help="Display full preset command flags")

    # show
    show_p = subparsers.add_parser("show", help="Show detailed recipe specification and preview", aliases=["info"])
    show_p.add_argument("recipe", help="Recipe specifier (e.g. 'ping/quick' or 'ping')")
    show_p.add_argument("-t", "--target", default="", help="Target IP or subnet to preview flag resolution")
    show_p.add_argument("-H", "--host", "--hostname", default="", dest="host", help="Hostname to preview flag resolution")
    show_p.add_argument("-i", "-I", "--interface", default="", help="Network interface to preview flag resolution")
    show_p.add_argument("-l", "--lhost", default="", help="Local host address to preview flag resolution")

    # run
    run_p = subparsers.add_parser("run", help="Execute a recipe against a target", aliases=["exec"])
    run_p.add_argument("recipe", help="Recipe specifier (e.g. 'ping/quick' or 'ping')")
    run_p.add_argument("target", nargs="?", default="", help="Target IP, CIDR subnet, or hostname")
    run_p.add_argument("-t", "--target", dest="target_flag", default="", help="Target IP, CIDR subnet, or hostname")
    run_p.add_argument("-H", "--host", "--hostname", dest="host", default="", help="Hostname for $HOST")
    run_p.add_argument("-i", "-I", "--interface", default="", help="Interface to bind to ($IFACE)")
    run_p.add_argument("-l", "--lhost", default="", help="Local host address ($LHOST)")
    run_p.add_argument("-w", "--workspace", default="", help="Target workspace root (default: ./targets)")
    run_p.add_argument("--artifact-root", default="", help="Log destination (default: <workspace>/<target>/raw)")
    run_p.add_argument("-n", "--dry-run", action="store_true", help="Print resolved command and env without executing")
    run_p.add_argument("--extra-args", default="", help="Append extra flags to command")
    run_p.add_argument("-q", "--quiet", action="store_true", help="Suppress banner/summary; stream only tool output")
    run_p.add_argument("--json", action="store_true", help="Emit completion manifest as JSON")
    run_p.add_argument("--timeout", type=float, default=None, help="Maximum execution duration in seconds")

    # history
    hist_p = subparsers.add_parser("history", help="View execution history for a target", aliases=["log", "runs"])
    hist_p.add_argument("target", nargs="?", default="", help="Target name or folder (e.g. '10.10.11.50')")
    hist_p.add_argument("-t", "--target", dest="target_flag", default="", help="Target name or folder")
    hist_p.add_argument("-w", "--workspace", default="", help="Target workspace root (default: ./targets)")
    hist_p.add_argument("--json", action="store_true", help="Emit history records as JSON")

    # report
    report_p = subparsers.add_parser("report", help="Render a target's runs as a Markdown report")
    report_p.add_argument("target", nargs="?", default="", help="Target name or folder (e.g. '10.10.11.50')")
    report_p.add_argument("-t", "--target", dest="target_flag", default="", help="Target name or folder")
    report_p.add_argument("-w", "--workspace", default="", help="Target workspace root (default: ./targets)")
    report_p.add_argument("-o", "--output", default="-", help="Write Markdown to FILE ('-' for stdout)")
    report_p.add_argument("--tail", type=int, default=DEFAULT_TAIL, help=f"Lines of each log to include (default: {DEFAULT_TAIL})")
    report_p.add_argument("--full", action="store_true", help="Include each log in full instead of a tail")
    report_p.add_argument("--since", default="", help="Only runs with an id at or above this number")

    # tui
    tui_p = subparsers.add_parser("tui", help="Launch interactive Textual TUI")
    tui_p.add_argument("target", nargs="?", default="", help="Target IP, CIDR subnet, or hostname")
    tui_p.add_argument("-t", "--target", dest="target_flag", default="", help="Target IP, CIDR subnet, or hostname")
    tui_p.add_argument("-H", "--host", "--hostname", dest="host", default="", help="Hostname for $HOST")
    tui_p.add_argument("-i", "-I", "--interface", default="", help="Interface to bind to ($IFACE)")
    tui_p.add_argument("-l", "--lhost", default="", help="Local host address ($LHOST; default: interface IP)")
    tui_p.add_argument("-w", "--workspace", default="", help="Target workspace root (default: ./targets)")

    return parser


def chain_runnable(catalog: Catalog, chain: dict) -> bool:
    """Every step's binary is in $PATH — the test `-r` applies to a tool, per step."""
    return all(
        is_tool_installed(p.get("bin", t.get("bin", t["id"])))
        for t, p, _cont in chain_steps(catalog, chain)
    )


def filter_chains(catalog: Catalog, query: str, cat_filter: str, runnable_only: bool) -> List[dict]:
    """Chains surviving the same query / category / runnable filters as tools."""
    out = []
    for chain in catalog.chains:
        if cat_filter and cat_filter not in chain.get("category", "").lower():
            continue
        if query and not chain_matches(chain, query):
            continue
        if runnable_only and not chain_runnable(catalog, chain):
            continue
        out.append(chain)
    return out


def handle_list(args: argparse.Namespace, catalog: Catalog) -> int:
    query = (args.query or "").strip().lower()
    cat_filter = (args.category or "").strip().lower()
    runnable_only = getattr(args, "runnable", False)
    tools_only = getattr(args, "tools", False)
    names_only = getattr(args, "names", False)
    as_json = getattr(args, "json", False)

    # Filter tools & presets
    filtered_tools: List[dict] = []
    for tool in catalog.tools:
        bin_name = tool.get("bin", tool.get("id", ""))
        installed = is_tool_installed(bin_name)
        if runnable_only and not installed:
            continue

        cat_name = tool.get("category", "")
        if cat_filter and cat_filter not in cat_name.lower():
            continue

        # Filter presets
        presets = tool.get("presets", [])
        matched_presets: List[dict] = []
        for preset in presets:
            if query:
                s = score(tool, preset, query)
                if s is None:
                    continue
            matched_presets.append(preset)

        if matched_presets:
            t_copy = dict(tool)
            t_copy["presets"] = matched_presets
            t_copy["installed"] = installed
            filtered_tools.append(t_copy)

    chains = filter_chains(catalog, query, cat_filter, runnable_only)

    if as_json:
        json_data = []
        for t in filtered_tools:
            json_presets = []
            for p in t.get("presets", []):
                flags = p.get("flags", "")
                used = template_vars(flags)
                requires = [f"${v}" for v in SCOPE_VARS if v in used]
                json_presets.append({
                    "id": p["id"],
                    "name": p.get("name", p["id"]),
                    "recipe_key": f"{t['id']}/{p['id']}",
                    "flags": flags,
                    "requires": requires,
                    "src": p.get("src", "recipes.yaml"),
                })
            json_data.append({
                "kind": "tool",
                "id": t["id"],
                "name": t.get("name", t["id"]),
                "category": t.get("category", ""),
                "bin": t.get("bin", t.get("id", "")),
                "installed": t.get("installed", False),
                "presets": json_presets,
            })
        for c in chains:
            json_data.append({
                "kind": "chain",
                "id": c["id"],
                "name": c.get("name", c["id"]),
                "category": c.get("category", "Chains"),
                "steps": [
                    {"recipe": s["recipe"], "continue": bool(s.get("continue"))}
                    for s in c.get("steps", [])
                ],
            })
        print(json.dumps(json_data, indent=2))
        return 0

    if names_only:
        for t in filtered_tools:
            for p in t.get("presets", []):
                print(f"{t['id']}/{p['id']}")
        for c in chains:
            print(c["id"])
        return 0

    if tools_only:
        table = Table(title="fieldlog Tools", show_header=True, header_style="bold cyan")
        table.add_column("TOOL", style="bold")
        table.add_column("CATEGORY")
        table.add_column("VARIANTS", justify="right")
        table.add_column("STATUS")

        for t in filtered_tools:
            bin_name = t.get("bin", t.get("id", ""))
            status = "[green]✓ installed[/green]" if t.get("installed") else f"[red]✗ missing[/red] ({KNOWN_INSTALL.get(bin_name, 'apt install ' + bin_name)})"
            table.add_row(
                t["id"],
                t.get("category", "General"),
                str(len(t.get("presets", []))),
                status,
            )
        Console().print(table)
        return 0

    # Default: Grouped category -> tool -> preset output
    console = Console()
    by_category: dict[str, List[dict]] = {}
    for t in filtered_tools:
        cat = t.get("category", "General")
        by_category.setdefault(cat, []).append(t)

    total_variants = sum(len(t.get("presets", [])) for t in filtered_tools)
    runnable_variants = sum(
        len(t.get("presets", [])) for t in filtered_tools if t.get("installed")
    )
    missing_bins = len([t for t in filtered_tools if not t.get("installed")])

    for cat_name, tools in by_category.items():
        console.print(f"\n[bold cyan]{cat_name}[/bold cyan]")
        for t in tools:
            bin_name = t.get("bin", t.get("id", ""))
            if t.get("installed"):
                bin_badge = f"[green]✓ {bin_name} in $PATH[/green]"
            else:
                hint = KNOWN_INSTALL.get(bin_name, f"apt install {bin_name}")
                bin_badge = f"[red]✗ {bin_name}: not found in $PATH — {hint}[/red]"
            console.print(f"  [bold]{t['id']}[/bold]  [dim]{t.get('name', '')}[/dim]  [{bin_badge}]")
            for p in t.get("presets", []):
                rkey = f"{t['id']}/{p['id']}"
                pname = p.get("name", p["id"])
                pflags = p.get("flags", "")
                console.print(f"    [green]{rkey:<20}[/green] [white]{pname:<25}[/white] [dim]{pflags}[/dim]")

    by_chain_category: dict[str, List[dict]] = {}
    for c in chains:
        by_chain_category.setdefault(c.get("category", "Chains"), []).append(c)
    for cat_name, items in by_chain_category.items():
        console.print(f"\n[bold cyan]{cat_name}[/bold cyan]")
        for c in items:
            console.print(
                f"  [bold]{c['id']:<10}[/bold] [white]{c.get('name', c['id']):<34}[/white] "
                f"[green]{chain_arrow(c)}[/green]"
            )

    console.print(
        f"\n[dim]{'─' * 80}[/dim]\n"
        f"[bold]Catalog:[/bold] {len(filtered_tools)} tools · {total_variants} recipes · "
        f"{runnable_variants} runnable · {missing_bins} missing binaries · "
        f"{len(chains)} chains · {len(catalog.files)} drop-in files"
    )
    return 0


def preview_session(args: argparse.Namespace) -> TargetSession:
    """The scope `show` resolves its previews against — flags only, no workspace."""
    return TargetSession(
        target=args.target.strip() if args.target else "",
        hostname=args.host.strip() if args.host else "",
        interface=args.interface.strip() if args.interface else "eth0",
        lhost=args.lhost.strip() if args.lhost else "",
    )


def handle_show_chain(chain: dict, catalog: Catalog, session: TargetSession) -> int:
    """The chain header, then each step's command as it would be run."""
    console = Console()
    steps = chain_steps(catalog, chain)
    console.print(f"\n[bold green]Chain: {chain['id']}[/bold green]")
    console.print(f"  [bold]Name:[/bold]        {chain.get('name', chain['id'])}")
    console.print(f"  [bold]Category:[/bold]    {chain.get('category', 'Chains')}")
    console.print(
        f"  [bold]Steps:[/bold]       {len(steps)} "
        f"[dim]· stops at the first failure, except where marked ?[/dim]\n"
    )
    for index, (tool, preset, keep_going) in enumerate(steps, start=1):
        rkey = f"{tool['id']}/{preset['id']}" + ("?" if keep_going else "")
        bin_name = preset.get("bin", tool.get("bin", tool["id"]))
        command = format_command(bin_name, resolve_flags(session, preset.get("flags", "")))
        mark = "[green]✓[/green]" if is_tool_installed(bin_name) else "[red]✗[/red]"
        console.print(f"  {index} {mark} [green]{rkey:<22}[/green] [dim]{command}[/dim]")
    return 0


def handle_show(args: argparse.Namespace, catalog: Catalog) -> int:
    session = preview_session(args)

    chain = find_chain(catalog, args.recipe) if "/" not in (args.recipe or "") else None
    if chain is not None:
        return handle_show_chain(chain, catalog, session)

    tool, preset, err = find_recipe(catalog, args.recipe)
    if err or not tool or not preset:
        sys.stderr.write(f"Error: {err}\n")
        return 1

    bin_name = preset.get("bin", tool.get("bin", tool.get("id", "")))
    installed = is_tool_installed(bin_name)
    raw_flags = preset.get("flags", "")
    resolved = resolve_flags(session, raw_flags)
    rkey = f"{tool['id']}/{preset['id']}"

    console = Console()
    console.print(f"\n[bold green]Recipe: {rkey}[/bold green]")
    console.print(f"  [bold]Name:[/bold]        {preset.get('name', preset['id'])}")
    console.print(f"  [bold]Tool:[/bold]        {tool['id']} ({tool.get('name', '')})")
    console.print(f"  [bold]Category:[/bold]    {tool.get('category', 'General')}")
    if installed:
        console.print(f"  [bold]Binary:[/bold]      [green]✓ {bin_name} (installed in $PATH)[/green]")
    else:
        hint = KNOWN_INSTALL.get(bin_name, f"apt install {bin_name}")
        console.print(f"  [bold]Binary:[/bold]      [red]✗ {bin_name} (missing from $PATH — {hint})[/red]")


    console.print(f"\n[bold]Raw Flags:[/bold]\n  {format_command(tool.get('bin', tool['id']), raw_flags)}")
    console.print(f"\n[bold]Resolved (sample/target preview):[/bold]\n  {format_command(tool.get('bin', tool['id']), resolved)}")
    return 0


async def execute_cli_job(
    plan: LaunchPlan,
    session: TargetSession,
    quiet: bool = False,
    as_json: bool = False,
    emit_json: bool = True,
) -> int:
    """Run one planned job on this terminal. `emit_json` is what a chain turns
    off: its steps stay silent under `--json` so the summary record is the
    only thing on stdout."""
    job, command, timeout = plan.job, plan.command, plan.timeout
    loop = asyncio.get_running_loop()
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    old_termios = None
    start_time = time.time()

    if not quiet and not as_json:
        console = Console()
        console.print(f"\n[bold cyan][fieldlog][/bold cyan] Spawning [bold]{job.name}[/bold]")
        console.print(f"  [dim]Command:[/dim]     {command}")
        console.print(f"  [dim]Log:[/dim]         {job.log_path}")
        console.print(f"[dim]{'─' * 80}[/dim]")

    def sink(text: str, _stream: str) -> None:
        if not as_json:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    stdin_reader_active = False

    def forward_stdin() -> None:
        try:
            chunk = os.read(sys.stdin.fileno(), 1024)
        except (OSError, BlockingIOError):
            chunk = b""
        if chunk and job.pty_fd is not None:
            try:
                os.write(job.pty_fd, chunk)
            except OSError:
                pass

    def on_sigint() -> None:
        interrupt_job(job)

    code = 1
    try:
        if is_tty:
            try:
                old_termios = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
                loop.add_reader(sys.stdin.fileno(), forward_stdin)
                stdin_reader_active = True
            except Exception:
                pass

        try:
            loop.add_signal_handler(signal.SIGINT, on_sigint)
        except (NotImplementedError, RuntimeError):
            pass

        run_task = asyncio.create_task(
            run_job(command, job, session, sink, on_state=None, env=plan.env)
        )

        # Propagate real terminal dimensions to PTY slave
        if is_tty:
            await asyncio.sleep(0.02)
            if job.pty_fd is not None:
                try:
                    cols, rows = shutil.get_terminal_size()
                    fcntl.ioctl(
                        job.pty_fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0),
                    )
                except (OSError, termios.error):
                    pass

        code = await run_task
        if timeout and code == 124 and not as_json:
            sys.stderr.write(f"\n[fieldlog] Job timed out after {timeout:g}s\n")

    except Exception as exc:
        code = 127
        if not as_json:
            sys.stderr.write(f"\n[fieldlog] Execution error: {exc}\n")
    finally:
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except Exception:
            pass

        if stdin_reader_active:
            try:
                loop.remove_reader(sys.stdin.fileno())
            except Exception:
                pass

        if old_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_termios)
            except Exception:
                pass

    elapsed = round(time.time() - start_time, 2)
    delta = job.artifact_delta

    if as_json:
        if not emit_json:
            return code
        manifest_record = {
            "id": job.id,
            "recipe": f"{job.recipe_id}/{job.variant_id}",
            "command": command,
            "exit_code": code,
            "duration_sec": elapsed,
            "primary_log": str(job.log_path),
            "artifacts": [
                {"path": a.path, "bytes": a.bytes, "lines": a.lines, "binary": a.binary}
                for a in (delta.artifacts if delta else [])
            ],
        }
        print(json.dumps(manifest_record, indent=2))
        return code

    if not quiet:
        console = Console()
        console.print(f"[dim]{'─' * 80}[/dim]")
        status_style = "green" if code == 0 else "red"
        status_label = f"[DONE:{code}]" if code == 0 else f"[FAIL:{code}]"
        if job.interrupted:
            status_label += " (interrupted)"
        art_count = delta.total_files if delta else 1
        lines_count = delta.total_lines if delta else job.lines_count
        bytes_count = delta.total_bytes if delta else job.bytes_count

        console.print(
            f"[{status_style}][fieldlog] {status_label} {job.name} | {elapsed}s | "
            f"+{art_count} files (+{lines_count} lines, {bytes_count} B)[/{status_style}]"
        )
        if delta and delta.artifacts:
            console.print("  [bold]Artifacts:[/bold]")
            for a in delta.artifacts:
                console.print(f"    - {a.path} ({a.lines or 0} lines, {a.bytes} B)")

    return code


def run_session(args: argparse.Namespace) -> TargetSession:
    """The scope and archive a `run` targets, from its flags and arguments."""
    return TargetSession(
        target=(getattr(args, "target_flag", "") or getattr(args, "target", "")).strip(),
        hostname=getattr(args, "host", "").strip(),
        interface=getattr(args, "interface", "") or "eth0",
        lhost=getattr(args, "lhost", ""),
        workspace_dir=Path(args.workspace) if getattr(args, "workspace", None) else Path("./targets"),
        artifact_root=getattr(args, "artifact_root", "") or DEFAULT_ARTIFACT_ROOT,
    )


def print_dry_run(plan: LaunchPlan, step: str = "") -> None:
    """What would run, where it would land, and nothing on disk to show for it."""
    job = plan.job
    console = Console()
    heading = f"── DRY-RUN PREVIEW{' · ' + step if step else ''} ──"
    console.print(f"\n[bold yellow]{heading}[/bold yellow]")
    console.print(f"  [bold]Recipe:[/bold]      {job.recipe_id}/{job.variant_id} #{job.id}")
    console.print(f"  [bold]Command:[/bold]     {plan.command}")
    console.print(f"  [bold]Environment:[/bold]")
    for k in ("TARGET", "TARGET_IP", "TARGET_HOST", "HOST", "LHOST", "IFACE", "OUT_DIR", "OUTDIR", "RUN_ID"):
        console.print(f"    {k}={plan.env[k]}")
    console.print(f"  [bold]Primary Log:[/bold] {job.log_path}")
    console.print(f"  [bold]Out Dir:[/bold]     {job.out_dir}")
    console.print(f"[bold yellow]{'─' * len(heading)}[/bold yellow]\n")


def handle_run_chain(args: argparse.Namespace, catalog: Catalog, chain: dict) -> int:
    """Run a chain's recipes in order, sharing one $OUTDIR, against one scope."""
    if getattr(args, "extra_args", "").strip():
        sys.stderr.write("Error: extra args apply to a single recipe, not a chain.\n")
        return 1

    session = run_session(args)
    steps = chain_steps(catalog, chain)
    timeout = getattr(args, "timeout", None)   # per step, as for a single recipe
    quiet = getattr(args, "quiet", False)
    as_json = getattr(args, "json", False)

    if getattr(args, "dry_run", False):
        # A preview of a chain that cannot run yet is still worth reading, so
        # the blocked check below is skipped here. Nothing is reserved.
        stamp = run_stamp()
        for index, (tool, preset, _cont) in enumerate(steps, start=1):
            plan = plan_launch(
                session, tool, preset, timeout=timeout, dry_run=True, stamp=stamp,
                chain={"id": chain["id"], "step": index, "of": len(steps)},
            )
            print_dry_run(plan, f"step {index}/{len(steps)}")
        return 0

    blocked, reason, hint = chain_blocked(catalog, chain, session)
    if blocked:
        sys.stderr.write(f"Error: Cannot run chain '{chain['id']}': {reason}\n")
        if hint:
            sys.stderr.write(f"Hint: {hint}\n")
        return 1

    console = Console()

    async def run_step(plan: LaunchPlan) -> int:
        info = plan.job.chain or {}
        if not quiet and not as_json:
            console.print(
                f"\n[bold cyan][fieldlog][/bold cyan] chain {chain['id']} · "
                f"step {info.get('step')}/{info.get('of')} "
                f"{plan.job.recipe_id}/{plan.job.variant_id} #{plan.job.id}"
            )
        for warning in plan.warnings:
            sys.stderr.write(f"Warning: {warning}\n")
        return await execute_cli_job(plan, session, quiet=quiet, as_json=as_json, emit_json=False)

    result = asyncio.run(
        run_chain(session, catalog, chain, run_step=run_step, timeout=timeout)
    )
    record = result.record

    if as_json:
        print(json.dumps(record, indent=2))
    elif not quiet:
        ran = len(record["steps"])
        if record["stopped_at"]:
            last_code = record["steps"][-1]["exit_code"] if record["steps"] else result.exit_code
            console.print(
                f"[red][fieldlog] chain {chain['id']} · stopped at step {ran} "
                f"({record['stopped_at']} exit {last_code})[/red]"
            )
        else:
            style = "green" if result.exit_code == 0 else "red"
            console.print(
                f"[{style}][fieldlog] chain {chain['id']} · {ran}/{len(steps)} steps · "
                f"exit {result.exit_code}[/{style}]"
            )
    return result.exit_code


def handle_run(args: argparse.Namespace, catalog: Catalog) -> int:
    # A chain id can never be a tool id, so trying it first is not observable.
    chain = find_chain(catalog, args.recipe) if "/" not in (args.recipe or "") else None
    if chain is not None:
        return handle_run_chain(args, catalog, chain)

    tool, preset, err = find_recipe(catalog, args.recipe)
    if err or not tool or not preset:
        sys.stderr.write(f"Error: {err}\n")
        return 1

    session = run_session(args)

    flags_template = preset.get("flags", "")
    needs_target = "TARGET" in template_vars(flags_template)
    if needs_target and not session.target:
        sys.stderr.write(
            f"Error: Recipe '{tool['id']}/{preset['id']}' requires a target IP or subnet ($TARGET).\n"
            f"Specify --target <IP> or pass [target]:\n"
            f"  fieldlog run {tool['id']}/{preset['id']} <target>\n"
        )
        return 1

    blocked, reason, hint = is_blocked(tool, preset, session)
    if blocked:
        sys.stderr.write(f"Error: Cannot run '{tool['id']}/{preset['id']}': {reason}\n")
        if hint:
            sys.stderr.write(f"Hint: {hint}\n")
        return 1

    dry_run = getattr(args, "dry_run", False)
    plan = plan_launch(
        session,
        tool,
        preset,
        extra_args=getattr(args, "extra_args", ""),
        timeout=getattr(args, "timeout", None),
        dry_run=dry_run,
    )

    if dry_run:
        print_dry_run(plan)
        return 0

    for warning in plan.warnings:
        sys.stderr.write(f"Warning: {warning}\n")

    return asyncio.run(
        execute_cli_job(
            plan,
            session,
            quiet=getattr(args, "quiet", False),
            as_json=getattr(args, "json", False),
        )
    )


def handle_history(args: argparse.Namespace) -> int:
    target = (getattr(args, "target_flag", "") or getattr(args, "target", "")).strip()
    workspace = Path(args.workspace) if getattr(args, "workspace", None) else Path("./targets")

    if not target:
        if not workspace.exists():
            sys.stderr.write(f"No targets found in {workspace}\n")
            return 1
        subdirs = [d for d in workspace.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not subdirs:
            sys.stderr.write(f"No target folders found under {workspace}\n")
            return 0
        console = Console()
        console.print(f"[bold cyan]Available Targets in {workspace}:[/bold cyan]")
        for d in sorted(subdirs, key=lambda p: p.name):
            hist = load_target_history(d)
            console.print(f"  [bold]{d.name}[/bold] ({hist.total_runs} runs)")
        return 0

    target_dir = workspace / scope_dir(target)
    manifest = target_dir / "session.json"
    if not manifest.exists():
        sys.stderr.write(f"No session.json found at {manifest}\n")
        return 1

    try:
        raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write(f"Failed to read session manifest: {exc}\n")
        return 1

    runs = raw_manifest if isinstance(raw_manifest, list) else raw_manifest.get("runs", []) if isinstance(raw_manifest, dict) else []

    if getattr(args, "json", False):
        print(json.dumps(runs, indent=2))
        return 0

    console = Console()
    console.print(f"\n[bold cyan]Run History for {target} ({target_dir}):[/bold cyan]\n")
    for r in runs:
        if not isinstance(r, dict):
            continue
        rid = r.get("id", "??")
        recipe = r.get("recipe", "unknown")
        code = r.get("exit_code", 0)
        dur = r.get("duration_sec", 0)
        start = r.get("start_time", "")
        artifacts = r.get("artifacts", [])
        status_col = "green" if code == 0 else "red"
        flag = " interrupted" if r.get("interrupted") else ""
        console.print(
            f"  [bold]#{rid}[/bold] [{status_col}]{recipe}[/{status_col}] "
            f"| exit {code}{flag} | {dur}s | {start}"
        )
        if artifacts:
            for a in artifacts:
                console.print(f"      ↳ {a.get('path')} ({a.get('bytes', 0)} B)")
    return 0


def handle_report(args: argparse.Namespace) -> int:
    target = (getattr(args, "target_flag", "") or getattr(args, "target", "")).strip()
    workspace = Path(args.workspace) if getattr(args, "workspace", None) else Path("./targets")

    if not target:
        sys.stderr.write("Error: report needs a target folder. Try `fieldlog history` to list them.\n")
        return 1

    target_dir = workspace / scope_dir(target)
    if not target_dir.is_dir():
        sys.stderr.write(f"No target folder at {target_dir}\n")
        return 1
    if not (target_dir / "session.json").exists():
        sys.stderr.write(f"No session.json found at {target_dir / 'session.json'}\n")
        return 1

    runs = load_runs(target_dir)

    since = str(getattr(args, "since", "") or "").strip()
    if since:
        try:
            floor = int(since)
        except ValueError:
            sys.stderr.write(f"Error: --since expects a run number, got '{since}'\n")
            return 1
        # An unparseable run id can't be compared, so it drops out of the window.
        runs = [r for r in runs if (run_number(r) or 0) >= floor]

    markdown = render_report(
        target_dir,
        runs,
        tail=getattr(args, "tail", DEFAULT_TAIL),
        full=getattr(args, "full", False),
    )

    destination = getattr(args, "output", "-") or "-"
    if destination == "-":
        sys.stdout.write(markdown)
        return 0

    out_path = Path(destination)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"Failed to write {out_path}: {exc}\n")
        return 1
    # stdout stays clean when writing a file, so `-o` is safe to pipe alongside.
    sys.stderr.write(f"wrote {out_path} ({len(runs)} runs)\n")
    return 0


def tui_session(args: argparse.Namespace) -> TargetSession:
    """Starting scope for the TUI: the workspace's remembered scope, with any
    flags given on the command line overriding it."""
    workspace = Path(args.workspace) if args.workspace else Path("./targets")
    scope = load_last_scope(workspace)
    given = {
        "target": args.target_flag or args.target,
        "hostname": args.host,
        "interface": args.interface,
        "lhost": args.lhost,
    }
    scope.update({k: v.strip() for k, v in given.items() if v.strip()})
    session = TargetSession(**scope) if scope else TargetSession()
    session.workspace_dir = workspace
    return session


def run_cli(argv: Optional[List[str]] = None) -> int:
    catalog = load_catalog()
    action, normalized_argv = dispatch_argv(argv)
    if action == "tui":
        normalized_argv = ["tui"] + normalized_argv

    parser = build_parser()
    args = parser.parse_args(normalized_argv)
    if args.command in ("list", "recipes", "ls"):
        return handle_list(args, catalog)
    if args.command in ("show", "info"):
        return handle_show(args, catalog)
    if args.command in ("run", "exec"):
        return handle_run(args, catalog)
    if args.command in ("history", "log", "runs"):
        return handle_history(args)
    if args.command == "report":
        return handle_report(args)
    if args.command == "tui":
        from fieldlog.app import FieldlogApp

        return FieldlogApp(tui_session(args)).run() or 0

    parser.print_help()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return run_cli(argv)


