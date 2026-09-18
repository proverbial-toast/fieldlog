# ssh + Wireshark GUI through fieldlog

2026-09-13 · investigation, no fieldlog code changed

**Question.** A work script sshes somewhere and then opens the Wireshark GUI. Is it
fine to run that bash script through fieldlog? And how would it work as a recipe
directly?

## Short answer

- **The script route works**, with three conditions:
  1. Call the script by **absolute path**. Jobs run with `targets/<target>/` as their
     working directory, so `./script.sh` is not found (E10).
  2. **Pass `$OUTDIR` in the recipe flags** if the script writes files there. fieldlog
     only creates `$OUTDIR` when the flags mention it, so a script that writes
     to `$OUTDIR` from the environment alone fails (E10).
  3. **Leave Wireshark in the foreground.** The job stays open until you close the
     window. `wireshark &` or `nohup wireshark &` inside the script gets killed as
     soon as the script exits (E9, F1, G1).
- **Directly, it is a one-line recipe**: `ssh host tcpdump -w - | tee $OUTDIR/remote.pcap | wireshark -k -i -`.
  fieldlog then keeps the capture as an artifact of the run (E2). See the
  [recipe](#route-b-a-recipe-directly) below.
- **Answer ssh prompts (host key, passphrase, password) on the CLI terminal, not
  the TUI reply bar.** The TUI writes whatever you type into the run's log file (E14).
- **Never give the ssh a remote terminal (`-t`).** It rewrites bytes and corrupts the
  capture (E3).

**Not tested:** a real Wireshark window. There is no Wireshark here, and this WSL
session has no X or Wayland socket (`/tmp/.X11-unix` is empty; `/mnt/wslg` holds
only `run/`). Everything else ran against a real OpenSSH server on localhost, with
stand-ins for `tcpdump` and Wireshark.

## How fieldlog runs a command (the parts that matter here)

From `fieldlog/runner.py` and `fieldlog/launch.py`:

| Behaviour | Consequence for ssh + GUI |
|---|---|
| The child gets a copy of fieldlog's whole environment (`build_env` starts from `os.environ`) | `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, `SSH_AUTH_SOCK` reach the job as they were when fieldlog started (E1) |
| Working directory is `targets/<target>/` | Relative paths in flags or scripts resolve there (E10) |
| Command runs under `/bin/sh` on a pty; the job is its own session with the pty as controlling terminal | ssh can prompt on `/dev/tty` (E12, E13). Anything written to stdout/stderr that is not piped lands in the log as text (E4) |
| The job ends when every holder of the pty has closed it, then the exit code is read | A GUI child still holding the pty keeps the job open (F1 `setsid` case) |
| Exit code of a pipeline is the last command's | For `ssh … \| wireshark`, the run records Wireshark's exit code |
| Ctrl-C on the CLI, or kill in the TUI, sends SIGINT to the whole process group; TUI kill adds SIGKILL after 10 s | Local ssh *and* Wireshark both get the signal (E6) |
| `--timeout N` wraps the command in `timeout` | The GUI is killed after N seconds (E8). Don't use it for GUI recipes |
| CLI: keystrokes are forwarded raw to the job's pty. TUI: the reply bar writes the line **and** logs it as `› reply` | Secrets typed in the TUI end up in the run log (E14); on the CLI they don't (E13) |
| Targets may only contain `A-Z a-z 0-9 . : / - _` | `user@host` is refused (E11). Use a `Host` alias in `~/.ssh/config`, or `ssh -l user` |

## What was tested

A throwaway `sshd` ran as my user on `127.0.0.1:2222`, with its own host key and
key-only auth; `~/.ssh` was not touched. Stand-ins:

- `gen_pcap.py` in place of remote `tcpdump -U -w -`. It streams a valid pcap whose
  packets contain every byte value (`\r`, `\n`, `^C`, `^D`, `^Q`, `^S` …), so any
  rewriting shows up as a hash mismatch.
- `fake_wireshark.py` in place of `wireshark -k -i -`. It reads pcap on stdin and
  reports magic, packet count and sha256. It can stop after N packets, like
  closing the window.
- `sigspy.py`: a backgrounded "GUI" that logs which signal ends it.

Every case ran through the real `fieldlog run` CLI. Prompts were answered through a
pty harness, and the TUI reply path through `runner.send_stdin`. E1–E14 ran twice,
with identical results.

| # | Case | Result |
|---|---|---|
| E1 | Environment and cwd | `DISPLAY`, `XAUTHORITY`, `SSH_AUTH_SOCK`, custom vars all present; cwd `targets/127.0.0.1` |
| E2 | `ssh … \| tee $OUTDIR/remote.pcap \| viewer` | 200/200 packets, hash matches. `remote.pcap` (54,424 B) recorded as an artifact; the log holds only the viewer's line |
| E3 | Same with `ssh -tt` | Corrupt after the first packet |
| E4 | `ssh … tcpdump -w -` with no pipe | 20 packets became a 43-line log with 2,584 replacement characters |
| E5 | Viewer closes after 50 packets, remote still sending | Job ends in 1.3 s, exit 0; remote process gone |
| E6 | Ctrl-C, remote sending 1 packet/s | Exit 130, recorded as interrupted; viewer got SIGINT too; remote gone within 3 s |
| E7 | Ctrl-C, remote sending nothing | **Remote process still running** 3 s later (it only dies when it next writes) |
| E8 | `--timeout 3` | Exit 124 after 3 s; viewer killed |
| E9 | GUI backgrounded in the recipe: `&`, `nohup … &`, `setsid -f` | `&` and `nohup`: job ends in 0.1 s, **child never finishes**. `setsid -f`: child finishes, but the job waits for it |
| F1/G1 | Why `nohup` failed | The child died before it could log. With `sleep 1` before the shell exits, the nohup'd child survives: a race with SIGHUP |
| G2 | `setsid -f cmd </dev/null >/dev/null 2>&1` | Job ends at once, child survives |
| E10 | Script route | Absolute path works (hash matches), but `tee $OUTDIR/…` failed because the dir didn't exist, exit 1. Relative path: exit 127 |
| E11 | `user@127.0.0.1` as target | Refused: unsafe character `@` |
| E12 | First-connection host-key prompt, answered on the CLI | Connected; prompt text in the log (harmless) |
| E13 | Key passphrase, answered on the CLI | Connected; passphrase **not** in the log |
| E14 | Key passphrase, answered the TUI way | Connected; the log contains `› lab-passphrase` |
| F2 | Remote cleanup `cmd & cat >/dev/null; kill $!` | The run never finished reporting after the viewer-close case (probably hit its 30 s limit). Dropped for the next form |
| G3–G5 | Remote cleanup `(cmd; kill 0) & cat >/dev/null; kill 0` | Viewer closes: remote gone. Ctrl-C with a silent remote: remote gone. Finite stream: intact, hash matches |

Why E9 happens: when the job's shell exits, the kernel hangs up the job's terminal
and sends SIGHUP to its foreground process group, which includes `&` children.
`nohup` ignores SIGHUP, but only once it is running, and a shell that exits
immediately wins that race (G1). `setsid -f` moves the child into a new session, but
if the child's stdio is still the pty, fieldlog waits for it (F1).

Why G3–G5 work: sshd runs the remote command in its own session, so `kill 0` hits
only that command's processes. `cat` returns when the channel closes (local ssh
died, E7's case), and the subshell's `kill 0` runs when the capture exits (viewer
closed, broken pipe). Either way the whole remote side goes.

## Route A: your existing bash script

Works as is if Wireshark runs in the foreground. A shape that avoids E10:

```bash
#!/usr/bin/env bash
# remote-wireshark.sh HOST OUTDIR
set -euo pipefail
host="$1"
out="$2"
mkdir -p "$out"
ssh "$host" "(tcpdump -i eth0 -U -s0 -w - not port 22; kill 0) & cat >/dev/null; kill 0" \
  | tee "$out/remote.pcap" \
  | wireshark -k -i -
```

```yaml
      - id: script
        name: "my capture script"
        flags: "env bash /home/operator/bin/remote-wireshark.sh $TARGET $OUTDIR"
```

Checked with `fieldlog show`: the `$OUTDIR` in the flags makes fieldlog create the
folder, and the leading `env` keeps the tool's `bin` (wireshark) as the `$PATH`
check. `set -o pipefail` makes the script's exit code reflect ssh failures.
Without it, only Wireshark's code counts.

If the script must return while Wireshark stays open, detach it completely:
`setsid -f wireshark -k -i - </dev/null >/dev/null 2>&1` (G2). But then fieldlog
records only the script, and a pipe into a detached Wireshark needs more care.
Foreground is simpler, and it's what makes the run record meaningful.

## Route B: a recipe directly

```yaml
recipes:
  - id: rcap
    name: "Remote Capture → Wireshark"
    bin: wireshark
    presets:
      - id: ssh
        name: "tcpdump over ssh · live gui + pcap"
        flags: >-
          env ssh $TARGET "(tcpdump -i ${RIFACE:-eth0} -U -s0 -w - not port 22; kill 0) & cat >/dev/null; kill 0"
          | tee $OUTDIR/remote.pcap
          | wireshark -k -i -
```

Resolved by `fieldlog show rcap/ssh -t jump1`:

```
env ssh jump1 "(tcpdump -i ${RIFACE:-eth0} -U -s0 -w - not port 22; kill 0) & cat >/dev/null; kill 0" | tee …/targets/jump1/raw/<stamp>/remote.pcap | wireshark -k -i -
```

Each piece, and why it's there:

- **`env ssh …`** Flags that start with neither the tool's `bin` nor a wrapper get
  the `bin` prepended, which would make this `wireshark ssh …`. `env` is a
  recognised wrapper, so the line runs as written and the `$PATH` check is still
  on `wireshark`.
- **`$TARGET`** is the ssh destination. For a user, use a `~/.ssh/config` alias (E11).
- **`${RIFACE:-eth0}`** is left to the shell (fieldlog only fills its own
  variables), so `RIFACE=ens192 fieldlog run rcap/ssh jump1` picks the remote
  interface.
- **`-U -w -`** writes unbuffered pcap to stdout. **`not port 22`** keeps the capture
  from recording its own ssh session.
- **`(…; kill 0) & cat >/dev/null; kill 0`** makes sure the remote capture stops
  when you Ctrl-C or close Wireshark, even on a quiet interface (E7 vs G3–G5).
- **`tee $OUTDIR/remote.pcap`** keeps the capture as a run artifact, so `report`
  lists it (E2).
- **`wireshark -k -i -`**: the man page says `-i -` reads "data from the standard
  input" in pcap or pcapng, and `-k` starts the capture immediately.
- **No `-t`, no `--timeout`** (E3, E8).

Run it from the CLI when ssh might prompt, and answer in the terminal (E12, E13).
With key auth through an agent there is nothing to answer, and the TUI is fine.

## Other routes (not tested)

- **Wireshark's own ssh capture (`sshdump` extcap).** From its man page:
  `wireshark '-oextcap.sshdump.remotehost:"remotehost"' -i sshdump -k`, with
  `-oextcap.sshdump.remotecapturecommand:"tcpdump -i eth0 -Uw- not port 22"` for a
  custom command. As a recipe: `flags: "wireshark '-oextcap.sshdump.remotehost:\"$TARGET\"' -i sshdump -k"`.
  Simpler, but fieldlog sees no packets (no pcap artifact unless you save from
  Wireshark), and ssh auth happens inside Wireshark.
- **`ssh -X host wireshark`** (Wireshark on the remote, drawn locally). E1 shows
  `DISPLAY`/`XAUTHORITY` reach the job, which is what X forwarding needs. Nothing
  is captured locally, and it is usually slow.

## Things to check on the work machine

1. In the terminal you start fieldlog from: `echo $DISPLAY $WAYLAND_DISPLAY`.
   If both are empty, no GUI can open, from fieldlog or otherwise.
2. What real Wireshark does on SIGINT (Ctrl-C in fieldlog sends it to Wireshark
   too, E6). The stand-in exited.
3. Whether the remote capture command prompts for anything. It can't be answered
   through this pipeline (no remote terminal), and `-t` breaks the stream (E3).

## Possible fieldlog changes (not made)

1. **TUI reply bar logs secrets.** `send_stdin` writes `› <reply>` into the run log,
   and the System tab shows `stdin → "<reply>"`. Suggest logging `› (reply sent)`
   instead, at least when the prompt looks like a password or passphrase.
   Highest value of the four.
2. **`$OUTDIR` only exists when the flags mention it.** Scripts that read it from
   the environment write into a missing folder. Either always create it, or
   document "pass `$OUTDIR` as an argument".
3. **`user@host` targets are refused.** `@` isn't a shell metacharacter, so allowing
   it is low-risk. Otherwise, document the ssh-config alias.
4. **README note for GUI recipes:** keep the GUI in the foreground, no `--timeout`,
   answer prompts on the CLI.

## Reproducing

The lab lived in the session scratchpad and was not kept: sshd config and keys,
`gen_pcap.py`, `fake_wireshark.py`, `sigspy.py`, recipe files `lab*.yaml`,
`experiments*.py`. Ask if you want it checked in under `docs/investigations/`.
