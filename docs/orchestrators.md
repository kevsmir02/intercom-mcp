# Orchestrators and the delegating subagent

Which tools can host the intercom server, how the `intercom-delegate` subagent keeps the main thread's context clean, and the standing guidance that makes it the default.

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

## Standing delegation guidance

Installing the subagent makes it available, not mandatory: a main agent with the intercom tools can
still call them directly and pull the whole report into its own context. To make the subagent the
default, `intercom setup` writes a short marked block into each orchestrator's always-loaded
instruction file:

| Orchestrator | File |
| --- | --- |
| Claude Code | `~/.claude/CLAUDE.md` (and `<profile>/CLAUDE.md` for extra profiles) |
| OpenCode | `~/.config/opencode/AGENTS.md` |
| Antigravity CLI | `~/.gemini/GEMINI.md` |

The block sits between `<!-- INTERCOM_START -->` and `<!-- INTERCOM_END -->`, so re-running setup
updates it in place rather than duplicating, `intercom uninstall` strips it, and everything else in
the file is untouched. It says two things: route delegations through the `intercom-delegate` subagent,
and leave `flags` empty unless the user names a model. The Antigravity variant omits the subagent
sentence, since agy has no subagent mechanism.

Skip it with `intercom setup --no-instructions` if you would rather write your own guidance.
