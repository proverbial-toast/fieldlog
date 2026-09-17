# fieldlog — proposed features

**Status:** reference document, 2026-09-16. Every item is a **[proposal]** unless marked otherwise. Each
one answers the same seven questions: what it does, what problem it solves, why it fits fieldlog, which
existing components carry it, what new architecture it needs, how hard it is, and what it risks.

Difficulty scale: **S** an afternoon · **M** a day or two · **L** a week · **XL** a project.

The ordering is by value to the project's stated purpose ("the archive is the point"), not by size.
`next-steps.md` turns this into a sequence.

---

## F1. History in the TUI

**What.** A `G` (or `Shift+H`) modal — or a "PAST RUNS" section above the tab strip — listing this target's
records newest first: id, recipe, exit/verdict, duration, started, summary, note. Enter on a row opens the
run's log in a read-only tab (same `RichLog`, tail-loaded, "archived" badge); `Y` copies its path.

**Problem.** The archive is the product, and its main surface cannot show it. An operator mid-session cannot
answer "did I already run this against this host today" without leaving the TUI.

**Fit.** Every other surface (`history`, `report`) reads `session.json`; the TUI is the odd one out.

**Existing components.** `report.load_runs`, `record_ok`, `format_time`, `resolve_log`, `read_log_tail`,
`human_size`; the tab strip, `TabDescriptor`, `ContentSwitcher`; `RecipeManagerModal` as the pattern for a
scrolling list modal; `resume_session` already knows when the target changes.

**New architecture.** A `tui/history.py` mixin (`_load_history`, `_open_archived_tab`); a `TabDescriptor`
status `"archived"` so `_run`-related actions (Ctrl+C, kill) no-op on it; a small `read_log_tail` wrapper for
the RichLog. Reading `session.json` on demand (modal open / target change), never per tick.

**Difficulty.** M.

**Risks.** A very large `session.json` read on every modal open (mitigated by reading once per target
change); an archived tab must never be confused with a live job by the status band (needs the status guard).

---

## F2. Notes as first-class records

**What.** Beyond `--note` on `run` (this pass): `fieldlog note <target> "text"` appends a note record
(`recipe: "note"`, no command, no log); in the TUI, `N` on a finished tab attaches a note to that run
(`amend_record`), and a field in the ARGS band attaches one at launch. `history` and `report` render notes
inline in the timeline.

**Problem.** A field log without the *why* — "after the firewall change", "customer reported this at 14:10" —
is a list of commands. Today the only place for context is a filename.

**Fit.** The name of the project. Records are append-only JSON; adding a kind of record that carries prose
and no command is the smallest possible extension.

**Existing components.** `archive.append_record` and the lock; `_append_manifest`'s record shape; `history`
and `report` already render `note` after this pass; `manifest_environment` for scope on the note record.

