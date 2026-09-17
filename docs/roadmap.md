# fieldlog — implementation roadmap

**Status:** reference document, written 2026-09-17 before the work it describes, and updated as each
group lands (see § 8). Tags as in `README.md`: **[proposal]** until built, **[confirmed]** once a test
guards it. This document reads `opportunities.md`, `features.md`, `directions.md` and `problems.md`
together and says what to build, in what order, and what not to.

---

## 1. The reading

Strip the proposals to what they change and most of them are the same change. `parse:` and `fields`
(landed), `success:` (landed), `expect:` (F3), chain summaries (F9), trends (F8), notes (F2) and the
session transcript (F10) all put *meaning* on a record or read it back: what the run found, whether it
passed, why the operator made it, what the chain amounted to. None of them touches the run model, the
launch funnel or the pty. The archive format grows only by optional keys.

That is one capability, not seven features: **the record carries its meaning, and every reader shows it.**
`directions.md` § 1 calls it the field notebook. The maintainer's stated direction narrows it further: the
archive and logging side is where the value is, and TUI *viewing* surfaces are not — the people running
the commands read the results with `history` and `report`, not in the TUI. So the notebook is built from
the archive outward, and F1 (history in the TUI), which `next-steps.md` put first, is deliberately not
in this roadmap.

Two proposals that look like part of it are not, and are left out on purpose:

- **F4 checksums + `verify`.** Free to write, but the verifier is a subcommand with no workflow behind it
  yet. A key nobody reads is what `fields` was until this pass; adding another is not progress.
- **F5 workspace report / export.** Real, but only once fieldlog is used on an engagement with several
  hosts. `render_report` is pure and `target_folders` exists; it can land in a day when that happens.

## 2. What is proposed, in two groups

### Group 1 — the record carries its meaning (this pass)

**1a. `expect:` — a verdict from the output.** A preset field, a regex that must match the finished log
(the same last-64-KB window `parse:` reads, last-match rule irrelevant: any match counts). A run *passes*
when its exit code is a declared success **and** its expectation, if it has one, was found. The record
keeps the tool's own `exit_code` untouched and adds `"expect": {"pattern": …, "found": true|false}`,
so a reader next year can see both what was checked and how it went. One reader function,
`recipes.run_passed(code, codes, found)`, replaces `run_succeeded` at every verdict site: the CLI's exit
status, the chain stop policy, the TUI tab and status band, `history`, `report`. Load-time validation
exactly as for `parse:`: a regex that does not compile is reported and dropped; the preset still runs,
now without a check.

Why a regex and not a `fail_if:` over `fields`: one field, one dialect, the same validation code path as
`parse:`, and it needs no parse rule to exist. Every case in the shipped catalog that needs a verdict is a
substring: `Verify return code: 0 (ok)`, `code=2\d\d`, `HTTP/[\d.]+ 2\d\d`. A comparison language over
fields is the complexity `directions.md` § 9 warns about.

**1b. Chain summaries.** The chain's summary record gains `summary`: each step's own summary, prefixed
with its tool id and joined with `→` (`ping: 4 replies · 0% loss → curl: HTTP 200 in 0.31s`), steps
without one skipped. Each entry in its `steps` list gains that step's `summary` and `expect` beside the
`exit_code` and `success` already there, so the step table in a report reads as a checklist. Not adopted
from F9: `fields` namespaced per step on the chain record. Two steps of one recipe would collide, and a
trend over a step's fields is already `history --recipe <step> --fields` on the step's own records.

**1c. Readers.** `history <target> --recipe <key>` filters to one recipe (chain summaries too, as
`chain/<id>`); `history <target> --fields` prints a table with one column per field name seen, in
first-seen order, blank where a run lacks it — the trend view F8 asks for. `history` with no argument
lists each folder's run count *and* its last run's time and summary, instead of the count alone
(O § 12). `report`'s overview table gains a Summary column, so a report of a chain reads as its checklist
before any log is opened. The TUI half of F8 (the previous value beside the current in the status band) is
not built: it is a viewing feature, and the band already shows the current summary.

**1d. `fieldlog note <target> "text"`.** A record with `recipe: "note"`, the text, a timestamp and a run
number of its own — no command, no log, no exit code. `history` and `report` render it in the timeline.
This resolves the amendment question `features.md` F2 and `directions.md` § 6 raise: **there is no
`amend_record`.** A note is a new record, never an edit of an old one, which keeps "the archive is
append-only" true in fact rather than in spirit and needs no lock discipline beyond what `append_record`
has. A note about a run names the run in its text; a `--run` back-reference is not built until a real
workflow asks for it. The TUI note action (O § 15) is not built for the same reason F1 is not.

### Group 2 — the operator's decisions are recorded (next)

