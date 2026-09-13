"""Drop-ins load from the config dir and from `./recipes.d`, local wins."""

from __future__ import annotations

import textwrap
from pathlib import Path


def _yaml(flags: str) -> str:
    return textwrap.dedent(
        f"""
        recipes:
          - id: zztool
            bin: true
            presets:
              - id: a
                flags: "{flags}"
        """
    )


def _dirs(tmp_path: Path, monkeypatch):
    """(config recipes.d, local recipes.d) with DROPIN_DIR and cwd pointed at them."""
    from fieldlog import recipes as recipes_mod

    config_dir = tmp_path / "config" / "recipes.d"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(recipes_mod, "DROPIN_DIR", config_dir)

    work = tmp_path / "work"
    local_dir = work / "recipes.d"
    local_dir.mkdir(parents=True)
    monkeypatch.chdir(work)
    return config_dir, local_dir


def test_local_recipes_d_is_scanned(tmp_path: Path, monkeypatch):
    from fieldlog.recipes import load_catalog

    config_dir, local_dir = _dirs(tmp_path, monkeypatch)
    (local_dir / "zz.yaml").write_text(_yaml("--local"), encoding="utf-8")

    cat = load_catalog()
    assert [t["id"] for t in cat.tools if t["id"] == "zztool"] == ["zztool"]
    assert "zz.yaml" in cat.files
    assert cat.file_paths["zz.yaml"].resolve() == (local_dir / "zz.yaml").resolve()

    # An explicit directory is scanned alone: the local one is not consulted.
    only_config = load_catalog(dropin_dir=config_dir)
    assert not any(t["id"] == "zztool" for t in only_config.tools)
    assert only_config.files == []


def test_local_file_shadows_the_config_one_of_the_same_name(tmp_path: Path, monkeypatch):
    from fieldlog.recipes import load_catalog

    config_dir, local_dir = _dirs(tmp_path, monkeypatch)
    (config_dir / "zz.yaml").write_text(_yaml("--config"), encoding="utf-8")
    (local_dir / "zz.yaml").write_text(_yaml("--local"), encoding="utf-8")

    cat = load_catalog()
    tool = next(t for t in cat.tools if t["id"] == "zztool")
    assert [p["flags"] for p in tool["presets"]] == ["--local"]
    assert cat.files == ["zz.yaml"]  # loaded once, not twice
    assert cat.file_paths["zz.yaml"].resolve() == (local_dir / "zz.yaml").resolve()

    warning = next(w for w in cat.errors if "shadowed by" in w)
    assert warning.startswith("recipes.d/zz.yaml: ")
    assert str(config_dir / "zz.yaml") in warning or "~" in warning
    assert warning.endswith("shadowed by ./recipes.d/zz.yaml")
