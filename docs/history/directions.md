# fieldlog — directions

_Moved to `history/` on 2026-09-17. § 11 (macOS in, Windows out) is the one decision here that still binds; `../next-steps.md` carries it._

**Status:** reference document, 2026-09-16. Everything here is **[speculative]** unless marked otherwise:
these are directions the core ideas could be pushed in, with the reasoning and, where it matters, a migration
path. `features.md` holds the concrete proposals; this document asks what they add up to.

---

## 0. What fieldlog is, stated as an idea rather than a feature list

Strip the catalog, the TUI and the network vocabulary away and what is left is: **a runner that makes any
command's execution into evidence** — the exact command, its environment, its own exit code, what it printed,
what it wrote, when, and against what. The catalog is a way of getting to that runner quickly; the TUI is a
way of running several at once; `report` is a way of reading the evidence back.

Every direction below is one of those four things pushed further. The most interesting ones push the runner
and the evidence, because that is what nothing else on the box does.

---

## 1. The field notebook (the coherent near-term direction)

Take the archive from "a folder of logs with an index" to "a notebook you would hand to the next engineer":

- **notes** (F2) give it prose; **fields** (this pass) and **trends** (F8) give it measurements;
- **`expect:`** (F3) gives it verdicts, so a chain reads as a checklist with pass/fail;
- **checksums** (F4) make it citable; **workspace reports and export** (F5) make it deliverable;
- **history in the TUI** (F1) and the **session transcript** (F10) make the notebook visible where the work
  happens, and record the operator's decisions as well as the tools'.

None of these changes the run model, the launch funnel or the archive format beyond additive keys. Together
they change what the project *is* — from a launcher that keeps logs to a record of an engagement — and each
is small enough to ship on its own. This is the direction `next-steps.md` sequences.

---

## 2. Vantage points: running the same recipe from somewhere else

Network diagnosis is about *where you look from*. "It works from the jump host but not from here" is half
of every ticket. The `RunStep` seam and the serialisable `LaunchPlan` mean a recipe could run on a remote
host with the same archive semantics:

- a scope field `via: jump1` (or per-recipe `runner: ssh`) makes `run_job` spawn `ssh -T jump1 sh -c '<exec
  form>'` with the env passed inline, stream the same pty, and `scp` `$OUTDIR` back on completion;
- the record gains `vantage: "jump1"`; `report` groups by it; a chain could run the same step from two
  vantage points and the trends view compares them.

**Migration path.** Nothing in `chain.py` or the archive changes. `plan_launch` gains a `vantage` and
`run_job` gains a transport. The hard parts are the ones the ssh investigation already mapped: prompts
(host keys, passphrases — answerable through the existing pty), the `-t` corruption rule, and interrupting
the remote side (the `(cmd; kill 0) & cat; kill 0` idiom from `recipes.d/examples.yaml`).

**Risks.** A second execution path to keep honest; secrets in remote environments; the artifact pull is a
new failure mode mid-record.

---

## 3. Runbooks: chains that carry intent

Chains are ordered recipe ids with a stop policy. With `expect:` and notes they become checkable runbooks:
"verify the change window" = `reach`, then `https-check`, each step with an expectation, the chain's record
a pass/fail per step. Two further steps make that a product:

- **parameters on chains** (`params: {port: 443}` substituted like scope vars), so one runbook serves many
  services;
- **a runbook report** (`report --chain https-check`) that reads as a checklist across every time it was run.

This overlaps with what monitoring does, but from the operator's side: on demand, from the field, with the
evidence kept. **Difficulty** M–L; the only new concept is chain parameters.

---

## 4. The catalog as a shared artefact

Drop-ins are per user or per directory. Teams share recipes by copying YAML. The pieces for something better
exist: the manager modal knows every source and override; `list --json` and `doctor --json` describe the
catalog; `Shift+R` reloads. A `fieldlog catalog add <git-url-or-path>` that clones into
`~/.config/fieldlog/recipes.d/<name>/` (the loader would need to walk one level of subdirectories) gives
teams versioned, reviewable recipe sets without inventing a package format. **Difficulty** S–M. **Risk**:
running someone else's shell — the trust note in `problems.md` § 2.3 becomes important.

---

## 5. An automation surface

Everything the CLI does is already machine-readable: `list --json`, `doctor --json`, `run --json` (now the
real record), `history --json`. That is enough for:

- CI: "run `https-check` against staging after deploy, fail the job on the chain's verdict, attach the report";
- an agent: an LLM tool that plans (`doctor` says what is runnable), runs (`run --json`), and reads back
  (`history --json`), with the archive as the audit trail of what the agent did — which is precisely the
  property an operator would demand before letting an agent touch a network.

