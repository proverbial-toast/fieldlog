"""The VARIANTS pane: one row per preset of the selected tool, or the
steps of the selected chain.

A mixin of FieldlogApp, not a widget: `self` is the app, and these
methods use its state and its widget tree directly. Split out for
navigability — app.py held every pane at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches, WrongType
from textual.widgets import Static

from fieldlog.recipes import check_chain, chain_steps
from fieldlog.tui.helpers import short_reason, truncate_right
from fieldlog.tui.theme import ACCENT, DIM, FG, MUTED, SOFT, UNFOCUSED, WARN
from fieldlog.tui.widgets import VariantRowWidget

if TYPE_CHECKING:
    from fieldlog.recipes import Verdict



class VariantsPaneMixin:
    """The VARIANTS pane: one row per preset of the selected tool, or the"""

    def _refresh_variants(self) -> None:
        try:
            var_list = self.query_one("#variant-list", Vertical)
        except Exception:
            return
        chain = self.selected_chain()
        if chain is not None:
            self._refresh_chain_variants(var_list, chain)
            return
        tool, preset = self.selected_recipe()
        if not tool:
            return
        key = f"{tool['id']}/{preset['id']}"
        verdict = self.verdict(tool, preset)

        with self._repaint("VARIANTS pane"):
            self.query_one("#variants-bin", Static).update(
                Text(tool.get("bin", tool["id"]), style=f"bold {FG}")
            )
            self.query_one("#variants-variant", Static).update(
                Text(f"variant {preset['id']}", style=DIM)
            )

            var_list.remove_children()
            try:
                # minus the row's border-left and padding-left, and the
                # scrollbar column — which only appears once the list
                # overflows, i.e. after this width would have been measured.
                avail = self.query_one("#variants-scroll", VerticalScroll).size.width - 3
            except (NoMatches, WrongType):
                avail = 37
            avail = max(24, avail)
            rows = []
            for i, p in enumerate(tool.get("presets", [])):
                is_active = (p["id"] == preset["id"])
                num = str(i + 1) if i < 9 else ("0" if i == 9 else "·")
                # Cap the label so a wordy variant name cannot squeeze out the
                # argument string, which is what this pane exists to show.
                label = truncate_right(p.get("name", p["id"]), max(10, avail * 3 // 5))
                text = Text.assemble((f"{num} ", MUTED), (label, ACCENT if is_active else SOFT))
                if p.get("src"):
                    text.append(f"  {p['src']}", style=ACCENT)
                if self._writes_outdir(p):
                    text.append("  ⇩", style=WARN)
                # The argument string itself — the reason this column is wide.
                flags = " ".join(str(p.get("flags", "")).split())
                budget = avail - len(text.plain) - 2       # 2 for the separator
                if flags and budget >= 4:
                    text.append("  " + truncate_right(flags, budget), style=DIM)
                row = VariantRowWidget(
                    p["id"], text, classes="variant-row variant-active" if is_active else "variant-row"
                )
                if self._writes_outdir(p):
                    row.tooltip = "writes its own file into $OUTDIR"
                elif p.get("src"):
                    row.tooltip = "from a drop-in file"
                rows.append(row)
            if rows:
                var_list.mount_all(rows)

            self._paint_run_button(verdict, "[Enter] Run")
            self.query_one("#btn-pin", Static).update(
                "[P] Unpin" if key in self.pinned else "[P] Pin"
            )
            self.query_one("#variants-crumb", Static).update(self._variants_crumb(tool, preset))

    def _refresh_chain_variants(self, var_list: Vertical, chain: dict) -> None:
        """The steps, numbered and read-only: a chain's order lives in its yaml.
        Plain Statics, not VariantRowWidgets — a click here selects nothing."""
        verdict = check_chain(self.catalog, chain, self.session, self.flag_edits)
        key = f"chain/{chain['id']}"
        with self._repaint("chain VARIANTS pane"):
            self.query_one("#variants-bin", Static).update(Text("chain", style=f"bold {FG}"))
            self.query_one("#variants-variant", Static).update(Text(chain["id"], style=DIM))

            var_list.remove_children()
            rows = []
            for index, (tool, preset, keep_going) in enumerate(chain_steps(self.catalog, chain), start=1):
                step_blocked = self.blocked_flag(tool, preset)
                label = f"{tool['id']}/{preset['id']}" + ("?" if keep_going else "")
                text = Text.assemble(
                    (f"{index} ", MUTED),
                    (label, UNFOCUSED if step_blocked else SOFT),
                )
                flags = " ".join(str(preset.get("flags", "")).split())
                if flags:
                    text.append("  " + flags, style=MUTED if step_blocked else DIM)
                rows.append(Static(text, classes="variant-row"))
            if rows:
                var_list.mount_all(rows)

            self._paint_run_button(verdict, "[Enter] Run chain")
            self.query_one("#btn-pin", Static).update(
                "[P] Unpin" if key in self.pinned else "[P] Pin"
            )
            self.query_one("#variants-crumb", Static).update(
                "" if self._current_layout != "stacked" else f"chain · {chain['id']}"
            )

    def _paint_run_button(self, verdict: "Verdict", ready_label: str) -> None:
        """What Enter will do, and when it will do nothing, why not.

        The button is the last thing an operator reads before pressing Enter,
        so a refusal has to say its reason there — `Not runnable` on its own
        sends them hunting through doctor for something the verdict already
        knows. The label has ~20 cells at the narrowest split, which is the
        short form and no more; the ✗ and the red of `-disabled` carry the
        "cannot run" that the words no longer spell out. The tooltip holds the
        reason whole, which for a chain names the step it stopped at, and
        `action_run_task` writes that same full reason to the System tab — so
        a refusal is legible whether they read the button first or pressed
        Enter and wondered why nothing happened.
        """
        btn_run = self.query_one("#btn-run", Static)
        if verdict.blocked:
            btn_run.update(f"✗ {short_reason(verdict, 16)}")
            btn_run.tooltip = verdict.reason
        else:
            btn_run.update(ready_label)
            btn_run.tooltip = None
        btn_run.set_class(verdict.blocked, "-disabled")

    def _variants_crumb(self, tool: dict, preset: dict) -> str:
        if self._current_layout != "stacked":
            return ""
        if self.stacked_pane == "task":
            return "esc · back to recipes"
        return f"{tool.get('bin', tool['id'])} · {preset.get('name', preset['id'])}"

    def _select_variant_by_index(self, index: int) -> None:
        if self.selected_chain_id:
            return          # a chain's steps are fixed; there is nothing to pick
        tool = self.get_tool(self.selected_tool_id)
        presets = tool.get("presets", []) if tool else []
        if 0 <= index < len(presets):
            self.selected_preset_id = presets[index]["id"]
            self._refresh_variants()
            self._refresh_args_band()

    def _step_variant(self, delta: int) -> None:
        if self.selected_chain_id:
            return
        tool = self.get_tool(self.selected_tool_id)
        presets = tool.get("presets", []) if tool else []
        if not presets:
            return
        idx = next((i for i, p in enumerate(presets) if p["id"] == self.selected_preset_id), 0)
        self.selected_preset_id = presets[(idx + delta) % len(presets)]["id"]
        self._refresh_variants()
        self._refresh_args_band()

    def action_prev_variant(self) -> None:
        self._step_variant(-1)

    def action_next_variant(self) -> None:
        self._step_variant(1)
