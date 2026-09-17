# fieldlog — recommended next steps

**Status:** reference document, 2026-09-16. This is the order we would do things in, with the reasoning.
Effort: **S** an afternoon · **M** a day or two · **L** a week. Items reference `problems.md` (P),
`opportunities.md` (O), `features.md` (F) and `directions.md` (D).

---

## Done in this pass

| Item | Reference |
|---|---|
| Hex-looking hostnames are hostnames again (`dc1`, `cafe`) | P §1.1 |
| A leading `-` in a scope value is refused; `doctor` shows the real reason | P §1.2 |
| The three broken curl recipes run; the shipped catalog has a load test | P §1.3 |
| `--artifact-root` and the workspace name their folder the same way | P §1.4 |
| `$OUTDIR` is per run (`raw/<stamp>_<NN>/`); chains share the first step's | P §1.5 |
| `run --json` prints the archived record | P §1.6 |
| Recent re-orders; the palette names the real block reason | P §1.7–1.8 |
| `--note` on `run` and chains; `history` and `report` show it | F2 (first half) |
| `fields` — the parse rule's named groups — stored on the record | O §2 |
| Second-pass fixes: `--json` never claims an unarchived record; cheap `$OUTDIR` preview; doctor's `refused` count; `_NN` in `show`; short palette labels via one `reason_kind`; malformed dotted quads refused; Recent repaints and the cursor keeps its row | P §1.9 |

The suite went from 305 tests in 27 files to 376 in 35; `ruff check` is clean.

## Done in the 2026-09-17 remediation pass

Every finding in `problems.md` was re-examined against the code; what was fixed, deferred or rejected is
recorded there (§ 2–4). Landed:

| Item | Reference |
|---|---|
| The stdin bar takes the keyboard only for prompt-shaped text or after a second of blocking | P §2.1 |
| A whitespace-only partial line no longer leaves a job flagged "awaiting input" (found on the way) | P §2.2 |
| `check_recipe` / `check_chain` return a `Verdict`; the reason-string matching is gone | P §2.3, D2 |
| One merge rule for base and drop-ins; duplicates and ignored `name:`/`bin:` are reported, never silent | P §2.4 |
| Stdin dismissal keyed by the job key; the status band updates in place; scope saved as the form saves | P §2.5–2.7 |
| A kill cut short by quitting still kills (SIGKILL on the cancellation path for a job already asked to die) | P §2.8 |
| Dead `ActiveJob.scope` gone; `theme.py` imports nothing of fieldlog's; the substitution boundary is documented in the code | P §2.9–2.11, D5, D6 |
| README: drop-ins carry the same trust as `--extra-args` | P §2.12 |

The suite is now 438 tests in 41 files; `ruff check` is clean. Committed to `main` on 2026-09-17.

## Done in the 2026-09-17 feature pass (`roadmap.md`)

The proposals were read as one capability — the record carries its meaning, and every reader shows it —
and built from the archive outward. F1 (history in the TUI) was deliberately not built: the maintainer's
path reads results with `history` and `report`, not in the TUI.

| Item | Reference |
|---|---|
| `expect:` — a regex the log must match for the run to pass; `run_passed` is the one reader; record carries `expect: {pattern, found}` | F3, O §8 |
| Chain summary records carry `summary`; step entries carry `summary` and `expect`; the chain-stop line says `expect not met` | F9, O §7 |
| `history --recipe`, `history --fields` (trend table), the overview shows each folder's last record; `report` gains a Summary column | F8, O §2, O §12 |
| `fieldlog note <target> "text"` — a note record, no `amend_record` by design | F2 (CLI half), D §6 |
| The TUI's System transcript is appended to `<workspace>/fieldlog.log` | F10, O §10 |
| `show` prints a preset's contract (`success`, `parse`, `expect`) when set | O §5 |
| macOS: per-platform `SIOCGIFADDR`, `DEFAULT_INTERFACE` (`en0`), `timeout`/`gtimeout` with a refusal, the `platform:` catalog key, Darwin ping/traceroute twins and `scutil`/`lsof`, `macos-latest` in CI | roadmap group 3, D §11 |

The suite is 532 tests in 47 files; `ruff check` is clean.

---

## Next

1. **Push, and read the first macOS CI run.** Nothing Darwin-specific has run on a Mac; `problems.md` § 3.11
   lists what only that run can confirm.
2. **Use it.** Every item above adds to the record or reads it back; a week of real runs will say whether
   `expect:` earns its risk (`roadmap.md` § 5) and which `history --fields` columns matter.
3. **F4 checksums + `verify`** — S. Free at write time; build the verifier when there is a hand-off to
   verify.
4. **F5 workspace report / export** — S–M. At the first multi-host engagement.
5. **The TUI halves left out on purpose** — F1 history in the TUI, a note action (O §15), the previous
   value beside the current in the status band (F8). Only if the maintainer's path changes.

## Then: hygiene that makes the above safer

6. **D11 pane tests** — M spread over time. The stdin bar, status band and scope form now have them
   (2026-09-17); the tree cursor, variant keys and layout switch do not.
7. **D3 narrow the broad `except` guards** — S each, as panes are touched. `_repaint` is the shape.
8. **P §3.7** palette chain rows could name the blocking step — S; `check_chain` already carries it.

## Later: the larger directions

9. **F6 ad-hoc runs and save-as-recipe** — M. Closes the loop between field work and the catalog; a second
   source of truth, so the expensive kind.
10. **D §2 vantage points** — L. The first change to the run model; do it only once the record is complete.
11. **D §5 automation surface** — M. After the injection footguns have become refusals. `expect:` makes a
    chain's exit status meaningful to CI, which is most of what this needs.
12. **D §7 structured parsers** — M. The day a regex `parse:`/`expect:` rule bites on a JSON-speaking tool.

## Not recommended now

- **D §6 JSONL** — deferred by the maintainer; revisit only if a real target folder gets slow.
- **mypy (D9)** — declined by the maintainer on 2026-09-16; not to be re-proposed.
- **D7 `command:` preset field** and **`$LHOST6` (P §3.4)** — both features; queue them behind the field
  notebook work above rather than alongside hygiene.
- **F12 repeat mode** — deferred; F8 makes it more attractive later.
- **A TUI rewrite around a repaint model (D1)** — the mixin split made the code navigable; a full
  invalidation model is worth doing only alongside F1, which is now itself deferred.

---

## How to keep these documents useful

- When an item lands, move its row into "Done" here and mark it in `problems.md` / `features.md` with the
  test that guards it.
- When a **[likely]** item is reproduced, upgrade its tag and add the reproduction.
- Keep the dated reviews as they are; they are the history of *why*.
