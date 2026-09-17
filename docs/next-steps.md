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

The suite went from 305 tests in 27 files to 376 in 35; `ruff check` is clean. The changes are uncommitted
on `fix/archive-correctness-and-tui-split`.

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

7. **P §2.1** soften prompt focus-stealing — S. Two conditions in `_refresh_stdin_bar`.
8. **D2 `reason_kind` from `is_blocked`** — S. The matching is now in one place (`recipes.reason_kind`);
   the last step is to return the kind from `is_blocked` itself and delete the matcher.
9. **P §3.2–3.4** merge-rule reporting, `_stdin_dismissed` key — S together.
10. **D11 pane tests** — M spread over time. Tree cursor, variant keys, stdin bar on a prompt, layout switch.
11. **D9 mypy in CI** — M. Start with `state.py`, `archive.py`, `launch.py`, `chain.py` (small, typed
    dataclasses) and grow.
12. **P §3.5** in-place status band updates — S.
13. **README security note** (P §2.2, §2.3) — S.

## Later: the larger directions

14. **F5 workspace report and export** — S–M. Needed the moment fieldlog is used on an engagement with more
    than one host.
15. **F6 ad-hoc runs and save-as-recipe** — M. Closes the loop between field work and the catalog.
16. **D §2 vantage points** — L. The first change to the run model; do it only once the record is complete.
17. **D §5 automation surface** — M. After the injection footguns have become refusals.

## Not recommended now

- **D §6 JSONL** — deferred by the maintainer; revisit only if a real target folder gets slow.
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
