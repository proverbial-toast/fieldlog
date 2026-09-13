"""M4: one variable detector, shared by substitution, blocking, the CLI and the TUI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog.state import TargetSession, resolve_flags, template_vars

NOOP_TOOL = {"id": "true", "bin": "true"}


@pytest.mark.parametrize(
    "flags, expected",
    [
        ("-c 4 $TARGET", {"TARGET"}),
        ("-c 4 ${TARGET_IP}", {"TARGET"}),
        ("$TARGET_HOST.stamp", {"HOST"}),  # never TARGET
        ("${HOST}.stamp", {"HOST"}),
        ("-w $OUT_DIR/x -I ${IFACE} $LHOST", {"OUTDIR", "IFACE", "LHOST"}),
        ("$TARGETS $OUTDIRECTORY", {"TARGETS", "OUTDIRECTORY"}),  # whole names only
        ("--id $RUN_ID", {"RUN_ID"}),
        ("${TARGET:-10.0.0.1}", set()),  # shell form, left for the shell
        ("", set()),
    ],
)
def test_template_vars(flags, expected):
    assert template_vars(flags) == expected


def test_substitution_covers_exactly_what_is_detected():
    session = TargetSession(target="10.0.0.1", hostname="h.example", interface="lo", lhost="127.0.0.1")
    flags = "$TARGET ${TARGET_IP} $HOST ${TARGET_HOST} $IFACE $LHOST $OUTDIR ${OUT_DIR} $RUN_ID ${TARGET:-x}"
    assert resolve_flags(session, flags, out_dir="/o") == (
        "10.0.0.1 10.0.0.1 h.example h.example lo 127.0.0.1 /o /o $RUN_ID ${TARGET:-x}"
    )


def test_is_blocked_uses_canonical_names():
    from fieldlog.recipes import is_blocked

    host_only = {"id": "h", "flags": "$TARGET_HOST.stamp"}
    assert is_blocked(NOOP_TOOL, host_only, TargetSession(target="", hostname="h.example"))[0] is False

    braced_host = {"id": "b", "flags": "${HOST}"}
    blocked, reason, _ = is_blocked(NOOP_TOOL, braced_host, TargetSession(target="10.0.0.1"))
    assert blocked and "dns name" in reason


def _catalog():
    from fieldlog.recipes import Catalog, normalize_recipe

    return Catalog(tools=[normalize_recipe({
        "id": "probe", "bin": "true", "category": "Test",
        "presets": [
            {"id": "hostonly", "flags": "$TARGET_HOST.stamp"},
            {"id": "bracedhost", "flags": "${HOST}.stamp"},
            {"id": "aliases", "flags": "${TARGET_IP} -w $OUT_DIR/x -I $IFACE $RUN_ID"},
        ],
    })])


def test_cli_run_host_only_preset_does_not_demand_a_target(tmp_workspace: Path, capsys):
    from fieldlog.cli import build_parser, handle_run

    args = build_parser().parse_args(
        ["run", "probe/hostonly", "-H", "h.example", "-w", str(tmp_workspace), "--dry-run"]
    )
    assert handle_run(args, _catalog()) == 0
    assert "requires a target" not in capsys.readouterr().err


def test_cli_list_json_requires_canonical_names(capsys):
    from fieldlog.cli import build_parser, handle_list

    assert handle_list(build_parser().parse_args(["list", "--json"]), _catalog()) == 0
    presets = {p["id"]: p["requires"] for t in json.loads(capsys.readouterr().out) for p in t["presets"]}
    assert presets == {
        "hostonly": ["$HOST"],
        "bracedhost": ["$HOST"],
        "aliases": ["$TARGET", "$IFACE", "$OUTDIR"],
    }


@pytest.mark.parametrize(
    "preset_flags, override, extra_args",
    [
        ("", None, "$OUTDIR/b.txt"),            # only in --extra-args
        ("", "-w ${OUT_DIR}/x.pcap", ""),       # hand-typed into a TUI args edit
        ("-w ${OUTDIR}/x.pcap", None, ""),      # braced, in the preset
    ],
)
def test_launch_creates_outdir_whenever_it_is_referenced(tmp_workspace: Path, preset_flags, override, extra_args):
    from fieldlog.launch import plan_launch

    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(
        session, NOOP_TOOL, {"id": "noop", "flags": preset_flags},
        flags_override=override, extra_args=extra_args,
    )
    assert plan.job.out_dir.is_dir()


def test_launch_leaves_outdir_alone_when_unreferenced(tmp_workspace: Path):
    from fieldlog.launch import plan_launch

    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, NOOP_TOOL, {"id": "noop", "flags": "-c 1 $TARGET $OUTDIRECTORY"})
    assert not plan.job.out_dir.exists()


def test_tui_outdir_marker():
    from fieldlog.app import FieldlogApp

    assert FieldlogApp._writes_outdir({"flags": "-w ${OUT_DIR}/x.pcap"})
    assert not FieldlogApp._writes_outdir({"flags": "--out $OUTDIRECTORY"})
