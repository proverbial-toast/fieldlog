"""The flattened RECIPES list: which rows exist, which one the cursor is
on, and what selecting one does.

A mixin of FieldlogApp, not a widget: `self` is the app, and these
methods use its state and its widget tree directly. Split out for
navigability — app.py held every pane at once.
"""

from __future__ import annotations

from typing import List

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from fieldlog.recipes import chain_matches, is_tool_installed, search, steps_label
from fieldlog.state import save_pinned_recent
from fieldlog.tui.helpers import truncate_right
from fieldlog.tui.models import TreeRow
from fieldlog.tui.theme import ACCENT, BG_BASE, DIM, FG, MUTED, UNFOCUSED
from fieldlog.tui.widgets import RecipeRowWidget



class RecipeTreeMixin:
    """The flattened RECIPES list: which rows exist, which one the cursor is"""

    def _chain_row(self, chain: dict) -> TreeRow:
        return TreeRow(
            "chain", label=chain.get("name", chain["id"]), bin=chain["id"],
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
                    "entry", label=p.get("name", p["id"]), bin=t.get("bin", t["id"]),
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
            key=lambda t: (0 if is_tool_installed(t.get("bin", "")) else 1, t.get("bin", t["id"])),
        ):
            ok = is_tool_installed(t.get("bin", ""))
            if self.hide_missing and not ok:
                continue        # `[!] all` is what reveals the n/a tools
            rows.append(TreeRow(
                "tool", label="", bin=t.get("bin", t["id"]), tool_id=t["id"],
                meta=f"{len(t.get('presets', []))}v" if ok else "n/a", blocked=not ok,
            ))
        chain_rows = self._chain_rows("")
        if chain_rows:
            rows.append(TreeRow("header", label="Chains"))
            rows.extend(chain_rows)
        return rows

    def _chain_rows(self, q: str) -> List[TreeRow]:
        """Chain rows matching `q`, hidden while blocked if runnable-only is on."""
        rows = []
        for chain in self.chains:
            if q and not chain_matches(chain, q):
                continue
            row = self._chain_row(chain)
            if self.hide_missing and row.blocked:
                continue
            rows.append(row)
        return rows

    def _entry_rows(self, keys: List[str]) -> List[TreeRow]:
        out = []
        for key in keys:
            tool_id, _, preset_id = key.partition("/")
            if tool_id == "chain":
                chain = self.get_chain(preset_id)
                if chain is None:
                    continue
                row = self._chain_row(chain)
                if self.hide_missing and row.blocked:
                    continue
                out.append(row)
                continue
            t = self.get_tool(tool_id)
            if not t:
                continue
            p = self.get_preset(t, preset_id)
            blocked = self.blocked_flag(t, p)
            if self.hide_missing and blocked:
                continue
            out.append(TreeRow(
                "entry", label=p.get("name", p["id"]), bin=t.get("bin", t["id"]),
                tool_id=t["id"], preset_id=p["id"], blocked=blocked,
            ))
        return out

    def _row_text(self, row: TreeRow, selected: bool, width: int = 34) -> Text:
        if row.kind == "header":
            return Text(f"  {row.label.upper()}", style=f"bold {MUTED}")
        if selected and not row.blocked:
            bin_style, label_style, meta_style = f"bold {BG_BASE} on {ACCENT}", f"{BG_BASE} on {ACCENT}", f"{BG_BASE} on {ACCENT}"
        elif selected:
            bin_style, label_style, meta_style = f"bold {DIM} on #161c1b", f"{UNFOCUSED} on #161c1b", f"{MUTED} on #161c1b"
        elif row.blocked:
            bin_style, label_style, meta_style = "#4a5754", UNFOCUSED, MUTED
        else:
            bin_style, label_style, meta_style = f"bold {FG}", DIM, MUTED

        label = truncate_right(row.label, width)
        text = Text.assemble(("  ", bin_style), (row.bin, bin_style), (" ", label_style), (label, label_style))
        if row.meta:
            text.append("  " + row.meta, style=meta_style)
        return text

    def _rebuild_tree(self) -> None:
        try:
            tree = self.query_one("#recipe-tree", VerticalScroll)
        except Exception:
            return
        self._rows = self.visible_rows()
        if self.cursor >= len(self._rows) or (
            self._rows and self._rows[min(self.cursor, len(self._rows) - 1)].kind == "header"
        ):
            self.cursor = self._first_selectable()
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
            self.selected_chain_id = row.bin
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
        """Clicking selects and moves focus — it never runs."""
        if not (0 <= index < len(self._rows)) or self._rows[index].kind == "header":
            return
        self.cursor = index
        self._select_row(self._rows[index])
        self._paint_rows()
        self._focus_variants()

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

    def action_activate(self) -> None:
        """Enter in RECIPES selects and jumps to VARIANTS; Enter in VARIANTS runs."""
        if self.focus_pane() == "recipes":
            if 0 <= self.cursor < len(self._rows) and self._rows[self.cursor].kind != "header":
                self._select_row(self._rows[self.cursor])
            self._focus_variants()
            return
        self.action_run_task()

    def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def action_toggle_hide_missing(self) -> None:
        self.hide_missing = not self.hide_missing
        toggle = self.query_one("#avail-toggle", Static)
        toggle.update("[!] runnable" if self.hide_missing else "[!] all")
        toggle.styles.color = ACCENT if self.hide_missing else MUTED
        self.cursor = 0
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
