"""Modal screens for the TUI (TargetModal stays in app.py, coupled to interface probing)."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from fieldlog import __version__ as VERSION
from fieldlog.recipes import (
    RECIPES_PATH,
    chain_matches,
    display_path,
    search,
    steps_label,
)
from fieldlog.tui.helpers import copy_text_to_clipboard, short_reason, truncate_right
from fieldlog.tui.theme import (
    ACCENT, BG_BASE, DIM, ERR, FAINT, FG, MUTED, SOFT, UNFOCUSED, WARN,
)

if TYPE_CHECKING:
    from fieldlog.app import FieldlogApp


class HelpModal(ModalScreen):
    """[?] Key bindings overlay."""

    BINDINGS = [
        ("escape", "dismiss_help", "Dismiss"),
        ("question_mark", "dismiss_help", "Dismiss"),
        ("enter", "dismiss_help", "Dismiss"),
    ]

    GROUPS = [
        ("navigate", [
            ("↑ ↓ / j k", "Move cursor in focused pane"),
            ("/", "Filter recipes"),
            ("!", "Toggle runnable-only"),
            ("Ctrl+P", "Task palette · ↵ loads args, ⇧↵ runs"),
            ("Ctrl+P ›", "Harness commands only"),
        ]),
        ("task", [
            ("Enter", "Recipes: select + jump to variants · Variants: run"),
            ("1-9,0", "Select variant · read chain step"),
            (", / .", "Previous / next variant / step"),
            ("Tab", "Move focus recipes / variants"),
            ("E", "Edit args (raw / tokens)"),
            ("R", "Reset args to variant"),
            ("P", "Pin / unpin task"),
        ]),
        ("jobs", [
            ("Esc", "Back to recipes from variants"),
            ("[ / ]", "Previous / next tab"),
            ("Ctrl+C", "Interrupt running job (SIGINT)"),
            ("Y", "Copy tail -f for active log or transcript"),
            ("Ctrl+Shift+C", "Copy whole log to clipboard"),
            ("Ctrl+W", "Close active tab · running job asks kill / detach"),
            ("⇧W", "Close all finished tabs"),
        ]),
        ("scope & config", [
            ("T", "Target scope + log destination"),
            ("M", "Recipe manager (sources + counts)"),
            ("⇧R", "Reload recipes (keeps sessions)"),
            ("Y", "Copy recipes yaml path (Ctrl+P)"),
            ("L", "Split / stacked layout"),
            ("H", "Show / hide the hotkey bar"),
            ("?", "This list"),
            ("Q Q", "Quit harness (double-tap)"),
        ]),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            with Horizontal(id="help-header"):
                yield Static("┤ KEY BINDINGS ├", id="help-title")
                yield Static(f"fieldlog v{VERSION} · esc to dismiss", id="help-subhint")
            # Two columns, two groups each. Four columns in a terminal leaves
            # ~20 cells per label, which truncates every one of them.
            with Horizontal(id="help-body"):
                for column in (self.GROUPS[0::2], self.GROUPS[1::2]):
                    with Vertical(classes="help-col"):
                        for title, keys in column:
                            yield Static(title.upper(), classes="help-group-title")
                            for k, label in keys:
                                with Horizontal(classes="help-row"):
                                    yield Static(k, classes="help-key")
                                    yield Static(label, classes="help-label")
            with Horizontal(id="help-footer"):
                yield Static(display_path(RECIPES_PATH), id="help-catalog-path")
                yield Static(
                    "defines every tool and variant · T → scope, then Y to copy",
                    id="help-catalog-note",
                )

    def action_dismiss_help(self) -> None:
        self.dismiss(None)

    def on_click(self, event) -> None:
        target = getattr(event, "widget", None) or getattr(event, "target", None)
        if target is self:
            event.stop()
            self.dismiss(None)

class RecipeManagerModal(ModalScreen):
    """[M] Recipe manager — stats, definition sources, indexed tools.

    Swallows Y and ⇧R so neither reaches the global map.
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("m", "close", "Close"),
    ]

    # Row index -> tool id, filled by compose. See `_mgr_tool_id`.
    _mgr_tool_ids: List[str] = []

    def compose(self) -> ComposeResult:
        app: "FieldlogApp" = self.app  # type: ignore[assignment]
        with Vertical(id="mgr-box"):
            with Horizontal(id="mgr-header"):
                yield Static("┤ RECIPE MANAGER ├", id="mgr-title")
                yield Static(f"rev {app.reload_revision} · esc to dismiss", id="mgr-subhint")

            with Horizontal(id="mgr-stats"):
                for label, value, color in app.manager_stats():
                    with Horizontal(classes="mgr-stat"):
                        yield Static(label, classes="mgr-stat-label")
                        yield Static(Text(value, style=color), classes="mgr-stat-value")

            with VerticalScroll(id="mgr-scroll"):
                yield Static("definition sources", classes="mgr-section-label")
                with Vertical(id="mgr-sources"):
                    for src in app.manager_sources():
                        with Horizontal(classes="mgr-source-row"):
                            yield Static(Text(src["kind"], style=src["kind_color"]), classes="mgr-src-kind")
                            yield Static(src["path"], classes="mgr-src-path", markup=False)
                            yield Static(Text(src["count"], style=src["count_color"]), classes="mgr-src-count")
                            if src["copyable"]:
                                yield Static("[Y] copy path", id="btn-copy-catalog-path", markup=False)

                yield Static("indexed tools", classes="mgr-section-label")
                with Vertical(id="mgr-tools"):
                    # A Textual id has to be an identifier, and a tool id is
                    # whatever the yaml said — a drop-in is free to call one
                    # `acme.probe` or `2fa`, which would raise BadIdentifier and
                    # take the whole modal down. Rows are numbered and the id is
                    # looked up on click instead.
                    mgr_tools = app.manager_tools()
                    self._mgr_tool_ids = [t["id"] for t in mgr_tools]
                    for index, t in enumerate(mgr_tools):
                        row = Horizontal(classes="mgr-tool-row", id=f"mgrtool-{index}")
                        row.tooltip = t["state"]
                        with row:
                            yield Static(Text(t["bin"], style=t["bin_style"]), classes="mgr-tool-bin")
                            yield Static(t["name"], classes="mgr-tool-name", markup=False)
                            yield Static(t["variants"], classes="mgr-tool-variants")
                            yield Static(Text(t["emits"], style=WARN), classes="mgr-tool-emits")
                            yield Static(Text(t["state"], style=t["state_color"]), classes="mgr-tool-state")
                            yield Static(Text(t["badge"], style=ACCENT), classes="mgr-tool-badge")

            with Horizontal(id="mgr-footer"):
                yield Static("[⇧R] Reload recipes", id="btn-mgr-reload", markup=False)
                yield Static(app.reload_note(), id="mgr-reload-note")
                yield Static("", id="mgr-footer-spacer")
                yield Static("[T] scope & logs →", id="btn-mgr-scope", markup=False)

    def on_click(self, event) -> None:
        target = getattr(event, "widget", None) or getattr(event, "target", None)
        target_id = getattr(target, "id", "") or ""
        if target is self:
            event.stop()
            self.dismiss(None)
            return
        if target_id == "btn-copy-catalog-path":
            self.action_copy_path()
        elif target_id == "btn-mgr-reload":
            self.action_reload()
        elif target_id == "btn-mgr-scope":
            self.action_scope()
        else:
            node = target
            while node is not None and not (getattr(node, "id", "") or "").startswith("mgrtool-"):
                node = node.parent
            if node is not None:
                tool_id = self._mgr_tool_id(node.id)
                if tool_id is not None:
                    self.app.select_tool(tool_id)
                    self.dismiss(None)

    def _mgr_tool_id(self, row_id: str) -> Optional[str]:
        """The tool a numbered row stands for, or None if the row is stale."""
        try:
            return self._mgr_tool_ids[int(row_id[len("mgrtool-"):])]
        except (ValueError, IndexError):
            return None

    def on_key(self, event) -> None:
        if event.key in ("y", "Y"):
            event.prevent_default()
            event.stop()
            self.action_copy_path()
        elif event.key == "R":
            event.prevent_default()
            event.stop()
            self.action_reload()
        elif event.key in ("t", "T"):
            event.prevent_default()
            event.stop()
            self.action_scope()

    def action_copy_path(self) -> None:
        btn = self.query_one("#btn-copy-catalog-path", Static)
        copy_text_to_clipboard(str(RECIPES_PATH), app=self.app)
        btn.update("copied ✓")
        btn.styles.color = ACCENT
        self.app.write_system_log(f"[config] {RECIPES_PATH} copied to clipboard")
        self.set_timer(1.6, lambda: (btn.update("[Y] copy path"), setattr(btn.styles, "color", DIM)))

    def action_reload(self) -> None:
        btn = self.query_one("#btn-mgr-reload", Static)
        btn.update("↻ reloading…")
        self.app.action_reload_recipes()
        self.dismiss(None)
        self.app.action_recipe_manager()

    def action_scope(self) -> None:
        self.dismiss(None)
        self.app.action_target_scope()

    def action_close(self) -> None:
        self.dismiss(None)


