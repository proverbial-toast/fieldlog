"""Small clickable widgets for the TUI. They reach the app through self.app."""

from __future__ import annotations

from typing import Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Static, TextArea

from fieldlog.tui.models import TabDescriptor
from fieldlog.tui.theme import ACCENT, DIM, ERR, FAINT, FG, MUTED, WARN


class TabClose(Static):
    """A tab's ×. Stops propagation so closing never also selects."""

    def on_click(self, event) -> None:
        event.stop()
        event.prevent_default()
        parent = self.parent
        while parent is not None and not isinstance(parent, TabItem):
            parent = parent.parent
        if parent is not None:
            self.app.action_close_tab(parent.tab.id)


class TabItem(Horizontal):
    """One tab button in the closable tab strip."""

    ICONS = {
        "system": ("▪", FAINT),
        "active": ("●", WARN),
        "await": ("⌨", WARN),
        "done": ("✓", ACCENT),
        "failed": ("✗", ERR),
    }

    def __init__(self, tab: TabDescriptor, is_active: bool, awaiting: bool = False) -> None:
        super().__init__(classes="tab-item-active" if is_active else "tab-item")
        self.tab = tab
        self.is_active = is_active
        self.awaiting = awaiting

    @property
    def tab_id(self) -> str:
        return self.tab.id

    @property
    def closable(self) -> bool:
        """Whether this tab has an ×. System holds the transcript and never does.

        It used to yield a near-invisible `·` in the ×'s place, to keep the two
        kinds of tab the same shape. That cost the System tab two cells to say
        what the missing × already says.
        """
        return self.tab.id != "system"

    def _icon(self) -> Tuple[str, str]:
        key = "await" if self.awaiting else self.tab.status
        return self.ICONS.get(key, ("▪", FAINT))

    def compose(self) -> ComposeResult:
        icon, icon_color = self._icon()
        yield Static(Text(icon, style=icon_color), classes="tab-icon")
        yield Static(Text(self.tab.label, style=FG if self.is_active else DIM), classes="tab-label")
        if self.closable:
            yield TabClose(Text("×", style=DIM if self.is_active else MUTED), classes="tab-close")

    def update_tab(self, tab: TabDescriptor, is_active: bool, awaiting: bool = False) -> None:
        self.tab = tab
        self.is_active = is_active
        self.awaiting = awaiting
        icon, icon_color = self._icon()
        try:
            self.query_one(".tab-icon", Static).update(Text(icon, style=icon_color))
            self.query_one(".tab-label", Static).update(
                Text(self.tab.label, style=FG if is_active else DIM)
            )
            if self.closable:
                self.query_one(".tab-close", Static).update(
                    Text("×", style=DIM if is_active else MUTED)
                )
        except Exception:
            pass

    def on_click(self, event) -> None:
        target = getattr(event, "widget", None) or getattr(event, "target", None)
        if isinstance(target, TabClose) or (
            getattr(target, "has_class", None) and target.has_class("tab-close")
        ):
            event.stop()
            self.app.action_close_tab(self.tab.id)
        else:
            self.app.action_select_tab(self.tab.id)


class RecipeRowWidget(Static):
    """One clickable row in the flattened RECIPES list."""

    def __init__(self, index: int, renderable: Text, classes: str = "recipe-row") -> None:
        super().__init__(renderable, classes=classes)
        self.index = index

    def on_click(self, event) -> None:
        event.stop()
        self.app.tree_row_clicked(self.index)


class VariantRowWidget(Static):
    """One variant row; clicking selects it and takes focus into VARIANTS."""

    def __init__(self, preset_id: str, renderable: Text, classes: str = "variant-row") -> None:
        super().__init__(renderable, classes=classes)
        self.preset_id = preset_id

    def on_click(self, event) -> None:
        event.stop()
        self.app.variant_row_clicked(self.preset_id)


class StdinChip(Static):
    """A quick-reply chip. Carries its reply, so a second prompt can mount a
    fresh set without colliding with IDs the previous set still holds."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply, classes="stdin-chip", markup=False)
        self.reply = reply

    def on_click(self, event) -> None:
        event.stop()
        self.app.send_stdin_reply(self.reply)


class ArgsTextArea(TextArea):
    """Raw flag editor. Esc and Ctrl+J commit, blur and return to token view."""

    def on_key(self, event) -> None:
        if event.key == "escape" or event.key in ("ctrl+enter", "ctrl+j", "super+enter"):
            event.prevent_default()
            event.stop()
            self.app.action_toggle_args_mode(force_raw=False)


class InterruptInput(Input):
    """An Input whose Ctrl+C is the app's: SIGINT to the active tab's job.

    Textual's Input binds ctrl+c to copy, and a focused widget's bindings are
    asked before the app's. With nothing selected the copy steps aside; with
    any text selected it won, so in the reply field — where the hint promises
    SIGINT, and where a job wedged at a prompt most needs it — Ctrl+C copied
    the selection and the job ran on.
    """

    def on_key(self, event) -> None:
        if event.key == "ctrl+c":
            event.prevent_default()
            event.stop()
            self.app.action_sigint()


class StdinInput(InterruptInput):
    """The stdin field. Enter sends; Esc blurs without sending; Ctrl+C interrupts."""

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.app.dismiss_stdin_focus()
            return
        super().on_key(event)
