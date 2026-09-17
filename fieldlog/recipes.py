"""Recipe catalog: base yaml + `recipes.d/*.yaml` drop-ins, and the search matcher.

Load order is base-first, then every drop-in in lexical filename order — the
config dir, then `./recipes.d`, where a same-named local file wins. One merge
rule holds everywhere, inside a file and across them: a repeated tool id adds
its variants to the tool defined first, a repeated `tool/variant` id replaces
the earlier one, and a `name:` or `bin:` restated on a tool that already exists
is ignored and said so. A drop-in can never delete a base recipe, and one
malformed file never takes down the catalog.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import shutil
import sys
from string import Formatter
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

import yaml

from fieldlog.state import is_ip_address, template_vars

if TYPE_CHECKING:
    from fieldlog.state import TargetSession

RECIPES_PATH = Path(__file__).parent / "recipes.yaml"

# libyaml when the wheel was built with it, which is 8x the pure-Python parser
# and the difference between a 12 ms and a 1.5 ms catalog load — paid by every
# CLI invocation and every TUI reload. Same safe subset either way; a build
# without libyaml falls back rather than failing.
_YamlLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _parse_yaml(text: str):
    """`yaml.safe_load` by another name, so the loader is chosen in one place."""
    return yaml.load(text, Loader=_YamlLoader)


def get_dropin_dir() -> Path:
    """Return user drop-in directory, respecting $XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "fieldlog" / "recipes.d"
    return Path.home() / ".config" / "fieldlog" / "recipes.d"


DROPIN_DIR = get_dropin_dir()

# Editor leftovers that must never take effect silently.
_SKIP_SUFFIXES = ("~", ".swp", ".swo", ".bak", ".orig", ".rej")


# One listing per $PATH directory, reused across lookups. shutil.which stats
# <binary> in every directory, so a catalog of 20 tools restats the whole of
# $PATH 20 times; a miss is the expensive case, because it reaches the end.
# That is 646 stat calls and 226 ms of a `fieldlog list` on a WSL $PATH, where
# half the entries are Windows directories behind a 9p mount. Listing each
# directory once instead answers every lookup from memory.
_PATH_DIR_NAMES: Dict[str, frozenset] = {}


def _names_in(directory: str) -> frozenset:
    """Every filename in a $PATH directory; empty for one we cannot read."""
    names = _PATH_DIR_NAMES.get(directory)
    if names is None:
        try:
            with os.scandir(directory) as entries:
                names = frozenset(entry.name for entry in entries)
        except OSError:
            names = frozenset()
        _PATH_DIR_NAMES[directory] = names
    return names


@functools.lru_cache(maxsize=256)
def is_tool_installed(bin_name: str) -> bool:
    """Whether a binary exists in $PATH (cached; this runs on every keypress).

    The directory listing is only a filter: a name that is present still has to
    pass the same executable-and-not-a-directory test shutil.which applies, and
    the directories are walked in $PATH order, so the answer is the one
    shutil.which would give. A name with a separator in it is not a $PATH
    lookup at all, and is handed straight back to shutil.which.
    """
    if not bin_name or os.path.dirname(bin_name):
        return shutil.which(bin_name) is not None
    seen = set()
    for directory in os.environ.get("PATH", os.defpath).split(os.pathsep):
        # A $PATH with the same directory six times over is normal; so is an
        # empty entry, which shutil.which skips rather than reading as cwd.
        if not directory or directory in seen:
            continue
        seen.add(directory)
        if bin_name not in _names_in(directory):
            continue
        candidate = os.path.join(directory, bin_name)
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return True
    return False


def clear_tool_cache() -> None:
    """Forget what is installed and where, so a reload sees a tool added since
    boot. Both caches go together: a stale listing would outlive the verdict
    built from it."""
    _PATH_DIR_NAMES.clear()
    is_tool_installed.cache_clear()


def timeout_binary() -> Optional[str]:
    """The coreutils `timeout` on this box, or None when it has neither.

    Darwin ships no coreutils `timeout`; Homebrew's coreutils installs it as
    `gtimeout` to keep it clear of the BSD userland. Resolved when a launch is
    planned rather than at import, so installing coreutils takes effect without
    a restart, and answered from the same $PATH cache every other lookup uses.
    """
    for name in ("timeout", "gtimeout"):
        if is_tool_installed(name):
            return name
    return None


