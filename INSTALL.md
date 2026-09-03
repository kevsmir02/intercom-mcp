# Install intercom (agent-followable)

You are an AI coding agent (OpenCode or Claude Code). Follow these steps to install the
**intercom** MCP server, which lets you delegate coding tasks to headless harnesses
(Antigravity `agy`, Claude Code `claude`, OpenCode `opencode`, pi `pi`) and get structured,
reviewable reports back. Every step is idempotent and safe to re-run; existing configuration
is preserved.

## 1. Install the project and launcher

Run:

```bash
curl -fsSL https://raw.githubusercontent.com/kevsmir02/intercom-mcp/main/install.sh | bash -s -- --no-setup
```

This clones the project to `~/.local/share/intercom`, creates its virtual environment, and
writes the `intercom` launcher to `~/.local/bin`. If `~/.local/bin` is not on `PATH`, use the
full path `~/.local/bin/intercom` in the following steps.

## 2. Configure harnesses and register the MCP server

Run the setup wizard in non-interactive mode. It detects which harness CLIs are installed,
enables them, registers the server with every orchestrator present (OpenCode and/or Claude
Code), links the skill, and saves the choices. Re-running keeps any selections already saved.

```bash
~/.local/bin/intercom setup --yes
```

To enable only specific harnesses or register with one orchestrator, pass flags instead, for
example:

```bash
~/.local/bin/intercom setup --yes --harness claude_code --harness opencode --orchestrator opencode
```

To set a default model for a harness (optional), add for example
`--flags claude_code="--model sonnet"`.

## 3. Verify

```bash
~/.local/bin/intercom doctor
```

Every line should read `[ok]`. If a harness shows DEGRADED, it means that harness is not
authenticated; that is expected if you do not use it, and does not block the others.

## 4. Tell the human to restart

An orchestrator loads MCP servers at startup, so the new `intercom` tools appear only after a
restart. Tell the human: "Restart OpenCode (or Claude Code), then ask me to run
`check_claude_code_health` — a report starting with `[HEALTH: READY]` confirms it works."

## Notes

- Do not edit the user's `opencode.json` or run `claude mcp` yourself; `intercom setup` does
  the registration and preserves other settings and MCP servers.
- Uninstall with `~/.local/bin/intercom uninstall`.
- This installs software and modifies the user's config. If the human has not asked for it,
  confirm before running step 1.
