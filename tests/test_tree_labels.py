"""A grouped tool row in the RECIPES tree has to say which tool it is.

`All Recipes` is one row per tool, and that row used to be built with
`label=""`, which was survivable only while every tool was one binary doing one
job: the binary name alone read as the tool. `python-server` broke that — two
variants of `python3`, one plain HTTP and one TLS — and the row rendered as a
bare `python3  2v` with nothing to say what it served. The row must carry the
catalog `name`.

The rename that produced `python-server` also moved the key the `tls-serve`
chain points at, so the shipped drop-ins are loaded here too: the chain must
still resolve, now against `python-server/https`, and not land in the catalog's
error list.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession

REPO_ROOT = Path(__file__).resolve().parent.parent


def _shipped_catalog_dirs(tmp_path: Path, monkeypatch) -> Path:
    """Point the loader at an empty config dir and a cwd holding the real drop-ins.

    The catalog then contains exactly what the repo ships, with nothing the
    developer happens to have in ~/.config leaking in.
    """
    from fieldlog import recipes as recipes_mod

    config_dir = tmp_path / "config" / "recipes.d"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(recipes_mod, "DROPIN_DIR", config_dir)

    work = tmp_path / "work"
    local_dir = work / "recipes.d"
    local_dir.mkdir(parents=True)
    for name in ("examples.yaml", "chains.yaml"):
        shutil.copy(REPO_ROOT / "recipes.d" / name, local_dir / name)
    monkeypatch.chdir(work)
    return work


async def test_grouped_tool_row_shows_its_name(tmp_workspace: Path, tmp_path: Path, monkeypatch):
    """The `python-server` row names the tool, not just the interpreter."""
    _shipped_catalog_dirs(tmp_path, monkeypatch)
    session = TargetSession(target="10.0.0.1")
    session.workspace_dir = tmp_workspace
    app = FieldlogApp(session)

    async with app.run_test(size=(140, 45)):
        matches = [r for r in app._rows if r.kind == "tool" and r.tool_id == "python-server"]
        assert len(matches) == 1, "python-server should appear once, as a single grouped tool row"
        row = matches[0]
        assert row.bin == "python3"
        assert row.label == "Python Server", "the tool row must carry the catalog name"
        assert row.meta == "2v", "both server variants belong to the one tool"


def test_tls_serve_chain_resolves_against_python_server(tmp_path: Path, monkeypatch):
    """The chain follows the recipe rename instead of dangling on `https/serve`."""
    from fieldlog.recipes import find_chain, load_catalog

    _shipped_catalog_dirs(tmp_path, monkeypatch)
    cat = load_catalog()

    chain = find_chain(cat, "tls-serve")
    assert chain is not None, "tls-serve should have loaded from the shipped chains.yaml"
    assert [s["recipe"] for s in chain["steps"]] == ["openssl/selfsigned", "python-server/https"]
    assert not [e for e in cat.errors if "tls-serve" in e], cat.errors
