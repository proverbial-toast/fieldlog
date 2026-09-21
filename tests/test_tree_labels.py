"""A grouped tool row in the RECIPES tree has to say which tool it is.

`All Recipes` is one row per tool, and that row used to be built with
`label=""`, which was survivable only while every tool was one binary doing one
job: the binary name alone read as the tool. `python-server` broke that — two
variants of `python3`, one plain HTTP and one TLS — and the row rendered as a
bare `python3  2v` with nothing to say what it served. The row must carry the
catalog `name`.

The bold word went the same way once `rtt` and `pmtu` arrived: three rows read
`ping`, and nothing on screen said `rtt`, the id `fieldlog list`, `run`, a job
tab and the archive all use. The bold word is the tool id now, not the binary.

The rename that produced `python-server` also moved the key the `tls-serve`
chain points at, so the shipped drop-ins are loaded here too: the chain must
still resolve, now against `python-server/https`, and not land in the catalog's
error list.
"""

from __future__ import annotations

from pathlib import Path

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession

def _shipped_catalog_dirs(tmp_path: Path, monkeypatch) -> Path:
    """Point the loader at empty drop-in directories, leaving only what ships.

    Since the themes split there is nothing to copy: the catalog lives in the
    package, as `fieldlog/recipes.d/*.yaml`. These directories are emptied so
    that whatever the developer happens to keep in `~/.config` or in the cwd
    cannot leak into the assertions below.
    """
    from fieldlog import recipes as recipes_mod

    config_dir = tmp_path / "config" / "recipes.d"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(recipes_mod, "DROPIN_DIR", config_dir)

    work = tmp_path / "work"
    (work / "recipes.d").mkdir(parents=True)
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
        assert row.id == "python-server", "the bold word is the tool id, not python3"
        assert row.label == "Python Server", "the tool row must carry the catalog name"
        assert row.meta == "2v", "both server variants belong to the one tool"


async def test_tools_on_one_binary_are_told_apart_by_id(tmp_workspace: Path, tmp_path: Path, monkeypatch):
    """`rtt` and `pmtu` run ping too; each row says its own id, sorted by it."""
    _shipped_catalog_dirs(tmp_path, monkeypatch)
    session = TargetSession(target="10.0.0.1")
    session.workspace_dir = tmp_workspace
    app = FieldlogApp(session)

    async with app.run_test(size=(140, 45)) as pilot:
        app.action_toggle_hide_missing()        # [!] all: nothing hangs on what this box has
        await pilot.pause()
        tools = [r for r in app._rows if r.kind == "tool"]
        by_tool = {r.tool_id: r.id for r in tools}
        assert by_tool["rtt"] == "rtt" and by_tool["pmtu"] == "pmtu" and by_tool["ping"] == "ping"
        assert all(r.id == r.tool_id for r in tools)
        keys = [(r.blocked, r.id) for r in tools]
        assert keys == sorted(keys), "installed first, then by the id the row shows"


def test_tls_serve_chain_resolves_against_python_server(tmp_path: Path, monkeypatch):
    """The chain follows the recipe rename instead of dangling on `https/serve`."""
    from fieldlog.recipes import find_chain, load_catalog

    _shipped_catalog_dirs(tmp_path, monkeypatch)
    cat = load_catalog()

    chain = find_chain(cat, "tls-serve")
    assert chain is not None, "tls-serve should have loaded from the shipped chains.yaml"
    assert [s["recipe"] for s in chain["steps"]] == ["openssl/selfsigned", "python-server/https"]
    assert not [e for e in cat.errors if "tls-serve" in e], cat.errors
