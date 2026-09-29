"""Async subprocess execution under a pty, with raw-log teeing and a session.json manifest.

Jobs run on a pty rather than plain pipes so tools that probe for a tty behave
normally and their prompts are visible. A job is "awaiting input" when it has
written text not ending in a newline and then gone quiet — the heuristic a
terminal itself uses. Prompts are never parsed semantically.
"""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import pty
import re
import shlex
import signal
import struct
import termios
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from fieldlog.archive import (
    ArtifactDelta,
    append_record,
    collect_job_artifacts,
    detect_artifact_deltas,
    manifest_environment,
    snapshot_workspace,
)
from fieldlog.recipes import expect_found, fields_from_match, parse_match, summary_from_match
from fieldlog.state import ActiveJob, TargetSession, outdir_value
from fieldlog.vantage import vantage

# Called for each output line: (text, stream) where stream is "out" or "err".
LineSink = Callable[[str, str], None]

# Quiet time after a partial line before we call the job blocked.
BLOCK_GRACE = 0.4

# The longest a run waits to learn where it is being made from (vantage.py).
# A route lookup takes milliseconds; this bounds a probe that has wedged.
VANTAGE_DEADLINE = 1.5

# What ends a run the way Ctrl+C does when it reaches fieldlog itself rather
# than the tool: the terminal hanging up (an ssh session to a jump box
# dropping), or a polite kill. Both front-ends answer them the same way. The
# grace is how long a tool gets to stop on its SIGINT before its group is
# SIGKILLed.
STOP_SIGNALS = (signal.SIGHUP, signal.SIGTERM)
STOP_SIGNAL_GRACE = 5.0

# The longest partial line held in memory; past it, the line is written out
# in pieces (each a line of its own in the log).
LINE_MAX = 1024 * 1024

# How long the pty is read after the tool has exited, waiting for it to close.
# It closes at once unless something the tool started holds it open: a daemon
# in a session of its own, which no signal to the tool's group ever reaches.
EXIT_DRAIN = 1.0

# The longest a preset's `parse:` and `expect:` rules get over the log's tail.
# A sane rule takes milliseconds; one that backtracks without end (`(a+)+b`)
# would take for ever, and in the TUI every other job's pane with it.
RULES_DEADLINE = 2.0

# One line end: `\r\n`, or `\r\r\n`, the tty's rendering of a `\r` then a `\n`.
_LINE_END = re.compile(r"\r+\n")

# The pty we hand the child. Wide enough that table-shaped output is not
# rewrapped into nonsense by the tool itself.
PTY_ROWS, PTY_COLS = 24, 200


# The escapes a terminal-aware CLI emits under a pty. A line is cut wherever
# the output was, so a sequence may arrive without its end and still has to go.
_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*(?:[@-~]|$)"      # CSI: colours, cursor moves, clears
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC: a window title, a link
    r"|\x1b[()*+]?[ -~]?"                   # ESC and a character or two: `tput sgr0`'s `\x1b(B`
)
# The one kind a pane can draw: a colour or a weight.
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """`text` without the escapes a pty makes tools emit — what the log holds,
    and what a prompt has to be read as before its shape means anything."""
    # Every escape starts with ESC, and most lines have none: one C scan for
    # it is far cheaper than running the pattern over every line of a flood.
    if "\x1b" not in text:
        return text
    return _ANSI.sub("", text)


def color_only(text: str) -> str:
    """`text` with its colour escapes kept and every other one gone.

    What a job tab can paint: a cursor move or a screen clear written into
    it as characters took up cells the terminal then drew as nothing.
    """
    if "\x1b" not in text:
        return text
    return _ANSI.sub(lambda m: m[0] if _SGR.fullmatch(m[0]) else "", text)


# How much of a finished log a `parse:` rule is shown. A server recipe can write
# for hours; the closing stats a summary is after are in the last few KB.
PARSE_TAIL_BYTES = 64 * 1024


