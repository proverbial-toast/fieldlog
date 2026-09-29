"""Lightweight session/job state.

The durable product is the on-disk archive under ./targets/<name>/; these
dataclasses just carry the in-memory context needed to spawn jobs and route
their evidence there.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple

if TYPE_CHECKING:
    import asyncio

    from fieldlog.archive import ArtifactDelta

DEFAULT_ARTIFACT_ROOT = ""  # empty: logs live in the target workspace raw/ dir

# The first wired interface is named for the platform, not for the box: a Mac
# has en0 where Linux has eth0. Chosen once here so every surface — the CLI's
# fallbacks, its help text and the TUI's picker — says the same name.
DEFAULT_INTERFACE = "en0" if sys.platform == "darwin" else "eth0"

# Loopback under either name. It never carries a $LHOST worth having, so the
# interface picker sinks it to the bottom and never starts on it.
LOOPBACK_NAMES = ("lo", "lo0")

# Darwin's SIOCGIFADDR is _IOWR('i', 33, struct ifreq) rather than Linux's
# 0x8915. Its `ifreq` is 16 bytes of interface name followed by a sockaddr_in
# whose address still lands at offset 20: Darwin spends one byte each on
# sa_len and sa_family where Linux spends two on the family alone. So the
# [20:24] slice below is right on both, and only the request number differs.
SIOCGIFADDR = 0xC0206921 if sys.platform == "darwin" else 0x8915


LAST_SCOPE_FILE = ".last-scope.json"
_LAST_SCOPE_KEYS = ("target", "hostname", "interface", "lhost", "artifact_root")


def load_last_scope(workspace_dir) -> dict:
    """Remembered scope for a workspace, or {} if none / unreadable."""
    try:
        data = json.loads((Path(workspace_dir) / LAST_SCOPE_FILE).read_text())
    except Exception:
        return {}
    return {k: v for k, v in data.items() if k in _LAST_SCOPE_KEYS and isinstance(v, str)}


def save_last_scope(session: "TargetSession") -> None:
    """Persist the current scope beside the workspace so bare `fieldlog` resumes."""
    try:
        session.workspace_dir.mkdir(parents=True, exist_ok=True)
        (session.workspace_dir / LAST_SCOPE_FILE).write_text(
            json.dumps({k: getattr(session, k) for k in _LAST_SCOPE_KEYS}, indent=2)
        )
    except Exception:
        pass  # ponytail: a scope we can't cache is not worth crashing the TUI over


PINNED_RECENT_FILE = ".pinned-recent.json"

# The keys load_pinned_recent left out because the catalog lacked them, per
# workspace. Written back on every save: a drop-in that failed to parse one
# morning took its recipes out of the catalog, and the next pin or launch then
# saved the pins without them — gone for good, even once the yaml was fixed.
_WITHHELD: dict[str, tuple[list[str], list[str]]] = {}


def load_pinned_recent(workspace_dir, valid_keys: Optional[set[str]] = None) -> tuple[list[str], list[str]]:
    """Load (pinned, recent) recipe keys from workspace file, leaving out unknown
    keys — kept aside, not dropped: save_pinned_recent writes them back."""
    withheld: tuple[list[str], list[str]] = ([], [])
    _WITHHELD[str(Path(workspace_dir).resolve())] = withheld
    try:
        data = json.loads((Path(workspace_dir) / PINNED_RECENT_FILE).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return [], []
        raw_pinned = data.get("pinned", [])
        raw_recent = data.get("recent", [])
        if not isinstance(raw_pinned, list) or not isinstance(raw_recent, list):
            return [], []
        pinned = [str(k) for k in raw_pinned if isinstance(k, str)]
        recent = [str(k) for k in raw_recent if isinstance(k, str)]
        if valid_keys is not None:
            withheld[0].extend(k for k in pinned if k not in valid_keys)
            withheld[1].extend(k for k in recent if k not in valid_keys)
            pinned = [k for k in pinned if k in valid_keys]
            recent = [k for k in recent if k in valid_keys]
        return pinned, recent
    except Exception:
        return [], []


def save_pinned_recent(workspace_dir, pinned: list[str], recent: list[str]) -> None:
    """Save pinned and recent lists next to .last-scope.json, with the keys the
    load left out after them (see _WITHHELD)."""
    try:
        p = Path(workspace_dir)
        extra_pinned, extra_recent = _WITHHELD.get(str(p.resolve()), ([], []))
        p.mkdir(parents=True, exist_ok=True)
        (p / PINNED_RECENT_FILE).write_text(
            json.dumps({
                "pinned": pinned + [k for k in extra_pinned if k not in pinned],
                "recent": recent + [k for k in extra_recent if k not in recent],
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


_IP_CACHE: dict[str, tuple[float, str]] = {}
_IP_CACHE_TTL = 2.0


def _clear_ip_cache() -> None:
    """Clear interface IP cache (used for testing)."""
    _IP_CACHE.clear()


def get_interface_ip(ifname: str) -> str:
    """IPv4 address for an interface name via ioctl SIOCGIFADDR, cached briefly."""
    if not ifname:
        return ""
    now = time.time()
    cached = _IP_CACHE.get(ifname)
    if cached and (now - cached[0] < _IP_CACHE_TTL):
        return cached[1]

    ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            ip = socket.inet_ntoa(
                fcntl.ioctl(
                    s.fileno(),
                    SIOCGIFADDR,
                    struct.pack("256s", ifname[:15].encode("utf-8")),
                )[20:24]
            )
    except Exception:
        ip = ""

    _IP_CACHE[ifname] = (now, ip)
    return ip


def scope_dir(target: str) -> str:
    """Folder name for a target scope.

    `/` becomes `_` so a CIDR keeps its shape, then anything outside
    `[A-Za-z0-9._-]` becomes `-`. Operator input is never interpolated raw.
    """
    name = (target or "").strip() or "unassigned"
    name = name.replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return "unassigned" if set(name) <= {"."} else name


def is_ip_address(value: str) -> bool:
    """Whether `value` is a literal IPv4/IPv6 address, as the stdlib reads one."""
    try:
        ipaddress.ip_address((value or "").strip())
        return True
    except ValueError:
        return False


# What an operator typing an IPv4 address produces, whether or not they got it
# right: `10.0.0.256` and `1.2.3` are addresses that are wrong, not hostnames.
_DOTTED = re.compile(r"[\d.]+")


def run_stamp(when: Optional[float] = None) -> str:
    """Compact ISO basic form: sorts lexically, needs no escaping."""
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(when if when is not None else time.time()))


def recipe_slug(tool_id: str, preset_id: str, run_id: str) -> str:
    """`ping_sweep_01` — the run's name within its scope directory."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"{tool_id}_{preset_id}_{run_id}")


