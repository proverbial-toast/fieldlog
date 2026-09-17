# fieldlog — next steps

**Status:** reference document, maintained. The one list of what is done, what is next, and what is not
being done and why. Effort: **S** an afternoon · **M** a day or two · **L** a week. The letter codes are
the sections of the planning documents that proposed each item, now under `history/`: `F` features,
`O` opportunities, `D` directions, `P` problems (fixed ones in `history/problems-fixed-2026-09-16-17.md`,
open ones in `problems.md`).

---

## Done

| Pass | What landed |
|---|---|
| 2026-09-16 investigation (P §1) | Hex-looking hostnames are hostnames; a leading `-` in a scope value is refused; the three broken curl recipes run; `--artifact-root` names its folder like the workspace; `$OUTDIR` is per run and chains share the first step's; `run --json` prints the archived record; Recent re-orders; `--note`; `fields` on the record; the second-pass fixes (P §1.9) |
| 2026-09-17 remediation (P §2) | Stdin bar takes the keyboard only for a prompt-shaped line or after a second; whitespace-only partial lines; `Verdict` from `check_recipe`/`check_chain`; one merge rule with reported duplicates; dismissal keyed by job; in-place status band; scope saved as the form saves; SIGKILL on quit for a job already asked to die; `theme.py` pure; the README trust note |
| 2026-09-17 feature pass (`history/roadmap.md`) | `expect:` with `run_passed` as the one verdict reader and `found: null` for an interrupted run; chain summaries on the summary record and its steps; `history --recipe` / `--fields`, the overview's last-record line, `report`'s Summary column; `fieldlog note` (no `amend_record`, by design); the System transcript in `<workspace>/fieldlog.log`; `show` prints a preset's contract; the cold review's fixes (P §2.14) |
| 2026-09-17 macOS (`history/roadmap.md` group 3) | `SIOCGIFADDR` per platform; `DEFAULT_INTERFACE` (`en0`); `timeout`/`gtimeout` with a refusal; the `platform:` catalog key; Darwin ping/traceroute twins and `scutil`/`lsof`; `macos-latest` in CI. **Unverified on a Mac** until the CI leg runs (P §3.11) |

438 → 532 tests in 47 files across the 2026-09-17 passes; `ruff check` clean. All committed on `main`.

## Next

1. **Push, and read the first macOS CI run.** Nothing Darwin-specific has run on a Mac; `problems.md`
   § 3.11 lists what only that run can confirm. Upgrade its tag when green.
2. **Use it for a week.** Every 2026-09-17 item adds to the record or reads it back. Real runs will say
   whether `expect:` earns its risk (a missed regex fails the run, not just the summary) and which
   `history --fields` columns matter.

## When there is a reason

- **F4 checksums + `fieldlog verify`** — S. `sha256` per artifact is free in `_build_delta`'s existing
  read; the verifier is the only new code. Build it at the first hand-off that needs verifying.
- **F5 workspace report and export** — S–M. `render_report` is pure and `target_folders` exists; a loop
  and an index. At the first multi-host engagement.
- **D3 narrow the broad `except` guards** in the TUI — S each, as panes are touched; `_repaint` is the shape.
- **D11 pane tests** — the tree cursor, variant keys and layout switch have none.
- **P §3.7** the palette's chain rows could name the blocking step — S, cosmetic.
- **P §3.10** `history <target>` and `report` read a corrupt manifest differently — decide which behaviour
  is wanted, then one reader.
- **D §7 structured parsers** (`parse: {json: …}`) — M. The day a regex `parse:`/`expect:` rule bites on
  a JSON-speaking tool (`iperf3 --json`, `curl -w %{json}`).

## Larger, and later

- **F6 ad-hoc runs and save-as-recipe** — M. A second source of truth for the catalog; the expensive kind.
- **D §2 vantage points** (run a recipe from a jump host) — L. The first change to the run model.
- **D §5 automation surface** — M. `expect:` already makes a chain's exit status meaningful to CI; the
  rest is turning the injection footguns into refusals first.
- **D §4 shared catalogs** (`fieldlog catalog add <git-url>`) — S–M, when a team needs it.

## Not doing, and why

- **F1 history in the TUI**, the TUI note action (O §15), the previous value beside the current in the
  status band (F8), and the repaint model (D1) that was gated on F1 — the maintainer's path reads results
  with `history` and `report`, not in the TUI. Only if that changes.
- **Native Windows** — a ConPTY twin of the runner, a second catalog and a shell decision; WSL runs it as
  is (`history/directions.md` § 11).
- **mypy** — declined 2026-09-16; not to be re-proposed.
- **JSONL manifest** (D §6) — deferred; revisit only if a real target folder gets slow.
- **F12 repeat mode**, **run diffing** — a shell loop does the first; the second is scope creep.
- **F7 shell completion** — `list -q | fzf` covers it; three shells' syntax to keep.
- **F11 scope validation in the target modal** — cosmetic; the failure is found one screen later today.
- **D7 `command:` preset field**, **`$LHOST6`** (P §3.4) — features, queued behind everything above.
- **HTML report export** — declined 2026-09-16.

---

## How to keep this useful

- Three documents are maintained: `architecture.md` (how it works), `problems.md` (what is open) and this
  one (what is next). Everything under `history/` is kept as written.
- When an item lands: a row in "Done" here, its entry removed from `problems.md` or moved with its test
  into `history/problems-fixed-…`, and `architecture.md` updated if the shape changed.
- When a **[likely]** item is reproduced, upgrade its tag and add the reproduction.