def log_tail(path: Path, limit: int = PARSE_TAIL_BYTES) -> str:
    """The last `limit` bytes of a log, escapes stripped, '' if unreadable.

    Bounded on purpose: it caps both the read and the text an operator-supplied
    regex is run over.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            raw = fh.read()
    except OSError:
        return ""
    return strip_ansi(raw.decode("utf-8", errors="replace"))


class RulesTooSlow(Exception):
    """A `parse:` or `expect:` rule outran RULES_DEADLINE."""


def _too_slow(_signum, _frame) -> None:
    raise RulesTooSlow()


def within_deadline(fn: Callable, *args, deadline: Optional[float] = None):
    """`fn(*args)`, or RulesTooSlow once it has run for `deadline` seconds
    (RULES_DEADLINE when not given).

    The regex engine checks for signals while it backtracks, so an alarm that
    raises stops a runaway match; a thread could not, since the engine never
    lets go of the GIL. Only the main thread gets signals, and SIGALRM is
    borrowed only when nobody else has a handler on it: anywhere else the
    call just runs.
    """
    if (threading.current_thread() is not threading.main_thread()
            or signal.getsignal(signal.SIGALRM) not in (signal.SIG_DFL, signal.SIG_IGN)):
        return fn(*args)
    previous = signal.signal(signal.SIGALRM, _too_slow)
    try:
        signal.setitimer(signal.ITIMER_REAL, RULES_DEADLINE if deadline is None else deadline)
        try:
            return fn(*args)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        signal.signal(signal.SIGALRM, previous)


def build_env(session: TargetSession, out_dir: Path, run_id: str) -> Dict[str, str]:
    """Env injected before spawning. Covers both $TARGET and $TARGET_IP spellings.

    The same bindings `state.resolve_flags` substitutes as text, exported so
    the shell forms it leaves alone (`${TARGET:-x}`, a script's own `$OUTDIR`)
    expand to the same values — see the note above `state._VAR`.
    """
    env = dict(os.environ)
    # The same text resolve_flags substitutes, so `${OUTDIR:-x}` and a script
    # reading its env agree with the command line (see state.outdir_value).
    outdir = outdir_value(session, out_dir)
    env.update(
        TARGET=session.target,
        TARGET_IP=session.target,
        TARGET_HOST=session.dns_name,
        HOST=session.dns_name,
        LHOST=session.effective_lhost(),
        IFACE=session.interface,
        OUT_DIR=outdir,
        OUTDIR=outdir,
        RUN_ID=run_id,
    )
    return env


def interrupt_job(job: ActiveJob) -> bool:
    """Send SIGINT to a running job's process group.

    A job planned but not yet spawned (run_job looks up the vantage first) has
    no process to signal: the interrupt is recorded instead and run_job
    delivers it the moment the process exists. Ignoring it lost a Ctrl+C
    pressed in that window, and the tool then ran anyway.
    """
    if not job.running:
        return False
    job.interrupted = True
    if job.process is None:
        return True
    try:
        os.killpg(os.getpgid(job.process.pid), signal.SIGINT)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            job.process.send_signal(signal.SIGINT)
            return True
        except (ProcessLookupError, AttributeError, OSError):
            return False


def kill_job(job: ActiveJob, grace: float = 10.0) -> bool:
    """SIGINT now so tools can flush their files; SIGKILL the group if still running after `grace`.

    Needs a running event loop. Jobs run in their own session, so the pgid is
    the pid and stays killable even after the leader exits.
    """
    if not interrupt_job(job):
        return False
    job.kill_requested = True
    job.kill_grace = grace
    if job.process is None:
        return True                    # run_job arms the SIGKILL once it spawns
    pgid = job.process.pid

    def force() -> None:
        if job.running:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    asyncio.get_running_loop().call_later(grace, force)
    return True


# A choice group a prompt offers: `[y/N]`, `(yes/no/[fingerprint])`. The brackets
# are required, so the words of a path in a prompt (`/home/operator/.ssh/…`) never count.
_CHOICES = re.compile(r"[\[(]\s*(\[?\w+\]?(?:\s*/\s*\[?\w+\]?)+)\s*[\])]")

HIDDEN_REPLY = "(reply hidden)"


def loggable_reply(prompt: str, text: str) -> Optional[str]:
    """`text` if it may be written into the run log, else None.

    Only an empty line or one of the prompt's bracketed choices may, so a
    password or passphrase typed into the reply field never lands in an artifact.
    """
    reply = text.strip().lower()
    if not reply:
        return text
    choices = {c.strip(" []").lower() for m in _CHOICES.finditer(prompt or "") for c in m[1].split("/")}
    return text if reply in choices else None


def send_stdin(job: ActiveJob, text: str) -> bool:
    """Write one operator-typed line to the job's pty.

    Never called with anything the operator did not type: there is no
    auto-answering and no remembered reply anywhere in this module.
    """
    fd = job.pty_fd
    if fd is None or not job.running:
        return False
    try:
        os.write(fd, (text + "\n").encode("utf-8", errors="replace"))
    except OSError:
        return False
    prompt = job.await_prompt or ""
    job.await_prompt = None
    job.await_since = None
    queue = job._queue
    if queue is not None:
        # Echo is off on the slave, so the reply reaches the artifact only
        # because we put it there — and it must, or the log reads as a
        # question nobody answered. A reply that is not one of the prompt's
        # choices may be a secret, so only the fact of it is written.
        shown = loggable_reply(prompt, text)
        queue.put_nowait(("note", f"› {shown if shown is not None else HIDDEN_REPLY}"))
    return True


def _open_pty(echo: bool = False, size: Optional[Tuple[int, int]] = None) -> tuple[int, int]:
    """The pty handed to the child. `echo` decides who shows the reply; `size`
    is `(rows, cols)`, PTY_ROWS x PTY_COLS when there is no terminal to copy.

    Off (the TUI): the operator's reply appears once, from the explicit
    `› reply` note. With echo on it lands twice and the second copy is raw,
    un-prefixed, and indistinguishable from tool output.

    On (the CLI): there is no note to render it, so echo off means typing
    blind and an artifact that reads as a question nobody answered. Leaving it
    on also hands the choice back to the tool, which is where it belongs — sudo
    turns echo off itself for a password, ssh leaves it on for a yes/no — so a
    secret stays hidden and a confirmation is visible, exactly as in a terminal.
    """
    master, slave = pty.openpty()
    rows, cols = size or (PTY_ROWS, PTY_COLS)
    try:
        set_winsize(slave, rows, cols)
        if not echo:
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
    except (OSError, termios.error):
        pass
    return master, slave


def set_winsize(fd: int, rows: int, cols: int) -> None:
    """Size the pty behind `fd`; the kernel tells the tool with a SIGWINCH."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def exec_form(command: str) -> str:
    """`exec <command>` when it is one simple command, else unchanged.

    /bin/sh (dash) forks even a lone `sh -c "ping …"`, and on a group SIGINT
    the shell dies of the signal while ping catches it and exits 0 — so the
    code proc.wait() saw was the shell's, never the tool's. exec makes the
    tool the process we wait on. A list or pipeline keeps the shell: exec
    would drop everything after the first command. Redirections are fine.

    A newline ends a command only outside quotes. One inside them is part of
    an argument — a script handed to `sh -c`, or that same command wrapped in
    `timeout … sh -c '…'` — and still leaves one simple command. Skipping exec
    for those left coreutils timeout a child of the shell, free to move into a
    process group of its own that no interrupt ever reached.

    `#` is read as text, never as a comment: shlex would end the command at
    the `#` of `http://x/#frag || echo y`, see one simple command, and exec it
    — and sh, which reads that `#` as part of the word, would then never
    reach the `||`. A real comment holding a `;` or a `|` only costs the exec,
    never the command.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.commenters = ""
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:              # unbalanced quote: let the shell complain
        return command
    if not tokens or tokens[0] == "exec":
        return command
    # shlex splits on an unquoted newline and keeps a quoted one in its token,
    # so any newline missing from the tokens was a command separator.
    if command.count("\n") != sum(tok.count("\n") for tok in tokens):
        return command
    for tok in tokens:
        if set(tok) <= set("();<>|&") and not (tok[0] in "<>" and set(tok) <= set("<>&")):
            return command
    return f"exec {command}"


def _make_ctty(slave: int) -> Callable[[], None]:
    """preexec_fn that makes `slave` the child's controlling terminal.

    A new session has no controlling tty, so without the TIOCSCTTY a tool that
    opens /dev/tty for its prompt (sudo, ssh passphrases, `read < /dev/tty`)
    fails with "no tty present" / ENXIO no matter what stdin is.

    ponytail: this runs BEFORE subprocess's close_fds sweep, so `slave` (an fd
    > 2 in the child) is still open here — no pass_fds needed. setsid() is also
    why start_new_session is gone: it would only do the same thing twice.
    """

    def preexec() -> None:
        os.setsid()  # session leader + own process group, what killpg(pid) relies on
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    return preexec


async def _vantage_within(target: str, deadline: float) -> Dict[str, str]:
    """`vantage(target)`, or {} if it fails or has not answered by `deadline`.

    A daemon thread rather than the loop's executor: `asyncio.run` waits for
    that executor on the way out, so a wedged probe abandoned here would still
    hold the CLI open after the job had finished.
    """
    loop = asyncio.get_running_loop()
    answer: asyncio.Future = loop.create_future()

    def settle(result: Dict[str, str]) -> None:
        if not answer.done():
            answer.set_result(result)

    def probe() -> None:
        try:
            result = vantage(target)
        except Exception:  # noqa: BLE001
            result = {}
        try:
            loop.call_soon_threadsafe(settle, result)
        except RuntimeError:      # the loop is gone; nobody is waiting any more
            pass

    threading.Thread(target=probe, name="fieldlog-vantage", daemon=True).start()
    try:
        return await asyncio.wait_for(answer, deadline)
    except asyncio.TimeoutError:
        return {}


async def run_job(
    command: str,
    job: ActiveJob,
    session: TargetSession,
    sink: LineSink,
    on_state: Optional[Callable[[], None]] = None,
    env: Optional[Dict[str, str]] = None,
    echo: bool = False,
    winsize: Optional[Tuple[int, int]] = None,
) -> int:
    """Run `command` on a pty, stream lines to `sink`, tee raw output to
    job.log_path, and append a manifest record to session.json.

    `on_state` is called whenever the blocked/unblocked state changes, so the
    UI can raise and drop the stdin bar. `env` is the launch plan's env; it is
    built from the session when omitted. `echo` leaves the pty's own echo on,
    which is what a front-end without a reply note wants (see `_open_pty`).
    `winsize` is the `(rows, cols)` the tool should see, set before it starts:
    a size applied after the spawn reached it only if the vantage lookup
    happened to finish first, and it rarely did. Returns the exit code.
    """
    work_dir = session.ensure_dirs()
    # Only a `scan: true` recipe needs the before-and-after picture; the default
    # reads this run's own log and $OUTDIR at the end and never walks the
    # archive, which is both exact under concurrency and two fewer tree walks.
    pre_snap: Dict[str, Tuple[int, int]] = {}
    extra_roots: Dict[Path, Dict[str, Tuple[int, int]]] = {}
    # One directory, and usually an empty one — but a chain's steps share an
    # $OUTDIR, so this is what keeps step 2 from claiming step 1's files.
    out_snap = snapshot_workspace(job.out_dir) if job.out_dir else {}
    if job.scan_workspace:
        pre_snap = snapshot_workspace(session.target_dir)
        # With a log destination set, the log and $OUTDIR live outside the
        # archive; without their own snapshot the run would record nothing.
        log_root = Path(job.log_path).parent
        try:
            log_root.resolve().relative_to(session.target_dir.resolve())
        except ValueError:
            extra_roots[log_root] = snapshot_workspace(log_root)
    if env is None:
        env = build_env(session, job.out_dir or work_dir, job.id)
    # Where this run is made from, asked before it starts: the route a tool is
    # about to use is the one worth recording. A thread, so a slow `ip` or
    # `route` never stalls the TUI, under one deadline for all its probes, so a
    # wedged one delays the run by at most that. Any failure is just no block.
    job.vantage = await _vantage_within(session.target, VANTAGE_DEADLINE)
    start = time.time()

    master, slave = _open_pty(echo=echo, size=winsize)
    try:
        proc = await asyncio.create_subprocess_shell(
            exec_form(command),
            stdin=slave,
            stdout=slave,
            stderr=slave,          # merged, so the raw log matches what the pane shows
            env=env,
            cwd=str(session.target_dir),
            preexec_fn=_make_ctty(slave),  # own session + the pty as controlling tty
        )
    finally:
        os.close(slave)

    job.process = proc
    job.pid = proc.pid
    job.pty_fd = master
    if job.interrupted:
        # Asked to stop before there was a process to ask (see interrupt_job).
        # Delivered now, so the tool stops as it would have and the run is
        # archived as interrupted rather than running on unasked.
        if job.kill_requested:
            kill_job(job, job.kill_grace)
        else:
            interrupt_job(job)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    job._queue = queue

    def _readable() -> None:
        try:
            data = os.read(master, 65536)
        except (OSError, BlockingIOError):
            data = b""
        if data:
            queue.put_nowait(("data", data))
        else:
            loop.remove_reader(master)
            queue.put_nowait(("eof", b""))

    loop.add_reader(master, _readable)

    async def _stop_reading_after_exit() -> None:
        # The run ends with the tool, not with the pty: a process the tool left
        # behind in a session of its own holds the pty open for as long as it
        # lives, and waiting for the close waited for ever. Everything the tool
        # wrote is in the pty's buffer when it exits, so that is read out
        # before the end is called.
        await proc.wait()
        await asyncio.sleep(EXIT_DRAIN)
        try:
            loop.remove_reader(master)
            os.set_blocking(master, False)
            while data := os.read(master, 65536):
                queue.put_nowait(("data", data))
        except (ValueError, OSError):
            pass
        queue.put_nowait(("eof", b""))

    drain = asyncio.create_task(_stop_reading_after_exit())

    try:
        lines = 0
        pending = ""          # partial line held back; it is the candidate prompt
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        held_cr = ""          # a read's trailing `\r`s, until the next read is seen
        # A line the loop committed before its newline arrived (a prompt, or a
        # line that ended in `\r` and then paused) is owed that newline: when
        # it comes, it ends the committed line and is not a blank line of its own.
        owed_lf = False
        # Buffered, and flushed whenever the loop is about to wait (below): the
        # log on disk is as current as a line-buffered one wherever it can be
        # read from outside, without a write call per line. Line buffering was
        # half the CPU of a run that printed a million lines.
        with open(job.log_path, "w", encoding="utf-8", errors="replace") as raw:

            def emit(text: str) -> None:
                nonlocal lines
                # Under a pty, color tools emit ANSI; strip it from the log so it
                # stays greppable. The live pane still gets the colored text.
                clean = strip_ansi(text)
                raw.write(clean + "\n")
                lines += 1
                job.lines_count = lines
                job.bytes_count += len(clean) + 1
                sink(text, "out")

            while True:
                # Wait forever unless a partial line is pending and unclaimed —
                # then only until the grace period says the tool has stopped.
                # `is None`, not truthiness, on both sides: a partial line of
                # only whitespace strips to an empty prompt, which is still a
                # prompt the tool has to be seen resuming from. A tool that has
                # exited is asking nothing: its last partial line is just that.
                timeout = (
                    BLOCK_GRACE
                    if pending and job.await_prompt is None and proc.returncode is None
                    else None
                )
                if queue.empty():
                    raw.flush()
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout)
                except asyncio.TimeoutError:
                    owed_lf = True
                    if held_cr:
                        # It ended in `\r`: a finished line whose `\n` is late
                        # (a slow runner, a busy box), never a prompt.
                        emit(pending)
                        pending = held_cr = ""
                        continue
                    # Held clean: the bar, its reply chips and the rule that
                    # decides which replies may be logged all read its shape.
                    job.await_prompt = strip_ansi(pending).strip()
                    job.await_since = time.time()
                    # Commit the prompt as a real line now, so the artifact
                    # reads question-then-answer rather than answer-then-question.
                    emit(pending)
                    pending = ""
                    if on_state:
                        on_state()
                    continue

                if kind == "note":
                    emit(str(payload))
                    continue

                # A read ends wherever the pty buffer did, not on a character
                # or a line: the decoder keeps a split UTF-8 sequence for the
                # next read (decoding each read alone made two `�` of one `é`),
                # and a trailing `\r` waits to see whether a `\n` follows it
                # (the pty writes `\r\n`, and split, it read as two newlines).
                final = kind == "eof"
                text = held_cr + decoder.decode(payload, final=final)
                held_cr = ""
                if not final:
                    body = text.rstrip("\r")
                    text, held_cr = body, text[len(body):]
                # `\r\n` ends a line, and so does the `\r\r\n` a tty makes of a
                # `\r` then a `\n`; a bare `\r` (a progress bar) ends one too.
                text = _LINE_END.sub("\n", text).replace("\r", "\n")
                if not text:
                    if final:
                        break
                    continue
                if job.await_prompt is not None:
                    job.await_prompt = None
                    job.await_since = None
                    if on_state:
                        on_state()
                if owed_lf:
                    owed_lf = False
                    if text.startswith("\n"):
                        text = text[1:]
                # Split only when a line ended: re-splitting the whole partial
                # line on every read made a long newline-free output quadratic.
                if "\n" in text:
                    *complete, pending = (pending + text).split("\n")
                    for line in complete:
                        emit(line)
                else:
                    pending += text
                if len(pending) > LINE_MAX:
                    # Binary on stdout, or a megabyte of JSON on one line: it is
                    # written out in pieces rather than held until it ends.
                    # A `\n` straight after the cut ends this piece, not an
                    # empty line of its own.
                    emit(pending)
                    pending = ""
                    owed_lf = True
                if final:
                    break

            if pending:
                emit(pending)

        code = shell_exit_code(await proc.wait())
        job.exit_code = code
        job.end_time = time.time()
        job.await_prompt = None
        job.await_since = None
        # One read of the tail, and one match off it: both regexes are an
        # operator's, run over 64 KB, and the parse rule's match is read twice.
        # An interrupted run makes no claim about its expectation: the tool
        # never got to print its closing line, and a `false` here would archive
        # an operator's Ctrl+C as a failed check. A timeout is not exempt — the
        # log it left is what the tool printed, and the run fails on 124 anyway.
        match, found = None, None
        if job.parse_rule or job.expect:
            tail = log_tail(job.log_path)

            def apply_rules() -> tuple:
                return (
                    parse_match(job.parse_rule, tail),
                    None if job.interrupted else expect_found(job.expect, tail),
                )

            try:
                match, found = within_deadline(apply_rules)
            except RulesTooSlow:
                # Unapplied rather than stuck: a `found` of null is a check
                # that was not made, never a pass.
                sink(f"[fieldlog] the recipe's parse:/expect: rule ran past "
                     f"{RULES_DEADLINE:g}s on this log · not applied", "err")
        job.summary = summary_from_match(job.parse_rule, match)
        job.fields = fields_from_match(match)
        job.expect_found = found
        if job.scan_workspace:
            delta = detect_artifact_deltas(
                session.target_dir, pre_snap, primary_log=job.log_path, extra_roots=extra_roots
            )
        else:
            delta = collect_job_artifacts(
                session.target_dir, primary_log=job.log_path, out_dir=job.out_dir,
                out_snap=out_snap,
            )
        job.artifact_delta = delta
        aside = _append_manifest(session, job, command, env, start, job.end_time, code, delta)
        if aside is not None:
            # Said where the operator is looking now; `history` and `report`
            # keep saying it for as long as the file sits there.
            sink(f"[fieldlog] session.json could not be read · kept as {aside.name} · "
                 "this run starts a new one", "err")
        return code
    finally:
        drain.cancel()
        try:
            loop.remove_reader(master)
        except (ValueError, OSError):
            pass
        try:
            os.close(master)
        except OSError:
            pass
        job.pty_fd = None
        if proc.returncode is None:
            # Reached only when the worker was cancelled (the app quitting)
            # while the job is still live. Closing the master above already
            # hung up the child's controlling terminal, which is the end of
            # most tools; the signal is for the ones that ignore SIGHUP.
            # Signal the whole process group, not just the leader: a
            # pipeline's shell leaves children in the group that a bare
            # terminate() would orphan. Jobs run in their own session (setsid
            # in _make_ctty), so the pgid is the leader's pid.
            #
            # A job the operator already asked to kill is past politeness:
            # kill_job scheduled its SIGKILL for after the grace, and the loop
            # that would have delivered it is going down with the app.
            sig = signal.SIGKILL if job.kill_requested else signal.SIGTERM
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.send_signal(sig)
                except (ProcessLookupError, OSError):
                    pass


def shell_exit_code(code: int) -> int:
    """asyncio reports a signal death as a negative number; record the shell's
    128+signal instead, so an uncaught SIGINT reads 130 and SIGKILL 137."""
    return 128 + (-code) if code < 0 else code


def _append_manifest(
    session: TargetSession,
    job: ActiveJob,
    command: str,
    env: Dict[str, str],
    start: float,
    end: float,
    code: int,
    delta: ArtifactDelta,
) -> Optional[Path]:
    """Append-only run record in session.json (the durable, greppable manifest).

    Returns where an unreadable manifest was set aside, if it was (see
    archive.append_record)."""
    artifact_entries = [
        {
            "path": a.path,
            "lines": a.lines,
            "bytes": a.bytes,
            **({"binary": True} if a.binary else {}),
        }
        for a in delta.artifacts
    ]

    recipe_key = job.recipe_id
    if job.variant_id and not recipe_key.endswith(f"/{job.variant_id}"):
        recipe_key = f"{recipe_key}/{job.variant_id}"

    record = {
        "id": job.id,
        "recipe": recipe_key,
        "command": command,
        "environment": manifest_environment(env),
        # The interface, local address, gateway and network the target was
        # reached through, when the OS would say (see vantage.py).
        **({"vantage": job.vantage} if job.vantage else {}),
        "artifact_log": str(job.log_path),
        "out_dir": str(job.out_dir) if job.out_dir else "",
        "start_time": datetime.fromtimestamp(start).isoformat(),
        "end_time": datetime.fromtimestamp(end).isoformat(),
        "duration_sec": round(end - start, 2),
        "exit_code": code,
        # The code is the tool's own; this says the operator asked it to stop.
        **({"interrupted": True} if job.interrupted else {}),
        # What the preset's `parse:` rule made of the log, when it has one.
        **({"summary": job.summary} if job.summary else {}),
        # The same match's named groups, kept raw so a value can be trended
        # across runs without parsing the log again.
        **({"fields": job.fields} if job.fields else {}),
        # Why the operator made this run, when they said (`--note`).
        **({"note": job.note} if job.note else {}),
        # The codes this recipe calls success, so a reader of the archive can
        # see why a non-zero exit was not a failure. The code itself stays raw.
        **({"success": job.success_codes} if job.success_codes else {}),
        # The check that was made and how it went, kept beside the raw exit
        # code, never in place of it.
        # `found` is null for a run that made no claim (an interrupt), which
        # every reader treats as unchecked rather than as a miss.
        **({"expect": {"pattern": job.expect, "found": job.expect_found}} if job.expect else {}),
        "artifacts": artifact_entries,
    }
    if job.chain:
        record["chain"] = job.chain

    aside = append_record(session.target_dir, record)
    # Only now: a front-end reads this as "what the archive holds", so a write
    # that raised (a full or read-only disk) must leave it unset.
    job.record = record
    return aside
