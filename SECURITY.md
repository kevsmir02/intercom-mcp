# Security model

intercom starts other coding agents on your machine and hands them your working tree. That is the
whole point of the tool, and it is also its entire threat surface. Read this before installing it on
a machine that holds anything you care about.

## What a delegation can do

By default `delegate_to_<harness>` runs the harness with:

- **auto-approval on** (`auto_approve` defaults to `true`), which injects the harness's
  skip-permissions flag (`--dangerously-skip-permissions` for `agy` and `claude`, `--auto` for
  `opencode`, `--approve` for `pi`). The harness will not stop to ask before editing a file, running
  a command, or reaching the network.
- **your environment**, minus a small deny list. The child inherits `PATH`, `HOME`, and every API
  key, token and credential in the server's environment, because that is how the harness
  authenticates. `BRIDGE_STRIP_ENV` removes named variables; nothing is stripped by default except
  the parent session's own identity variables.
- **any directory you name.** `working_dir` accepts any existing readable directory unless you set
  `BRIDGE_ALLOWED_DIRS`.

There is no sandbox. A delegation is as privileged as the user running the MCP server.

## Hardening

| Control | What it does |
| --- | --- |
| `BRIDGE_ALLOWED_DIRS=/path/one:/path/two` | Refuses any `working_dir` outside those roots. Set it with `intercom setup --allowed-dir /path/one`. |
| `auto_approve: false` on a call | The harness asks for permission instead of proceeding; in headless mode it records denials rather than editing. |
| `consult_<harness>` / `consult_many` | The harness's read-only mode (plan mode, or edit tools excluded) with auto-approval off. Use it for reviews and second opinions instead of a delegation. **It disables the harness's edit tools; it is not a sandbox** -- the harness can still run commands and reach the network, and observed behaviour includes writing plan files outside the working tree. The report carries a change summary either way, and warns if anything in the tree changed. |
| `isolate: true` on a call | Runs in a throwaway `git worktree`, so the delegation cannot touch your working tree; you apply the resulting patch yourself. |
| `BRIDGE_STRIP_ENV=NAME,NAME` | Hides named variables from the harness. |
| `BRIDGE_MAX_DEPTH` | Stops a delegated harness from delegating further (default: one level). |

## What intercom does on its own behalf

- **Secret redaction.** Before a report is returned or journalled, values of environment variables
  whose names match `BRIDGE_REDACT_ENV` (default: `API_KEY`, `_KEY`, `TOKEN`, `SECRET`, `PASSWORD`,
  `CREDENTIAL`) are replaced with `[redacted:$NAME]`. This is a backstop against a harness echoing
  its environment, not a guarantee: a secret the harness paraphrases or re-encodes will get through.
- **Process-tree termination.** On timeout or cancellation the harness and every descendant are
  killed (SIGTERM, then SIGKILL). Descendants are found through `/proc`, with a `ps` fallback for
  systems without it, so a grandchild that left the process group is still caught. This is tested on
  Linux only, which is the supported platform; the Windows path (`taskkill /F /T`) skips the
  graceful stage and is not exercised.
- **A run journal** under `$XDG_STATE_HOME/intercom` (default `~/.local/state/intercom`) holding
  each run's full report, and the patch of isolated runs. Reports contain your code. Treat that
  directory as sensitive; `BRIDGE_KEEP_RUNS=0` keeps everything, and pruning is by count, not age.

## Reporting a problem

Open an issue at https://github.com/kevsmir02/intercom-mcp/issues. This is a personal project with
no security SLA; do not use it where that matters.
