# Reference

Tools, results, change attribution, timeouts, harness specifics, and every environment variable.

## Tools

Per enabled harness:

| Tool | What it does |
| --- | --- |
| `delegate_to_<harness>` | Runs one headless task that may edit files. Auto-approval on by default. |
| `consult_<harness>` | Asks the harness a question in its read-only mode, auto-approval off. Nothing is edited. |
| `check_<harness>_health` | Binary discovery, version and authentication probe. |

Shared:

| Tool | What it does |
| --- | --- |
| `consult_many` | Puts one question to several harnesses at once, in parallel and read-only, and returns their answers side by side. |
| `list_runs` | Recent runs: harness, status, duration, tokens, cost, files touched. |
| `get_run` | One stored run by Run ID: the full report, its patch, or a one-line summary. |

The stored report of a run is also readable as the MCP resource `intercom://runs/<run id>`.

## Tool results

Every `delegate_to_<harness>` and `consult_<harness>` result starts with one of:

- `[SUCCESS]` exit 0 and the harness reported success: the response, then the attributed change
  summary (plus the diff when `include_diff` is true)
- `[ROADBLOCK / FAILURE]` non-zero exit, harness-reported error, or a working tree that is already
  busy: stderr, extracted stack traces and test failures, probable cause, call to action
- `[TIMEOUT_ERROR]` process tree killed (SIGTERM then SIGKILL), partial logs attached
- `[INVALID_ARGUMENT]` bad input, e.g. a `working_dir` that does not exist

Each report also carries `Conversation ID`, `Run ID` and `Harness stats` (status, turns, tokens,
cost, model). `check_<harness>_health` returns `[HEALTH: READY]`, `[HEALTH: DEGRADED]` or
`[HEALTH: UNAVAILABLE]`.

## Change attribution

The working tree is snapshotted before the harness starts -- `HEAD`, every dirty path, and each of
those paths' size and mtime -- and compared with its state afterwards. The report therefore separates:

- **changed by this delegation** -- paths that appeared, changed status, or changed on disk during
  the run. Only these are diffed when `include_diff` is true.
- **already modified before this delegation, untouched by it** -- listed, never diffed, never
  attributed to the harness.
- **a commit warning** -- if `HEAD` moved, the report says so and lists the commits. A harness that
  commits its work would otherwise leave the tree looking clean, as if nothing had happened.

## Second opinions (`consult_many`)

`consult_many` asks several harnesses the same question at the same time. It is the tool behind
"what does another model think of this plan?".

```
consult_many(prompt="<the plan, and what you want attacked>",
             working_dir="/repo",
             harnesses=["opencode", "antigravity"])   # default: every enabled harness
```

- **Parallel.** The panel costs the slowest answer, not the sum. Two harnesses answering in ~45s
  each return together in ~45s.
- **Read-only, and never queued.** Each answer is a `consult`, so the harness's edit tools are off and
  a panel neither waits for a running delegation nor blocks one. Read-only is a permission setting,
  not a sandbox: the harness can still run commands, and every report carries a change summary that
  says so if the tree moved anyway.
- **Independent.** The harnesses see the repository but not each other's answers and not your
  conversation, so two of them raising the same objection is real signal rather than agreement. Put
  the plan itself in the prompt.
- **Resumable per harness.** Every row of the panel carries that harness's Conversation ID. Push
  back on one with `consult_<harness>`, or on all of them at once by calling `consult_many` again
  with `conversation_ids={"antigravity": "...", "opencode": "..."}` and your rebuttal -- a second
  round is one tool call and nobody is told the plan twice.
- **Compact by default.** Each answer is capped at a share of `BRIDGE_MAX_OUTPUT_CHARS`; the panel
  and every individual answer are journalled, so `get_run("<run id>")` returns any of them in full.
