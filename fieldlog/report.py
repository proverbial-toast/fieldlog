"""Markdown run reports from a target's session.json: pure rendering, no printing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

DEFAULT_TAIL = 40

# Exit codes the runner assigns a meaning to; everything else is just a number.
EXIT_NOTES = {
    124: "timeout",
    130: "interrupted",
}


def load_runs(target_dir: Path) -> List[dict]:
    """Run records from session.json, newest last. A missing or corrupt
    manifest reads as no runs, the same way `history` tolerates it."""
    manifest = Path(target_dir) / "session.json"
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(parsed, list):
        return []
    return [r for r in parsed if isinstance(r, dict)]


def run_number(record: dict) -> Optional[int]:
    """Numeric run id, or None when the record has an unparseable one."""
    try:
        return int(str(record.get("id", "")).strip())
    except (TypeError, ValueError):
        return None


def human_size(num_bytes: object) -> str:
    """B / KB / MB with one decimal — sizes are for scanning, not accounting."""
    try:
        size = float(num_bytes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "0 B"
    if size < 1024:
        return f"{int(size)} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def exit_label(code: object, interrupted: bool = False) -> str:
    """Plain reading of an exit code, for headings. `interrupted` is the
    operator's SIGINT, which the code itself no longer implies."""
    try:
        value = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(code)
    note = "interrupted" if interrupted else EXIT_NOTES.get(value)
    return f"{value} ({note})" if note else str(value)


def exit_cell(code: object, interrupted: bool = False) -> str:
    """Table form: a clean run stays quiet, a failure is bold. An interrupt
    annotates the code without making a 0 read as a failure."""
    label = exit_label(code, interrupted)
    return label if exit_label(code) == "0" else f"**{label}**"


def record_interrupted(record: dict) -> bool:
    """A record carries the flag only when the operator asked for the stop."""
    return bool(record.get("interrupted"))


def format_time(stamp: object) -> str:
    """`2026-09-12T12:00:49.734145` -> `2026-09-12 12:00:49`, as stored (localtime)."""
    text = str(stamp or "").strip()
    if not text:
        return "—"
    head = text.replace("T", " ")
    for cut in ("+", "Z"):
        idx = head.find(cut, 10)
        if idx != -1:
            head = head[:idx]
    return head.split(".")[0].strip() or "—"


def format_duration(seconds: object) -> str:
    try:
        return f"{float(seconds):g}s"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


def escape_cell(text: object) -> str:
    """A pipe in a command would end the table cell early."""
    return str(text or "").replace("|", "\\|")


