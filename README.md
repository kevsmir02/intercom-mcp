# harness-bridge

A lightweight MCP server (stdio) that lets an orchestrator such as OpenCode or Claude Code delegate
implementation, refactoring and testing tasks to a headless coding harness and get a structured,
reviewable report back. Two harnesses are wired in:

| Harness | Binary | Tools |
| --- | --- | --- |
| `antigravity` | Google Antigravity CLI `agy` | `delegate_to_antigravity`, `check_antigravity_health` |
| `claude_code` | Anthropic Claude Code `claude` | `delegate_to_claude_code`, `check_claude_code_health` |

Each harness runs in its JSON print mode, so every report carries the harness's conversation ID.
Passing it back as `conversation_id` resumes that session with its full context: a review-fix round
costs one short prompt.

## Files

| File | Purpose |
| --- | --- |
| `server.py` | The MCP server (harness adapters, subprocess runner, report formatting) |
| `requirements.txt` | Runtime dependency (`mcp`, works with 1.x and 2.x) |
| `opencode.json` | Registration snippet for OpenCode's `mcp` settings |
| `test_bridge.py` | Hermetic tests using fake `agy` and `claude` binaries (no quota consumed) |
| `skills/harness-bridge/SKILL.md` | Skill teaching the orchestrator the delegate -> review -> fix loop |

## Installation

The steps below assume a POSIX shell. Every path in the registrations must be absolute, so the
walkthrough sets `BRIDGE_DIR` once and reuses it.

### 1. Prerequisites

- Python 3.10 or newer (`python3 --version`).
- `git` on `PATH`, used for the change summary in every report.
- At least one harness installed and logged in:
  - Antigravity: `agy` on `PATH`. Run `agy` once interactively to log in, then confirm with
    `agy models`, which lists models only when the login is valid.
  - Claude Code: `claude` on `PATH`. Log in with `claude auth login`, then confirm with
    `claude auth status`, which must report `"loggedIn": true`.

### 2. Place the project

Copy or clone this directory to a permanent location and point `BRIDGE_DIR` at it:

```bash
export BRIDGE_DIR="$HOME/Projects/PERSONAL/intercom"   # adjust to where you put it
cd "$BRIDGE_DIR"
```

### 3. Create the virtual environment and install the dependency

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

`python3 -m venv` bootstraps `pip` inside the venv, so a system-wide `pip` is not required.

### 4. Run the test suite

```bash
.venv/bin/python test_bridge.py
```

The suite starts the real server over stdio against fake `agy` and `claude` scripts, so it consumes no
quota. It ends with `OK` when the installation is sound. `python -m pytest -q test_bridge.py` works too
if pytest is installed.

### 5. Register the server with OpenCode

Merge the `mcp` block of `opencode.json` into your OpenCode config, either the global
`~/.config/opencode/opencode.json` or a project-level `opencode.json`. Replace the two entries of
`command` with `$BRIDGE_DIR/.venv/bin/python` and `$BRIDGE_DIR/server.py` spelled out as absolute paths.

OpenCode merges the `environment` block over its own process environment, so `PATH`, `HOME` and the
harness logins are inherited. The `timeout` of 1200000 ms keeps long delegations alive. Restart
OpenCode; the tools appear prefixed with the server key, `harness-bridge`.

### 6. Register the server with Claude Code

```bash
claude mcp add -s user harness-bridge \
  -e BRIDGE_MAX_DEPTH=1 \
  -- "$BRIDGE_DIR/.venv/bin/python" "$BRIDGE_DIR/server.py"
claude mcp list        # harness-bridge should show as Connected
```

Add more `-e KEY=value` pairs for any variable from the table below, for example
`-e CLAUDE_DEFAULT_FLAGS="--model sonnet"` to pin a cheaper model for delegations.
Use `-s project` instead of `-s user` to register for one repository only.

### 7. Install the skill

Both Claude Code and OpenCode load skills from `~/.claude/skills/`, so one symlink serves both:

```bash
mkdir -p ~/.claude/skills
ln -sn "$BRIDGE_DIR/skills/harness-bridge" ~/.claude/skills/harness-bridge
```

OpenCode also reads `~/.config/opencode/skills/` and project-level `.opencode/skills/` if you prefer
one of those. The skill is model-invoked: the orchestrator reaches for it on its own when a task is
worth delegating or a delegation report needs review, and you can invoke it by name as
`/harness-bridge`.

### 8. Verify from the orchestrator

Start OpenCode or Claude Code and ask it to run `check_claude_code_health` or
`check_antigravity_health`. A report starting with `[HEALTH: READY]` means the binary was found, its
version answered and the authentication probe passed. `[HEALTH: DEGRADED]` names the failing probe;
`[HEALTH: UNAVAILABLE]` means the binary is missing from `PATH`, which `AGY_BIN` or `CLAUDE_BIN` can fix.

Then try a small delegation in a scratch repository, for example "create hello.txt containing the
word hello, run `cat hello.txt` and quote the output", and confirm the report starts with
`[SUCCESS]` and lists the file under `git status --short`.

### Updating

Pull or copy the new files into `$BRIDGE_DIR`, rerun step 4, and restart the orchestrator. The
registrations and the skill symlink keep pointing at the same paths.

### Uninstalling

```bash
claude mcp remove -s user harness-bridge
rm ~/.claude/skills/harness-bridge
```

Remove the `harness-bridge` entry from your OpenCode config, then delete `$BRIDGE_DIR`.

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

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGY_BIN` / `CLAUDE_BIN` | `agy` / `claude` | Binary name or absolute path |
| `AGY_AUTO_APPROVE_FLAGS` / `CLAUDE_AUTO_APPROVE_FLAGS` | `--dangerously-skip-permissions` | Injected when `auto_approve` is true |
| `AGY_DEFAULT_FLAGS` / `CLAUDE_DEFAULT_FLAGS` | (empty) | Flags appended to every delegation, e.g. `--model <id>` |
| `BRIDGE_MAX_DEPTH` | `1` | Delegation depth allowed below this server (loop guard) |
| `BRIDGE_MAX_OUTPUT_CHARS` | `60000` | Per-stream cap in tool results |
| `BRIDGE_KILL_GRACE_SECONDS` | `5` | Delay between SIGTERM and SIGKILL |
| `BRIDGE_STRIP_ENV` | (empty) | Extra comma-separated variables hidden from every harness |
| `BRIDGE_LOG_LEVEL` | `INFO` | Server log level (stderr) |
