# Reference

Tool results, timeouts, harness specifics, and every environment variable.

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
