"""Markdown run reports from a target's session.json: pure rendering, no printing."""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fieldlog.archive import UNREADABLE_MANIFEST
from fieldlog.recipes import run_passed
from fieldlog.vantage import vantage_line

DEFAULT_TAIL = 40

# Exit codes the runner assigns a meaning to; everything else is just a number.
EXIT_NOTES = {
    124: "timeout",
    130: "interrupted",
}


@dataclass(frozen=True)
class Manifest:
    """What a target's session.json holds, and anything wrong with it.

    The archive is the product, so "there are no runs" and "the runs cannot be
    read" must never come out as the same sentence. They used to: `report`
    printed `No runs recorded.` and exited 0 over a damaged manifest, while
    `history` printed the raw exception and exited 1 — two readers of one file
    with two answers, neither of them the whole one.
    """

    runs: List[dict] = field(default_factory=list)
    # One line for stderr; '' when there is nothing to say.
    problem: str = ""
    # False when session.json is there and nothing could be read out of it.
    readable: bool = True


def read_manifest(target_dir: Path) -> Manifest:
    """Read a target's session.json, saying what it could not make sense of.

    A folder with no manifest holds no runs and has no problem — nothing has
    been run against it yet, which is an answer. A manifest that is there and
    will not parse is not an answer, and says so. One that parses but carries
    entries that are not records keeps the records and reports the rest, since
    dropping them silently is how a partial archive reads as a whole one.
    """
    manifest = Path(target_dir) / "session.json"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return Manifest()
    except OSError as exc:
        return Manifest(problem=f"{manifest} cannot be read · {exc}", readable=False)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return Manifest(problem=f"{manifest} is not valid JSON · {exc}", readable=False)
    if not isinstance(parsed, list):
        return Manifest(
            problem=f"{manifest} is not a list of run records", readable=False
        )
    runs = [r for r in parsed if isinstance(r, dict)]
    dropped = len(parsed) - len(runs)
    problems = []
    if dropped:
        problems.append(
            f"{manifest}: {dropped} entr{'y is' if dropped == 1 else 'ies are'} "
            f"not a run record · skipped"
        )
    problems.extend(set_aside_notes(Path(target_dir)))
    return Manifest(runs=runs, problem=" | ".join(problems))


def set_aside_notes(target_dir: Path) -> List[str]:
    """One warning per manifest the writer set aside as unreadable.

    The runs in it are not in session.json any more, so a reader that stayed
    quiet would present the fresh manifest as the whole history of the target.
    """
    return [
        f"{path} is an earlier session.json that could not be read · "
        f"its runs are not listed here · repair it and merge it back by hand"
        for path in sorted(target_dir.glob(f"{UNREADABLE_MANIFEST}*"))
    ]


def load_runs(target_dir: Path) -> List[dict]:
    """Run records from session.json, newest last, for a caller with nothing to
    say about a manifest it cannot read. `read_manifest` is the fuller answer."""
    return read_manifest(target_dir).runs


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


def exit_label(
    code: object,
    interrupted: bool = False,
    ok: Optional[bool] = None,
    expect_found: Optional[bool] = None,
) -> str:
    """Plain reading of an exit code, for headings. `interrupted` is the
    operator's SIGINT, which the code itself no longer implies; `ok` is the
    recipe's own verdict, so a declared success does not read as a failure;
    `expect_found` is its `expect:` rule, which is why an exit 0 can still be
    the thing that failed."""
    try:
        value = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(code)
    note = "interrupted" if interrupted else EXIT_NOTES.get(value)
    if note is None and ok and value != 0:
        note = "ok"
    if note is None and expect_found is False:
        note = "expect not met"
    return f"{value} ({note})" if note else str(value)


def exit_cell(
    code: object,
    interrupted: bool = False,
    ok: Optional[bool] = None,
    expect_found: Optional[bool] = None,
) -> str:
    """Table form: a successful run stays quiet, a failure is bold. An interrupt
    annotates the code without making a 0 read as a failure."""
    label = exit_label(code, interrupted, ok, expect_found)
    good = ok if ok is not None else exit_label(code) == "0"
    return label if good else f"**{label}**"


