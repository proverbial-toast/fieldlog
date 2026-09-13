"""Entrypoint: python -m fieldlog"""

from __future__ import annotations

import sys
from typing import List, Optional

from fieldlog.app import FieldlogApp
from fieldlog.cli import main as cli_main


def main(argv: Optional[List[str]] = None) -> None:
    sys.exit(cli_main(argv))


if __name__ == "__main__":
    main()

