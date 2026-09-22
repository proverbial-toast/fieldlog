# fieldlog — architecture

**Status:** reference document, written 2026-09-16 from the code, and updated 2026-09-17 for the feature
pass that added `expect:`, chain summaries, the note record, the `history` views and the session transcript.
Everything here is **[confirmed]** — reproduced by running the code, or read directly from it — unless
tagged **[likely]** (inferred from reading, not executed). Line references were correct on the date of
writing; they drift.

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
  list · show · run · history · note             ├─ tui/catalog.py   lookups, blocked verdicts, reload, manager
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
  report.py ── record readers (record_ok, record_kind, …) + pure Markdown from session.json
              (imports only recipes.run_passed; cli.py and tui/jobs.py import its readers)
```

The graph is acyclic. Two edges are worth knowing about:

- `recipes.py` imports `template_vars` from `state.py` (the `$VAR` grammar lives with the session model, but
  the catalog needs it to know which variables a preset uses). `TargetSession` is a `TYPE_CHECKING` import only.
- `tui/jobs.py` imports `record_expect_found` from `report.py` (2026-09-17): the chain worker's System line
  reads a step entry the way every other reader does. `report.py` is the home of the record readers
  (`record_ok`, `record_kind`, `record_expect_found`, `chain_outcome`, `note_line`) because it was the first reader; nothing
  in it imports Textual, so the edge is cheap.

`tui/theme.py` imports nothing of fieldlog's own (guarded by `tests/test_theme_is_pure.py`); the one
palette hint that needed the catalog path lives with the palette in `tui/modals.py`. `transcript.py` is
stdlib-only and is imported by `app.py` alone: it appends the TUI's System-tab lines to
`<workspace>/fieldlog.log` (§ 8).

Textual is imported only under `app.py` and `tui/`; `cli.py` imports `fieldlog.app` lazily inside the `tui`
branch, so `fieldlog list -q` in a pipeline never pays for it (measured in the repo's own comments: a catalog
load is ~1.5 ms with libyaml, ~12 ms without).

## 2. The catalog

`recipes.load_catalog(base, dropin_dir, platform, themes_path)` →
`Catalog(tools, chains, files, file_paths, overrides, errors, themes, withheld)`.

**Sources, in load order.** The shipped `fieldlog/recipes.d/*.yaml` (19 tools, 6 chains — one file per
theme, all at base precedence), then every `*.yaml` in `$XDG_CONFIG_HOME/fieldlog/recipes.d/` (default
`~/.config/fieldlog/recipes.d/`), then `./recipes.d/` relative to the cwd. Within a directory files load in
name order, which is what the shipped files' numeric prefixes fix. `base=` takes a directory *or* a single
file (`base_files`), the latter being what the merge-rule tests hand it. It was one `fieldlog/recipes.yaml`
until the themes split; the examples and chains that lived in the repo-root `recipes.d/` were outside the
package and so reached no wheel at all — an install had 15 tools and 1 chain where a checkout had 19 and 6.
[confirmed: `tests/test_themes.py`]

**Themes.** Each shipped file names its own `theme:`. `~/.config/fieldlog/themes.yaml` (`read_themes`) is a
short list of exceptions — only names written `false` are off, an absent or empty file switches nothing, and
an unusable one switches nothing *and says so*, because a catalog that quietly shrinks is the failure this
loader exists to avoid. A theme that is off is not filtered at display time: its entries never enter the
catalog, the same disappearance `platform:` performs. `Catalog.themes` maps every shipped theme to whether
it loaded, and `Catalog.withheld` maps a `tool` / `tool/preset` key to *why* it is absent ("theme scan is
off", "not on darwin") — populated for both causes, and read by `_validate_chains` so a chain missing a step
for a known reason lands in `overrides` ("your setting") rather than `errors` ("broken catalog"). Surfaced
by `doctor` (a footer line plus `themes` in `--json`), the recipe manager, and the TUI's boot transcript. The same basename in both directories loads once:
the local file wins and the config one is not read at all (reported as "shadowed"). Editor leftovers
(`*~ .swp .swo .bak .orig .rej`) and dotfiles are skipped.

**Shape.** A tool is `{id, name?, bin?, platform?, presets: [{id, name?, flags, bin?, outdir?, parse?,
summary?, success?, expect?, scan?, platform?}]}`.

**Platforms.** `platform:` (a name or a list — `linux`, `darwin`; the vocabulary is open and compared
against the first word of `sys.platform`) makes a tool or a preset exist only on those systems. It is
applied in `_read_file`, before merging, so an entry for another platform is not written at all — the same
`tool/preset` id once per platform in one file is one entry in the catalog, never a duplicate — and `list`,
`doctor` and the TUI only ever see this platform's catalog. An empty value is the same as absent; a value
that is not a name is reported by `_validate_platform` and ignored. `load_catalog(..., platform=)` is how
tests read the shipped catalog as a Mac would. Guarded by `tests/test_platform.py`. `normalize_recipe` fills `bin` from `id`, turns a bare `bin: true` back into the string
`"true"` (YAML reads it as a boolean), and synthesises a single `default` preset for a tool written with
top-level `flags`. A chain is `{id, name?, steps: [<recipe id> | {recipe, continue?}]}`.

**Merge rules** (`_merge`, `_merge_chains`). One rule, applied by one function to the base file and to every
drop-in alike (`_read_file` returns a file's entries in order without merging anything). A new tool id adds
a tool; an existing id adds presets to it. A `tool/preset` id that already exists is replaced: across files
that is recorded in `overrides`, inside one file it is a mistake and lands in `errors` ("defined twice ·
last one kept"). A `name:` or `bin:` restated on an existing tool with a different value is ignored and
reported in `errors` (a preset-level `bin:` is the way to run one preset under another binary); restating
the same value is silent. An entry with neither `presets` nor `flags` (or an empty `presets:`) adds nothing.
Chains follow the same rule through `_merge_chains`, base file included: a repeated id replaces the earlier
chain, recorded in `overrides` across files and in `errors` inside one. Guarded by
`tests/test_merge_rules.py`.

**Validation after everything is merged**, so a base chain may name a drop-in's preset:
`_validate_chains` drops a chain whose id collides with a tool, has no steps, or names an unknown recipe;
`_validate_parsers` drops a `parse:` rule whose regex does not compile or whose `summary:` template names a
group the regex lacks (the preset survives, without a summary); `_validate_expect` drops an `expect:` regex
that does not compile (the preset survives, without its check) and quietly discards an empty one;
`_validate_success` normalises `success:` to a list of ints in 0..255 and drops anything else. Every rejection lands in `catalog.errors` with the file and,
for YAML errors, the line. A bad file costs that file; a bad rule costs the rule. This fail-soft policy is
applied consistently and is one of the two things the earlier reviews said to leave alone.

**Runnability** is one function: `check_recipe(tool, preset, session, flags=None) -> Verdict`, a frozen
dataclass of `blocked`, `kind` (`ready | binary | target | dns | lhost | interface`), `reason` and `missing`
(the value is unset, as opposed to set and refused). `is_blocked` returns just `(blocked, reason)` for the
callers that want the text. It checks, in order: the binary is on `$PATH` (via a cached per-directory listing, `is_tool_installed`);
then for each `$VAR` the template uses — `$TARGET` non-empty, no leading `-`, only `[A-Za-z0-9._:/@-]`;
`$HOST` (`session.dns_name`) non-empty, no leading `-`, only `[A-Za-z0-9._:/-]`; `$LHOST`
(`session.effective_lhost()`) non-empty, same rules; `$IFACE` no leading `-`, same allowlist (empty allowed).
A target made only of digits and dots that `ipaddress` rejects (`10.0.0.256`) is refused as "not a valid
address". `flags` is the template that will actually run when a TUI args edit differs from the preset. Every
caller (CLI `run`, `doctor`, chains, the TUI's Run button, the palette) goes through it. `check_chain`
returns the first blocked step's verdict with the step named in the reason; `chain_blocked` is its
`(blocked, reason)` form. `doctor` buckets by `verdict.kind`/`verdict.missing` and the TUI's `short_reason`
labels from the same fields, so the reason text is prose and nothing matches on it (guarded by
`tests/test_verdict.py`).

Scope values are **allowlisted, not quoted**: they are interpolated raw into a `sh` command line, so safety
rests entirely on the allowlist. `--extra-args` is appended unvalidated by design (documented as "append to
the command").

**Summaries and fields.** `parse:` is a regex run over the last 64 KB of the finished log; the *last* match
wins (a `-c 4` ping writes four lines that look like its closing stats). `summary:` is a `str.format` template
over the named groups; without it the whole match is the summary. The named groups themselves are kept on the
record as `fields`. `success:` lists the exit codes that count as success (default `[0]`);
`run_succeeded(code, codes)` is that half of the verdict. `expect:` is a regex that must match the same
64 KB tail for the run to *pass* — any match, not the last (`expect_found`); the record keeps
`expect: {pattern, found}` beside the raw exit code, with `found: null` for an interrupted run, which made
no claim (a timeout is judged on what it printed). **`run_passed(code, codes, found)`** —
`run_succeeded` and `found is not False` — is the one definition of a passing run, used by the CLI's exit
status, the chain stop policy, the TUI tab and band, and `report.record_ok` (which `history` also reads).

## 3. Scope and paths (`state.py`)

`TargetSession(target, hostname, lhost, interface, workspace_dir, artifact_root)` is the scope. `interface`
defaults to `DEFAULT_INTERFACE` (`eth0`, `en0` on Darwin), one constant every surface reads; `LOOPBACK_NAMES`
(`lo`, `lo0`) is what the TUI's picker never starts on. Derived values:

| Property | Meaning |
|---|---|
| `target_kind` | `subnet` (`/N` suffix), `address` (parses with `ipaddress`), `user@host`, else `hostname` |
| `dns_name` | `hostname` if set; else the target when it is a plain hostname; else `''`. Nothing else is inferred. |
| `effective_lhost()` | the set `lhost`, else the interface's IPv4 from `SIOCGIFADDR` (2 s cache; the ioctl number is chosen per platform, the `ifreq` offset is the same on Linux and Darwin). Resolved at spawn, never stored, so a VPN that comes up later lands the right address. |
| `slug` | `scope_dir(hostname or target)`: `/`→`_`, anything outside `[A-Za-z0-9._-]`→`-` |
| `target_dir` | `<workspace>/<slug>/` — where `session.json` lives |
| `log_dir(root)` | `<artifact_root>/<slug>/` when a log destination is set, else `<target_dir>/raw/` |

`prepare_job_paths` produces, for one run: the log `<log_dir><stamp>_<tool>_<preset>_<NN>.log` and the
`$OUTDIR` `<log_dir><stamp>_<NN>/` (this pass: the run number is part of the directory name, so two runs a
second apart never share one; a chain passes its first step's directory to every later step). If the log
destination is unwritable it falls back to `raw/` and says so. The command and the env see `$OUTDIR` through
`state.outdir_value`: relative to the target folder (the job's `cwd`) when it is inside it, so the workspace
path, which may contain spaces, never reaches the shell; absolute only under an outside log destination,
which `check_recipe` refuses (`kind="outdir"`) when it would need quoting. The job and record keep the
absolute path.

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
6. optional `timeout -k 5 Ns sh -c '<exec form>'` wrapper — the binary is `recipes.timeout_binary()`,
   coreutils `timeout` or Homebrew's `gtimeout`; `run` refuses `--timeout` when neither is installed rather
   than run unbounded (a dry run still previews);
7. capture everything the runner and the UI will need onto `ActiveJob` (paths, stamp, parse rule, expect
   rule, success codes, scan flag, chain position, note) so a later scope change cannot rewrite a running job's display.

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
  (`job.await_prompt`, which may be `''` for a whitespace-only line — the flag is `is not None`), and the
  partial line is committed to the log so the artifact reads question-then-answer. Nothing is parsed
  semantically; the next output clears the flag. The TUI raises its stdin bar for every block but moves the
  keyboard into it only when the text ends like a prompt (`?`, `:`, `]`, `)`) or the block has lasted
  `STDIN_FOCUS_AFTER` (1 s). `send_stdin` writes the reply and injects a `› reply`
  note — or `› (reply hidden)` unless the reply is one of the prompt's bracketed choices (`[y/N]`);
- on exit: `shell_exit_code` (signal death → 128+N), then one read of the log tail for both the summary
  and fields (`parse:`) and the expectation (`expect:`), artifact attribution (§6), then `_append_manifest` builds the record, stores it on `job.record`, and
  `archive.append_record` writes it;
- on cancellation (the TUI quitting): closing the pty master hangs up the child's controlling terminal
  (SIGHUP, the end of most tools), then SIGTERM to the process group — or SIGKILL if `kill_job` had already
  been asked for this job (`job.kill_requested`), since the SIGKILL it scheduled dies with the loop.

Interrupt is `os.killpg(SIGINT)` (`interrupt_job`); the TUI's kill adds SIGKILL after 10 s (`kill_job`).

**D. `run_chain(session, catalog, chain, *, run_step, timeout, flags_overrides, note)`** — inverted control.
The driver plans each step lazily (a halted chain reserves no numbers for steps that never ran), shares the
first step's `$OUTDIR` with every later step, and calls the front-end's `run_step(plan) -> exit code`
coroutine — the CLI streams to the terminal, the TUI opens a tab. Policy: stop at the first step that did not
pass (`run_passed`: exit code and expectation) unless it says `continue: true`; an interrupt stops the chain
with code 130 whatever the step says; the chain's exit code is the first failing step's code, or 1 when that
step exited 0 and only its expectation failed. The summary record (`recipe: chain/<id>`, `steps: [{id,
recipe, exit_code, success?, summary?, expect?}]`, `stopped_at`, shared `out_dir`, and `summary` — the
steps' summaries prefixed with their tool id and joined with `→`, when any step had one) claims its run
number **last**, so step numbers are contiguous.

## 5. State

| Tier | Where | Lifetime |
|---|---|---|
| Scope | `TargetSession` | process; the TUI persists it to `<workspace>/.last-scope.json` whenever the `T` form is saved and again on unmount (only an operator-set `lhost` is saved) |
| Job | `ActiveJob` — id, paths, pty fd, queue, counts, exit, summary, fields, note, record, prompt state | one run; the TUI drops it with its tab |
| Catalog | `Catalog` in the process; `Shift+R` reloads | process |
| Durable | `<workspace>/<slug>/session.json`, `raw/`, `.run-counter`, `.session.lock`; `<workspace>/.pinned-recent.json`; `<workspace>/fieldlog.log` (the TUI's transcript) | forever |

Process-global caches: the interface IP (2 s), the `$PATH` directory listings and the `is_tool_installed`
LRU (cleared together by `clear_tool_cache`).

Concurrency: one event loop per process, no threads. The CLI runs one job under `asyncio.run`; the TUI runs
N jobs as non-exclusive Textual workers. Cross-process safety is on disk (§6).

## 6. The archive

```text
<workspace>/                          ./targets by default
├── .last-scope.json                  TUI's remembered scope
├── .pinned-recent.json               pinned + last six launched from the TUI
├── fieldlog.log                      the TUI's System transcript, one stamped line per event
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
other's record. This is O(records) per append under the lock, deliberately: the format is fine to a few thousand
records per target, and JSONL is deferred until a real target folder gets slow.

**Artifact attribution.** By default a run records its own log plus whatever appeared or changed in its
`$OUTDIR` since the run began (`collect_job_artifacts`, which never walks the archive; the pre-snapshot of
that one directory is what lets chain steps sharing a directory attribute per step). `scan: true` opts into the
older whole-folder before/after diff (`detect_artifact_deltas`), which is only accurate when runs are
serialised. Each artifact carries `path` (relative to the target folder, absolute when outside it), `bytes`,
`lines` (None for binary) and `binary`.

**A note record** (`fieldlog note`) is `{id, recipe: "note", note, start_time}` and nothing else: no
command, exit code, log or artifacts. It takes a run number from the same counter. `report.record_kind`
tells the three kinds apart — `chain` (a non-empty `steps`), `note` (`recipe == "note"` and no `command`),
else `run` — and every reader branches on it.

**A run record** (all times naive local ISO-8601):

```json
{"id": "04", "recipe": "ping/quick", "command": "ping -c 4 -W 1 10.0.0.1",
 "environment": {"TARGET": "...", "TARGET_IP": "...", "TARGET_HOST": "", "LHOST": "...", "IFACE": "eth0", "OUT_DIR": "...", "RUN_ID": "04"},
 "artifact_log": "/abs/.../raw/20260916T120000_ping_quick_04.log", "out_dir": "/abs/.../raw/20260916T120000_04",
 "start_time": "2026-09-16T12:00:00.1", "end_time": "2026-09-16T12:00:03.2", "duration_sec": 3.1,
 "exit_code": 0, "summary": "4 replies · 0% loss", "fields": {"rx": "4", "loss": "0"},
 "artifacts": [{"path": "raw/20260916T120000_ping_quick_04.log", "lines": 9, "bytes": 425}]}
```

Optional keys: `interrupted`, `note`, `success`, `expect: {pattern, found}`, `chain: {id, step, of}`. A chain
summary has `steps`, `stopped_at`, an empty `artifact_log`, no artifacts, and `summary` when any step had one.

## 7. Readers of the archive

- `history` with no argument lists folders with their run count and last record (time, recipe, summary or
  note); with one, that folder's records: id, recipe, exit (with `(ok)` when a declared success is non-zero,
  `(expect not met)` for a missed expectation — the same `exit_label` and `format_time` as `report`),
  duration, start, summary, note, artifacts; a note record shows its time and text only.
  `--fields` prints a table instead — `#`, started, exit label, then one column per field name in first-seen
  order — the trend view. `--json` dumps the (filtered) records. `find_target_dir` accepts the folder name or
  the target the runs were made against (it scans manifests on a miss).
- `note` writes a note record through `archive.append_note`; the folder is resolved as `history` resolves it
  and created when the name is new.
- `report` renders Markdown: a summary table (with a Summary column), then a section per run with the
  command, timings, summary, the expectation when it was missed, note, artifacts and a code-fenced tail of
  the log (default 40 lines; `--full`). Exit cells and headings read `0 (expect not met)` for a missed
  expectation and are bold for any run `record_ok` rejects. A log over 4 MB is read from
  the end in a growing window rather than whole. Fences are chosen longer than any backtick run in the output;
  table cells escape `|`. Chain summaries render as a step table.
- **Both readers take the same two filters**, through `cli.filter_runs`: `--recipe <key>` (exact; `chain/<id>`
  and `note` count — a prefix match would make `ping` the tool here and the preset everywhere else) and
  `--since WHEN` (`cli.parse_since`: a date, a date and time, a span such as `3h`, or `today`/`yesterday`,
  compared against each record's naive local `start_time`; a record with none readable drops out). They
  compose. `cli.filter_note` renders what was narrowed, which `render_report(selection=…)` puts in the
  report's summary line — a filtered report is read away from the command that made it and must not pass as
  the whole archive. [confirmed: `tests/test_reader_filters.py`]
- **One manifest reader.** `report.read_manifest` returns `Manifest(runs, problem, readable)`. A folder with
  no `session.json` holds no runs and has no problem; one that will not parse is `readable=False` and stops
  `history`/`report` with **exit 2** and the reason on stderr; one that parses with non-record entries keeps
  the records and warns. The workspace overview marks an unreadable folder instead of printing `0 runs`.
  `load_runs` is the runs-only form for callers with nothing to say. Before this there were three answers:
  `report` rendered `No runs recorded.` and exited 0, the overview said `0 runs`, and `history` parsed the
  file itself and printed a raw exception. [confirmed: `tests/test_manifest_problems.py`]
- `doctor` runs `is_blocked` over the whole catalog against a scope and buckets the reasons (missing binary
  vs. scope value), with `--json`.
- The TUI reads nothing back from the archive (a deliberate gap), but it writes one more thing to
  it: every System-tab line, through `write_system_log` → `transcript.append_transcript`, to
  `<workspace>/fieldlog.log`. An unwritable workspace is said once in the System tab and then left alone.

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

Selection: `selected_recipe()` (in `tui/catalog.py`) resolves `selected_tool_id`/`selected_preset_id`
against the catalog **and writes the answer back**, so the two ids always name a recipe that exists; every
reader used to absorb a stale id with its own silent fallback. On that, `_rebuild_tree` keeps one invariant:
*the row under the cursor is the row that names what VARIANTS and ARGS are painting.* `_place_cursor` settles
it in order — the selection moved elsewhere (palette, recipe manager) and a row names it, go there; the row
the cursor was on is still listed, follow it; neither, take the first row and repaint the panes from it.
`[!]` and the filter box no longer reset the cursor to 0. Before this the two could come apart in both
directions, and Enter in VARIANTS ran what the highlight did not show. [confirmed:
`tests/test_cursor_and_panes_agree.py`]

Repaint model: a family of `_refresh_*` methods each re-derives its widget subtree from `self`, called in
varying combinations from ~15 call sites. `_repaint(what)` keeps a failure from taking the app down and names
it in the System transcript unless it is only a missing widget. There is no invalidation model — the open
refactor #5 from the previous review.

> **Worth pursuing later: make the selection a reactive, and let the panes follow it.** The
> cursor/selection drift fixed above was not one bug but the shape of this gap. Every caller that changes
> what is selected — a tree row, the palette, the recipe manager, a variant step, a reload, a rebuild that
> hid the row underneath — has to remember which `_refresh_*` calls go with it, and each one remembers a
> slightly different list: `select_tool` calls three, `variant_row_clicked` calls two plus a focus repaint,
> `action_toggle_hide_missing` called none and left the panes behind. `selected_recipe` and `_place_cursor`
> settle *what* is selected in one place; *who repaints when it changes* is still spread across the call
> sites. Textual already has the mechanism: make `selected_tool_id`/`selected_preset_id`/`selected_chain_id`
> `reactive`s with a `watch_` that repaints VARIANTS, ARGS and the tree highlight, and the callers go back to
> assigning a value. It is worth doing when a pane is next added or a third thing learns to change the
> selection, and not before — it touches every mixin at once, and the invariant it would enforce is now
> guarded by tests either way (`tests/test_cursor_and_panes_agree.py`), which is what makes the refactor
> safe to attempt later rather than urgent now.

Jobs: `_spawn_job` → `plan_launch` → `_open_job_tab` (tab, RichLog, transcript) → worker `_run` →
`run_job` with a sink that writes the first 500 lines to the RichLog and caps the in-memory copy. A chain
worker awaits each step's `_run` in turn. Closing a running tab asks kill or detach; a detached job keeps
writing to disk with nothing in the app reading it again.

## 9. Tests and CI

714 tests in 63 files (376 in 35 when this document was written), ~35 s. Three tiers: pure
unit tests (parsing, scope characters, arg tokens, path lookup); filesystem integration tests on a
`tmp_workspace` fixture that run real `true`/`false`/`echo`/`sh` tools through `plan_launch` + `run_job` or
`handle_run`; and async tests that drive the real app through `app.run_test()`. `test_app_structure.py`
guards the mixin assembly. CI: ruff + pytest on 3.11/3.12/3.13 on Ubuntu and macOS (the macOS leg is what
proves the Darwin ioctl number, through the loopback test in `tests/test_platform.py`), plus a job that
builds the wheel, installs it clean and imports `fieldlog.app` (a hand-listed `packages` once shipped without
`fieldlog.tui`).

## 10. Invariants worth protecting

1. **One launch path.** Neither front-end builds a command; `plan_launch` does.
2. **The exit code is the tool's own.** Verdicts (`success:`, `expect:`) are recorded beside it, never in
   place of it, and `run_passed` is the one place they are combined.
3. **Paths are captured at spawn; `$LHOST` is resolved at spawn.** Opposite choices, both deliberate.
4. **Scope values are allowlisted.** Anything interpolated into the shell goes through `is_blocked`.
5. **Fail-soft catalog.** A bad file, rule or value costs itself, never the catalog.
6. **Secrets never reach the log.** Only a prompt's bracketed choices are logged as replies.
7. **The archive is append-only and cross-process safe.** Numbers under the lock; records under the lock.
8. **An unreadable archive is never rendered as an empty one.** `read_manifest` is the one reader, and
   "nothing was run here" and "this cannot be read" are different answers with different exit codes.
   Nor is one ever written over: `append_record` renames it `session.json.unreadable-<stamp>` and
   starts afresh, and `read_manifest` warns while that file exists.
9. **The TUI's highlight is what runs.** The RECIPES cursor and the VARIANTS/ARGS panes always name the
   same recipe; `_place_cursor` is where that is settled.
