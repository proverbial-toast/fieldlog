"""Recipe catalog: base yaml + `recipes.d/*.yaml` drop-ins, and the search matcher.

Load order is base-first, then every drop-in in lexical filename order — the
config dir, then `./recipes.d`, where a same-named local file wins. A
drop-in may add a tool or add variants to an existing one; a colliding
`tool/variant` id means last file wins. A drop-in can never delete a base
recipe, and one malformed file never takes down the catalog.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import shutil
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

import yaml

from fieldlog.state import template_vars

if TYPE_CHECKING:
    from fieldlog.state import TargetSession

RECIPES_PATH = Path(__file__).parent / "recipes.yaml"


def get_dropin_dir() -> Path:
    """Return user drop-in directory, respecting $XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "fieldlog" / "recipes.d"
    return Path.home() / ".config" / "fieldlog" / "recipes.d"


DROPIN_DIR = get_dropin_dir()

# Editor leftovers that must never take effect silently.
_SKIP_SUFFIXES = ("~", ".swp", ".swo", ".bak", ".orig", ".rej")

KNOWN_INSTALL: Dict[str, str] = {
    "mtr": "apt install mtr-tiny",
    "dig": "apt install dnsutils",
    "resolvectl": "systemd-resolved",
    "wrk": "apt install wrk",
    "ss": "apt install iproute2",
    "tcpdump": "apt install tcpdump",
    "iperf3": "apt install iperf3",
    "ethtool": "apt install ethtool",
    "traceroute": "apt install traceroute",
    "ping": "apt install iputils-ping",
    "curl": "apt install curl",
    "openssl": "apt install openssl",
}


@functools.lru_cache(maxsize=256)
def is_tool_installed(bin_name: str) -> bool:
    """Whether a binary exists in $PATH (cached; this runs on every keypress)."""
    return shutil.which(bin_name) is not None


# A scope value is interpolated into a shell command line (runner runs it via
# create_subprocess_shell), so anything outside this set — spaces, ; | & $ ` ( )
# etc. — could break out of the command. IPs, CIDRs and hostnames need none of it.
# A target may also be an ssh `user@host`: `@` means nothing to sh without the
# `$` or `(` that stay refused.
_UNSAFE_SCOPE = re.compile(r"[^A-Za-z0-9._:/-]")
_UNSAFE_TARGET = re.compile(r"[^A-Za-z0-9._:/@-]")


def unsafe_scope_chars(value: str, pattern: re.Pattern = _UNSAFE_SCOPE) -> str:
    """The distinct disallowed characters in a target/host, '' if it is clean."""
    return "".join(dict.fromkeys(pattern.findall(value or "")))


def is_blocked(tool: dict, preset: dict, session: TargetSession) -> Tuple[bool, str, str]:
    """(blocked, reason, hint). Missing binary and missing dns name are
    distinct reasons and must never be reported as each other."""
    bin_name = preset.get("bin", tool.get("bin", tool.get("id", "")))
    if not is_tool_installed(bin_name):
        return True, f"{bin_name}: not found in $PATH", KNOWN_INSTALL.get(bin_name, f"apt install {bin_name}")
    used = template_vars(preset.get("flags", ""))
    if "TARGET" in used:
        if not (session.target or "").strip():
            return True, "variant needs a target · set one in T → scope", "T → scope, then fill in target IP or subnet"
        bad = unsafe_scope_chars(session.target, _UNSAFE_TARGET)
        if bad:
            return True, f"target has unsafe characters ({bad}) · fix it in T → scope", "targets are IPs, CIDRs, hostnames or user@host — no shell metacharacters"
    if "HOST" in used:
        if not session.dns_name:
            return True, "variant needs a dns name · set one in T → scope", "T → scope, then fill in dns name"
        bad = unsafe_scope_chars(session.dns_name)
        if bad:
            return True, f"dns name has unsafe characters ({bad}) · fix it in T → scope", "hostnames are letters, digits, dots and hyphens — no shell metacharacters"
    if "LHOST" in used:
        lhost = session.effective_lhost()
        if not lhost:
            # An interface with no IPv4 would turn `-B $LHOST` into a bare `-B`.
            iface = session.interface or "the interface"
            return True, f"variant needs a local address · {iface} has no IPv4 address", "T → scope, then pick an interface that has an address or type iface / address · on the CLI, -i IFACE or -l ADDR"
        bad = unsafe_scope_chars(lhost)
        if bad:
            return True, f"local address has unsafe characters ({bad}) · fix it in T → scope", "local addresses are IPv4 or IPv6 — no shell metacharacters"
    if "IFACE" in used:
        # $IFACE is interpolated into the shell command like the scope above, so
        # it takes the same allowlist. An empty interface stays allowed: the
        # command just carries a blank, the same as before this check.
        bad = unsafe_scope_chars(session.interface)
        if bad:
            return True, f"interface has unsafe characters ({bad}) · fix it in T → scope", "interface names are letters, digits, dots, colons and hyphens — no shell metacharacters"
    return False, f"{bin_name} · in $PATH", ""


