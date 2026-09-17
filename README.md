# fieldlog

![fieldlog](fieldlog.jpg)

fieldlog - log what you did in the field.

A terminal UI and CLI for running network diagnostic tools from a catalog of
saved commands, and keeping a log of every run. You pick a tool and a target;
fieldlog runs the command, streams the output, and files the log plus a run
record under `targets/<name>/`. The archive is the point: a week later you can
see exactly what was run against a host, when, with what result.

```bash
fieldlog run ping/quick 192.168.1.20
fieldlog history 192.168.1.20
fieldlog report 192.168.1.20 -o ping-report.md
```

```text
targets/192.168.1.20/
├── session.json                                  # one record per run
└── raw/20260912T180156_ping_quick_01.log         # what ping printed
```

## Requirements

- Linux (the runner uses a pty and `SIOCGIFADDR`)
- Python 3.11 or newer
- The tools themselves: ping, curl, dig, traceroute and so on. fieldlog does not
  bundle them. A tool missing from `$PATH` is listed but marked as unavailable.
- Root, or the right capabilities, for tools that need it (tcpdump, arp-scan,
  `ping -f`). fieldlog does not escalate; write `sudo` into the recipe if you want it.

## Install

```bash
pipx install git+https://github.com/proverbial-toast/fieldlog
fieldlog            # opens the TUI
```

For development:

```bash
git clone https://github.com/proverbial-toast/fieldlog && cd fieldlog
uv sync --extra dev             # or: python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
uv run pytest                   # or: pytest
```

## Terms

| Term | Meaning | Example |
|------|---------|---------|
| **Tool** | An entry under `recipes:`: one binary plus its presets. | `ping` |
| **Preset** | One saved set of flags for a tool. Called a **variant** in the TUI. | `quick` |
| **Recipe ID** | `tool/preset`, how the CLI names something runnable. | `ping/quick` |
| **Chain** | An entry under `chains:`: recipe IDs run in order against one scope. | `reach` |
| **Scope** | The target, DNS name, interface and local address a run is aimed at. | `192.168.1.0/24` |
| **Workspace** | The archive root, `./targets` by default. One folder per target inside it. | `~/audits` |

## TUI

Run `fieldlog`, or `fieldlog tui`. Press `?` for the full key list.

To start with the scope already set, pass it to `tui`. It takes the same scope
flags as `run`:

```bash
fieldlog tui 192.168.1.20 -i eth0                        # or -t 192.168.1.20
fieldlog tui -t 192.168.1.0/24 -H router1 -i eth0 -w ~/audits
```

The scope is remembered per workspace in `<workspace>/.last-scope.json`, so a
bare `fieldlog` resumes where you left off; any flag you pass overrides the
remembered value. If no interface is passed or remembered, the TUI starts on
`eth0` if it has an IPv4 address, else on the first other interface that has
one (never `lo`). An interface you name is kept even with no address yet, such
as a VPN that comes up later. `T` changes all of it at runtime.

`$LHOST` is the address the interface has when a job starts, unless you set one:
with `-l`, or by typing `tun0 / 10.8.0.2` in the interface field of `T`. Typing
just `tun0`, or clicking an interface in the list, goes back to its own address.
Only an address you set is remembered. `fieldlog run` never switches
interfaces: without `-i` it uses `eth0`.

### Keys

| Key | Action |
|-----|--------|
| `T` | Set target, DNS name, interface, local address and log destination: where logs and `$OUTDIR` go instead of the workspace, `--artifact-root` on the CLI |
| `↑ ↓` `j k` | Move within the focused pane |
| `Tab` | Switch focus between RECIPES and VARIANTS |
| `Enter` | On a tool: jump to its variants. On a variant or chain: run it |
| `1`–`9`, `0` | Select variant 1–10 of the current tool |
| `,` `.` | Previous / next variant |
| `/` | Filter recipes. `Esc` clears it |
| `!` | Toggle runnable-only (default) / show everything |
| `Ctrl+P` | Palette. `Enter` loads a recipe's args, `Shift+Enter` runs it. Type `>` for commands only |
| `E` | Edit the args of the selected variant (raw template). `Esc` or `Ctrl+J` applies |
| `R` | Reset edited args to the variant default |
| `P` | Pin / unpin the selected variant or chain |
| `[` `]` | Previous / next output tab |
| `W` | Close the active tab. A running job asks: kill, or detach and keep it running |
| `Shift+W` | Close every finished tab |
| `Ctrl+C` | Send SIGINT to the active tab's job |
| `Y` | Copy `tail -f <log>` for the active tab to the clipboard |
| `Ctrl+Shift+C` | Copy the active tab's log to the clipboard, the last 500 KB if it is longer. Many terminals keep this key for their own copy; the *Copy active log* command in `Ctrl+P` does the same |
| `M` | Recipe manager: sources, drop-ins, overrides, missing tools |
| `Shift+R` | Reload recipes. Running jobs are untouched |
| `L` | Toggle split / stacked layout (stacked is automatic under 120 columns) |
| `H` | Show / hide the hotkey bar |
| `Q Q` | Quit (double-tap). Asks first if jobs are running |

