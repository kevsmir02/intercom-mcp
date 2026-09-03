# intercom

A lightweight MCP server that lets an orchestrator such as OpenCode or Claude Code delegate
implementation, refactoring and testing tasks to a headless coding harness and get a structured,
reviewable report back. Two harnesses are wired in:

| Harness | Binary | Tools |
| --- | --- | --- |
| `antigravity` | Google Antigravity CLI `agy` | `delegate_to_antigravity`, `check_antigravity_health` |
| `claude_code` | Anthropic Claude Code `claude` | `delegate_to_claude_code`, `check_claude_code_health` |
| `opencode` | OpenCode `opencode` | `delegate_to_opencode`, `check_opencode_health` |
| `pi` | pi coding agent `pi` | `delegate_to_pi`, `check_pi_health` |

Each harness runs in its JSON print mode, so every report carries the harness's conversation ID.
Passing it back as `conversation_id` resumes that session with its full context: a review-fix round
costs one short prompt. A bundled skill teaches the orchestrator the delegate -> review -> fix loop, and a bundled
delegating subagent runs that loop in its own context so the main thread stays clean.

## Install

One command installs the project, then a short wizard asks which harnesses to expose and which
orchestrators to register:

```bash
curl -fsSL https://raw.githubusercontent.com/kevsmir02/intercom-mcp/main/install.sh | bash
```

### Or ask your agent to install it

If you already run OpenCode or Claude Code, tell it:

> Fetch and follow the instructions at https://raw.githubusercontent.com/kevsmir02/intercom-mcp/main/INSTALL.md

The agent runs the installer, configures the harnesses it finds, registers the server, and
verifies with `intercom doctor`. You then restart the orchestrator so it loads the new tools.

What it does:

1. Checks for `git` and Python 3.10+.
2. Clones the repository into `~/.local/share/intercom` (or updates it when already present).
3. Creates a virtual environment there and installs the `mcp` dependency.
4. Writes the `intercom` launcher to `~/.local/bin`.
5. Runs `intercom setup`, which detects the installed harness CLIs (`agy`, `claude`, `opencode`,
   `pi`), probes their health, then presents arrow-key checkboxes (space to toggle, enter to confirm)
   for which harnesses to expose and which orchestrators to register with. It writes the OpenCode
   config entry, runs `claude mcp add` for Claude Code, links the skill and the `intercom-delegate`
   subagent where each orchestrator finds them, and saves its choices to
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
| `intercom setup` | The wizard above. Safe to re-run: it preserves your current selection and lets you toggle harnesses, flags or orchestrators. |
| `intercom setup --harness claude_code --orchestrator opencode --flags claude_code="--model sonnet" --yes` | Scripted setup for dotfiles and CI |
| `intercom setup --orchestrator antigravity --claude-config-dir ~/.claude-b` | Also register the Antigravity CLI and an extra Claude profile |
| `intercom doctor` | Health of the enabled harnesses plus registration, skill and subagent checks; exits non-zero on any failure |
| `intercom serve` | Runs the MCP server on stdio. This is what the registrations invoke |
| `intercom test` | Runs the hermetic test suite (fake harnesses, no quota consumed) |
| `intercom update` | `git pull` plus dependency reinstall |
| `intercom config` | Prints the configuration and every path in use |
| `intercom uninstall [--purge]` | Removes registrations, skill links, launcher and config; `--purge` also deletes the checkout |

`serve` turns `config.json` into the environment variables the server reads, filling in only what the
orchestrator's own environment block left unset. Setting `INTERCOM_HARNESSES=claude_code` there, for
example, exposes only Claude Code's tools.

## Updating

Update an existing install in place:

```bash
intercom update
```

That pulls the latest code into `~/.local/share/intercom` and reinstalls the dependency. It does not
change your registrations or which harnesses are exposed, since those are your settings. To pick up
newly added harnesses (for example `opencode` and `pi`) and refresh the OpenCode timeout, run setup
once more and restart the orchestrator:

```bash
intercom setup      # your current choices come pre-selected; tick any new harness you want
```

Re-running `curl ... | bash` or the agent-followed `INSTALL.md` updates in place the same way.

## Orchestrators

Three orchestrators can host the intercom server, and `intercom setup` registers whichever you pick:

- **OpenCode** — entry merged into `~/.config/opencode/opencode.json`.
- **Claude Code** — `claude mcp add` at user scope; also gets the `intercom-delegate` subagent.
- **Antigravity CLI (`agy`)** — `agy mcp add`; gets the skill in `~/.agents/skills` (agy reads it), but
  not the subagent, which is a Claude/OpenCode construct.

