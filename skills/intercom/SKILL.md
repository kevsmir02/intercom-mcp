---
name: intercom
description: Delegate implementation, refactoring or test-writing work to a headless coding harness (Antigravity `agy`, Claude Code, OpenCode, or pi) through the intercom MCP tools, then run the review-fix loop on the report. Use when a task is scoped enough to hand off, when this session should spare its own context or quota, or when a delegation report has come back and needs review.
---

# Intercom: delegate, review, fix

You are the orchestrator. The harness is a subagent with its own quota, tools and memory:
it edits files and runs tests; you own the brief, the review and the acceptance. The
tools are `delegate_to_antigravity`, `delegate_to_claude_code` and the matching
`check_<harness>_health`; their parameters are documented in the tool descriptions.

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
   `timeout_seconds` sized to the task (default 900), the model via `flags`.
4. **Branch on the prefix** of the report:
   - `[SUCCESS]`: review (step 5).
   - `[ROADBLOCK / FAILURE]`: read "Probable cause" and the diagnostics. An
     environment cause goes back to step 1 (or to the other harness), a brief defect
     to step 2, a code or test failure to step 6.
   - `[TIMEOUT_ERROR]`: inspect the partial logs and the working tree; split the task
     or raise the timeout, then step 3.
   - `[INVALID_ARGUMENT]`: fix the call.
5. **Review** every file in the report's `git status --short` list, reading the diff
   (from the report or `git diff`), then run the test command yourself. Done when each
   changed file is accounted for as correct and the tests are green.
6. **Fix round**: re-delegate with `conversation_id` from the report and a short brief:
   the findings, the recommended fix, the same acceptance criteria. The harness keeps
   its context, so the original task stays out of the fix brief. After three rounds
   without a green review, take the work over yourself.
7. **Accept**: summarise what changed and what was verified. Commit from this
   conversation once accepted.

## Choosing a harness

- `antigravity`: Gemini, Claude and open models on the Google Antigravity subscription.
- `claude_code`: Claude models on the Claude subscription or API key.
- `opencode`: whichever provider OpenCode is configured with (e.g. OpenCode Go).
- `pi`: the pi coding agent, provider configurable (Google by default).

Only harnesses whose tools appear are enabled; call the matching `check_<harness>_health`
first. Pick by remaining quota and by which harness already holds context on the codebase.
On a quota roadblock, resend the same brief to another harness.

## Guardrails

- Keep secrets out of the brief: the harness inherits the environment and stores its
  transcript.
- One task per delegation, one delegation at a time per working tree; parallel edits
  in the same tree collide.
- The bridge refuses nested delegation past `BRIDGE_MAX_DEPTH`. A roadblock naming
  depth means: do the work directly.
