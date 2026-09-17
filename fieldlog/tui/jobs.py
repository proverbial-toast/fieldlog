"""Running jobs: the tab strip, the status band, the stdin bar, and the
workers that spawn and drive a run or a chain.

A mixin of FieldlogApp, not a widget: `self` is the app, and these
methods use its state and its widget tree directly. Split out for
navigability — app.py held every pane at once.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import List, Optional, Tuple

from rich.text import Text
from textual.app import ScreenStackError
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches, WrongType
from textual.widgets import ContentSwitcher, RichLog, Static

from fieldlog.recipes import run_passed
from fieldlog import __version__ as VERSION
from fieldlog.chain import run_chain
from fieldlog.launch import LaunchPlan, plan_launch
from fieldlog.report import chain_outcome
from fieldlog.runner import HIDDEN_REPLY, interrupt_job, kill_job, loggable_reply, run_job, send_stdin, strip_ansi
from fieldlog.state import ActiveJob, save_pinned_recent
from fieldlog.tui.helpers import copy_text_to_clipboard, truncate_right
from fieldlog.tui.models import TabDescriptor
from fieldlog.tui.modals import CloseJobModal
from fieldlog.tui.theme import ACCENT, DIM, ERR, FAINT, FG, GUTTER, SOFT, WARN
from fieldlog.tui.widgets import StdinChip, StdinInput, TabItem


# How long a job must have been blocked before an unrecognised prompt is worth
# the operator's keyboard. The runner calls a job "awaiting" after a partial
# line and 0.4s of quiet (runner.BLOCK_GRACE), which a tool that prints
# `working...` and pauses also satisfies; a wait this long is no longer
# explained by a slow tool.
STDIN_FOCUS_AFTER = 1.0


class JobsMixin:
    """Running jobs: the tab strip, the status band, the stdin bar, and the"""

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
        # EXIT is never fabricated: an unrecorded code is —, never 0. The code
        # shown is the tool's own; the colour is the recipe's verdict, so a
        # declared success reads green here and on its tab, not one of each.
        if job.exit_code is None:
            exit_val, exit_color = "—", FAINT
        else:
            exit_val = str(job.exit_code)
            exit_color = ACCENT if run_passed(job.exit_code, job.success_codes, job.expect_found) else ERR
        items = [
            ("state", state, state_color),
            ("exit", exit_val, exit_color),
            ("elapsed", job.elapsed_str(), SOFT),
            ("lines", str(job.lines_count), SOFT),
            ("bytes", f"{job.bytes_count / 1024:.1f} KiB", SOFT),
        ]
        # What the preset's `parse:` rule made of the log. The band is the only
        # place the TUI can say it: a tab strip has room for a label, not a finding.
        if job.summary:
            items.append(("summary", truncate_right(job.summary, 60), ACCENT))
        # Last of all, so a narrow band drops it before the summary that says
        # what was found instead. Only a miss is said; a check that was met is
        # already the green exit code.
        if job.expect_found is False:
            items.append(("expect", "not met", ERR))
        return items

    def _refresh_status_band(self) -> None:
        try:
            box = self.query_one("#status-items", Horizontal)
        except (NoMatches, WrongType, ScreenStackError):
            return                      # not mounted yet, or no screen at all
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
        texts = [
            Text.assemble((label.upper() + "\n", FAINT), (value, color))
            for label, value, color in keep
        ]
        # A running job redraws this every second, and every System transcript
        # line redraws it again: the numbers change, the shape almost never
        # does. Repaint the cells that are already there and only rebuild the
        # row when the band has actually changed shape.
        cells = list(box.query(Static))
        if len(cells) == len(texts):
            for cell, text in zip(cells, texts):
                cell.update(text)
            return
        box.remove_children()
        if texts:
            box.mount_all([Static(text, classes="status-item") for text in texts])

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
        # The pty hands the prompt over with its colour escapes still on; the
        # bar shows it clean, and the shape test below must not be fooled by a
        # reset sequence sitting after the colon.
        prompt = strip_ansi(job.await_prompt or "")
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

        # The bar goes up for any block, but the keyboard is only taken when the
        # process is plainly asking: text that ends the way a prompt ends
        # (`Password:`, `[y/N]`, `(yes/no)?`), or a block that has outlasted
        # STDIN_FOCUS_AFTER, by which point a slow tool is no longer the likely
        # reading. Anything else keeps the operator's next hotkey out of the
        # reply field. Esc (dismiss_stdin_focus) still hands the keyboard back,
        # and is checked ahead of both.
        if self._stdin_dismissed.get(tab.job_id) != job.await_since:
            blocked_for = time.time() - (job.await_since or time.time())
            if prompt.rstrip().endswith(("?", ":", "]", ")")) or blocked_for >= STDIN_FOCUS_AFTER:
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
        tab = self.active_tab()
        job = self.active_job()
        # Keyed by the job key, like self.jobs: job.id is the per-target run
        # number, so two targets' #01 runs would share one dismissal.
        if job is not None and tab is not None and tab.job_id:
            self._stdin_dismissed[tab.job_id] = job.await_since
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
            shown = loggable_reply(prompt, reply)
            said = f'"{shown}"' if shown is not None else HIDDEN_REPLY
            self.write_system_log(f"[runner] stdin → {said} · resuming")
            self._refresh_stdin_bar()
            self._refresh_status_band()

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

    @property
    def active_jobs_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.running)

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
                        f"SIGKILL in 10s if still running (at once on quit) · partial output at {tab.artifact}",
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
        tab = self.tabs.pop(idx)
        # The tab was the only thing holding the job: its buffered lines and
        # artifact record go with it. A detached job keeps running and keeps
        # writing to its log; nothing in the app reads it again either way.
        self.jobs.pop(tab.job_id or "", None)
        self._stdin_dismissed.pop(tab.job_id or "", None)
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
        """Keep the last six things launched, recipe keys and `chain/<id>` alike.

        Re-launching something already listed moves it back to the front, or the
        list would be the first six ever launched rather than the last six.
        """
        if key in self.recent:
            self.recent.remove(key)
        self.recent.insert(0, key)
        del self.recent[6:]
        save_pinned_recent(self.session.workspace_dir, self.pinned, self.recent)
        # The list just changed shape, so the pane showing it has to be redrawn
        # — the cursor keeps its row, not its index (see _rebuild_tree).
        self._rebuild_tree()

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
        """plan_launch captures root, stamp and out_dir onto the job; every
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
        if record["stopped_at"]:
            style = ERR
        else:
            style = ACCENT if result.exit_code == 0 else WARN
        self.write_system_log(f"[chain] {cid} · {chain_outcome(record, planned)}", style=style)

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
            if job.interrupted:
                exit_line += " · interrupted"
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
            if job.end_time is None:
                job.end_time = time.time()
            for t in self.tabs:
                if t.id == tab_id:
                    t.status = "done" if run_passed(code, job.success_codes, job.expect_found) else "failed"
                    break
            self._refresh_tab_strip()
            self._refresh_header()
            self._refresh_results_chrome()

    def _on_job_block(self) -> None:
        """Raise or drop the stdin bar when a job blocks or resumes."""
        with self._repaint("stdin bar"):
            self._refresh_stdin_bar()
            self._refresh_status_band()
            self._refresh_tab_strip()
