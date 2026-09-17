"""Command-line interface for fieldlog: recipe execution, listing, and inspection."""

from __future__ import annotations

import argparse
import asyncio
import difflib
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
from rich.markup import escape

from fieldlog import __version__
from fieldlog.archive import load_target_history
from fieldlog.report import DEFAULT_TAIL, load_runs, record_ok, render_report, run_number
from fieldlog.recipes import (
    Catalog,
    Verdict,
    chain_arrow,
    chain_blocked,
    chain_matches,
    chain_steps,
    check_chain,
    check_recipe,
    find_chain,
    find_recipe,
    is_blocked,
    format_command,
    is_tool_installed,
    load_catalog,
    run_succeeded,
    score,
    search,
    steps_label,
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
    "doctor",
    "check",
}
ROOT_FLAGS = {"-h", "--help", "-v", "--version"}

# Rich reads `[fieldlog]`, or any `[word]` in a command, as a style tag and
# prints nothing for it — so everything printed from data goes through escape().
TAG = escape("[fieldlog]")


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

    if first in ("check",):
        return "cli", ["doctor"] + argv[1:]

    if first in ("list", "show", "run", "history", "report", "doctor") or first in ROOT_FLAGS:
        return "cli", argv

    # Any other bare token defaults to prepending 'run'
    return "cli", ["run"] + argv


def subcommand_hint(spec: str) -> str:
    """A nudge when an unknown recipe is really a fat-fingered subcommand.

    Any unrecognised first token is dispatched to `run` — that is what makes
    `fieldlog nmap` work, and it is also what turns `fieldlog repot` into a
    lookup for a recipe named `repot`. Only reached once the lookup has already
    failed, so a working shorthand never sees this.
    """
    near = difflib.get_close_matches(spec.strip().lower(), sorted(SUBCOMMANDS), n=1, cutoff=0.8)
    return f" Did you mean `fieldlog {near[0]}`?" if near else ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fieldlog",
        description="fieldlog: run catalogued network diagnostics against a target and keep a log of every run",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    # list
    list_p = subparsers.add_parser("list", help="List tools and chains; name a tool to see its recipes", aliases=["recipes", "ls"])
    list_p.add_argument("query", nargs="?", default="", help="A tool id to list its recipes, or a search over tool, preset name and flags")
    list_p.add_argument("-r", "--runnable", action="store_true", help="Only show tools installed in $PATH")
    list_p.add_argument("-q", "--names", action="store_true", help="Output bare recipe IDs (one per line)")
    list_p.add_argument("--json", action="store_true", help="Output recipe catalog as JSON")
    list_p.add_argument("-V", "--verbose", action="store_true", help="Show every recipe and its flags")

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
    run_p.add_argument("target", nargs="?", default="", help="Target IP, CIDR subnet, hostname, or ssh user@host")
    run_p.add_argument("-t", "--target", dest="target_flag", default="", help="Target IP, CIDR subnet, hostname, or ssh user@host")
    run_p.add_argument("-H", "--host", "--hostname", dest="host", default="", help="Hostname for $HOST")
    run_p.add_argument("-i", "-I", "--interface", default="", help="Interface to bind to ($IFACE)")
    run_p.add_argument("-l", "--lhost", default="", help="Local host address ($LHOST)")
    run_p.add_argument("-w", "--workspace", default="", help="Target workspace root (default: ./targets)")
    run_p.add_argument("--artifact-root", default="", help="Log destination (default: <workspace>/<target>/raw)")
    run_p.add_argument("-n", "--dry-run", action="store_true", help="Print resolved command and env without executing")
    run_p.add_argument("--extra-args", default="", help="Append extra flags to command")
    run_p.add_argument("--note", default="", help="Free text stored on the run record, e.g. why this run was made")
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

    # doctor
    doc_p = subparsers.add_parser(
        "doctor",
        help="Report which recipes and chains can run against a scope, and what is missing",
        aliases=["check"],
    )
    doc_p.add_argument("target", nargs="?", default="", help="Target IP, CIDR, hostname or ssh user@host")
    doc_p.add_argument("-t", "--target", dest="target_flag", default="", help="Target (or pass it as the argument)")
    doc_p.add_argument("-H", "--host", "--hostname", dest="host", default="", help="DNS name ($HOST)")
    doc_p.add_argument("-i", "-I", "--interface", default="", help="Interface ($IFACE); default eth0")
    doc_p.add_argument("-l", "--lhost", default="", help="Local address ($LHOST)")
    doc_p.add_argument("-v", "--verbose", action="store_true", help="Show every preset's status and reason")
    doc_p.add_argument("--json", action="store_true", help="Emit the report as JSON")

    # tui
    tui_p = subparsers.add_parser("tui", help="Launch interactive Textual TUI")
    tui_p.add_argument("target", nargs="?", default="", help="Target IP, CIDR subnet, hostname, or ssh user@host")
    tui_p.add_argument("-t", "--target", dest="target_flag", default="", help="Target IP, CIDR subnet, hostname, or ssh user@host")
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


