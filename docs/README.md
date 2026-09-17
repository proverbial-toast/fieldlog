# fieldlog documentation

A map of what is in this folder, and the conventions the documents share.

## The reference set (maintained)

| Document | What it is for |
|---|---|
| [`architecture.md`](architecture.md) | How the system actually works today: modules, the four execution paths, state, persistence, the TUI's shape, tests and CI. Confirmed from the code, not from names or docstrings. |
| [`problems.md`](problems.md) | Bugs found and fixed, bugs still open, likely problems, and technical debt. Each item says how sure we are. |
| [`opportunities.md`](opportunities.md) | Things the existing architecture already makes cheap that the project is not taking advantage of. |
| [`features.md`](features.md) | Concrete feature proposals, each with the user problem, fit, supporting components, new architecture, difficulty and risks. |
| [`directions.md`](directions.md) | More speculative directions: what fieldlog could become if its core ideas were pushed further, and how they fit together. |
| [`next-steps.md`](next-steps.md) | The recommended order of work, with effort estimates. |

## Dated snapshots (historical, kept as written)

| Document | What it is |
|---|---|
| [`review-2026-09-15.md`](review-2026-09-15.md) | The first open-ended code review. Its status block records what was actioned. |
| [`architecture-review-2026-09-16.md`](architecture-review-2026-09-16.md) | The structural review that led to the archive-correctness and TUI-split changes. Its architecture map is superseded by `architecture.md`; its open items (#5 repaint model, #10 `reason_kind`) are tracked in `problems.md`. |
| [`investigations/2026-09-13-ssh-wireshark.md`](investigations/2026-09-13-ssh-wireshark.md) | An experiment log: what it takes to run ssh + a GUI through fieldlog. The README's "GUI recipes" section is distilled from it. |
| `superpowers/` | The spec and plan for the localtime standardisation, written for an agentic workflow. Done; kept for the record. |

## Conventions

Every claim in the reference set carries one of four tags, so a reader can tell evidence from opinion:

- **[confirmed]** — reproduced by running the code (a test, a command, a script), or read directly from the code with no interpretation needed.
- **[likely]** — inferred from reading the code; a real problem or behaviour in all probability, but not executed.
- **[proposal]** — a concrete change we recommend, with enough detail to implement.
- **[speculative]** — a direction worth thinking about; not a recommendation.

Line references are of the form `module.py:NN` and were correct on the date of the document; they drift.

The investigation behind the reference set was done on 2026-09-16 against branch
`fix/archive-correctness-and-tui-split`, and a remediation pass over its findings followed on 2026-09-17.
`problems.md` § 1 records what the investigation fixed as it went, § 2 what the remediation pass fixed, and
§ 3–4 the state of everything else; `next-steps.md` has a "Done" table for each.
