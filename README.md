# intercom

A lightweight MCP server that lets an orchestrator such as OpenCode, Claude Code, or the Antigravity
CLI delegate implementation, refactoring and testing tasks to a headless coding harness, and get a
structured, reviewable report back.

| Harness | Binary | Tools |
| --- | --- | --- |
| `antigravity` | Google Antigravity CLI `agy` | `delegate_to_antigravity`, `consult_antigravity`, `check_antigravity_health` |
| `claude_code` | Anthropic Claude Code `claude` | `delegate_to_claude_code`, `consult_claude_code`, `check_claude_code_health` |
| `opencode` | OpenCode `opencode` | `delegate_to_opencode`, `consult_opencode`, `check_opencode_health` |
| `pi` | pi coding agent `pi` | `delegate_to_pi`, `consult_pi`, `check_pi_health` |

Plus `consult_many`, which asks several harnesses one question in parallel, and `list_runs` /
`get_run`, which read the journal every delegation is recorded in.

Each harness runs in its JSON print mode, so every report carries the harness's conversation ID.
Passing it back as `conversation_id` resumes that session with its full context: a review-fix round
costs one short prompt. A bundled skill teaches the orchestrator the delegate -> review -> fix loop,
and a bundled delegating subagent runs that loop in its own context so the main thread stays clean.

What the report tells you:

- **only what this delegation changed.** The tree is snapshotted before the harness starts, so edits
  that were already there are listed separately, and a harness that commits its work is flagged
  instead of leaving the tree looking untouched.
- **one delegation at a time per working tree.** A second one is refused rather than interleaved.
  Pass `isolate: true` to run in a throwaway `git worktree` instead, which is also how you race the
  same brief on two harnesses at once and keep the better patch.
- **a Run ID.** `get_run` returns the full report later, so the conclusion can stay short now.
- **structured fields as well as prose.** The same result carries `outcome`, `files_changed`,
  `committed`, `conversation_id` and cost, so a caller can branch on data rather than on a prefix.

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

Prerequisites: **Linux**, `git`, Python 3.10+, and at least one harness logged in.

Linux is the only platform this is developed and tested on. The installer is bash, the setup wizard
needs a POSIX terminal, and process-tree termination is built around `/proc` (with a `ps` fallback
that should cover macOS but is not exercised there). Windows is not supported.

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

For a question rather than an edit, ask a harness that cannot touch anything:

> Have Claude Code review this diff for concurrency bugs.

That routes to `consult_claude_code`, which runs in the harness's read-only mode with permission
auto-approval off. To put a plan to several models at once:

> Consult opencode and antigravity about the plan we're making.

That is `consult_many`: they answer in parallel, so the panel costs the slowest reply rather than
the sum, and each answer comes back with its own conversation id so you can argue with one of them
without repeating the plan.

Common commands:

```bash
intercom doctor     # harnesses, registrations, skill, subagent, instructions
intercom setup      # change harnesses, orchestrators or flags (preserves your choices)
intercom runs       # recent delegations: status, duration, tokens, cost
intercom show <id>  # the full stored report of one run (--patch for an isolated run's diff)
intercom update     # pull the latest version
```

## Documentation

| Guide | Contents |
| --- | --- |
| [Installation](docs/installation.md) | Installer options, the `intercom` command, updating, manual install |
| [Orchestrators](docs/orchestrators.md) | OpenCode / Claude Code / Antigravity, extra Claude profiles, the delegating subagent, standing guidance |
| [Reference](docs/reference.md) | Tools, results, change attribution, isolated runs, the run journal, harness specifics, environment variables |
| [Security](SECURITY.md) | What a delegation can do on your machine, and how to confine it |

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

## Safety in one paragraph

A delegation runs a real coding agent on your machine with permission prompts turned off, your
environment (API keys included) and no sandbox. Confine it with `intercom setup --allowed-dir
~/Projects`, keep it off your working tree with `isolate: true`, or use `consult_<harness>` when you
only need an answer. [SECURITY.md](SECURITY.md) has the details.

## License

MIT -- see [LICENSE](LICENSE).