Both copy keys use OSC 52: fieldlog writes the text as an escape sequence and
your terminal puts it on the clipboard. That works over ssh, and nothing is
copied on the remote machine. It needs a terminal that supports OSC 52 (macOS
Terminal does not) and, inside tmux, `set -g set-clipboard on`.

The RECIPES pane lists **Pinned** (`P`) and **Recent** above **All Recipes**.
Recent holds the last six recipes or chains launched from the TUI; runs started
with `fieldlog run` are not added. `Ctrl+P` with nothing typed shows the same
pinned and recent entries. Both lists are kept per workspace in
`.pinned-recent.json`.

### Jobs

Every run opens its own tab and runs at once; start ping and curl and both hit
the wire together. `Ctrl+C` interrupts only the tab you are on, and the tab
then shows whatever exit code the tool returned, marked `interrupted`.

If a job stops at a prompt (`[y/N]`, a passphrase), a reply field appears under
its output: type the answer and press `Enter`, or click one of the chips
fieldlog offers for a `[y/N]`-style prompt. `Esc` leaves the field with the
job still waiting. Prompts that read the terminal directly, such as sudo and
ssh, work the same way because each job owns its own pty. Hotkeys are
suspended while the reply field has focus.

The log shows the reply, as `› yes`, only when it is one of the choices the
prompt lists in brackets, such as `[y/N]` or `(yes/no/[fingerprint])`. Any
other reply, such as a passphrase, is logged as `› (reply hidden)`. `fieldlog run`
forwards keystrokes to the job and logs none of them.

The target and DNS name cannot be changed while a job is running; stop the
jobs first. The interface and log destination can.

The ARGS band shows the command that will run. `E` opens the raw editor on the
*template*, so `$TARGET` stays `$TARGET` and an edit made under one target
still runs correctly against the next. Edits are per recipe and live for the
session; `R` restores the variant default.

## CLI

```bash
fieldlog list                            # one line per tool, then the chains
fieldlog list ping                       # a tool id: that tool's recipes
fieldlog list sweep                      # anything else: search tool, preset name and flags
fieldlog list -V                         # every recipe with its flags (also with a tool or search)
fieldlog list --runnable                 # only tools found in $PATH
fieldlog list -q                         # tool/preset IDs and chain ids, one per line, nothing else (for fzf / xargs)
fieldlog list --json

fieldlog show ping                       # a tool's first preset
fieldlog show ping/quick -t 192.168.1.20 -H router1     # with the variables filled in
fieldlog show reach -t 192.168.1.20      # a chain: every step's resolved command

fieldlog run ping/quick 192.168.1.20                # target as an argument...
fieldlog run ping/quick -t 192.168.1.20             # ...or as a flag
fieldlog ping/quick 192.168.1.20                    # "run" is optional
fieldlog run ping 192.168.1.20                      # a tool id alone: its first preset, ping/quick
fieldlog run ping/quick 192.168.1.20 --dry-run      # print command, env and paths; run nothing
fieldlog run ping/quick 192.168.1.20 --extra-args "-c 1"
fieldlog run reach 192.168.1.20                     # a chain: its recipes in order

fieldlog history                         # target folders in the workspace, with run counts
fieldlog history 192.168.1.20            # runs for one folder
fieldlog history router1 --json

fieldlog report 192.168.1.20                        # every run as Markdown on stdout
fieldlog report router1 --tail 10                   # 10 lines of each log instead of 40
fieldlog report router1 --full -o run-report.md     # whole logs, written to a file
fieldlog report router1 --since 12                  # only runs #12 and up

fieldlog doctor                                     # what can run now, and what's missing
fieldlog doctor 192.168.1.20 -H router1             # runnability against a scope
fieldlog doctor --json                              # the same report as a JSON record
```

