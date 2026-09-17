"""Shared palette and layout constants for the TUI.

Imports nothing of fieldlog's own: a colour is wanted by every module here, and
one import of the catalog would make reading a hex code parse recipes.yaml.
"""

from __future__ import annotations

# Exact hex color palette.
ACCENT = "#6fd7bd"
ACCENT_HOVER = "#9fe8d6"
WARN = "#e0b25f"
ERR = "#e0776a"
FG = "#cfd8d4"
SOFT = "#a8b4b0"
DIM = "#7f8c88"
UNFOCUSED = "#5a6764"
FAINT = "#4e5d5a"
MUTED = "#3f4c49"
GUTTER = "#2c3634"
BG_BASE = "#08090a"
BG_PANEL = "#0b0d0e"
BG_SIDEBAR = "#0a0c0d"
BG_INPUT = "#06080a"
BORDER = "#1e2725"
BORDER_INPUT = "#2a3634"

# VARIANTS shows this many rows and no more, in both layouts. Fixed rather
# than content-sized: a list that grows and shrinks with the selected tool
# moves the Run button between one Enter and the next. RECIPES takes the rest.
VARIANT_ROWS_VISIBLE = 10

# ARGS is content-sized up to this many token rows, then scrolls. The band
# growing is what costs the VARIANTS list rows (see _anchor_tree_height): the
# catalog is meant to fill up with long, specific commands, so reserving the
# tall case as dead space would waste a band-height most of the time.
ARGS_ROWS_MAX = 8

# Rows the ARGS band occupies at its smallest: header + one token row + the
# [H] chip + its top rule. The tree is anchored against this, so the VARIANTS
# pane keeps its position and gives up rows as the band grows.
ARGS_BAND_MIN = 4

# Left-column chrome above and around the variant list: the VARIANTS top rule,
# its header, its title row, and the RECIPES header.
LEFT_COLUMN_CHROME = 6

STACKED_BREAKPOINT = 120

# (key, label, primary?) — the eight chips, in order.
HOTKEYS = [
    ("Ctrl+P", "Tasks", True),
    ("/", "Filter", True),
    ("T", "Scope & Logs", False),
    ("E", "Edit Args", False),
    ("M", "Recipes", False),
    ("W", "Close Tab", False),
    ("L", "Layout", False),
    ("?", "All Keys", False),
]
