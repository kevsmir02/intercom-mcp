# intercom

A lightweight MCP server that lets an orchestrator such as OpenCode or Claude Code delegate
implementation, refactoring and testing tasks to a headless coding harness and get a structured,
reviewable report back. Two harnesses are wired in:

| Harness | Binary | Tools |
| --- | --- | --- |
| `antigravity` | Google Antigravity CLI `agy` | `delegate_to_antigravity`, `check_antigravity_health` |
| `claude_code` | Anthropic Claude Code `claude` | `delegate_to_claude_code`, `check_claude_code_health` |

Each harness runs in its JSON print mode, so every report carries the harness's conversation ID.
Passing it back as `conversation_id` resumes that session with its full context: a review-fix round
costs one short prompt. A bundled skill teaches the orchestrator the delegate -> review -> fix loop.

## Install

One command installs the project, then a short wizard asks which harnesses to expose and which
orchestrators to register:

```bash
curl -fsSL https://raw.githubusercontent.com/kevsmir02/intercom-mcp/main/install.sh | bash
```

What it does:

1. Checks for `git` and Python 3.10+.
2. Clones the repository into `~/.local/share/intercom` (or updates it when already present).
3. Creates a virtual environment there and installs the `mcp` dependency.
4. Writes the `intercom` launcher to `~/.local/bin`.
5. Runs `intercom setup`, which detects `agy` and `claude`, probes their health, then presents
   arrow-key checkboxes (space to toggle, enter to confirm) for which harnesses to expose and which
   orchestrators to register with. It writes the OpenCode config entry, runs `claude mcp add` for
   Claude Code, links the skill where both orchestrators find it, and saves its choices to
   `~/.config/intercom/config.json`. On a terminal that cannot do raw input it falls back to a
   numbered prompt.

Restart the orchestrator afterwards and ask it to run `check_claude_code_health` or
`check_antigravity_health`; a report starting with `[HEALTH: READY]` confirms the installation.

Prerequisites for a useful install: at least one harness logged in. `agy models` lists models only
when the Antigravity login is valid, and `claude auth status` must report `"loggedIn": true`.

### Installer options

| Form | Effect |
| --- | --- |
| `curl ... \| bash -s -- --no-setup` | Install only; run `intercom setup` later |
| `curl ... \| bash -s -- --yes` | Setup with detected defaults, no questions |
| `INTERCOM_HOME=/opt/intercom curl ... \| bash` | Install elsewhere |
| `INTERCOM_BIN_DIR=/usr/local/bin curl ... \| bash` | Put the launcher elsewhere |
| `INTERCOM_REF=v1.0.0 curl ... \| bash` | Install a branch or tag |

