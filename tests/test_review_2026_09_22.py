"""Findings from the 2026-09-22 pre-release review, each reproduced before the fix.

1. An unreadable session.json was read as empty and replaced by the next run's
   record — every earlier record gone, silently.
2. `$OUTDIR` was pasted into the command as an absolute path, so a workspace
   under `My Work/` split it into two words (and `> $OUTDIR/x` wrote `My`).
3. Each pty read was decoded on its own, so a UTF-8 character split across two
   reads became `�`.
4. The same for `\\r\\n`: split across two reads, it became two newlines.
5. An IPv6 link-local target with its zone (`fe80::1%eth0`) was refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from fieldlog.archive import UNREADABLE_MANIFEST, append_record, snapshot_workspace
from fieldlog.launch import plan_launch
from fieldlog.recipes import check_recipe
from fieldlog.report import read_manifest
from fieldlog.runner import run_job
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}
PY_TOOL = {"id": "py", "bin": sys.executable}


async def _run(session: TargetSession, tool: dict, flags: str):
    """Run one preset for real. Returns (code, job, lines the sink was handed)."""
    plan = plan_launch(session, tool, {"id": "t", "flags": flags})
    seen: list[str] = []
    code = await run_job(plan.command, plan.job, session, lambda t, s: seen.append(t), env=plan.env)
    return code, plan.job, seen


# ---- 1. an unreadable manifest is set aside, never written over -------------


@pytest.mark.parametrize("damage", ['[{"id": "01", broken', '{"not": "a list"}'])
def test_an_unreadable_manifest_is_set_aside_not_replaced(tmp_path: Path, damage: str):
    manifest = tmp_path / "session.json"
    manifest.write_text(damage, encoding="utf-8")

    aside = append_record(tmp_path, {"id": "02", "recipe": "t/echo"})

    assert aside is not None and aside.name.startswith(UNREADABLE_MANIFEST)
    assert aside.read_text(encoding="utf-8") == damage          # byte for byte
    assert [r["id"] for r in json.loads(manifest.read_text())] == ["02"]


def test_a_sound_manifest_is_appended_to_and_nothing_is_set_aside(tmp_path: Path):
    append_record(tmp_path, {"id": "01"})
    assert append_record(tmp_path, {"id": "02"}) is None
    assert [r["id"] for r in json.loads((tmp_path / "session.json").read_text())] == ["01", "02"]
    assert not list(tmp_path.glob(f"{UNREADABLE_MANIFEST}*"))


def test_two_set_asides_in_one_second_keep_both(tmp_path: Path):
    for damage in ("first", "second"):
        (tmp_path / "session.json").write_text(damage, encoding="utf-8")
        append_record(tmp_path, {"id": "01"})
    kept = sorted(p.read_text() for p in tmp_path.glob(f"{UNREADABLE_MANIFEST}*"))
    assert kept == ["first", "second"]


def test_the_readers_warn_while_a_set_aside_manifest_is_there(tmp_path: Path):
    (tmp_path / "session.json").write_text("{broken", encoding="utf-8")
    append_record(tmp_path, {"id": "02", "recipe": "t/echo"})

    manifest = read_manifest(tmp_path)

    assert manifest.readable                       # the fresh one still reads
    assert [r["id"] for r in manifest.runs] == ["02"]
    assert UNREADABLE_MANIFEST in manifest.problem


def test_a_set_aside_manifest_is_never_counted_as_a_runs_artifact(tmp_path: Path):
    (tmp_path / f"{UNREADABLE_MANIFEST}20260922T000000").write_text("x")
    assert snapshot_workspace(tmp_path) == {}


async def test_a_run_over_an_unreadable_manifest_says_so(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    session.target_dir.mkdir(parents=True)
    (session.target_dir / "session.json").write_text("{broken", encoding="utf-8")

    code, _job, seen = await _run(session, SH_TOOL, "-c 'echo hi'")

    assert code == 0
    assert any("session.json could not be read" in line for line in seen)


# ---- 2. $OUTDIR survives a workspace path with a space in it -----------------


async def test_outdir_survives_a_space_in_the_workspace_path(tmp_path: Path):
    workspace = tmp_path / "My Work" / "targets"
    session = TargetSession(target="10.0.0.1", workspace_dir=workspace)

    code, job, _ = await _run(session, SH_TOOL, "-c 'echo data > $OUTDIR/x.txt'")

    assert code == 0
    assert (job.out_dir / "x.txt").read_text() == "data\n"
    assert not (tmp_path / "My").exists()             # the stray file the split made
    assert "My Work" not in job.command


async def test_a_script_reading_outdir_from_its_env_finds_the_same_directory(tmp_path: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_path / "My Work")
    code, job, _ = await _run(session, SH_TOOL, """-c 'd="${OUTDIR:-nowhere}"; mkdir -p "$d" && echo data > "$d/y.txt"'""")
    assert code == 0
    assert (job.out_dir / "y.txt").exists()


def test_a_log_destination_that_needs_quoting_is_refused(tmp_path: Path):
    session = TargetSession(
        target="10.0.0.1", workspace_dir=tmp_path / "targets",
        artifact_root=str(tmp_path / "Client Logs"),
    )
    verdict = check_recipe(SH_TOOL, {"id": "t", "flags": "-c 'ls $OUTDIR'"}, session)
    assert verdict.blocked and verdict.kind == "outdir"
    # A recipe that never touches $OUTDIR does not care where logs land.
    assert not check_recipe(SH_TOOL, {"id": "t", "flags": "-c true"}, session).blocked


def test_a_clean_log_destination_outside_the_target_stays_absolute(tmp_path: Path):
    root = tmp_path / "logs"
    session = TargetSession(
        target="10.0.0.1", workspace_dir=tmp_path / "targets", artifact_root=str(root),
    )
    plan = plan_launch(session, SH_TOOL, {"id": "t", "flags": "-c 'ls $OUTDIR'"}, dry_run=True)
    assert str(root) in plan.command
    assert not check_recipe(SH_TOOL, {"id": "t", "flags": "-c 'ls $OUTDIR'"}, session).blocked


# ---- 3 + 4. a pty read boundary never lands in the text ----------------------


async def test_a_multibyte_character_split_across_reads_is_kept_whole(tmp_workspace: Path):
    # ~400 KB of two-byte characters: pty reads end mid-character many times over.
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    code, job, seen = await _run(
        session, PY_TOOL, """-c 'import sys; sys.stdout.write("\\u00e9" * 200000)'"""
    )
    assert code == 0
    assert "�" not in "".join(seen)
    assert "�" not in Path(job.log_path).read_text(encoding="utf-8")


async def test_crlf_split_across_reads_is_one_newline(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    code, job, seen = await _run(
        session, PY_TOOL, """-c 'print("\\n".join(str(i) for i in range(1, 200001)))'"""
    )
    assert code == 0
    assert seen == [str(i) for i in range(1, 200001)]


async def test_a_bare_carriage_return_still_ends_a_line(tmp_workspace: Path):
    # Progress output rewrites one line with `\r`; the log keeps each state.
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    code, _job, seen = await _run(session, SH_TOOL, r"""-c 'printf "10%%\r50%%\rdone\n"'""")
    assert code == 0
    assert seen == ["10%", "50%", "done"]


# ---- 5. IPv6 link-local with a zone ------------------------------------------


def test_an_ipv6_link_local_target_with_a_zone_can_run(tmp_workspace: Path):
    session = TargetSession(target="fe80::1%eth0", workspace_dir=tmp_workspace)
    assert session.target_kind == "address"
    assert not check_recipe(SH_TOOL, {"id": "t", "flags": "-c 'echo $TARGET'"}, session).blocked


async def test_the_zone_reaches_the_tool_intact(tmp_workspace: Path):
    session = TargetSession(target="fe80::1%eth0", workspace_dir=tmp_workspace)
    code, _job, seen = await _run(session, SH_TOOL, "-c 'echo $TARGET'")
    assert code == 0
    assert seen == ["fe80::1%eth0"]
