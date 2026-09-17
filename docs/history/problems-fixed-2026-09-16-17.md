# fieldlog — problems fixed in the 2026-09-16 and 2026-09-17 passes

**Status:** historical, kept as written. Moved out of `problems.md` on 2026-09-17 so that file holds only
what is still open; these sections are the record of what each pass found, reproduced and fixed, and the
test that guards each fix. Section numbers are the ones other documents cite (`§ 1.x`, `§ 2.x`). The tags
are the ones in `../README.md`.

---

## 1. Fixed in the 2026-09-16 pass

Each of these was reproduced by execution before it was fixed; the tests named are the regression guards.

### 1.1 Hostnames made of hex letters were treated as addresses — **[confirmed, fixed]**

`TargetSession.target_kind` decided "address" with `^[0-9a-fA-F:.]+$`, so `dc1`, `cafe`, `db2`, `ad`, `fe1`
— all plausible hostnames — were addresses, and `dns_name` (which stands the target in for `$HOST` only when
the target is a hostname) was empty. `fieldlog run dig/a dc1` refused with "needs a dns name".

```text
dc1   kind=address  dns_name=''      # before
dc1   kind=hostname dns_name='dc1'   # after
```

Fix: classify with `ipaddress.ip_address`. Test: `tests/test_target_kind.py`.

### 1.2 A scope value starting with `-` was an argument injection — **[confirmed, fixed]**

`fieldlog run ping/quick --target=-f --dry-run` built `ping -c 4 -W 1 -f`: the target became a flood-ping
flag. The allowlist correctly permits `-` inside a value (`box-1`), but nothing refused a *leading* one, and
scope values are pasted after the tool's own flags. Operator-supplied, so a footgun rather than a
vulnerability, but the one hole in an otherwise complete validation story. Fix: `is_blocked` refuses a
leading `-` on target, dns name, local address and interface with a distinct reason. Tests in
`tests/test_scope_chars.py`.

While there: `doctor` showed every scope-bucket block as "needs a target" even when the reason was "target
has unsafe characters (;)". It now shows the terse note only for "needs …" reasons.

### 1.3 Three shipped curl recipes could never run — **[confirmed, fixed]**

`curl/timing80`, `curl/timing443` and `curl/loop` used `-w @fmt`, which tells curl to read its output format
from a file named `fmt` in the working directory. No such file exists anywhere in the project, and the job's
cwd is the target folder:

```console
$ curl -o /dev/null -s -w @fmt http://127.0.0.1:1/
curl: option -w: error encountered when reading a file      # exit 26
```

The formats are now inline (as `curl/trace` and the examples already did), and `timing*` gained a parse rule.
`tests/test_base_catalog.py` loads the shipped catalog and asserts no preset references `@fmt`.

### 1.4 `--artifact-root` named its folder after the target, the workspace after the dns name — **[confirmed, fixed]**

With `-t 10.0.0.1 -H box.htb --artifact-root /x`, `session.json` went to `targets/box.htb/` and the logs to
`/x/10.0.0.1/`. The README said both are `<name>` ("DNS name if set, else the target"). `log_dir` now uses the
same slug as the workspace folder. Test in `tests/test_artifact_root.py`.

### 1.5 Two runs started in the same second shared one `$OUTDIR` — **[confirmed, fixed, layout change]**

`$OUTDIR` was `raw/<stamp>/` with a one-second stamp. Two `plan_launch` calls in a row returned the same
directory for runs `01` and `02`; two `tcpdump/host` runs launched a second apart (Enter, Enter in the TUI)
would both write `capture.pcap` there and the second would overwrite the first. The attribution code even
documented the case ("runs launched in the same second also share a stamp") without preventing the overwrite.

Fix: `$OUTDIR` is `raw/<stamp>_<NN>/`, named for the run. A chain's later steps receive the first step's
directory explicitly (`plan_launch(..., out_dir=)`), so sharing is a deliberate act of the caller rather than a
coincidence of the clock. The log filename was already unique. Records written before this change keep their
old paths; nothing reads the layout back. Tests: `tests/test_outdir_per_run.py`.

### 1.6 `fieldlog run --json` printed a shape that was not the run record — **[confirmed, fixed]**

The README promises "the run record"; the chain path printed the real summary record, the single-run path
printed a hand-built dict missing `start_time`, `end_time`, `environment`, `summary`, `success`,
`interrupted` and `out_dir`, and spelling the log `primary_log`. The runner now keeps the record it archived
on `job.record` and the CLI prints exactly that. Test: `tests/test_run_json.py`.

