# fieldlog documentation

A map of what is in this folder, and the conventions the documents share.

## Maintained

| Document | What it is for |
|---|---|
| [`architecture.md`](architecture.md) | How the system actually works today: modules, the four execution paths, state, persistence, the archive, the TUI's shape, tests and CI. Confirmed from the code, not from names or docstrings. |
| [`problems.md`](problems.md) | What is still open, deferred or rejected, and the technical debt. Each item says how sure we are and what its status is. |
| [`next-steps.md`](next-steps.md) | The one list: what has landed, what is next, what is not being done and why. |

## History (kept as written)

| Document | What it is |
|---|---|
| [`history/roadmap.md`](history/roadmap.md) | The 2026-09-17 reading of the proposals as one capability, the plan, and its status table — all landed. |
| [`history/features.md`](history/features.md), [`history/opportunities.md`](history/opportunities.md), [`history/directions.md`](history/directions.md) | The 2026-09-16 proposals: concrete features, cheap wins the architecture allowed, and speculative directions. Built items are marked inline; the rest are in `next-steps.md` with reasons. `directions.md` § 11 records the platform decision (macOS in, Windows out). |
| [`history/problems-fixed-2026-09-16-17.md`](history/problems-fixed-2026-09-16-17.md) | Every finding the 2026-09-16 investigation, the 2026-09-17 remediation and the 2026-09-17 feature-pass review fixed, with reproductions and guarding tests. `problems.md` cites its `§ 1.x` / `§ 2.x` numbers. |
| [`history/review-2026-09-15.md`](history/review-2026-09-15.md) | The first open-ended code review. Its status block records what was actioned. |
| [`history/architecture-review-2026-09-16.md`](history/architecture-review-2026-09-16.md) | The structural review that led to the archive-correctness and TUI-split changes. Its architecture map is superseded by `architecture.md`. |
| [`history/investigations/2026-09-13-ssh-wireshark.md`](history/investigations/2026-09-13-ssh-wireshark.md) | An experiment log: what it takes to run ssh + a GUI through fieldlog. The README's "GUI recipes" section is distilled from it. |
| `history/superpowers/` | The spec and plan for the localtime standardisation, written for an agentic workflow. Done; kept for the record. |

## Conventions

Every claim carries one of four tags, so a reader can tell evidence from opinion:

- **[confirmed]** — reproduced by running the code (a test, a command, a script), or read directly from the code with no interpretation needed.
- **[likely]** — inferred from reading the code; a real problem or behaviour in all probability, but not executed.
- **[proposal]** — a concrete change we recommend, with enough detail to implement.
- **[speculative]** — a direction worth thinking about; not a recommendation.

Line references are of the form `module.py:NN` and were correct on the date of the document; they drift.