def writes_outdir(preset: dict, flags: Optional[str] = None) -> bool:
    """Whether `$OUTDIR` must exist before the preset runs.

    Either its flags (or `flags`, an edit of them) reference it, or the preset
    says `outdir: true` for a script that writes there from its environment,
    which no scan of the command line can see.
    """
    text = preset.get("flags", "") if flags is None else flags
    return preset.get("outdir") is True or "OUTDIR" in template_vars(str(text))


def find_recipe(catalog: Catalog, spec: str) -> Tuple[Optional[dict], Optional[dict], Optional[str]]:
    """Look up a recipe by '<tool>/<preset>' or '<tool>'.
    Returns (tool, preset, error_message). If tool only, uses first preset.
    If not found, error_message lists suggestions or available presets.
    """
    spec = (spec or "").strip()
    if not spec:
        return None, None, "No recipe specified."

    if "/" in spec:
        tool_id, preset_id = spec.split("/", 1)
        tool = next((t for t in catalog.tools if t["id"] == tool_id), None)
        if not tool:
            available_tools = [t["id"] for t in catalog.tools]
            matches = [t for t in available_tools if tool_id.lower() in t.lower() or t.lower() in tool_id.lower()]
            hint = f" Did you mean '{matches[0]}'?" if matches else " Run 'fieldlog list' to see available tools."
            return None, None, f"Unknown tool '{tool_id}'.{hint}"
        preset = next((p for p in tool.get("presets", []) if p["id"] == preset_id), None)
        if not preset:
            available = ", ".join(p["id"] for p in tool.get("presets", []))
            return None, None, f"Unknown preset '{preset_id}' for tool '{tool_id}'. Available presets: {available}"
        return tool, preset, None

    # Only tool_id specified
    tool = next((t for t in catalog.tools if t["id"] == spec), None)
    if not tool:
        available_tools = [t["id"] for t in catalog.tools]
        matches = [t for t in available_tools if spec.lower() in t.lower() or t.lower() in spec.lower()]
        hint = f" Did you mean '{matches[0]}'?" if matches else " Run 'fieldlog list' to see available tools."
        return None, None, f"Unknown recipe or tool '{spec}'.{hint}"
    presets = tool.get("presets", [])
    preset = presets[0] if presets else {"id": "default", "name": "default", "flags": ""}
    return tool, preset, None



def display_path(p: Path) -> str:
    """Render a path with ~ for the home prefix, as every surface shows it."""
    text = str(p)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + "/") else text


_WRAPPERS = ("timeout", "sudo", "doas", "env", "nice")


def format_command(bin_name: str, flags: str) -> str:
    """Format the executable command from binary and flags."""
    flags = (flags or "").strip()
    if not flags:
        return bin_name
    first_tok = flags.split(None, 1)[0]
    if flags == bin_name or flags.startswith(f"{bin_name} ") or first_tok in _WRAPPERS:
        return flags
    return f"{bin_name} {flags}"