**New architecture.** `archive.amend_record(target_dir, run_id, **fields)` (read-modify-write under the
existing lock, refusing to touch `exit_code`/`artifacts`); a `note` subcommand; a TUI action and a one-line
input (the stdin bar's `Input` pattern). `report._render_output` needs a branch for a record without a
command, and the summary table needs a row style for it.

**Difficulty.** S for the subcommand; M with the TUI parts.

**Risks.** `amend_record` is the first write to an existing record; it must be limited to additive fields so
the "append-only" property stays true in spirit. A note record has no run number semantics of its own and
takes one, so `history` counts rise — document it as the chain summary is documented.

---

## F3. `expect:` — a semantic verdict from output

**What.** A preset field `expect: <regex>` that must match the finished log (same last-64-KB window as
`parse`), and/or `fail_if: {field: <regex>}` over `fields`. When it fails, the record carries
`verdict: "fail · expected …"` and every reader treats the run as failed: `run` exits non-zero, chains stop
(unless `continue`), the tab shows ✗, `report` bolds it. The exit code stays the tool's own.

**Problem.** Diagnostic tools exit 0 whatever they found. `ping` with 100 % loss, `curl -s` with a 503,
`openssl s_client` with `Verify return code: 21` are all "success" today. A chain cannot stop on any of them.

**Fit.** It is the natural completion of `success:` (which judges the code) and `parse:` (which reads the
output). Together the three make a recipe a *check* and a chain a *checklist*, without changing the run model
or the exit-code rule the project is strict about.

**Existing components.** `parse_match`, `log_tail`, `run_succeeded` (the one definition of success),
`_validate_parsers` (the same load-time validation applies), `record_ok` in the report.

**New architecture.** A `verdict` on `ActiveJob` and the record; `run_succeeded` gains a verdict input, or a
sibling `run_passed(record)` that readers call instead. The chain driver and the CLI exit status read the
verdict. Load-time validation of the regex like `parse`.

**Difficulty.** M.

**Risks.** Two notions of success in the archive (`success:` codes and `expect:`) need one reader function so
they cannot disagree — exactly the R1 lesson from the previous review. A regex that matches a *header* line
by accident makes a run pass; the last-match rule and anchoring guidance in the README mitigate it.

---

## F4. Artifact checksums and `fieldlog verify`

**What.** Each artifact entry gains `sha256`. `fieldlog verify <target>` re-hashes every artifact of every
record and reports missing or changed files; `report` can print the hash beside each artifact.

**Problem.** An archive that will be cited later ("this is what the host looked like on the 12th") should be
able to show that its evidence has not changed since. Today nothing distinguishes an edited log from the
original.

**Fit.** The project already goes to unusual lengths for record accuracy (own exit codes, per-run
attribution, secret-safe replies). Integrity is the missing property.

**Existing components.** `_build_delta` already streams every artifact end-to-end for line counts — the
hash is computed in the same read at no extra I/O. `load_runs` and `find_target_dir` for the verifier.

**New architecture.** A `hashlib.sha256` in `count_lines_safe`'s loop (or a sibling that returns both); a
`verify` subcommand with `--json`.

**Difficulty.** S.

**Risks.** Binary artifacts still being written when the run ends (a detached GUI) hash a partial file; the
same is true of `bytes` today. Log files are line-buffered and closed before hashing, so the primary log is
exact.

---

## F5. Workspace-level report and export bundle

**What.** `fieldlog report --all [-o dir/]` renders one Markdown per target plus an index with a cross-target
timeline; `fieldlog export <target> [-o bundle.tar.gz]` packs `session.json`, `raw/` and the rendered report
into one archive for hand-off.

**Problem.** An engagement is several hosts. Today a report is per folder and the artifacts have to be
collected by hand.

**Fit.** `render_report` is pure; `target_folders` and `load_runs` exist; the archive layout is already
self-contained per target.

**Existing components.** `render_report`, `target_folders`, `find_target_dir`, `shutil.make_archive`.

**New architecture.** A loop and an index renderer; an `export` subcommand. Optionally the checksums from
F4 in the index so the bundle is verifiable.

**Difficulty.** S–M.

**Risks.** Logs may contain secrets echoed by tools (the reply hiding covers operator input, not tool
output); an export command should say so and maybe support `--since`.

---

## F6. Ad-hoc runs and "save as recipe"

**What.** `fieldlog run -c 'nmap -sV $TARGET' 10.0.0.1 [--save nmap/versions]` runs a one-off command through
the same runner and archive (recorded as `adhoc/<bin>` with the template on the record), and optionally
writes it into `~/.config/fieldlog/recipes.d/saved.yaml` as a preset. In the TUI, the same for an args edit:
`Ctrl+S` "save this variant as…".

**Problem.** Field work produces commands before it produces recipes. Today a command that is not yet
catalogued is either run outside fieldlog (unrecorded) or catalogued first (friction at the worst moment).

**Fit.** The launcher takes tool/preset dicts, not catalog entries; an ad-hoc tool dict is
`{"id": "adhoc", "bin": first_token}` and a preset `{"id": ..., "flags": rest}`. The template goes through
`is_blocked`, `resolve_flags` and `exec_form` like any other, so scope safety and exit-code correctness hold.
Drop-ins, override reporting and `Shift+R` reload already exist for the save half.

**Existing components.** `plan_launch`, `is_blocked`, `format_command`, `dropin_files`, the recipe manager.

**New architecture.** A `-c/--command` argument on `run` (mutually exclusive with a recipe id); a `save_preset`
helper that appends to a YAML file without reformatting it (write a new document or a dedicated file per
save); a record field `template` so the archive keeps the unresolved form.

**Difficulty.** S for `-c`; M with save.

**Risks.** Scope creep: the catalog stops being the only source of truth. Mitigated by recording the template
and by making save explicit. A bare `-c` could tempt scripts to pass untrusted strings; the README's
security note (`problems.md` § 2.2) applies.

---

## F7. Shell completion

**What.** `fieldlog completion bash|zsh|fish` prints a script that completes subcommands, recipe ids (from
`list -q`), chain ids and target folders (from `history`).

**Problem.** Recipe ids are `tool/preset` pairs nobody remembers; today the workaround is `list -q | fzf`.

**Fit / components.** `list -q`, `history` with no argument, `SUBCOMMANDS`.

**New architecture.** None. **Difficulty.** S. **Risks.** Maintaining three shells' syntax.

---

## F8. Trends over `fields`

**What.** `fieldlog history <target> --recipe ping/quick --fields` prints a table with one column per field
across runs; `report` adds the fields to the summary table when every run of a recipe has them; the TUI
status band shows the previous run's value beside the current one (`loss 0% ← 12%`).

**Problem.** "Is it getting better or worse" is the question behind most repeated diagnostics, and it
currently needs `history --json | jq`.

**Fit.** The storage landed in this pass; this is the read side.

**Existing components.** `fields` on the record, `history`'s rendering loop, `_status_items`, `load_runs`.

**New architecture.** A `--recipe` filter and a `--fields` table renderer in `handle_history`; in the TUI,
one lookup of the previous record for the same recipe when a job finishes (F1's loader).

**Difficulty.** S–M.

**Risks.** Fields are strings; comparison as numbers must be tolerant (`"0"`, `"0.5"`, `"n/a"`).

---

## F9. Chain summaries

**What.** The chain summary record carries `summary` (the steps' summaries joined with `→`) and `fields`
(namespaced by step, `{"ping/quick.loss": "0"}`), so `history` shows what a chain found, not only that it ran.

**Fit / components.** `run_chain` already collects each step's `plan.job` after it runs; the summary and
fields are on it. **Difficulty.** S. **Risks.** Long summaries; cap like `PARSE_SUMMARY_MAX`.

---

## F10. A persisted session transcript

**What.** `<workspace>/fieldlog.log` receives every System-tab line with a timestamp.

**Fit / components.** `write_system_log` is the single choke point. **Difficulty.** S. **Risks.** Growth;
rotate at a size, or per day.

---

## F11. Scope validation in the target modal

**What.** `TargetModal` refuses to save a target, dns name or interface that `is_blocked` would refuse, and
says why in the form.

**Fit / components.** `unsafe_scope_chars`, the new leading-dash rule; the modal's `_form_note` label.
**Difficulty.** S. **Risks.** None; today the failure is discovered at Enter, one screen later.

---

## F12. Repeat / watch mode — **[deferred by the maintainer]**

`fieldlog run … --every 60s` re-plans and re-runs a recipe until interrupted, each iteration its own record.
The maintainer has taken this out of scope along with diffing; it is recorded here because F8 (trends) makes
it far more useful than it was, and because the loop is a `while` around `plan_launch` + `run_job`.
