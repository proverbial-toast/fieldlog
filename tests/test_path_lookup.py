"""is_tool_installed answers exactly what shutil.which would, only fewer syscalls.

The directory listing it keeps is a filter, never the verdict: a name present
in a $PATH directory still has to be an executable file there.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from fieldlog.recipes import clear_tool_cache, is_tool_installed


@pytest.fixture(autouse=True)
def fresh_cache():
    clear_tool_cache()
    yield
    clear_tool_cache()


@pytest.fixture
def path_dir(tmp_path: Path, monkeypatch) -> Path:
    """A $PATH directory holding one of everything awkward."""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "isdir").mkdir()                       # a directory named like a tool
    (d / "noexec").write_text("#!/bin/sh\n")    # present, not executable
    runnable = d / "yesexec"
    runnable.write_text("#!/bin/sh\n")
    runnable.chmod(0o755)
    (d / "broken").symlink_to(tmp_path / "gone")   # dangling symlink
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


@pytest.mark.parametrize("name", ["yesexec", "noexec", "isdir", "broken", "absent"])
def test_agrees_with_shutil_which(path_dir: Path, name: str):
    assert is_tool_installed(name) is (shutil.which(name) is not None)


def test_finds_only_the_executable(path_dir: Path):
    assert is_tool_installed("yesexec") is True
    assert is_tool_installed("noexec") is False
    assert is_tool_installed("isdir") is False
    assert is_tool_installed("broken") is False


def test_a_name_with_a_separator_is_not_a_path_lookup(path_dir: Path):
    """`./tool` and `/usr/bin/tool` name a file, not something to search for."""
    exe = path_dir / "yesexec"
    assert is_tool_installed(str(exe)) is (shutil.which(str(exe)) is not None)
    assert is_tool_installed("nope/yesexec") is False


def test_empty_name_is_not_installed():
    assert is_tool_installed("") is False


def test_empty_path_finds_nothing(monkeypatch):
    monkeypatch.setenv("PATH", "")
    clear_tool_cache()
    assert is_tool_installed("sh") is (shutil.which("sh") is not None)


def test_reload_sees_a_tool_installed_since_boot(tmp_path: Path, monkeypatch):
    """The TUI's reload clears both caches; a stale listing must not outlive it."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    clear_tool_cache()
    assert is_tool_installed("latecomer") is False

    tool = d / "latecomer"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)

    assert is_tool_installed("latecomer") is False, "the answer is cached, as it always was"
    clear_tool_cache()
    assert is_tool_installed("latecomer") is True


def test_duplicate_path_entries_are_visited_once(path_dir: Path, monkeypatch):
    """A $PATH that repeats a directory six times is normal, and costs one scan."""
    monkeypatch.setenv("PATH", os.pathsep.join([str(path_dir)] * 6))
    clear_tool_cache()
    assert is_tool_installed("yesexec") is True
    assert is_tool_installed("absent") is False
