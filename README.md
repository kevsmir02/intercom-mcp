# intercom

A lightweight MCP server that lets an orchestrator such as OpenCode, Claude Code, or the Antigravity
CLI delegate implementation, refactoring and testing tasks to a headless coding harness, and get a
structured, reviewable report back.

| Harness | Binary | Tools |
| --- | --- | --- |
| `antigravity` | Google Antigravity CLI `agy` | `delegate_to_antigravity`, `check_antigravity_health` |
| `claude_code` | Anthropic Claude Code `claude` | `delegate_to_claude_code`, `check_claude_code_health` |
| `opencode` | OpenCode `opencode` | `delegate_to_opencode`, `check_opencode_health` |
| `pi` | pi coding agent `pi` | `delegate_to_pi`, `check_pi_health` |

Each harness runs in its JSON print mode, so every report carries the harness's conversation ID.
Passing it back as `conversation_id` resumes that session with its full context: a review-fix round
costs one short prompt. A bundled skill teaches the orchestrator the delegate -> review -> fix loop,
and a bundled delegating subagent runs that loop in its own context so the main thread stays clean.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/kevsmir02/intercom-mcp/main/install.sh | bash
```

This clones the project to `~/.local/share/intercom`, creates its virtual environment, writes the
`intercom` launcher to `~/.local/bin`, then runs `intercom setup`. The wizard detects the installed
harness CLIs, probes their health, and presents arrow-key checkboxes for which harnesses to expose and
which orchestrators to register with. It then writes the registrations, links the skill and the
`intercom-delegate` subagent, adds a short delegation note to your `CLAUDE.md` / `AGENTS.md` /
`GEMINI.md`, and saves your choices to `~/.config/intercom/config.json`.

Restart the orchestrator afterwards and ask it to run `check_antigravity_health`; a report starting
with `[HEALTH: READY]` confirms the installation.

Prerequisites: `git`, Python 3.10+, and at least one harness logged in.

### Or ask your agent to install it

If you already run OpenCode or Claude Code, tell it:

> Fetch and follow the instructions at https://raw.githubusercontent.com/kevsmir02/intercom-mcp/main/INSTALL.md

## Using it

Ask the main agent to delegate, and it routes through the subagent:

> Fix the failing payments test and delegate it to Antigravity.

The subagent writes the brief, calls intercom, reviews the diff, runs a fix round with the
conversation id if needed, and reports back the outcome, files changed, and test result. Every
delegation result starts with `[SUCCESS]`, `[ROADBLOCK / FAILURE]`, `[TIMEOUT_ERROR]` or
`[INVALID_ARGUMENT]`, so the caller can branch on the prefix.

Common commands:

```bash
intercom doctor     # harnesses, registrations, skill, subagent, instructions
intercom setup      # change harnesses, orchestrators or flags (preserves your choices)
intercom update     # pull the latest version
```

## Documentation

| Guide | Contents |
| --- | --- |
| [Installation](docs/installation.md) | Installer options, the `intercom` command, updating, manual install |
| [Orchestrators](docs/orchestrators.md) | OpenCode / Claude Code / Antigravity, extra Claude profiles, the delegating subagent, standing guidance |
| [Reference](docs/reference.md) | Tool results, long-running delegations, harness specifics, environment variables |

## Files

| File | Purpose |
| --- | --- |
| `install.sh` | The `curl \| bash` installer |
| `intercom.py` | The `intercom` command (setup wizard, doctor, serve, update, uninstall) |
| `server.py` | The MCP server (harness adapters, subprocess runner, report formatting) |
| `skills/intercom/SKILL.md` | Skill teaching the orchestrator the delegate -> review -> fix loop |
| `agents/claude/`, `agents/opencode/` | The `intercom-delegate` subagent, one per orchestrator format |
| `test_bridge.py`, `test_cli.py` | Hermetic tests with fake harness binaries |
| `opencode.json` | Template for a manual OpenCode registration |
| `requirements.txt` | Runtime dependency (`mcp`, works with 1.x and 2.x) |