### Additional Claude Code profiles

To also register a second Claude profile that uses `CLAUDE_CONFIG_DIR` (for example `~/.claude-b`),
pass it to setup; it is registered, skill- and subagent-linked, tracked in the config, and removed by
`intercom uninstall`:

```bash
intercom setup --claude-config-dir ~/.claude-b
```

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

- **OpenCode**: the generated config sets `mcp.intercom.timeout` to three hours as a backstop; the
  heartbeat handles anything longer.
- **Claude Code**: it honours the heartbeat as well; if a delegation still gets cut off, raise
  `MCP_TOOL_TIMEOUT` (milliseconds) in its environment.

Set `BRIDGE_HEARTBEAT_SECONDS=0` to disable the heartbeat.

## Delegating subagent (keep the main context clean)

`intercom setup` also installs an **`intercom-delegate`** subagent into each orchestrator you pick
(`~/.claude/agents/` for Claude Code, `~/.config/opencode/agent/` for OpenCode). It runs in its own
context window: the main orchestrator hands it a task, it calls the intercom tools, holds the verbose
report and full diff in its own context, and returns only a short summary. The big report never
enters the main thread.

Use it by asking the main agent to delegate, for example:

> Fix the failing payments test and delegate it to Antigravity.

The main agent spawns `intercom-delegate`, which delegates through intercom, reviews the diff, runs a
fix round with the conversation id if needed, and reports back: outcome, files changed, and test
result. The subagent cannot edit files itself, so it must delegate the implementation. The delegation
depth guard is unaffected, since the subagent is on the orchestrator side, not a delegated harness.

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
| `agents/claude/`, `agents/opencode/` | The `intercom-delegate` subagent, one per orchestrator format |
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
- `opencode` (1.18.x): headless mode is `opencode run <prompt>` (or the prompt on stdin); auto-approve is
  `--auto`; structured output is `--format json`, a newline-delimited event stream whose events carry a
  `sessionID` and completed-text parts; the auth probe is `opencode auth list`; resume is `--session <id>`.
- `pi` (0.84.x): headless mode is `pi -p` with the prompt guarded by `--`; auto-approve (project trust) is
  `--approve`; structured output is `--mode json`, a JSON-lines event stream with a `session` header and a
  final `message_end`; the auth probe is `pi auth check --provider <name> --json`; resume is `--session <id>`.
  pi defaults to the `google` provider and needs provider credentials configured.

The `agy` and `claude` adapters are verified end to end on this project. The `opencode` and `pi` command
construction and JSON schemas are verified from each CLI's `--help`, source, and docs, and exercised against
faithful fake binaries in the test suite; confirm them with a real `check_<harness>_health` and one small
delegation in your environment.

## Environment variables

Set these in the orchestrator's environment block, or let `intercom setup` manage the ones it covers.

| Variable | Default | Meaning |
| --- | --- | --- |
| `INTERCOM_HARNESSES` | all | Comma-separated harness keys to expose: `antigravity`, `claude_code`, `opencode`, `pi` |
| `<H>_BIN` (`AGY_`, `CLAUDE_`, `OPENCODE_`, `PI_`) | the CLI name | Binary name or absolute path per harness |
| `<H>_AUTO_APPROVE_FLAGS` | per harness | Injected when `auto_approve` is true (agy/claude `--dangerously-skip-permissions`, opencode `--auto`, pi `--approve`) |
| `AGY_DEFAULT_FLAGS` / `CLAUDE_DEFAULT_FLAGS` | (empty) | Flags appended to every delegation, e.g. `--model sonnet` |
| `BRIDGE_MAX_DEPTH` | `1` | Delegation depth allowed below this server (loop guard) |
| `BRIDGE_MAX_OUTPUT_CHARS` | `60000` | Per-stream cap in tool results |
| `BRIDGE_KILL_GRACE_SECONDS` | `5` | Delay between SIGTERM and SIGKILL |
| `BRIDGE_HEARTBEAT_SECONDS` | `15` | Progress-notification interval that keeps long delegations under the client's tool-call timeout (`0` disables) |
| `BRIDGE_STRIP_ENV` | (empty) | Extra comma-separated variables hidden from every harness |
| `BRIDGE_LOG_LEVEL` | `INFO` | Server log level (stderr) |