class CloseJobModal(ModalScreen[Optional[str]]):
    """W on a running tab: kill or detach. Esc keeps the tab and the job."""

    BINDINGS = [
        ("escape", "keep", "Keep"),
        ("k", "kill", "Kill"),
        ("d", "detach", "Detach"),
    ]

    def __init__(self, label: str, artifact: str) -> None:
        super().__init__()
        self.label = label
        self.artifact = artifact

    def compose(self) -> ComposeResult:
        with Vertical(id="closing-box"):
            with Horizontal(id="closing-header"):
                yield Static("┤ JOB STILL RUNNING ├", id="closing-title")
                yield Static("esc to keep the tab", id="closing-subhint")
            with Vertical(id="closing-body"):
                yield Static(f"{self.label} has not exited.", id="closing-label", markup=False)
                with Horizontal(classes="closing-choice", id="choice-kill"):
                    yield Static(Text("[K]", style=f"bold {ERR}"), classes="closing-key")
                    yield Static("Kill", classes="closing-name")
                    yield Static("SIGINT, SIGKILL after 10s, close the tab", classes="closing-hint")
                with Horizontal(classes="closing-choice", id="choice-detach"):
                    yield Static(Text("[D]", style=f"bold {ACCENT}"), classes="closing-key")
                    yield Static("Detach", classes="closing-name")
                    yield Static("keep running, close the tab, keep writing to disk", classes="closing-hint")
                # On screen at the moment of decision, so output stays findable.
                yield Static(self.artifact, id="closing-path", markup=False)

    def on_click(self, event) -> None:
        target = getattr(event, "widget", None) or getattr(event, "target", None)
        node = target
        while node is not None and (getattr(node, "id", "") or "") not in ("choice-kill", "choice-detach"):
            node = node.parent
        if node is not None:
            self.dismiss("kill" if node.id == "choice-kill" else "detach")
        elif target is self:
            self.dismiss(None)

    def action_kill(self) -> None:
        self.dismiss("kill")

    def action_detach(self) -> None:
        self.dismiss("detach")

    def action_keep(self) -> None:
        self.dismiss(None)


