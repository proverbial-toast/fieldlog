"""The recipe catalog as the app sees it: lookups, blocked verdicts,
reloading, and the recipe manager's tables.

A mixin of FieldlogApp, not a widget: `self` is the app, and these
methods use its state and its widget tree directly. Split out for
navigability — app.py held every pane at once.
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from textual.widgets import Static

from fieldlog import recipes as recipes_mod
from fieldlog.recipes import (
    RECIPES_PATH,
    Verdict,
    chain_blocked,
    check_recipe,
    display_path,
    clear_tool_cache,
    is_tool_installed,
    load_catalog,
    writes_outdir,
)
from fieldlog.tui.helpers import copy_text_to_clipboard
from fieldlog.tui.modals import RecipeManagerModal
from fieldlog.tui.theme import ACCENT, DIM, FAINT, FG, MUTED, UNFOCUSED, WARN



class CatalogMixin:
    """The recipe catalog as the app sees it: lookups, blocked verdicts,"""

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

    def selected_recipe(self) -> Tuple[dict, dict]:
        """The tool and preset the panes are painting, with the ids made to agree.

        `selected_tool_id` and `selected_preset_id` are plain strings, set from
        a tree row, a palette pick, a saved default or a reload, so either can
        name something this catalog does not have — `ping/sweep` on a box
        without ping, a preset a drop-in edit has since renamed. Every reader
        used to absorb that with its own silent fallback, which left the two
        ids saying one thing while every pane showed another; the tree could
        then find no row for what was on screen. Resolving once and writing the
        answer back is what makes the ids worth matching a row against.

        `({}, {})` only when the catalog holds no tools at all.
        """
        tool = self.get_tool(self.selected_tool_id) or (self.recipes[0] if self.recipes else None)
        if tool is None:
            return {}, {}
        preset = self.get_preset(tool, self.selected_preset_id)
        self.selected_tool_id = tool["id"]
        self.selected_preset_id = preset.get("id", "default")
        return tool, preset

    @property
    def chains(self) -> List[dict]:
        return self.catalog.chains

    def get_chain(self, chain_id: str) -> Optional[dict]:
        return next((c for c in self.chains if c["id"] == chain_id), None)

    def selected_chain(self) -> Optional[dict]:
        return self.get_chain(self.selected_chain_id) if self.selected_chain_id else None

    def verdict(self, tool: dict, preset: dict) -> Verdict:
        """Whether this recipe can run now, and what kind of gap stops it.

        Judged against this recipe's args edit when it has one, because that is
        the template `_spawn_job` will launch — a row that says it can run and
        then runs with an empty `$HOST` is the same bug either way.
        """
        key = f"{tool.get('id', '')}/{preset.get('id', '')}"
        return check_recipe(tool, preset, self.session, flags=self.flag_edits.get(key))

    def is_blocked(self, tool: dict, preset: dict) -> Tuple[bool, str]:
        """(blocked, reason) for a caller that wants only the text."""
        verdict = self.verdict(tool, preset)
        return verdict.blocked, verdict.reason

    def blocked_flag(self, tool: dict, preset: dict) -> bool:
        return self.is_blocked(tool, preset)[0]

    def chain_blocked_flag(self, chain: dict) -> bool:
        return chain_blocked(self.catalog, chain, self.session, flags_overrides=self.flag_edits)[0]

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
        clear_tool_cache()

        old_keys = {f"{t['id']}/{p['id']}" for t in self._recipes for p in t.get("presets", [])}
        self.catalog = load_catalog()
        self._recipes = self.catalog.tools
        new_keys = {f"{t['id']}/{p['id']}" for t in self._recipes for p in t.get("presets", [])}

        if self.selected_chain_id and self.get_chain(self.selected_chain_id) is None:
            self.selected_chain_id = None   # a chain the reloaded yaml no longer defines
        # Whatever the reload changed, the two ids have to name something this
        # catalog holds. `selected_recipe` settles that the way every pane
        # does; the fixup written here used to jump to the first tool in the
        # file whenever a preset id went missing, so renaming one variant of
        # ping threw the operator to the top of the catalog.
        self.selected_recipe()

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
        # One row per shipped theme, off ones included: a theme that is off has
        # no recipes in the catalog to be found by any other means, so this is
        # the only place the TUI can say it exists.
        for name, active in sorted(self.catalog.themes.items()):
            rows.append({
                "kind": "theme", "kind_color": ACCENT if active else FAINT,
                "path": name,
                "count": "on" if active else f"off · {display_path(recipes_mod.THEMES_PATH)}",
                "count_color": DIM if active else WARN, "copyable": False,
            })
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
        return writes_outdir(preset)

    def manager_tools(self) -> List[dict]:
        out = []
        for t in sorted(self.recipes, key=lambda t: t.get("bin", t["id"])):
            ok = is_tool_installed(t.get("bin", ""))
            emitting = sum(1 for p in t.get("presets", []) if self._writes_outdir(p))
            fresh = sum(1 for k in self.added_variants if k.split("/")[0] == t["id"])
            out.append({
                "id": t["id"],
                "bin": t.get("bin", t["id"]),
                "bin_style": f"bold {FG}" if ok else UNFOCUSED,
                "name": t.get("name", ""),
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
