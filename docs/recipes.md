# Writing recipes

The reference for the YAML behind every recipe and chain. The [project README](../README.md#recipes)
covers where drop-in files go, the built-in themes and switching a theme off;
[`recipes.d/example.yaml.sample`](../recipes.d/example.yaml.sample) is one to copy.

## Format

```yaml
recipes:
  - id: ping                          # tool id (required)
    name: "ICMP Reachability"         # display name
    bin: ping                         # executable checked in $PATH (default: id)
    presets:                          # required
      - id: quick                     # recipe ID becomes ping/quick
        name: "quick · 4 probes"
        flags: "-c 4 -W 1 $TARGET"
      - id: sweep
        name: "subnet sweep"
        flags: "-c 1 -W 1 -I $IFACE $TARGET"
```

| Field | Level | Required | Meaning |
|-------|-------|----------|---------|
| `id` | tool | yes | Tool id, first half of the recipe ID |
| `name` | tool | no | Display name |
| `bin` | tool | no | Executable to check in `$PATH` and run. Default: `id` |
| `presets` | tool | yes | List of presets |
| `id` | preset | yes | Second half of the recipe ID |
| `flags` | preset | yes | What to run after `bin` (see below) |
| `name` | preset | no | Display name |
| `bin` | preset | no | Overrides the tool's `bin` for this preset |
| `outdir` | preset | no | `true` creates `$OUTDIR` even when `flags` don't mention it (see below) |
| `parse` | preset | no | Regex run over the finished log; what it finds becomes the run's `summary` (see below) |
| `summary` | preset | no | Template filled from `parse`'s named groups. Without it the whole match is used |
| `success` | preset | no | Exit codes this recipe calls a success (see below). Default: `0` alone |
| `expect` | preset | no | Regex the finished log must match for the run to pass (see below) |
| `scan` | preset | no | `true` records every file that changed under the target folder, not just `$OUTDIR` (see below) |
| `platform` | tool or preset | no | `linux`, `darwin` or a list; the entry exists only on those platforms (see below) |

A recipe that needs different flags on different systems is written once per
platform under the same id. `platform:` is applied when the file is read, so only
this system's entry is ever in the catalog; on a tool it applies to every preset.
The shipped `ping/quick` is `-W 1` on Linux and `-t 6` on macOS, and `ss`,
`ethtool` and `resolvectl` are absent there, where `lsof` and `scutil` stand in.

## How the command is built

- The command is `bin`, a space, and `flags`, run by `/bin/sh` on a pty.
  `flags` is shell syntax: quote values with spaces; pipes, `;` and `$(...)` work.
- If `flags` starts with the binary's own name or with a wrapper (`timeout`,
  `sudo`, `doas`, `env`, `nice`), it is used as the whole command:

  ```yaml
  flags: "timeout 60s ping -c 4 $TARGET"
  ```

  The `$PATH` check still uses `bin`.
- A single simple command is `exec`'d, so the tool is the process fieldlog waits
  on and the recorded exit code is the tool's own. Redirections (`> out.txt`,
  `2>&1`) and backquotes still count as one command. A pipeline (`|`), a list
  (`;`, `&&`, `||`, a trailing `&`), parentheses, including `$( … )` and
  `$(( … ))`, or a line break keeps the shell in front, and the exit code is
  then the shell's view of it.
- If the flags reference `$OUTDIR`, that per-run folder is created before the
  command starts, so a tool that writes side files (`-w`, `--logfile`, `-oA`)
  has somewhere to put them. Whatever it writes is recorded in the run's record
  afterwards; nothing has to be declared.
- A script that writes to `$OUTDIR` from its environment doesn't show it in the
  flags. Set `outdir: true` on its preset and the folder is created anyway:

  ```yaml
  - id: script
    flags: "env bash /home/operator/bin/capture.sh $TARGET"
    outdir: true
  ```

## Summaries

A preset can say what to pull out of its own output. `parse` is a regex run over
the finished log, and `summary` is a template filled from its named groups:

```yaml
- id: quick
  flags: "-c 4 -W 1 $TARGET"
  parse: '(?P<rx>\d+) received, (?P<loss>[\d.]+)% packet loss'
  summary: "{rx} replies · {loss}% loss"
```

`history`, `report` and the TUI's status band then show `4 replies · 0% loss`
beside that run, and the record carries it as `summary`.

- The last match wins, so a rule can name a tool's closing statistics without
  anchoring to them.
- Without `summary` the whole match is used. A template may only reference
  groups the regex names.
- The named groups are kept on the record as `fields`
  (`{"rx": "4", "loss": "0"}`) beside the formatted summary, so `history --json`
  can trend a value across runs without re-parsing the logs.
- The last 64 KB of the log is scanned, so a server recipe that ran for hours
  still summarises at the same cost. A `parse:` or `expect:` regex gets 2 s
  there; one that runs longer (a pattern that backtracks without end, such as
  `(a+)+b`) is not applied, the run says so, and it is archived without a
  summary and with its expectation unchecked.