Wherever `run`, `show` or a chain step expects a recipe ID, a tool id alone
means that tool's first preset, and for `run` and `show` a chain id means the
chain. The bare form `fieldlog <recipe> <target>` takes all three, as long as
the id is not itself a command name (`list`, `show`, `run`, `history`,
`report`, `doctor`, `tui`, or their aliases `ls`, `recipes`, `info`, `exec`,
`log`, `runs`, `check`).

`history` and `report` take a folder name, not a target, whether as an argument
or with `-t`. The folder is the DNS name if the runs had one, else the target:
after `fieldlog run ping/quick 192.168.1.20 -H router1`, use
`fieldlog history router1`, because `history 192.168.1.20` finds nothing.
`fieldlog history` with no name lists the folders.

`doctor` (alias `check`) reads the whole catalog against a scope and reports what
is runnable, what is missing from `$PATH`, and which scope values would unlock
the rest — a preflight before a job. Each verdict is the same
one `run` would reach. It takes the scope flags `-t`, `-H`, `-i`, `-l` (or a bare
target), plus `-v` for a per-preset breakdown and `--json`. It touches nothing on
disk and exits 0.

### `run` options

| Option | Meaning |
|--------|---------|
| `-t`, `--target` | Target IP, CIDR, hostname or ssh `user@host`. Or pass it as the second argument |
| `-H`, `--host` | DNS name (`$HOST`, `$TARGET_HOST`) |
| `-i`, `--interface` | Interface name (`$IFACE`). Default `eth0`, even if it has no address: `run` never switches like the TUI does |
| `-l`, `--lhost` | Local IP (`$LHOST`). Default: the interface's IPv4 address. Presets using `$LHOST` are not runnable without one |
| `-w`, `--workspace` | Archive root (default `./targets`) |
| `--artifact-root DIR` | Write logs and `$OUTDIR` under `DIR/<name>/` instead of the workspace |
| `--timeout SECONDS` | Stop the job after this long, recorded as exit 124. Per step for a chain |
| `-n`, `--dry-run` | Show what would run. Reserves no run number, creates nothing |
| `--extra-args "..."` | Append to the command. Not accepted for a chain |
| `--note "..."` | Free text stored on the record, shown by `history` and `report`. On a chain the note goes on the chain's summary record |
| `-q`, `--quiet` | Tool output only, no banner or summary |
| `--json` | Print the run record (or the chain summary record) as JSON when done. On an execution error before the archive was written, a short `{"error": true}` record instead |

`show` accepts `-t`, `-H`, `-i` and `-l`. `history` accepts `-t`, `-w` and
`--json`. `list` accepts `-r`/`--runnable`, `-V`/`--verbose`, `-q`/`--names`
and `--json`. `doctor` accepts `-t`, `-H`, `-i`, `-l`, `-v`/`--verbose` and
`--json`.

### `report` options

| Option | Meaning |
|--------|---------|
| `-t`, `--target` | Target folder name, or pass it as the argument. Same rule as `history` |
| `-w`, `--workspace` | Archive root (default `./targets`) |
| `-o`, `--output FILE` | Write the Markdown to `FILE`, creating parent folders. `-` is stdout |
| `--tail N` | Lines of each run's log to include (default 40) |
| `--full` | Include each log in full |
| `--since ID` | Only runs numbered `ID` or higher |

A report is one Markdown document: a summary table of every run, then a
section per run with its command, timings, artifacts and log output. A chain's
summary record renders as a step table. With `-o`, stdout stays empty and the
confirmation goes to stderr, so the command is safe to pipe. Binary artifacts
are listed, never quoted.

## Recipes

The built-in catalog is [`fieldlog/recipes.yaml`](fieldlog/recipes.yaml). Add
your own as `*.yaml` files in either:

- `~/.config/fieldlog/recipes.d/` (`$XDG_CONFIG_HOME/fieldlog/recipes.d/` if set)
- `./recipes.d/` in the directory you run fieldlog from

### Format

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
| `scan` | preset | no | `true` records every file that changed under the target folder, not just `$OUTDIR` (see below) |