### 1.7 The TUI's Recent list never re-ordered — **[confirmed from code, fixed]**

`_remember` inserted only keys not already present, so once six entries existed a re-launched older recipe
never moved to the front. Test: `tests/test_recent.py`.

### 1.8 The palette labelled every scope block "needs dns name" — **[confirmed from code, fixed]**

`PaletteModal._render_item` hard-coded the label; a missing target, an unsafe value or a missing local address
all read "needs dns name". It now shows the real reason from `is_blocked`.

### 1.9 Found by the independent second pass — **[confirmed, fixed]**

After the eight fixes above landed, a fresh review of the diff (a separate agent, reading only the change)
found and reproduced the following, all fixed in the same pass:

- **`run --json` could print a record that was never archived.** `job.record` was assigned before
  `append_record`, so a failed write (disk full, permissions) printed a full record with exit 0 while
  fieldlog exited 127 and no `session.json` existed. The assignment now follows the write; the error branch
  prints a deliberately small `{"id", "recipe", "command", "exit_code", "duration_sec", "error": true}` so
  no consumer can depend on a second schema. Test in `tests/test_run_json.py`.
- **The `$OUTDIR` preview parsed the whole manifest on every keypress.** The first fix made
  `pending_out_dir` call `next_run_number`, which reads `session.json` (measured: 0.6 ms at 200 records,
  19 ms at 5,000) from every args-band refresh, including each raw-editor keystroke.
  `archive.peek_run_number` reads `.run-counter` alone — the counter is never below any recorded id by
  construction — and falls back to the manifest only for an archive with no counter. Reservations are
  unchanged. Test: `tests/test_outdir_preview.py`.
- **`doctor`'s footer contradicted its rows.** A refused target (`--target=-f`) was counted into "set a
  target (-t) to unlock N". Refused values are now a separate `refused` count with their own hint, in the
  text and JSON output.
- **`show` previewed the old `$OUTDIR` layout.** The no-run fallback in `resolve_flags` now reads
  `raw/<stamp>_NN/`, a placeholder that pastes harmlessly (`<run>` would be a shell redirect).
- **The palette's reason column truncated every real reason** ("needs a dns na…"). The phrase matching
  now lives in one place, `recipes.reason_kind` / `reason_missing`, and `tui.helpers.short_reason` renders
  short labels ("needs dns name", "bad target", "not installed") from the kind. `doctor_bucket` is an
  alias of `reason_kind`. This is most of the value of the deferred `reason_kind` refactor (§4 D2) without
  changing `is_blocked`'s return shape.
- **A malformed dotted quad became a hostname.** With the `ipaddress` classification, `1.2.3`,
  `10.0.0.256` and `192.168.001.020` stopped being addresses and started binding `$HOST`. A value made only
  of digits and dots is now an address whether or not it parses, and `is_blocked` refuses one that does not
  parse ("target is not a valid address"), so a typo is caught rather than run.
- **Recent re-ordered without repainting, and the cursor drifted.** `_remember` now rebuilds the tree, and
  `_rebuild_tree` keeps the cursor on the same row *identity* (kind, bin, tool, preset) rather than the same
  index, so a Recent re-order underneath the cursor no longer moves the highlight to a different recipe.
- Smaller: `--note "   "` stored a blank note (now stripped); the parse regex ran twice over the tail (now
  once, via `parse_match`); a stale sentence in `collect_job_artifacts` about same-second runs sharing a
  directory; the unused `TargetSession.out_dir_for`; README wording for `--json`'s error shape and where a
  chain's note lands.

The second pass also confirmed by execution that the risky changes (per-run `$OUTDIR`, chain sharing,
the `raw/` fallback under an unwritable log destination, artifact-root naming, the leading-`-` gate) hold
under their edge cases, and that the three repaired curl recipes run against a local HTTP server.

---

---

## 2. Fixed in the 2026-09-17 remediation pass

Each entry names the finding it closes by its old section number. Each was reproduced, or read from the
code with no interpretation needed, before it was changed.

### 2.1 The stdin bar took the keyboard for any partial line — was §2.1 — **[confirmed, resolved]**