def resolve_log(target_dir: Path, record: dict) -> Optional[Path]:
    """Locate a run's primary log.

    `artifact_log` was written absolute by newer runs and relative to the cwd
    the run was launched from by older ones; as a last resort the artifact
    layout puts it in the target's raw/ folder under the same name.
    """
    target_dir = Path(target_dir)
    raw = str(record.get("artifact_log", "") or "").strip()
    if not raw:
        return None

    candidates = [
        Path(raw),
        target_dir.parent.parent / raw,
        target_dir / "raw" / Path(raw).name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def display_path(target_dir: Path, path: Path) -> str:
    """Paths read best relative to the target folder; absolute when outside it."""
    try:
        return path.resolve().relative_to(Path(target_dir).resolve()).as_posix()
    except ValueError:
        return str(path)


def _fence(body: str) -> str:
    """A fence longer than any backtick run inside, so tool output can't break out."""
    longest = 0
    current = 0
    for char in body:
        current = current + 1 if char == "`" else 0
        longest = max(longest, current)
    return "`" * max(3, longest + 1)


def _code_block(body: str, lang: str = "") -> List[str]:
    fence = _fence(body)
    return [f"{fence}{lang}", body.rstrip("\n"), fence]


def read_log_lines(path: Path) -> List[str]:
    """Logs are ANSI-stripped on write; undecodable bytes are replaced, never fatal."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if lines and not lines[-1].strip():
        lines.pop()
    return lines


def _artifact_line(artifact: dict) -> str:
    path = artifact.get("path", "")
    size = human_size(artifact.get("bytes", 0))
    if artifact.get("binary"):
        return f"- `{path}` · binary · {size}"
    lines = artifact.get("lines")
    if lines is None:
        return f"- `{path}` · {size}"
    return f"- `{path}` · {lines} lines · {size}"


def _step_table(record: dict, steps: List[dict]) -> List[str]:
    """A chain record has no log of its own; its steps each have theirs."""
    lines = ["| Step | Recipe | Exit |", "|------|--------|------|"]
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        lines.append(
            f"| {index} | {escape_cell(step.get('recipe', 'unknown'))} "
            f"| {exit_cell(step.get('exit_code', 0))} |"
        )
    stopped = str(record.get("stopped_at") or "").strip()
    if stopped:
        lines += ["", f"stopped at `{escape_cell(stopped)}`"]
    return lines


def _render_output(target_dir: Path, record: dict, tail: int, full: bool) -> List[str]:
    steps = record.get("steps")
    if isinstance(steps, list) and steps:
        return _step_table(record, steps)

    raw = str(record.get("artifact_log", "") or "").strip()
    if not raw:
        return ["No log recorded for this run."]

    log_path = resolve_log(target_dir, record)
    if log_path is None:
        return [f"log not found: `{raw}`"]

    # A run whose primary artifact is binary (pcap, tarball) has nothing to quote.
    name = Path(raw).name
    for artifact in record.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("binary") and Path(str(artifact.get("path", ""))).name == name:
            return [f"Output: binary log `{display_path(target_dir, log_path)}`, not shown."]

    try:
        lines = read_log_lines(log_path)
    except OSError as exc:
        return [f"log not readable: `{raw}` ({exc})"]

    if not lines:
        return ["Output: empty log."]

    total = len(lines)
    if full or tail <= 0 or total <= tail:
        heading = f"Output ({total} lines):"
        shown = lines
    else:
        omitted = total - tail
        heading = f"Output (last {tail} of {total} lines, {omitted} omitted):"
        shown = lines[-tail:]

    return [heading, ""] + _code_block("\n".join(shown), "text")


def _scope_line(runs: List[dict]) -> str:
    """The latest run's scope; the rest of the environment block is noise."""
    env = {}
    for record in reversed(runs):
        candidate = record.get("environment")
        if isinstance(candidate, dict):
            env = candidate
            break

    fields = [
        ("target", env.get("TARGET", "")),
        ("dns", env.get("TARGET_HOST", "")),
        ("interface", env.get("IFACE", "")),
        ("lhost", env.get("LHOST", "")),
    ]
    parts = [f"{label} `{value}`" for label, value in fields if str(value or "").strip()]
    return " · ".join(parts)


def render_report(
    target_dir: Path,
    runs: List[dict],
    *,
    tail: int = DEFAULT_TAIL,
    full: bool = False,
) -> str:
    """The whole report as Markdown: summary table first, then a section per run."""
    target_dir = Path(target_dir)
    workspace = target_dir.parent
    lines: List[str] = [f"# {target_dir.name}", ""]

    summary = f"Workspace `{workspace.resolve()}` · {len(runs)} run{'' if len(runs) == 1 else 's'}"
    if runs:
        first = format_time(runs[0].get("start_time"))
        last = format_time(runs[-1].get("end_time") or runs[-1].get("start_time"))
        summary += f" · {first} to {last}"
    lines += [summary, ""]

    if not runs:
        lines += ["No runs recorded.", ""]
        return "\n".join(lines)

    scope = _scope_line(runs)
    if scope:
        lines += [f"Scope (from the latest run): {scope}", ""]

    lines += [
        "| # | Recipe | Started | Duration | Exit | Files |",
        "|---|--------|---------------|----------|------|-------|",
    ]
    for record in runs:
        artifacts = record.get("artifacts") or []
        lines.append(
            f"| {escape_cell(record.get('id', '??'))} "
            f"| {escape_cell(record.get('recipe', 'unknown'))} "
            f"| {format_time(record.get('start_time'))} "
            f"| {format_duration(record.get('duration_sec'))} "
            f"| {exit_cell(record.get('exit_code', 0), record_interrupted(record))} "
            f"| {len(artifacts)} |"
        )
    lines.append("")

    for record in runs:
        rid = record.get("id", "??")
        recipe = record.get("recipe", "unknown")
        label = exit_label(record.get("exit_code", 0), record_interrupted(record))
        lines += [f"## #{rid} · {recipe} · exit {label}", ""]
        lines += _code_block(str(record.get("command", "") or ""))
        lines.append("")

        started = format_time(record.get("start_time"))
        duration = format_duration(record.get("duration_sec"))
        log_path = resolve_log(target_dir, record)
        raw_log = str(record.get("artifact_log", "") or "")
        shown_log = display_path(target_dir, log_path) if log_path else raw_log
        meta = f"Started {started} · {duration}"
        if shown_log:
            meta += f" · log `{shown_log}`"
        lines += [meta, ""]

        artifacts = [a for a in (record.get("artifacts") or []) if isinstance(a, dict)]
        if artifacts:
            lines.append("Artifacts:")
            lines += [_artifact_line(a) for a in artifacts]
            lines.append("")

        lines += _render_output(target_dir, record, tail, full)
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