### How the command is built

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
    flags: "env bash /home/chris/bin/capture.sh $TARGET"
    outdir: true
  ```

### Summaries

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
  still summarises at the same cost.
- A regex that does not compile, or a template naming a group that does not
  exist, is reported at load; the preset still runs, without a summary.

### Exit codes

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

### Variables

fieldlog fills these in before running and shows the result in previews.
`${NAME}` works too. Every variable below is also exported into the job's
environment, under both spellings, so whatever fieldlog leaves alone still
expands in the shell: any other `$VAR`, shell forms like `${TARGET:-x}`, and a
script that reads `$OUTDIR` itself.

| Variable | Value |
|----------|-------|
| `$TARGET`, `$TARGET_IP` | Target IP, CIDR, hostname or ssh `user@host` |
| `$HOST`, `$TARGET_HOST` | DNS name. If unset and the target is a hostname (not `user@host`), the target. Presets using it are not runnable without one |
| `$LHOST` | Local IP: the one given, else the interface's IPv4 address, read when the job starts (see [TUI](#tui) to set one there). Presets using it are not runnable without one |
| `$IFACE` | Interface name |
| `$OUTDIR`, `$OUT_DIR` | Per-run folder for files the tool writes: `targets/<name>/raw/<timestamp>_<run>/` |
| `$RUN_ID` | Run number, `01`, `02`, … Set in the environment only |

A preset is **not runnable** while its variables are unmet or its binary is
missing. The TUI says why; the CLI refuses with the same reason. Targets and
DNS names may only contain letters, digits, `.`, `:`, `/`, `-` and `_`, since
they are pasted into a shell command. Targets may also contain `@`, for an ssh
`user@host`. None of them may start with `-`, which would be read as one more
flag (`--target=-f` is a flood ping, not a target), and a target made only of
digits and dots has to be a valid address: `10.0.0.256` is refused rather than
run. `--extra-args` and TUI args edits are appended as typed and are not
checked; they are the operator's own shell. The same trust applies to
`./recipes.d/`: a recipe's `flags` is shell, so running fieldlog inside a
directory you do not trust and choosing one of its recipes runs that recipe.

### Chains

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

- A chain stops at the first non-zero exit unless that step says
  `continue: true`. `Ctrl+C` always stops it. The chain's own exit status is the
  first non-zero one it saw, including one from a step it continued past, or 130
  if it was interrupted. So a chain whose `continue: true` step failed still
  exits non-zero, even when every later step succeeds.
- A chain is runnable only when every step is. The reason names the step.
- Every step shares one `$OUTDIR` — the first step's — so side files from one
  chain land together. Logs stay separate, one per step.
- Each step is archived as its own run record, tagged with its position. When
  the chain ends, it appends one summary record named `chain/<id>` to the same
  `session.json`: each step's run number and exit code, where it stopped, and
  the shared `$OUTDIR`. The summary takes a run number of its own, so a
  three-step chain that runs to the end adds four runs to `history`'s count.
- Steps are checked once every file is merged, so a built-in chain may name a
  preset that a drop-in adds. A chain with a bad id, no steps, or an unknown
  recipe is skipped with a message; the rest of the catalog still loads.

`fieldlog list` shows chains in their own section, one line each. A `?` marks a
step the chain continues past:

```text
reach      reachability · ping, trace, ptr    ping/quick → traceroute/icmp? → dig/ptr
```

In the TUI, chains sit under the recipes. Selecting one lists its steps
read-only; `Enter` runs the whole chain, one tab per step. Per-recipe args
edits (`E`) still apply inside a chain. The steps themselves are edited in the
YAML.

### Drop-in files

Drop-ins load after the built-in catalog, in filename order: the config
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

Adding a preset to the built-in `ping`:

```yaml
recipes:
  - id: ping
    presets:
      - id: gentle
        name: "single probe"
        flags: "-c 1 -W 1 $TARGET"
```

This repo's own `recipes.d/` holds example recipes (`examples.yaml`) and chains
built from them (`chains.yaml`). It is the local drop-in directory, so they load
when you run fieldlog from a checkout.

### GUI recipes

A recipe can open a window and run until you close it. `rcap/ssh` in
[`recipes.d/examples.yaml`](recipes.d/examples.yaml) streams a remote `tcpdump`
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

The tests behind these points are in
[`docs/investigations/2026-09-13-ssh-wireshark.md`](docs/investigations/2026-09-13-ssh-wireshark.md).

## The archive

```text
targets/
├── .last-scope.json                # the TUI's remembered scope for this workspace
├── .pinned-recent.json             # pinned and recent recipes
└── <name>/                         # DNS name if set, else the target ("/" becomes "_")
    ├── session.json                # JSON array, one record per run, appended under a lock
    ├── .run-counter                # highest run number handed out
    └── raw/
        ├── 20260912T180156_ping_quick_01.log    # what the tool printed, ANSI stripped
        └── 20260912T180156_01/                 # that run's $OUTDIR, if it used one