**2a. Session transcript (F10).** Every System-tab line the TUI writes is appended, timestamped, to
`<workspace>/fieldlog.log` through the single choke point `write_system_log`. Kill and detach decisions,
scope changes, reloads and args resets become part of the archive. No rotation: one line per event, a
long day is tens of KB. Not opt-in: the workspace *is* the archive, and this is the archive of the session
that produced it. Group 2 rather than group 1 only because it touches `app.py` and nothing group 1
touches, so it can be built and reviewed on its own.

**2b. `show` explains the contract.** `fieldlog show <recipe>` prints the preset's `success:`, `parse:`
and `expect:` when set, beside the flags. Lands with 1a since it is the surface for reading it.

### Group 3 — macOS (added 2026-09-17, after group 2)

The maintainer's call: network engineers carry MacBooks; Windows is out (a ConPTY runner and a second
catalog would be a second product — see `problems.md` § 2.14 for the review that priced it, and § 6 below).

The pty core, the archive lock and the TUI already work on Darwin; what breaks is at the edges:

- **`$LHOST` lookup.** `SIOCGIFADDR` is requested with the Linux ioctl number. Darwin's differs
  (`0xC0206921`) but its `ifreq` puts the address at the same offset, so the fix is the constant, chosen by
  `sys.platform`. Verified in CI by asking the loopback interface (`lo` / `lo0`) for its address.
- **The default interface.** `eth0` everywhere becomes one `DEFAULT_INTERFACE` (`en0` on Darwin), and the
  TUI's loopback rule knows `lo0`.
- **`--timeout`.** coreutils `timeout` is not on a Mac; Homebrew ships it as `gtimeout`. The wrapper is
  resolved at plan time (`timeout`, else `gtimeout`); `run` refuses `--timeout` with an install hint when
  neither is there, rather than running unbounded. `gtimeout` joins the wrapper list so a recipe may start
  with it.
- **The catalog.** Most shipped recipes are the same command on both. The ones that are not are a handful of
  `ping`/`traceroute` flags (`-W 1` is *milliseconds* on Darwin; `-M do`, `-6`, `--mtu`, `-T` do not exist)
  and three Linux-only tools (`ss`, `ethtool`, `resolvectl`). A new optional `platform:` key on a tool or a
  preset (`linux`, `darwin`, or a list) makes an entry exist only on the platforms it names; it is applied
  when a file is *read*, so the same `tool/preset` id written once per platform is two entries in the file
  and one in the catalog, never "defined twice". The ping `parse:` rule is made to read both stats lines
  (`4 received` / `4 packets received`). Darwin gains `scutil/dns` and `lsof/listen` as the stand-ins for
  `resolvectl` and `ss`.
- **CI** runs the suite on `macos-latest` too. README requirements say Linux and macOS.

**Not done for macOS:** nothing in the runner; no per-platform `parse:`; no Homebrew path guessing
(`doctor` says what is missing, which is the right answer).

## 3. Why these belong together, and the dependencies

```text
  parse:/fields (landed) ──┐
  success: (landed) ───────┼──► run_passed()  ◄── expect: (1a)
                           │        │
                           │        ├──► CLI exit status, chain stop policy, TUI tab   (1a)
                           │        └──► record_ok() ──► history, report               (1a, 1c)
                           │
  step summaries ──────────┴──► chain summary record (1b) ──► report step table, Summary column (1c)
  fields on the record ────────► history --fields (1c)
  --note (landed) ─────────────► note records (1d) ──► history / report timeline (1c)
  write_system_log ────────────► transcript (2a)
```

- 1a must land before 1b's step table means anything: a chain step's "pass" is `run_passed`.
- 1b and 1c share the report's table; 1c and 1d share `history`'s rendering loop and the report's
  per-record branch. They are one pass over `cli.py` and `report.py`, done in sequence, not in parallel.
- 2a is independent of all of it.

## 4. Architectural changes

| Module | Change |
|---|---|
| `recipes.py` | `expect_rule(preset)`, `expect_found(pattern, text)`, `run_passed(code, codes, found)`; `_validate_parsers` also validates `expect:` (drop and report on `re.error`) |
| `state.py` | `ActiveJob.expect`, `ActiveJob.expect_found` |
| `launch.py` | captures `expect` onto the job at plan time, like the parse rule |
| `runner.py` | reads the log tail once for both `parse` and `expect`; record gains `expect: {pattern, found}` |
| `chain.py` | step entries carry `summary`/`expect`; stop policy is `run_passed`; summary record carries the joined `summary` |
| `archive.py` | `append_note(target_dir, text) -> record` (reserve a number, build, append) |
| `report.py` | `record_kind(record)` (`run`/`chain`/`note`); `record_ok` reads `expect`; `exit_label` says `expect not met`; Summary column; step table Summary column; note rendering |
| `cli.py` | `note` subcommand and dispatch; `history --recipe/--fields`, the no-arg overview; `run` exit status via `run_passed`; `show` prints the contract |
| `tui/jobs.py` | tab status and band colour via `run_passed`; an `expect · not met` band item |
| `app.py` (group 2) | `write_system_log` appends to `<workspace>/fieldlog.log` |
| `recipes.yaml` | `openssl/chain` gains `expect: 'Verify return code: 0 \(ok\)'` — the one shipped recipe whose exit code says nothing about its finding |