- **No hostages.** Once the first harness answers, the rest have `BRIDGE_PANEL_GRACE` seconds
  (default 120) to finish. Stragglers are stopped, their process trees killed, and their row reads
  `gave up` -- one hung harness cannot hold the answers that did arrive for the whole
  `timeout_seconds`. Ask it on its own afterwards with `consult_<harness>`.

A partly failed panel still returns `[SUCCESS]`: one answer is an answer, and the table says which
harness is missing and why.

## One delegation per working tree

A second delegation against a working tree that already has one running returns
`[ROADBLOCK / FAILURE] working tree busy` instead of interleaving edits with the first.
Consultations are exempt, since they change nothing. To run
harnesses in parallel, either give each a different `working_dir`, or pass `isolate: true`.

## Isolated runs (`isolate: true`)

The delegation runs in a throwaway `git worktree` checked out at `HEAD`, so it cannot touch the real
working tree; isolated runs do not take the tree lock and can run concurrently. Afterwards the
worktree is removed and its changes are stored as a patch. The report prints the command to apply it:

```bash
git -C /path/to/repo apply ~/.local/state/intercom/runs/<run id>.patch
intercom apply <run id>                # the same thing, from the CLI
intercom show <run id> --patch         # read it first
```

`isolate: true` needs a git repository with at least one commit; otherwise the run falls back to the
working tree and the report says so.

## The run journal

Every run is appended to `$XDG_STATE_HOME/intercom/runs.jsonl` (default
`~/.local/state/intercom/runs.jsonl`) with its full report next to it under `runs/`, and its patch
for isolated runs. `BRIDGE_KEEP_RUNS` (default 200) caps how many are kept; `0` keeps everything.
Reports contain your code -- see [SECURITY.md](../SECURITY.md).

```bash
intercom runs --limit 10 --harness claude_code   # or list_runs() from the orchestrator
intercom show 20260904T101500Z-1a2b3c4d          # or get_run("...")
```

Both listings end with a per-harness rollup -- runs, success rate, median duration, cost, and a
warning when recent runs hit quota or authentication trouble. That is what "pick the harness with
remaining quota" should be read from, rather than guessed:

```
by harness (use this to choose one: recent success rate, speed and cost)
harness       runs   ok  fail  timeout   median      cost  recent trouble
antigravity     12   12     0        0    44.0s         -
claude_code      7    5     2        0     6.5s   $1.2400  2 of the last 5 runs hit quota/rate limit -> check_claude_code_health
```

## Long-running delegations

A delegation's own limit is `timeout_seconds` (default 900, up to 86400). The orchestrator also
applies its own timeout to every MCP tool call, which can be shorter. To stop a long delegation from
being cut off by the orchestrator, the server emits a progress notification every
`BRIDGE_HEARTBEAT_SECONDS` (default 15), carrying the elapsed time and the harness's most recent
activity, so a long run shows what it is doing rather than only that it is alive. OpenCode resets
its tool-call timer on each one, so the call survives for the whole delegation, and `timeout_seconds`
stays the real bound.

For that to say anything, the harness has to emit progress as it goes. `opencode` and `pi` already
stream events. `agy` and `claude` print a single JSON object at the very end, so they are asked for
their event streams instead (`--output-format stream-json`, plus `--verbose` for claude, which
refuses stream-json in print mode without it). The terminal result event carries exactly the object
their non-streaming mode prints, so reports are unchanged -- only the progress messages get better,
showing the tool each harness is currently running. `BRIDGE_STREAM_PROGRESS=0` returns them to
single-object mode.

- **OpenCode**: the generated config sets `mcp.intercom.timeout` to three hours as a backstop; the
  heartbeat handles anything longer.
- **Claude Code**: it honours the heartbeat as well; if a delegation still gets cut off, raise
  `MCP_TOOL_TIMEOUT` (milliseconds) in its environment.

Set `BRIDGE_HEARTBEAT_SECONDS=0` to disable the heartbeat.

## Harness facts the bridge relies on

