# fieldlog — architecture

**Status:** reference document, written 2026-09-16 from the code on branch
`fix/archive-correctness-and-tui-split`, after the changes described in `problems.md` § "Fixed in this pass".
Everything here is **[confirmed]** unless tagged otherwise.

fieldlog runs catalogued diagnostic commands against a target and keeps a per-target archive of every run.
The archive is the product; the TUI and CLI are two front-ends to the same launch path. This document
describes how that actually fits together.

---

## 1. Modules and dependency direction

```text
fieldlog/__main__.py ─► cli.main
                          │
        ┌─────────────────┴──────────────────────┐
        ▼                                        ▼
     cli.py                                   app.py  (FieldlogApp = 6 mixins + textual.App)
  list · show · run · history                    ├─ tui/catalog.py   lookups, blocked verdicts, reload, manager
  report · doctor · tui                          ├─ tui/tree.py      the RECIPES list and its cursor
        │                                        ├─ tui/variants.py  the VARIANTS pane
        │                                        ├─ tui/args.py      substitution, token view, raw editor
        │                                        ├─ tui/jobs.py      tabs, status band, stdin bar, workers
        │                                        ├─ tui/layout.py    split/stacked, focus, hotkey bar
        │                                        └─ tui/{modals,widgets,theme,models,helpers}.py
        │                                        │
        └──────────────────┬─────────────────────┘
                           ▼
                      chain.py ── run_chain(): step driver, front-end agnostic
                           │
                      launch.py ── plan_launch(): the ONE place a command, env and paths are built
                           │
      ┌────────────────────┼─────────────────────┬──────────────────────┐
      ▼                    ▼                     ▼                      ▼
  recipes.py           state.py             archive.py              runner.py
  catalog, YAML,       TargetSession,       run numbers + lock,     pty, streaming,
  merge, validation,   ActiveJob, paths,    artifact attribution,   prompt heuristic,
  is_blocked, parse,   $VAR grammar         session.json writer     manifest record
  success, search
      ▲
  report.py ── pure Markdown from session.json (imports only recipes.run_succeeded)
```

The graph is acyclic. Two edges point the "wrong" way and are worth knowing about:

- `recipes.py` imports `template_vars` from `state.py` (the `$VAR` grammar lives with the session model, but
  the catalog needs it to know which variables a preset uses). `TargetSession` is a `TYPE_CHECKING` import only.
- `tui/theme.py` imports `RECIPES_PATH` from `recipes.py` to fill one palette hint, so the palette cannot be
  loaded without YAML being importable.

Textual is imported only under `app.py` and `tui/`; `cli.py` imports `fieldlog.app` lazily inside the `tui`
branch, so `fieldlog list -q` in a pipeline never pays for it (measured in the repo's own comments: a catalog
load is ~1.5 ms with libyaml, ~12 ms without).

## 2. The catalog

`recipes.load_catalog(base, dropin_dir)` → `Catalog(tools, chains, files, file_paths, overrides, errors)`.

**Sources, in load order.** The shipped `fieldlog/recipes.yaml` (14 tools, 1 chain), then every `*.yaml` in
`$XDG_CONFIG_HOME/fieldlog/recipes.d/` (default `~/.config/fieldlog/recipes.d/`), then `./recipes.d/` relative
to the cwd. Within a directory files load in name order. The same basename in both directories loads once:
the local file wins and the config one is not read at all (reported as "shadowed"). Editor leftovers
(`*~ .swp .swo .bak .orig .rej`) and dotfiles are skipped.

**Shape.** A tool is `{id, name?, bin?, presets: [{id, name?, flags, bin?, outdir?, parse?, summary?,
success?, scan?}]}`. `normalize_recipe` fills `bin` from `id`, turns a bare `bin: true` back into the string
`"true"` (YAML reads it as a boolean), and synthesises a single `default` preset for a tool written with
top-level `flags`. A chain is `{id, name?, steps: [<recipe id> | {recipe, continue?}]}`.

