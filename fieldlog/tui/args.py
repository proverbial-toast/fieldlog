"""The ARGS band: variable substitution, the token view, and the raw
template editor.

A mixin of FieldlogApp, not a widget: `self` is the app, and these
methods use its state and its widget tree directly. Split out for
navigability — app.py held every pane at once.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from fieldlog.archive import peek_run_number
from fieldlog.state import resolve_flags, run_stamp
from fieldlog.tui.helpers import arg_groups
from fieldlog.tui.theme import ACCENT, FG, MUTED, SOFT, WARN
from fieldlog.tui.widgets import ArgsTextArea



class ArgsBandMixin:
    """The ARGS band: variable substitution, the token view, and the raw"""

    def resolve_flags(self, flags: str, out_dir: Optional[str] = None) -> str:
        """Straight string replacement of the bindings, every occurrence."""
        return resolve_flags(self.session, flags, out_dir=out_dir if out_dir is not None else self.pending_out_dir())

    def pending_out_dir(self) -> str:
        """$OUTDIR as it would resolve for a job started now (preview only).

        The run number is read, never reserved: the launch path claims it, and
        a preview that claimed one would burn a number per keypress.
        """
        run_id = peek_run_number(self.session.target_dir)
        return f"{self.session.log_dir()}{run_stamp()}_{run_id:02d}"

    def bound_values(self) -> List[str]:
        """Substituted values that should render amber in the token view."""
        s = self.session
        root = (s.artifact_root or "").strip().rstrip("/")
        return [v for v in (s.target, s.dns_name, s.interface, s.effective_lhost(), root) if v]

    CHAIN_ARGS_NOTE = "[args] edit a chain's steps in its yaml · per-recipe edits still apply"

    def current_flags(self) -> Tuple[dict, dict, str, str]:
        """(tool, preset, key, template). The template is unresolved, edit or
        not: baking today's scope into an edit is how a job ends up aimed at
        the target the operator has since moved off. Use `resolve_flags` for a
        preview.

        With a chain selected there is no single recipe to edit: the key is
        `chain/<id>` (so pins work) and tool, preset and template are empty.
        """
        if self.selected_chain_id:
            return {}, {}, f"chain/{self.selected_chain_id}", ""
        tool = self.get_tool(self.selected_tool_id) or (self.recipes[0] if self.recipes else {})
        preset = self.get_preset(tool, self.selected_preset_id) if tool else {}
        key = f"{tool.get('id', '')}/{preset.get('id', '')}"
        return tool, preset, key, self.flag_edits.get(key, preset.get("flags", ""))

    def _refresh_args_band(self) -> None:
        try:
            tokens_wrap = self.query_one("#args-tokens-wrap", Vertical)
        except Exception:
            return
        chain = self.selected_chain()
        if chain is not None:
            self._refresh_chain_args_band(tokens_wrap, chain)
            return
        with self._repaint("ARGS band"):
            tool, preset, key, template = self.current_flags()
            if not tool:
                return
            # Tokens show the command as it would run; the raw editor holds the
            # template. Inside the guard: resolving reads the interface address
            # through an ioctl, which is not a thing that cannot fail.
            resolved = self.resolve_flags(template)
            is_dirty = key in self.flag_edits

            self.query_one("#args-header-title", Static).update("ARGS *" if is_dirty else "ARGS")
            btn_reset = self.query_one("#args-btn-reset", Static)
            btn_reset.set_class(is_dirty, "-dirty")
            btn_reset.styles.color = WARN if is_dirty else MUTED
            self.query_one("#args-btn-mode", Static).update(
                "[E] token view" if self.args_raw_mode else "[E] edit raw"
            )
            self.query_one("#args-bin-label", Static).update(
                Text.assemble(("$ ", ACCENT), (tool.get("bin", tool["id"]), f"bold {FG}"))
            )

            tokens_wrap.remove_children()
            bound = self.bound_values()
            max_row_width = 70 if self._current_layout == "split" else 40
            current: List[Static] = []
            width = 0

            def flush() -> None:
                nonlocal current, width
                if current:
                    row = Horizontal(classes="arg-pairs-row")
                    tokens_wrap.mount(row)
                    row.mount_all(current)
                    current, width = [], 0

            for g in arg_groups(resolved)["groups"]:
                flag, value = g["flag"], g["value"]
                is_bound = bool(value) and any(v in value for v in bound)
                if flag and value:
                    text = Text.assemble((flag + " ", ACCENT), (value, WARN if is_bound else SOFT))
                elif flag:
                    text = Text(flag, style=ACCENT)
                else:
                    text = Text(value, style=WARN if is_bound else FG)

                group_len = len(flag) + (len(value) + 1 if value else 0)
                if group_len > 36:
                    flush()
                    row = Horizontal(classes="arg-pairs-row-wide")
                    tokens_wrap.mount(row)
                    row.mount(Static(text, classes="arg-pair arg-pair-wide"))
                    continue
                if width + group_len + 4 > max_row_width and current:
                    flush()
                current.append(Static(text, classes="arg-pair"))
                width += group_len + 4
            flush()

            raw_area = self.query_one("#args-raw-area", ArgsTextArea)
            if raw_area.text != template:
                raw_area.text = template

    def _refresh_chain_args_band(self, tokens_wrap: Vertical, chain: dict) -> None:
        """`$ chain reach` and its steps as tokens. There is nothing to edit
        here: a step's own args edit still applies when the chain runs it."""
        with self._repaint("chain ARGS band"):
            self.query_one("#args-header-title", Static).update("ARGS")
            btn_reset = self.query_one("#args-btn-reset", Static)
            btn_reset.set_class(False, "-dirty")
            btn_reset.styles.color = MUTED
            self.query_one("#args-btn-mode", Static).update("[E] edit raw")
            self.query_one("#args-bin-label", Static).update(
                Text.assemble(("$ ", ACCENT), ("chain ", f"bold {FG}"), (chain["id"], f"bold {ACCENT}"))
            )
            tokens_wrap.remove_children()
            row = Horizontal(classes="arg-pairs-row")
            tokens_wrap.mount(row)
            row.mount_all([
                Static(Text(step["recipe"] + ("?" if step.get("continue") else ""), style=SOFT),
                       classes="arg-pair")
                for step in chain.get("steps", [])
            ])

    @property
    def args_dirty(self) -> bool:
        _, _, key, _ = self.current_flags()
        return key in self.flag_edits

    def action_toggle_args_mode(self, force_raw: Optional[bool] = None) -> None:
        if self.selected_chain_id:
            self.write_system_log(self.CHAIN_ARGS_NOTE, style=WARN)
            return
        self.args_raw_mode = (not self.args_raw_mode) if force_raw is None else force_raw
        tokens = self.query_one("#args-tokens-scroll", VerticalScroll)
        raw_wrap = self.query_one("#args-raw-wrap", Vertical)
        raw_area = self.query_one("#args-raw-area", ArgsTextArea)
        btn_mode = self.query_one("#args-btn-mode", Static)

        if self.args_raw_mode:
            tokens.add_class("hidden")
            raw_wrap.remove_class("hidden")
            btn_mode.update("[E] token view")
            raw_area.focus()
        else:
            _, _, key, _ = self.current_flags()
            self.flag_edits[key] = raw_area.text  # edits apply live; there is no cancel
            raw_wrap.add_class("hidden")
            tokens.remove_class("hidden")
            btn_mode.update("[E] edit raw")
            self.set_focus(None)
            self._refresh_args_band()
            self._refresh_variants()

    def action_reset_args(self) -> None:
        if self.selected_chain_id:
            self.write_system_log(self.CHAIN_ARGS_NOTE, style=WARN)
            return
        tool, preset, key, _ = self.current_flags()
        self.flag_edits.pop(key, None)
        self.query_one("#args-raw-area", ArgsTextArea).text = preset.get("flags", "")
        self._refresh_args_band()
        self._refresh_variants()
        self.write_system_log(f"[args] reset {key} to variant default")