The runner's "awaiting input" is a timeout (a partial line and 0.4 s of quiet), and the TUI moved focus into
the reply field on every one, so a tool that printed `working...` and paused ate the operator's next hotkey.
The bar still rises for every block, so the block is visible; the keyboard moves only when the text ends the
way a prompt ends (`?`, `:`, `]`, `)` — `Password:`, `[y/N]`, `(yes/no)?`) or the block has outlasted
`STDIN_FOCUS_AFTER` (1 s, which the next `_tick` sees; no new timer). Esc still hands the keyboard back and
is checked ahead of both. The accepted trade-off: a tool silent for more than a second on a partial line still
gets focus, because past that a slow tool is no longer the likely reading. Tests: `tests/test_stdin_bar.py`.

### 2.2 A whitespace-only partial line left the job flagged "awaiting input" — new, **[confirmed, resolved]**

Found while reading §2.1. `printf '    '; sleep 0.7; echo done` through `run_job` fired `on_state` once (the
block) and never again: the prompt was stripped to `''`, and the resume check was `if job.await_prompt:`, so
the empty prompt never cleared. The job read as blocked until it exited, and the stdin bar stayed up with an
empty prompt. Both checks in the read loop are now `is None` / `is not None`, which is what `ActiveJob.awaiting`
already tested. Tests: `tests/test_prompt_heuristic.py` (the ordinary case and the whitespace one).

### 2.3 The reason strings were an implicit API — was §2.4 and D2 — **[confirmed, resolved]**

`reason_kind` and `reason_missing` recovered the kind of a block by matching English substrings of
`is_blocked`'s reasons; `doctor` and the palette both read them, and `cli._DOCTOR_SCOPE_NOTE["interface"]`
was unreachable. Now `check_recipe(tool, preset, session, flags=None)` returns a frozen
`Verdict(blocked, kind, reason, missing)` with `kind` in `ready | binary | target | dns | lhost | interface`
and `missing` true only when the scope value is unset; `check_chain` returns the first blocked step's verdict
with the step named in the reason. `is_blocked` and `chain_blocked` are two-line wrappers, so no caller's
contract changed. `reason_kind`, `reason_missing` and `doctor_bucket` are gone; the palette rows carry the
verdict; `doctor` buckets by `kind`/`missing` and its `--json` shape is unchanged. Tests:
`tests/test_verdict.py` (every reason → kind, missing, label; `is_blocked` still equals the pair),
`tests/test_palette.py`, `tests/test_doctor.py` (no edits needed).

### 2.4 Two merge rules, both partly silent — was §3.2 and §3.3 — **[confirmed, resolved]**

Reading `_merge` upgraded both findings to confirmed: its `src is None` branch — the only place a tool's
`name`/`bin` could ever be updated — was unreachable, because the base file was loaded through `_read_file`
and never merged. So a drop-in's `bin:` or `name:` on an existing tool was always silently ignored, a duplicate
preset id inside one file silently kept the first, and `_read_tools` was dead code.

Now `_read_file` returns a file's entries in order, merging nothing, and one `_merge` folds them in for the
base file and every drop-in alike: a repeated tool id adds its presets to the tool defined first; a repeated
`tool/preset` replaces the earlier one, reported to `overrides` across files (as before) and to `errors`
inside one file (`recipes.d/x.yaml: ping/quick defined twice · last one kept`); `name:`/`bin:` restated on an
existing tool with a *different* value is ignored and reported (`ping: bin ignored · … set bin: on the
preset`), while restating the same value — the drop-in an operator writes by copying the base entry — says
nothing; and an entry with neither `presets` nor `flags` adds nothing, where before `normalize_recipe`'s
stand-in `default` preset would have landed as a phantom recipe (a bare `ping`, running under the very `bin`
the message said was ignored). Explicitness is read from the raw YAML keys, never the normalised dict, so a
second drop-in adding a preset to `http` (`bin: python3`) without restating `bin` is silent. Nothing is ever
dropped over a message. Tests: `tests/test_merge_rules.py` (10 cases); `tests/test_dropins.py` and
`tests/test_base_catalog.py` unchanged.

### 2.5 `_stdin_dismissed` was keyed by the per-target run number — was §3.4 — **[confirmed, resolved]**

Keyed by the process-wide job key (`tab.job_id`) in all three places, the same key `self.jobs` uses and for
the same reason. Test: the two-targets-both-`#01` case in `tests/test_stdin_bar.py`.

### 2.6 The status band was rebuilt from scratch every second — was §3.5 — **[confirmed, resolved]**

`_refresh_status_band` now updates the existing cells in place when their count matches the items that
survive the width budget, and rebuilds only when the band changes shape (another tab, an item dropped for
width). This covers both callers: `_tick` once a second and `write_system_log` per transcript line. Two
refreshes inside one frame still rebuild (Textual queues the removal, so the count no longer matches); that is
the safe direction. Tests: `tests/test_status_band.py`.

