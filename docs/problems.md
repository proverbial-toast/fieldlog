# fieldlog — open problems and technical debt

**Status:** reference document, maintained. What is still open, deferred or rejected, and the debt that
shapes the next change. What the 2026-09-16 investigation, the 2026-09-17 remediation pass and the
2026-09-17 feature pass *fixed* — with each reproduction and its guarding test — is in
[`history/problems-fixed-2026-09-16-17.md`](history/problems-fixed-2026-09-16-17.md); the `§ 1.x` / `§ 2.x`
numbers cited below are its sections. Tags: **[confirmed]** reproduced by execution or read unambiguously
from the code · **[likely]** inferred from reading · **[proposal]** a recommended change. Every entry also
carries a status: **deferred** (real, not now, and why) · **rejected** (not a defect, or not worth the
change) · **open** (still to do or to verify).

The suite is 532 tests in 47 files; `ruff check` is clean.

---

## 3. Not fixed: deferred, rejected, open

### 3.1 `session.json` is rewritten whole on every append — **[likely, deferred]**

Cost is O(records) per run under the lock. The maintainer has explicitly deferred JSONL (D §6); the format is
fine to a few thousand records per target, and the writer and the two readers are the only places that would
change. Revisit only if a real target folder gets slow.

### 3.2 A note is quoted, not escaped, in the report — **[confirmed, rejected: by design]**

Operator prose, not tool output; a note containing `|`, `#` and a fence stays inside the blockquote. Recorded
so the asymmetry with the escaped summary is known to be deliberate.

### 3.3 `peek_run_number` trusts `.run-counter` — **[confirmed, rejected]**

A hand-edited `session.json` with an id above the counter makes the `$OUTDIR` *preview* understate by one.
Reading the manifest is exactly what the preview avoids (it is redrawn per keypress), reservations read both,
and a hand-edited manifest is outside the contract. Left as is.

### 3.4 `$LHOST` can never be IPv6 — **[likely, deferred]**

`get_interface_ip` uses `SIOCGIFADDR`, IPv4 only. A `$LHOST6` from `/proc/net/if_inet6` or `getaddrinfo` is
a feature, not a correctness fix, and was kept out of this pass on purpose.

### 3.5 `report --full` on a very large log reads it whole — **[likely, deferred]**

By request: `--full` means the whole log goes into the report, so the report string has to hold it anyway.
The only saving available inside `render_report`'s current shape is the intermediate list of lines (roughly
half of peak); a streaming writer would be a different API (`write_report(fh)`), which nothing needs yet.

### 3.6 `doctor --json` reports `summary.needs.interface`, always 0 — new, **[confirmed, open, minor]**

An interface block is never a *missing* value (an empty interface is allowed), so the count cannot move and no
footer hint reads it. It was already unreachable before the verdict change; dropping the key would alter the
pinned JSON shape. Drop it the next time that shape changes for a real reason.

### 3.7 The palette says "step blocked" for every blocked chain — new, **[confirmed, proposal]**

`check_chain` now carries the blocking step and its kind, so the chain row could say which step and why, as
the task rows do (`tui/modals.py` `_render_item`). Cosmetic; noted by the implementation agent.

### 3.8 The stdin-bar focus fallback — **[confirmed, accepted]**

The other half of §2.1: a tool silent for more than `STDIN_FOCUS_AFTER` on a partial line still gets focus.
Documented as the trade-off; the alternative (never taking focus for an unrecognised prompt) would break
tools whose prompts end in nothing recognisable.

### 3.9 Smaller things noticed while reviewing, not changed

- `find_recipe` fabricates a `default` preset for a tool with none, but `normalize_recipe` guarantees at least
  one, so that branch only serves hand-built catalogs.
- `tui/variants.py` calls `is_blocked` / `chain_blocked` and discards the reason; `blocked_flag` /
  `chain_blocked_flag` say that more directly.
- `write_system_log` wraps its `_refresh_status_band` call in a broad `except`; the method now handles its
  own missing-widget case. One of the D3 guards.

---

### 3.10 Two manifest readers — new, **[confirmed, open, minor]**