# A scope value is interpolated into a shell command line (runner runs it via
# create_subprocess_shell), so anything outside this set — spaces, ; | & $ ` ( )
# etc. — could break out of the command. IPs, CIDRs and hostnames need none of it.
# A target may also be an ssh `user@host`: `@` means nothing to sh without the
# `$` or `(` that stay refused.
_UNSAFE_SCOPE = re.compile(r"[^A-Za-z0-9._:/-]")
_UNSAFE_TARGET = re.compile(r"[^A-Za-z0-9._:/@-]")

# Anything an operator typing an IPv4 address could produce, right or wrong.
_DOTTED = re.compile(r"[\d.]+")


def unsafe_scope_chars(value: str, pattern: re.Pattern = _UNSAFE_SCOPE) -> str:
    """The distinct disallowed characters in a target/host, '' if it is clean."""
    return "".join(dict.fromkeys(pattern.findall(value or "")))


@dataclass(frozen=True)
class Verdict:
    """Whether a recipe can run against a scope, and what kind of gap stops it.

    `kind` is one of `ready | binary | target | dns | lhost | interface`: what
    the reader would have to go and fix. It is decided here, beside the reason
    text, so doctor's buckets and the palette's labels read one answer instead
    of each re-deriving it from the English.

    `missing` splits the two things a reader is told to do: set the value, or
    look at the value that is already there. Only an unset scope value is
    missing — one that is set and refused is not, and telling the operator to
    set it sends them to fill in what is already filled in.
    """

    blocked: bool
    kind: str
    reason: str
    missing: bool = False


def check_recipe(
    tool: dict, preset: dict, session: TargetSession, flags: Optional[str] = None
) -> Verdict:
    """The verdict for one recipe now. Reasons are terse and surface-neutral —
    the caller frames them (a CLI error, doctor's marks, the TUI's disabled
    button). Missing binary and missing dns name are distinct reasons and must
    never be reported as each other.

    `flags` is the template that will actually run, when that is not the
    preset's own — a TUI args edit. The gate has to read what launches, not what
    the catalog says: an edit can introduce a `$VAR` the preset never used, and
    checking the preset would pass it through unvalidated.
    """
    bin_name = preset.get("bin", tool.get("bin", tool.get("id", "")))
    if not is_tool_installed(bin_name):
        return Verdict(True, "binary", f"{bin_name}: not found in $PATH")
    used = template_vars(preset.get("flags", "") if flags is None else flags)
    if "TARGET" in used:
        if not (session.target or "").strip():
            return Verdict(True, "target", "needs a target", missing=True)
        # A scope value is pasted in after the tool's own flags, so a leading
        # `-` is read as one more flag: `--target=-f` would arm ping's flood.
        # A `-` inside the value (`box-1`, `10-0-0-1.example`) is fine.
        target = session.target.strip()
        if target.startswith("-"):
            return Verdict(True, "target", "target must not start with -")
        # Digits and dots is an address being typed, so a typo in one (`1.2.3`,
        # `10.0.0.256`) is a mistake worth catching rather than a hostname.
        if _DOTTED.fullmatch(target) and not is_ip_address(target):
            return Verdict(True, "target", "target is not a valid address")
        bad = unsafe_scope_chars(session.target, _UNSAFE_TARGET)
        if bad:
            return Verdict(True, "target", f"target has unsafe characters ({bad})")
    if "HOST" in used:
        if not session.dns_name:
            return Verdict(True, "dns", "needs a dns name", missing=True)
        if session.dns_name.startswith("-"):
            return Verdict(True, "dns", "dns name must not start with -")
        bad = unsafe_scope_chars(session.dns_name)
        if bad:
            return Verdict(True, "dns", f"dns name has unsafe characters ({bad})")
    if "LHOST" in used:
        lhost = session.effective_lhost()
        if not lhost:
            # An interface with no IPv4 would turn `-B $LHOST` into a bare `-B`.
            iface = session.interface or "the interface"
            return Verdict(
                True, "lhost", f"needs a local address · {iface} has no IPv4 address", missing=True
            )
        if lhost.startswith("-"):
            return Verdict(True, "lhost", "local address must not start with -")
        bad = unsafe_scope_chars(lhost)
        if bad:
            return Verdict(True, "lhost", f"local address has unsafe characters ({bad})")
    if "IFACE" in used:
        # $IFACE is interpolated into the shell command like the scope above, so
        # it takes the same allowlist. An empty interface stays allowed: the
        # command just carries a blank, the same as before this check.
        if (session.interface or "").startswith("-"):
            return Verdict(True, "interface", "interface must not start with -")
        bad = unsafe_scope_chars(session.interface)
        if bad:
            return Verdict(True, "interface", f"interface has unsafe characters ({bad})")
    return Verdict(False, "ready", f"{bin_name} · in $PATH")


