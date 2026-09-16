"""Split / stacked layout, pane focus, and the hotkey bar.

A mixin of FieldlogApp, not a widget: `self` is the app, and these
methods use its state and its widget tree directly. Split out for
navigability — app.py held every pane at once.
"""

from __future__ import annotations

from typing import Optional

from textual.containers import Horizontal
from textual.widgets import Static

from fieldlog.tui.theme import (
    ACCENT,
    ARGS_BAND_MIN,
    DIM,
    FG,
    HOTKEYS,
    LEFT_COLUMN_CHROME,
    STACKED_BREAKPOINT,
    UNFOCUSED,
    VARIANT_ROWS_VISIBLE,
)



class LayoutMixin:
    """Split / stacked layout, pane focus, and the hotkey bar."""

    def focus_pane(self) -> str:
        if self._current_layout == "stacked":
            return "variants" if self.stacked_pane == "task" else "recipes"
        return self.kbd_pane

    def _focus_recipes(self) -> None:
        self.kbd_pane = "recipes"
        self.stacked_pane = "recipes"
        self._apply_stacked_classes()
        self._paint_pane_focus()

    def _focus_variants(self) -> None:
        self.kbd_pane = "variants"
        if self._current_layout == "stacked":
            self.stacked_pane = "task"
            self._apply_stacked_classes()
        self._paint_pane_focus()

    def _paint_pane_focus(self) -> None:
        """The focused pane gets an accent rule on its header and a brighter label."""
        try:
            recipes_pane = self.query_one("#recipes-pane")
            variants_pane = self.query_one("#variants-pane")
            r_title = self.query_one("#recipes-title", Static)
            v_title = self.query_one("#variants-title", Static)
        except Exception:
            return
        on_recipes = self.focus_pane() == "recipes"
        recipes_pane.set_class(on_recipes, "-focused")
        variants_pane.set_class(not on_recipes, "-focused")
        stacked = self._current_layout == "stacked"
        r_title.update("RECIPES ›" if stacked and not on_recipes else "RECIPES")
        r_title.styles.color = FG if on_recipes else UNFOCUSED
        v_title.update("VARIANTS")
        v_title.styles.color = FG if not on_recipes else UNFOCUSED
        if self.selected_chain_id:
            return          # the chain branch of _refresh_variants owns the crumb
        tool = self.get_tool(self.selected_tool_id)
        if tool:
            preset = self.get_preset(tool, self.selected_preset_id)
            self.query_one("#variants-crumb", Static).update(self._variants_crumb(tool, preset))

    def action_swap_pane(self) -> None:
        if self.focus_pane() == "recipes":
            self._focus_variants()
        else:
            self._focus_recipes()

    def on_resize(self, event) -> None:
        self._update_responsive_layout(event.size.width)
        self._refresh_status_band()
        self._refresh_pinned_block()
        self._anchor_tree_height()
        if self.show_hotkey_bar:
            self._fit_hotkey_bar()

    def _update_responsive_layout(self, width: Optional[int] = None) -> None:
        if width is None:
            width = self.size.width or STACKED_BREAKPOINT
        if self.layout_override in ("split", "stacked"):
            mode = self.layout_override
        else:
            mode = "stacked" if width < STACKED_BREAKPOINT else "split"
        self._apply_layout_mode(mode)

    def _apply_layout_mode(self, mode: str) -> None:
        self._current_layout = mode
        try:
            body = self.query_one("#body")
            self.query_one("#cell-layout", Static).update(f"[L] {mode}")
        except Exception:
            return
        body.set_class(mode == "stacked", "-stacked")
        self._apply_stacked_classes()
        self._anchor_tree_height()
        self._paint_pane_focus()

    def _anchor_tree_height(self) -> None:
        """Pin the tree to a fixed row count in split mode.

        The ARGS band is content-sized, so it grows when a variant with a long
        argument string is selected. With the tree flexible that growth came
        out of the tree and slid the VARIANTS pane up the screen, moving the
        list out from under the cursor. Anchoring the tree instead means the
        band grows into the variant list: VARIANTS keeps its position and shows
        fewer rows, which is the cheaper thing to lose.
        """
        try:
            tree = self.query_one("#recipe-tree")
        except Exception:
            return
        if self._current_layout != "stacked":
            rows = self.size.height or 40
            anchored = rows - 3 - ARGS_BAND_MIN - LEFT_COLUMN_CHROME - VARIANT_ROWS_VISIBLE
            tree.styles.height = max(4, anchored)
        else:
            tree.styles.height = None   # back to the stylesheet's 1fr

    def _apply_stacked_classes(self) -> None:
        """Exactly one pane expanded in stacked mode; both headers stay mounted."""
        try:
            recipes_pane = self.query_one("#recipes-pane")
            variants_pane = self.query_one("#variants-pane")
        except Exception:
            return
        if self._current_layout != "stacked":
            recipes_pane.remove_class("-collapsed")
            variants_pane.remove_class("-collapsed")
            return
        on_task = self.stacked_pane == "task"
        recipes_pane.set_class(on_task, "-collapsed")
        variants_pane.set_class(not on_task, "-collapsed")
        assert recipes_pane.has_class("-collapsed") != variants_pane.has_class("-collapsed"), (
            "stacked accordion must have exactly one expanded pane"
        )

    def action_toggle_layout(self) -> None:
        self.layout_override = "stacked" if self._current_layout == "split" else "split"
        self._update_responsive_layout()
        self.write_system_log(
            "[layout] stacked · recipes/variants accordion"
            if self._current_layout == "stacked"
            else "[layout] split · recipes over variants · results as monitor"
        )

    def action_toggle_hotkey_bar(self) -> None:
        self.show_hotkey_bar = not self.show_hotkey_bar
        bar = self.query_one("#footer-keys", Horizontal)
        bar.set_class(not self.show_hotkey_bar, "hidden")
        chip = self.query_one("#btn-bar-toggle", Static)
        chip.update("[H] hide keys" if self.show_hotkey_bar else "[H] keys")
        chip.styles.color = ACCENT if self.show_hotkey_bar else DIM
        if self.show_hotkey_bar:
            self._fit_hotkey_bar()

    def _fit_hotkey_bar(self) -> None:
        """Decide keys-only vs labelled before paint, from an estimated width."""
        needed = sum(len(k) + len(lbl) + 5 for k, lbl, _ in HOTKEYS) + 4
        tight = self.size.width < needed
        for idx in range(len(HOTKEYS)):
            try:
                self.query_one(f"#footer-label-{idx}", Static).set_class(tight, "hidden")
            except Exception:
                pass
