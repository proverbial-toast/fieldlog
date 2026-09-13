"""Entrypoint: python -m fieldlog"""

from __future__ import annotations

import sys
from typing import List, Optional

# The TUI (and Textual) is imported by cli.py only when `tui` is chosen, so
# `fieldlog list -q` in a pipeline never pays for it.
from fieldlog.cli import main as cli_main


def main(argv: Optional[List[str]] = None) -> None:
    sys.exit(cli_main(argv))


if __name__ == "__main__":
    main()