def is_blocked(
    tool: dict, preset: dict, session: TargetSession, flags: Optional[str] = None
) -> Tuple[bool, str]:
    """`(blocked, reason)` — check_recipe for a caller that wants only the text."""
    verdict = check_recipe(tool, preset, session, flags)
    return verdict.blocked, verdict.reason


def writes_outdir(preset: dict, flags: Optional[str] = None) -> bool:
    """Whether `$OUTDIR` must exist before the preset runs.

    Either its flags (or `flags`, an edit of them) reference it, or the preset
    says `outdir: true` for a script that writes there from its environment,
    which no scan of the command line can see.
    """
    text = preset.get("flags", "") if flags is None else flags
    return preset.get("outdir") is True or "OUTDIR" in template_vars(str(text))


def scans_workspace(preset: dict) -> bool:
    """Whether this recipe's artifacts have to be found by scanning the target
    folder, rather than read from the `$OUTDIR` it owns.

    Off by default: a run records its primary log and whatever it wrote into
    `$OUTDIR`, both of which belong to it alone. A recipe that writes somewhere
    else under the target folder — a tool with a fixed output name, or one
    writing into the working directory — declares `scan: true` and gets every
    file that appeared or changed while it ran instead. That is only accurate
    when nothing else is running against the same target.
    """
    return preset.get("scan") is True


# A summary sits on one line of `history` beside the exit code, so it is capped
# rather than allowed to push the columns off the terminal.
PARSE_SUMMARY_MAX = 120


def parse_rule(preset: dict) -> Optional[dict]:
    """The preset's `parse:`/`summary:` pair, or None when it has no rule.

    Carried on the job so the runner can summarise the log it just wrote without
    reaching back into the catalog the run was planned from — the same reason
    the scope and paths are captured at spawn.
    """
    pattern = str(preset.get("parse", "") or "")
    if not pattern:
        return None
    return {"parse": pattern, "summary": str(preset.get("summary", "") or "")}


def parse_match(rule: Optional[dict], text: str) -> Optional[re.Match]:
    """The match `rule` makes in `text`, or None when there is no rule or no match.

    The last match wins, because the numbers worth keeping are a tool's closing
    stats and a `-c 4` ping writes four lines that look much like them. A rule
    that survived the load has a compiling regex, so an operator's file cannot
    fail here — the guard is for a rule handed in from somewhere else.
    """
    if not rule:
        return None
    try:
        matches = list(re.finditer(rule["parse"], text, re.MULTILINE))
    except re.error:
        return None
    return matches[-1] if matches else None


def fields_from_match(match: Optional[re.Match]) -> Dict[str, str]:
    """The named groups of a match already made, `None` read as ''.

    Kept on the record beside the formatted summary, so a value can be trended
    across runs without re-parsing the logs. A rule with no named groups, or one
    that did not match, contributes nothing.
    """
    return {k: v or "" for k, v in match.groupdict().items()} if match else {}


def summary_from_match(rule: Optional[dict], match: Optional[re.Match]) -> str:
    """The one-line summary for a match already made: '' when there was none.

    A rule that survived the load has a template naming only groups its regex
    defines, so the guards below are for a rule handed in from somewhere else.
    """
    if match is None:
        return ""
    template = (rule or {}).get("summary", "")
    try:
        found = template.format_map(fields_from_match(match)) if template else match.group(0)
    except (KeyError, IndexError, ValueError):
        return ""
    return " ".join(found.split())[:PARSE_SUMMARY_MAX]


def parse_fields(rule: Optional[dict], text: str) -> Dict[str, str]:
    """`fields_from_match` for a rule and a text — the one-shot form."""
    return fields_from_match(parse_match(rule, text))


