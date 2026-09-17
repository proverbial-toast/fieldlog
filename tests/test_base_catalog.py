"""The shipped catalog has to run as shipped: nothing in it may depend on a
file that is not in the package."""

from __future__ import annotations

from pathlib import Path

from fieldlog.recipes import load_catalog


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