def record_expect_found(record: dict) -> Optional[bool]:
    """What the record's `expect:` check found, None when it made none.

    Junk in the key is read as no check rather than trusted, the same way
    `record_ok` ignores a `success` that is not a list of codes.
    """
    expect = record.get("expect")
    if isinstance(expect, dict) and isinstance(expect.get("found"), bool):
        return expect["found"]
    return None


def record_ok(record: dict) -> bool:
    """Whether a run passed, by the rules its own recipe declared.

    A record carries `success:` and `expect` only when its preset set them, so
    a record from before the fields existed — or from a preset without them —
    reads as exit 0.
    """
    codes = record.get("success")
    return run_passed(
        record.get("exit_code", 0),
        codes if isinstance(codes, list) else None,
        record_expect_found(record),
    )


def record_interrupted(record: dict) -> bool:
    """A record carries the flag only when the operator asked for the stop."""
    return bool(record.get("interrupted"))


def record_kind(record: dict) -> str:
    """What a record is: a `run`, a `chain` summary, or the operator's own `note`.

    Every reader branches on this one definition rather than testing for `steps`
    or for a recipe named `note` itself. What tells a note from a run is that
    nothing ran: a run of a recipe that happens to be called `note` has a
    `command`, and a note never does.
    """
    steps = record.get("steps")
    if isinstance(steps, list) and steps:
        return "chain"
    if record.get("recipe") == "note" and "command" not in record:
        return "note"
    return "run"


def chain_outcome(record: dict, planned: int) -> str:
    """What a chain amounted to, in one line — the same words wherever it is said.

    A chain that stopped names the step it stopped on and that step's own exit
    code, because an exit of 0 that stopped a chain reads as a fieldlog bug
    unless the line also says what the step missed. `planned` is how many steps
    the chain has, which the record itself does not carry.
    """
    steps = [s for s in (record.get("steps") or []) if isinstance(s, dict)]
    ran = len(steps)
    stopped = str(record.get("stopped_at") or "").strip()
    if stopped:
        code = steps[-1].get("exit_code", 0) if steps else record.get("exit_code", 0)
        unmet = ", expect not met" if steps and record_expect_found(steps[-1]) is False else ""
        return f"stopped at step {ran} ({stopped} exit {code}{unmet})"
    return f"{ran}/{planned} steps · exit {record.get('exit_code', 0)}"


def note_line(record: dict) -> str:
    """A note's first line — what a table cell or an overview row has room for.

    The whole note is still there for the reader who opens the record; this is
    the one line that has to distinguish it from the note above it.
    """
    lines = str(record.get("note", "") or "").splitlines()
    return lines[0] if lines else ""


def format_time(stamp: object) -> str:
    """`2026-09-12T12:00:49.734145` -> `2026-09-12 12:00:49`, as stored (localtime).

    A stamp with an offset — a chain summary written before chains switched to
    localtime — is converted to local time, so it reads on its steps' clock."""
    text = str(stamp or "").strip()
    if not text:
        return "—"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = None
    if moment is not None:
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%Y-%m-%d %H:%M:%S")
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


_BACKTICK_RUN = re.compile(r"`+")


def _fence(body: str) -> str:
    """A fence longer than any backtick run inside, so tool output can't break out.

    The `in` test is the whole point: tool output has no backticks, and one C
    scan settles it. Walking the body a character at a time in Python was a
    tenth of a report's runtime.
    """
    if "`" not in body:
        return "```"
    longest = max(len(run) for run in _BACKTICK_RUN.findall(body))
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


# A log this size or smaller is read whole, exactly as every log always was.
# Reading it in one go is the faster way round up to a few MB; past that the
# allocation stops paying — measured against a 4 MB log the two are level, at
# 32 MB the streaming read is 3.5x quicker, and at 410 MB (a server recipe left
# running overnight) reading it whole cost 1.4 s and 1.1 GB of RSS to print
# 3.6 KB of report.
WHOLE_FILE_MAX = 4 * 1024 * 1024

# The first bite taken off the end of a log too big to read whole. Big enough
# to hold the default 40 lines of anything line-shaped, and grown from there.
TAIL_WINDOW = 64 * 1024