def filter_chains(catalog: Catalog, query: str, runnable_only: bool) -> List[dict]:
    """Chains surviving the same query / runnable filters as tools."""
    out = []
    for chain in catalog.chains:
        if query and not chain_matches(chain, query):
            continue
        if runnable_only and not chain_runnable(catalog, chain):
            continue
        out.append(chain)
    return out


def recipe_missing(tool: dict, preset: dict) -> bool:
    """The recipe's binary is not in $PATH — the one blocker `list` can know."""
    return not is_tool_installed(preset.get("bin", tool.get("bin", tool["id"])))


# The narrowest id / name columns `list` uses, and the widest a name may push them.
MIN_WIDTHS = (12, 34)
NAME_CAP = 44


def clip(text: str, width: int) -> str:
    """`text` cut to `width` columns, the cut marked with an ellipsis."""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def column_widths(rows: List[dict]) -> Tuple[int, int]:
    """(id, name) widths fitting every row, so a long chain name cannot push its
    step count out of line. Names wider than NAME_CAP are clipped instead."""
    id_width = max([MIN_WIDTHS[0]] + [len(r["id"]) for r in rows])
    name_width = max([MIN_WIDTHS[1]] + [len(r.get("name", "")) for r in rows])
    return id_width, min(name_width, NAME_CAP)


def tool_line(tool: dict, widths: Tuple[int, int] = MIN_WIDTHS) -> str:
    """A tool as the TUI's recipe tree shows it: id, name, variant count or n/a."""
    tid = escape(tool["id"].ljust(widths[0]))
    name = escape(clip(tool.get("name", ""), widths[1]).ljust(widths[1]))
    if is_tool_installed(tool.get("bin", tool["id"])):
        return f"  [bold]{tid}[/bold] {name} [dim]{len(tool.get('presets', []))}v[/dim]"
    return f"  [dim]{tid} {name} n/a[/dim]"


def recipe_line(tool: dict, preset: dict, verbose: bool, indent: str = "  ", mark: str = "") -> str:
    """A recipe as one row: its ID (plus a chain step's `?`) and name, and its
    flags under -V."""
    key = escape(f"{tool['id']}/{preset['id']}{mark}".ljust(22))
    name = preset.get("name", preset["id"])
    if verbose:
        rest = f"{escape(name.ljust(30))} [dim]{escape(preset.get('flags', ''))}[/dim]"
    else:
        rest = escape(name)
    if recipe_missing(tool, preset):
        return f"{indent}[dim]{key} {rest}[/dim]"
    return f"{indent}[green]{key}[/green] {rest}"


def chain_line(chain: dict, verbose: bool, widths: Tuple[int, int] = MIN_WIDTHS) -> str:
    """A chain as one row: id, name and step count — its steps under -V."""
    cid = escape(chain["id"].ljust(widths[0]))
    name = escape(clip(chain.get("name", chain["id"]), widths[1]).ljust(widths[1]))
    if verbose:
        tail = f"[green]{escape(chain_arrow(chain))}[/green]"
    else:
        tail = f"[dim]{steps_label(chain)}[/dim]"
    return f"  [bold]{cid}[/bold] {name} {tail}"


def print_chain(console: Console, catalog: Catalog, chain: dict, verbose: bool) -> None:
    """One chain and its steps in order — what `list <chain>` digs into."""
    console.print(
        f"[bold]{escape(chain['id'])}[/bold]  {escape(chain.get('name', chain['id']))}  "
        f"[dim]{steps_label(chain)} · stops at the first failure, except where marked ?[/dim]"
    )
    for index, (tool, preset, keep_going) in enumerate(chain_steps(catalog, chain), start=1):
        mark = "?" if keep_going else ""
        console.print(recipe_line(tool, preset, verbose, indent=f"  {index} ", mark=mark))