**Merge rules** (`_merge`, `_merge_chains`). A new tool id adds a tool; an existing id adds presets to it. A
preset or chain id that already exists is replaced and recorded in `overrides`. A drop-in cannot change an
existing tool's `name` or `bin` (silently ignored — see `problems.md`). Within one file, a duplicate tool id
merges presets and a duplicate preset id keeps the first.

**Validation after everything is merged**, so a base chain may name a drop-in's preset:
`_validate_chains` drops a chain whose id collides with a tool, has no steps, or names an unknown recipe;
`_validate_parsers` drops a `parse:` rule whose regex does not compile or whose `summary:` template names a
group the regex lacks (the preset survives, without a summary); `_validate_success` normalises `success:` to a
list of ints in 0..255 and drops anything else. Every rejection lands in `catalog.errors` with the file and,
for YAML errors, the line. A bad file costs that file; a bad rule costs the rule. This fail-soft policy is
applied consistently and is one of the two things the earlier reviews said to leave alone.

**Runnability** is one function: `is_blocked(tool, preset, session, flags=None) -> (blocked, reason)`.
It checks, in order: the binary is on `$PATH` (via a cached per-directory listing, `is_tool_installed`);
then for each `$VAR` the template uses — `$TARGET` non-empty, no leading `-`, only `[A-Za-z0-9._:/@-]`;
`$HOST` (`session.dns_name`) non-empty, no leading `-`, only `[A-Za-z0-9._:/-]`; `$LHOST`
(`session.effective_lhost()`) non-empty, same rules; `$IFACE` no leading `-`, same allowlist (empty allowed).
A target made only of digits and dots that `ipaddress` rejects (`10.0.0.256`) is refused as "not a valid
address". `flags` is the template that will actually run when a TUI args edit differs from the preset. Every
caller (CLI `run`, `doctor`, chains, the TUI's Run button, the palette) goes through it. The reason strings
are a small protocol: `reason_kind(reason)` (`ready | binary | target | dns | lhost | interface | other`) and
`reason_missing(reason)` (a "needs …" reason, as opposed to a refused value) sit beside `is_blocked` and are
the only place the prose is matched; `doctor` buckets with them and the TUI's `short_reason` labels with them
(see `problems.md` § 2.4).

Scope values are **allowlisted, not quoted**: they are interpolated raw into a `sh` command line, so safety
rests entirely on the allowlist. `--extra-args` is appended unvalidated by design (documented as "append to
the command").

**Summaries and fields.** `parse:` is a regex run over the last 64 KB of the finished log; the *last* match
wins (a `-c 4` ping writes four lines that look like its closing stats). `summary:` is a `str.format` template
over the named groups; without it the whole match is the summary. The named groups themselves are kept on the
record as `fields` (this pass). `success:` lists the exit codes that count as success (default `[0]`);
`run_succeeded(code, codes)` is the single definition every reader uses.

## 3. Scope and paths (`state.py`)

`TargetSession(target, hostname, lhost, interface, workspace_dir, artifact_root)` is the scope. Derived values:

| Property | Meaning |
|---|---|
| `target_kind` | `subnet` (`/N` suffix), `address` (parses with `ipaddress`), `user@host`, else `hostname` |
| `dns_name` | `hostname` if set; else the target when it is a plain hostname; else `''`. Nothing else is inferred. |
| `effective_lhost()` | the set `lhost`, else the interface's IPv4 from `SIOCGIFADDR` (2 s cache). Resolved at spawn, never stored, so a VPN that comes up later lands the right address. |
| `slug` | `scope_dir(hostname or target)`: `/`→`_`, anything outside `[A-Za-z0-9._-]`→`-` |
| `target_dir` | `<workspace>/<slug>/` — where `session.json` lives |
| `log_dir(root)` | `<artifact_root>/<slug>/` when a log destination is set, else `<target_dir>/raw/` |

`prepare_job_paths` produces, for one run: the log `<log_dir><stamp>_<tool>_<preset>_<NN>.log` and the
`$OUTDIR` `<log_dir><stamp>_<NN>/` (this pass: the run number is part of the directory name, so two runs a
second apart never share one; a chain passes its first step's directory to every later step). If the log
destination is unwritable it falls back to `raw/` and says so.

Variables: `$NAME` / `${NAME}` whole names only. Canonical names `TARGET HOST IFACE LHOST OUTDIR`, with
aliases `TARGET_IP→TARGET`, `TARGET_HOST→HOST`, `OUT_DIR→OUTDIR`. `resolve_flags` substitutes exactly what
`template_vars` detects, and everything is also exported into the child's environment under both spellings,
so shell forms like `${RIFACE:-eth0}` and scripts reading `$OUTDIR` still work. `$RUN_ID` is env-only.
A caller with no run in hand (`show`) gets `$OUTDIR` previewed as `raw/<stamp>_NN/`; the TUI's ARGS band
previews the real next number through `archive.peek_run_number`, which reads only `.run-counter` and
reserves nothing.

## 4. The four execution paths

**A. `fieldlog run <recipe> <target>`** — `cli.dispatch_argv` normalises aliases and turns any unknown first
token into `run <token>` (so `fieldlog ping/quick 10.0.0.1` works, and a typo'd subcommand gets a "did you
mean" hint after the recipe lookup fails) → argparse → `handle_run` → `find_chain` first (chain ids cannot
collide with tool ids) → `find_recipe` → `is_blocked` gate → `plan_launch` → `asyncio.run(execute_cli_job)` →
`run_job`. The process exit status is the recipe's *verdict* (`run_succeeded`); the archive keeps the tool's
own code.

**B. `plan_launch(session, tool, preset, *, flags_override, extra_args, timeout, dry_run, stamp, chain,
out_dir, note)` → `LaunchPlan(job, command, env, timeout, warnings)`** — the single funnel:

1. reserve the run number under the archive lock (`--dry-run` previews without reserving);
2. `prepare_job_paths` (creates the log file unless dry-run);
3. `resolve_flags` on the **unresolved template** (the preset's flags, or the TUI edit), then append
   `extra_args` unsubstituted;
4. `format_command`: prepend `bin` unless the flags already start with it or with a wrapper
   (`timeout sudo doas env nice`);
5. create `$OUTDIR` if the template mentions it or the preset says `outdir: true`;
6. optional `timeout -k 5 Ns sh -c '<exec form>'` wrapper;
7. capture everything the runner and the UI will need onto `ActiveJob` (paths, stamp, parse rule, success
   codes, scan flag, chain position, note) so a later scope change cannot rewrite a running job's display.

**C. `run_job(command, job, session, sink, on_state, env, echo)`** — the systems core:

- opens a pty (24×200; echo off for the TUI, on for the CLI so the tool decides what is visible — sudo hides,
  ssh shows);
- `create_subprocess_shell(exec_form(command))` with stdin/out/err on the pty slave, cwd = `target_dir`,
  and a `preexec_fn` that calls `setsid()` and `TIOCSCTTY`. `exec_form` prefixes `exec` when the command is one
  simple command, so the tool — not dash — is the process waited on and its exit code is the one recorded; a
  pipeline or list keeps the shell. The controlling tty is why `/dev/tty` prompts (sudo, ssh) work;
  `setsid` is why `killpg(pid)` reaches the whole job;
- `loop.add_reader(master)` feeds an `asyncio.Queue`; the loop splits chunks into lines, writes the
  ANSI-stripped line to the log (line-buffered) and hands the raw line to the front-end's `sink`;
- **prompt heuristic:** a partial line followed by 0.4 s of silence marks the job "awaiting input"
  (`job.await_prompt`), and the partial line is committed to the log so the artifact reads
  question-then-answer. Nothing is parsed semantically. `send_stdin` writes the reply and injects a `› reply`
  note — or `› (reply hidden)` unless the reply is one of the prompt's bracketed choices (`[y/N]`);
- on exit: `shell_exit_code` (signal death → 128+N), summary and fields from the log tail, artifact
  attribution (§6), then `_append_manifest` builds the record, stores it on `job.record`, and
  `archive.append_record` writes it;
- on cancellation (the TUI quitting): SIGTERM to the process group.

Interrupt is `os.killpg(SIGINT)` (`interrupt_job`); the TUI's kill adds SIGKILL after 10 s (`kill_job`).

**D. `run_chain(session, catalog, chain, *, run_step, timeout, flags_overrides, note)`** — inverted control.
The driver plans each step lazily (a halted chain reserves no numbers for steps that never ran), shares the
first step's `$OUTDIR` with every later step, and calls the front-end's `run_step(plan) -> exit code`
coroutine — the CLI streams to the terminal, the TUI opens a tab. Policy: stop at the first step that is not a
success unless it says `continue: true`; an interrupt stops the chain with code 130 whatever the step says;
the chain's exit code is the first non-success it saw. The summary record (`recipe: chain/<id>`, `steps:
[{id, recipe, exit_code, success?}]`, `stopped_at`, shared `out_dir`) claims its run number **last**, so step
numbers are contiguous.

## 5. State

| Tier | Where | Lifetime |
|---|---|---|
| Scope | `TargetSession` | process; the TUI persists it to `<workspace>/.last-scope.json` on unmount (only an operator-set `lhost` is saved) |
| Job | `ActiveJob` — id, paths, pty fd, queue, counts, exit, summary, fields, note, record, prompt state | one run; the TUI drops it with its tab |
| Catalog | `Catalog` in the process; `Shift+R` reloads | process |
| Durable | `<workspace>/<slug>/session.json`, `raw/`, `.run-counter`, `.session.lock`; `<workspace>/.pinned-recent.json` | forever |

Process-global caches: the interface IP (2 s), the `$PATH` directory listings and the `is_tool_installed`
LRU (cleared together by `clear_tool_cache`).

Concurrency: one event loop per process, no threads. The CLI runs one job under `asyncio.run`; the TUI runs
N jobs as non-exclusive Textual workers. Cross-process safety is on disk (§6).

## 6. The archive

```text
<workspace>/                          ./targets by default
├── .last-scope.json                  TUI's remembered scope
├── .pinned-recent.json               pinned + last six launched from the TUI
└── <slug>/                           dns name if set, else the target
    ├── session.json                  JSON array, one record per run and per chain summary
    ├── .run-counter                  highest run number handed out (≥ highest recorded)
    ├── .session.lock                 flock sidecar (a lock on session.json would be stranded by os.replace)
    └── raw/
        ├── 20260912T180156_ping_quick_01.log
        └── 20260912T180156_01/       that run's $OUTDIR, if it used one
```

**Run numbers** are reserved under the lock and the counter stores *highest handed out*
(`max(issued, highest recorded) + 1` at each reservation), so a job still running in another process never
has its number reissued, and the counter is never below any recorded id — which is what lets a preview
read it alone. **Records** are appended by read-append-replace
under the same lock (tmp file + `os.replace`), so a CLI run and the TUI finishing together cannot drop each
other's record. This is O(n) per append; see `problems.md` for the trade-off.

**Artifact attribution.** By default a run records its own log plus whatever appeared or changed in its
`$OUTDIR` since the run began (`collect_job_artifacts`, which never walks the archive; the pre-snapshot of
that one directory is what lets chain steps sharing a directory attribute per step). `scan: true` opts into the
older whole-folder before/after diff (`detect_artifact_deltas`), which is only accurate when runs are
serialised. Each artifact carries `path` (relative to the target folder, absolute when outside it), `bytes`,
`lines` (None for binary) and `binary`.

**A record** (all times naive local ISO-8601):

```json
{"id": "04", "recipe": "ping/quick", "command": "ping -c 4 -W 1 10.0.0.1",
 "environment": {"TARGET": "...", "TARGET_IP": "...", "TARGET_HOST": "", "LHOST": "...", "IFACE": "eth0", "OUT_DIR": "...", "RUN_ID": "04"},
 "artifact_log": "/abs/.../raw/20260916T120000_ping_quick_04.log", "out_dir": "/abs/.../raw/20260916T120000_04",
 "start_time": "2026-09-16T12:00:00.1", "end_time": "2026-09-16T12:00:03.2", "duration_sec": 3.1,
 "exit_code": 0, "summary": "4 replies · 0% loss", "fields": {"rx": "4", "loss": "0"},
 "artifacts": [{"path": "raw/20260916T120000_ping_quick_04.log", "lines": 9, "bytes": 425}]}
```

Optional keys: `interrupted`, `note`, `success`, `chain: {id, step, of}`. A chain summary has `steps`,
`stopped_at`, an empty `artifact_log` and no artifacts.

## 7. Readers of the archive

- `history` lists folders (with run counts) or one folder's runs: id, recipe, exit (with `(ok)` when a
  declared success is non-zero), duration, start, summary, note, artifacts. `--json` dumps the records.
  `find_target_dir` accepts the folder name or the target the runs were made against (it scans manifests on
  a miss).
- `report` renders Markdown: a summary table, then a section per run with the command, timings, summary,
  note, artifacts and a code-fenced tail of the log (default 40 lines; `--full`). A log over 4 MB is read from
  the end in a growing window rather than whole. Fences are chosen longer than any backtick run in the output;
  table cells escape `|`. Chain summaries render as a step table. `--since N` filters by run number.
- `doctor` runs `is_blocked` over the whole catalog against a scope and buckets the reasons (missing binary
  vs. scope value), with `--json`.
- The TUI reads nothing back from the archive (see `opportunities.md`).

## 8. The TUI

`FieldlogApp` composes the widget tree in `compose()` and inherits its behaviour from six mixins. They are
mixins, not widgets: every one reads and writes the app's state through `self` and queries its widget tree,
so the split buys navigability, not isolation. State the mixins share: `session`, `catalog`/`_recipes`,
`selected_tool_id`/`selected_preset_id`/`selected_chain_id`, `flag_edits` (per-recipe unresolved
templates), `jobs` (keyed by a monotonically increasing job sequence, not the per-target run number),
`tabs`/`active_tab_id`, `pinned`/`recent`, `filter_text`, `hide_missing`, layout fields.

Layout: a top bar (scope, interface, layout, job count); RECIPES over VARIANTS on the left; RESULTS (tab
strip, status band, pinned command/artifact, a `ContentSwitcher` of one `RichLog` per tab, the stdin bar) on
the right; a full-width ARGS band; an optional hotkey bar. Under 120 columns the left column becomes an
accordion ("stacked").

Repaint model: a family of `_refresh_*` methods each re-derives its widget subtree from `self`, called in
varying combinations from ~15 call sites. `_repaint(what)` keeps a failure from taking the app down and names
it in the System transcript unless it is only a missing widget. There is no invalidation model — the open
refactor #5 from the previous review.

Jobs: `_spawn_job` → `plan_launch` → `_open_job_tab` (tab, RichLog, transcript) → worker `_run` →
`run_job` with a sink that writes the first 500 lines to the RichLog and caps the in-memory copy. A chain
worker awaits each step's `_run` in turn. Closing a running tab asks kill or detach; a detached job keeps
writing to disk with nothing in the app reading it again.

## 9. Tests and CI

376 tests in 35 files after this pass (305 in 27 before), ~11 s. Three tiers: pure
unit tests (parsing, scope characters, arg tokens, path lookup); filesystem integration tests on a
`tmp_workspace` fixture that run real `true`/`false`/`echo`/`sh` tools through `plan_launch` + `run_job` or
`handle_run`; and async tests that drive the real app through `app.run_test()`. `test_app_structure.py`
guards the mixin assembly. CI: ruff + pytest on 3.11/3.12/3.13, plus a job that builds the wheel, installs it
clean and imports `fieldlog.app` (a hand-listed `packages` once shipped without `fieldlog.tui`).

## 10. Invariants worth protecting

1. **One launch path.** Neither front-end builds a command; `plan_launch` does.
2. **The exit code is the tool's own.** Verdicts (`success:`) are recorded beside it, never in place of it.
3. **Paths are captured at spawn; `$LHOST` is resolved at spawn.** Opposite choices, both deliberate.
4. **Scope values are allowlisted.** Anything interpolated into the shell goes through `is_blocked`.
5. **Fail-soft catalog.** A bad file, rule or value costs itself, never the catalog.
6. **Secrets never reach the log.** Only a prompt's bracketed choices are logged as replies.
7. **The archive is append-only and cross-process safe.** Numbers under the lock; records under the lock.
