# fieldlog

![fieldlog](fieldlog.jpg)

A terminal UI and CLI for running tools from a catalog of saved commands
and keeping a log of every run. You choose the tool and the target; fieldlog runs it,
streams the output, and files the log and a run record under `./targets/<name>/`.

## Requirements

- Linux
- Python 3.11+
- The diagnostic tools themselves (ping, curl, dig, …). fieldlog doesn't bundle them;
  tools missing from `$PATH` are flagged with an install hint.
- Permission to access the network interface for some local diagnostics.

## Install

```bash
pipx install git+https://github.com/proverbial-toast/fieldlog
fieldlog            # opens the TUI
```

For development:

```bash
git clone https://github.com/proverbial-toast/fieldlog && cd fieldlog
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Terms

| Term | Meaning | Example |
|------|---------|---------|
| **Tool** | An entry under `recipes:` in the YAML: one binary plus its presets. Listed in the TUI's recipes pane. | `ping` |
| **Preset** | One saved set of flags for a tool. Called a **variant** in the TUI. | `top100` |
| **Recipe ID** | `tool/preset`, how the CLI names something runnable. | `ping/quick` |
| **Chain** | An entry under `chains:`: recipe IDs run in order against one scope. | `reach` |

## TUI

Run `fieldlog` (or `fieldlog tui`). Press `?` for every key.

To start with the scope already set, pass it to `tui`. It takes the same flags as `run`:

```bash
fieldlog tui 192.168.1.20 -i eth0              # or -t 192.168.1.20
fieldlog tui -t 192.168.1.0/24 -H router1 -i eth0 -w ~/out
```

An interface you name is kept even with no IP yet; `T` still changes everything
at runtime.

The scope is remembered per workspace in `<workspace>/.last-scope.json`, so a bare
`fieldlog` resumes where you left off. Any flag you pass overrides the remembered
value.

| Key         | Action                                                  |
|-------------|---------------------------------------------------------|
| `T`         | Set target, dns name, interface and log destination     |
| `↑ ↓` `j k` | Move                                                    |
| `Enter`     | On a tool: jump to its variants · on a variant: run it  |
| `Ctrl+P`    | Palette: fuzzy-find and run any recipe                  |
| `[` `]`     | Previous / next output tab                              |
| `Ctrl+C`    | Interrupt the active tab's job (SIGINT)                 |
| `Shift+R`   | Reload recipes                                          |
| `M`         | Recipe manager (sources, drop-ins, overrides)           |
| `?`         | All keys                                                |
| `Q Q`       | Quit (double-tap) · `Ctrl+Q` quits in one press         |

Each run opens a tab and runs in parallel — start ping and curl and both
hit the wire at once; `Ctrl+C` interrupts only the tab you're on. (`--timeout` is
a `run` CLI flag, per-invocation, not a TUI control.) If a job stops at a prompt (y/n, passphrase), type the
answer in the reply field under its output and press Enter; `Esc` leaves the field.

## CLI

```bash
fieldlog list                        # every recipe
fieldlog list ping                   # search tool, preset and flags
fieldlog list -c ports               # category filter (case-insensitive substring)
fieldlog list --runnable             # only tools found in $PATH
fieldlog list --tools                # tools without presets
fieldlog list -q                     # bare recipe IDs, one per line (for fzf/xargs)
fieldlog list --json

fieldlog show ping                   # a tool's first preset
fieldlog show ping/quick -t 192.168.1.20 -H router1     # preset with variables filled in

fieldlog run ping/quick 192.168.1.20              # target as an argument...
fieldlog run ping/quick -t 192.168.1.20           # ...or as a flag
fieldlog ping/quick 192.168.1.20                # "run" is optional (except for IDs
                                               #   named list/show/run/history/report/
                                               #   tui and their aliases — use "run"
                                               #   explicitly)
fieldlog run ping/quick 192.168.1.20 --dry-run  # print command, env and paths; run nothing
fieldlog run ping/quick 192.168.1.20 --extra-args "-c 1"
fieldlog run reach 10.0.0.1                     # a chain: its recipes in order

fieldlog history                     # list target folders
fieldlog history 192.168.1.20         # runs for one target folder
fieldlog history router1 --json