def parse_summary(rule: Optional[dict], text: str) -> str:
    """`summary_from_match` for a rule and a text — the one-shot form.

    A caller that wants both a summary and its fields matches once instead (see
    parse_match): the regex is an operator's, run over 64 KB of log.
    """
    return summary_from_match(rule, parse_match(rule, text))


def expect_rule(preset: dict) -> Optional[str]:
    """The preset's `expect:` regex, or None when it has no expectation.

    Carried on the job like the parse rule, and for the same reason: the runner
    settles the verdict from the log it just wrote, without reaching back into
    the catalog the run was planned from.
    """
    pattern = str(preset.get("expect", "") or "")
    return pattern or None


def expect_found(pattern: Optional[str], text: str) -> Optional[bool]:
    """Whether `pattern` is anywhere in `text`; None when there is no pattern.

    Any match counts — this is a verdict, not a reading, so the last-match rule
    parse_match follows has nothing to decide here. A rule that survived the
    load compiles, so the guard is for one handed in from somewhere else.
    """
    if not pattern:
        return None
    try:
        return re.search(pattern, text, re.MULTILINE) is not None
    except re.error:
        return None


def success_codes(preset: dict) -> Optional[List[int]]:
    """The exit codes this preset calls a success, or None to mean 0 alone.

    A tool that reports "nothing found" as 1, or a `--help` that exits 2, has
    not failed. Saying so on the recipe keeps that judgement with the thing that
    knows it, instead of in every reader of the archive.
    """
    codes = preset.get("success")
    return list(codes) if isinstance(codes, list) and codes else None


def run_succeeded(code: object, codes: Optional[List[int]] = None) -> bool:
    """Whether `code` counts as success under `codes` (default: 0 alone).

    The exit-code half of the verdict, and a function of its own because
    `success:` is its own concept with its own tests. `run_passed` is the one
    production caller and the one a reader should call: a run's verdict is its
    code *and* its expectation, never the code alone.
    """
    try:
        value = int(code)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False
    return value in codes if codes else value == 0


def run_passed(code: object, codes: Optional[List[int]] = None, found: Optional[bool] = None) -> bool:
    """Whether a run passed: the definition, and the only one.

    A run passes when its exit code is one the recipe declared a success and
    its expectation, when it has one, was found in the log. `found` is None for
    a recipe that expects nothing, which is not a failure — it is no claim.
    `run_succeeded` is the exit-code half of it.
    """
    return run_succeeded(code, codes) and found is not False


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


_WRAPPERS = ("timeout", "gtimeout", "sudo", "doas", "env", "nice")


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
    # A variant replaced by a later file — expected, and worth saying. The same
    # id twice inside one file is a mistake instead, and is reported in `errors`.
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


def platform_key(name: Optional[str] = None) -> str:
    """`sys.platform`, or a name handed in, as a recipe writes it.

    sys.platform carries the release on some systems — `freebsd14` — and a
    recipe names the system, not the release, so only the leading word is kept.
    The vocabulary is deliberately open: `linux` and `darwin` are what the
    shipped catalog uses, and anything else an operator's box calls itself
    works the same way, because this is only ever compared against itself.
    """
    text = (sys.platform if name is None else str(name)).strip().lower()
    match = re.match(r"[a-z]+", text)
    return match.group(0) if match else text


def _platform_names(value) -> Optional[List[str]]:
    """The platform names a `platform:` value lists, or None when it is not names.

    One string is one name; a list is several. A key written and left empty —
    `platform:`, `platform: ""`, `platform: []` — lists nothing and is the same
    as absent, as an empty `presets:` or `expect:` is: the entry exists
    everywhere. None means the value is not a platform name at all (`platform:
    3`), which is the operator's mistake, not a filter.
    """
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    if not all(isinstance(v, str) for v in values):
        return None
    return [key for key in (platform_key(v) for v in values) if key]


def _wrong_platform(entry: dict, platform: str) -> bool:
    """Whether `platform:` puts this entry on some system other than `platform`.

    A value that names nothing, or names no platform at all, leaves the entry
    everywhere; the second kind is reported once the catalog is merged
    (_validate_platform), where the entry can still be named by the file it
    came from.
    """
    if "platform" not in entry:
        return False
    names = _platform_names(entry["platform"])
    return bool(names) and platform not in names


