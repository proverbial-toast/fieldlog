"""Three findings from the 2026-09-18 review, each with the gap it closed.

1. Widget ids were built from names the operator controls — a recipe id from a
   drop-in, an interface name from the box. Textual ids must be identifiers, so
   a vlan child (`eth0.100`) or a drop-in calling its tool `acme.probe` raised
   BadIdentifier and took the modal down on open.
2. `${TARGET:-default}` is left for the shell, so `template_vars` never saw it
   and `check_recipe` skipped the scope allowlist — while `build_env` still
   exported the value for the shell to expand. The allowlist now guards both
   paths, without demanding a value the form supplies a default for.
3. `--timeout 0` read as "no time at all" and meant "no limit", because the
   launch path gates the wrapper on a truthy timeout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from textual.widgets import Input

from fieldlog.recipes import check_recipe
from fieldlog.state import TargetSession, shell_vars, template_vars

TOOL = {"id": "sh", "bin": "sh"}


def _verdict(flags: str, **scope):
    return check_recipe(TOOL, {"id": "p", "flags": flags}, TargetSession(**scope))


# ---- 1. ids built from operator-controlled names ------------------------


@pytest.mark.parametrize("name", ["flannel.1", "eth0.100", "eth0:1", "2wire", "br-lan.42"])
async def test_scope_modal_opens_and_selects_an_awkward_interface(tmp_workspace: Path, name, monkeypatch):
    """Open the scope modal on a dotted interface, then click it and save.

    `flannel.1` is the real report: a Textual id cannot hold a dot, so
    `iface-flannel.1` raised BadIdentifier and the modal died on open. Rows are
    numbered instead of named, and the name itself is left alone — sanitising
    it would collide two interfaces onto one id and, worse, change what
    `$IFACE` resolves to.
    """
    from fieldlog import app as app_mod

    monkeypatch.setattr(app_mod, "list_box_interfaces", lambda: [("eth0", "10.0.0.4"), (name, "10.0.0.5")])
    session = TargetSession(target="10.0.0.1", interface="eth0")
    session.workspace_dir = tmp_workspace
    app = app_mod.FieldlogApp(session)

    async with app.run_test(size=(140, 45)) as pilot:
        app.action_target_scope()
        await pilot.pause()
        modal = app.screen

        rows = modal.query(".iface-row")
        assert len(rows) == len(modal.ifaces) == 2
        assert [modal._iface_at(r.id) for r in rows] == [n for n, _ in modal.ifaces]

        index = [n for n, _ in modal.ifaces].index(name)
        await pilot.click(f"#iface-row-{index}")
        await pilot.pause()
        assert modal.iface_name == name, "clicking the row selects that interface"
        assert modal.query_one("#in-iface", Input).value == name, "the name is written back unaltered"

        modal.action_save()
        await pilot.pause()
        assert app.session.interface == name, "$IFACE keeps the real name, dots and all"

    assert modal._iface_at("iface-row-9999") is None, "a stale row resolves to nothing, not an IndexError"
    assert modal._iface_at("iface-list") is None, "the container id is not a row"


async def test_recipe_manager_opens_with_a_dotted_tool_id(tmp_workspace: Path, monkeypatch):
    """A drop-in tool id is yaml, not a Textual identifier."""
    from fieldlog.app import FieldlogApp

    session = TargetSession(target="10.0.0.1")
    session.workspace_dir = tmp_workspace
    app = FieldlogApp(session)

    async with app.run_test(size=(140, 45)) as pilot:
        app.recipes.append({"id": "acme.probe", "bin": "sh", "name": "Dotted", "presets": [{"id": "a", "flags": ""}]})
        app.action_recipe_manager()
        await pilot.pause()
        modal = app.screen
        rows = modal.query(".mgr-tool-row")
        assert rows, "the manager listed no tools"
        ids = [modal._mgr_tool_id(r.id) for r in rows]
        assert "acme.probe" in ids, "the dotted tool must still be reachable by click"
        assert modal._mgr_tool_id("mgrtool-9999") is None, "a stale row resolves to nothing, not an IndexError"


# ---- 2. shell parameter forms take the same allowlist -------------------


def test_shell_vars_sees_what_template_vars_leaves_alone():
    flags = "curl ${TARGET:-1.1.1.1} ${HOST} $IFACE ${RIFACE:-eth0}"
    assert "TARGET" not in template_vars("curl ${TARGET:-1.1.1.1}")
    assert shell_vars(flags) == {"TARGET", "HOST", "RIFACE"}


@pytest.mark.parametrize("flags", ["curl $TARGET", "curl ${TARGET:-1.1.1.1}", "curl ${TARGET:?}"])
def test_unsafe_target_is_refused_whatever_form_the_recipe_uses(flags):
    """The env carries the value for every form, so every form is checked."""
    verdict = _verdict(flags, target="10.0.0.1; id")
    assert verdict.blocked
    assert verdict.kind == "target"
    assert "unsafe characters" in verdict.reason


def test_brace_default_does_not_demand_the_value_it_defaults():
    """`${HOST:-localhost}` is how openssl/selfsigned runs without a dns name."""
    verdict = _verdict("req -subj /CN=${HOST:-localhost}", target="10.0.0.1")
    assert not verdict.blocked, verdict.reason


def test_bare_var_still_demands_its_value():
    verdict = _verdict("dig $HOST", target="10.0.0.1")
    assert verdict.blocked and verdict.kind == "dns" and verdict.missing


# ---- 3. --timeout takes a real duration ---------------------------------


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "1e400", "abc"])
def test_timeout_rejects_anything_that_is_not_a_duration(value, tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "fieldlog", "run", "ping/quick", "-t", "10.0.0.1",
         "--timeout", value, "-n", "-w", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--timeout" in proc.stderr


def test_a_real_timeout_still_wraps_the_command(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "fieldlog", "run", "ping/quick", "-t", "10.0.0.1",
         "--timeout", "0.5", "-n", "-w", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0.5s" in proc.stdout