`cli.handle_history` parses `session.json` itself and exits 1 on a corrupt file; `report.load_runs` (used by
`report` and by `history`'s workspace overview) reads it as empty. See `history/problems-fixed-2026-09-16-17.md` § 2.14. Unify only with a decision
on which behaviour is wanted; the direct question arguably deserves the error.

### 3.11 macOS support is unverified on a Mac — new, **[likely, open]**

The 2026-09-17 macOS pass (`history/roadmap.md` group 3) was written and tested on Linux; the suite also passes
with `sys.platform` forced to `darwin`. What only a Mac can confirm, in order of consequence:

- `SIOCGIFADDR = 0xC0206921` and the address at `ifreq` offset 20 — derived from Darwin's headers
  (`_IOWR('i', 33, struct ifreq)`), proven only by `tests/test_platform.py`'s loopback test on the
  `macos-latest` CI leg. If wrong, `$LHOST` is empty on a Mac and presets using it are not runnable.
- The Darwin flags in the shipped catalog: `ping -c 4 -t 6`, `-c 1 -t 2 -b $IFACE`, `-D -s 1472`,
  `ping6 -c 4`, `scutil --dns`, `lsof -nP -iTCP -sTCP:LISTEN`. Each parses and resolves; none has been run.
- Darwin's ping statistics line: the rule reads `4 packets received, 0.0% packet loss`; a wording not seen
  costs the summary, not the run.
- Whether `gtimeout` is on the `macos-latest` image (the test that needs it is monkeypatched; only the
  real resolution goes unexercised).
- The pty tests (`test_controlling_tty.py`, `test_kill_escalation.py`) and Textual's `run_test` on Darwin.

Upgrade to **[confirmed]** when the macOS CI run is green; move anything it breaks into § 2.


## 4. Technical debt

What makes the next change harder than it should be. None of these is a bug.

| # | Debt | Where | Status |
|---|---|---|---|
| D1 | **No repaint/invalidation model in the TUI.** Eleven `_refresh_*` methods, ~15 call sites, each caller choosing a subset. | `tui/*.py`, `app.py` | **Deferred.** Worth doing only alongside F1 (history in the TUI), the first feature that would consume it. Open refactor #5. |
| D2 | `is_blocked` reasons were an implicit API. | `recipes.py`, `cli.py`, `tui/helpers.py` | **Resolved** — history § 2.3, `Verdict`. |
| D3 | **Broad `except Exception` guards** in the TUI: 35 before this pass, 34 after (the one in `_refresh_status_band` is now `(NoMatches, WrongType)`). | `tui/*.py`, `app.py` | **Deferred.** Correct for a missing widget; they also hide real bugs in the guarded body. `_repaint` is the shape to adopt; narrow them as each pane is next touched rather than in a sweep. |
| D4 | **`TargetModal` lives in `app.py`** so tests can monkeypatch `get_interface_ip` through that module. | `app.py` | **Deferred, minor.** Injecting the probe functions into the modal would free it; nothing is waiting on that. |
| D5 | `tui/theme.py` imported the catalog. | `tui/theme.py` | **Resolved** — history § 2.10. |
| D6 | Two substitution mechanisms for one set of bindings. | `state.py`, `runner.py` | **Resolved** — history § 2.11, said in the code. |
| D7 | **The `_WRAPPERS` heuristic** decides whether `bin` is prepended from the first token of the flags (`env`, `sudo`, `timeout`, `doas`, `nice`). | `recipes.py` | **Deferred.** The honest replacement is an explicit preset field (`command: true`, "flags are the whole command"), which matches the maintainer's preference for opt-in over inference — but it is a feature, kept out of a correctness pass. |
| D8 | `Catalog.files` vs `file_paths` vs `overrides` vs `errors` — four provenance collections. | `recipes.py` | **Deferred.** A single `Source(path, kind, tools, presets, chains, errors)` would answer the manager modal and the boot transcript in one shape; the merge unification (§2.4) did not need it. |
| D9 | No type checking in CI. | `pyproject.toml`, CI | **Rejected.** The maintainer declined mypy outright on 2026-09-16 (gating, annotation work or a CI step). Do not re-propose. |
| D10 | The catalog is untyped dicts. | everywhere | **Deferred.** `preset.get("bin", tool.get("bin", tool["id"]))` is still copied a dozen times; a `Preset`/`Tool` dataclass is the fix, and a large one. |
| D11 | TUI coverage is thin. | `tests/` | **Partly resolved.** This pass added pane tests for the stdin bar (focus rule, dismissal), the status band and the scope form — 10 tests driving the mounted app. The tree cursor, the variants pane and the layout switch are still untested. |

---

## 5. Things the reviews said to leave alone, and still should

The pty + `exec_form` + `_make_ctty` triangle in `runner.py`, and the catalog's fail-soft loading. Both were
re-read for this document; every comment in them is still load-bearing. The 2026-09-17 passes touched the runner
(history § 2.2, § 2.8, and `expect:`) and the loader (history § 2.4, `platform:`), each time inside that policy: nothing new fails closed, and the
pty/exec/ctty code is as it was.
