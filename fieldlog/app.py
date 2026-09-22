"""fieldlog main Textual app: composition, lifecycle and key bindings.

Two columns: RECIPES over VARIANTS on the left, RESULTS on the right, with a
full-width ARGS band beneath and an optional hotkey bar under that. Scope is
two independent bindings ($TARGET / $HOST); artifacts live in a per-run
directory captured onto the job at spawn.

Each pane's behaviour lives in its own mixin, so this file holds the widget
tree and the wiring rather than all of them at once:

    tui/catalog.py    CatalogMixin       lookups, blocked verdicts, reload, manager
    tui/tree.py       RecipeTreeMixin    the flattened RECIPES list and its cursor
    tui/variants.py   VariantsPaneMixin  the VARIANTS pane
    tui/args.py       ArgsBandMixin      substitution, token view, raw editor
    tui/jobs.py       JobsMixin          tabs, status band, stdin bar, the workers
    tui/layout.py     LayoutMixin        split / stacked, pane focus, hotkey bar

They are mixins rather than widgets on purpose: every one of them reads and
writes the app's own state and queries its widget tree, so `self` has to stay
the app. The split buys navigability, not isolation — see the review notes on
a repaint model, which is what isolation would actually need.

TargetModal and the interface helpers stay here: the interface tests reach them
through this module (`monkeypatch.setattr(app_mod, "get_interface_ip", ...)`),
which only works while the code that calls them resolves them here too.
"""

from __future__ import annotations

import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches, WrongType
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    Input,
    Label,
    RichLog,
    Static,
    TextArea,
)

from fieldlog import __version__
from fieldlog import recipes as recipes_mod
from fieldlog.recipes import (
    Catalog,
    RECIPES_PATH,
    display_path,
    is_tool_installed,
    load_catalog,
)
from fieldlog.state import (
    DEFAULT_INTERFACE,
    LOOPBACK_NAMES,
    ActiveJob,
    TargetSession,
    get_interface_ip,
    load_pinned_recent,
    save_last_scope,
)
from fieldlog.transcript import TRANSCRIPT_FILE, append_transcript
from fieldlog.tui.helpers import (
    parse_iface_field,
    truncate_right,
)
from fieldlog.tui.args import ArgsBandMixin
from fieldlog.tui.catalog import CatalogMixin
from fieldlog.tui.jobs import JobsMixin
from fieldlog.tui.layout import LayoutMixin
from fieldlog.tui.tree import RecipeTreeMixin
from fieldlog.tui.variants import VariantsPaneMixin
from fieldlog.tui.models import TabDescriptor, TreeRow
from fieldlog.tui.modals import (
    HelpModal,
    PaletteModal,
    QuitConfirm,
)
from fieldlog.tui.theme import (
    ACCENT,
    DIM,
    ERR,
    FAINT,
    FG,
    HOTKEYS,
    MUTED,
    SOFT,
    WARN,
)
from fieldlog.tui.widgets import (
    ArgsTextArea,
    StdinInput,
)

VERSION = __version__

# ---- Helpers ---------------------------------------------------------------


def list_box_interfaces() -> List[Tuple[str, str]]:
    """(iface_name, ipv4_address) for every interface on the box."""
    ifaces: List[Tuple[str, str]] = []
    try:
        for _idx, name in socket.if_nameindex():
            ifaces.append((name, get_interface_ip(name)))
    except Exception:
        pass
    if not ifaces:
        ifaces = [(DEFAULT_INTERFACE, "")]

    def sort_key(item: Tuple[str, str]) -> Tuple[int, str]:
        name, ip = item
        if name in LOOPBACK_NAMES:
            return (3, name)
        return (1, name) if ip else (2, name)

    ifaces.sort(key=sort_key)
    return ifaces


def default_interface() -> str:
    """The interface to start on when none is named: `DEFAULT_INTERFACE` if it has an
    IPv4 address, else the first other interface that has one (never loopback), else
    `DEFAULT_INTERFACE`."""
    fallback = TargetSession.interface
    if get_interface_ip(fallback):
        return fallback
    return next(
        (name for name, ip in list_box_interfaces() if name not in LOOPBACK_NAMES and ip),
        fallback,
    )


# ---- Modals ----------------------------------------------------------------