@dataclass
class TargetSession:
    """Current target scope + workspace root."""

    target: str = ""                          # address or subnet -> $TARGET
    hostname: str = ""                        # dns name -> $HOST
    lhost: str = ""                           # address the operator set -> $LHOST ('' follows the interface)
    interface: str = DEFAULT_INTERFACE        # local interface name -> $IFACE
    workspace_dir: Path = field(default_factory=lambda: Path("./targets"))
    artifact_root: str = DEFAULT_ARTIFACT_ROOT

    @property
    def target_kind(self) -> str:
        """Classify target as subnet, literal address, ssh user@host, or hostname."""
        t = (self.target or "").strip()
        if re.search(r"/\d+$", t):
            return "subnet"
        # Digits and dots is an address, sound or not — a mistyped one must not
        # fall through to `hostname` and quietly start binding `$HOST`.
        # Otherwise the parser decides, not the alphabet: `dc1`, `cafe` and
        # `db2` are spelled entirely in hex digits and are hostnames all the same.
        if _DOTTED.fullmatch(t) or is_ip_address(t):
            return "address"
        if "@" in t:
            return "user@host"
        return "hostname"

    @property
    def dns_name(self) -> str:
        """The value `$HOST` resolves to, or '' when nothing does.

        The dns name field is authoritative. The single fallback: a target that
        is itself a hostname stands in for it. Nothing else is inferred — the
        two fields are independent, and editing one never rewrites the other.
        """
        h = (self.hostname or "").strip()
        if h:
            return h
        return self.target.strip() if self.target_kind == "hostname" else ""

    def effective_lhost(self) -> str:
        """`$LHOST` value: the set address, else the interface's current IP.

        Resolved at spawn, not stored, so a VPN that comes up after launch
        (tun0 with no IP yet) still lands the right address.
        """
        return self.lhost.strip() or get_interface_ip(self.interface)

    @property
    def scope_dir(self) -> str:
        return scope_dir(self.target)

    @property
    def slug(self) -> str:
        """Folder-safe name for this target's workspace under ./targets/."""
        return scope_dir(self.hostname or self.target)

    @property
    def target_dir(self) -> Path:
        return Path(os.path.abspath(self.workspace_dir / self.slug))

    @property
    def raw_dir(self) -> Path:
        return self.target_dir / "raw"

    # ---- Artifact layout (captured onto the job at spawn, never re-derived) --
    @staticmethod
    def artifact_dir_for(root: str, name: str) -> str:
        """`name` is what `slug` is built from — the dns name if set, else the
        target — so a log destination folder and the workspace folder agree."""
        base = (root or "").strip().rstrip("/") or "."
        return f"{os.path.abspath(base)}/{scope_dir(name)}/"

    def log_dir(self, root: Optional[str] = None) -> str:
        """Where this scope's logs land: `<root>/<scope>/` when a log destination
        is set, else the target workspace's raw/ dir."""
        root = self.artifact_root if root is None else root
        name = self.hostname or self.target
        return TargetSession.artifact_dir_for(root, name) if root.strip() else f"{self.raw_dir}/"

    def ensure_dirs(self) -> Path:
        """Create raw/ (owned by the operator) and return it."""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        return self.raw_dir


