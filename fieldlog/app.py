"""fieldlog main Textual app & layout.

Two columns: RECIPES over VARIANTS on the left, RESULTS on the right, with a
full-width ARGS band beneath and an optional hotkey bar under that. Scope is
two independent bindings ($TARGET / $HOST); artifacts live in a per-run
directory captured onto the job at spawn.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import re
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
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
    KNOWN_INSTALL,
    RECIPES_PATH,
    Catalog,
    chain_blocked,
    chain_matches,
    chain_steps,
    display_path,
    is_blocked,
    is_tool_installed,
    load_catalog,
    search,
)
from fieldlog.chain import run_chain
from fieldlog.launch import LaunchPlan, plan_launch
from fieldlog.runner import interrupt_job, kill_job, run_job, send_stdin
from fieldlog.state import (
    ActiveJob,
    TargetSession,
    get_interface_ip,
    load_pinned_recent,
    recipe_slug,
    resolve_flags,
    run_stamp,
    save_last_scope,
    save_pinned_recent,
    template_vars,
)

VERSION = __version__

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

META_COMMANDS = [
    {"id": "mgr", "key": "M", "label": "Open recipe manager", "hint": "sources · counts · availability", "action": "recipe_manager"},
    {"id": "reload", "key": "⇧R", "label": "Reload recipes from yaml", "hint": "keeps sessions", "action": "reload_recipes"},
    {"id": "scope", "key": "T", "label": "Target scope & log destination", "hint": "", "action": "target_scope"},
    {"id": "copypath", "key": "Y", "label": "Copy recipes yaml path", "hint": str(RECIPES_PATH), "action": "copy_catalog_path"},
    {"id": "copytail", "key": "Y", "label": "Copy tail -f for active artifact", "hint": "read output in a pager", "action": "copy_tail"},
    {"id": "layout", "key": "L", "label": "Toggle split / stacked layout", "hint": "stacked ≤ 120 cols", "action": "toggle_layout"},
    {"id": "runnable", "key": "!", "label": "Toggle runnable-only filter", "hint": "", "action": "toggle_hide_missing"},
    {"id": "closefin", "key": "⇧W", "label": "Close finished job tabs", "hint": "", "action": "close_finished_tabs"},
    {"id": "copylog", "key": "Ctrl+Shift+C", "label": "Copy active log to clipboard", "hint": "whole buffer", "action": "copy_log"},
    {"id": "keys", "key": "?", "label": "Key bindings", "hint": "", "action": "help"},
]


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
        ifaces = [("eth0", "")]

    def sort_key(item: Tuple[str, str]) -> Tuple[int, str]:
        name, ip = item
        if name == "lo":
            return (3, name)
        return (1, name) if ip else (2, name)

    ifaces.sort(key=sort_key)
    return ifaces


def tokenize(s: str) -> List[str]:
    """Shell-tokenise an argument string, respecting quoted substrings."""
    out: List[str] = []
    cur = ""
    q: Optional[str] = None
    for ch in str(s):
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in ('"', "'"):
            q = ch
            cur += ch
            continue
        if ch.isspace():
            if cur:
                out.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def arg_groups(s: str) -> dict:
    """Group flags with their associated parameter values."""
    toks = tokenize(s)
    out: List[dict] = []
    i = 0
    while i < len(toks):
        tk = toks[i]
        if len(tk) > 1 and tk[0] in ("-", "+"):
            vals: List[str] = []
            while i + 1 < len(toks) and not re.match(r"^[-+]\S", toks[i + 1]):
                vals.append(toks[i + 1])
                i += 1
            out.append({"flag": tk, "value": " ".join(vals)})
        else:
            out.append({"flag": "", "value": tk})
        i += 1
    return {"groups": out, "count": len(toks)}


def truncate_right(text: str, width: int) -> str:
    """Explicit right-side truncation. Never bidi — a reordered path is a wrong
    value for something meant to be copied."""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def copy_text_to_clipboard(text: str, app: Optional[App] = None) -> bool:
    """Copy via the Textual clipboard inside the TUI, or stdout OSC 52 outside."""
    copied = False
    if app is not None and hasattr(app, "copy_to_clipboard"):
        try:
            app.copy_to_clipboard(text)
            return True
        except Exception:
            pass
    else:
        try:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            sys.stdout.write(f"\033]52;c;{encoded}\007")
            sys.stdout.flush()
            copied = True
        except Exception:
            pass
    try:
        import pyperclip  # type: ignore[import-not-found]
        pyperclip.copy(text)
        copied = True
    except Exception:
        pass
    return copied


@dataclass
class TabDescriptor:
    id: str                 # "system", "job-01", …
    label: str              # "[System]", "ping/sweep #01"
    status: str             # "system", "active", "done", "failed"
    tool_id: str
    job_id: Optional[str] = None
    cmd: str = ""
    artifact: str = ""      # captured at spawn; never re-derived from live state


@dataclass
class TreeRow:
    """One line of the flattened RECIPES list."""

    kind: str               # "header" | "tool" | "entry" | "chain"
    label: str = ""
    bin: str = ""
    meta: str = ""
    tool_id: str = ""
    preset_id: str = ""
    blocked: bool = False


# ---- Widgets ---------------------------------------------------------------
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

    def _icon(self) -> Tuple[str, str]:
        key = "await" if self.awaiting else self.tab.status
        return self.ICONS.get(key, ("▪", FAINT))

    def compose(self) -> ComposeResult:
        icon, icon_color = self._icon()
        yield Static(Text(icon, style=icon_color), classes="tab-icon")
        yield Static(Text(self.tab.label, style=FG if self.is_active else DIM), classes="tab-label")
        if self.tab.id == "system":
            yield Static(Text("·", style="#232c2b"), classes="tab-locked tab-lock")
        else:
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


class StdinInput(Input):
    """The stdin field. Enter sends; Esc blurs without sending."""

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.app.dismiss_stdin_focus()


# ---- Modals ----------------------------------------------------------------
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
            ("1-9,0", "Select variant 1-10"),
            (", / .", "Previous / next variant"),
            ("Tab", "Move focus recipes / variants"),
            ("E", "Edit args (raw / tokens)"),
            ("R", "Reset args to variant"),
            ("P", "Pin / unpin task"),
        ]),
        ("jobs", [
            ("Esc", "Back to recipes (stacked)"),
            ("[ / ]", "Previous / next tab"),
            ("Ctrl+C", "Interrupt running job (SIGINT)"),
            ("Y", "Copy tail -f for active artifact"),
            ("Ctrl+Shift+C", "Copy whole log to clipboard"),
            ("W", "Close active tab · running job asks kill / detach"),
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
                            for k, l in keys:
                                with Horizontal(classes="help-row"):
                                    yield Static(k, classes="help-key")
                                    yield Static(l, classes="help-label")
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
                    for name, ip in self.ifaces:
                        yield Static(
                            self._iface_text(name, ip),
                            classes="iface-row",
                            id=f"iface-{name}",
                            markup=False,
                        )
                yield Input(
                    value=f"{s.interface} / {s.lhost}" if s.lhost else s.interface,
                    placeholder="or type an interface · eth0 / 192.168.1.50",
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
        return f"$OUTDIR → {base}<stamp>/ · per-run, so repeat scans stay diffable"

    def _iface_text(self, name: str, ip: str) -> Text:
        on = (self.iface_name == name)
        return Text.assemble(
            ("● " if on else "○ ", ACCENT if on else MUTED),
            (f"{truncate_right(name, 14):<15}", f"bold {FG}" if on else SOFT),
            (ip or "no IPv4", DIM),
        )

    def _repaint_ifaces(self) -> None:
        for name, ip in self.ifaces:
            try:
                self.query_one(f"#iface-{name}", Static).update(self._iface_text(name, ip))
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
        if target_id.startswith("iface-"):
            name = target_id[len("iface-"):]
            ip = dict(self.ifaces).get(name, "")
            self.iface_name = name
            self.query_one("#in-iface", Input).value = f"{name} / {ip}" if ip else name
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

        iface_raw = self.query_one("#in-iface", Input).value.strip()
        name = iface_raw.split("/")[0].strip() if iface_raw else self.iface_name
        if name:
            self.session.interface = name
            self.session.lhost = get_interface_ip(name) or dict(self.ifaces).get(name, "")
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class RecipeManagerModal(ModalScreen):
    """[M] Recipe manager — stats, definition sources, indexed tools.

    Swallows Y and ⇧R so neither reaches the global map.
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("m", "close", "Close"),
    ]

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
                    for t in app.manager_tools():
                        row = Horizontal(classes="mgr-tool-row", id=f"mgrtool-{t['id']}")
                        row.tooltip = t["state"]
                        with row:
                            yield Static(Text(t["bin"], style=t["bin_style"]), classes="mgr-tool-bin")
                            yield Static(t["cat"], classes="mgr-tool-cat")
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
                self.app.select_tool(node.id[len("mgrtool-"):])
                self.dismiss(None)

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
            yield Label(f"{self.running} {plural} still running — quitting kills them.")
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
                rows = [
                    {"kind": "task", "tool": t, "preset": p, "blocked": b}
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
                        rows.append({"kind": "task", "tool": t, "preset": p, "blocked": app.blocked_flag(t, p)})
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
                f"{('chain · ' + str(len(c.get('steps', []))) + ' steps')[:16]:>16}",
            )
        else:
            t, p = item["tool"], item["preset"]
            t_bin = t.get("bin", t["id"])
            state = "" if ok else ("needs dns name" if is_tool_installed(t_bin) else "not installed")
            cells = (
                f"{t_bin[:10]:<11}",
                f"{p.get('name', p['id'])[:26]:<27}",
                f"{state[:15]:<16}",
                f"{t.get('category', '')[:16]:>16}",
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


# ---- App -------------------------------------------------------------------
class FieldlogApp(App):
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
        ("w", "close_active_tab", "Close Tab"),
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
        self.session = session or TargetSession()
        if not self.session.lhost:
            ip = get_interface_ip(self.session.interface)
            if ip:
                self.session.lhost = ip
            # ponytail: an interface named on the command line is kept even with no IP yet
            # (tun0 before the VPN is up); only the default gets swapped for one that has an IP.
            elif self.session.interface == TargetSession.interface:
                for name, if_ip in list_box_interfaces():
                    if name != "lo" and if_ip:
                        self.session.interface, self.session.lhost = name, if_ip
                        break

        self.jobs: Dict[str, ActiveJob] = {}
        self.tabs: List[TabDescriptor] = [TabDescriptor("system", "[System]", "system", "system")]
        self.active_tab_id: str = "system"
        self.system_log_lines: List[str] = []

        self.selected_tool_id: str = "ping"
        self.selected_preset_id: str = "sweep"
        # Set only while a chain row is selected; every tool/preset path below
        # behaves exactly as it did when this is None.
        self.selected_chain_id: Optional[str] = None
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

    # ---- Catalog ---------------------------------------------------------
    @property
    def recipes(self) -> List[dict]:
        return self._recipes

    def _total_variants(self) -> int:
        return sum(len(t.get("presets", [])) for t in self.recipes)

    def get_tool(self, tool_id: str) -> Optional[dict]:
        return next((t for t in self.recipes if t["id"] == tool_id), None)

    def get_preset(self, tool: dict, preset_id: str) -> dict:
        presets = tool.get("presets", [])
        return next(
            (p for p in presets if p["id"] == preset_id),
            presets[0] if presets else {"id": "default", "name": "default", "flags": ""},
        )

    @property
    def chains(self) -> List[dict]:
        return self.catalog.chains

    def get_chain(self, chain_id: str) -> Optional[dict]:
        return next((c for c in self.chains if c["id"] == chain_id), None)

    def selected_chain(self) -> Optional[dict]:
        return self.get_chain(self.selected_chain_id) if self.selected_chain_id else None

    def is_blocked(self, tool: dict, preset: dict) -> Tuple[bool, str, str]:
        """(blocked, reason, hint). Missing binary and missing dns name are
        distinct reasons and must never be reported as each other."""
        return is_blocked(tool, preset, self.session)

    def blocked_flag(self, tool: dict, preset: dict) -> bool:
        return self.is_blocked(tool, preset)[0]

    def chain_blocked_flag(self, chain: dict) -> bool:
        return chain_blocked(self.catalog, chain, self.session)[0]

    def _chain_row(self, chain: dict) -> TreeRow:
        return TreeRow(
            "chain", label=chain.get("name", chain["id"]), bin=chain["id"],
            meta=f"{len(chain.get('steps', []))} steps",
            blocked=self.chain_blocked_flag(chain),
        )

    # ---- Substitution ----------------------------------------------------
    def resolve_flags(self, flags: str, out_dir: Optional[str] = None) -> str:
        """Straight string replacement of the bindings, every occurrence."""
        return resolve_flags(self.session, flags, out_dir=out_dir if out_dir is not None else self.pending_out_dir())

    def pending_out_dir(self) -> str:
        """$OUTDIR as it would resolve for a job started now (preview only)."""
        return self.session.log_dir() + run_stamp()

    def bound_values(self) -> List[str]:
        """Substituted values that should render amber in the token view."""
        s = self.session
        root = (s.artifact_root or "").strip().rstrip("/")
        return [v for v in (s.target, s.dns_name, s.interface, s.lhost, root) if v]

    # ---- Logging ---------------------------------------------------------
    def _log_width(self) -> Optional[int]:
        """Width a hidden RichLog should wrap to.

        A RichLog inside a ContentSwitcher has a zero-width content region while
        it is not the visible child, so an unwidthed write collapses to
        `min_width`. Borrow the switcher's width instead.
        """
        try:
            return self.query_one("#tab-content", ContentSwitcher).content_size.width or None
        except Exception:
            return None

    def write_system_log(self, text: str, style: str = DIM) -> None:
        self.system_log_lines.append(text)
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

    # ---- Layout ----------------------------------------------------------
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
                    with VerticalScroll(id="recipe-tree"):
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
                    with VerticalScroll(id="variants-scroll"):
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
                yield Static("$ ping", id="args-bin-label")
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
                    (f" ({s.lhost or '—'})", DIM),
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

    # ---- RECIPES tree (flattened) ----------------------------------------
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

    # ---- VARIANTS pane ---------------------------------------------------
    def _refresh_variants(self) -> None:
        try:
            var_list = self.query_one("#variant-list", Vertical)
        except Exception:
            return
        chain = self.selected_chain()
        if chain is not None:
            self._refresh_chain_variants(var_list, chain)
            return
        tool = self.get_tool(self.selected_tool_id)
        if not tool:
            if not self.recipes:
                return
            tool = self.recipes[0]
            self.selected_tool_id = tool["id"]
        preset = self.get_preset(tool, self.selected_preset_id)
        key = f"{tool['id']}/{preset['id']}"
        blocked, _, _ = self.is_blocked(tool, preset)

        try:
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
            except Exception:
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

            btn_run = self.query_one("#btn-run", Static)
            btn_run.update("Not runnable" if blocked else "[Enter] Run")
            btn_run.set_class(blocked, "-disabled")
            self.query_one("#btn-pin", Static).update(
                "[P] Unpin" if key in self.pinned else "[P] Pin"
            )
            self.query_one("#variants-crumb", Static).update(self._variants_crumb(tool, preset))
        except Exception:
            pass

    def _refresh_chain_variants(self, var_list: Vertical, chain: dict) -> None:
        """The steps, numbered and read-only: a chain's order lives in its yaml.
        Plain Statics, not VariantRowWidgets — a click here selects nothing."""
        blocked, _reason, _hint = chain_blocked(self.catalog, chain, self.session)
        key = f"chain/{chain['id']}"
        try:
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

            btn_run = self.query_one("#btn-run", Static)
            btn_run.update("Not runnable" if blocked else "[Enter] Run chain")
            btn_run.set_class(blocked, "-disabled")
            self.query_one("#btn-pin", Static).update(
                "[P] Unpin" if key in self.pinned else "[P] Pin"
            )
            self.query_one("#variants-crumb", Static).update(
                "" if self._current_layout != "stacked" else f"chain · {chain['id']}"
            )
        except Exception:
            pass

    def _variants_crumb(self, tool: dict, preset: dict) -> str:
        if self._current_layout != "stacked":
            return ""
        if self.stacked_pane == "task":
            return "esc · back to recipes"
        return f"{tool.get('bin', tool['id'])} · {preset.get('name', preset['id'])}"

    # ---- ARGS band -------------------------------------------------------
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
        tool, preset, key, template = self.current_flags()
        if not tool:
            return
        # Tokens show the command as it would run; the raw editor holds the template.
        resolved = self.resolve_flags(template)
        is_dirty = key in self.flag_edits

        try:
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
        except Exception:
            pass

    def _refresh_chain_args_band(self, tokens_wrap: Vertical, chain: dict) -> None:
        """`$ chain reach` and its steps as tokens. There is nothing to edit
        here: a step's own args edit still applies when the chain runs it."""
        try:
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
        except Exception:
            pass

    # ---- RESULTS chrome --------------------------------------------------
    def active_tab(self) -> Optional[TabDescriptor]:
        return next((t for t in self.tabs if t.id == self.active_tab_id), None)

    def active_job(self) -> Optional[ActiveJob]:
        tab = self.active_tab()
        return self.jobs.get(tab.job_id) if tab and tab.job_id else None

    def _refresh_results_chrome(self) -> None:
        self._refresh_status_band()
        self._refresh_pinned_block()
        self._refresh_stdin_bar()

    def _status_items(self) -> List[Tuple[str, str, str]]:
        tab = self.active_tab()
        if tab is None or tab.id == "system":
            return [
                ("state", "harness", ACCENT),
                ("recipes", f"rev {self.reload_revision}", SOFT),
                ("lines", str(len(self.system_log_lines)), SOFT),
            ]
        job = self.jobs.get(tab.job_id or "")
        if job is None:
            return [("state", "gone", FAINT)]
        finished = not job.running
        if finished:
            state, state_color = "finished", ACCENT
        elif job.awaiting:
            state, state_color = "awaiting input", WARN
        else:
            state, state_color = "running", WARN
        # EXIT is never fabricated: an unrecorded code is —, never 0.
        if job.exit_code is None:
            exit_val, exit_color = "—", FAINT
        else:
            exit_val = str(job.exit_code)
            exit_color = ACCENT if job.exit_code == 0 else ERR
        return [
            ("state", state, state_color),
            ("exit", exit_val, exit_color),
            ("elapsed", job.elapsed_str(), SOFT),
            ("lines", str(job.lines_count), SOFT),
            ("bytes", f"{job.bytes_count / 1024:.1f} KiB", SOFT),
        ]

    def _refresh_status_band(self) -> None:
        try:
            box = self.query_one("#status-items", Horizontal)
        except Exception:
            return
        items = self._status_items()
        # Must not wrap to a second row: drop items rather than wrap.
        avail = max(20, self.size.width - 26)
        budget, keep = 0, []
        for label, value, color in items:
            cost = max(len(label), len(value)) + 4
            if budget + cost > avail:
                break
            budget += cost
            keep.append((label, value, color))
        box.remove_children()
        cells = []
        for label, value, color in keep:
            cells.append(Static(
                Text.assemble((label.upper() + "\n", FAINT), (value, color)),
                classes="status-item",
            ))
        if cells:
            box.mount_all(cells)

    def _refresh_pinned_block(self) -> None:
        try:
            cmd_widget = self.query_one("#pinned-cmd", Static)
            art_widget = self.query_one("#pinned-art", Static)
        except Exception:
            return
        tab = self.active_tab()
        if tab is None or tab.id == "system":
            cmd_widget.update(Text(f"fieldlog {VERSION} · harness event log", style=FG))
            art_widget.update(Text("harness log · not written to disk", style=DIM))
            art_widget.tooltip = "harness log · not written to disk"
            return
        cmd_widget.update(Text(tab.cmd or "—", style=FG))
        width = max(20, self.size.width - 30)
        art_widget.update(Text(truncate_right(tab.artifact, width), style=DIM))
        art_widget.tooltip = tab.artifact

    def _refresh_stdin_bar(self) -> None:
        try:
            bar = self.query_one("#stdin-bar", Vertical)
        except Exception:
            return
        job = self.active_job()
        tab = self.active_tab()
        show = bool(job and job.awaiting and tab and tab.id != "system")
        if not show:
            bar.add_class("hidden")
            return
        bar.remove_class("hidden")
        prompt = job.await_prompt or ""
        self.query_one("#stdin-prompt-text", Static).update(Text(prompt, style=FG))
        waited = max(1, int(time.time() - (job.await_since or time.time())))
        self.query_one("#stdin-waiting", Static).update(Text(f"blocked {waited}s", style=DIM))

        replies = self._quick_replies(prompt)
        if replies != getattr(self, "_stdin_replies", None):
            chips = self.query_one("#stdin-chips", Horizontal)
            chips.remove_children()
            if replies:
                chips.mount_all([StdinChip(r) for r in replies])
        self._stdin_replies = replies

        # The one place automatic focus-stealing is correct: the process waits.
        if self._stdin_dismissed.get(job.id) != job.await_since:
            field = self.query_one("#stdin-input", StdinInput)
            if self.focused is not field:
                field.focus()

    @staticmethod
    def _quick_replies(prompt: str) -> List[str]:
        """Chips from an unambiguous bracketed hint (`[y/N]`), else nothing."""
        m = re.search(r"\[([A-Za-z](?:/[A-Za-z])+)\]\s*$", prompt.strip())
        if not m:
            return []
        parts = m.group(1).split("/")
        return parts if 2 <= len(parts) <= 4 else []

    def dismiss_stdin_focus(self) -> None:
        """Esc in the stdin field: blur, keys return to the harness, job stays blocked."""
        job = self.active_job()
        if job:
            self._stdin_dismissed[job.id] = job.await_since
        self.set_focus(None)

    def send_stdin_reply(self, text: Optional[str] = None) -> None:
        job = self.active_job()
        if not job or not job.awaiting:
            return
        field = self.query_one("#stdin-input", StdinInput)
        reply = field.value if text is None else text
        prompt = job.await_prompt or ""
        # An empty line is a legitimate reply only when a default is advertised.
        if not reply.strip() and not self._quick_replies(prompt):
            return
        if send_stdin(job, reply):
            field.value = ""
            self.write_system_log(f'[runner] stdin → "{reply}" · resuming')
            self._refresh_stdin_bar()
            self._refresh_status_band()

    # ---- Tab strip -------------------------------------------------------
    def _refresh_tab_strip(self) -> None:
        try:
            tabs_list = self.query_one("#tabs-list", Horizontal)
        except Exception:
            return
        existing = {item.tab.id: item for item in tabs_list.query(TabItem)}
        current_ids = [t.id for t in self.tabs]
        for tid, item in list(existing.items()):
            if tid not in current_ids:
                item.remove()
                del existing[tid]

        for tab in self.tabs:
            is_active = (tab.id == self.active_tab_id)
            job = self.jobs.get(tab.job_id or "")
            awaiting = bool(job and job.awaiting)
            if tab.id in existing:
                item = existing[tab.id]
                item.set_class(is_active, "tab-item-active")
                item.set_class(not is_active, "tab-item")
                item.update_tab(tab, is_active, awaiting)
            else:
                tabs_list.mount(TabItem(tab, is_active=is_active, awaiting=awaiting))

        try:
            finished = sum(1 for t in self.tabs if t.id != "system" and t.status != "active")
            btn = self.query_one("#btn-close-finished", Static)
            if finished >= 2:
                btn.update(f"× close {finished} finished  [⇧W]")
                btn.remove_class("hidden")
            else:
                btn.add_class("hidden")
        except Exception:
            pass

    # ---- Events ----------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-input":
            self.filter_text = event.value
            self.cursor = 0
            self._rebuild_tree()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter-input":
            self.set_focus(None)
            self.action_activate()
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

    @property
    def args_dirty(self) -> bool:
        _, _, key, _ = self.current_flags()
        return key in self.flag_edits

    @property
    def active_jobs_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.running)

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

    # ---- Focus -----------------------------------------------------------
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

    # ---- Actions: navigation ---------------------------------------------
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

    def action_swap_pane(self) -> None:
        if self.focus_pane() == "recipes":
            self._focus_variants()
        else:
            self._focus_recipes()

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

    # ---- Actions: args ---------------------------------------------------
    CHAIN_ARGS_NOTE = "[args] edit a chain's steps in its yaml · per-recipe edits still apply"

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

    # ---- Actions: catalog ------------------------------------------------
    def action_reload_recipes(self) -> None:
        if self._reloading:
            return
        self._reloading = True
        chip = self.query_one("#mgr-chip", Static)
        chip.update("↻ …")
        chip.styles.color = WARN

        self.write_system_log(
            f"[recipes] reloading {display_path(RECIPES_PATH)} + "
            f"{display_path(recipes_mod.DROPIN_DIR)}/*.yaml + ./recipes.d/*.yaml …"
        )
        is_tool_installed.cache_clear()

        old_keys = {f"{t['id']}/{p['id']}" for t in self._recipes for p in t.get("presets", [])}
        self.catalog = load_catalog()
        self._recipes = self.catalog.tools
        new_keys = {f"{t['id']}/{p['id']}" for t in self._recipes for p in t.get("presets", [])}

        if self.selected_chain_id and self.get_chain(self.selected_chain_id) is None:
            self.selected_chain_id = None   # a chain the reloaded yaml no longer defines
        if f"{self.selected_tool_id}/{self.selected_preset_id}" not in new_keys and self._recipes:
            self.selected_tool_id = self._recipes[0]["id"]
            if self._recipes[0].get("presets"):
                self.selected_preset_id = self._recipes[0]["presets"][0]["id"]

        added = sorted(new_keys - old_keys)
        self.added_variants = added
        self.reload_revision += 1
        self.last_reload_time = time.strftime("%H:%M:%S")

        self._log_catalog_sources()
        total = self._total_variants()
        change = f" · +{len(added)} new ({', '.join(added)})" if added else " · no changes"
        self.write_system_log(
            f"[recipes] {len(self.recipes)} tools · {total} variants{change}",
            style=ACCENT if added else DIM,
        )
        sessions = len([j for j in self.jobs.values() if j.running])
        self.write_system_log(
            f"[runner] {sessions} session{'' if sessions == 1 else 's'} preserved · no jobs interrupted"
        )

        self._rebuild_tree()
        self._refresh_variants()
        self._refresh_args_band()
        self._refresh_status_band()

        if added:
            chip.update(f"↻ +{len(added)}")
            chip.styles.color = ACCENT
            self.set_timer(4.0, self._settle_mgr_chip)
        else:
            self._settle_mgr_chip()
        self._reloading = False

    def _settle_mgr_chip(self) -> None:
        try:
            chip = self.query_one("#mgr-chip", Static)
            chip.update("[M]")
            chip.styles.color = MUTED
        except Exception:
            pass
        self.added_variants = []

    def reload_note(self) -> str:
        return f"rev {self.reload_revision} · last {self.last_reload_time} · open sessions keep streaming"

    def manager_stats(self) -> List[Tuple[str, str, str]]:
        runnable = sum(1 for t in self.recipes if is_tool_installed(t.get("bin", "")))
        missing = len(self.recipes) - runnable
        drop_ins = len(self.catalog.files)
        return [
            ("tools", str(len(self.recipes)), FG),
            ("variants", str(self._total_variants()), FG),
            ("runnable", f"{runnable}/{len(self.recipes)}", ACCENT),
            ("missing", str(missing), WARN if missing else DIM),
            ("drop-ins", str(drop_ins), ACCENT if drop_ins else DIM),
            ("reloaded", self.last_reload_time, DIM),
        ]

    def manager_sources(self) -> List[dict]:
        rows = [{
            "kind": "base", "kind_color": ACCENT, "path": display_path(RECIPES_PATH),
            "count": f"{self.catalog.base_variant_count} variants", "count_color": DIM, "copyable": True,
        }]
        for name in self.catalog.files:
            rows.append({
                "kind": "drop-in", "kind_color": ACCENT,
                "path": display_path(self.catalog.file_paths.get(name, recipes_mod.DROPIN_DIR / name)),
                "count": f"{self.catalog.dropin_variant_count(name)} variants",
                "count_color": ACCENT, "copyable": False,
            })
        if not self.catalog.files:
            # Never an empty list — say what is being watched and where.
            rows.append({
                "kind": "watch", "kind_color": FAINT,
                "path": f"{display_path(recipes_mod.DROPIN_DIR)}/*.yaml + ./recipes.d/*.yaml",
                "count": "nothing loaded", "count_color": FAINT, "copyable": False,
            })
        return rows

    @staticmethod
    def _writes_outdir(preset: dict) -> bool:
        """True if the preset writes into $OUTDIR (so its dir must be created)."""
        return "OUTDIR" in template_vars(str(preset.get("flags", "")))

    def manager_tools(self) -> List[dict]:
        out = []
        for t in sorted(self.recipes, key=lambda t: (t.get("category", ""), t.get("bin", t["id"]))):
            ok = is_tool_installed(t.get("bin", ""))
            emitting = sum(1 for p in t.get("presets", []) if self._writes_outdir(p))
            fresh = sum(1 for k in self.added_variants if k.split("/")[0] == t["id"])
            out.append({
                "id": t["id"],
                "bin": t.get("bin", t["id"]),
                "bin_style": f"bold {FG}" if ok else UNFOCUSED,
                "cat": t.get("category", ""),
                "variants": f"{len(t.get('presets', []))}v",
                "emits": f"⇩ {emitting}" if emitting else "",
                "state": (t.get("version") or "available") if ok else "not on $PATH",
                "state_color": DIM if ok else WARN,
                "badge": f"+{fresh}" if fresh else "",
            })
        return out

    def action_recipe_manager(self) -> None:
        self.push_screen(RecipeManagerModal())

    def action_copy_catalog_path(self) -> None:
        copy_text_to_clipboard(str(RECIPES_PATH), app=self)
        self.write_system_log(f"[config] {RECIPES_PATH} copied to clipboard")

    # ---- Actions: layout -------------------------------------------------
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
        needed = sum(len(k) + len(l) + 5 for k, l, _ in HOTKEYS) + 4
        tight = self.size.width < needed
        for idx in range(len(HOTKEYS)):
            try:
                self.query_one(f"#footer-label-{idx}", Static).set_class(tight, "hidden")
            except Exception:
                pass

    # ---- Actions: escape chain -------------------------------------------
    def action_escape(self) -> None:
        """Stop at the first match. Esc never quits.

        palette → kill/detach → recipe manager → Target Scope → key bindings →
        raw ARGS → stdin field → filter with a query → stacked with VARIANTS
        expanded → no-op. (The modal screens above handle their own Esc.)
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
        if self._current_layout == "stacked" and self.stacked_pane == "task":
            self._focus_recipes()
            return

    # ---- Actions: tabs ---------------------------------------------------
    def action_select_tab(self, tab_id: str) -> None:
        self.active_tab_id = tab_id
        try:
            self.query_one("#tab-content", ContentSwitcher).current = f"log-{tab_id}"
        except Exception:
            pass
        self._refresh_tab_strip()
        self._refresh_results_chrome()

    def action_close_tab(self, tab_id: str) -> None:
        if tab_id == "system":
            return
        tab = next((t for t in self.tabs if t.id == tab_id), None)
        if tab is None:
            return
        job = self.jobs.get(tab.job_id or "")
        if job is not None and job.running:
            def resolved(choice: Optional[str]) -> None:
                if choice == "kill":
                    kill_job(job)
                    self.write_system_log(
                        f"[runner] {tab.label} killed by operator · SIGINT sent, "
                        f"SIGKILL in 10s if still running · partial output at {tab.artifact}",
                        style=WARN,
                    )
                    self._drop_tab(tab_id)
                elif choice == "detach":
                    # The process keeps running and keeps writing; only the tab goes.
                    self.write_system_log(
                        f"[runner] {tab.label} detached · still running · "
                        f"output continues at {tab.artifact}"
                    )
                    self._drop_tab(tab_id)

            self.push_screen(CloseJobModal(tab.label, tab.artifact), resolved)
            return
        self._drop_tab(tab_id)

    def _drop_tab(self, tab_id: str) -> None:
        idx = next((i for i, t in enumerate(self.tabs) if t.id == tab_id), -1)
        if idx == -1:
            return
        was_active = (self.active_tab_id == tab_id)
        self.tabs.pop(idx)
        if was_active:
            new_idx = max(0, min(idx, len(self.tabs) - 1))
            self.active_tab_id = self.tabs[new_idx].id
            try:
                self.query_one("#tab-content", ContentSwitcher).current = f"log-{self.active_tab_id}"
            except Exception:
                pass
        try:
            self.query_one(f"#log-{tab_id}", RichLog).remove()
        except Exception:
            pass
        self._refresh_tab_strip()
        self._refresh_results_chrome()

    def action_close_active_tab(self) -> None:
        if self.active_tab_id != "system":
            self.action_close_tab(self.active_tab_id)

    def action_close_finished_tabs(self) -> None:
        """Never prompts, and never touches a running job."""
        for tid in [t.id for t in self.tabs if t.id != "system" and t.status in ("done", "failed")]:
            self._drop_tab(tid)
        self.write_system_log("[runner] closed finished job tabs")

    def action_prev_tab(self) -> None:
        self._switch_tab(-1)

    def action_next_tab(self) -> None:
        self._switch_tab(1)

    def _switch_tab(self, delta: int) -> None:
        if not self.tabs:
            return
        idx = next((i for i, t in enumerate(self.tabs) if t.id == self.active_tab_id), 0)
        self.action_select_tab(self.tabs[(idx + delta) % len(self.tabs)].id)

    # ---- Actions: copy ---------------------------------------------------
    def action_copy_log(self) -> None:
        tab = self.active_tab()
        if not tab:
            return
        clock = self._get_clock_str()
        truncated = False
        if tab.id == "system":
            stamp = [f"# fieldlog {VERSION} · [System] · {clock}"]
            lines = list(self.system_log_lines)
        else:
            job = self.jobs.get(tab.job_id or "")
            running = " · still running" if job and job.running else ""
            stamp = [f"# fieldlog {VERSION} · {tab.label} · {clock}{running}"]
            if tab.cmd:
                stamp.append(f"# $ {tab.cmd}")
            if tab.artifact:
                stamp.append(f"# artifact: {tab.artifact}")
            lines = []
            if job:
                if job.log_path and job.log_path.exists():
                    try:
                        # Cap at 500 KB to avoid blocking UI thread or overflowing OSC 52
                        MAX_COPY_BYTES = 500 * 1024
                        file_size = job.log_path.stat().st_size
                        with job.log_path.open("rb") as f:
                            if file_size > MAX_COPY_BYTES:
                                f.seek(file_size - MAX_COPY_BYTES)
                                raw = f.read().decode("utf-8", errors="replace")
                                lines = raw.splitlines()
                                if len(lines) > 1:
                                    lines = lines[1:]
                                truncated = True
                            else:
                                raw = f.read().decode("utf-8", errors="replace")
                                lines = raw.splitlines()
                    except OSError:
                        lines = list(job.log_lines)
                else:
                    lines = list(job.log_lines)

        copy_text_to_clipboard("\n".join(stamp + [""] + lines) + "\n", app=self)
        status_lines = f"last {len(lines)} lines (capped at 500 KB)" if truncated else f"{len(lines)} lines"
        self.write_system_log(f"[clip] active log copied ({status_lines})")
        btn = self.query_one("#btn-copy-log", Static)
        btn.update(f"copied {len(lines)} lines ✓")
        btn.styles.color = ACCENT
        self.set_timer(1.8, lambda: (btn.update("⧉ copy log  [Ctrl+Shift+C]"), setattr(btn.styles, "color", DIM)))

    def action_copy_tail(self) -> None:
        tab = self.active_tab()
        if not tab or tab.id == "system" or not tab.artifact:
            self.write_system_log("[clip] harness log is not written to disk", style=WARN)
            return
        cmd = f"tail -f {tab.artifact}"
        copy_text_to_clipboard(cmd, app=self)
        self.write_system_log(f"[clip] {cmd}")

    def action_sigint(self) -> None:
        tab = self.active_tab()
        job = self.jobs.get(tab.job_id or "") if tab else None
        if job and job.running:
            interrupt_job(job)
            self.write_system_log(f"[runner] job #{job.id} interrupted by operator (SIGINT)", style=WARN)
            self._refresh_status_band()
            return
        self.notify("No running job · Ctrl+Shift+C copies the log", timeout=2.5)

    # ---- Actions: run ----------------------------------------------------
    def action_run_task(self) -> None:
        chain = self.selected_chain()
        if chain is not None:
            if not self.chain_blocked_flag(chain):
                self.run_worker(
                    self._run_chain_worker(chain), name=f"chain {chain['id']}", exclusive=False
                )
            return
        tool, preset, key, _ = self.current_flags()
        if not tool or self.is_blocked(tool, preset)[0]:
            return
        self._spawn_job(tool, preset, key)

    def select_and_run(self, tool_id: str, preset_id: str) -> None:
        self.selected_chain_id = None
        self.selected_tool_id = tool_id
        self.selected_preset_id = preset_id
        self._refresh_variants()
        self._refresh_args_band()
        self.action_run_task()

    def _remember(self, key: str) -> None:
        """Keep the last six things launched, recipe keys and `chain/<id>` alike."""
        if key not in self.recent:
            self.recent.insert(0, key)
            del self.recent[6:]
            save_pinned_recent(self.session.workspace_dir, self.pinned, self.recent)

    def _open_job_tab(self, plan: LaunchPlan, tool: dict, preset: dict) -> Tuple[RichLog, str]:
        """Tab, RichLog and transcript lines for a planned job. The caller starts
        the worker — a chain awaits its step rather than firing it and moving on."""
        job = plan.job
        for warning in plan.warnings:
            self.write_system_log(f"[artifact] {warning}", style=WARN)
        # job.id is the per-target run number, for display and the archive only.
        self._job_seq += 1
        job_key = str(self._job_seq)
        self.jobs[job_key] = job
        artifact = str(job.log_path)

        tab_id = f"job-{job_key}"
        self.tabs.append(TabDescriptor(
            id=tab_id, label=job.name, status="active", tool_id=tool["id"],
            job_id=job_key, cmd=plan.command, artifact=artifact,
        ))
        self.active_tab_id = tab_id

        rlog = RichLog(id=f"log-{tab_id}", wrap=True, markup=False, min_width=20)
        switcher = self.query_one("#tab-content", ContentSwitcher)
        switcher.mount(rlog)
        switcher.current = f"log-{tab_id}"

        self.write_system_log(f"[runner] spawn {tool.get('bin', tool['id'])}/{preset.get('id')} #{job.id}", style=ACCENT)
        self.write_system_log(f"[artifact] {artifact}")

        self._refresh_tab_strip()
        self._refresh_header()
        self._refresh_results_chrome()
        return rlog, tab_id

    def _spawn_job(self, tool: dict, preset: dict, key: str) -> None:
        """plan_launch captures root, scope and stamp onto the job; every
        displayed path renders from those captured fields, never from live state."""
        plan = plan_launch(self.session, tool, preset, flags_override=self.flag_edits.get(key))
        rlog, tab_id = self._open_job_tab(plan, tool, preset)
        self._remember(key)
        self.run_worker(self._run(plan, rlog, tab_id), name=plan.job.name, exclusive=False)

    async def _run_chain_worker(self, chain: dict) -> None:
        """Drive a chain, opening a tab per step as the driver reaches it."""
        cid = chain["id"]
        planned = len(chain.get("steps", []))
        self.write_system_log(f"[chain] {cid} · start · {planned} steps", style=ACCENT)
        self._remember(f"chain/{cid}")

        async def run_step(plan: LaunchPlan) -> int:
            tool = self.get_tool(plan.job.recipe_id) or {"id": plan.job.recipe_id}
            preset = {"id": plan.job.variant_id}
            rlog, tab_id = self._open_job_tab(plan, tool, preset)
            await self._run(plan, rlog, tab_id)
            return plan.job.exit_code if plan.job.exit_code is not None else 1

        result = await run_chain(
            self.session, self.catalog, chain,
            run_step=run_step, flags_overrides=self.flag_edits,
        )
        record = result.record
        ran = len(record["steps"])
        if record["stopped_at"]:
            last = record["steps"][-1]["exit_code"] if record["steps"] else result.exit_code
            self.write_system_log(
                f"[chain] {cid} · stopped at step {ran} {record['stopped_at']} (exit {last})",
                style=ERR,
            )
        else:
            self.write_system_log(
                f"[chain] {cid} · {ran}/{planned} steps · exit {result.exit_code}",
                style=ACCENT if result.exit_code == 0 else WARN,
            )

    async def _run(self, plan: LaunchPlan, rlog: RichLog, tab_id: str) -> None:
        job = plan.job
        UI_LINE_CAP = 500
        capped = False
        line_num = 0

        def _safe_write(renderable) -> None:
            if rlog.is_mounted:
                try:
                    width = rlog.scrollable_content_region.width or self._log_width()
                    rlog.write(renderable, width=width)
                except Exception:
                    pass

        def sink(text: str, _stream: str) -> None:
            nonlocal line_num, capped
            line_num += 1
            if line_num <= UI_LINE_CAP:
                _safe_write(Text.assemble((f"{line_num:3d}  ", GUTTER), (text, FG)))
                job.log_lines.append(text)
            elif not capped:
                capped = True
                msg = f"[Preview capped at {UI_LINE_CAP} lines · full output streaming to {job.log_path}]"
                _safe_write(Text(msg, style=WARN))
                job.log_lines.append(msg)

        code = 130 if job.interrupted else 1
        try:
            code = await run_job(plan.command, job, self.session, sink, on_state=self._on_job_block, env=plan.env)
            if line_num > UI_LINE_CAP:
                msg = f"[UI omitted {line_num - UI_LINE_CAP} lines · see {job.log_path}]"
                _safe_write(Text(msg, style=DIM))
                job.log_lines.append(msg)
            exit_line = f"[Runner] exit {code} in {job.elapsed:.2f}s"
            _safe_write(Text(exit_line, style=ACCENT if code == 0 else ERR))
            job.log_lines.append(exit_line)
        except asyncio.CancelledError:
            job.exit_code = code = 130 if job.interrupted else 1
            job.end_time = time.time()
        except Exception as exc:  # noqa: BLE001
            job.exit_code = code = 127
            job.end_time = time.time()
            err = f"[Runner] failed: {exc}"
            _safe_write(Text(err, style=ERR))
            job.log_lines.append(err)
        finally:
            if job.interrupted:
                job.exit_code = code = 130
            if job.end_time is None:
                job.end_time = time.time()
            for t in self.tabs:
                if t.id == tab_id:
                    t.status = "done" if code == 0 else "failed"
                    break
            self._refresh_tab_strip()
            self._refresh_header()
            self._refresh_results_chrome()

    def _on_job_block(self) -> None:
        """Raise or drop the stdin bar when a job blocks or resumes."""
        try:
            self._refresh_stdin_bar()
            self._refresh_status_band()
            self._refresh_tab_strip()
        except Exception:
            pass

    # ---- Workers ---------------------------------------------------------
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