def _bin_text(value) -> str:
    """A binary name as written. YAML reads a bare `bin: true` as a boolean,
    and `True` is not a program — the operator meant coreutils `true`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def normalize_recipe(r: dict) -> dict:
    """Ensure id, bin and presets exist."""
    item = dict(r)
    item["id"] = str(item.get("id", "")).strip()

    if "bin" not in item:
        item["bin"] = item.get("id", "")
    item["bin"] = _bin_text(item["bin"])

    bin_name = item["bin"]

    if "presets" not in item or not item["presets"]:
        flags = item.get("flags", "")
        preset_dict = {
            "id": "default",
            "name": item.get("name", "default"),
            "bin": bin_name,
            "flags": flags,
        }
        item["presets"] = [preset_dict]

    item["presets"] = [
        dict(p, **({"bin": _bin_text(p["bin"])} if "bin" in p else {})) for p in item["presets"]
    ]
    return item


def dropin_label(p: Path) -> str:
    """`./recipes.d/x.yaml` for a local drop-in, `~/…` for a config one."""
    try:
        return "./" + p.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return display_path(p)


def scan_dropins(dropin_dir: Optional[Path] = None) -> Tuple[List[Path], List[str]]:
    """(files in load order, shadowing warnings).

    With no explicit directory both the config dir and `./recipes.d` are
    scanned, config first, lexical within each. The same basename in both is
    loaded once: the local file wins, being the more specific of the two.
    """
    if dropin_dir is not None:
        dirs = [dropin_dir]
    else:
        dirs = [DROPIN_DIR, Path.cwd() / "recipes.d"]

    by_name: Dict[str, Path] = {}
    order: List[str] = []
    warnings: List[str] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.yaml"), key=lambda p: p.name):
            if not p.is_file() or p.name.startswith(".") or p.name.endswith(_SKIP_SUFFIXES):
                continue
            previous = by_name.get(p.name)
            if previous is None:
                order.append(p.name)
            elif previous.resolve() != p.resolve():
                warnings.append(
                    f"recipes.d/{p.name}: {dropin_label(previous)} shadowed by {dropin_label(p)}"
                )
            by_name[p.name] = p
    return [by_name[name] for name in order], warnings


def dropin_files(dropin_dir: Optional[Path] = None) -> List[Path]:
    """Every `*.yaml` in the drop-in directories, in load order."""
    return scan_dropins(dropin_dir)[0]


@dataclass
class Catalog:
    """One resolved catalog plus the provenance needed to explain it."""

    tools: List[dict] = field(default_factory=list)
    chains: List[dict] = field(default_factory=list)
    base_path: Path = RECIPES_PATH
    dropin_dir: Path = DROPIN_DIR
    # Filenames *scanned*, not merged — a list built from merged variants goes
    # empty on the second reload and reports "no drop-ins" while the manager
    # still lists two.
    files: List[str] = field(default_factory=list)
    # Where each of those filenames was actually read from — the config dir and
    # `./recipes.d` both feed `files`, so the name alone cannot say.
    file_paths: Dict[str, Path] = field(default_factory=dict)
    overrides: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def total_variants(self) -> int:
        return sum(len(t.get("presets", [])) for t in self.tools)

    def dropin_variant_count(self, filename: str) -> int:
        return sum(
            1 for t in self.tools for p in t.get("presets", []) if p.get("src") == filename
        )

    @property
    def base_variant_count(self) -> int:
        return sum(
            1 for t in self.tools for p in t.get("presets", []) if not p.get("src")
        )


def _read_file(path: Path) -> Tuple[List[dict], List[dict]]:
    """(tools, chains) from one yaml file. A list-form file is tools only."""
    data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    raw_chains: List[dict] = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("recipes", [])
        if not isinstance(raw, list):
            raise ValueError("`recipes:` is not a list")
        raw_chains = data.get("chains", []) or []
        if not isinstance(raw_chains, list):
            raise ValueError("`chains:` is not a list")
    else:
        raise ValueError("top level is not a mapping or list")

    out: List[dict] = []
    by_id: Dict[str, dict] = {}

    for r in raw:
        if not isinstance(r, dict) or "id" not in r:
            raise ValueError("a recipe entry has no id")
        norm = normalize_recipe(r)
        tid = norm["id"]
        if tid in by_id:
            existing = by_id[tid]
            preset_ids = {p["id"] for p in existing.get("presets", [])}
            for p in norm.get("presets", []):
                if p["id"] not in preset_ids:
                    existing["presets"].append(p)
                    preset_ids.add(p["id"])
        else:
            by_id[tid] = norm
            out.append(norm)

    return out, [normalize_chain(c) for c in raw_chains]


def _read_tools(path: Path) -> List[dict]:
    """Just the tools of a file — the shape every caller but load_catalog wants."""
    return _read_file(path)[0]


def _merge(tools: List[dict], incoming: List[dict], src: Optional[str], overrides: List[str]) -> None:
    """Merge `incoming` into `tools` in place. Last definition wins, and says so."""
    by_id = {t["id"]: t for t in tools}
    for new in incoming:
        target = by_id.get(new["id"])
        if target is None:
            tool = dict(new)
            tool["presets"] = [dict(p, **({"src": src} if src else {})) for p in new["presets"]]
            if src:
                tool["src"] = src
            tools.append(tool)
            by_id[tool["id"]] = tool
            continue
        for key in ("name", "bin"):
            if key in new and src is None:
                target[key] = new[key]
        existing = {p["id"]: i for i, p in enumerate(target["presets"])}
        for p in new["presets"]:
            merged = dict(p, **({"src": src} if src else {}))
            if p["id"] in existing:
                target["presets"][existing[p["id"]]] = merged
                if src:
                    overrides.append(f"{target['id']}/{p['id']} overridden by recipes.d/{src}")
            else:
                existing[p["id"]] = len(target["presets"])
                target["presets"].append(merged)


# ---- Chains ---------------------------------------------------------------


def normalize_chain(c: dict) -> dict:
    """Ensure id, name and a list of `{recipe, continue}` steps.

    A step written as a bare string is "stop on failure"; the recipe itself is
    left as written here and resolved to `tool/preset` once every file is in.
    """
    if not isinstance(c, dict) or "id" not in c:
        raise ValueError("a chain entry has no id")
    steps: List[dict] = []
    for s in c.get("steps") or []:
        if isinstance(s, dict):
            steps.append({"recipe": str(s.get("recipe", "")).strip(), "continue": bool(s.get("continue", False))})
        else:
            steps.append({"recipe": str(s).strip(), "continue": False})
    cid = str(c.get("id", "")).strip()
    return {
        "id": cid,
        "name": str(c.get("name") or cid),
        "steps": steps,
    }


def _merge_chains(chains: List[dict], incoming: List[dict], src: Optional[str], overrides: List[str]) -> None:
    """Last definition of an id wins, and says so — as preset overrides do."""
    by_id = {c["id"]: i for i, c in enumerate(chains)}
    for new in incoming:
        item = dict(new, **({"src": src} if src else {}))
        if item["id"] in by_id:
            chains[by_id[item["id"]]] = item
            if src:
                overrides.append(f"chain {item['id']} overridden by recipes.d/{src}")
        else:
            by_id[item["id"]] = len(chains)
            chains.append(item)


def _validate_chains(cat: Catalog) -> None:
    """Drop unusable chains, explaining each one. Runs after every file is
    merged: a base chain may legitimately reference a drop-in's preset."""
    tool_ids = {t["id"] for t in cat.tools}
    kept: List[dict] = []
    for chain in cat.chains:
        cid = chain["id"]
        if cid in tool_ids:
            cat.errors.append(f"chain {cid}: id collides with tool {cid} · skipped")
            continue
        if not chain["steps"]:
            cat.errors.append(f"chain {cid}: no steps · skipped")
            continue
        resolved: List[dict] = []
        unknown: Optional[str] = None
        for step in chain["steps"]:
            tool, preset, _err = find_recipe(cat, step["recipe"])
            if tool is None or preset is None:
                unknown = step["recipe"]
                break
            # A bare tool id is stored as the explicit recipe it resolved to.
            resolved.append({"recipe": f"{tool['id']}/{preset['id']}", "continue": step["continue"]})
        if unknown is not None:
            cat.errors.append(f"chain {cid}: unknown step {unknown} · skipped")
            continue
        chain["steps"] = resolved
        kept.append(chain)
    cat.chains = kept