def _read_file(path: Path, platform: Optional[str] = None) -> Tuple[List[Tuple[dict, frozenset]], List[dict]]:
    """(entries, chains) from one yaml file. A list-form file is tools only.

    An entry is `(tool, keys as written)`: nothing is merged here, not even two
    entries of the same file, so that `_merge` is the only place a collision is
    decided. The raw keys ride along because normalize_recipe fills `bin` in for
    every tool, and only the file says whether the operator typed it.

    `platform:` is applied here, before any of that: an entry for another system
    does not exist on this one. Filtering as the file is read is what lets the
    same `tool/preset` id be written once per platform — two entries in the file,
    one in the catalog — instead of reading as a definition repeated.
    """
    platform = platform_key(platform)
    data = _parse_yaml(path.read_text(encoding="utf-8", errors="replace")) or {}
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

    entries: List[Tuple[dict, frozenset]] = []
    for r in raw:
        if not isinstance(r, dict) or "id" not in r:
            raise ValueError("a recipe entry has no id")
        # A tool's own `platform:` carries its presets with it; a preset's is its
        # own. Either way the entry is gone rather than disabled — a recipe for
        # another system is not a recipe this box can be told about.
        if _wrong_platform(r, platform):
            continue
        item = normalize_recipe(r)
        item["presets"] = [p for p in item["presets"] if not _wrong_platform(p, platform)]
        if not item["presets"]:
            continue
        # `presets:` left empty or null is the same as absent: nothing written.
        entries.append((item, frozenset(k for k in r if k != "presets" or r["presets"])))

    return entries, [normalize_chain(c) for c in raw_chains]


# What a second definition of a tool is for, and what it is not for — said once
# here because the same sentence is the answer for both `name:` and `bin:`.
_IGNORED_TOOL_KEY = {
    "bin": "a later file adds presets to a tool, not a new bin · set bin: on the preset",
    "name": "the first definition names the tool",
}


def _merge(
    tools: List[dict],
    entries: List[Tuple[dict, frozenset]],
    src: Optional[str],
    overrides: List[str],
    errors: List[str],
    label: str,
) -> None:
    """Fold one file's entries into `tools` in place, saying what it changed.

    The whole merge rule, and the only one: a repeated tool id adds its presets
    to the tool defined first, a repeated `tool/preset` id replaces the earlier
    preset, and `name:`/`bin:` written again on a tool that already exists is
    ignored. The base file comes through here too (`src=None`), so a drop-in and
    the shipped catalog cannot be merged by different rules.

    Nothing is ever dropped over a message: a repeat inside one file is a
    mistake worth reporting, but the preset the operator wrote last still wins.
    """
    by_id = {t["id"]: t for t in tools}
    written: set = set()                # `tool/preset` keys this file has placed
    for new, as_written in entries:
        tid = new["id"]
        target = by_id.get(tid)
        if target is None:
            target = dict(new, presets=[], **({"src": src} if src else {}))
            tools.append(target)
            by_id[tid] = target
        else:
            for key, why in _IGNORED_TOOL_KEY.items():
                # Only a value that differs is worth a line: a drop-in that
                # copies the base entry, `bin:` and all, before adding to it
                # has restated the tool, not tried to change it.
                if key in as_written and new.get(key) != target.get(key):
                    errors.append(f"{label}: {tid}: {key} ignored · {why}")
            # An entry with no recipe of its own has nothing left to add.
            # normalize_recipe gives every tool a `default` preset to stand in
            # for absent ones; merged here it would be a phantom recipe.
            if "presets" not in as_written and "flags" not in as_written:
                continue

        index = {p["id"]: i for i, p in enumerate(target["presets"])}
        for p in new["presets"]:
            pid = p["id"]
            merged = dict(p, **({"src": src} if src else {}))
            if pid in index:
                target["presets"][index[pid]] = merged
                if f"{tid}/{pid}" in written:
                    errors.append(f"{label}: {tid}/{pid} defined twice · last one kept")
                elif src:
                    overrides.append(f"{tid}/{pid} overridden by recipes.d/{src}")
            else:
                index[pid] = len(target["presets"])
                target["presets"].append(merged)
            written.add(f"{tid}/{pid}")


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