If `~/.local/bin` is not on your `PATH`, the installer says so; add
`export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

## The `intercom` command

| Command | Purpose |
| --- | --- |
| `intercom setup` | The wizard above. Re-run it any time to change harnesses, default flags or orchestrators. |
| `intercom setup --harness claude_code --orchestrator opencode --flags claude_code="--model sonnet" --yes` | Scripted setup for dotfiles and CI |
| `intercom doctor` | Health of the enabled harnesses plus registration and skill checks; exits non-zero on any failure |
| `intercom serve` | Runs the MCP server on stdio. This is what the registrations invoke |
| `intercom test` | Runs the hermetic test suite (fake harnesses, no quota consumed) |
| `intercom update` | `git pull` plus dependency reinstall |
| `intercom config` | Prints the configuration and every path in use |
| `intercom uninstall [--purge]` | Removes registrations, skill links, launcher and config; `--purge` also deletes the checkout |

`serve` turns `config.json` into the environment variables the server reads, filling in only what the
orchestrator's own environment block left unset. Setting `INTERCOM_HARNESSES=claude_code` there, for
example, exposes only Claude Code's tools.

## Manual installation

For a checkout you manage yourself:

```bash
git clone https://github.com/kevsmir02/intercom-mcp.git
cd intercom-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python test_bridge.py
.venv/bin/python intercom.py setup
```

`intercom.py setup` writes the launcher and performs the same registrations as the installer. To
register by hand instead, use `opencode.json` in this repository as the OpenCode template and
`claude mcp add -s user intercom -- <launcher> serve` for Claude Code, then symlink `skills/intercom`
into `~/.claude/skills/intercom`.

## Long-running delegations

A delegation's own limit is `timeout_seconds` (default 900, up to 86400). The orchestrator also
applies its own timeout to every MCP tool call, which can be shorter. To stop a long delegation from
being cut off by the orchestrator, the server emits a progress notification every
`BRIDGE_HEARTBEAT_SECONDS` (default 15). OpenCode resets its tool-call timer on each one, so the call
survives for the whole delegation, and `timeout_seconds` stays the real bound.

- **OpenCode**: the generated config sets `mcp.intercom.timeout` to one hour as a backstop; the
  heartbeat handles anything longer.
- **Claude Code**: it honours the heartbeat as well; if a delegation still gets cut off, raise
  `MCP_TOOL_TIMEOUT` (milliseconds) in its environment.

Set `BRIDGE_HEARTBEAT_SECONDS=0` to disable the heartbeat.

## Tool results

Every `delegate_to_<harness>` result starts with one of:

- `[SUCCESS]` exit 0 and the harness reported success: response, then `git status --short` and
  `git diff --stat HEAD` (plus the full diff when `include_diff` is true)
- `[ROADBLOCK / FAILURE]` non-zero exit or harness-reported error: stderr, extracted stack traces and
  test failures, probable cause, call to action
- `[TIMEOUT_ERROR]` process tree killed (SIGTERM then SIGKILL), partial logs attached
- `[INVALID_ARGUMENT]` bad input, e.g. a `working_dir` that does not exist

Each report also carries `Conversation ID` and `Harness stats` (status, turns, tokens, cost, model).
`check_<harness>_health` returns `[HEALTH: READY]`, `[HEALTH: DEGRADED]` or `[HEALTH: UNAVAILABLE]`.

## Files

| File | Purpose |
| --- | --- |
| `install.sh` | The `curl | bash` installer |
| `intercom.py` | The `intercom` command (setup wizard, doctor, serve, update, uninstall) |
| `server.py` | The MCP server (harness adapters, subprocess runner, report formatting) |
| `skills/intercom/SKILL.md` | Skill teaching the orchestrator the delegate -> review -> fix loop |
| `test_bridge.py`, `test_cli.py` | Hermetic tests with fake `agy` and `claude` binaries |
| `opencode.json` | Template for a manual OpenCode registration |
| `requirements.txt` | Runtime dependency (`mcp`, works with 1.x and 2.x) |

## Harness facts the bridge relies on

- `agy` (1.1.24 and 1.1.25): headless mode is `-p <prompt>` or the prompt on stdin; auto-approve is
  `--dangerously-skip-permissions`; print mode has its own `--print-timeout` (default 5m) which the bridge
  raises above `timeout_seconds`; there is no `auth` subcommand, so `agy models` is the auth probe;
  resume is `--conversation <id>`.
- `claude` (2.1.259): `-p` is a boolean flag with the prompt as a positional argument or on stdin;
  auto-approve is `--dangerously-skip-permissions`; no print timeout flag; `claude auth status --json` is
  the auth probe; resume is `--resume <id>`. The bridge hides the parent session's `CLAUDECODE` and
  `CLAUDE_CODE_SESSION_ID`-style variables from the child so it starts as an independent session.

## Environment variables

Set these in the orchestrator's environment block, or let `intercom setup` manage the ones it covers.

| Variable | Default | Meaning |
| --- | --- | --- |
| `INTERCOM_HARNESSES` | all | Comma-separated harness keys whose tools are exposed |
| `AGY_BIN` / `CLAUDE_BIN` | `agy` / `claude` | Binary name or absolute path |
| `AGY_AUTO_APPROVE_FLAGS` / `CLAUDE_AUTO_APPROVE_FLAGS` | `--dangerously-skip-permissions` | Injected when `auto_approve` is true |
| `AGY_DEFAULT_FLAGS` / `CLAUDE_DEFAULT_FLAGS` | (empty) | Flags appended to every delegation, e.g. `--model sonnet` |
| `BRIDGE_MAX_DEPTH` | `1` | Delegation depth allowed below this server (loop guard) |
| `BRIDGE_MAX_OUTPUT_CHARS` | `60000` | Per-stream cap in tool results |
| `BRIDGE_KILL_GRACE_SECONDS` | `5` | Delay between SIGTERM and SIGKILL |
| `BRIDGE_HEARTBEAT_SECONDS` | `15` | Progress-notification interval that keeps long delegations under the client's tool-call timeout (`0` disables) |
| `BRIDGE_STRIP_ENV` | (empty) | Extra comma-separated variables hidden from every harness |
| `BRIDGE_LOG_LEVEL` | `INFO` | Server log level (stderr) |
