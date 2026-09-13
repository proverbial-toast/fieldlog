"""Async subprocess execution under a pty, with raw-log teeing and a session.json manifest.

Jobs run on a pty rather than plain pipes so tools that probe for a tty behave
normally and their prompts are visible. A job is "awaiting input" when it has
written text not ending in a newline and then gone quiet — the heuristic a
terminal itself uses. Prompts are never parsed semantically.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import shlex
import signal
import struct
import termios
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from fieldlog.archive import ArtifactDelta, detect_artifact_deltas, manifest_lock, snapshot_workspace
from fieldlog.state import ActiveJob, TargetSession

# Called for each output line: (text, stream) where stream is "out" or "err".
LineSink = Callable[[str, str], None]

# Quiet time after a partial line before we call the job blocked.
BLOCK_GRACE = 0.4

# The pty we hand the child. Wide enough that table-shaped output is not
# rewrapped into nonsense by the tool itself.
PTY_ROWS, PTY_COLS = 24, 200


# CSI escapes (colors, cursor moves) and OSC escapes (title sets), the two a
# terminal-aware CLI emits under a pty.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def build_env(session: TargetSession, out_dir: Path, run_id: str) -> Dict[str, str]:
    """Env injected before spawning. Covers both $TARGET and $TARGET_IP spellings."""
    env = dict(os.environ)
    env.update(
        TARGET=session.target,
        TARGET_IP=session.target,
        TARGET_HOST=session.dns_name,
        HOST=session.dns_name,
        LHOST=session.effective_lhost(),
        IFACE=session.interface,
        OUT_DIR=str(out_dir),
        OUTDIR=str(out_dir),
        RUN_ID=run_id,
    )
    return env


def interrupt_job(job: ActiveJob) -> bool:
    """Send SIGINT to a running job's process group."""
    if not job.running or job.process is None:
        return False
    job.interrupted = True
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
    pgid = job.process.pid

    def force() -> None:
        if job.running:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    asyncio.get_running_loop().call_later(grace, force)
    return True


def send_stdin(job: ActiveJob, text: str) -> bool:
    """Write one operator-typed line to the job's pty.

    Never called with anything the operator did not type: there is no
    auto-answering and no remembered reply anywhere in this module.
    """
    fd = getattr(job, "pty_fd", None)
    if fd is None or not job.running:
        return False
    try:
        os.write(fd, (text + "\n").encode("utf-8", errors="replace"))
    except OSError:
        return False
    job.await_prompt = None
    job.await_since = None
    queue = getattr(job, "_queue", None)
    if queue is not None:
        # Echo is off on the slave, so the reply reaches the artifact only
        # because we put it there — and it must, or the log reads as a
        # question nobody answered.
        queue.put_nowait(("note", f"› {text}"))
    return True


def _open_pty() -> tuple[int, int]:
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        attrs = termios.tcgetattr(slave)
        # ponytail: echo off so the operator's reply appears once, from the
        # explicit `› reply` note. With echo on it lands twice and the second
        # copy is raw, un-prefixed, and indistinguishable from tool output.
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
    except (OSError, termios.error):
        pass
    return master, slave


def exec_form(command: str) -> str:
    """`exec <command>` when it is one simple command, else unchanged.

    /bin/sh (dash) forks even a lone `sh -c "ping …"`, and on a group SIGINT
    the shell dies of the signal while ping catches it and exits 0 — so the
    code proc.wait() saw was the shell's, never the tool's. exec makes the
    tool the process we wait on. A list or pipeline keeps the shell: exec
    would drop everything after the first command. Redirections are fine.
    """
    if "\n" in command:
        return command
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:              # unbalanced quote: let the shell complain
        return command
    if not tokens or tokens[0] == "exec":
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