def find_chain(catalog: Catalog, spec: str) -> Optional[dict]:
    """A chain by id. Ids cannot collide with tool ids, so trying this before
    find_recipe is not observable."""
    spec = (spec or "").strip()
    return next((c for c in catalog.chains if c["id"] == spec), None) if spec else None


def chain_steps(catalog: Catalog, chain: dict) -> List[Tuple[dict, dict, bool]]:
    """`(tool, preset, continue_on_failure)` per step. Steps are validated at
    load, so an unresolvable one here means the catalog was built by hand."""
    out: List[Tuple[dict, dict, bool]] = []
    for step in chain.get("steps", []):
        tool, preset, _err = find_recipe(catalog, step["recipe"])
        if tool is not None and preset is not None:
            out.append((tool, preset, bool(step.get("continue"))))
    return out


def chain_blocked(catalog: Catalog, chain: dict, session: TargetSession) -> Tuple[bool, str, str]:
    """(blocked, reason, hint) for the first step that cannot run now."""
    steps = chain_steps(catalog, chain)
    for index, (tool, preset, _cont) in enumerate(steps, start=1):
        blocked, reason, hint = is_blocked(tool, preset, session)
        if blocked:
            return True, f"step {index} {tool['id']}/{preset['id']}: {reason}", hint
    return False, f"{len(steps)} steps ready", ""