# `$NAME` or `${NAME}`, whole names only. Shell forms such as `${NAME:-x}` do not
# match and are left for the shell, which has every binding in its env.
#
# This regex is the boundary between the two ways a recipe's variables get
# filled. What it matches, fieldlog substitutes as raw text (resolve_flags),
# which is safe only because is_blocked has allowlisted every scope value
# first; that is also what previews show. What it leaves alone reaches the
# shell untouched and expands through real parameter expansion from the
# child's environment (runner.build_env exports the same bindings). Same
# values either way — so the allowlist has to guard both paths, and
# `shell_vars` below is how the second one is seen.
_VAR = re.compile(r"\$(?:\{(\w+)\}|(\w+))")

# Every `${NAME...}` brace form, modifier or not. `_VAR` matches only the bare
# `${NAME}` it substitutes; this one also sees `${NAME:-default}`, `${NAME:?}`
# and `${NAME%.pcap}`, which are left for the shell and expand from the env.
# The name is what the check needs: the value still reaches the command line,
# so it still has to pass the scope allowlist — its *presence*, though, is not
# required, since supplying a default is the whole point of writing the form.
_SHELL_VAR = re.compile(r"\$\{(\w+)")

# Alternate spellings, mapped to the binding they resolve as.
_VAR_ALIASES = {"TARGET_IP": "TARGET", "TARGET_HOST": "HOST", "OUT_DIR": "OUTDIR"}

# The scope bindings fieldlog fills in, by canonical name.
SCOPE_VARS = ("TARGET", "HOST", "IFACE", "LHOST", "OUTDIR")


def _var_name(match: re.Match) -> str:
    name = match[1] or match[2]
    return _VAR_ALIASES.get(name, name)


def template_vars(flags: str) -> set[str]:
    """Variables a flags template references, by canonical name.

    `$TARGET_HOST` and `${HOST}` are both HOST, never TARGET. The match is the
    one resolve_flags substitutes with, so detection and substitution agree.
    Unknown names (e.g. RUN_ID) come back as written.
    """
    return {_var_name(m) for m in _VAR.finditer(flags or "")}


def shell_vars(flags: str) -> set[str]:
    """Variables the *shell* expands from the env, by canonical name.

    `${TARGET:-10.0.0.1}` is never substituted by `resolve_flags`, so
    `template_vars` does not report it — but `build_env` exports TARGET, so the
    value still lands in the command. A caller checking whether a scope value
    is safe to paste into a shell has to look here as well.
    """
    return {_VAR_ALIASES.get(m[1], m[1]) for m in _SHELL_VAR.finditer(flags or "")}


def outdir_value(session: TargetSession, out_dir) -> str:
    """`$OUTDIR` as a command sees it: relative to the target folder when it is
    inside it, which is every run's working directory (runner's `cwd`).

    The value is pasted into a shell command unquoted, and the absolute form
    carries the whole workspace path — so a workspace under `My Work/` split
    one path into two words, and `> $OUTDIR/x` wrote a file named `My` outside
    the archive. Relative, it is `raw/<stamp>_<run>`, built only from
    characters `scope_dir` and `run_stamp` allow. A log destination outside the
    target folder stays absolute, and `check_recipe` refuses one that needs quoting.
    """
    text = str(out_dir)
    try:
        return Path(os.path.abspath(text)).relative_to(session.target_dir).as_posix()
    except ValueError:
        return text


def resolve_flags(session: TargetSession, flags: str, out_dir: Optional[str] = None) -> str:
    """Replace known `$NAME` / `${NAME}` bindings, matched as whole names.

    Unknown names (e.g. `$RUN_ID`) are left for the shell, which gets them as env.
    """
    # The fallback is a preview for a caller with no run in hand (`show`, which
    # knows no workspace): `NN` stands where the run number would be, and is a
    # placeholder that pastes into a command as harmlessly as it reads.
    outdir = out_dir if out_dir is not None else f"{session.log_dir()}{run_stamp()}_NN"
    vals = {
        "TARGET": session.target,
        "HOST": session.dns_name,
        "IFACE": session.interface,
        "LHOST": session.effective_lhost(),
        "OUTDIR": outdir_value(session, outdir),
    }
    return _VAR.sub(lambda m: vals.get(_var_name(m), m[0]), flags)