def _merge_chains(
    chains: List[dict],
    incoming: List[dict],
    src: Optional[str],
    overrides: List[str],
    errors: List[str],
    label: str,
) -> None:
    """The preset rule, for chains: a repeated id replaces the earlier chain —
    an override across files, a mistake inside one. The base file comes through
    here too, so a chain id repeated in it is one chain, not two."""
    by_id = {c["id"]: i for i, c in enumerate(chains)}
    written: set = set()                # chain ids this file has placed
    for new in incoming:
        item = dict(new, **({"src": src} if src else {}))
        cid = item["id"]
        if cid in by_id:
            chains[by_id[cid]] = item
            if cid in written:
                errors.append(f"{label}: chain {cid} defined twice · last one kept")
            elif src:
                overrides.append(f"chain {cid} overridden by recipes.d/{src}")
        else:
            by_id[cid] = len(chains)
            chains.append(item)
        written.add(cid)


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


def check_chain(
    catalog: Catalog,
    chain: dict,
    session: TargetSession,
    flags_overrides: Optional[Dict[str, str]] = None,
) -> Verdict:
    """The verdict of the first step that cannot run now, naming that step.

    The kind and the missing/refused split are the step's own, so a chain
    blocked on an unset target is bucketed exactly as the recipe would be.

    `flags_overrides` maps a recipe key to the template that step will run with,
    so the gate sees the same args `run_chain` will hand it.
    """
    steps = chain_steps(catalog, chain)
    overrides = flags_overrides or {}
    for index, (tool, preset, _cont) in enumerate(steps, start=1):
        key = f"{tool['id']}/{preset['id']}"
        step = check_recipe(tool, preset, session, flags=overrides.get(key))
        if step.blocked:
            return Verdict(True, step.kind, f"step {index} {key}: {step.reason}", step.missing)
    return Verdict(False, "ready", f"{len(steps)} steps ready")