def chain_matches(chain: dict, q: str) -> bool:
    """Substring match over the text an operator would search a chain by."""
    if not q:
        return True
    hay = f"{chain['id']} {chain.get('name', '')} {chain_arrow(chain)}".lower()
    return q.lower() in hay


def chain_arrow(chain: dict, mark_continue: bool = True) -> str:
    """`ping/quick → traceroute/icmp? → dig/ptr`; `?` is continue-on-failure."""
    return " → ".join(
        s["recipe"] + ("?" if mark_continue and s.get("continue") else "")
        for s in chain.get("steps", [])
    )


def steps_label(chain: dict) -> str:
    """`1 step`, `3 steps`."""
    n = len(chain.get("steps", []))
    return f"{n} step{'' if n == 1 else 's'}"


def _where(exc: Exception) -> str:
    """`:12` when the parser knows the line, else nothing. One line, always —
    a System transcript entry that spans a raw YAML dump is unreadable."""
    mark = getattr(exc, "problem_mark", None)
    return f":{mark.line + 1}" if mark is not None else ""


def load_catalog(
    base: Optional[Path] = None,
    dropin_dir: Optional[Path] = None,
) -> Catalog:
    """Read the base file then every drop-in, tolerating a bad operator file.

    With no `dropin_dir` both the config dir and `./recipes.d` are scanned; an
    explicit one is scanned alone.
    """
    base = RECIPES_PATH if base is None else base
    cat = Catalog(base_path=base, dropin_dir=DROPIN_DIR if dropin_dir is None else dropin_dir)
    if base.exists():
        try:
            cat.tools, cat.chains = _read_file(base)
        except Exception as exc:  # noqa: BLE001 — a bad base is still not a crash
            cat.errors.append(f"{display_path(base)}{_where(exc)} invalid · no base recipes loaded")
            cat.tools, cat.chains = [], []

    paths, shadowed = scan_dropins(dropin_dir)
    cat.errors.extend(shadowed)

    bad: List[str] = []
    for path in paths:
        try:
            incoming, incoming_chains = _read_file(path)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"recipes.d/{path.name}{_where(exc)} invalid · skipped")
            continue
        _merge(cat.tools, incoming, path.name, cat.overrides)
        _merge_chains(cat.chains, incoming_chains, path.name, cat.overrides)
        cat.files.append(path.name)
        cat.file_paths[path.name] = path

    # Only now can a step be resolved: a base chain may name a drop-in's preset.
    _validate_chains(cat)

    # Never fail closed on one bad operator file: say what survived.
    n = len(cat.files)
    cat.errors.extend(f"{msg}, {n} other file{'' if n == 1 else 's'} loaded" for msg in bad)
    return cat


# ---- Search ---------------------------------------------------------------

BlockedFn = Callable[[dict, dict], bool]


def score(tool: dict, preset: dict, q: str) -> Optional[int]:
    """Scored match. Loose (subsequence) matching never sees the flag string —
    that is what lets a tool name match a curl variant through its `-w "…"` argument."""
    if not q:
        return 3
    bin_name = str(tool.get("bin", tool.get("id", ""))).lower()
    name = f"{tool.get('bin', tool.get('id', ''))} {preset.get('name', preset.get('id', ''))}".lower()
    flags = str(preset.get("flags", "")).lower()

    if bin_name.startswith(q):
        return 0
    if q in bin_name:
        return 1
    if q in name:
        return 2
    if q in flags:
        return 3
    i = 0
    for ch in q:
        i = name.find(ch, i)
        if i == -1:
            return None
        i += 1
    return 4


def search(
    tools: List[dict],
    query: str,
    blocked_fn: BlockedFn,
    hide_missing: bool = True,
    limit: int = 40,
) -> List[Tuple[dict, dict, bool]]:
    """Ranked `(tool, preset, blocked)` hits: runnable first, then score."""
    q = (query or "").strip().lower()
    hits = []
    for t in tools:
        for p in t.get("presets", []):
            blocked = bool(blocked_fn(t, p))
            if hide_missing and blocked:
                continue
            s = score(t, p, q)
            if s is None:
                continue
            hits.append((1 if blocked else 0, s, t, p, blocked))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [(t, p, b) for _, _, t, p, b in hits[:limit]]