def print_tool(console: Console, tool: dict, verbose: bool) -> None:
    """One tool and its recipes — what `list <tool>` digs into."""
    bin_name = tool.get("bin", tool["id"])
    if is_tool_installed(bin_name):
        badge = f"[green]✓ {escape(bin_name)} in $PATH[/green]"
    else:
        badge = f"[red]✗ {escape(bin_name)} not in $PATH[/red]"
    console.print(f"[bold]{escape(tool['id'])}[/bold]  {escape(tool.get('name', ''))}  {badge}")
    for preset in tool.get("presets", []):
        console.print(recipe_line(tool, preset, verbose))


def handle_list(args: argparse.Namespace, catalog: Catalog) -> int:
    query = (args.query or "").strip().lower()
    runnable_only = getattr(args, "runnable", False)
    names_only = getattr(args, "names", False)
    as_json = getattr(args, "json", False)
    verbose = getattr(args, "verbose", False)

    # Filter tools & presets
    filtered_tools: List[dict] = []
    for tool in catalog.tools:
        bin_name = tool.get("bin", tool.get("id", ""))
        installed = is_tool_installed(bin_name)
        if runnable_only and not installed:
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

    chains = filter_chains(catalog, query, runnable_only)

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
                "bin": t.get("bin", t.get("id", "")),
                "installed": t.get("installed", False),
                "presets": json_presets,
            })
        for c in chains:
            json_data.append({
                "kind": "chain",
                "id": c["id"],
                "name": c.get("name", c["id"]),
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

    console = Console()

    if query:
        # An exact tool or chain id digs into it, as selecting it in the TUI does.
        tool = next((t for t in catalog.tools if t["id"].lower() == query), None)
        if tool is not None:
            print_tool(console, tool, verbose)
            return 0
        chain = next((c for c in catalog.chains if c["id"].lower() == query), None)
        if chain is not None:
            print_chain(console, catalog, chain, verbose)
            return 0

        # Any other query: ranked recipe rows, as the TUI's filter shows them.
        hits = search(catalog.tools, query, recipe_missing, hide_missing=runnable_only, limit=catalog.total_variants)
        for t, p, _missing in hits:
            console.print(recipe_line(t, p, verbose))
        widths = column_widths(chains)
        for c in chains:
            console.print(chain_line(c, verbose, widths))
        if not hits and not chains:
            console.print(f"[dim]no recipes or chains match '{escape(query)}'[/dim]")
        return 0

    # Default: one row per tool, installed first, as the TUI's recipe tree.
    tools = sorted(filtered_tools, key=lambda t: (not t["installed"], t.get("bin", t["id"])))
    if tools:
        console.print("[bold dim]RECIPES[/bold dim]")
    widths = column_widths(tools + chains)
    for t in tools:
        console.print(tool_line(t, widths))
        if verbose:
            for p in t["presets"]:
                console.print(recipe_line(t, p, True, indent="    "))
    if chains:
        console.print("\n[bold dim]CHAINS[/bold dim]")
    for c in chains:
        console.print(chain_line(c, verbose, widths))

    installed =sum(1 for t in tools if t["installed"])
    variants = sum(len(t["presets"]) for t in tools)
    console.print(
        f"\n[dim]{len(tools)} tools · {variants} recipes · {installed} installed · "
        f"{len(chains)} chain{'' if len(chains) == 1 else 's'}[/dim]"
    )
    if not verbose:
        console.print("[dim]list <tool> for its recipes · show <recipe> for detail · -V for everything[/dim]")
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
    console.print(f"\n[bold green]Chain: {escape(chain['id'])}[/bold green]")
    console.print(f"  [bold]Name:[/bold]        {escape(chain.get('name', chain['id']))}")
    console.print(
        f"  [bold]Steps:[/bold]       {len(steps)} "
        f"[dim]· stops at the first failure, except where marked ?[/dim]\n"
    )
    for index, (tool, preset, keep_going) in enumerate(steps, start=1):
        rkey = f"{tool['id']}/{preset['id']}" + ("?" if keep_going else "")
        bin_name = preset.get("bin", tool.get("bin", tool["id"]))
        command = format_command(bin_name, resolve_flags(session, preset.get("flags", "")))
        mark = "[green]✓[/green]" if is_tool_installed(bin_name) else "[red]✗[/red]"
        console.print(f"  {index} {mark} [green]{escape(rkey.ljust(22))}[/green] [dim]{escape(command)}[/dim]")
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
    console.print(f"\n[bold green]Recipe: {escape(rkey)}[/bold green]")
    console.print(f"  [bold]Name:[/bold]        {escape(preset.get('name', preset['id']))}")
    console.print(f"  [bold]Tool:[/bold]        {escape(tool['id'])} ({escape(tool.get('name', ''))})")
    if installed:
        console.print(f"  [bold]Binary:[/bold]      [green]✓ {escape(bin_name)} (installed in $PATH)[/green]")
    else:
        console.print(f"  [bold]Binary:[/bold]      [red]✗ {escape(bin_name)} (missing from $PATH)[/red]")

    # The preset's own bin, when it has one — the binary that actually runs.
    console.print(f"\n[bold]Raw Flags:[/bold]\n  {escape(format_command(bin_name, raw_flags))}")
    console.print(f"\n[bold]Resolved (sample/target preview):[/bold]\n  {escape(format_command(bin_name, resolved))}")
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
        console.print(f"\n[bold cyan]{TAG}[/bold cyan] Spawning [bold]{escape(job.name)}[/bold]")
        console.print(f"  [dim]Command:[/dim]     {escape(command)}")
        console.print(f"  [dim]Log:[/dim]         {escape(str(job.log_path))}")
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
            # echo on: nothing here renders the operator's reply, so the pty's
            # own echo is what makes it visible and what puts it in the log.
            # The tool keeps control of it, so a password prompt stays hidden.
            run_job(command, job, session, sink, on_state=None, env=plan.env, echo=is_tty)
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
    # The verdict is for display here; what this returns is always the tool's
    # own code, so a chain step's record and its own run record agree. The
    # caller turns the code into fieldlog's exit status (see handle_run).
    ok = run_succeeded(code, job.success_codes)

    if as_json:
        if not emit_json:
            return code
        if job.record is not None:
            # What was archived, not a second rendering of it: the two used to
            # disagree on both the key names and what they left out.
            print(json.dumps(job.record, indent=2))
            return code
        # No manifest was written, so the run never reached the archive (an
        # execution error). Say that, and no more: a fuller shape here would be
        # a second schema for a consumer to come to depend on.
        print(json.dumps({
            "id": job.id,
            "recipe": f"{job.recipe_id}/{job.variant_id}",
            "command": command,
            "exit_code": code,
            "duration_sec": elapsed,
            "error": True,
        }, indent=2))
        return code

    if not quiet:
        console = Console()
        console.print(f"[dim]{'─' * 80}[/dim]")
        status_style = "green" if ok else "red"
        status_label = f"[DONE:{code}]" if ok else f"[FAIL:{code}]"
        if job.interrupted:
            status_label += " (interrupted)"
        art_count = delta.total_files if delta else 1
        lines_count = delta.total_lines if delta else job.lines_count
        bytes_count = delta.total_bytes if delta else job.bytes_count

        console.print(
            f"[{status_style}]{TAG} {escape(status_label)} {escape(job.name)} | {elapsed}s | "
            f"+{art_count} files (+{lines_count} lines, {bytes_count} B)[/{status_style}]"
        )
        if delta and delta.artifacts:
            console.print("  [bold]Artifacts:[/bold]")
            for a in delta.artifacts:
                console.print(f"    - {escape(a.path)} ({a.lines or 0} lines, {a.bytes} B)")

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
    console.print(f"  [bold]Recipe:[/bold]      {escape(f'{job.recipe_id}/{job.variant_id}')} #{job.id}")
    console.print(f"  [bold]Command:[/bold]     {escape(plan.command)}")
    console.print("  [bold]Environment:[/bold]")
    for k in ("TARGET", "TARGET_IP", "TARGET_HOST", "HOST", "LHOST", "IFACE", "OUT_DIR", "OUTDIR", "RUN_ID"):
        console.print(f"    {k}={escape(str(plan.env[k]))}")
    console.print(f"  [bold]Primary Log:[/bold] {escape(str(job.log_path))}")
    console.print(f"  [bold]Out Dir:[/bold]     {escape(str(job.out_dir))}")
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
        shared: Optional[Path] = None           # the first step's $OUTDIR, as run_chain shares it
        for index, (tool, preset, _cont) in enumerate(steps, start=1):
            plan = plan_launch(
                session, tool, preset, timeout=timeout, dry_run=True, stamp=stamp,
                out_dir=shared,
                chain={"id": chain["id"], "step": index, "of": len(steps)},
            )
            shared = plan.job.out_dir
            print_dry_run(plan, f"step {index}/{len(steps)}")
        return 0

    blocked, reason = chain_blocked(catalog, chain, session)
    if blocked:
        sys.stderr.write(f"Error: Cannot run chain '{chain['id']}': {reason}\n")
        return 1

    console = Console()

    async def run_step(plan: LaunchPlan) -> int:
        info = plan.job.chain or {}
        if not quiet and not as_json:
            console.print(
                f"\n[bold cyan]{TAG}[/bold cyan] chain {escape(chain['id'])} · "
                f"step {info.get('step')}/{info.get('of')} "
                f"{escape(f'{plan.job.recipe_id}/{plan.job.variant_id}')} #{plan.job.id}"
            )
        for warning in plan.warnings:
            sys.stderr.write(f"Warning: {warning}\n")
        return await execute_cli_job(plan, session, quiet=quiet, as_json=as_json, emit_json=False)

    result = asyncio.run(
        run_chain(
            session, catalog, chain, run_step=run_step, timeout=timeout,
            note=getattr(args, "note", "").strip(),
        )
    )
    record = result.record

    if as_json:
        print(json.dumps(record, indent=2))
    elif not quiet:
        ran = len(record["steps"])
        if record["stopped_at"]:
            last_code = record["steps"][-1]["exit_code"] if record["steps"] else result.exit_code
            console.print(
                f"[red]{TAG} chain {escape(chain['id'])} · stopped at step {ran} "
                f"({escape(record['stopped_at'])} exit {last_code})[/red]"
            )
        else:
            style = "green" if result.exit_code == 0 else "red"
            console.print(
                f"[{style}]{TAG} chain {escape(chain['id'])} · {ran}/{len(steps)} steps · "
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
        sys.stderr.write(f"Error: {err}{subcommand_hint(args.recipe)}\n")
        return 1

    session = run_session(args)

    blocked, reason = is_blocked(tool, preset, session)
    if blocked:
        sys.stderr.write(f"Error: Cannot run '{tool['id']}/{preset['id']}': {reason}\n")
        return 1

    dry_run = getattr(args, "dry_run", False)
    plan = plan_launch(
        session,
        tool,
        preset,
        extra_args=getattr(args, "extra_args", ""),
        timeout=getattr(args, "timeout", None),
        dry_run=dry_run,
        # Stripped here: `--note "  "` is not a note, and an empty one keeps
        # the key off the record entirely.
        note=getattr(args, "note", "").strip(),
    )

    if dry_run:
        print_dry_run(plan)
        return 0

    for warning in plan.warnings:
        sys.stderr.write(f"Warning: {warning}\n")

    code = asyncio.run(
        execute_cli_job(
            plan,
            session,
            quiet=getattr(args, "quiet", False),
            as_json=getattr(args, "json", False),
        )
    )
    # The recipe's verdict is fieldlog's exit status; the archive keeps the
    # tool's own code either way, so nothing in it is fabricated.
    return 0 if run_succeeded(code, plan.job.success_codes) else code


def target_folders(workspace: Path) -> List[Path]:
    """Every target folder in a workspace, in name order."""
    if not workspace.is_dir():
        return []
    return sorted(
        (d for d in workspace.iterdir() if d.is_dir() and not d.name.startswith(".")),
        key=lambda p: p.name,
    )


def find_target_dir(workspace: Path, name: str) -> Optional[Path]:
    """The archive folder for `name`, which may be the folder itself or the
    target its runs were made against.

    A folder is named for the dns name when one was set, so an operator who ran
    `-t 10.10.11.50 -H box.htb` and later asks for the address would otherwise
    be told there is nothing there. The folder name is tried first and answers
    without reading anything; only a miss goes looking through the manifests.
    """
    name = (name or "").strip()
    if not name:
        return None
    direct = workspace / scope_dir(name)
    if (direct / "session.json").is_file():
        return direct
    for folder in target_folders(workspace):
        for record in reversed(load_runs(folder)):
            env = record.get("environment")
            if isinstance(env, dict) and name in (env.get("TARGET", ""), env.get("TARGET_HOST", "")):
                return folder
    return None


def no_such_target(workspace: Path, name: str) -> str:
    """The message for a target nothing in the workspace matches.

    A folder that exists but holds no manifest is its own case: the operator
    named the right place, there is just nothing recorded in it yet.
    """
    direct = workspace / scope_dir(name)
    if direct.is_dir():
        return f"No runs recorded in {direct} (no session.json)\n"
    known = [d.name for d in target_folders(workspace) if (d / "session.json").is_file()]
    if not known:
        return f"No target folders with runs under {workspace}\n"
    listed = ", ".join(known[:6]) + (" …" if len(known) > 6 else "")
    return f"No runs for '{name}' in {workspace}. Target folders: {listed}\n"


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
        console.print(f"[bold cyan]Available Targets in {escape(str(workspace))}:[/bold cyan]")
        for d in sorted(subdirs, key=lambda p: p.name):
            hist = load_target_history(d)
            console.print(f"  [bold]{escape(d.name)}[/bold] ({hist.total_runs} runs)")
        return 0

    target_dir = find_target_dir(workspace, target)
    if target_dir is None:
        sys.stderr.write(no_such_target(workspace, target))
        return 1
    manifest = target_dir / "session.json"

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
    console.print(f"\n[bold cyan]Run History for {escape(target)} ({escape(str(target_dir))}):[/bold cyan]\n")
    for r in runs:
        if not isinstance(r, dict):
            continue
        rid = r.get("id", "??")
        recipe = r.get("recipe", "unknown")
        code = r.get("exit_code", 0)
        dur = r.get("duration_sec", 0)
        start = r.get("start_time", "")
        artifacts = r.get("artifacts", [])
        ok = record_ok(r)
        status_col = "green" if ok else "red"
        # Said in words as well as colour: history is piped and redirected.
        verdict = " (ok)" if ok and code != 0 else ""
        flag = " interrupted" if r.get("interrupted") else ""
        console.print(
            f"  [bold]#{escape(str(rid))}[/bold] [{status_col}]{escape(str(recipe))}[/{status_col}] "
            f"| exit {code}{verdict}{flag} | {dur}s | {escape(str(start))}"
        )
        summary = str(r.get("summary", "") or "")
        if summary:
            console.print(f"      [cyan]{escape(summary)}[/cyan]")
        note = str(r.get("note", "") or "")
        if note:
            console.print(f"      [dim]✎ {escape(note)}[/dim]")
        if artifacts:
            for a in artifacts:
                console.print(f"      ↳ {escape(str(a.get('path')))} ({a.get('bytes', 0)} B)")
    return 0


def handle_report(args: argparse.Namespace) -> int:
    target = (getattr(args, "target_flag", "") or getattr(args, "target", "")).strip()
    workspace = Path(args.workspace) if getattr(args, "workspace", None) else Path("./targets")

    if not target:
        sys.stderr.write("Error: report needs a target folder. Try `fieldlog history` to list them.\n")
        return 1

    target_dir = find_target_dir(workspace, target)
    if target_dir is None:
        sys.stderr.write(no_such_target(workspace, target))
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


def doctor_session(args: argparse.Namespace) -> TargetSession:
    """The scope `doctor` checks the catalog against — scope flags only."""
    return TargetSession(
        target=(getattr(args, "target_flag", "") or getattr(args, "target", "")).strip(),
        hostname=getattr(args, "host", "").strip(),
        interface=(getattr(args, "interface", "") or "eth0").strip(),
        lhost=getattr(args, "lhost", "").strip(),
    )


# Terse, CLI-neutral phrasing for a scope value that is simply not set; the
# footer says how to set each. A verdict is only `missing` for these three
# kinds, so there is no fourth note to write.
_DOCTOR_SCOPE_NOTE = {
    "target": "needs a target",
    "dns": "needs a dns name",
    "lhost": "needs a local address",
}


def doctor_mark(verdict: Verdict, ready_note: str) -> Tuple[str, str, str]:
    """(mark, style, note) for one line. A missing binary is a ✗ — the gap doctor
    is for; a scope-only block is a ◐ (otherwise ready, just needs a scope value),
    with doctor's own terse note for the scope kind (the footer says how to set it)."""
    if not verdict.blocked:
        return "✓", "green", ready_note
    head = verdict.reason.split(" · ")[0]
    if verdict.kind == "binary":
        return "✗", "red", head
    # The terse note stands in for a value that is simply missing. A value that
    # is set and refused — unsafe characters, a leading `-` — has to show its
    # own reason, or doctor reads as "needs a target" for a target that is there.
    if not verdict.missing:
        return "◐", "yellow", head
    return "◐", "yellow", _DOCTOR_SCOPE_NOTE.get(verdict.kind, head)


def doctor_scan(catalog: Catalog, session: TargetSession) -> dict:
    """Per-tool, per-preset and per-chain runnability, plus rolled-up counts.

    Every verdict comes from check_recipe / check_chain, so doctor and an actual
    run can never disagree on whether something is runnable.
    """
    tools: List[dict] = []
    recipes_ready = 0
    recipes_total = 0
    missing: set = set()                        # bin names not on $PATH, deduped
    needs = {"target": 0, "dns": 0, "lhost": 0, "interface": 0}
    refused = 0                                 # scope values that are set and rejected

    for tool in catalog.tools:
        rows = []
        for preset in tool.get("presets", []):
            verdict = check_recipe(tool, preset, session)
            recipes_total += 1
            if verdict.blocked:
                if verdict.kind == "binary":
                    missing.add(preset.get("bin", tool.get("bin", tool["id"])))
                elif verdict.kind in needs and verdict.missing:
                    needs[verdict.kind] += 1
                elif verdict.kind in needs:
                    # Set and refused: the footer's "set a target" would send
                    # the operator to fill in what is already filled in.
                    refused += 1
            else:
                recipes_ready += 1
            rows.append({
                "preset": preset, "blocked": verdict.blocked,
                "reason": verdict.reason, "verdict": verdict,
            })
        ready = sum(1 for r in rows if not r["blocked"])
        tools.append({"tool": tool, "rows": rows, "ready": ready, "total": len(rows)})

    chains = []
    chains_ready = 0
    for chain in catalog.chains:
        verdict = check_chain(catalog, chain, session)
        if not verdict.blocked:
            chains_ready += 1
        chains.append({
            "chain": chain, "blocked": verdict.blocked,
            "reason": verdict.reason, "verdict": verdict,
        })

    tools_installed = sum(1 for t in catalog.tools if is_tool_installed(t.get("bin", t["id"])))
    return {
        "tools": tools,
        "chains": chains,
        "recipes_ready": recipes_ready,
        "recipes_total": recipes_total,
        "tools_installed": tools_installed,
        "tools_total": len(catalog.tools),
        "chains_ready": chains_ready,
        "chains_total": len(catalog.chains),
        "refused": refused,
        "missing": missing,
        "needs": needs,
    }


def doctor_report_json(scan: dict, session: TargetSession) -> dict:
    """The scan as a machine-readable record."""
    return {
        "scope": {
            "target": session.target,
            "host": session.dns_name,
            "interface": session.interface,
            "lhost": session.effective_lhost(),
        },
        "tools": [
            {
                "id": t["tool"]["id"],
                "bin": t["tool"].get("bin", t["tool"]["id"]),
                "ready": t["ready"],
                "total": t["total"],
                "presets": [
                    {
                        "recipe": f"{t['tool']['id']}/{r['preset']['id']}",
                        "runnable": not r["blocked"],
                        "reason": r["reason"],
                    }
                    for r in t["rows"]
                ],
            }
            for t in scan["tools"]
        ],
        "chains": [
            {
                "id": c["chain"]["id"],
                "runnable": not c["blocked"],
                "reason": c["reason"],
            }
            for c in scan["chains"]
        ],
        "summary": {
            "tools_installed": scan["tools_installed"],
            "tools_total": scan["tools_total"],
            "recipes_ready": scan["recipes_ready"],
            "recipes_total": scan["recipes_total"],
            "chains_ready": scan["chains_ready"],
            "chains_total": scan["chains_total"],
            "missing_binaries": sorted(scan["missing"]),
            "needs": scan["needs"],
            "refused": scan["refused"],
        },
    }


def handle_doctor(args: argparse.Namespace, catalog: Catalog) -> int:
    session = doctor_session(args)
    scan = doctor_scan(catalog, session)

    if getattr(args, "json", False):
        print(json.dumps(doctor_report_json(scan, session), indent=2))
        return 0

    console = Console()
    verbose = getattr(args, "verbose", False)

    scope_bits = []
    if session.target:
        scope_bits.append(f"target {escape(session.target)}")
    if session.dns_name:
        scope_bits.append(f"dns {escape(session.dns_name)}")
    ip = session.effective_lhost()
    scope_bits.append(f"iface {escape(session.interface)}" + (f" ({escape(ip)})" if ip else " (no address)"))
    console.print(f"[bold cyan]{TAG}[/bold cyan] doctor · {' · '.join(scope_bits)}\n")

    id_w = max([12] + [len(t["tool"]["id"]) for t in scan["tools"]] + [len(c["chain"]["id"]) for c in scan["chains"]])

    console.print("[bold dim]RECIPES[/bold dim]")
    for t in scan["tools"]:
        tool = t["tool"]
        bin_name = tool.get("bin", tool["id"])
        if t["ready"] == t["total"]:
            mark, style, note = "✓", "green", f"{t['ready']}/{t['total']} ready"
        elif t["ready"] == 0 and not is_tool_installed(bin_name):
            mark, style, note = "✗", "red", f"{bin_name} not in $PATH"
        else:
            mark, style, note = "◐", "yellow", f"{t['ready']}/{t['total']} ready"
        tid = escape(tool["id"].ljust(id_w))
        name = escape(clip(tool.get("name", ""), NAME_CAP))
        console.print(f"  [{style}]{mark}[/{style}] [bold]{tid}[/bold] {name}  [dim]{escape(note)}[/dim]")
        if verbose:
            for r in t["rows"]:
                mark, style, note = doctor_mark(r["verdict"], "ready")
                key = escape(f"{tool['id']}/{r['preset']['id']}")
                console.print(f"      [{style}]{mark}[/{style}] {key}  [dim]{escape(note)}[/dim]")

    if scan["chains"]:
        console.print("\n[bold dim]CHAINS[/bold dim]")
        for c in scan["chains"]:
            # A chain's own reason names the step that blocks it, which the
            # per-kind note would drop — "needs a target" for which of five.
            mark, style, _note = doctor_mark(c["verdict"], c["reason"])
            cid = escape(c["chain"]["id"].ljust(id_w))
            console.print(f"  [{style}]{mark}[/{style}] [bold]{cid}[/bold] [dim]{escape(c['reason'])}[/dim]")

    console.print(
        f"\n[dim]{scan['recipes_ready']}/{scan['recipes_total']} recipes ready · "
        f"{scan['tools_installed']}/{scan['tools_total']} tools installed · "
        f"{scan['chains_ready']}/{scan['chains_total']} chains ready[/dim]"
    )
    if scan["missing"]:
        items = " · ".join(escape(b) for b in sorted(scan["missing"]))
        console.print(f"[red]missing:[/red] {items}")
    hints = []
    if scan["needs"]["target"]:
        hints.append(f"set a target (-t) to unlock {scan['needs']['target']}")
    if scan["needs"]["dns"]:
        hints.append(f"set a dns name (-H) to unlock {scan['needs']['dns']}")
    if scan["needs"]["lhost"]:
        hints.append(f"pick an interface with an address (-i/-l) to unlock {scan['needs']['lhost']}")
    if scan["refused"]:
        hints.append(f"{scan['refused']} refused by the scope value (-v to see why)")
    if hints:
        console.print(f"[dim]{escape(' · '.join(hints))}[/dim]")
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
    # An interface neither passed nor remembered stays unset, so the TUI picks
    # one with an address; a named one is kept even without.
    scope.setdefault("interface", "")
    session = TargetSession(**scope)
    session.workspace_dir = workspace
    return session


def run_cli(argv: Optional[List[str]] = None) -> int:
    action, normalized_argv = dispatch_argv(argv)
    if action == "tui":
        normalized_argv = ["tui"] + normalized_argv

    parser = build_parser()
    args = parser.parse_args(normalized_argv)
    # Read only where it is read: `history` and `report` work off the archive
    # and never look at a recipe, and the TUI loads its own in FieldlogApp —
    # so an eager load here cost those three a parse of every yaml, twice over
    # for the TUI, which is the command a bare `fieldlog` runs.
    if args.command in ("list", "recipes", "ls"):
        return handle_list(args, load_catalog())
    if args.command in ("show", "info"):
        return handle_show(args, load_catalog())
    if args.command in ("run", "exec"):
        return handle_run(args, load_catalog())
    if args.command in ("history", "log", "runs"):
        return handle_history(args)
    if args.command == "report":
        return handle_report(args)
    if args.command in ("doctor", "check"):
        return handle_doctor(args, load_catalog())
    if args.command == "tui":
        from fieldlog.app import FieldlogApp

        return FieldlogApp(tui_session(args)).run() or 0

    parser.print_help()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return run_cli(argv)