- `agy` (1.1.24 and 1.1.25): headless mode is `-p <prompt>`; auto-approve is
  `--dangerously-skip-permissions`; read-only mode is `--mode plan`; `--output-format stream-json`
  emits `{"event": <name>, <name>: {...}}` lines ending in a `result` event whose payload is the
  plain-json object; print mode has its own
  `--print-timeout` (default 5m) which the bridge raises above `timeout_seconds`; there is no `auth`
  subcommand, so `agy models` is the auth probe; resume is `--conversation <id>`.
  **`agy` cannot take the prompt on stdin** -- `-p` is a value flag and `-p ""` with a piped prompt
  answers `Error: empty prompt` -- so a brief too large for the command line (120 000 bytes) is
  rejected with `[INVALID_ARGUMENT]` rather than sent without a prompt.
- `claude` (2.1.259): `-p` is a boolean flag with the prompt as a positional argument or on stdin;
  auto-approve is `--dangerously-skip-permissions`; read-only mode is `--permission-mode plan`;
  `--output-format stream-json` **requires `--verbose`** in print mode and ends with the same
  `{"type": "result", ...}` object that plain json mode prints; no print timeout flag; `claude auth status --json` is the auth probe; resume is `--resume <id>`. The bridge hides the parent session's `CLAUDECODE` and
  `CLAUDE_CODE_SESSION_ID`-style variables from the child so it starts as an independent session.
- `opencode` (1.18.x): headless mode is `opencode run <prompt>` (or the prompt on stdin); auto-approve is
  `--auto`; read-only mode is the built-in `--agent plan`; structured output is `--format json`, a
  newline-delimited event stream whose events carry a `sessionID` and completed-text parts; the auth probe
  is `opencode auth list`; resume is `--session <id>`.
- `pi` (0.84.x): headless mode is `pi -p` with the prompt guarded by `--`; auto-approve (project trust) is
  `--approve`; read-only mode is `--exclude-tools edit,write`; structured output is `--mode json`, a
  JSON-lines event stream with a `session` header and a final `message_end`; the auth probe is
  `pi auth check --provider <name> --json`; resume is `--session <id>`.
  pi defaults to the `google` provider and needs provider credentials configured.

`check_<harness>_health` prints the version it found next to the version the adapter was verified
against, and says so when they differ -- the flags above are the whole contract with each CLI.

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
| `<H>_DEFAULT_FLAGS` | (empty) | Flags appended to every delegation, e.g. `--model sonnet` |
| `BRIDGE_MAX_DEPTH` | `1` | Delegation depth allowed below this server (loop guard) |
| `BRIDGE_MAX_OUTPUT_CHARS` | `60000` | Per-stream cap in tool results |
| `BRIDGE_KILL_GRACE_SECONDS` | `5` | Delay between SIGTERM and SIGKILL |
| `BRIDGE_HEARTBEAT_SECONDS` | `15` | Progress-notification interval that keeps long delegations under the client's tool-call timeout (`0` disables) |
| `BRIDGE_STRIP_ENV` | (empty) | Extra comma-separated variables hidden from every harness |
| `BRIDGE_ALLOWED_DIRS` | (anywhere) | `:`-separated roots that `working_dir` must sit inside |
| `BRIDGE_REDACT_ENV` | secret-ish names | Regex for env names whose values are masked in reports |
| `BRIDGE_KEEP_RUNS` | `200` | Run records kept on disk (`0` keeps everything) |
| `BRIDGE_STREAM_PROGRESS` | `1` | Ask `agy`/`claude` for an event stream so progress shows live activity (`0` disables) |
| `BRIDGE_PANEL_GRACE` | `120` | Seconds a `consult_many` panel waits for stragglers after its first answer (`0` disables) |
| `INTERCOM_STATE_DIR` | `$XDG_STATE_HOME/intercom` | Where the run journal and patches live |
| `BRIDGE_LOG_LEVEL` | `INFO` | Server log level (stderr) |
