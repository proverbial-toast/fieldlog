# fieldlog — architectural investigation

**Date:** 2026-09-16
**Scope:** the whole Python codebase — `fieldlog/`, `fieldlog/tui/`, `tests/`, packaging and CI.
**Method:** read the source and traced the run / chain / archive paths through the implementation
rather than from names or docstrings. Ran the suite (269 passing) and `ruff check` (clean).
Several findings below were confirmed by execution, not by reading; those are marked **verified**.

> **Superseded as a reference, kept as a record.** The architecture map in §1 is now maintained in
> [`architecture.md`](../architecture.md); the open items (#5 repaint model, #10 `reason_kind`, the "Smaller"
> list) are tracked in [`problems.md`](../problems.md) §4 and [`next-steps.md`](../next-steps.md). See
> [`README.md`](../README.md) for the documentation map.
>
> Companion to [`review-2026-09-15.md`](review-2026-09-15.md), which was an open-ended code review.
> This one is structural: how the system actually fits together, and where that differs from how it
> presents itself. It does not re-litigate items that review already actioned.

> **Status — updated 2026-09-16.** Most of this has since been actioned; the text below is the
> original write-up and describes the pre-change state, so its **line references are as of the review
> date** and several have shifted since.
> - ✅ **R1** chain step exit codes — both front-ends now record the tool's own code, `success` rides
>   on the step record, and the report's step table applies it. Also fixed the TUI status band (R8).
> - ✅ **R2** artifact attribution — a run records its own log, plus what it wrote in `$OUTDIR`
>   (diffed against a snapshot of that one directory, so a chain's shared `$OUTDIR` still attributes
>   per step); `scan: true` opts back into the folder-wide diff.
> - ✅ **R3** the block gate now reads the template that will actually run, args edit included.
> - ✅ **R4** `history` / `report` accept the folder name or the target the runs were made against.
> - ⊘ **R5** withdrawn after measurement (see below); refactor #8 goes with it.
> - ✅ **R6** a job is reclaimed with its tab; the harness transcript is capped.
> - ✅ **R7** the five largest silent guards now degrade *loudly* — a real failure names itself in
>   the harness transcript; a missing widget stays quiet. 36 broad handlers down to 31.
> - ✅ **R9** echo is the front-end's choice: the CLI leaves the pty's own echo on, handing control
>   back to the tool (sudo hides a password, ssh shows a yes/no), so a reply is visible and archived.
> - ✅ **R10 / #6** `FieldlogApp` split into one mixin per pane — `app.py` 2241 → 800 lines, with
>   `tui/{catalog,tree,variants,args,jobs,layout}.py` beside it. No behaviour change: the class's
>   159 public members are unchanged, and `tests/test_app_structure.py` guards the assembly.
> - ✅ **#7** manifest writing moved to `archive.py`; `chain.py` no longer imports a runner private.
> - ✅ **Also fixed:** the 2026-09-15 review's open "Minor" item — an empty primary log dropping out
>   of a `scan` run's artifact list — reproduced and closed. A run always records its own log.
> - ❌ Still open: refactors #5 (repaint model) and #10 (`reason_kind`), and the smaller items.

---

## 1. Architecture map

### Layering (actual, from the import graph)

```text
                       __main__.py  ──►  cli.main
                                          │
          ┌───────────────────────────────┴─────────────────────┐
          │                                                     │
      cli.py  (6 subcommands, argparse)              app.py (FieldlogApp, Textual)
          │                                                     │  composition · bindings · lifecycle
          │                                                     ├─ tui/catalog · tree · variants
          │                                                     │      args · jobs · layout   (mixins)
          │                                                     └─ tui/modals · widgets · theme
          │                                                            models · helpers
          └──────────────────┬──────────────────────────────────┘
                             ▼
                        launch.py — plan_launch()  ← the single command-building funnel
                             │
       ┌─────────────────────┼──────────────────────┬────────────────┐
       ▼                     ▼                      ▼                ▼
   recipes.py            state.py               archive.py       runner.py
  (catalog, YAML,    (TargetSession,          (snapshot/delta,   (pty, stream,
   merge, search,     ActiveJob, path          flock, counter,    manifest record)
   is_blocked)        resolution, $vars)       manifest write)
       ▲                     ▲                                       ▲
       └─── report.py ───────┘                          chain.py ────┘
            (pure Markdown)                        (step driver, front-end agnostic)
```