A thin MCP server or HTTP shim over the CLI is a day's work. The interesting design question is not the
transport but *what must stay human*: `--extra-args`, ad-hoc `-c` and any scope value should still pass
`is_blocked`, and the runner's "no auto-answering" rule (`send_stdin`'s docstring) is the right default for
prompts. **Difficulty** M. **Risk**: an automation surface makes the injection footguns (`problems.md`
§ 2.2) matter more; they would need to become refusals.

---

## 6. The archive format, if it ever has to change — **[migration sketch, not a recommendation]**

`session.json` as one JSON array is simple, human-readable and rewritten whole on every append. The
maintainer has deferred JSONL; if a target ever holds tens of thousands of records the change is contained:

- **writer:** `append_record` appends one line to `session.jsonl` under the same lock (O(1));
- **readers:** `load_runs`, `load_target_history`, `find_target_dir` and `handle_history` read either file
  (four call sites, one helper);
- **migration:** `fieldlog migrate` converts in place; both files existing means the JSON one is the
  archive up to the switch and the JSONL one after, and the reader concatenates.

The one thing to decide first is whether `amend_record` (F2) is wanted, because an append-only line format
makes amendments into a second record (`kind: "amend", of: "04"`) rather than an in-place edit — arguably
the more honest model for an evidence log.

---

## 7. Structured parsers beyond regex

`parse:` is a regex over text. Several catalogued tools speak JSON or XML (`iperf3 --json`, `nmap -oX`,
`curl -w '%{json}'`). A `parse: {json: "$.end.sum_received.bits_per_second"}` form (a minimal JSONPath, or
`jq` via subprocess when present) would give exact fields without fragile regexes. Fits the load-time
validation model (a bad path costs the summary). **Difficulty** M. **Risk**: a second parser dialect to
document.

---

## 8. Serving the TUI

Textual apps can be served to a browser (`textual serve`). For a jump host with no local terminal
emulator worth the name, `fieldlog serve` would give the same TUI over HTTP with the same archive
underneath. Cheap to try; the OSC 52 clipboard path would need a web equivalent. **Difficulty** S to try,
M to ship.

---

## 9. What not to do

- **Do not make fieldlog a scheduler or a monitoring system.** Repeat mode (F12) is a loop; alerting,
  dashboards and retention policies are someone else's product, and the archive-as-evidence idea is
  weakened by every record that was produced by a cron job nobody was watching.
- **Do not put the catalog in a database.** YAML with drop-ins is the reason a recipe can be reviewed in a
  pull request.
- **Do not parse prompts semantically.** The timeout heuristic is crude and it is why sudo, ssh and `read`
  all work without a table of known prompts.

---

## 10. How the directions fit together

```text
                    ┌──────────────────── the runner makes execution into evidence ────────────────────┐
                    │                                                                                   │
   catalog ──► plan_launch ──► run_job ──► record ──► session.json ──► history / report / TUI history   │
      ▲             │              │           │                              │                          │
      │        vantage (§2)   transport   notes · fields · verdict ·     trends · runbook report ·       │
      │                                   checksums (§1)                 export (§1, §3)                 │
      │                                                                       │                          │
   save-as-recipe (F6) ◄──── ad-hoc runs ◄──────────────────── automation surface (§5) ◄────────────────┘
   shared catalogs (§4)
```

The near-term direction (§1) is entirely on the right-hand side of the record: richer records, better
readers. Vantage points (§2) and automation (§5) come after, because both depend on the record being
trustworthy and complete first. Shared catalogs (§4) and structured parsers (§7) are independent and can
land whenever someone needs them.

---

## 11. Platforms — **[decided 2026-09-17]**

**macOS: in.** Network engineers carry MacBooks. The pty core, the archive lock and the TUI are BSD-clean;
what needed changing was the `SIOCGIFADDR` ioctl number, the default interface name, the `timeout` wrapper
(Homebrew's `gtimeout`), and a handful of `ping`/`traceroute` flags, which is what the `platform:` catalog
key is for. `roadmap.md` § 2 group 3.

**Windows (native): out.** Not a port but a second product: no `pty`, `fcntl`, `termios`, `setsid` or
`killpg`; the Proactor loop cannot `add_reader` a pty; the recipe format is `sh` text and `cmd.exe` breaks
every pipe, quote and `${VAR:-x}` in the shipped recipes; the useful tools are different ones (`ping -n`,
`tracert`, `nslookup`, `Test-NetConnection`). It would need a ConPTY twin of `run_job` (the code both
reviews said to leave alone), Windows process-group kills, a second catalog and a shell decision, and then
two runners to keep honest. WSL runs fieldlog as it is. A pipes-only Windows runner (no prompts, argv-only
recipes) is the one middle ground, and it is not planned.