- A regex that does not compile, or a template naming a group that does not
  exist, is reported at load; the preset still runs, without a summary.

## Exit codes

A run succeeded if the tool exited 0. `success:` says otherwise for the recipes
where that is wrong — `grep` reports "nothing found" as 1, and plenty of tools
print their usage and exit 2:

```yaml
- id: find
  flags: "-rl $TARGET /etc"
  success: [0, 1]                   # 1 is "no match", not a failure
```

- List every code that counts, `0` included. A preset saying `success: [1]` has
  said only 1, and a run exiting 0 is then a failure. Nothing is implied.
- The record always keeps the tool's own `exit_code`. The codes are stored
  beside it, so a report rendered next year still knows why 1 was fine.
- One rule, four readers: `fieldlog run` exits 0, `history` and `report` read
  `1 (ok)` instead of a failure, a chain carries on to its next step, and the
  TUI's tab shows done.
- Usage text and errors usually go to stderr. fieldlog merges stderr into the
  run log, so it is captured whatever the exit code turns out to be.

## Expectations

`expect:` is a regex that has to match the finished log — the same last 64 KB
`parse` reads — for the run to pass. It is for the tools whose exit code says
nothing about what they found:

```yaml
- id: chain
  flags: "s_client -connect $HOST:443 -showcerts"
  parse: 'Verify return code: (?P<code>\d+) \((?P<what>[^)]+)\)'
  summary: "verify: {what}"
  expect: 'Verify return code: 0 \(ok\)'   # openssl exits 0 on a bad chain too
```

- The exit code stays the tool's own. The record carries the check beside it as
  `"expect": {"pattern": "…", "found": true}`, so a reader next year sees both
  what was checked and how it went.
- Any match, anywhere in the window, counts.
- One rule, the same readers as `success:`: `fieldlog run` exits 1 for a tool
  that exited 0 but missed its expectation, `history` and `report` read
  `0 (expect not met)`, a chain stops at that step unless it says
  `continue: true`, and the TUI's tab shows failed.
- An interrupted run (`Ctrl+C`) is left unchecked, `"found": null`, rather than
  counted as a miss: the tool never got to print its closing line. A timed-out
  run is still checked, and fails on exit 124 anyway.
- A regex that does not compile is reported at load; the preset still runs,
  without the check.
- An expectation that is too specific fails good runs. Anchor on the tool's own
  closing line, and prefer `success:` wherever the exit code already says it.

## Variables

fieldlog fills these in before running and shows the result in previews.
`${NAME}` works too. Every variable below is also exported into the job's
environment, under both spellings, so whatever fieldlog leaves alone still
expands in the shell: any other `$VAR`, shell forms like `${TARGET:-x}`, and a
script that reads `$OUTDIR` itself.

| Variable | Value |
|----------|-------|
| `$TARGET`, `$TARGET_IP` | Target IP, CIDR, hostname or ssh `user@host` |
| `$HOST`, `$TARGET_HOST` | DNS name. If unset and the target is a hostname (not `user@host`), the target. Presets using it are not runnable without one |
| `$LHOST` | Local IP: the one given, else the interface's IPv4 address, read when the job starts (see [TUI](../README.md#tui) to set one there). Presets using it are not runnable without one |
| `$IFACE` | Interface name |
| `$OUTDIR`, `$OUT_DIR` | Per-run folder for files the tool writes, `raw/<timestamp>_<run>`, relative to `targets/<name>/`, where every run starts. Absolute only under a log destination, which must then be a path without spaces or shell characters |
| `$RUN_ID` | Run number, `01`, `02`, … Set in the environment only |

A preset is **not runnable** while its variables are unmet or its binary is
missing. The TUI says why; the CLI refuses with the same reason. Targets and
DNS names may only contain letters, digits, `.`, `:`, `/`, `-` and `_`, since
they are pasted into a shell command. Targets may also contain `@`, for an ssh
`user@host`, and `%`, for an IPv6 address's zone (`fe80::1%eth0`). None of
them may start with `-`, which would be read as one more
flag (`--target=-f` is a flood ping, not a target), and a target made only of
digits and dots has to be a valid address: `10.0.0.256` is refused rather than
run. `--extra-args` and TUI args edits are appended as typed and are not
checked; they are the operator's own shell.

## Chains

A **chain** is a named, ordered list of recipe IDs run one after another
against the current scope. Chains live beside `recipes:` in the same files:

```yaml
chains:
  - id: reach                                 # chain id (required, cannot be a tool id)
    name: "reachability · ping, trace, ptr"   # display name (default: the id)
    steps:                                    # required, at least one
      - ping/quick                            # a step; the chain stops if it fails
      - recipe: traceroute/icmp               # the same step, written out
        continue: true                        # …but keep going if this one fails
      - dig/ptr
```

| Field | Level | Required | Meaning |
|-------|-------|----------|---------|
| `id` | chain | yes | How the CLI and TUI name it. Must not match a tool id |
| `name` | chain | no | Display name |
| `steps` | chain | yes | Recipe IDs in order. A bare tool id means its first preset |
| `recipe` | step | yes | `tool/preset`, in the mapping form |
| `continue` | step | no | `true` keeps the chain going when this step fails |