class QuitConfirm(ModalScreen[bool]):
    """Guard against a fat-fingered quit while scans are still running."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("n", "cancel", "Cancel"),
        ("enter", "confirm", "Quit"),
        ("y", "confirm", "Quit"),
    ]

    def __init__(self, running: int) -> None:
        super().__init__()
        self.running = running

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("┤ CONFIRM QUIT ├", id="modal-title")
            plural = "scan" if self.running == 1 else "scans"
            yield Label(f"{self.running} {plural} still running — quitting stops them and keeps their records.")
            yield Static("[Enter/Y] quit     [Esc/N] cancel", id="quit-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
class PaletteRowItem(Static):
    def __init__(self, index: int, text: Text, classes: str = "") -> None:
        super().__init__(text, classes=classes)
        self.index = index

    def on_click(self, event) -> None:
        if isinstance(self.screen, PaletteModal):
            shift = bool(getattr(event, "shift", False))
            self.screen.pick_index(self.index, run=shift)


# The palette's second half: what the harness itself can do, in the same list
# as the tasks. Here rather than in theme.py because one hint is a catalog path,
# and the palette constants have to stay readable without loading the yaml.
META_COMMANDS = [
    {"id": "mgr", "key": "M", "label": "Open recipe manager", "hint": "sources · counts · availability", "action": "recipe_manager"},
    {"id": "reload", "key": "⇧R", "label": "Reload recipes from yaml", "hint": "keeps sessions", "action": "reload_recipes"},
    {"id": "scope", "key": "T", "label": "Target scope & log destination", "hint": "", "action": "target_scope"},
    {"id": "copypath", "key": "Y", "label": "Copy recipes yaml path", "hint": str(RECIPES_PATH), "action": "copy_catalog_path"},
    {"id": "copytail", "key": "Y", "label": "Copy tail -f for active log", "hint": "the session transcript on the System tab", "action": "copy_tail"},
    {"id": "layout", "key": "L", "label": "Toggle split / stacked layout", "hint": "stacked ≤ 120 cols", "action": "toggle_layout"},
    {"id": "runnable", "key": "!", "label": "Toggle runnable-only filter", "hint": "", "action": "toggle_hide_missing"},
    {"id": "closefin", "key": "⇧W", "label": "Close finished job tabs", "hint": "", "action": "close_finished_tabs"},
    {"id": "copylog", "key": "Ctrl+Shift+C", "label": "Copy active log to clipboard", "hint": "whole buffer", "action": "copy_log"},
    {"id": "keys", "key": "?", "label": "Key bindings", "hint": "", "action": "help"},
]


class PaletteModal(ModalScreen[Optional[Tuple]]):
    """Ctrl+P palette. Enter loads the row's args; ⇧Enter loads and runs."""

    BINDINGS = [
        ("escape", "cancel", "Dismiss"),
        ("up", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
        ("enter", "load_selected", "Load"),
        ("shift+enter", "run_selected", "Run"),
    ]

    def __init__(self, fieldlog_app: "FieldlogApp") -> None:
        super().__init__()
        self.fieldlog_app = fieldlog_app
        self.query_text = ""
        self.cursor = 0
        self.flat_items: List[dict] = []
        self.row_widgets: List[PaletteRowItem] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            with Horizontal(id="palette-input-row"):
                yield Static("›", id="palette-prompt")
                yield Input(placeholder="run a task", id="palette-input")
                yield Static("tasks + harness · > for commands only", id="palette-hint")
            with Vertical(id="palette-results"):
                pass

    def on_mount(self) -> None:
        self.refresh_results()
        self.query_one("#palette-input", Input).focus()

    def get_groups(self) -> Tuple[List[dict], List[dict]]:
        app = self.fieldlog_app
        raw = self.query_text.strip()
        meta_only = raw.startswith(">")
        q = (raw[1:] if meta_only else raw).strip().lower()

        groups: List[dict] = []
        flat: List[dict] = []

        if not meta_only:
            chain_rows: List[dict] = []
            if q:
                hits = search(app.recipes, q, app.blocked_flag, app.hide_missing, limit=7)
                # The verdict rides along, so a row can say what is actually
                # missing. Asked for only when the row is blocked: it re-runs a
                # $PATH lookup and an ioctl per row.
                rows = [
                    {"kind": "task", "tool": t, "preset": p, "blocked": b,
                     "verdict": app.verdict(t, p) if b else None}
                    for t, p, b in hits
                ]
                chain_rows = [
                    {"kind": "chain", "chain": c, "blocked": app.chain_blocked_flag(c)}
                    for c in app.chains if chain_matches(c, q)
                ]
                title = "tasks"
            else:
                rows = []
                seen = set()
                for key in app.pinned + app.recent:
                    if key in seen:
                        continue
                    seen.add(key)
                    tool_id, _, preset_id = key.partition("/")
                    if tool_id == "chain":
                        c = app.get_chain(preset_id)
                        if c is not None:
                            rows.append({"kind": "chain", "chain": c, "blocked": app.chain_blocked_flag(c)})
                    else:
                        t = app.get_tool(tool_id)
                        if not t:
                            continue
                        p = app.get_preset(t, preset_id)
                        verdict = app.verdict(t, p)
                        rows.append({"kind": "task", "tool": t, "preset": p,
                                     "blocked": verdict.blocked,
                                     "verdict": verdict if verdict.blocked else None})
                    if len(rows) >= 6:
                        break
                title = "pinned & recent"
            if rows:
                groups.append({"title": title, "items": rows})
                flat.extend(rows)
            if chain_rows:
                groups.append({"title": "chains", "items": chain_rows})
                flat.extend(chain_rows)

        metas = [{"kind": "meta", "meta": m} for m in META_COMMANDS if self._meta_match(m, q)]
        if metas:
            groups.append({"title": "harness commands" if meta_only else "harness", "items": metas})
            flat.extend(metas)
        return groups, flat

    @staticmethod
    def _meta_match(meta: dict, q: str) -> bool:
        if not q:
            return True
        hay = f"{meta['label']} {meta['id']} {meta['hint']}".lower()
        i = 0
        for ch in q:
            i = hay.find(ch, i)
            if i == -1:
                return False
            i += 1
        return True

    def _render_item(self, item: dict, is_on: bool) -> Tuple[Text, str]:
        if item["kind"] == "meta":
            m = item["meta"]
            cells = (f"{m['key'][:12]:<12}", f"{m['label'][:34]:<35}", f"{m['hint'][:24]:>24}")
            if is_on:
                return (
                    Text.assemble((cells[0] + " ", f"bold {BG_BASE}"), (cells[1] + " ", BG_BASE), (cells[2], BG_BASE)),
                    "palette-row palette-row-cursor-ok",
                )
            return (
                Text.assemble((cells[0] + " ", f"bold {ACCENT}"), (cells[1] + " ", SOFT), (cells[2], MUTED)),
                "palette-row",
            )

        ok = not item["blocked"]
        if item["kind"] == "chain":
            c = item["chain"]
            cells = (
                f"{c['id'][:10]:<11}",
                f"{c.get('name', c['id'])[:26]:<27}",
                f"{('' if ok else 'step blocked'):<16}",
                f"{('chain · ' + steps_label(c))[:16]:>16}",
            )
        else:
            t, p = item["tool"], item["preset"]
            # The blocked verdict as check_recipe reached it: a missing target,
            # an unsafe one and a missing local address are different problems
            # and used to read alike here.
            # The verdict is fetched only for a row search() called blocked; if
            # the two evaluations disagree within one keystroke (a binary just
            # installed, an interface just up) the cell is blank, not a crash.
            verdict = item.get("verdict")
            state = short_reason(verdict) if not ok and verdict is not None else ""
            cells = (
                f"{t['id'][:10]:<11}",
                f"{p.get('name', p['id'])[:26]:<27}",
                f"{state[:15]:<16}",
                f"{truncate_right(t.get('name', ''), 16):>16}",
            )
        if is_on and ok:
            return (
                Text.assemble(
                    (cells[0] + " ", f"bold {BG_BASE}"), (cells[1] + " ", BG_BASE),
                    (cells[2] + " ", BG_BASE), (cells[3], BG_BASE),
                ),
                "palette-row palette-row-cursor-ok",
            )
        if is_on:
            return (
                Text.assemble(
                    (cells[0] + " ", f"bold {DIM}"), (cells[1] + " ", DIM),
                    (cells[2] + " ", WARN), (cells[3], FAINT),
                ),
                "palette-row palette-row-cursor-blocked",
            )
        if ok:
            return (
                Text.assemble(
                    (cells[0] + " ", f"bold {FG}"), (cells[1] + " ", SOFT),
                    (cells[2] + " ", FAINT), (cells[3], MUTED),
                ),
                "palette-row",
            )
        return (
            Text.assemble(
                (cells[0] + " ", UNFOCUSED), (cells[1] + " ", UNFOCUSED),
                (cells[2] + " ", WARN), (cells[3], MUTED),
            ),
            "palette-row",
        )

    def refresh_results(self) -> None:
        groups, self.flat_items = self.get_groups()
        self.cursor = max(0, min(len(self.flat_items) - 1, self.cursor)) if self.flat_items else 0

        hint = "↵ loads args · ⇧↵ runs" if self.query_text else "tasks + harness · > for commands only"
        self.query_one("#palette-hint", Static).update(Text(hint, style=MUTED))

        box = self.query_one("#palette-results", Vertical)
        box.remove_children()
        self.row_widgets = []
        pos = 0
        for g in groups:
            box.mount(Static(g["title"].upper(), classes="palette-group-header"))
            for item in g["items"]:
                text, cls = self._render_item(item, pos == self.cursor)
                widget = PaletteRowItem(pos, text, classes=cls)
                self.row_widgets.append(widget)
                box.mount(widget)
                pos += 1

    def _repaint(self, old: int, new: int) -> None:
        if len(self.row_widgets) != len(self.flat_items):
            self.refresh_results()
            return
        for idx, on in ((old, False), (new, True)):
            if 0 <= idx < len(self.row_widgets):
                text, cls = self._render_item(self.flat_items[idx], on)
                self.row_widgets[idx].update(text)
                self.row_widgets[idx].set_classes(cls)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-input":
            self.query_text = event.value
            self.cursor = 0
            self.refresh_results()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_load_selected()

    def action_cursor_up(self) -> None:
        if self.flat_items and self.cursor > 0:
            old, self.cursor = self.cursor, self.cursor - 1
            self._repaint(old, self.cursor)

    def action_cursor_down(self) -> None:
        if self.flat_items and self.cursor < len(self.flat_items) - 1:
            old, self.cursor = self.cursor, self.cursor + 1
            self._repaint(old, self.cursor)

    def _dismiss_with(self, run: bool) -> None:
        if not (0 <= self.cursor < len(self.flat_items)):
            self.dismiss(None)
            return
        item = self.flat_items[self.cursor]
        if item["kind"] == "meta":
            self.dismiss(("meta", item["meta"]["action"]))
        elif item["kind"] == "chain":
            self.dismiss(("chain", item["chain"]["id"], run))
        else:
            self.dismiss(("task", item["tool"]["id"], item["preset"]["id"], run))

    def action_load_selected(self) -> None:
        self._dismiss_with(False)

    def action_run_selected(self) -> None:
        self._dismiss_with(True)

    def pick_index(self, index: int, run: bool = False) -> None:
        self.cursor = index
        self._dismiss_with(run)

    def action_cancel(self) -> None:
        self.dismiss(None)
