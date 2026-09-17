"""Durable target archive snapshotting, delta detection, and session history parsing."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

BINARY_EXTENSIONS = {
    ".pcap",
    ".cap",
    ".bin",
    ".tar",
    ".gz",
    ".zip",
    ".iso",
    ".7z",
    ".sqlite",
    ".db",
}


@dataclass
class ArtifactRecord:
    path: str              # Relative to target_dir ("raw/01_scan.xml"), absolute when outside it
    bytes: int             # File size in bytes
    lines: Optional[int]   # Line count for text files; None for binary
    binary: bool           # True if binary detected


@dataclass
class ArtifactDelta:
    artifacts: List[ArtifactRecord]
    total_files: int
    total_lines: int
    total_bytes: int


def is_binary_file(path: Path) -> bool:
    """Check if file is binary via extension check or null-byte sniffing."""
    path = Path(path)
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
            if b"\x00" in chunk:
                return True
    except OSError:
        return False
    return False


def count_lines_safe(path: Path, check_binary: bool = True) -> Optional[int]:
    """Count newline characters in a text file; return None if binary."""
    path = Path(path)
    if check_binary and is_binary_file(path):
        return None
    lines = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                lines += chunk.count(b"\n")
        return lines
    except OSError:
        return None


def snapshot_workspace(target_dir: Path) -> Dict[str, Tuple[int, int]]:
    """Scan target_dir and return {relative_path: (mtime_ns, size_bytes)}."""
    target_dir = Path(target_dir)
    snapshot: Dict[str, Tuple[int, int]] = {}
    if not target_dir.exists():
        return snapshot

    try:
        for root, dirs, files in os.walk(target_dir):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for filename in files:
                if filename.startswith(".") or filename == "session.json":
                    continue
                full_path = Path(root) / filename
                try:
                    stat = full_path.stat()
                    rel_path = full_path.relative_to(target_dir).as_posix()
                    snapshot[rel_path] = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    continue
    except OSError:
        return {}
    return snapshot


def changed_since(root: Path, pre_snap: Dict[str, Tuple[int, int]]) -> List[str]:
    """Relative paths under `root` that are new or modified since `pre_snap`."""
    changed: List[str] = []
    for rel_path, (curr_mtime, curr_size) in snapshot_workspace(root).items():
        if rel_path not in pre_snap:
            changed.append(rel_path)
        else:
            old_mtime, old_size = pre_snap[rel_path]
            if curr_mtime != old_mtime or curr_size != old_size:
                changed.append(rel_path)
    return changed


def recorded_path(target_dir: Path, path: Path) -> str:
    """How a file is named in a run record: relative to the target folder when
    it sits inside it, absolute when it does not (a `--artifact-root` log
    destination), since no relative path would ever find that one.
    """
    target_dir = Path(target_dir)
    path = Path(path)
    try:
        return path.relative_to(target_dir).as_posix()
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(target_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _build_delta(found: Dict[str, Path], primary_rel: Optional[str]) -> ArtifactDelta:
    """Size, count and classify the files a run is recording, primary log first."""
    sorted_paths = sorted(
        found,
        key=lambda p: (0 if p == primary_rel else 1, p),
    )

    records: List[ArtifactRecord] = []
    total_lines = 0
    total_bytes = 0

    for rel in sorted_paths:
        full = found[rel]
        try:
            sz = full.stat().st_size
        except OSError:
            sz = 0
        bin_flag = is_binary_file(full)
        lc = None if bin_flag else count_lines_safe(full, check_binary=False)
        if lc is not None:
            total_lines += lc
        total_bytes += sz
        records.append(
            ArtifactRecord(
                path=rel,
                bytes=sz,
                lines=lc,
                binary=bin_flag,
            )
        )

    return ArtifactDelta(
        artifacts=records,
        total_files=len(records),
        total_lines=total_lines,
        total_bytes=total_bytes,
    )


def collect_job_artifacts(
    target_dir: Path,
    primary_log: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    out_snap: Optional[Dict[str, Tuple[int, int]]] = None,
) -> ArtifactDelta:
    """The files one run owns: its primary log, plus what it wrote into `$OUTDIR`.

    The log belongs to the run outright. `$OUTDIR` needs one qualifier: the
    steps of a chain deliberately share one, so a step records what changed in
    it since that step began (`out_snap`, taken at spawn), not everything in it
    — otherwise step 2 would claim step 1's output.

    That snapshot is one directory, not the archive. The whole-tree diff below
    knows only that a file changed *during* the run, which means "this run
    wrote it" only when runs are serialised; a recipe that genuinely writes
    outside its `$OUTDIR` asks for it with `scan: true`.
    """
    target_dir = Path(target_dir)
    found: Dict[str, Path] = {}

    primary_rel: Optional[str] = None
    if primary_log is not None and Path(primary_log).exists():
        primary_rel = recorded_path(target_dir, Path(primary_log))
        found[primary_rel] = Path(primary_log)

    if out_dir is not None:
        out_dir = Path(out_dir)
        for rel in changed_since(out_dir, out_snap or {}):
            full = out_dir / rel
            found.setdefault(recorded_path(target_dir, full), full)

    return _build_delta(found, primary_rel)


def detect_artifact_deltas(
    target_dir: Path,
    pre_snap: Dict[str, Tuple[int, int]],
    primary_log: Optional[Path] = None,
    extra_roots: Optional[Dict[Path, Dict[str, Tuple[int, int]]]] = None,
) -> ArtifactDelta:
    """Every file new or modified under target_dir since pre_snap — what a
    `scan: true` recipe records.

    `extra_roots` maps a directory outside the archive (a `--artifact-root` log
    destination) to its own pre-run snapshot. Those files are recorded as
    absolute paths: no path relative to target_dir would ever find them.

    Attributes by timestamp, not by ownership, so a run overlapping another will
    claim its files too. `collect_job_artifacts` is the default for that reason.
    """
    target_dir = Path(target_dir)

    # recorded path -> file on disk. The recorded form is what sorts and what
    # lands in the manifest, so the primary log is matched against it too.
    found: Dict[str, Path] = {}
    for rel in changed_since(target_dir, pre_snap):
        found[rel] = target_dir / rel
    for root, snap in (extra_roots or {}).items():
        root = Path(root)
        for rel in changed_since(root, snap):
            full = root / rel
            found.setdefault(recorded_path(target_dir, full), full)

    primary_rel: Optional[str] = None
    if primary_log and primary_log.exists():
        primary_rel = recorded_path(target_dir, primary_log)
        # A run always owns its own log, whatever the diff made of it. The log
        # is created at plan time, before the snapshot, so a job that prints
        # nothing leaves it 0 bytes with the mtime it was born with — and
        # `changed_since` rightly says nothing changed.
        found.setdefault(primary_rel, Path(primary_log))

    return _build_delta(found, primary_rel)


@dataclass
class TargetHistory:
    max_run_id: int
    total_runs: int


def load_target_history(target_dir: Path) -> TargetHistory:
    """Load session history and compute max run ID."""
    target_dir = Path(target_dir)
    manifest = target_dir / "session.json"
    max_id = 0
    runs = []

    if manifest.exists():
        try:
            parsed = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
            if isinstance(parsed, list):
                runs = [r for r in parsed if isinstance(r, dict)]
        except (json.JSONDecodeError, OSError):
            runs = []

        for r in runs:
            rid = r.get("id")
            if rid is not None:
                try:
                    max_id = max(max_id, int(str(rid)))
                except (ValueError, TypeError):
                    pass

    return TargetHistory(max_run_id=max_id, total_runs=len(runs))


# Hidden, so snapshot_workspace never reports them as a run's artifacts.
MANIFEST_LOCK = ".session.lock"
RUN_COUNTER = ".run-counter"


@contextmanager
def manifest_lock(target_dir: Path) -> Iterator[None]:
    """Exclusive lock over session.json and the run counter, across processes.

    The lock lives on a sidecar file: session.json is swapped in with
    os.replace, so a lock taken on it would be left behind on the old inode.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with open(target_dir / MANIFEST_LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # released when fh closes
        yield


# The scope a record pins down, and the only env keys worth archiving.
MANIFEST_ENV_KEYS = ("TARGET", "TARGET_IP", "TARGET_HOST", "LHOST", "IFACE", "OUT_DIR", "RUN_ID")


def manifest_environment(env: Dict[str, str]) -> Dict[str, str]:
    """The environment block of a record. One definition, so a chain summary
    and its steps cannot drift apart on which keys they carry."""
    return {k: env.get(k, "") for k in MANIFEST_ENV_KEYS}


def append_record(target_dir: Path, record: dict) -> None:
    """Append one record to session.json, read-append-replace under the
    manifest lock, so a CLI run and the TUI finishing together cannot drop
    each other's."""
    target_dir = Path(target_dir)
    manifest = target_dir / "session.json"
    with manifest_lock(target_dir):
        runs = []
        if manifest.exists():
            try:
                parsed = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
                if isinstance(parsed, list):
                    runs = parsed
            except (json.JSONDecodeError, OSError):
                runs = []
        runs.append(record)
        tmp_manifest = target_dir / f".session_{record.get('id', 'x')}.json.tmp"
        tmp_manifest.write_text(json.dumps(runs, indent=2), encoding="utf-8")
        os.replace(tmp_manifest, manifest)


def next_run_number(target_dir: Path) -> int:
    """The number the next reservation would get. Reads only, for previews."""
    target_dir = Path(target_dir)
    try:
        issued = int((target_dir / RUN_COUNTER).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        issued = 0
    return max(issued, load_target_history(target_dir).max_run_id) + 1


def peek_run_number(target_dir: Path) -> int:
    """The next number, for a preview that must stay cheap.

    The counter alone answers it: `reserve_run_number` writes
    `max(issued, max_recorded) + 1`, so it is never below any recorded id and
    the manifest need not be parsed — which a preview redrawn on every keypress
    would otherwise do. An archive from before the counter existed has no file,
    and only that falls back to reading the runs. Reserves nothing either way.
    """
    try:
        return int((Path(target_dir) / RUN_COUNTER).read_text(encoding="utf-8").strip()) + 1
    except (OSError, ValueError):
        return next_run_number(target_dir)


def reserve_run_number(target_dir: Path) -> int:
    """Claim the next run number for a target, unique across processes.

    The counter holds the highest number handed out, not the highest recorded,
    so a job still running (and so not yet in session.json) in this or another
    process never has its number issued twice.
    """
    target_dir = Path(target_dir)
    with manifest_lock(target_dir):
        number = next_run_number(target_dir)
        tmp = target_dir / f"{RUN_COUNTER}.tmp"
        tmp.write_text(f"{number}\n", encoding="utf-8")
        os.replace(tmp, target_dir / RUN_COUNTER)
    return number


def append_note(target_dir: Path, text: str, when: Optional[float] = None) -> dict:
    """Write the operator's own words into a target's archive, as a record of
    its own, and return what was written.

    A note is a new record, never an edit of an old one: there is no
    `amend_record`, so "the archive is append-only" stays true in fact rather
    than in spirit, and a note needs no lock discipline beyond the one
    `append_record` already takes. A note about a particular run names that run
    in its text. The record is minimal — nothing ran, so it carries no command,
    no exit code and no artifacts.
    """
    target_dir = Path(target_dir)
    number = reserve_run_number(target_dir)
    record = {
        # Zero-padded like a run's, and from the same counter: a note and a run
        # cannot be handed the same number.
        "id": f"{number:02d}",
        "recipe": "note",
        "note": text,
        # Naive local time, exactly as a run record carries it.
        "start_time": datetime.fromtimestamp(when if when is not None else time.time()).isoformat(),
    }
    append_record(target_dir, record)
    return record