- A chain stops at the first step that does not pass — a non-zero exit outside
  its `success:` codes, or a missed `expect:` — unless that step says
  `continue: true`. `Ctrl+C` always stops it. The chain exits with the first
  failing step's code, even one it continued past (1 if that step only missed
  its expectation, 130 if interrupted), so a failed `continue: true` step still
  makes the chain exit non-zero.
- A chain is runnable only when every step is. The reason names the step.
- Every step shares one `$OUTDIR` — the first step's — so side files from one
  chain land together. Logs stay separate, one per step.
- Each step is archived as its own run record, tagged with its position. When
  the chain ends, it appends one summary record named `chain/<id>` to the same
  `session.json`: each step's run number, exit code, `summary` and `expect`,
  where it stopped, and the shared `$OUTDIR`. Its own `summary` joins the
  steps' with `→`, so `report` reads the chain as a checklist. It takes a run
  number too, so a three-step chain that runs to the end adds four runs to
  `history`'s count.
- Steps are checked once every file is merged, so a built-in chain may name a
  preset that a drop-in adds. A chain with a bad id, no steps, or an unknown
  recipe is skipped with a message; the rest of the catalog still loads.

`fieldlog list` shows chains in their own section, one line each. A `?` marks a
step the chain continues past:

```text
reach      reachability · ping, trace, ptr    ping/quick → traceroute/icmp? → dig/ptr
```

In the TUI, chains sit under the recipes. Selecting one turns the VARIANTS
pane into `STEPS`: the chain's steps in order, `?` on the ones it continues
past, `▸` on the one being read. `↑ ↓`, `,` `.` and the number keys move the
`▸`, and the ARGS band shows that step's command resolved against the current
scope; reading a step does not change what runs. `Enter` runs the whole chain,
one tab per step. Args edits made with `E` on a recipe still apply inside a
chain, and a step carrying one is marked `*`. The steps themselves are edited
in the YAML.

## Drop-in files

Drop-ins load after the built-in themed catalog, in filename order: the config
directory first, then `./recipes.d/`.

- A new tool `id` adds a tool. A new chain `id` adds a chain.
- An existing tool `id` adds its presets to that tool. A preset or chain with an
  existing `id` replaces the earlier one; the recipe manager (`M`) lists these
  overrides.
- The same filename in both directories is loaded once: the local `./recipes.d/`
  file wins and the config-directory file is not read at all, so even ids that
  only it defines are gone. The shadowing is reported.
- A file that fails to parse is reported, with the line, and skipped. The rest
  still load. Editor leftovers (`*~`, `*.swp`, `*.bak`, `*.orig`) are ignored.
- A recipe's `flags` is shell. `./recipes.d/` is read from wherever you start
  fieldlog, so treat a checkout's drop-ins the way you would its `Makefile`: do
  not run one you have not read.

Adding a preset to the built-in `ping`:

```yaml
recipes:
  - id: ping
    presets:
      - id: gentle
        name: "single probe"
        flags: "-c 1 -W 1 $TARGET"
```

In a checkout, the repo's `recipes.d/` holds only
[`example.yaml.sample`](../recipes.d/example.yaml.sample). Anything else in it is
ignored by git, so drop-ins you keep there while testing stay out of commits.

## GUI recipes

A recipe can open a window and run until you close it. `rcap/ssh` in
[`fieldlog/recipes.d/50-capture.yaml`](../fieldlog/recipes.d/50-capture.yaml)
streams a remote `tcpdump`
over ssh into a local Wireshark and keeps the capture as a run artifact:

```yaml
flags: >-
  env ssh $TARGET "(tcpdump -i ${RIFACE:-eth0} -U -s0 -w - not port 22; kill 0) & cat >/dev/null; kill 0"
  | tee $OUTDIR/remote.pcap
  | wireshark -k -i -
```

- **Keep the GUI in the foreground.** The run lasts until the window closes and
  records the GUI's exit code. `wireshark &` or `nohup wireshark &` is killed
  as soon as the shell exits.
- **No `--timeout`.** It closes the window when time runs out.
- **Start fieldlog where a GUI can open.** Jobs inherit fieldlog's environment,
  so `DISPLAY` or `WAYLAND_DISPLAY` must be set in that terminal.
- **`env` in front** stops fieldlog prepending `bin`, which would give
  `wireshark ssh …`. The `$PATH` check still uses `wireshark`.
- **Never `ssh -t` in a pipe.** A remote terminal rewrites bytes and corrupts
  the stream.
- **`(…; kill 0) & cat >/dev/null; kill 0`** stops the remote capture when you
  close the window or press `Ctrl+C`, even on a quiet interface.
- The target can be `user@host` or a `Host` alias from `~/.ssh/config`.
  `RIFACE=ens192 fieldlog run rcap/ssh jump1` picks the remote interface.
