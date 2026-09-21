"""What a report about fieldlog itself needs, gathered where the reporter is.

A report that arrives without the version, the box or the catalog costs one
round trip each to ask for, and the answers come back a day apart. These pin
the three places that hand the whole of it over at once: `doctor --json`, the
block a crash prints, and the System tab's own copy keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog import __version__
from fieldlog.cli import (
    ISSUES_URL,
    build_parser,
    environment_report,
    handle_doctor,
    main,
)
from fieldlog.recipes import load_catalog

GOOD = """
recipes:
  - id: alive
    bin: sh
    presets:
      - id: t
        flags: "-c true"
"""

# Indentation a yaml parser refuses, which is what a hand-written drop-in
# actually gets wrong.
BROKEN = """
recipes:
  - id: x
   bad: indent
"""


@pytest.fixture
def catalog(tmp_path: Path):
    dropins = tmp_path / "recipes.d"
    dropins.mkdir()
    (dropins / "good.yaml").write_text(GOOD, encoding="utf-8")
    (dropins / "broken.yaml").write_text(BROKEN, encoding="utf-8")
    # A base that does not exist keeps the shipped catalog out of the report.
    return load_catalog(base=tmp_path / "no-base.yaml", dropin_dir=dropins)


# ---- doctor carries the box, not only the scope --------------------------


def test_doctor_json_carries_the_box_and_what_the_catalog_refused(catalog, capsys):
    args = build_parser().parse_args(["doctor", "--json"])

    assert handle_doctor(args, catalog) == 0
    report = json.loads(capsys.readouterr().out)["environment"]

    assert report["version"] == __version__
    assert report["python"] and report["platform"] and report["machine"]
    assert "timeout_binary" in report, "a Mac without coreutils is the first thing to rule out"
    assert report["catalog"]["dropins"] == ["good.yaml"]
    assert any("broken.yaml" in e for e in report["catalog"]["errors"]), (
        "the file the loader refused is the whole answer to 'why is my recipe missing'"
    )


def test_doctor_says_which_version_it_is_and_which_file_it_refused(catalog, capsys):
    args = build_parser().parse_args(["doctor"])

    assert handle_doctor(args, catalog) == 0
    out = capsys.readouterr().out

    assert f"fieldlog {__version__}" in out, "the line a reporter quotes"
    # `list` and `run` show a quietly smaller catalog; doctor is where the
    # reason has to be, because nothing else on the CLI says it.
    assert "broken.yaml" in out


def test_the_environment_report_stands_up_without_a_catalog():
    """A crash may happen before one is loaded, so the catalog half is optional."""
    report = environment_report()

    assert report["version"] == __version__
    assert "catalog" not in report


# ---- a crash is a paste, not a traceback ---------------------------------


def _raises(exc: BaseException):
    def boom(argv=None):
        raise exc
    return boom


def test_a_crash_hands_over_everything_a_report_needs(monkeypatch, capsys):
    monkeypatch.setattr("fieldlog.cli.run_cli", _raises(RuntimeError("the archive said no")))

    code = main(["run", "ping/quick", "10.0.0.1"])

    err = capsys.readouterr().err
    assert code == 1
    assert __version__ in err, "which version crashed"
    assert "fieldlog run ping/quick 10.0.0.1" in err, "what was run"
    assert "Traceback" in err and "RuntimeError: the archive said no" in err
    assert ISSUES_URL in err, "where to put it"


def test_ctrl_c_is_a_decision_and_not_a_bug(monkeypatch, capsys):
    monkeypatch.setattr("fieldlog.cli.run_cli", _raises(KeyboardInterrupt()))

    # 130 is what an interrupted chain already reports, and there is nothing
    # to say about it.
    assert main([]) == 130
    assert capsys.readouterr().err == ""


def test_a_usage_error_still_exits_the_way_argparse_meant_it_to():
    """The handler is for the unexpected: argparse's own exit must pass through."""
    with pytest.raises(SystemExit):
        main(["--no-such-flag"])


# ---- the System tab is the harness's own log -----------------------------


@pytest.mark.asyncio
async def test_the_system_tab_copies_the_path_to_its_own_transcript(tmp_workspace, monkeypatch):
    """`Y` said "harness log is not written to disk" long after it was."""
    from fieldlog.app import FieldlogApp
    from fieldlog.state import TargetSession
    from fieldlog.tui import jobs as jobs_mod

    copied: list = []
    monkeypatch.setattr(
        jobs_mod, "copy_text_to_clipboard", lambda text, app=None: (copied.append(text), True)[1]
    )

    app = FieldlogApp(session=TargetSession(workspace_dir=tmp_workspace))
    async with app.run_test():
        assert app.active_tab().id == "system"
        app.action_copy_tail()

    assert copied == [f"tail -f {tmp_workspace / 'fieldlog.log'}"]


@pytest.mark.asyncio
async def test_a_copied_system_log_says_where_the_rest_of_it_is(tmp_workspace, monkeypatch):
    from fieldlog.app import FieldlogApp
    from fieldlog.state import TargetSession
    from fieldlog.tui import jobs as jobs_mod

    copied: list = []
    monkeypatch.setattr(
        jobs_mod, "copy_text_to_clipboard", lambda text, app=None: (copied.append(text), True)[1]
    )

    app = FieldlogApp(session=TargetSession(workspace_dir=tmp_workspace))
    async with app.run_test():
        app.write_system_log("[test] a line worth reporting")
        app.action_copy_log()

    text = copied[0]
    assert f"# fieldlog {__version__}" in text
    assert f"# transcript: {tmp_workspace / 'fieldlog.log'}" in text, (
        "the paste has to name the file that holds more than the 5000 lines kept in memory"
    )
    assert "[test] a line worth reporting" in text
