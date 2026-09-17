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

---

## Next: finish the field notebook (D §1)

1. **F1 History in the TUI** — M. The biggest gap between the pitch and the product. Do this before anything
   that adds more to the record, so that what is added is visible where the work happens.
2. **F3 `expect:`** — M. Turns recipes into checks and chains into checklists. Design first: one reader
   function for "did this run pass" (codes + verdict) so the R1 mistake is not repeated.
3. **F9 chain summaries** and **F8 trends** — S each. Both are readers of data that now exists.
4. **F4 checksums + `verify`** — S. Free at write time; the verifier is the only new code.
5. **F2 second half** — `fieldlog note`, `amend_record`, a TUI note action — S–M. Decide the amendment
   model (in-place vs. amendment record, D §6) before writing `amend_record`.
6. **F10 session transcript** — S.

## Then: hygiene that makes the above safer

7. **D11 pane tests** — M spread over time. The stdin bar, status band and scope form now have them
   (2026-09-17); the tree cursor, variant keys and layout switch do not.
8. **D3 narrow the broad `except` guards** — S each, as panes are touched. `_repaint` is the shape.
9. **P §3.7** palette chain rows could name the blocking step — S; `check_chain` already carries it.

## Later: the larger directions

10. **F5 workspace report and export** — S–M. Needed the moment fieldlog is used on an engagement with more
    than one host.
11. **F6 ad-hoc runs and save-as-recipe** — M. Closes the loop between field work and the catalog.
12. **D §2 vantage points** — L. The first change to the run model; do it only once the record is complete.
13. **D §5 automation surface** — M. After the injection footguns have become refusals.

## Not recommended now

- **D §6 JSONL** — deferred by the maintainer; revisit only if a real target folder gets slow.
- **mypy (D9)** — declined by the maintainer on 2026-09-16; not to be re-proposed.
- **D7 `command:` preset field** and **`$LHOST6` (P §3.4)** — both features; queue them behind the field
  notebook work above rather than alongside hygiene.
- **F12 repeat mode** — deferred; F8 makes it more attractive later.
- **A TUI rewrite around a repaint model (D1)** — the mixin split made the code navigable; a full
  invalidation model is worth doing only alongside F1, which is the next big TUI feature and would be the
  first consumer.

---

## How to keep these documents useful

- When an item lands, move its row into "Done" here and mark it in `problems.md` / `features.md` with the
  test that guards it.
- When a **[likely]** item is reproduced, upgrade its tag and add the reproduction.
- Keep the dated reviews as they are; they are the history of *why*.
