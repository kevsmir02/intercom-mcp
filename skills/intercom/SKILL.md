---
name: intercom
description: Delegate implementation, refactoring or test-writing work to a headless coding harness (Antigravity `agy`, Claude Code, OpenCode, or pi) through the intercom MCP tools, then run the review-fix loop on the report. Use when a task is scoped enough to hand off, when this session should spare its own context or quota, or when a delegation report has come back and needs review.
---

# Intercom: delegate, review, fix

You are the orchestrator. The harness is a subagent with its own quota, tools and memory:
it edits files and runs tests; you own the brief, the review and the acceptance. Per harness
(`antigravity`, `claude_code`, `opencode`, `pi`) you get `delegate_to_<harness>` (it edits),
`consult_<harness>` (read-only: it answers, it cannot change anything) and
`check_<harness>_health`. `consult_many` puts one question to several harnesses at once,
in parallel. `list_runs` and `get_run` read the run journal. Parameters are documented in
the tool descriptions.

## Second opinion on a plan

When the user asks to "check this with opencode/antigravity", or you are unsure of a plan
you are still writing, call **`consult_many`** with the harnesses they named (or all of
them) and the plan itself in the `prompt`. The harnesses share the repository but not this
conversation, so paste the plan and say what you want attacked -- "what breaks, what did I
miss, where am I wrong?" They answer in parallel, so the panel costs the slowest reply, not
the sum, and none of them can touch a file.

Read the answers against each other. Two harnesses raising the same objection independently
is signal; a disagreement is the part of the plan you have not settled. To press one of
them, call `consult_<harness>` with its Conversation ID from the panel table; to press all
of them, call `consult_many` again with `conversation_ids` and your rebuttal -- one more
tool call, and nobody has to be told the plan twice.

## Loop

1. **Pre-flight** once per session: call `check_<harness>_health`. Done when the report
   starts with `[HEALTH: READY]`. On DEGRADED or UNAVAILABLE, fix the cause it names or
   switch harness.
2. **Write the brief** (the `prompt`). It carries the goal, the files to touch, the
   constraints (style, dependencies, "leave changes uncommitted"), the acceptance
   criteria, and the exact test command followed by "run it and quote the result".
   Done when someone with no access to this conversation could execute it without
   asking a question.
3. **Delegate** with `delegate_to_<harness>`: absolute `working_dir`,
   `include_diff: true` when the report alone should be enough to review,
   `timeout_seconds` sized to the task (default 900). Add `isolate: true` to run in a
   throwaway git worktree instead of the real tree -- use it when the tree is dirty, or
   to race the same brief on two harnesses at once. Only one non-isolated delegation
   runs per working tree; a second returns "working tree busy".
4. **Branch on the prefix** of the report:
   - `[SUCCESS]`: review (step 5).
   - `[ROADBLOCK / FAILURE]`: read "Probable cause" and the diagnostics. An
     environment cause goes back to step 1 (or to another harness), a brief defect
     to step 2, a code or test failure to step 6.
   - `[TIMEOUT_ERROR]`: inspect the partial logs and the working tree; split the task
     or raise the timeout, then step 3.
   - `[INVALID_ARGUMENT]`: fix the call.
5. **Review** the report's "changed by this delegation" list. It is attributed: files
   that were already modified before the run are listed separately and are not the
   harness's work. Read the diff (from the report or `git diff`), then run the test
   command yourself. Done when each changed file is accounted for as correct and the
   tests are green. A "the harness committed" warning means the work is in a commit, not
   in the tree -- say so when you report. For a second opinion on a risky diff, ask a
   different harness with `consult_<harness>`.
6. **Fix round**: re-delegate with `conversation_id` from the report and a short brief:
   the findings, the recommended fix, the same acceptance criteria. The harness keeps
   its context, so the original task stays out of the fix brief. After three rounds
   without a green review, take the work over yourself.
7. **Accept**: summarise what changed and what was verified. Commit from this
   conversation once accepted. An isolated run is accepted by applying its patch:
   the report gives the `git apply` command, or run `intercom apply <run id>`.

Every report carries a **Run ID**. `get_run("<id>")` returns the whole stored report later,
so you can keep only the conclusion in context now and fetch the detail if you need it.
`list_runs()` shows recent runs with status, duration, tokens and cost -- useful for
choosing a harness by what has actually been working and what it has been costing.

## Choosing a harness

- `antigravity`: Gemini, Claude and open models on the Google Antigravity subscription.
- `claude_code`: Claude models on the Claude subscription or API key.
- `opencode`: whichever provider OpenCode is configured with (e.g. OpenCode Go).
- `pi`: the pi coding agent, provider configurable (Google by default).

Only harnesses whose tools appear are enabled; call the matching `check_<harness>_health`
first. To pick between them, call `list_runs()`: it ends with a per-harness rollup of recent
success rate, median duration, cost, and whether that harness has just been hitting quota or
authentication trouble. Otherwise prefer whichever already holds context on the codebase. On
a quota roadblock, resend the same brief to another harness.

## Keeping the main context clean

For a long session, run this loop inside the `intercom-delegate` subagent instead of the main
thread: spawn it with the task, let it delegate, review and fix, and return a short summary. The
verbose report and diff stay in the subagent's context. `intercom setup` installs that subagent.

## Guardrails

- Leave `flags` empty. The harness runs on its own configured model, which is the tested path;
  picking a different model has produced fabricated file contents. Pass `--model` only when the
  user names one.

- Keep secrets out of the brief: the harness inherits the environment and stores its
  transcript. Delegations run with permission auto-approval on and no sandbox; use
  `consult_<harness>` when you only need an answer, not an edit.
- One task per delegation. The bridge enforces one delegation at a time per working
  tree; to run harnesses in parallel, give each `isolate: true` (separate worktrees) or a
  different `working_dir`.
- The bridge refuses nested delegation past `BRIDGE_MAX_DEPTH`. A roadblock naming
  depth means: do the work directly.