```

A run record:

```json
{
  "id": "01",
  "recipe": "ping/quick",
  "command": "ping -c 4 -W 1 192.168.1.20",
  "environment": {"TARGET": "192.168.1.20", "TARGET_IP": "192.168.1.20", "TARGET_HOST": "",
                  "LHOST": "192.168.1.5", "IFACE": "eth0", "OUT_DIR": "…/raw/20260912T180156_01", "RUN_ID": "01"},
  "artifact_log": "…/targets/192.168.1.20/raw/20260912T180156_ping_quick_01.log",
  "out_dir": "…/targets/192.168.1.20/raw/20260912T180156_01",
  "start_time": "2026-09-12T18:01:56.734145",
  "end_time": "2026-09-12T18:01:59.801200",
  "duration_sec": 3.07,
  "exit_code": 0,
  "artifacts": [{"path": "raw/20260912T180156_ping_quick_01.log", "lines": 9, "bytes": 425}]
}
```

Optional keys: `"interrupted": true` when the operator sent SIGINT, `"summary"`
when the preset has a `parse` rule that matched, `"fields"` with that match's
named groups, `"note"` when `--note` was given, `"success"` listing the exit
codes its preset calls a success, `"chain": {"id", "step", "of"}`
on a chain step, `"binary": true` on an artifact that is not text. The
`environment` block leaves out `HOST` and `OUTDIR`: both are exported to the
job, but they always equal `TARGET_HOST` and `OUT_DIR`.

- The folder name is built in two steps. First `/` becomes `_`, so the subnet
  `192.168.1.0/24` gets the folder `192.168.1.0_24`. Then any character still
  outside `[A-Za-z0-9._-]` becomes `-`: `fe80::1` gets `fe80--1` and
  `chris@jump1` gets `chris-jump1`.
- `fieldlog history <name>` and `report <name>` take either the folder name or
  the target the runs were made against. A run made with `-t 10.10.11.50 -H
  box.htb` lands in `targets/box.htb/`, and both spellings find it. An
  unrecognised name lists the folders that do have runs.
- The exit code is the tool's own, never fabricated. `ping` catches SIGINT,
  prints its statistics and exits 0; the record says `exit_code: 0` and
  `interrupted: true`. A tool that does not handle SIGINT is killed by it and
  shows 130, the shell's 128 + signal.
- `artifacts` lists the files the run owns: its primary log, then what it wrote
  into `$OUTDIR`. The list stays right however many runs are in flight — which a
  scan of the target folder could not, since "changed while this ran" and "this
  run wrote it" are only the same thing when runs are serialised.
- A chain's steps share one `$OUTDIR`, which is how a step uses what the step
  before it produced. Each step still records only the files it wrote or changed
  itself, so the shared folder does not make every step claim all of them.
- A recipe that writes somewhere else under the target folder — a tool with a
  fixed output name, or one writing into the working directory (`cwd` is the
  target folder) — sets `scan: true` on its preset and gets the folder-wide
  before-and-after comparison instead:

  ```yaml
  - id: report
    scan: true                        # writes ./report.html, not into $OUTDIR
    flags: "--output report.html $TARGET"
  ```

  Only accurate when nothing else runs against that target at the same time: a
  scan cannot tell which of two overlapping runs produced a file, and will list
  it for both. Prefer `$OUTDIR` where the tool lets you choose.
- `--artifact-root DIR` (or the *log destination* in `T`) moves logs and
  `$OUTDIR` to `DIR/<name>/`. `session.json` stays in the workspace and records
  those files with absolute paths, since they sit outside the target folder.
- Run numbers are per target folder, claimed under `.session.lock`, and the
  highest number handed out is kept in `.run-counter`, so a CLI run beside the
  TUI never reuses one. A dry run claims nothing.
- Log filenames and record timestamps use local time.

## Scope

Built for tools that run to completion and write to stdout or files: ping,
curl, dig, traceroute, iperf3 and similar. Answering a one-line prompt works;
long interactive sessions are out of scope.

A preset's `parse` rule turns its own output into one line on the run record, so
a ping run reads as `4 replies · 0% loss` without opening the log. Everything
else stays a log you read yourself.

## License

MIT