def count_log_lines(path: Path) -> int:
    """`len(read_log_lines(path))` without holding the file in memory.

    Counted with the same str.splitlines the reader uses, not by counting
    newline bytes: splitlines also breaks on \v, \f and U+2028, so a byte
    count would quietly disagree with the lines actually quoted.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    total = 0
    carry = ""       # a final part the next chunk may continue
    last = ""        # the file's last part, for the blank-line rule below
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(1 << 20)
            parts = (carry + decoder.decode(raw, not raw)).splitlines(keepends=True)
            carry = ""
            if parts and raw:
                # Mid-file, the last part may be a line the next chunk finishes
                # — and a trailing "\r" may yet turn out to be a "\r\n", which
                # is one break, not two.
                if not _line_ended(parts[-1]) or parts[-1].endswith("\r"):
                    carry = parts.pop()
            total += len(parts)
            if parts:
                last = parts[-1]
            if not raw:
                break
    # read_log_lines drops a blank final line; the count has to drop it too.
    if total and not last.strip():
        total -= 1
    return total


def _line_ended(part: str) -> bool:
    """Whether a `splitlines(keepends=True)` part carries its line break."""
    return part.splitlines()[0] != part


def read_log_tail(path: Path, tail: int) -> tuple[List[str], int]:
    """`(last `tail` lines, total lines)`, reading only the end of a large log.

    Logs up to WHOLE_FILE_MAX — and a `tail` of 0 or less, which asks for all
    of them — go through read_log_lines untouched. A bigger one is read
    backwards from its end, in a window grown until it holds enough lines: a
    log of very long lines needs a bigger bite than one of short ones.
    """
    size = path.stat().st_size
    # `tail <= 0` is the caller's way of saying "no limit", and a bounded read
    # cannot serve it — the whole file is the answer.
    if tail <= 0 or size <= WHOLE_FILE_MAX:
        lines = read_log_lines(path)
        return lines[-tail:] if 0 < tail < len(lines) else lines, len(lines)

    total = count_log_lines(path)
    window = TAIL_WINDOW
    while True:
        start = max(0, size - window)
        with open(path, "rb") as fh:
            fh.seek(start)
            text = fh.read().decode("utf-8", errors="replace")
        lines = text.splitlines()
        if start > 0:
            # The seek landed mid-line (and possibly mid-codepoint); that first
            # fragment is not a line of the log.
            lines = lines[1:]
        if lines and not lines[-1].strip():
            lines.pop()
        if len(lines) >= tail or start == 0:
            return lines[-tail:] if 0 < tail < len(lines) else lines, total
        window *= 4


def _quoted_note(note: str) -> List[str]:
    """A note as a blockquote: the operator's own prose, so it is quoted rather
    than escaped. Every line carries the marker, blank ones included, so a
    multi-line note reads as one blockquote instead of two."""
    return [f"> {line}" if line.strip() else ">" for line in note.splitlines()]


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
    lines = ["| Step | Recipe | Exit | Summary |", "|------|--------|------|---------|"]
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        summary = str(step.get("summary", "") or "")
        lines.append(
            f"| {index} | {escape_cell(step.get('recipe', 'unknown'))} "
            f"| {exit_cell(step.get('exit_code', 0), ok=record_ok(step), expect_found=record_expect_found(step))} "
            f"| {escape_cell(summary)} |"
        )
    stopped = str(record.get("stopped_at") or "").strip()
    if stopped:
        lines += ["", f"stopped at `{escape_cell(stopped)}`"]
    return lines


def _render_output(
    target_dir: Path,
    record: dict,
    tail: int,
    full: bool,
    log_path: Optional[Path] = None,
) -> List[str]:
    if record_kind(record) == "chain":
        return _step_table(record, record["steps"])

    raw = str(record.get("artifact_log", "") or "").strip()
    if not raw:
        return ["No log recorded for this run."]

    # The caller has already located the log for the run's heading; resolving it
    # again costs three more stat calls per run, for the same answer.
    if log_path is None:
        log_path = resolve_log(target_dir, record)
    if log_path is None:
        return [f"log not found: `{raw}`"]

    # A run whose primary artifact is binary (pcap, tarball) has nothing to quote.
    name = Path(raw).name
    for artifact in record.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("binary") and Path(str(artifact.get("path", ""))).name == name:
            return [f"Output: binary log `{display_path(target_dir, log_path)}`, not shown."]

    try:
        if full or tail <= 0:
            # Every line is going to be quoted, so there is nothing to save by
            # reading it in pieces.
            shown = read_log_lines(log_path)
            total = len(shown)
        else:
            shown, total = read_log_tail(log_path, tail)
    except OSError as exc:
        return [f"log not readable: `{raw}` ({exc})"]

    if not total:
        return ["Output: empty log."]

    if full or tail <= 0 or total <= tail:
        heading = f"Output ({total} lines):"
    else:
        omitted = total - tail
        heading = f"Output (last {tail} of {total} lines, {omitted} omitted):"

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
    selection: str = "",
) -> str:
    """The whole report as Markdown: summary table first, then a section per run.

    `selection` is how the caller narrowed `runs`, in words. It goes in the
    summary line because a report is read away from the command that made it:
    without it, a report of one recipe's runs is indistinguishable from a
    report of everything that was ever run against the host.
    """
    target_dir = Path(target_dir)
    workspace = target_dir.parent
    lines: List[str] = [f"# {target_dir.name}", ""]

    summary = f"Workspace `{workspace.resolve()}` · {len(runs)} run{'' if len(runs) == 1 else 's'}"
    if selection:
        summary += f" · {selection}"
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
        "| # | Recipe | Started | Duration | Exit | Files | Summary |",
        "|---|--------|---------------|----------|------|-------|---------|",
    ]
    for record in runs:
        if record_kind(record) == "note":
            # Nothing ran, so there is no code to report and no files to count;
            # what the operator wrote is the row's own summary.
            row_summary = note_line(record)
            exit_text = files_text = "—"
        else:
            # A chain summary record's own `summary` is its steps' joined, so the
            # table reads as the chain's checklist before a log is opened.
            row_summary = str(record.get("summary", "") or "")
            exit_text = exit_cell(
                record.get("exit_code", 0),
                record_interrupted(record),
                record_ok(record),
                record_expect_found(record),
            )
            files_text = str(len(record.get("artifacts") or []))
        lines.append(
            f"| {escape_cell(record.get('id', '??'))} "
            f"| {escape_cell(record.get('recipe', 'unknown'))} "
            f"| {format_time(record.get('start_time'))} "
            f"| {format_duration(record.get('duration_sec'))} "
            f"| {exit_text} "
            f"| {files_text} "
            f"| {escape_cell(row_summary.replace('`', ''))} |"
        )
    lines.append("")

    for record in runs:
        rid = record.get("id", "??")
        recipe = record.get("recipe", "unknown")

        if record_kind(record) == "note":
            # A note has no command, no log and no verdict to render: when it
            # was written, and what it says.
            lines += [f"## #{rid} · note · {format_time(record.get('start_time'))}", ""]
            lines += _quoted_note(str(record.get("note", "") or ""))
            lines.append("")
            continue

        label = exit_label(
            record.get("exit_code", 0),
            record_interrupted(record),
            record_ok(record),
            record_expect_found(record),
        )
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

        vantage = vantage_line(record.get("vantage"))
        if vantage:
            # Where it was run from: the same check from the VPN and from the
            # switch port are two different results.
            lines += [f"From: `{vantage.replace('`', '')}`", ""]

        summary = str(record.get("summary", "") or "")
        if summary:
            # Inline code with the backticks dropped: a summary is tool output,
            # and tool output never becomes markup in this file.
            lines += [f"Summary: `{summary.replace('`', '')}`", ""]

        if record_expect_found(record) is False:
            # Only the miss is worth a line: a check that was met is already
            # what the heading reads as. The pattern says what was looked for,
            # so a false failure is readable as one beside the log below.
            pattern = str((record.get("expect") or {}).get("pattern", "") or "")
            lines += [f"Expected: `{pattern.replace('`', '')}`", ""]

        note = str(record.get("note", "") or "")
        if note:
            lines += _quoted_note(note)
            lines.append("")

        artifacts = [a for a in (record.get("artifacts") or []) if isinstance(a, dict)]
        if artifacts:
            lines.append("Artifacts:")
            lines += [_artifact_line(a) for a in artifacts]
            lines.append("")

        lines += _render_output(target_dir, record, tail, full, log_path=log_path)
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