### 2.7 The scope was persisted only on a clean unmount — was §3.8 — **[confirmed, resolved]**

`TargetModal.action_save` writes `.last-scope.json` as it saves, so a crash or a killed terminal no longer
loses the target the operator just typed. Test: `tests/test_scope_persists.py`.

### 2.8 A kill cut short by quitting was lost — was §3.9 — **[confirmed, resolved]**

Executed rather than inferred this time. Cancelling `run_job` closes the pty master, which hangs up the
child's controlling terminal: most tools die of that SIGHUP before the SIGTERM the cleanup sends (observed:
`sh -c 'trap "" INT TERM; sleep 30'` exited −1). What survived was a tool that ignores INT, TERM *and* HUP —
a daemon, in effect — after the operator had chosen Kill and quit inside the 10 s grace: `kill_job`'s SIGKILL
was scheduled on the loop that the quit tore down. `kill_job` now sets `job.kill_requested`, and the
cancellation path sends SIGKILL to such a job instead of SIGTERM; a job that was not asked to die still gets
SIGTERM. Test: `tests/test_kill_escalation.py`.

### 2.9 `ActiveJob.scope` was captured and never read — was §3.7 — **[confirmed, resolved]**

Removed, with the `scope` element of `prepare_job_paths`'s tuple. The one test that read it
(`tests/test_h6_run_ids.py`) now asserts what production actually uses: each job's log sits under the target
folder it was spawned for.

### 2.10 `tui/theme.py` imported the catalog for one string — was D5 — **[confirmed, resolved]**

`META_COMMANDS` moved to `tui/modals.py`, its only consumer, which already imported `RECIPES_PATH`. The theme
imports nothing of fieldlog's own; `tests/test_theme_is_pure.py` imports it in a fresh interpreter and
asserts neither `yaml` nor `fieldlog.recipes` loaded.

### 2.11 The two substitution mechanisms had no marked boundary — was D6 — **[resolved, in comments]**

The regex `state._VAR` *is* the boundary, and now says so: what it matches, fieldlog substitutes as text after
`is_blocked` has allowlisted the value; what it leaves alone reaches the shell and expands from the child's
environment, which `runner.build_env` fills with the same bindings. Same values, one gated path. No code
change.

### 2.12 The README did not state the trust model — was §2.2 and §2.3 — **[resolved]**

The sentence about `--extra-args` and TUI args edits being the operator's own shell, and `./recipes.d/`
carrying the same trust, was already in the working tree under *Variables* from the 2026-09-16 pass
(uncommitted, so §2 had not caught up with it). This pass adds the cross-reference under *Drop-in files*,
where a reader looking at drop-ins would actually look.

### 2.13 Found by the independent second pass — **[confirmed, resolved]**

A separate agent reconstructed the pre-pass tree from the diff, compared behaviour old against new over a
matrix of catalogs, scopes and CLI invocations, and ran every new test against the old code to see which ones
discriminate. It found no correctness regression: `doctor -v` and `doctor --json` are byte-identical across
four scopes and per-row identical across ten; the run record is unchanged; the status band never shows a
stale cell (tab switches, a summary appearing and vanishing, a width-truncated band, two refreshes in one
frame); the focus rule is actually delivered by `_tick`; and the signal path never touches a finished job or
its recorded exit code. Its risks and nits were all taken:

- **A palette row could crash instead of going blank.** The row's `blocked` came from `search()`'s own
  `check_recipe` call and its `verdict` from a second one; if the two disagree within a keystroke (a binary
  just installed, an interface just up) `short_reason(None)` raised, where the old code rendered an empty
  cell. Guarded.
- **`presets: []` and `presets:` (null) still produced the phantom `default` preset** that §2.4's guard was
  meant to stop, because the guard tested whether the key was written, not whether it held anything — and
  the `bin ignored` message is exactly what nudges an operator to write an empty `presets:`. Now an empty
  or null list counts as absent. Test in `tests/test_merge_rules.py`.
- **Chains did not follow the "one merge rule" the docstring claimed.** Base chains never went through
  `_merge_chains` at all, so a chain id repeated in the base file gave *two* chains and `find_chain` returned
  the first; inside one drop-in a repeat was reported as an override. Pre-existing, but the claim was new.
  Base chains now go through `_merge_chains` with the same written/errors treatment as presets: within a
  file "chain X defined twice · last one kept", across files the override as before. Tests in
  `tests/test_merge_rules.py`.
