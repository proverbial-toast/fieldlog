"""The shipped catalog has to run as shipped: nothing in it may depend on a
file that is not in the package."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fieldlog.recipes import format_command, load_catalog


def _base(tmp_path: Path):
    """The shipped catalog alone — no drop-in on this machine is scanned."""
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(dropin_dir=dropins)


def test_the_shipped_catalog_loads_without_errors(tmp_path: Path):
    cat = _base(tmp_path)
    assert cat.errors == []
    # Every base chain resolved: a dropped one is only reported, never fatal.
    assert cat.chains


def test_no_shipped_preset_reads_its_format_from_a_file(tmp_path: Path):
    # `curl -w @fmt` reads the format from a file named `fmt` in the job's cwd,
    # and no such file was ever shipped — curl exited 26 every time.
    offenders = [
        f"{tool['id']}/{preset.get('id', 'default')}"
        for tool in _base(tmp_path).tools
        for preset in tool.get("presets", [])
        if "@fmt" in str(preset.get("flags", ""))
    ]
    assert offenders == []


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_every_shipped_preset_parses_as_shell(tmp_path: Path, platform: str):
    """`flags` is shell, and a good half of this catalog is a one-liner with an
    awk program quoted inside it. `sh -n` parses the command without running a
    byte of it, which is the check a quoting slip cannot get past — and the
    only one that covers the other platform's presets, since they are filtered
    out of this machine's catalog before anything else ever sees them."""
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    cat = load_catalog(dropin_dir=dropins, platform=platform)

    unparseable = []
    for tool in cat.tools:
        for preset in tool.get("presets", []):
            command = format_command(preset.get("bin", tool["bin"]), preset.get("flags", ""))
            parsed = subprocess.run(
                ["/bin/sh", "-n"], input=command, text=True, capture_output=True,
            )
            if parsed.returncode:
                unparseable.append(f"{tool['id']}/{preset['id']}: {parsed.stderr.strip()}")

    assert unparseable == []