No new module, no new execution path, no new record *kind* beyond the note (the chain summary was the
first). The invariants in `architecture.md` § 10 all hold; #2 gains a clause: *verdicts (`success:`,
`expect:`) are recorded beside the exit code, never in place of it.*

## 5. Risks and trade-offs

- **`expect:` is fragile in the way `parse:` is, and the failure is louder.** A `parse:` regex that
  misses costs a summary; an `expect:` regex that misses *fails the run*, stops a chain, and makes
  `fieldlog run` exit 1. The maintainer already called `parse:` fragile and kept it because it is opt-in;
  the same holds here, with the record showing the pattern and the tail of the log beside the verdict
  so a false failure is readable as one. Only one shipped recipe gets a rule. The README says plainly:
  anchor on the tool's own closing line, and prefer `success:` where the exit code already says it.
- **An interrupted run makes no claim about its expectation** (`found: null`); found by the cold review
  after the first cut archived Ctrl+C as a failed check. A timeout is judged on what it printed.
- **`fieldlog run` cannot return the tool's code when it is 0 and the check failed.** It returns 1
  in that one case; otherwise the tool's non-zero code as before. Documented.
- **A note takes a run number.** So does a chain summary; `history` counts rise and the README already
  documents that. The alternative — notes outside the numbering — would need a second id space.
- **Reader branches.** `history` and `report` now distinguish three record kinds. Kept in one helper,
  `record_kind`, so the branch is written once per reader.
- **CLI output changes.** `history`'s no-argument listing gains two fields per line; `report`'s table
  gains a column; `show` gains lines for recipes with a contract. `--json` shapes are unchanged except
  for the new optional keys, and `doctor` is untouched.
- **The transcript file grows.** Bounded by the operator's own activity; no rotation until someone has a
  file worth rotating.

## 6. Left for later, deliberately

| Item | Why not now |
|---|---|
| F1 history in the TUI, D1 repaint model | Viewing in the TUI is not the maintainer's path; D1 was gated on F1 |
| F4 checksums + `verify` | No workflow yet; hashing at write time is free whenever one appears |
| F5 workspace report / export | Needed at the first multi-host engagement, not before |
| F6 ad-hoc runs / save-as-recipe | A second source of truth for the catalog; the expensive kind of feature |
| F7 shell completion | `list -q \| fzf` covers it; three shells' syntax to keep |
| F11 scope validation in the target modal | TUI, cosmetic; the failure is found one screen later today |
| F12 repeat mode | Deferred by the maintainer |
| P § 3.7 palette chain rows naming the step | Cosmetic |
| D § 2 vantage points, § 4 shared catalogs, § 5 automation, § 7 structured parsers, § 8 serving | Each is a direction, not a feature; § 7 becomes worth it the day a regex rule bites |
| D7 `command:` field, `$LHOST6`, JSONL, mypy | As `problems.md` § 3–4 records |

## 7. How it is built and checked

Two implementation passes by Opus agents, each from a written spec, run in sequence because both touch
`cli.py` and `report.py`: first 1a + 1b + 2b (the verdict), then 1c + 1d (the readers and notes). Group
2a follows on its own. After each pass: the full suite and `ruff`, a read of the diff against the spec,
and the CLI surfaces compared by hand (`show`, `run --json`, `history`, `report` on a fixture archive).
A fresh review of the whole project follows group 2, and its findings go into `problems.md`.

_As done:_ two Opus passes in sequence for group 1, the transcript in parallel on its own files, a cold Opus
review of the whole diff (`problems.md` § 2.14: one real record bug, four consistency findings, all fixed in a
third pass), and the CLI readers compared byte for byte against a fixture archive produced by the pre-pass
code. 438 → 504 tests, 41 → 46 files.

## 8. Status

_Updated as the work lands._

| Group | Status |
|---|---|
| 1a `expect:` | **[confirmed]** built 2026-09-17 — `tests/test_expect.py` |
| 1b chain summaries | **[confirmed]** built 2026-09-17 — `tests/test_expect.py` (chain summary cases) |
| 1c readers | **[confirmed]** built 2026-09-17 — `tests/test_history_views.py`, `tests/test_expect.py` (report) |
| 1d `fieldlog note` | **[confirmed]** built 2026-09-17 — `tests/test_note_command.py` |
| 2a session transcript | **[confirmed]** built 2026-09-17 — `tests/test_transcript.py` |
| 2b `show` contract | **[confirmed]** built 2026-09-17 — `tests/test_expect.py` (show cases) |
| 3 macOS | **[likely]** built 2026-09-17 — `tests/test_platform.py`; the suite passes here with `sys.platform` forced to `darwin`, but no Mac has run it: the `macos-latest` CI leg is the proof (`problems.md` § 3.11) |
