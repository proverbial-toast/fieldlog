"""Lightweight view models for the TUI tab strip and recipe tree."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TabDescriptor:
    id: str                 # "system", "job-01", …
    label: str              # "System", "ping/sweep #01"
    status: str             # "system", "active", "done", "failed"
    tool_id: str
    job_id: Optional[str] = None
    cmd: str = ""
    artifact: str = ""      # captured at spawn; never re-derived from live state


@dataclass
class TreeRow:
    """One line of the flattened RECIPES list."""

    kind: str               # "header" | "tool" | "entry" | "chain"
    label: str = ""
    bin: str = ""
    meta: str = ""
    tool_id: str = ""
    preset_id: str = ""
    blocked: bool = False
