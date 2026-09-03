# Installation

How to install, update, and remove intercom. For the one-line install, see the [README](../README.md).

## Installer options

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
| `intercom setup` | The setup wizard. Safe to re-run: it preserves your current selection and lets you toggle harnesses, flags or orchestrators. |
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
