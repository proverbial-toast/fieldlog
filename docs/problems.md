# fieldlog — problems and technical debt

**Status:** reference document, 2026-09-16. Tags: **[confirmed]** reproduced by execution or read
unambiguously from the code · **[likely]** inferred from reading · **[proposal]** a recommended change.
See `README.md` for the conventions.

The two dated reviews in this folder already found and fixed the big structural issues (chain exit codes,
artifact attribution, the args-edit gate, `history` lookup, memory reclamation, the app split). This document
starts where they stopped. §1 is what this pass fixed, §2 what is confirmed and still open, §3 what is
probably wrong, §4 the debt that shapes how hard the next changes will be.

---

## 1. Fixed in this pass

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

## 2. Confirmed and still open

### 2.1 The prompt heuristic fires on any partial line, and the TUI steals focus for it — **[confirmed]**

`printf working...; sleep 1; echo done` through `run_job` produces `await_prompt='working...'` after 0.4 s.
In the TUI that raises the stdin bar and moves keyboard focus into it ("the one place automatic
focus-stealing is correct: the process waits"). For a tool that prints a progress prefix and pauses — a
slow `openssl s_client`, a scanner printing `Scanning…` — the operator's next hotkey lands in the reply field.
`Esc` recovers, and the heuristic is documented as a timeout, not a parser.

**[proposal]** Keep the heuristic (it is what makes sudo/ssh prompts work) but soften the *consequence*:
raise the bar without taking focus unless the partial line ends in a prompt-like character (`?`, `:`, `]`,
`)`) or the job has been quiet for a second tick. Both are one-line conditions in `_refresh_stdin_bar`.

### 2.2 `--extra-args` and TUI args edits bypass scope validation by design — **[confirmed]**

`fieldlog run ping/quick 10.0.0.1 --extra-args "; id"` runs `id`. The README says "append to the command"
and the operator typed it, so this is the documented contract, not a bug. Worth stating in the README's
security note so nobody wraps fieldlog in something that passes untrusted extra args.

### 2.3 Recipes in the current directory execute — **[confirmed]**

`./recipes.d/*.yaml` loads from wherever fieldlog is started, and a recipe's `flags` is shell. Running
`fieldlog` inside an untrusted checkout and pressing Enter on one of its recipes runs that recipe. This is the
same trust model as `make` or a `Makefile`, and the manager modal shows where each file came from, but the
README does not say it. **[proposal]** One sentence under "Drop-in files".

### 2.4 Reason bucketing is still substring matching on English — **[confirmed, narrowed]**

`recipes.reason_kind` matches `"not found in $PATH"`, `"dns name"`, `"local address"`, `"interface …"`,
`"target"` against the prose `is_blocked` returns; `reason_missing` tests for a `"needs "` prefix. This pass
moved that matching next to the strings it matches, so doctor, the palette and any future reader share one
copy — but it is still prose, and adding a reason still means checking the matcher. Returning the kind from
`is_blocked` itself (refactor #10) remains the clean end state. Two leftovers noted by the second pass:
`cli._DOCTOR_SCOPE_NOTE["interface"]` is unreachable (no interface reason starts with "needs "), and
`peek_run_number` trusts the counter, so a hand-edited `session.json` with a higher id than `.run-counter`
would make the *preview* understate by one (reservations still read both).

### 2.5 A note is quoted, not escaped, in the report — **[confirmed, by design]**

`report.py` writes a note as a Markdown blockquote without escaping, two lines under a summary that is
escaped on the principle that "tool output never becomes markup". A note is operator prose, not tool output,
and the second pass confirmed a note containing `|`, `#` and a fence stays inside the blockquote and does not
break the artifacts list. Recorded so the asymmetry is known to be deliberate.

---

## 3. Likely problems

Inferred from reading; none executed. Ordered by how much they would matter.

### 3.1 `session.json` is rewritten whole on every append — **[likely]**

`append_record` reads the array, appends, writes a temp file and `os.replace`s it, under the lock. Cost is
O(records) per run, so a target with thousands of runs (a server recipe restarted daily, a monitoring loop)
pays a growing tax and holds the lock longer. The maintainer has explicitly deferred JSONL; the honest
statement is that the current format is fine up to a few thousand records per target and that the writer
and readers are the only two places that would change. `directions.md` § 6 sketches the migration.

### 3.2 A drop-in cannot change a tool's `bin` or `name`, and nothing says so — **[likely]**

`_merge` copies `name`/`bin` from an incoming tool only when `src is None` (the base file). A drop-in that
redefines `ping` with `bin: /opt/ping` adds its presets and silently keeps the base `bin`. Either allow it
(and report it as an override) or report the ignored keys in `catalog.errors`.

### 3.3 Duplicate preset ids inside one file keep the first; across files the last wins — **[likely]**

`_read_file` merges a repeated tool id and drops a repeated preset id silently; `_merge` replaces and reports.
Two rules for one situation. The within-file case should at least be reported.

### 3.4 `_stdin_dismissed` is keyed by the per-target run number — **[likely]**

`jobs.py` uses `job.id` (e.g. `"01"`) as the key, while `self.jobs` is keyed by the process-wide job
sequence precisely because run numbers restart per target. Two targets' `#01` jobs share the key. The value
compared is `await_since`, so a false match needs two prompts at the same float timestamp — practically
impossible, but the key is wrong on principle and the fix is `job_key`.

### 3.5 The status band is rebuilt from scratch every second — **[likely]**

`_tick` calls `_refresh_status_band` for a running job, which does `remove_children()` + `mount_all()` of
five `Static`s. `write_system_log` does the same for every transcript line while the System tab is active.
Textual copes, but a busy chain writing transcript lines and a running job together mean two full rebuilds a
second. In-place `update()` of existing cells is the obvious fix and is what `_refresh_tab_strip` already does.

### 3.6 `$LHOST` can never be IPv6 — **[likely]**

`get_interface_ip` uses `SIOCGIFADDR`, which returns the IPv4 address only. A recipe like `ping -6 -I $LHOST`
cannot be expressed. Reading `/proc/net/if_inet6` or `socket.getaddrinfo` would give a `$LHOST6`.

### 3.7 `ActiveJob.scope` is captured and never read — **[likely]**

`plan_launch` sets `scope=session.target`; nothing reads `job.scope`. Dead field; the comment about capturing
at spawn applies to `root`, `stamp` and `out_dir`, which are used.

### 3.8 The TUI persists scope only on a clean unmount — **[likely]**

`.last-scope.json` and `.pinned-recent.json` are written on `on_unmount` (pins/recent also on change). A crash
or a killed terminal loses the scope. Saving on every `TargetModal` save is one call.

### 3.9 `kill_job`'s SIGKILL escalation is lost if the app exits inside the grace — **[likely]**

Carried over from the previous review. `run_job`'s cancellation path sends SIGTERM to the group, which
covers the common case.

### 3.10 `report --full` on a very large log reads it whole — **[likely]**

By request, and the bounded tail reader exists for the default path; but `--full` on the 410 MB log the code
comments mention will allocate over a gigabyte. A streaming copy into the fenced block would be cheap.

---

## 4. Technical debt

What makes the next change harder than it should be. None of these is a bug.

| # | Debt | Where | Why it matters |
|---|---|---|---|
| D1 | **No repaint/invalidation model in the TUI.** Eleven `_refresh_*` methods, ~15 call sites, each caller choosing a subset. | `tui/*.py`, `app.py` | The only way to know a state change repaints correctly is to trace every caller; a pane cannot be tested on its own. Open refactor #5. |
| D2 | **`is_blocked` reasons are an implicit API.** The matching is now in one place (`recipes.reason_kind`, `reason_missing`), consumed by `doctor_bucket` and `tui.helpers.short_reason`. | `recipes.py`, `cli.py`, `tui/helpers.py` | Rewording a reason still means checking one matcher. A `(blocked, kind, reason)` triple from `is_blocked` would delete it. Open refactor #10, now small. |
| D3 | **31 broad `except Exception` guards remain** in the TUI, mostly `query_one` guards that return early. | `tui/*.py`, `app.py` | Correct for a missing widget; they also hide real bugs in the guarded body. `_repaint` is the right shape; the remaining guards could adopt it. |
| D4 | **`TargetModal` lives in `app.py`** so tests can monkeypatch `get_interface_ip` through that module. | `app.py` | The test coupling dictates the file layout. Injecting the probe functions into the modal would free it. |
| D5 | **`tui/theme.py` imports the catalog** for one string. | `tui/theme.py` | The palette cannot be imported without YAML. Pass the path at render time. |
| D6 | **Two substitution mechanisms for one set of bindings**: fieldlog's `$VAR` regex and the child's environment. | `state.py`, `runner.py` | Safety differs (allowlisted raw text vs. real parameter expansion) and nothing marks the boundary. Documented behaviour; keep, but say so in the code. |
| D7 | **The `_WRAPPERS` heuristic** decides whether `bin` is prepended from the first token of the flags. | `recipes.py` | `env`, `sudo`, `timeout`, `doas`, `nice` get one behaviour, `ionice` another, silently. An explicit preset field (`command: true`, "flags are the whole command") would be honest and matches the maintainer's stated preference for explicit opt-in. |
| D8 | **`Catalog.files` vs `file_paths` vs `overrides` vs `errors`** — four provenance collections with a comment explaining a workaround. | `recipes.py` | A single list of `Source(path, kind, tools, presets, chains, errors)` would answer the manager modal and the boot transcript in one shape. |
| D9 | **No type checking in CI.** The previous review counted 37 mypy baseline errors and deferred it. | `pyproject.toml`, CI | The dict-shaped catalog (`tool["presets"]`, `preset.get("flags")`) is exactly where types would pay. |
| D10 | **The catalog is untyped dicts.** | everywhere | Every consumer re-derives `preset.get("bin", tool.get("bin", tool["id"]))`. A `Preset`/`Tool` dataclass with `.bin` would delete a dozen copies of that line. |
| D11 | **TUI coverage is thin.** 8 tests drive the app; none exercise the tree cursor, the variants pane, the stdin bar or the layout switch. | `tests/` | The mixin split made these reachable; nothing uses that yet. |

---

## 5. Things the reviews said to leave alone, and still should

The pty + `exec_form` + `_make_ctty` triangle in `runner.py`, and the catalog's fail-soft loading. Both were
re-read for this document; every comment in them is still load-bearing.