Dependency direction is genuinely clean and acyclic. Two edges are worth naming:

- [`recipes.py:23`](../fieldlog/recipes.py#L23) — the *catalog* module imports `template_vars` from
  *state*. `is_blocked` needs to know which `$VARS` a preset references, so catalog validation
  depends on the session model. `TargetSession` is only a `TYPE_CHECKING` import, so it isn't a
  cycle, but the direction is backwards from what the names suggest.
- [`chain.py:20`](../fieldlog/chain.py#L20) — `chain` imports `_write_record`, a private, from
  `runner`. The chain summary is a manifest record that no public API exposes. *(Since fixed: the
  writer moved to `archive.py` as `append_record`, and `chain` imports only public names.)*

### The four execution paths

**A. `fieldlog run <recipe> <target>`** — [`cli.py:1141`](../fieldlog/cli.py#L1141)
`dispatch_argv` → argparse → `handle_run` → `find_chain` (tried first; chain ids can't collide with
tool ids) → `find_recipe` → `is_blocked` gate → `plan_launch` → `asyncio.run(execute_cli_job)` →
`run_job`.

**B. `plan_launch`** — [`launch.py:44`](../fieldlog/launch.py#L44) — the one place command, env and
paths are built:

1. `reserve_run_number` under `flock` (or a read-only preview under `--dry-run`)
2. `prepare_job_paths` → log path + `$OUTDIR`, with a fallback to `raw/` if the log destination is
   unwritable
3. `resolve_flags` substitutes `$TARGET/$HOST/$IFACE/$LHOST/$OUTDIR` into the **unresolved template**
4. `format_command` prepends the binary unless the flags already start with it or a wrapper
   (`timeout`, `sudo`, `doas`, `env`, `nice`)
5. optional `timeout -k 5 Ns sh -c '<exec form>'` wrapper
6. everything captured onto the `ActiveJob` — root, scope, stamp, out_dir, parse rule, success codes

**C. `run_job`** — [`runner.py:214`](../fieldlog/runner.py#L214) — the systems core:
`snapshot_workspace` (pre) → `openpty` with ECHO off →
`create_subprocess_shell(exec_form(cmd), preexec_fn=_make_ctty)` → `loop.add_reader(master)` feeding
an `asyncio.Queue` → line-splitting loop that tees ANSI-stripped text to the log and raw text to the
sink → `proc.wait()` → `parse_summary(log_tail(...))` → artifact collection → `_write_record`
under the manifest lock. *(Since: artifact collection is `collect_job_artifacts` unless the recipe
says `scan: true`, and the writer is `archive.append_record`.)*

**D. `run_chain`** — [`chain.py:37`](../fieldlog/chain.py#L37) — inverted control. It plans each step
lazily (so a halted chain reserves no numbers for steps that never ran), shares one `$OUTDIR` stamp
across steps, and hands the actual execution to a `run_step` callback the front-end supplies. Its own
summary run id is claimed **last**, keeping step numbers contiguous.

### State management

Three tiers, well separated:

| Tier | Where | Lifetime |
|---|---|---|
| Scope | `TargetSession` — target, hostname, lhost, interface, workspace, artifact_root | Process; persisted by the TUI only, on unmount |
| Job | `ActiveJob` — captured paths, pty fd, queue, counts, exit, summary | One run |
| Durable | `targets/<slug>/session.json` + `raw/` + `.run-counter` | Forever — this is the product |

Process-global mutable state lives in three caches: `_IP_CACHE` (2 s TTL,
[`state.py:88`](../fieldlog/state.py#L88)), `_PATH_DIR_NAMES` (a `$PATH` directory listing,
[`recipes.py:62`](../fieldlog/recipes.py#L62)), and `is_tool_installed`'s `lru_cache`.
`clear_tool_cache()` correctly invalidates the last two together.

**Deliberate and important:** `effective_lhost()` is resolved *at spawn*, never stored, so a VPN that
comes up after launch lands the right address. Conversely, `ActiveJob.root/scope/stamp/out_dir` are
captured *at spawn* and never re-derived, so a later scope change can't rewrite the path shown for a
running job. These two opposite choices are both correct and both load-bearing.

### Persistence & concurrency

`session.json` is a JSON array, read-append-replace under `flock` on a sidecar `.session.lock` — a
lock on `session.json` itself would be stranded on the old inode by `os.replace`. `.run-counter`
stores *highest handed out*, not *highest recorded*, so a still-running job's number can't be
reissued.

**Verified** with six concurrent processes against one target: counter reached 6, ids `01`–`06`, six
records, no collisions.

Concurrency model: single event loop per process. The CLI runs one job via `asyncio.run`; the TUI
runs N jobs as non-exclusive Textual workers. There are no threads and no locks in-process —
correctly, since everything shares the loop.

### CLI/UI boundary

`plan_launch` is the seam and it holds well. Both front-ends build zero commands themselves.
`run_chain`'s `RunStep` callback is the other seam — and it's the one that leaked (see R1, since
fixed).

*Since the review:* the TUI side was split into six mixins (R10), so `app.py` is the widget tree and
the wiring and each pane's behaviour sits beside its own name. The seam between the front-ends is
unchanged — it was never the problem.

### Test architecture

269 tests, ~6 s, 144 test functions across 21 files — **303 across 25 files** after the changes
above. Three tiers: pure-function unit tests (parse,
scope chars, arg parsing, path lookup), filesystem integration tests using a `tmp_workspace` fixture,
and ~20 async tests, 8 of which drive the real app through `app.run_test()`. CI matrixes 3.11–3.13
with ruff, plus a separate `package` job that builds the wheel, installs it clean and imports
`fieldlog.app` — that job exists because a hand-listed `packages` once shipped a wheel without
`fieldlog.tui`.

---

## 2. The ten implementation details to hold in your head

1. **`exec_form` exists to fix a real exit-code bug.**
   [`runner.py:170`](../fieldlog/runner.py#L170). dash forks even for `sh -c "ping …"`; on a group
   SIGINT the shell dies *of* the signal while ping catches it and exits 0, so `proc.wait()` returned
   the shell's code, not the tool's. `exec` makes the tool the process you wait on. It bails out to
   the plain command for pipelines and lists — `shlex` with `punctuation_chars=True` is what splits
   `a|b` into three tokens so the check actually fires.

2. **`_make_ctty` runs before `close_fds`.**
   [`runner.py:186`](../fieldlog/runner.py#L186). `os.setsid()` + `TIOCSCTTY` on the slave fd. The
   comment that this runs *before* subprocess's close_fds sweep is why no `pass_fds` is needed, and
   the `setsid()` is why `killpg(pid)` works everywhere else in the file. Touching either end breaks
   sudo/ssh prompts silently.

3. **Two writes to `$OUTDIR` semantics.** `writes_outdir`
   ([`recipes.py:163`](../fieldlog/recipes.py#L163)) fires on `$OUTDIR` appearing in the *template*
   **or** an explicit `outdir: true` — the latter exists for scripts that read `$OUTDIR` from the
   environment, which no command-line scan can see. `plan_launch` checks
   `f"{template} {extra_args}"`, deliberately the unresolved form.

4. **The blocked-reason strings are a protocol.** `doctor_bucket`
   ([`cli.py:911`](../fieldlog/cli.py#L911)) matches on phrases `is_blocked` emits
   (`"not found in $PATH"`, `"dns name"`, `"local address"`). It's string-matching, but it's the
   mechanism that guarantees doctor and an actual run can never disagree.

5. **Catalog merge order and validation order.** Base → config `recipes.d` → `./recipes.d` (local
   wins on same basename), then `_validate_chains` / `_validate_parsers` / `_validate_success` run
   *after everything is merged*, because a base chain may legitimately name a drop-in's preset. A bad
   file costs that file, never the catalog; a bad regex costs the summary, never the recipe.

6. **`parse_summary` takes the *last* match.**
   [`recipes.py:189`](../fieldlog/recipes.py#L189). Deliberate: a `-c 4` ping writes four lines that
   look like the closing stats. It runs over at most the last 64 KB (`PARSE_TAIL_BYTES`), which also
   bounds the text an operator-supplied regex sees.

7. **Scope values are allowlisted, not quoted.** `_UNSAFE_SCOPE` / `_UNSAFE_TARGET`
   ([`recipes.py:113`](../fieldlog/recipes.py#L113)). Values are interpolated raw into a shell line,
   so safety rests entirely on `is_blocked` refusing metacharacters. `@` is allowed for ssh
   `user@host`; `$` and `(` stay refused.

8. **Reply logging is secret-aware.** `loggable_reply`
   ([`runner.py:127`](../fieldlog/runner.py#L127)) writes a reply into the log only if it matches one
   of the prompt's **bracketed** choices; anything else becomes `(reply hidden)`. The brackets
   requirement is what stops words in a path (`/home/operator/.ssh/…`) counting as choices. The pty
   slave has ECHO off so the reply lands exactly once, from the explicit `› ` note.

9. **The "blocked" heuristic is a timeout, not a parser.**
   [`runner.py:298`](../fieldlog/runner.py#L298). A partial line + 0.4 s of quiet = awaiting input.
   The pending text is then committed as a real line *before* the reply, so the artifact reads
   question-then-answer. Nothing is ever parsed semantically.

10. **`run_succeeded` is the single definition of a good exit**
    ([`recipes.py:213`](../fieldlog/recipes.py#L213)) — used by the CLI status, the TUI tab icon, the
    chain stop policy and the report. The raw code is always what's archived; `success:` is recorded
    alongside so a reader can see *why* a non-zero exit wasn't a failure.

---

## 3. The ten most significant architectural risks

### R1 — `RunStep` returns different things from the two front-ends, and the archive shows it — **verified · FIXED 2026-09-16**

`run_chain`'s contract says "run it and come back with its exit code"
([`chain.py:24`](../fieldlog/chain.py#L24)). The CLI's `run_step` returns `execute_cli_job`'s
**normalised status** ([`cli.py:599`](../fieldlog/cli.py#L599)); the TUI's returns the **raw**
`job.exit_code` ([`app.py:2053`](../fieldlog/app.py#L2053)). `chain.py:69` writes whatever it gets
into the chain summary.

Demonstrated: a step with `success: [0, 1]` exiting 1 produced, **in the same `session.json`**:

```text
step record 03  noop/soft  exit 1   success [0,1]
CHAIN SUMMARY   noop/soft  exit_code: 0
```

The TUI would have written `1`. The archive is the product; two records in it disagree about the same
fact, and which one you get depends on the front-end. `_step_table`
([`report.py:263`](../fieldlog/report.py#L263)) also renders step codes with no `ok=` argument, so it
can't recover the distinction either.

### R2 — Concurrent runs claim each other's artifacts — **verified · FIXED 2026-09-16**

`detect_artifact_deltas` attributes files by mtime/size diff against a snapshot taken at job start
([`runner.py:242`](../fieldlog/runner.py#L242), [`archive.py:115`](../fieldlog/archive.py#L115)).
There is no ownership concept. Six concurrent runs against one target produced:

```text
01 -> [01.log, 02.log, 03.log, 04.log, 05.log, 06.log]
02 -> [02.log, 01.log, 03.log, 04.log, 05.log, 06.log]
06 -> [06.log, 05.log]
```

Every record claims every sibling that happened to be running. Line/byte/file totals in the CLI
summary and the report's **Files** column are correspondingly inflated. The TUI's whole design is
parallel job tabs, so this is the normal case there, not an edge case. The model is only sound when
runs are serialised.

### R3 — The block gate validates the preset; the launch runs the edit — **FIXED 2026-09-16**

`action_run_task` gates on `is_blocked(tool, preset, session)`
([`app.py:1982`](../fieldlog/app.py#L1982)), which reads `preset["flags"]`. `_spawn_job` then
launches with `flags_override=self.flag_edits.get(key)`
([`app.py:2036`](../fieldlog/app.py#L2036)). An args edit that introduces a `$VAR` the preset never
used is never validated — `$HOST` silently resolves to empty, and an unsafe `$TARGET` reaches the
shell because the gate never looked at TARGET. Same hole for chains: `chain_blocked` checks step
presets, `run_chain` is passed `flags_overrides=self.flag_edits`.

### R4 — `history` / `report` look up a different directory than `run` writes to — **verified · FIXED 2026-09-16**

`run` archives under `slug = scope_dir(hostname or target)`
([`state.py:195`](../fieldlog/state.py#L195)); `history` and `report` compute
`workspace / scope_dir(target_arg)` ([`cli.py:801`](../fieldlog/cli.py#L801),
[`cli.py:856`](../fieldlog/cli.py#L856)).

```console
$ fieldlog run ping/quick 10.10.10.55 -H box.htb     → targets/box.htb/
$ fieldlog history 10.10.10.55   → No session.json found at targets/10.10.10.55/session.json
$ fieldlog history box.htb       → works
```

The README documents this ("take the folder name"), but the flag is spelled `-t/--target` in both
commands and means two different things. Anyone who used `-H` once will hit it.

### R5 — ~~A log big enough to need a windowed read is still read whole at job end~~ — **withdrawn**

*Corrected 2026-09-16: this overstated the cost and the original text is struck rather than kept.*
`count_lines_safe` ([`archive.py:58`](../fieldlog/archive.py#L58)) does read every recorded file
end-to-end at job completion, but it **streams** in 64 KB chunks and never holds the file. Measured
on a 100 MB log: 0.05 s and 0.1 MB of RSS, against 0.15 s and 204 MB for the `read_text` that
`report.py` goes to such trouble to avoid. Extrapolated to the 410 MB log in that module's comment,
the runner pays roughly 0.2 s of sequential I/O and no memory — not the 1.4 s / 1.1 GB the report
path would. There is no asymmetry here worth fixing; the two ends of the pipeline have different
problems and each already solves its own.

The one real cost was the pair of full tree walks per run, and R2 removed those from the default
path.

### R6 — Nothing in the TUI is ever reclaimed — **FIXED 2026-09-16**

`self.jobs` is written at [`app.py:2010`](../fieldlog/app.py#L2010) and never popped — `_drop_tab`
removes the tab and the `RichLog`, leaving the `ActiveJob` with its `log_lines`, `artifact_delta` and
captured paths. `system_log_lines` and `_stdin_dismissed` grow without bound too. Bounded per item,
unbounded in count; a long field session with many short runs grows monotonically.

### R7 — 36 `except Exception` blocks in `app.py`, 30 of them silent — **LARGELY FIXED 2026-09-16**

The pyproject comment explains this as deliberate graceful degradation, and for `query_one` guards it
is. But `_refresh_args_band` ([`app.py:1038`](../fieldlog/app.py#L1038)) wraps its *entire* 50-line
body — token layout, styling, mounting — in one `try/except: pass`. A genuine bug in there leaves the
band showing stale args with no trace anywhere, and the operator is about to press Enter on it. Same
shape in `_refresh_chain_args_band` and `_refresh_stdin_bar`.

### R8 — The status band ignores `success:`, so one screen contradicts itself — **FIXED 2026-09-16**

`exit_color = ACCENT if job.exit_code == 0 else ERR` ([`app.py:1168`](../fieldlog/app.py#L1168))
doesn't consult `run_succeeded(code, job.success_codes)`, but the tab icon at
[`app.py:2124`](../fieldlog/app.py#L2124) does. A `success: [0,1]` recipe exiting 1 shows a green ✓
tab beside a red `EXIT 1`.

### R9 — CLI prompts are answered completely blind — **FIXED 2026-09-16**

`tty.setcbreak` clears ECHO on the operator's terminal (confirmed in this Python's
`tty.cfmakecbreak`), and `_open_pty` clears ECHO on the slave. The TUI compensates with the `› reply`
note; the CLI's `forward_stdin` ([`cli.py:520`](../fieldlog/cli.py#L520)) writes straight to `pty_fd`
with no note path. So answering an ssh `yes/no` from the CLI shows nothing on screen and records
nothing in the log. Right for a passphrase, wrong for a confirmation — and the archive loses the
answer either way.

### R10 — `app.py` is still 2208 lines and 23% of the source — **FIXED 2026-09-16**

The prior review's split extracted modals and widgets, but `FieldlogApp` itself is ~1850 lines with
~90 methods holding layout, tree, variants, args band, status band, stdin bar, tab strip, layout
modes, clipboard, and job orchestration. `TargetModal` also still lives here, which is why `app.py`
is the only module that can't be imported without Textual.

### Smaller

- The `run` fallback in `dispatch_argv` ([`cli.py:110`](../fieldlog/cli.py#L110)) turns any
  unrecognised leading token into `["run", token]`. `fieldlog --json` becomes `run --json` and dies
  in argparse; `fieldlog repot 10.0.0.1` becomes "Unknown recipe or tool 'repot'". The shorthand is
  good; the diagnostic for a near-miss subcommand is not.
- `kill_job`'s SIGKILL escalation is a `loop.call_later` ([`runner.py:106`](../fieldlog/runner.py#L106))
  that silently never fires if the app exits inside the 10 s grace.
- `handle_history` accepts a `{"runs": [...]}` manifest shape
  ([`cli.py:822`](../fieldlog/cli.py#L822)) that `load_runs` and `load_target_history` both reject —
  dead tolerance for a format nothing writes.

---

## 4. Unnecessary coupling

| Coupling | Where | Why it's avoidable |
|---|---|---|
| ~~`chain` → `runner._write_record`~~ — **fixed** | [`chain.py:20`](../fieldlog/chain.py#L20) | Manifest writing is an archive concern; `append_record` and `manifest_environment` now live in `archive.py`. `runner` imports them, and `chain` imports only the public `build_env`. |
| `recipes` → `state.template_vars` | [`recipes.py:23`](../fieldlog/recipes.py#L23) | `template_vars` and the `$VAR` grammar are catalog-schema concerns that `state` happens to host. Moving the regex + `SCOPE_VARS` into `recipes` (or a `vars.py`) reverses an edge that currently points the wrong way. |
| `tui/theme` → `recipes.RECIPES_PATH` | [`theme.py:5`](../fieldlog/tui/theme.py#L5) | Only to fill one palette hint string. A constants module importing the catalog means the palette can't be loaded without YAML. Pass the path in at render time. |
| Doctor's bucketing ↔ `is_blocked`'s prose | [`cli.py:911`](../fieldlog/cli.py#L911) | The intent (one source of truth for runnability) is right; the mechanism is substring matching on human-readable English. A `reason_kind` field on the return would keep the guarantee and let the prose be edited freely. |
| `ActiveJob` owns `pty_fd` and `_queue` | [`state.py:319`](../fieldlog/state.py#L319) | Documented honestly ("owned by runner.py, but declared here") because the CLI registers a stdin reader before `run_job` opens the pty. That ordering is the actual coupling; the field placement is the symptom. |
| `TargetModal` in `app.py` | [`app.py:141`](../fieldlog/app.py#L141) | Kept there "coupled to interface probing", but the probing is `list_box_interfaces` / `default_interface`, two pure-ish functions that would move with it. |

---

## 5. Abstractions hiding complexity rather than reducing it

1. **`ArtifactDelta` is the big one.** — **fixed (R2).** It presented as "this run's artifacts" and
   was consumed everywhere as provenance — the manifest, the CLI summary, the report's Files column —
   while underneath it was a whole-tree mtime/size diff with no notion of which process wrote what:
   *lossless-looking and lossy*, which is worse than an obviously approximate one. There are now two
   producers with honest names — `collect_job_artifacts` (exact, the default) and
   `detect_artifact_deltas` (the diff, opt-in via `scan: true`, and its docstring says what it cannot
   know).

2. **`RunStep: Callable[[LaunchPlan], Awaitable[int]]`.** — **fixed (R1).** The type said `int`, the
   docstring said "its exit code", and the two implementations disagreed about which int. Both now
   return the tool's own code, and the contract is stated where the callback is defined.

3. **The `_refresh_*` family.** — **half addressed.** The blanket `try/except` is gone from the five
   largest: they run under `_repaint(what)`, which stays quiet for a missing widget and names
   anything else in the harness transcript (R7). The deeper half stands: eleven methods still
   re-derive their whole widget subtree from `self`, called in varying combinations from ~15 call
   sites (`_refresh_results_chrome` calls three; `action_target_scope`'s callback calls four;
   `action_reload_recipes` calls four *different* ones). There is still no invalidation model, so the
   only way to know whether a state change repaints correctly is to trace every caller. That is
   refactor #5, and it is the reason the mixin split buys navigability and not isolation.

4. **`format_command`'s wrapper list.** `_WRAPPERS = ("timeout", "sudo", "doas", "env", "nice")`
   ([`recipes.py:290`](../fieldlog/recipes.py#L290)) silently changes whether the binary is prepended
   based on the first token of an operator's flags. Convenient, but a preset whose flags legitimately
   start with `env` gets different behaviour than one starting with `ionice`, and nothing says so.

5. **`resolve_flags` vs. the environment.** Two independent substitution mechanisms for the same
   bindings — fieldlog's `$VAR` regex, and the real env `sh` expands. `_VAR_ALIASES` exists to
   reconcile the spellings. It works, and the "unknown names are left for the shell" rule is sound,
   but the safety properties differ sharply between the two paths (allowlisted-raw vs. genuinely safe
   parameter expansion) and nothing in the code marks the boundary.

6. **`Catalog.files` vs. `file_paths` vs. `overrides` vs. `errors`.** Four provenance collections
   with a comment explaining that `files` means *scanned, not merged* because a merged-variant list
   "goes empty on the second reload". That comment is documenting a bug that was worked around by
   adding a field rather than fixing the reload.

---

## 6. Refactoring opportunities, ranked by value

### 1. Give `RunStep` an unambiguous return, and make both front-ends honour it — **done**

*Landed as: `execute_cli_job` returns the tool's own code and `handle_run` turns it into the
process exit status; step records carry `success`; `_step_table` and the TUI status band apply
`run_succeeded`.*

Return a small `StepOutcome(raw_code, ok)` instead of a bare int, or fix the CLI's `run_step` to
return `plan.job.exit_code` and let `run_chain` apply `run_succeeded` itself. Then have `_step_table`
render with `ok=`. **Highest value: it's a correctness bug in the durable artifact, it's ~15 lines,
and the two-front-end divergence is exactly what `plan_launch` was built to prevent everywhere else.**

### 2. Attribute artifacts per job — **done**

*Landed as: `archive.collect_job_artifacts` (primary log + `$OUTDIR`, no tree diff) is the
default; the preset field `scan: true` keeps `detect_artifact_deltas` for recipes that write
elsewhere. The default path no longer walks the archive at all.*

Cheapest correct-enough fix: intersect the delta with files whose mtime falls within
`[job.start, job.end]` **and** exclude paths that are another live job's `log_path` or `out_dir` (the
app already knows every running job). A fuller fix is to make `$OUTDIR` the unit of attribution and
record only it plus the primary log. Either way, add a test that runs two jobs concurrently and
asserts disjoint artifact lists.

### 3. Validate the template that will actually run — **done**

Change `is_blocked` to take the resolved-from template rather than reading `preset["flags"]` itself —
`is_blocked(tool, preset, session, flags=None)` defaulting to the preset. Then `action_run_task`
passes `self.flag_edits.get(key)` and `chain_blocked` passes the overrides. Closes R3 for both
front-ends with no new concepts.

### 4. Make `history` / `report` accept the target *or* the folder — **done**

Try `scope_dir(arg)`, then scan the workspace for a folder whose latest record has
`environment.TARGET == arg`, then suggest the near-miss. Small, and it removes the single most likely
day-to-day papercut.

### 5. Introduce one repaint entry point in the TUI

`self.invalidate("scope"|"catalog"|"job"|"layout")` dispatching to the right `_refresh_*` set, and let
the individual methods raise. Keep the `query_one` guards, drop the whole-body swallows. This is the
structural precondition for #6 and for testing any of it.

### 6. Split `FieldlogApp` along the seams it already has — **done**

*Landed as six mixins — `tui/catalog.py`, `tui/tree.py`, `tui/variants.py`, `tui/args.py`,
`tui/jobs.py`, `tui/layout.py` — leaving `app.py` (800 lines) as the widget tree, the bindings, the
lifecycle and the event dispatchers.*

Two notes for whoever picks this up next. **`TargetModal` and the interface helpers did not move**,
against the original suggestion of a `tui/scope.py`: `test_tui_interface.py` reaches them through
this module (`monkeypatch.setattr(app_mod, "get_interface_ip", …)`), which works only while the code
calling them resolves them in the same module. That was a deliberate choice in the 2026-09-15 split
and it still holds. **And mixins buy navigability, not isolation** — every one of them still reads
and writes the app's state through `self`, so this does not make a pane testable on its own. #5 is
what that would take.

### 7. Move manifest writing into `archive.py` — **done**

`_write_record` + `manifest_environment` + `MANIFEST_ENV_KEYS` are archive concerns. Removes
`chain`'s reach into a runner private and puts all `session.json` knowledge in one module alongside
the lock that protects it.

### 8. ~~Reuse `report`'s bounded reader in `archive.count_lines_safe`~~ — **withdrawn**

Rests on R5, which was withdrawn after measurement: `count_lines_safe` already streams and costs
~0.2 s and no memory on a 410 MB log. Nothing to reuse.

### 9. Reclaim finished jobs — **done**

Drop `self.jobs[key]` in `_drop_tab`, cap `system_log_lines` to a few thousand, and key
`_stdin_dismissed` by the job key rather than the per-target run id.

### 10. Return a `reason_kind` from `is_blocked`

`(blocked, kind, reason)` where kind ∈ `{binary, target, dns, lhost, interface}`. Deletes
`doctor_bucket` and frees the reason prose from being an implicit API.


---

## Two things worth resisting

The pty + `exec_form` + `_make_ctty` triangle in `runner.py` is the most carefully-reasoned code here
and every comment in it is load-bearing. And the catalog's fail-soft loading — bad file skipped with a
line number, bad regex costs the summary not the recipe, bad `success:` value costs the declaration
not the run — is a consistently applied policy that's genuinely hard to get right. Leave both alone.