def prepare_job_paths(
    session: TargetSession,
    tool_id: str,
    preset_id: str,
    run_id: str,
    create: bool = True,
    stamp: Optional[str] = None,
    out_dir: Optional[Path] = None,
) -> Tuple[Path, Path, str, str]:
    """Resolve (log_path, out_dir, root, stamp). With `create`, also create
    the log, falling back to raw/ when the log destination is not writable.

    The directory is named for the run — `<stamp>_<run_id>` — so two runs
    started in the same second never write into one another's `$OUTDIR`. A
    caller that wants one shared deliberately, which is what a chain's later
    steps want, passes the directory in as `out_dir`. A caller-supplied `stamp`
    only shares the name of the hour; the log filenames still differ, by their
    per-run slug.
    """
    root = session.artifact_root
    stamp = run_stamp() if stamp is None else stamp
    slug = recipe_slug(tool_id, preset_id, run_id)
    base = session.log_dir()
    log_path = Path(f"{base}{stamp}_{slug}.log")
    out_dir = Path(out_dir) if out_dir is not None else Path(f"{base}{stamp}_{run_id}")
    if not create:
        return log_path, out_dir, root, stamp

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
    except (PermissionError, OSError):
        session.ensure_dirs()
        log_path = session.raw_dir / f"{stamp}_{slug}.log"
        # The fallback moves the directory, never renames it: a shared one keeps
        # the name its chain knows it by.
        out_dir = session.raw_dir / out_dir.name

    return log_path, out_dir, root, stamp


@dataclass
class ActiveJob:
    """One spawned recon run."""

    id: str                                   # zero-padded, e.g. "01"
    recipe_id: str
    name: str                                 # tab label, e.g. "ping/sweep #01"
    log_path: Path
    process: Optional["object"] = None        # asyncio subprocess handle
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    exit_code: Optional[int] = None
    variant_id: str = ""
    command: str = ""
    pid: Optional[int] = None
    # Owned by runner.py, but declared here: the CLI reads pty_fd from the moment
    # it registers its stdin reader, which is before run_job has opened the pty.
    pty_fd: Optional[int] = None
    _queue: Optional[asyncio.Queue] = None
    lines_count: int = 0
    bytes_count: int = 0
    interrupted: bool = False
    # The operator chose kill, not just interrupt: SIGINT now and SIGKILL after
    # a grace (runner.kill_job). Kept on the job so a shutdown that lands
    # inside the grace can finish the kill rather than lose it.
    kill_requested: bool = False
    # The grace kill_job was given, kept for an interrupt that lands before the
    # process exists and has to be delivered by run_job once it does.
    kill_grace: float = 10.0
    # `{"id": "reach", "step": 2, "of": 3}` when this run is a chain's step.
    chain: Optional[dict] = None
    # The preset's parse rule (see recipes.parse_rule), and the one-line summary
    # it found in the finished log. Both stay empty for a preset without one.
    parse_rule: Optional[dict] = None
    summary: str = ""
    # The named groups that rule found, so a reader can trend a value across
    # runs without parsing the log again.
    fields: Dict[str, str] = field(default_factory=dict)
    # The preset's `expect:` regex (see recipes.expect_rule) and whether the
    # finished log held it. `expect_found` stays None until the run ends, and
    # for good on a preset that expects nothing.
    expect: Optional[str] = None
    expect_found: Optional[bool] = None
    # The preset's `success:` codes. None means the default, 0 alone.
    success_codes: Optional[list] = None
    # The preset's `scan:` flag — find this run's artifacts by scanning the
    # target folder instead of reading the $OUTDIR it owns. See recipes.py.
    scan_workspace: bool = False
    log_lines: list[str] = field(default_factory=list)
    artifact_delta: Optional[ArtifactDelta] = None
    # Free text the operator gave with `--note`: why this run was made.
    note: str = ""
    # Where the run was made from (vantage.vantage), read as it starts.
    vantage: Dict[str, str] = field(default_factory=dict)
    # The archived record, set when the manifest is written, so a front-end can
    # show exactly what was archived rather than rebuilding its own version.
    record: Optional[dict] = None

    # Captured at spawn so a later scope / log-destination change never
    # retroactively rewrites the path shown for a job already running.
    root: str = DEFAULT_ARTIFACT_ROOT
    stamp: str = ""
    out_dir: Optional[Path] = None

    # Set while the process is blocked on a prompt (see runner.py).
    await_prompt: Optional[str] = None
    await_since: Optional[float] = None

    @property
    def elapsed(self) -> float:
        """Wall time, frozen at the finish transition."""
        return (self.end_time or time.time()) - self.start_time

    @property
    def running(self) -> bool:
        return self.exit_code is None

    @property
    def awaiting(self) -> bool:
        return self.running and self.await_prompt is not None

    def elapsed_str(self) -> str:
        d = max(0, int(self.elapsed))
        return f"{d // 3600:02d}:{d // 60 % 60:02d}:{d % 60:02d}"