async def run_job(
    command: str,
    job: ActiveJob,
    session: TargetSession,
    sink: LineSink,
    on_state: Optional[Callable[[], None]] = None,
    env: Optional[Dict[str, str]] = None,
) -> int:
    """Run `command` on a pty, stream lines to `sink`, tee raw output to
    job.log_path, and append a manifest record to session.json.

    `on_state` is called whenever the blocked/unblocked state changes, so the
    UI can raise and drop the stdin bar. `env` is the launch plan's env; it is
    built from the session when omitted. Returns the exit code.
    """
    work_dir = session.ensure_dirs()
    pre_snap = snapshot_workspace(session.target_dir)
    # With a log destination set, the log and $OUTDIR live outside the archive;
    # without their own snapshot the run would record no artifacts at all.
    log_root = Path(job.log_path).parent
    extra_roots: Dict[Path, Dict[str, Tuple[int, int]]] = {}
    try:
        log_root.resolve().relative_to(session.target_dir.resolve())
    except ValueError:
        extra_roots[log_root] = snapshot_workspace(log_root)
    if env is None:
        env = build_env(session, job.out_dir or work_dir, job.id)
    start = time.time()

    master, slave = _open_pty()
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

    try:
        lines = 0
        pending = ""          # partial line held back; it is the candidate prompt
        with open(job.log_path, "w", buffering=1, encoding="utf-8", errors="replace") as raw:

            def emit(text: str) -> None:
                nonlocal lines
                # Under a pty, color tools emit ANSI; strip it from the log so it
                # stays greppable. The live pane still gets the colored text.
                # ponytail: CSI + OSC covers real tool output; add more if some
                # tool's escapes leak through.
                raw.write(_ANSI.sub("", text) + "\n")
                lines += 1
                job.lines_count = lines
                job.bytes_count += len(text) + 1
                sink(text, "out")

            while True:
                # Wait forever unless a partial line is pending and unclaimed —
                # then only until the grace period says the tool has stopped.
                timeout = BLOCK_GRACE if (pending and not job.await_prompt) else None
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout)
                except asyncio.TimeoutError:
                    job.await_prompt = pending.strip()
                    job.await_since = time.time()
                    # Commit the prompt as a real line now, so the artifact
                    # reads question-then-answer rather than answer-then-question.
                    emit(pending)
                    pending = ""
                    if on_state:
                        on_state()
                    continue

                if kind == "eof":
                    break
                if kind == "note":
                    emit(str(payload))
                    continue

                text = payload.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
                if job.await_prompt:
                    job.await_prompt = None
                    job.await_since = None
                    if on_state:
                        on_state()
                pending += text
                *complete, pending = pending.split("\n")
                for line in complete:
                    emit(line)

            if pending:
                emit(pending)

        code = shell_exit_code(await proc.wait())
        job.exit_code = code
        job.end_time = time.time()
        job.await_prompt = None
        job.await_since = None
        delta = detect_artifact_deltas(
            session.target_dir, pre_snap, primary_log=job.log_path, extra_roots=extra_roots
        )
        job.artifact_delta = delta
        _append_manifest(session, job, command, env, start, job.end_time, code, delta)
        return code
    finally:
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
            try:
                proc.terminate()
            except ProcessLookupError:
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
) -> None:
    """Append-only run record in session.json (the durable, greppable manifest)."""
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
        "artifact_log": str(job.log_path),
        "out_dir": str(job.out_dir) if job.out_dir else "",
        "start_time": datetime.fromtimestamp(start).isoformat(),
        "end_time": datetime.fromtimestamp(end).isoformat(),
        "duration_sec": round(end - start, 2),
        "exit_code": code,
        # The code is the tool's own; this says the operator asked it to stop.
        **({"interrupted": True} if job.interrupted else {}),
        "artifacts": artifact_entries,
    }
    if job.chain:
        record["chain"] = job.chain

    _write_record(session, record)


# The scope a record pins down, and the only env keys worth archiving.
MANIFEST_ENV_KEYS = ("TARGET", "TARGET_IP", "TARGET_HOST", "LHOST", "IFACE", "OUT_DIR", "RUN_ID")


def manifest_environment(env: Dict[str, str]) -> Dict[str, str]:
    """The environment block of a record. One definition, so a chain summary
    and its steps cannot drift apart on which keys they carry."""
    return {k: env.get(k, "") for k in MANIFEST_ENV_KEYS}


def _write_record(session: TargetSession, record: dict) -> None:
    """Append one record to session.json, read-append-replace under the
    manifest lock, so a CLI run and the TUI finishing together cannot drop
    each other's."""
    manifest = session.target_dir / "session.json"
    with manifest_lock(session.target_dir):
        runs = []
        if manifest.exists():
            try:
                parsed = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
                if isinstance(parsed, list):
                    runs = parsed
            except (json.JSONDecodeError, OSError):
                runs = []
        runs.append(record)
        tmp_manifest = session.target_dir / f".session_{record.get('id', 'x')}.json.tmp"
        tmp_manifest.write_text(json.dumps(runs, indent=2), encoding="utf-8")
        os.replace(tmp_manifest, manifest)