- **`_DOCTOR_SCOPE_NOTE[verdict.kind]` was an unguarded subscript** where the old code fell back; safe today
  (verified: `missing` is only ever set for the three kinds in the table) but back to `.get(kind, head)`.
- **The narrowed status-band guard let `ScreenStackError` escape** on an app with no screen (reproduced on
  a never-run app; unverified as reachable in the running one). Added to the tuple.
- **The prompt-shape test read the un-stripped pty text**, so a coloured prompt ending in a reset
  (`Password: \x1b[0m`) missed the colon and fell back to the timer. `runner.strip_ansi` (the same regex the
  log uses) is now applied to the prompt in the stdin bar, so the bar also shows it clean. Test in
  `tests/test_stdin_bar.py`.
- **One test was a wall-clock race** (it relied on less than a second passing between building the job and
  the refresh). It now sets `await_since` immediately before the call.
- **The kill grace is cut short by quitting** — the one deliberate trade. A job the operator chose to Kill
  and that is still alive when the app quits inside the 10 s grace now gets SIGKILL at once rather than
  SIGTERM (§2.8). A tool that catches SIGINT and is mid-flush would lose that window; a tool that ignores
  INT, TERM and HUP would otherwise outlive its kill. The quit dialog already says quitting kills running
  jobs; the close-tab transcript line now says "SIGKILL in 10s if still running (at once on quit)".

### 2.14 Found by the cold review of the 2026-09-17 feature pass — **[confirmed, resolved]**

A separate agent read the whole feature diff (`roadmap.md`) against the code, reproduced what it claimed, and
ran the new tests against the old code to see which ones discriminate. Fixed in the same pass:

- **An interrupted run was archived as having failed its expectation.** `expect_found` ran over the partial
  log after Ctrl+C and the record said `found: false`; `report` suppressed it, `history` said
  `(expect not met) interrupted`, `run` said both. The record was the false claim. An interrupted run now
  makes no claim: `found: null`, which every reader treats as unchecked. A timeout is judged on what it
  printed. Test: `tests/test_expect.py` (the interrupted case across writer, `history`, `--fields`, `report`).
- **`history`'s listing worded an exit differently from `--fields` and `report`** (`exit 124` with no
  `(timeout)`; both notes on an interrupt) and printed the raw ISO stamp. It now uses `exit_label` and
  `format_time` like the other two. Test: `tests/test_history_views.py`.
- **A `--fields` cell could hold a newline or the string `None`**, and the workspace overview clipped a note
  but not a chain summary (573 characters, measured). One `CELL_WIDTH`, whitespace collapsed, both clipped.
- **The chain-stop line was written twice** (CLI and TUI), differently. One `report.chain_outcome`.
- Smaller: a note's Files cell is `—` like its exit and duration; `fieldlog note "" "…"` no longer writes
  into `unassigned`; an unwritable folder is an error line, not a traceback; the status band's
  `expect · not met` item goes last so a narrow band keeps the summary; `run_succeeded`'s docstring no
  longer promises callers it does not have; a typo-nudge test that would have passed on the old code now
  asserts that `not` nudges to `note`.

Judgement calls from the same review, not taken:

- **No write-side cap on a chain's joined `summary`.** The per-step summaries are capped; the readers whose
  width matters (the overview, the TUI band) clip; `history` and `report` show it whole on its own line or
  cell. A cap would lose the checklist. Recorded in `features.md` F9.
- **`note_line` (first line) is not applied to a run's `summary`.** A summary is one line by construction
  (`summary_from_match` collapses whitespace); the helper is for prose.
- **`history` still reads `session.json` itself** while `report` and the overview use the tolerant
  `load_runs`: a corrupt manifest exits 1 from `history <target>` and renders as "no runs" elsewhere. Two
  behaviours, both defensible (the direct question deserves the error). Left as an open, minor
  inconsistency — § 3.10.

Behaviour changes the pass makes on purpose, for an operator reading the boot transcript: a `tool/preset`
repeated inside one file is now last-wins plus an error (the old code kept *both* under one id when the
repeat was inside a single entry); `name:` restated with a different value is now reported; an entry that
only restates a tool no longer adds a phantom preset, so that file's variant count in the recipe manager
drops by one. The shipped catalog is unaffected: 20 tools, 61 variants, no messages, and the base and
drop-in counts still sum to the total.

---