def chain_blocked(
    catalog: Catalog,
    chain: dict,
    session: TargetSession,
    flags_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """`(blocked, reason)` — check_chain for a caller that wants only the text."""
    verdict = check_chain(catalog, chain, session, flags_overrides)
    return verdict.blocked, verdict.reason


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


def _validate_parsers(cat: Catalog) -> None:
    """Drop a `parse:` rule that could never produce a summary, and say why.

    The preset itself survives: a typo in an operator's regex costs the summary,
    not the recipe. Checking the template against the regex's group names here
    is what lets parse_summary treat a loaded rule as sound.
    """
    for tool in cat.tools:
        for preset in tool.get("presets", []):
            pattern = preset.get("parse")
            if not pattern:
                continue
            src = f"recipes.d/{preset['src']}: " if preset.get("src") else ""
            where = f"{src}{tool['id']}/{preset.get('id', 'default')}"
            try:
                names = set(re.compile(str(pattern)).groupindex)
            except re.error as exc:
                cat.errors.append(f"{where} parse: invalid regex · {exc.msg} · no summary")
                preset.pop("parse", None)
                continue

            template = str(preset.get("summary", "") or "")
            try:
                fields = [f for _, f, _, _ in Formatter().parse(template) if f]
            except ValueError as exc:
                cat.errors.append(f"{where} summary: {exc} · no summary")
                preset.pop("parse", None)
                continue

            unknown = [f for f in fields if f not in names]
            if unknown:
                cat.errors.append(
                    f"{where} summary: parse: has no group named {unknown[0]} · no summary"
                )
                preset.pop("parse", None)


def _validate_expect(cat: Catalog) -> None:
    """Drop an `expect:` regex that does not compile, and say why.

    The same bargain a parse rule gets: the preset survives, now without a
    check, rather than a recipe disappearing over a typo. An `expect:` written
    and left empty says nothing, so it goes quietly.
    """
    for tool in cat.tools:
        for preset in tool.get("presets", []):
            if "expect" not in preset:
                continue
            pattern = str(preset.get("expect") or "")
            if not pattern:
                preset.pop("expect", None)
                continue
            src = f"recipes.d/{preset['src']}: " if preset.get("src") else ""
            where = f"{src}{tool['id']}/{preset.get('id', 'default')}"
            try:
                re.compile(pattern)
            except re.error as exc:
                cat.errors.append(f"{where} expect: invalid regex · {exc.msg} · no check")
                preset.pop("expect", None)


def _validate_success(cat: Catalog) -> None:
    """Normalise `success:` to a list of exit codes, dropping what cannot be one.

    Same bargain as a parse rule: an unusable value costs the declaration and is
    reported, never the recipe. The default — 0 alone — is what it falls back to.
    """
    for tool in cat.tools:
        for preset in tool.get("presets", []):
            raw = preset.get("success")
            if raw is None:
                continue
            src = f"recipes.d/{preset['src']}: " if preset.get("src") else ""
            where = f"{src}{tool['id']}/{preset.get('id', 'default')}"

            values = raw if isinstance(raw, list) else [raw]
            codes: List[int] = []
            bad: object = None
            for value in values:
                # bool is an int in Python; `success: true` is a mistake, not 1.
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                    bad = value
                    break
                codes.append(value)

            if bad is not None:
                cat.errors.append(f"{where} success: {bad!r} is not an exit code · ignored")
                preset.pop("success", None)
                continue
            if not codes:
                cat.errors.append(f"{where} success: lists no exit code · ignored")
                preset.pop("success", None)
                continue
            preset["success"] = codes


def _validate_platform(cat: Catalog) -> None:
    """Report a `platform:` value that names no platform; the entry stays everywhere.

    The filtering itself happens when a file is read, before anything is merged,
    so only a value the filter could make nothing of reaches this far. It is
    reported here rather than there because only a merged entry carries the
    `src` that names the drop-in it was written in.
    """
    for tool in cat.tools:
        entries = [(tool, tool["id"])]
        entries += [(p, f"{tool['id']}/{p.get('id', 'default')}") for p in tool.get("presets", [])]
        for entry, name in entries:
            if "platform" not in entry or _platform_names(entry["platform"]) is not None:
                continue
            src = f"recipes.d/{entry['src']}: " if entry.get("src") else ""
            cat.errors.append(
                f"{src}{name} platform: {entry['platform']!r} is not a platform name · ignored"
            )
            entry.pop("platform", None)


def load_catalog(
    base: Optional[Path] = None,
    dropin_dir: Optional[Path] = None,
    platform: Optional[str] = None,
) -> Catalog:
    """Read the base file then every drop-in, tolerating a bad operator file.

    With no `dropin_dir` both the config dir and `./recipes.d` are scanned; an
    explicit one is scanned alone. `platform` is the system whose recipes to
    load, this box's by default; an entry marked for another one never enters
    the catalog, so what `list` and `doctor` show is this platform's catalog.
    """
    base = RECIPES_PATH if base is None else base
    platform = platform_key(platform)
    cat = Catalog(base_path=base, dropin_dir=DROPIN_DIR if dropin_dir is None else dropin_dir)
    if base.exists():
        try:
            entries, base_chains = _read_file(base, platform)
        except Exception as exc:  # noqa: BLE001 — a bad base is still not a crash
            cat.errors.append(f"{display_path(base)}{_where(exc)} invalid · no base recipes loaded")
            cat.tools, cat.chains = [], []
        else:
            # The base goes through the same merge as a drop-in, untagged: a
            # duplicate id in the shipped file is the same mistake as in one of
            # the operator's, and is worth the same message.
            _merge(cat.tools, entries, None, cat.overrides, cat.errors, display_path(base))
            _merge_chains(cat.chains, base_chains, None, cat.overrides, cat.errors, display_path(base))

    paths, shadowed = scan_dropins(dropin_dir)
    cat.errors.extend(shadowed)

    bad: List[str] = []
    for path in paths:
        try:
            incoming, incoming_chains = _read_file(path, platform)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"recipes.d/{path.name}{_where(exc)} invalid · skipped")
            continue
        _merge(
            cat.tools, incoming, path.name, cat.overrides, cat.errors,
            f"recipes.d/{path.name}",
        )
        _merge_chains(
            cat.chains, incoming_chains, path.name, cat.overrides, cat.errors,
            f"recipes.d/{path.name}",
        )
        cat.files.append(path.name)
        cat.file_paths[path.name] = path

    # Only now can a step be resolved: a base chain may name a drop-in's preset.
    _validate_chains(cat)
    _validate_parsers(cat)
    _validate_expect(cat)
    _validate_success(cat)
    _validate_platform(cat)

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
