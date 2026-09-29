"""The flattened RECIPES list: which rows exist, which one the cursor is
on, and what selecting one does.

A mixin of FieldlogApp, not a widget: `self` is the app, and these
methods use its state and its widget tree directly. Split out for
navigability — app.py held every pane at once.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from fieldlog.recipes import (
    chain_matches, chain_missing, is_tool_installed, recipe_missing, search, steps_label,
)
from fieldlog.state import save_pinned_recent
from fieldlog.tui.helpers import truncate_right
from fieldlog.tui.models import TreeRow
from fieldlog.tui.theme import ACCENT, BG_BASE, DIM, FG, MUTED, UNFOCUSED
from fieldlog.tui.widgets import RecipeRowWidget



class RecipeTreeMixin:
    """The flattened RECIPES list: which rows exist, which one the cursor is"""

    def _chain_row(self, chain: dict) -> TreeRow:
        return TreeRow(
            "chain", label=chain.get("name", chain["id"]), id=chain["id"],
            meta=steps_label(chain),
            blocked=self.chain_blocked_flag(chain),
        )

    def visible_rows(self) -> List[TreeRow]:
        """Pinned / Recent / All Recipes — or ranked results while filtering.

        `All Recipes` is one row per *tool*, not per variant.
        """
        q = self.filter_text.strip().lower()
        if q:
            hits = search(self.recipes, q, self.blocked_flag, self.hide_missing, limit=40)
            rows = [TreeRow("header", label="Results")]
            for t, p, blocked in hits:
                rows.append(TreeRow(
                    "entry", label=p.get("name", p["id"]), id=t["id"],
                    tool_id=t["id"], preset_id=p["id"], blocked=blocked,
                ))
            rows.extend(self._chain_rows(q))
            return rows

        rows = [TreeRow("header", label="Pinned")]
        rows.extend(self._entry_rows(self.pinned))
        rows.append(TreeRow("header", label="Recent"))
        rows.extend(self._entry_rows(self.recent))
        rows.append(TreeRow("header", label="All Recipes"))
        for t in sorted(
            self.recipes,
            key=lambda t: (0 if is_tool_installed(t.get("bin", "")) else 1, t["id"]),
        ):
            ok = is_tool_installed(t.get("bin", ""))
            if self.hide_missing and not ok:
                continue        # `[!] all` is what reveals the n/a tools
            rows.append(TreeRow(
                "tool",
                label=t.get("name", t["id"]),
                id=t["id"],
                tool_id=t["id"],
                meta=f"{len(t.get('presets', []))}v" if ok else "n/a",
                blocked=not ok,
            ))
        chain_rows = self._chain_rows("")
        if chain_rows:
            rows.append(TreeRow("header", label="Chains"))
            rows.extend(chain_rows)
        return rows

    def _chain_rows(self, q: str) -> List[TreeRow]:
        """Chain rows matching `q`. Runnable-only hides one with a step that is
        not installed; one waiting on the scope stays, blocked, as a recipe does."""
        rows = []
        for chain in self.chains:
            if q and not chain_matches(chain, q):
                continue
            if self.hide_missing and chain_missing(self.catalog, chain):
                continue
            rows.append(self._chain_row(chain))
        return rows

    def _entry_rows(self, keys: List[str]) -> List[TreeRow]:
        out = []
        for key in keys:
            tool_id, _, preset_id = key.partition("/")
            if tool_id == "chain":
                chain = self.get_chain(preset_id)
                if chain is None:
                    continue
                if self.hide_missing and chain_missing(self.catalog, chain):
                    continue
                out.append(self._chain_row(chain))
                continue
            # A key this catalog lacks is kept, not shown: a drop-in that failed
            # to parse this morning brings its recipes back once it is fixed,
            # and the pins with them. get_preset's fallback would show it as
            # the tool's first variant instead.
            t = self.get_tool(tool_id)
            p = next((p for p in t.get("presets", []) if p["id"] == preset_id), None) if t else None
            if p is None:
                continue
            if self.hide_missing and recipe_missing(t, p):
                continue
            out.append(TreeRow(
                "entry", label=p.get("name", p["id"]), id=t["id"],
                tool_id=t["id"], preset_id=p["id"], blocked=self.blocked_flag(t, p),
            ))
        return out

    def _row_text(self, row: TreeRow, selected: bool, width: int = 34) -> Text:
        if row.kind == "header":
            return Text(f"  {row.label.upper()}", style=f"bold {MUTED}")
        if selected and not row.blocked:
            id_style, label_style, meta_style = f"bold {BG_BASE} on {ACCENT}", f"{BG_BASE} on {ACCENT}", f"{BG_BASE} on {ACCENT}"
        elif selected:
            id_style, label_style, meta_style = f"bold {DIM} on #161c1b", f"{UNFOCUSED} on #161c1b", f"{MUTED} on #161c1b"
        elif row.blocked:
            id_style, label_style, meta_style = "#4a5754", UNFOCUSED, MUTED
        else:
            id_style, label_style, meta_style = f"bold {FG}", DIM, MUTED

        label = truncate_right(row.label, width)
        text = Text.assemble(("  ", id_style), (row.id, id_style), (" ", label_style), (label, label_style))
        if row.meta:
            text.append("  " + row.meta, style=meta_style)
        return text

    @staticmethod
    def _row_identity(row: TreeRow) -> tuple:
        """What makes a row the same row across a rebuild, index aside."""
        return (row.kind, row.id, row.tool_id, row.preset_id)

    def _row_shows(self, row: TreeRow) -> bool:
        """Whether `row` names what VARIANTS and ARGS are painting right now.

        A tool row stands for its whole tool, so it still names the selection
        after the operator has stepped to another of that tool's variants —
        which is what keeps a rebuild from pulling the variant back to the
        first one. Every other kind has to match exactly.
        """
        if row.kind == "chain":
            return row.id == self.selected_chain_id
        if self.selected_chain_id is not None:
            return False
        if row.kind == "tool":
            return row.tool_id == self.selected_tool_id
        if row.kind == "entry":
            return row.tool_id == self.selected_tool_id and row.preset_id == self.selected_preset_id
        return False

    def _row_index(self, matches: Callable[[TreeRow], bool]) -> Optional[int]:
        """The first selectable row `matches` accepts, or None."""
        return next((i for i, r in enumerate(self._rows) if r.kind != "header" and matches(r)), None)

    def _place_cursor(self, was_on: Optional[tuple], drifted: bool) -> None:
        """Leave the cursor and the panes naming the same thing.

        The highlight in RECIPES is a promise about what `[Enter] Run` will
        run, and the two used to come apart in both directions: `[!]` or a
        filter could hide the row under the cursor, moving it without telling
        VARIANTS, and the palette or the recipe manager could move the
        selection without moving the cursor. Either way the operator read one
        recipe and ran another.

        So, in order: something else moved the selection and a row names it —
        go there; the row the cursor was on is still listed — follow it
        wherever it went; neither — take the first row there is and repaint
        the panes from it, because a cursor with nowhere to go back to is the
        one case where the selection has to give way.

        A filter that matches nothing leaves no row to stand on, and the
        cursor parks on the `Results` heading — which every caller already
        refuses to act on. The panes deliberately keep painting the last
        recipe rather than blanking: the run button still says what `[Enter]
        Run` will run, so nothing is claimed that is not true, and it is also
        what lets the palette pick a recipe the current filter hides.
        """
        index = self._row_index(self._row_shows) if drifted else None
        if index is None and was_on is not None:
            index = self._row_index(lambda r: self._row_identity(r) == was_on)
        if index is not None:
            self.cursor = index
            return
        self.cursor = self._first_selectable()
        here = self._rows[self.cursor] if self.cursor < len(self._rows) else None
        if here is not None and here.kind != "header":
            self._select_row(here)

    def _rebuild_tree(self) -> None:
        try:
            tree = self.query_one("#recipe-tree", VerticalScroll)
        except Exception:
            return
        # The cursor follows the row it was on, not the index it sat at:
        # launching something re-orders Recent underneath it, and an index held
        # still would leave the cursor on a different recipe than the one the
        # operator put it on.
        if self.selected_chain_id is None:
            # Settle the two ids against this catalog first: `_row_shows` below
            # compares a row to them, and an id naming a preset the tool does
            # not have would match no row at all.
            self.selected_recipe()
        was = self._rows[self.cursor] if 0 <= self.cursor < len(self._rows) else None
        was_on = self._row_identity(was) if was is not None and was.kind != "header" else None
        drifted = was is None or not self._row_shows(was)
        self._rows = self.visible_rows()
        self._place_cursor(was_on, drifted)
        tree.remove_children()
        widgets = [
            RecipeRowWidget(i, self._row_text(r, i == self.cursor),
                            classes="recipe-row-header" if r.kind == "header" else "recipe-row")
            for i, r in enumerate(self._rows)
        ]
        if widgets:
            tree.mount_all(widgets)

    def _paint_rows(self) -> None:
        try:
            tree = self.query_one("#recipe-tree", VerticalScroll)
        except Exception:
            return
        for widget in tree.query(RecipeRowWidget):
            if widget.index < len(self._rows):
                widget.update(self._row_text(self._rows[widget.index], widget.index == self.cursor))
                if widget.index == self.cursor:
                    widget.scroll_visible(animate=False)

    def _first_selectable(self) -> int:
        return next((i for i, r in enumerate(self._rows) if r.kind != "header"), 0)

    def move_cursor(self, delta: int) -> None:
        if not self._rows:
            return
        i = self.cursor
        for _ in range(len(self._rows)):
            nxt = i + delta
            if nxt < 0 or nxt >= len(self._rows):
                break
            i = nxt
            if self._rows[i].kind != "header":
                break
        if self._rows[i].kind == "header":
            return
        self.cursor = i
        self._select_row(self._rows[i])
        self._paint_rows()

    def _select_row(self, row: TreeRow) -> None:
        """A tool row selects that tool's first variant; an entry row is exact.
        A chain row selects the chain, and any other row clears it."""
        if row.kind == "chain":
            if row.id != self.selected_chain_id:
                self.chain_step = 0     # a different chain is read from its first step
            self.selected_chain_id = row.id
        elif row.kind == "tool":
            self.selected_chain_id = None
            tool = self.get_tool(row.tool_id)
            if tool and tool.get("presets"):
                self.selected_tool_id = row.tool_id
                self.selected_preset_id = tool["presets"][0]["id"]
        elif row.kind == "entry":
            self.selected_chain_id = None
            self.selected_tool_id = row.tool_id
            self.selected_preset_id = row.preset_id
        self._refresh_variants()
        self._refresh_args_band()

    def tree_row_clicked(self, index: int) -> None:
        """Clicking a recipe puts the cursor on it and never runs it.

        The keyboard stays in RECIPES: a click says "this row", not "hand the
        arrow keys to the other pane". Only Tab and Enter move between panes.
        """
        if not (0 <= index < len(self._rows)) or self._rows[index].kind == "header":
            return
        self.cursor = index
        self._select_row(self._rows[index])
        self._paint_rows()
        self._focus_recipes()

    def variant_row_clicked(self, preset_id: str) -> None:
        self.selected_preset_id = preset_id
        self.kbd_pane = "variants"
        self._refresh_variants()
        self._refresh_args_band()
        self._paint_pane_focus()

    def select_chain(self, chain_id: str) -> None:
        tool = self.get_chain(chain_id)
        if tool is None:
            return
        if chain_id != self.selected_chain_id:
            self.chain_step = 0
        self.selected_chain_id = chain_id
        self._rebuild_tree()
        self._refresh_variants()
        self._refresh_args_band()

    def select_tool(self, tool_id: str) -> None:
        tool = self.get_tool(tool_id)
        if not tool:
            return
        self.selected_chain_id = None
        self.selected_tool_id = tool_id
        if tool.get("presets"):
            self.selected_preset_id = tool["presets"][0]["id"]
        self._rebuild_tree()
        self._refresh_variants()
        self._refresh_args_band()

    def action_cursor_up(self) -> None:
        if self.focus_pane() == "variants":
            self.action_prev_variant()
        else:
            self.move_cursor(-1)

    def action_cursor_down(self) -> None:
        if self.focus_pane() == "variants":
            self.action_next_variant()
        else:
            self.move_cursor(1)

    def select_under_cursor(self) -> Optional[str]:
        """Select whatever the cursor stands on, and answer with its kind."""
        row = self._rows[self.cursor] if 0 <= self.cursor < len(self._rows) else None
        if row is None or row.kind == "header":
            return None
        self._select_row(row)
        return row.kind

    def action_activate(self) -> None:
        """Enter in RECIPES selects and jumps to VARIANTS; Enter in VARIANTS runs.

        A chain goes the same way as a recipe: Enter is how the operator gets
        to look at what they are about to launch, and the launch is the second
        Enter. It never runs on the first one.
        """
        if self.focus_pane() != "recipes":
            self.action_run_task()
            return
        self.select_under_cursor()
        self._focus_variants()

    def commit_filter(self) -> None:
        """Enter in the filter box: stop typing, hand the keyboard to the hit.

        It runs nothing, whatever pane the keyboard was in before `/` was
        pressed — routing it through `action_activate` meant that a filter
        typed from VARIANTS launched the old selection on the keystroke that
        finished the word.
        """
        self.select_under_cursor()
        self._focus_variants()

    def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def action_toggle_hide_missing(self) -> None:
        self.hide_missing = not self.hide_missing
        toggle = self.query_one("#avail-toggle", Static)
        toggle.update("[!] runnable" if self.hide_missing else "[!] all")
        toggle.styles.color = ACCENT if self.hide_missing else MUTED
        # No cursor reset: a row that survives the toggle keeps the cursor, and
        # one that does not hands the panes to whatever the cursor lands on.
        self._rebuild_tree()

    def action_toggle_pin(self) -> None:
        _, _, key, _ = self.current_flags()
        if key in self.pinned:
            self.pinned.remove(key)
        else:
            self.pinned.append(key)
        save_pinned_recent(self.session.workspace_dir, self.pinned, self.recent)
        self._refresh_variants()
        self._rebuild_tree()