class TargetModal(ModalScreen[bool]):
    """[T] Target scope, interface, resolver sync and log destination.

    Recipe metadata lives in the recipe manager ([M]) — nothing about the
    catalog appears here.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, session: TargetSession) -> None:
        super().__init__()
        self.session = session
        box_ifaces = list_box_interfaces()
        known = {name for name, _ in box_ifaces}
        if session.interface and session.interface not in known:
            box_ifaces.insert(0, (session.interface, get_interface_ip(session.interface)))
        self.ifaces = box_ifaces
        self.iface_name = session.interface

    def compose(self) -> ComposeResult:
        s = self.session
        with Vertical(id="modal-box", classes="-tall"):
            with Horizontal(id="modal-header-row"):
                yield Static("┤ TARGET SCOPE ├", id="modal-title")
                yield Static("modal · esc to dismiss", id="modal-subhint")
            with VerticalScroll(id="modal-scroll"):
                yield Label("target · address or subnet → $TARGET")
                yield Input(value=s.target, placeholder="192.168.1.0/24", id="in-target")

                yield Label("dns name · optional → $HOST")
                yield Input(
                    value=s.hostname,
                    placeholder="test.com · needed by dig / host / curl variants",
                    id="in-hostname",
                )
                yield Label(self._form_note(s.hostname), id="target-detected-note")

                yield Label("source interface")
                with Vertical(id="iface-list"):
                    # Numbered, not named: a Textual id has to be an identifier
                    # and an interface name need not be one — a vlan child is
                    # `eth0.100`, an alias `eth0:1`, either of which would raise
                    # BadIdentifier and take the scope modal down on open.
                    for index, (name, ip) in enumerate(self.ifaces):
                        yield Static(
                            self._iface_text(name, ip),
                            classes="iface-row",
                            id=f"iface-row-{index}",
                            markup=False,
                        )
                yield Input(
                    value=f"{s.interface} / {s.lhost}" if s.lhost else s.interface,
                    placeholder=f"or type an interface · {DEFAULT_INTERFACE} / 192.168.1.50",
                    id="in-iface",
                )

                yield Label("log destination")
                yield Input(value=s.artifact_root, placeholder="default: targets/<name>/raw", id="in-log-dest")
                yield Label(self._root_preview(s.artifact_root, s.target, s.hostname), id="log-dest-preview")
                yield Label(self._outdir_preview(s.artifact_root, s.target, s.hostname), id="outdir-preview")

            with Horizontal(id="modal-buttons"):
                yield Button(r"\[Esc] Cancel", id="btn-cancel")
                yield Button(r"\[Enter] Save & Apply", id="btn-save", variant="success")

    # -- previews & notes, all reflecting the *pending* form values -----------
    def _form_note(self, dns_name: str) -> str:
        return "" if (dns_name or "").strip() else "no dns name · variants that need one stay non-executable"

    def _log_dir(self, root: str, target: str, host: str) -> str:
        pending = TargetSession(target=target, hostname=host, workspace_dir=self.session.workspace_dir)
        return pending.log_dir(root)

    def _root_preview(self, root: str, target: str, host: str) -> str:
        return "stdout logs land in " + self._log_dir(root, target, host)

    def _outdir_preview(self, root: str, target: str, host: str) -> str:
        base = self._log_dir(root, target, host)
        return f"$OUTDIR → {base}<stamp>_<run>/ · per-run, so repeat scans stay diffable"

    def _iface_text(self, name: str, ip: str) -> Text:
        on = (self.iface_name == name)
        return Text.assemble(
            ("● " if on else "○ ", ACCENT if on else MUTED),
            (f"{truncate_right(name, 14):<15}", f"bold {FG}" if on else SOFT),
            (ip or "no IPv4", DIM),
        )

    def _iface_at(self, row_id: str) -> Optional[str]:
        """The interface a numbered row stands for, or None if the row is stale."""
        try:
            return self.ifaces[int(row_id[len("iface-row-"):])][0]
        except (ValueError, IndexError):
            return None

    def _repaint_ifaces(self) -> None:
        for index, (name, ip) in enumerate(self.ifaces):
            try:
                self.query_one(f"#iface-row-{index}", Static).update(self._iface_text(name, ip))
            except Exception:
                pass

    def _refresh_previews(self) -> None:
        try:
            root = self.query_one("#in-log-dest", Input).value
            target = self.query_one("#in-target", Input).value
            host = self.query_one("#in-hostname", Input).value
            self.query_one("#log-dest-preview", Label).update(self._root_preview(root, target, host))
            self.query_one("#outdir-preview", Label).update(self._outdir_preview(root, target, host))
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "in-hostname":
            try:
                self.query_one("#target-detected-note", Label).update(self._form_note(event.value))
            except Exception:
                pass
            self._refresh_previews()
        elif event.input.id in ("in-log-dest", "in-target"):
            self._refresh_previews()
        elif event.input.id == "in-iface":
            self.iface_name = event.value.split("/")[0].strip() or self.iface_name
            self._repaint_ifaces()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save()
        else:
            self.action_cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_save()

    def on_click(self, event) -> None:
        target = getattr(event, "widget", None) or getattr(event, "target", None)
        target_id = getattr(target, "id", "") or ""
        if target_id.startswith("iface-row-"):
            name = self._iface_at(target_id)
            if name is None:
                return
            self.iface_name = name
            # The row already shows its address; `name / ip` here would save it as a set one.
            self.query_one("#in-iface", Input).value = name
            self._repaint_ifaces()

    def action_save(self) -> None:
        new_target = self.query_one("#in-target", Input).value.strip()
        new_host = self.query_one("#in-hostname", Input).value.strip()
        old_slug = self.session.slug

        running = sum(1 for j in self.app.jobs.values() if j.running)
        if running and (new_target != self.session.target or new_host != self.session.hostname):
            self.app.write_system_log(
                f"[scope] Cannot change target while {running} scan(s) are actively running. "
                "Stop running jobs first.",
                style=ERR,
            )
            self.dismiss(False)
            return

        # Two independent fields. Neither is ever derived from the other here.
        self.session.target = new_target
        self.session.hostname = new_host

        if self.session.slug != old_slug:
            self.app.write_system_log(
                f"[scope] workspace now targets/{self.session.slug} "
                f"(prior evidence remains in targets/{old_slug})"
            )
            self.app.resume_session()

        # Empty means the default: the target workspace's raw/ dir.
        self.session.artifact_root = self.query_one("#in-log-dest", Input).value.strip().rstrip("/")

        name, addr = parse_iface_field(self.query_one("#in-iface", Input).value, self.iface_name)
        if name:
            self.session.interface = name
            # Only a typed `iface / address` sets one; otherwise $LHOST follows the interface.
            self.session.lhost = addr
        # Cache it now rather than only at unmount: a scope the operator typed
        # here should survive a crash or a killed terminal, not just a clean quit.
        save_last_scope(self.session)
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---- App -------------------------------------------------------------------
class FieldlogApp(
    # No two mixins define the same name, so this order carries no meaning
    # beyond reading order.
    CatalogMixin,
    RecipeTreeMixin,
    VariantsPaneMixin,
    ArgsBandMixin,
    JobsMixin,
    LayoutMixin,
    App,
):
    DOUBLE_TAP_QUIT_TIMEOUT: float = 2.0
    CSS_PATH = "app.tcss"
    # Nothing is focused at rest: the filter is reached with `/`, so every
    # single-letter hotkey stays live until the operator asks to type.
    AUTO_FOCUS = None
    BINDINGS = [
        ("slash", "focus_filter", "Filter"),
        ("exclamation_mark", "toggle_hide_missing", "Runnable only"),
        ("up", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("j", "cursor_down", "Down"),
        Binding("tab", "swap_pane", "Swap pane", priority=True),
        ("enter", "activate", "Select / run"),
        ("comma", "prev_variant", "Previous variant"),
        ("full_stop", "next_variant", "Next variant"),
        ("t", "target_scope", "Scope & Logs"),
        ("m", "recipe_manager", "Recipe manager"),
        ("p", "toggle_pin", "Pin"),
        ("e", "toggle_args_mode", "Edit Args"),
        ("r", "reset_args", "Reset Args"),
        ("R", "reload_recipes", "Reload Recipes"),
        ("shift+r", "reload_recipes", "Reload Recipes"),
        ("y", "copy_tail", "Copy tail -f"),
        ("l", "toggle_layout", "Layout"),
        ("h", "toggle_hotkey_bar", "Show / hide keys"),
        ("left_square_bracket", "prev_tab", "Prev Tab"),
        ("right_square_bracket", "next_tab", "Next Tab"),
        # Ctrl+W, as in every tabbed thing, and not a bare `w`: a single
        # letter that closes something is the browser convention broken, and
        # `w` sits next to the `W` that closes every finished tab at once.
        # Both `Input` and `TextArea` bind ctrl+w to delete-word-left and are
        # asked before the app, so typing a filter or editing args still
        # deletes a word rather than shutting a tab underneath the operator.
        ("ctrl+w", "close_active_tab", "Close Tab"),
        ("W", "close_finished_tabs", "Close Finished"),
        ("shift+w", "close_finished_tabs", "Close Finished"),
        ("ctrl+p", "command_palette", "Quick Run"),
        ("ctrl+c", "sigint", "Interrupt"),
        ("ctrl+shift+c", "copy_log", "Copy Log"),
        ("question_mark", "help", "Key Bindings"),
        *[(str(n), f"variant_{n}", f"Variant {n}") for n in range(1, 10)],
        ("0", "variant_10", "Variant 10"),
        ("escape", "escape", "Back"),
        ("q", "quit_tap", "Quit"),
    ]

    def __init__(self, session: Optional[TargetSession] = None) -> None:
        super().__init__()
        self._last_quit_press: float = float("-inf")
        self.catalog: Catalog = load_catalog()
        self._recipes: List[dict] = self.catalog.tools
        self.session = session or TargetSession(interface="")
        # ponytail: a named interface (flag or remembered) is kept even with no IP yet
        # (tun0 before the VPN is up); only an unset one is chosen for its address.
        # session.lhost is never filled in from the interface: effective_lhost() reads
        # it when a job starts, so a saved scope cannot pin yesterday's address.
        if not self.session.interface:
            self.session.interface = default_interface()

        self.jobs: Dict[str, ActiveJob] = {}
        self.tabs: List[TabDescriptor] = [TabDescriptor("system", "System", "system", "system")]
        self.active_tab_id: str = "system"
        self.system_log_lines: List[str] = []
        self._transcript_ok: bool = True

        self.selected_tool_id: str = "ping"
        self.selected_preset_id: str = "sweep"
        # Set only while a chain row is selected; every tool/preset path below
        # behaves exactly as it did when this is None.
        self.selected_chain_id: Optional[str] = None
        # Which of that chain's steps the pane is reading. A reading glass and
        # nothing more: every step runs, in the order the yaml sets.
        self.chain_step: int = 0
        self.flag_edits: Dict[str, str] = {}
        self.args_raw_mode: bool = False

        self._reloading: bool = False
        self.reload_revision: int = 1
        self.last_reload_time: str = "boot"
        self.added_variants: List[str] = []

        self.layout_override: Optional[str] = None
        self._current_layout: str = "split"
        self.stacked_pane: str = "recipes"      # which accordion pane is expanded
        self.kbd_pane: str = "recipes"          # keyboard focus: recipes | variants
        self.show_hotkey_bar: bool = False      # hidden on first paint; H toggles

        valid_keys = {
            f"{t['id']}/{p['id']}"
            for t in self.catalog.tools
            for p in t.get("presets", [])
        } | {f"chain/{c['id']}" for c in self.catalog.chains}
        self.pinned, self.recent = load_pinned_recent(self.session.workspace_dir, valid_keys=valid_keys)
        self.filter_text: str = ""
        self.hide_missing: bool = True          # runnable-only is the default
        self._stdin_replies: List[str] = []
        self.cursor: int = 0
        self._rows: List[TreeRow] = []
        # Keys self.jobs and the job tabs. Never reset: run numbers restart per
        # target, so a finished #01 tab can sit beside a new target's #01.
        self._job_seq = 0
        self._start = time.time()
        self._stdin_dismissed: Dict[str, Optional[float]] = {}


    # ---- Logging ---------------------------------------------------------

    # The harness transcript is kept for [Ctrl+Shift+C]; the RichLog does its own
    # scrollback. A session left open for a day should not grow a list forever.
    SYSTEM_LOG_MAX = 5000

    def write_system_log(self, text: str, style: str = DIM) -> None:
        self.system_log_lines.append(text)
        if len(self.system_log_lines) > self.SYSTEM_LOG_MAX:
            del self.system_log_lines[: len(self.system_log_lines) - self.SYSTEM_LOG_MAX]
        self._persist_transcript(text)
        try:
            log = self.query_one("#log-system", RichLog)
            width = log.scrollable_content_region.width or self._log_width()
            log.write(Text(text, style=style), width=width)
        except Exception:
            pass
        if self.active_tab_id == "system":
            try:
                self._refresh_status_band()
            except Exception:
                pass

    def _persist_transcript(self, text: str) -> None:
        """Append one System line to the workspace transcript, or give up for good.

        The workspace is fixed for the app's life (the [T] modal edits the scope,
        never where it is kept), so every write goes to the same file. An
        unwritable workspace is said once and then left alone: the flag is
        cleared *before* the warning is written, so the nested write_system_log
        skips the file here and cannot recurse.
        """
        if not self._transcript_ok:
            return
        try:
            append_transcript(self.session.workspace_dir, text)
        except OSError as exc:
            self._transcript_ok = False
            path = Path(self.session.workspace_dir) / TRANSCRIPT_FILE
            self.write_system_log(
                f"[transcript] not written · {display_path(path)} · {exc.strerror or exc}", style=WARN
            )

    @contextmanager
    def _repaint(self, what: str):
        """Keep one pane's repaint from taking the harness down — but not quietly.

        A widget that is not there is ordinary: a pane not mounted yet, a log
        removed with its tab, a query run before the first layout. Anything else
        is a bug, and a repaint that silently does nothing is the one that costs
        an operator a scan — the ARGS band would go on showing the previous
        recipe's flags with Enter still armed.
        """
        try:
            yield
        except (NoMatches, WrongType):
            pass
        except Exception as exc:  # noqa: BLE001 — degrade the pane, keep the app
            self.write_system_log(f"[ui] {what} failed · {type(exc).__name__}: {exc}", style=ERR)

    def _get_clock_str(self) -> str:
        elapsed = int(time.time() - self._start)
        return f"{elapsed // 3600:02d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}"

    def resume_session(self) -> None:
        from fieldlog.archive import load_target_history, next_run_number
        history = load_target_history(self.session.target_dir)
        if history.total_runs > 0 or history.max_run_id > 0:
            count_info = (
                f"{history.total_runs} prior runs indexed" if history.total_runs > 0
                else "prior runs detected on disk"
            )
            self.write_system_log(
                f"[session] Resumed targets/{self.session.slug} · {count_info} "
                f"(next: #{next_run_number(self.session.target_dir):02d})"
            )
        if self.is_running:
            self._refresh_variants()

    # ---- Composition -----------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(id="cell-target", classes="cell")
            yield Static(id="cell-iface", classes="cell")
            yield Static("[L] split", id="cell-layout", markup=False)
            yield Static(id="cell-jobs", classes="cell")

        with Horizontal(id="body"):
            with Vertical(id="left-column"):
                with Vertical(id="recipes-pane"):
                    with Horizontal(id="recipes-header"):
                        yield Static("RECIPES", id="recipes-title")
                        yield Static("/", id="filter-prompt")
                        yield Input(placeholder="filter", id="filter-input")
                        yield Static("[!] runnable", id="avail-toggle", markup=False)
                        yield Static("[M]", id="mgr-chip", markup=False)
                    with VerticalScroll(id="recipe-tree", can_focus=False):
                        pass

                with Vertical(id="variants-pane"):
                    with Horizontal(id="variants-header"):
                        yield Static("VARIANTS", id="variants-title")
                        yield Static("", id="variants-crumb")
                        yield Static("[Enter] Run", id="btn-run", markup=False)
                        yield Static("[P] Pin", id="btn-pin", markup=False)
                    with Horizontal(id="variants-title-row"):
                        yield Static("", id="variants-bin")
                        yield Static("", id="variants-variant")
                    with VerticalScroll(id="variants-scroll", can_focus=False):
                        yield Vertical(id="variant-list")

            with Vertical(id="main"):
                with Horizontal(id="tab-strip"):
                    with Horizontal(id="tabs-scroll"):
                        with Horizontal(id="tabs-list"):
                            pass
                    yield Static("", id="btn-close-finished", classes="hidden")
                with Horizontal(id="status-band"):
                    with Horizontal(id="status-items"):
                        pass
                    yield Static("", id="status-spacer")
                    yield Static("⧉ copy log  [Ctrl+Shift+C]", id="btn-copy-log")
                with Vertical(id="pinned-block"):
                    with Horizontal(id="pinned-cmd-row"):
                        yield Static("$", id="pinned-cmd-sigil")
                        with VerticalScroll(id="pinned-cmd-scroll"):
                            yield Static("", id="pinned-cmd")
                    with Horizontal(id="pinned-art-row"):
                        yield Static("↳", id="pinned-art-sigil")
                        yield Static("", id="pinned-art", markup=False)
                with ContentSwitcher(id="tab-content", initial="log-system"):
                    yield RichLog(id="log-system", wrap=True, markup=True, min_width=20)
                with Vertical(id="stdin-bar", classes="hidden"):
                    with Horizontal(id="stdin-top"):
                        yield Static("⌨ STDIN", id="stdin-title")
                        yield Static("", id="stdin-prompt-text")
                        yield Static("", id="stdin-waiting")
                    with Horizontal(id="stdin-entry"):
                        yield Static("›", id="stdin-sigil")
                        yield StdinInput(placeholder="type a reply, enter sends", id="stdin-input")
                        yield Horizontal(id="stdin-chips")
                    yield Static(
                        "enter sends · Ctrl+C sends SIGINT instead · "
                        "harness hotkeys are suspended while stdin is focused",
                        id="stdin-hint",
                    )

        with Vertical(id="args-band"):
            with Horizontal(id="args-header-row"):
                yield Static("ARGS", id="args-header-title")
                yield Static("", id="args-header-spacer")
                yield Static("[R] reset", id="args-btn-reset", markup=False)
                yield Static("[E] edit raw", id="args-btn-mode", markup=False)
            with Horizontal(id="args-body-row"):
                yield Static("", id="args-bin-label")
                with VerticalScroll(id="args-tokens-scroll"):
                    with Vertical(id="args-tokens-wrap"):
                        pass
                with Vertical(id="args-raw-wrap", classes="hidden"):
                    yield ArgsTextArea(id="args-raw-area")
                    with Horizontal(id="args-raw-exit"):
                        yield Static("✓ done · esc", id="btn-args-done", markup=False)
                        yield Static(
                            "template: $TARGET $HOST $IFACE $LHOST $OUTDIR are filled in at launch · "
                            "newlines allowed · esc or ctrl+j applies · [R] resets to the variant default",
                            id="args-raw-hint",
                        )
            with Horizontal(id="args-foot"):
                yield Static("[H] keys", id="btn-bar-toggle", markup=False)

        with Horizontal(id="footer-keys", classes="hidden"):
            for idx, (key, label, primary) in enumerate(HOTKEYS):
                pill = Static(key, id=f"footer-key-{idx}", classes="key-pill key-pill-primary" if primary else "key-pill")
                pill.tooltip = label
                yield pill
                yield Static(label, classes="key-label", id=f"footer-label-{idx}")

    # ---- Lifecycle -------------------------------------------------------
    def on_mount(self) -> None:
        self.resume_session()
        self._refresh_header()
        self.set_interval(1.0, self._tick)
        self._rebuild_tree()
        self._refresh_variants()
        self._refresh_args_band()
        self._refresh_tab_strip()
        self._refresh_results_chrome()
        self._update_responsive_layout()
        # After the first layout: RichLog wraps to whatever width it had when a
        # line was written and never re-wraps, so a pre-layout boot transcript
        # is pinned to a guessed width for the life of the session.
        self.call_after_refresh(self._log_boot_transcript)

    def on_unmount(self) -> None:
        save_last_scope(self.session)

    def _log_boot_transcript(self) -> None:
        self.write_system_log(f"[fieldlog] v{VERSION} · harness up")
        self._log_catalog_sources()
        self.write_system_log(
            f"[recipes] {len(self.recipes)} tools · {self._total_variants()} variants indexed"
        )
        installed = sum(1 for t in self.recipes if is_tool_installed(t.get("bin", "")))
        missing = len(self.recipes) - installed
        self.write_system_log(
            f"[preflight] {installed} available · {missing} missing",
            style=WARN if missing else ACCENT,
        )
        self.write_system_log(
            f"[scope] target set {self.session.target} ({self.session.target_kind})", style=ACCENT
        )

    def _log_catalog_sources(self) -> None:
        """Name what was read, not what changed — built from files scanned."""
        files = self.catalog.files
        suffix = (
            f"{len(files)} drop-in ({', '.join(files)})" if files else "no drop-ins"
        )
        self.write_system_log(f"[recipes] base {display_path(RECIPES_PATH)} · {suffix}")
        off = self.catalog.inactive_themes
        if off:
            # Otherwise the operator is left wondering where a tool went, and
            # the one file that explains it is not one the TUI ever mentions.
            self.write_system_log(
                f"[recipes] {len(off)} theme{'' if len(off) == 1 else 's'} off "
                f"({', '.join(off)}) · {display_path(recipes_mod.THEMES_PATH)}",
                style=WARN,
            )
        for err in self.catalog.errors:
            self.write_system_log(f"[recipes] {err}", style=WARN)
        for ov in self.catalog.overrides:
            self.write_system_log(f"[recipes] {ov}", style=WARN)

    def _tick(self) -> None:
        self._refresh_header()
        job = self.active_job()
        if job and job.running:
            self._refresh_status_band()
            if job.awaiting:
                self._refresh_stdin_bar()

    def _refresh_header(self) -> None:
        try:
            target_cell = self.query_one("#cell-target", Static)
        except Exception:
            return
        s = self.session
        dns = (s.hostname or "").strip()
        suffix = f" · {dns}" if dns and dns != s.target else ""
        target_cell.update(
            Text.assemble(("TARGET SCOPE\n", FAINT), (s.target, f"bold {ACCENT}"), (suffix, FAINT))
        )
        try:
            self.query_one("#cell-iface", Static).update(
                Text.assemble(
                    ("INTERFACE\n", FAINT),
                    (s.interface, FG),
                    (f" ({s.effective_lhost() or '—'})", DIM),
                )
            )
            active = sum(1 for j in self.jobs.values() if j.running)
            self.query_one("#cell-jobs", Static).update(
                Text.assemble(
                    ("JOBS\n", FAINT),
                    (f"● {active} active", WARN),
                    ("   " + self._get_clock_str(), DIM),
                )
            )
        except Exception:
            pass


    # ---- Events ----------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-input":
            self.filter_text = event.value
            # No cursor reset: a recipe that survives the filter keeps the
            # cursor, and _rebuild_tree repaints the panes from wherever it
            # lands when the row it was on is filtered away.
            self._rebuild_tree()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter-input":
            self.set_focus(None)
            self.commit_filter()
        elif event.input.id == "stdin-input":
            self.send_stdin_reply()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self.selected_chain_id:
            return
        if event.text_area.id == "args-raw-area" and self.args_raw_mode:
            tool, preset, key, _ = self.current_flags()
            if tool:
                self.flag_edits[key] = event.text_area.text
                self._refresh_args_band()


    def on_click(self, event) -> None:
        target = getattr(event, "widget", None) or getattr(event, "target", None)
        target_id = getattr(target, "id", "") or ""
        classes = getattr(target, "classes", set())

        if target_id == "btn-run":
            self.action_run_task()
        elif target_id == "btn-pin":
            self.action_toggle_pin()
        elif target_id == "args-btn-reset":
            self.action_reset_args()
        elif target_id in ("args-btn-mode", "btn-args-done"):
            self.action_toggle_args_mode()
        elif target_id == "btn-bar-toggle":
            self.action_toggle_hotkey_bar()
        elif target_id == "btn-close-finished":
            self.action_close_finished_tabs()
        elif target_id == "btn-copy-log":
            self.action_copy_log()
        elif target_id == "cell-layout":
            self.action_toggle_layout()
        elif target_id in ("cell-target", "cell-iface"):
            self.action_target_scope()
        elif target_id == "mgr-chip":
            self.action_recipe_manager()
        elif target_id == "avail-toggle":
            self.action_toggle_hide_missing()
        elif target_id in ("recipes-header", "recipes-title"):
            self._focus_recipes()
        elif target_id in ("variants-header", "variants-title", "variants-crumb"):
            self._focus_variants()
        elif "arg-pair" in classes or target_id in ("args-tokens-scroll", "args-tokens-wrap"):
            self.action_toggle_args_mode(force_raw=True)
        elif target_id.startswith("footer-key-") or target_id.startswith("footer-label-"):
            idx = int(target_id.rsplit("-", 1)[1])
            [
                self.action_command_palette, self.action_focus_filter, self.action_target_scope,
                self.action_toggle_args_mode, self.action_recipe_manager, self.action_close_active_tab,
                self.action_toggle_layout, self.action_help,
            ][idx]()


    # ---- Actions: escape chain -------------------------------------------
    def action_escape(self) -> None:
        """Stop at the first match. Esc never quits.

        palette → kill/detach → recipe manager → Target Scope → key bindings →
        raw ARGS → stdin field → filter with a query → VARIANTS → no-op.
        (The modal screens above handle their own Esc.)

        The last step used to be the stacked layout only, where VARIANTS is an
        accordion pane that visibly covers RECIPES. In split both panes are on
        screen at once and Esc did nothing at all — but `Tab` having taken the
        arrow keys into VARIANTS is the same one step in, whether or not the
        other pane is still visible, and Esc is the way back out of it.
        """
        if self.args_raw_mode:
            self.action_toggle_args_mode(force_raw=False)
            return
        focused = self.focused
        if isinstance(focused, StdinInput):
            self.dismiss_stdin_focus()
            return
        if isinstance(focused, Input) and getattr(focused, "id", "") == "filter-input":
            if self.filter_text:
                focused.value = ""
                self.filter_text = ""
                self._rebuild_tree()
            self.set_focus(None)
            return
        if self.filter_text:
            self.filter_text = ""
            try:
                self.query_one("#filter-input", Input).value = ""
            except Exception:
                pass
            self._rebuild_tree()
            return
        if self.focus_pane() == "variants":
            self._focus_recipes()
            return


    # ---- Actions: misc ---------------------------------------------------
    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_quit_tap(self) -> None:
        now = time.monotonic()
        if now - self._last_quit_press > self.DOUBLE_TAP_QUIT_TIMEOUT:
            self._last_quit_press = now
            self.notify(
                "Press 'q' again to exit",
                timeout=self.DOUBLE_TAP_QUIT_TIMEOUT,
                severity="warning",
            )
            return
        self._last_quit_press = float("-inf")
        self.clear_notifications()
        self.action_quit()

    def action_quit(self) -> None:  # type: ignore[override]
        running = sum(1 for j in self.jobs.values() if j.running)
        if running and not isinstance(self.screen, QuitConfirm):
            self.push_screen(QuitConfirm(running), lambda ok: self.exit() if ok else None)
        else:
            self.exit()

    def action_target_scope(self) -> None:
        def done(_saved) -> None:
            self._refresh_header()
            self._rebuild_tree()
            self._refresh_variants()
            self._refresh_args_band()
        self.push_screen(TargetModal(self.session), done)

    def action_command_palette(self) -> None:
        def on_pick(result: Optional[Tuple]) -> None:
            if not result:
                return
            if result[0] == "meta":
                fn = getattr(self, f"action_{result[1]}", None)
                if fn:
                    fn()
                return
            if result[0] == "chain":
                _, chain_id, run = result
                self.select_chain(chain_id)
                self._focus_variants()
                if run:
                    self.action_run_task()
                return
            _, tool_id, preset_id, run = result
            self.selected_chain_id = None
            self.selected_tool_id = tool_id
            self.selected_preset_id = preset_id
            self._rebuild_tree()
            self._refresh_variants()
            self._refresh_args_band()
            self._focus_variants()
            if run:
                self.action_run_task()

        self.push_screen(PaletteModal(self), on_pick)


for _n in range(1, 11):
    setattr(
        FieldlogApp, f"action_variant_{_n}",
        (lambda i: lambda self: self._select_variant_by_index(i))(_n - 1),
    )
