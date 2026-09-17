# fieldlog — missed opportunities

_Moved to `history/` on 2026-09-17. Built items are marked inline; the rest are listed with reasons in `../next-steps.md`._

**Status:** reference document, 2026-09-16. Everything here is something the *existing* architecture
already makes cheap. Each item names the components that support it. Tags as in `README.md`; items are
**[proposal]** unless marked otherwise. Larger ideas are developed in `features.md`; this document is the
inventory.

---

## 1. The archive is the product, and the TUI cannot show it — **[confirmed gap]**

The TUI reads nothing back from `session.json`. It writes runs there, `resume_session` counts them in one
transcript line, and that is the whole relationship. An operator who wants to know what was run against this
target yesterday has to leave the TUI and run `fieldlog history`.

What already exists: `report.load_runs` (tolerant reader), `record_ok`, `format_time`, `resolve_log`,
`read_log_tail`; the tab strip and `RichLog`-per-tab machinery; `ContentSwitcher`. A History modal (or a
"past runs" section of the RESULTS pane) that lists records with summary/fields/note and opens a past log in a
read-only tab is a composition of parts that exist. This is the single largest gap between what the project
says it is and what its main surface does. → `features.md` F1.

## 2. `fields` turns an archive of answers into an archive of measurements — **[built 2026-09-17: `history --fields`]**

`parse:` already extracts named groups; this pass stores them on the record as `fields`. Nothing reads them
yet. With `history --json` a shell loop can already trend `loss` over runs; a `history` column, a `report`
table column and a `--recipe` filter would make it native. → `features.md` F8.

## 3. Evidence integrity for free — **[proposal]**

`_build_delta` already reads every recorded artifact end-to-end to count lines. Hashing in the same pass costs
nothing extra, and a `sha256` on each artifact entry turns the run record into something that can be
*verified* later: `fieldlog verify <target>` re-hashes and reports drift. For a tool whose pitch is "a week
later you can see exactly what was run", tamper-evidence is the natural next property. → `features.md` F4.

## 4. `RunStep` is an executor seam nobody has used — **[proposal]**

`run_chain` hands each planned step to a callback and only asks for an exit code back. That is exactly the
shape needed to run a step somewhere else: on a jump host over ssh, in a container, under a different user.
The plan carries the command and the environment; a remote `run_step` would ship both, stream the pty back,
and pull `$OUTDIR` home. Nothing in `chain.py` would change. → `directions.md` § 2.

## 5. `LaunchPlan` is a complete, serialisable description of a run — **[proposal]**

`--dry-run` proves it: the plan has the command, env, paths and timeout, and nothing else is needed to execute
it later. That makes queuing and scheduling ("run this chain at 02:00 and archive it") a matter of pickling
plans or re-planning from `(recipe, scope)` at fire time — no new execution model. It also makes an
"explain" surface trivial: `show` already prints the resolved command; it could print the env and paths too.

## 6. `report.render_report` is pure — **[proposal]**

It takes a directory and a list of records and returns a string. A workspace-level report (`report --all`:
one document per target, or one document with a section per target and a cross-target timeline) is a loop
around it. An HTML renderer is a second function over the same records. A JSON "report" already exists as
`history --json`. → `features.md` F5.

## 7. Chains have no summary of their own — **[built 2026-09-17]**

A chain summary record has `steps` but no `summary` and no `fields`, so `history` shows `chain/https-check |
exit 0` and nothing else. The steps' own records carry summaries; the summary record could carry them joined
(`ping: 4 replies · 0% loss → curl: HTTP 200 in 0.31s`), which is what an operator scanning history wants to
see. Ten lines in `chain.py`.

## 8. `success:` plus `parse:` is one field away from a semantic verdict — **[built 2026-09-17, as `expect:`]**

A run can already be judged by exit code. Many diagnostic tools exit 0 whatever they found (`ping` with 100%
loss, `curl -s` with a 500, `openssl s_client` with a failed verify). An `expect:` regex that must match the
log — or a `fail_if:` over `fields` — turns a recipe into a check, and a chain into a checklist that stops at
the first *semantic* failure. The record would carry `verdict` beside the raw exit code, preserving the
"never fabricate the exit code" rule. → `features.md` F3.

## 9. Args edits and `--extra-args` are field knowledge that evaporates — **[proposal]**

An operator who edits a variant's args in the TUI, or passes `--extra-args`, has produced a better recipe and
fieldlog forgets it at exit. "Save as preset" — write the edited template into
`~/.config/fieldlog/recipes.d/saved.yaml` under a name the operator types — closes the loop between running
and cataloguing. The drop-in mechanism, the override reporting and the reload key already exist. →
`features.md` F6 (ad-hoc runs) shares the mechanism.

## 10. The System transcript is the only record that is not recorded — **[built 2026-09-17]**

Kill/detach decisions, scope changes, reloads, args resets — the TUI narrates all of them into the System tab
and writes none of them to disk. A `<workspace>/fieldlog.log` appended from `write_system_log` (a file open
in append mode, one line per entry) would make the operator's *session* part of the archive, which is what
"log what you did in the field" implies. Ten lines.

## 11. `list -q` and `doctor --json` are most of a shell completion — **[proposal]**

`list -q` prints recipe and chain ids one per line for fzf. A `completion bash|zsh|fish` subcommand that
emits a script calling it is small and makes the CLI feel finished. `doctor --json` also makes a
`--runnable` completion possible.

## 12. `history` folder lookup already knows more than it shows — **[built 2026-09-17: the overview shows each folder's last record; the cross-target search is not]**

`find_target_dir` scans manifests to match a target to a folder. The same scan could answer "which folders
have runs against 10.0.0.0/24 or its members", i.e. a cross-target search, and `history` with no argument
could show each folder's last run time and last summary, not just a count.

## 13. Tests: the harness is there, the panes are untested — **[confirmed gap]**

`app.run_test()` works (8 tests use it) and the mixin split made every pane reachable by name. There are no
tests for cursor movement in the tree, variant selection by number, the stdin bar appearing on a prompt, or the
stacked/split switch. Each is a ten-line test with the existing fixtures.

## 14. `doctor` could be the TUI's boot preflight — **[proposal]**

`_log_boot_transcript` prints "N available · M missing". `doctor_scan` computes per-recipe, per-chain
readiness against the scope and the reasons. Printing its summary line in the transcript (and offering the
verbose breakdown from the manager modal) reuses it verbatim.

## 15. The `note` field wants a TUI entry point — **[proposal; the CLI half is `fieldlog note`, 2026-09-17]**

`--note` exists on the CLI after this pass. The TUI has no way to attach one at launch (a text field in the
ARGS band, or a prompt on Enter when a modifier is held) or after the fact (a "note this run" action on a
finished tab that rewrites its record under the lock). The archive writer already takes the lock; an
`amend_record(target_dir, id, **fields)` beside `append_record` is the one new primitive.