fieldlog report 192.168.1.20                      # every run as Markdown on stdout
fieldlog report router1 --tail 10                 # 10 lines of each log instead of 40
fieldlog report router1 --full -o run-report.md   # whole logs, written to a file
fieldlog report router1 --since 12                # only runs #12 and up
```

### `run` options

| Option | Meaning |
|--------|---------|
| `-t`, `--target` | Target IP, CIDR or hostname (or pass it as the second argument) |
| `-H`, `--host` | DNS name (`$HOST`, `$TARGET_HOST`) |
| `-i`, `--interface` | Interface name (`$IFACE`, default `eth0`) |
| `-l`, `--lhost` | Local IP (`$LHOST`); defaults to the interface's current IP |
| `-w`, `--workspace` | Archive root (default `./targets`) |
| `--artifact-root DIR` | Write logs to `DIR/<target>/` instead of the workspace |
| `--timeout SECONDS` | Stop the job after this many seconds (recorded as exit 124) |
| `-n`, `--dry-run` | Show what would run without running it |
| `--extra-args "..."` | Append to the command |
| `-q`, `--quiet` | Tool output only, no banner or summary |
| `--json` | Print the run record as JSON when done |

### `report` options

| Option | Meaning |
|--------|---------|
| `-t`, `--target` | Target folder name (or pass it as the argument), as `history` uses it |
| `-w`, `--workspace` | Archive root (default `./targets`) |
| `-o`, `--output FILE` | Write the Markdown to `FILE` (parent folders created); `-` is stdout |
| `--tail N` | Lines of each run's log to include (default 40) |
| `--full` | Include each log in full instead of a tail |
| `--since ID` | Only runs numbered `ID` or higher |

A report is one Markdown document: a summary table of every run, then a section
per run with its command, timings, artifacts and log output. With `-o` nothing
but the file is written — the confirmation goes to stderr — so it is safe to
redirect. Binary artifacts are listed, never quoted.

`show` accepts `-t`, `-H`, `-i`, `-l`. `history` accepts `-w` and `--json`.

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
    bin: ping                          # executable checked in $PATH (default: id)
    category: "Host & Reachability"   # grouping in the TUI and `list -c`
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
| `category` | tool | no | Group name |
| `presets` | tool | yes | List of presets |
| `id` | preset | yes | Second half of the recipe ID |
| `flags` | preset | yes | What to run after `bin` (see below) |
| `name` | preset | no | Display name |
| `bin` | preset | no | Overrides the tool's `bin` for this preset |

### How the command is built

If a preset's flags reference `$OUTDIR`, that per-run folder is created before
the command runs, so a tool writing side-cars (`-oA`, `-w`, `--logfile`) has
somewhere to write. Whatever files it produces are recorded in the run's
`session.json` afterward — no need to declare them.

- The command is `bin` + space + `flags`, run by `/bin/sh` under a pty.
  `flags` is shell syntax: quote values with spaces; pipes, `;` and `$(...)` work.
- If `flags` starts with `timeout ` or with the binary's own name, it is used as
  the whole command. That is how you wrap a tool:

  ```yaml
  flags: "timeout 60s ping -c 4 $TARGET"
  ```

  The `$PATH` check still uses `bin`. The exit code (124 when `timeout` fires)
  is recorded in `session.json`.

### Variables

fieldlog fills these in before running and shows the result in previews.
`${NAME}` works too; any other `$VAR` is left for the shell.

| Variable | Value |
|----------|-------|
| `$TARGET`, `$TARGET_IP` | Target IP, CIDR or hostname |
| `$HOST`, `$TARGET_HOST` | DNS name. If unset and the target is a hostname, the target. Presets using it won't run without one |
| `$LHOST` | Local IP |
| `$IFACE` | Interface name |
| `$OUTDIR`, `$OUT_DIR` | Per-run folder for files the tool writes: `targets/<name>/raw/<timestamp>/` |
| `$RUN_ID` | Run number: `01`, `02`, … (set in the environment only) |

### Chains

A **chain** is a named, ordered list of recipe IDs run one after another against
the current scope. Chains live beside `recipes:` in the same YAML files:

```yaml
chains:
  - id: reach                                 # chain id (required, can't be a tool id)
    name: "reachability · ping, trace, ptr"   # display name (default: the id)
    category: "Chains"                        # grouping (default: "Chains")
    steps:                                    # required, at least one
      - ping/quick                            # a step, stops the chain if it fails
      - recipe: traceroute/icmp               # the same step, written out
        continue: true                        # …but keep going when this one fails
      - dig/ptr
