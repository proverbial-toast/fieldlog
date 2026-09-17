"""The session transcript: `<workspace>/fieldlog.log`.

Every line the TUI writes to its System tab is appended here, stamped with the
local time it happened — kill and detach decisions, scope changes, reloads, args
resets and the boot preflight. The workspace *is* the archive, so this is the
archive of the session that produced it, and it sits beside the run folders
rather than hidden with `.last-scope.json`.

One line per event, and no rotation: the file grows with the operator's own
activity, and a long day of it is tens of KB. A file worth rotating would be a
surprise worth reading before writing a rotator for it.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

TRANSCRIPT_FILE = "fieldlog.log"


def transcript_line(text: str, when: Optional[float] = None) -> str:
    """One stamped line: naive local ISO-8601 to the second, as the records carry it.

    `text` is written as it is. A multi-line event stays one entry — the next
    stamp is what starts the next one.
    """
    stamp = datetime.fromtimestamp(when if when is not None else time.time()).isoformat(timespec="seconds")
    return f"{stamp} {text}"


def append_transcript(workspace_dir, text: str, when: Optional[float] = None) -> None:
    """Append one stamped event to the workspace transcript, creating the workspace if needed.

    Unlike `save_last_scope`, an `OSError` propagates: the caller decides what to
    say about a workspace it cannot write, and the TUI wants to say it once.
    """
    path = Path(workspace_dir)
    path.mkdir(parents=True, exist_ok=True)
    with open(path / TRANSCRIPT_FILE, "a", encoding="utf-8") as fh:
        fh.write(transcript_line(text, when) + "\n")
