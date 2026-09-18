# fieldlog documentation

| Document | What it is for |
|---|---|
| [`architecture.md`](architecture.md) | How the system actually works today: modules, the four execution paths, state, persistence, the archive, the TUI's shape, tests and CI. Confirmed from the code, not from names or docstrings. |

The [project README](../README.md) is the user-facing document: install, the TUI, recipes, the CLI and the
archive's layout. This folder is for how the implementation fits together.

Claims in `architecture.md` are tagged **[confirmed]** (reproduced by running the code, or read directly
from it) or **[likely]** (inferred from reading, not executed). Line references are of the form
`module.py:NN` and were correct on the date of the document; they drift.