```

| Field | Level | Required | Meaning |
|-------|-------|----------|---------|
| `id` | chain | yes | How the CLI and TUI name it. Must not match a tool id |
| `name` | chain | no | Display name |
| `category` | chain | no | Group name. Default `Chains` |
| `steps` | chain | yes | Recipe IDs in order. A bare tool id means its first preset |
| `recipe` | step | yes | `tool/preset` (the mapping form) |
| `continue` | step | no | `true` keeps the chain going when this step fails |

- A chain stops at the first non-zero exit unless that step says `continue: true`.
  `Ctrl+C` always stops it, whatever the step says. The chain's exit status is the
  first non-zero one it saw (130 if it was interrupted), so `fieldlog run reach …`
  fails the way the step that failed did.
- Every step shares one `$OUTDIR`, so a chain's side-car files land together. The
  logs stay separate, one per step, named after the recipe as usual.
- A chain whose id collides with a tool, that has no steps, or that names a recipe
  nothing defines is skipped with a message — the rest of the catalog still loads.
  Steps are checked once every file is merged, so a built-in chain may name a
  preset a drop-in adds.
- A drop-in adds chains; a chain with an existing id replaces the earlier one and
  is listed as an override, exactly as a preset is.

Each step is archived as its own run record, and the chain adds one more record
of its own — `chain/<id>`, with each step's run number, recipe and exit code,
where it stopped, and the shared `$OUTDIR`. `fieldlog report` renders that as a
step table instead of a log.

`fieldlog list` shows chains in their own category, one line each:

```text
reach      reachability · ping, trace, ptr    ping/quick → traceroute/icmp? → dig/ptr
```

A `?` marks a step the chain continues past. In the TUI a `Chains` section sits
under the recipes; selecting one lists its steps read-only and `Enter` runs the
whole chain, a tab per step. Per-recipe args edits (`E`) still apply inside a
chain — the order and the steps themselves are edited in the YAML.

### Drop-in files

Drop-ins load after the built-in catalog, in filename order: the config
directory first, then `./recipes.d/`.

- The same filename in both directories is loaded once — the local `./recipes.d/`
  file wins, being the more specific of the two, and the shadowing is reported.
- A new tool `id` adds a tool.
- An existing tool `id` adds its presets to that tool. A preset with an existing
  `id` replaces the built-in one; the recipe manager (`M`) lists overrides.
- A file that fails to parse is reported and skipped; the rest still load.

This repo ships a `recipes.d/` of optional, longer-running network diagnostic
recipes kept out of the packaged catalog on purpose.
To use them after a `pipx` install, copy them into your config directory:

```bash
cp path/to/fieldlog/recipes.d/*.yaml ~/.config/fieldlog/recipes.d/
```

Adding a preset to the built-in `ping`:

```yaml
recipes:
  - id: ping
    presets:
      - id: gentle
        name: "single probe"
        flags: "-c 1 -W 1 $TARGET"
```

## Where output goes

```text
targets/
└── <name>/                         # dns name if set, else the target (/ becomes _)
    ├── session.json                # JSON array, one record per run
    ├── notes/
    └── raw/
        ├── 20260912T180156_ping_quick_01.log    # full terminal output
        └── 20260912T180156/                    # that run's $OUTDIR
```

- A subnet like `192.168.1.0/24` gets the folder `192.168.1.0_24`.
- `fieldlog history <name>` uses the folder name: the dns name if you set one.
- Each record holds the recipe ID, command, variables, log path, start and end
  times, exit code, and any new files found in the target folder.
- `--artifact-root DIR` (or *log destination* in `T`) moves logs and `$OUTDIR` to
  `DIR/<target>/`; `session.json` stays in the workspace and records those files
  with absolute paths, since they sit outside the target folder.
- Run numbers are claimed under a lock (`.session.lock`), and the highest number
  handed out is kept in `.run-counter`, so a CLI run beside the TUI never reuses one.

## Scope

Built for tools that run to completion and write to stdout or files: ping, curl,
dig, traceroute and similar. Answering a one-line prompt works; long interactive
sessions are out of scope.

Planned: parsers that turn tool output into structured findings (e.g. ping
output into reachability results).

## License

MIT
