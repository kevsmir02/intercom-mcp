#!/usr/bin/env python3
"""
intercom: a lightweight MCP server (stdio) that lets an orchestrator such as
OpenCode or Claude Code delegate implementation, refactoring and testing tasks to a
headless coding harness and get a structured, reviewable report back.

Supported harnesses
-------------------
antigravity   Google Antigravity CLI `agy`         tools: delegate_to_antigravity, check_antigravity_health
claude_code   Anthropic Claude Code `claude`       tools: delegate_to_claude_code,  check_claude_code_health

Each harness runs in its JSON print mode, so every report carries the harness's
conversation/session ID. Passing that ID back as `conversation_id` resumes the same
session with its full context: the review -> fix loop costs one short prompt per round.

Result prefixes (branch on these)
---------------------------------
[SUCCESS]              exit 0 and the harness reported success; response + git change summary
[ROADBLOCK / FAILURE]  non-zero exit or harness-reported error; stderr + diagnostics + call to action
[TIMEOUT_ERROR]        process tree killed after timeout_seconds; partial logs attached
[INVALID_ARGUMENT]     bad input (missing working_dir, interactive flags, malformed conversation_id)
[HEALTH: READY|DEGRADED|UNAVAILABLE]  from the check_*_health tools

Configuration (environment variables, all optional)
---------------------------------------------------
Shared
  INTERCOM_HARNESSES         comma-separated harness keys to expose            (default: all)
  BRIDGE_MAX_OUTPUT_CHARS    per-stream cap in tool results                    (default 60000)
  BRIDGE_KILL_GRACE_SECONDS  SIGTERM -> SIGKILL escalation delay               (default 5)
  BRIDGE_HEARTBEAT_SECONDS   progress-notification interval during a run       (default 15; 0 disables)
  BRIDGE_MAX_DEPTH           delegation depth allowed below this server        (default 1)
  BRIDGE_STRIP_ENV           extra comma-separated env names hidden from every harness
  BRIDGE_LOG_LEVEL           DEBUG | INFO | WARNING | ERROR                    (default INFO)
  BRIDGE_DEPTH               set automatically on children; do not set by hand
Per harness (prefix AGY_ for antigravity, CLAUDE_ for claude_code)
  <PREFIX>_BIN                binary name or absolute path
  <PREFIX>_AUTO_APPROVE_FLAGS flags injected when auto_approve=true   (default --dangerously-skip-permissions)
  <PREFIX>_DEFAULT_FLAGS      flags appended to every delegation, e.g. "--model gemini-3.1-pro-high"

Verified against agy 1.1.24/1.1.25 and Claude Code 2.1.259 (see README.md).
Transport is stdio: stdout belongs to the MCP protocol, all logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable

from pydantic import Field

try:  # mcp >= 2.0 (FastMCP was renamed to MCPServer)
    from mcp.server.mcpserver import MCPServer as _ServerImpl
    from mcp.server.mcpserver import Context as _Context
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerImpl
    from mcp.server.fastmcp import Context as _Context

__version__ = "2.0.0"
SERVER_NAME = "intercom"

log = logging.getLogger(SERVER_NAME)

# ---------------------------------------------------------------------------
# Constants and configuration
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 900
MAX_TIMEOUT_SECONDS = 86_400
HEALTH_PROBE_TIMEOUT = 45
GIT_PROBE_TIMEOUT = 30
PRINT_TIMEOUT_MARGIN = 60  # harness-side print timeout = timeout_seconds + margin
PROMPT_ARG_MAX_BYTES = 65_536  # above this the prompt is piped through stdin
MAX_UNTRACKED_DIFF_FILES = 20
DEPTH_ENV = "BRIDGE_DEPTH"
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# An async progress sink: (progress_value, total_or_None, message) -> None.
ProgressFn = Callable[[float, "float | None", str], "Any"]


def _env(name: str, *aliases: str, default: str = "") -> str:
    for key in (name, *aliases):
        raw = os.environ.get(key)
        if raw is not None and raw.strip():
            return raw.strip()
    return default


def _env_int(name: str, default: int, minimum: int = 0, *aliases: str) -> int:
    raw = _env(name, *aliases)
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log.warning("Ignoring non-integer %s=%r", name, raw)
        return default


def _env_flags(name: str, default: str) -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    try:
        return shlex.split(raw)
    except ValueError as exc:
        log.warning("Ignoring unparsable %s=%r (%s)", name, raw, exc)
        return shlex.split(default)


def max_output_chars() -> int:
    return _env_int("BRIDGE_MAX_OUTPUT_CHARS", 60_000, 2_000, "AGY_MAX_OUTPUT_CHARS")


def kill_grace_seconds() -> float:
    return float(_env_int("BRIDGE_KILL_GRACE_SECONDS", 5, 0, "AGY_KILL_GRACE_SECONDS"))


def heartbeat_seconds() -> float:
    """Interval between progress notifications during a delegation. Clients that reset their
    per-call timeout on progress (e.g. OpenCode) will not abort a long-running delegation."""
    return float(_env_int("BRIDGE_HEARTBEAT_SECONDS", 15, 0))


def bridge_depth() -> int:
    return _env_int(DEPTH_ENV, 0)


def max_depth() -> int:
    return _env_int("BRIDGE_MAX_DEPTH", 1)


def extra_strip_env() -> list[str]:
    return [name.strip() for name in _env("BRIDGE_STRIP_ENV").split(",") if name.strip()]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def clean_text(text: str) -> str:
    """Strip ANSI escapes and normalise line endings."""
    text = _ANSI_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def truncate(text: str, limit: int) -> str:
    """Keep the head and tail of an over-long text with a marker in between."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    dropped = len(text) - head - tail
    return text[:head] + f"\n... [{dropped} characters truncated by {SERVER_NAME}] ...\n" + text[-tail:]


_DIAG_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"Traceback \(most recent call last\)",
        r"^\s*(?:FAILED|FAIL|ERROR|ERRORS)\b",
        r"^\s*E\s{2,}\S",  # pytest assertion detail lines
        r"\b(?:AssertionError|TypeError|ValueError|KeyError|RuntimeError|ImportError|"
        r"ModuleNotFoundError|SyntaxError|NameError|AttributeError|IndexError)\b",
        r"\berror\[E\d+\]",  # rustc
        r"^\s*error(?: TS\d+)?:",  # tsc / generic
        r"^\s*panic:",  # go
        r"\bnpm (?:ERR!|error)\b",
        r"^\s*[✗×]\s",
        r"^\s*●\s.*›",  # jest
        r"\b\d+ (?:failed|failing|errors?)\b",
        r"^\s*Error:",
        r"\bfatal:",
        r"Segmentation fault",
        r"\bE(?:NOENT|ACCES|PERM)\b",
        r"exit status \d+",
        r"(?:quota|rate.?limit|too many requests|unauthenticated|unauthori[sz]ed|"
        r"permission denied|login required|HTTP (?:401|403|429))",
    )
]


def extract_diagnostics(text: str, max_lines: int = 80, context: int = 2) -> str:
    """Pull stack traces, test failures and error lines (with context) out of raw output."""
    lines = text.splitlines()
    keep: set[int] = set()
    for idx, line in enumerate(lines):
        if not any(p.search(line) for p in _DIAG_PATTERNS):
            continue
        lo = max(0, idx - context)
        hi = min(len(lines), idx + context + 1)
        if "Traceback (most recent call last)" in line:
            end = idx + 1  # a traceback block ends at its (non-indented) exception line
            while end < len(lines) and (not lines[end].strip() or lines[end][:1].isspace()):
                end += 1
            hi = min(len(lines), end + 1)
        keep.update(range(lo, hi))
    if not keep:
        return ""
    out: list[str] = []
    last = -2
    for idx in sorted(keep):
        if idx != last + 1 and out:
            out.append("    ...")
        out.append(lines[idx])
        last = idx
        if len(out) >= max_lines:
            remaining = sum(1 for k in keep if k > idx)
            if remaining:
                out.append(f"    ... ({remaining} more matching lines omitted)")
            break
    return "\n".join(out)


def _signal_name(num: int) -> str:
    try:
        return signal.Signals(num).name
    except (ValueError, AttributeError):
        return str(num)


def _fmt_duration(seconds: float) -> str:
    return f"{seconds:.1f}s"


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Find the JSON object a harness printed, tolerating banner lines around it."""
    stripped = text.strip()
    if not stripped:
        return None
    candidates: list[str] = []
    if stripped.startswith("{"):
        candidates.append(stripped)
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            candidates.append(line)
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ---------------------------------------------------------------------------
# Subprocess execution with process-tree termination guarantees
# ---------------------------------------------------------------------------


class _BoundedBuffer:
    """Keeps the first and last `limit` bytes of a stream and counts what was dropped."""

    __slots__ = ("_head", "_tail", "_limit", "dropped")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._head = bytearray()
        self._tail = bytearray()
        self.dropped = 0

    def write(self, chunk: bytes) -> None:
        if len(self._head) < self._limit:
            room = self._limit - len(self._head)
            self._head += chunk[:room]
            chunk = chunk[room:]
            if not chunk:
                return
        self._tail += chunk
        if len(self._tail) > self._limit:
            excess = len(self._tail) - self._limit
            self.dropped += excess
            del self._tail[:excess]

    def text(self) -> str:
        data = bytes(self._head)
        if self.dropped:
            data += f"\n... [{self.dropped} bytes dropped by {SERVER_NAME} capture limit] ...\n".encode()
        data += bytes(self._tail)
        return clean_text(data.decode("utf-8", errors="replace"))


@dataclass
class ProcessResult:
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False


def _pid_alive(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            data = stat_path.read_bytes()
            state = data[data.rfind(b")") + 2 : data.rfind(b")") + 3]
            return state != b"Z"
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def descendant_pids(root: int) -> list[int]:
    """All descendants of `root` (Linux /proc walk; empty elsewhere)."""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return []
    children: dict[int, list[int]] = {}
    for entry in os.listdir(proc_dir):
        if not entry.isdigit():
            continue
        try:
            data = (proc_dir / entry / "stat").read_bytes()
        except OSError:
            continue
        fields = data[data.rfind(b")") + 2 :].split()
        if len(fields) < 2:
            continue
        children.setdefault(int(fields[1]), []).append(int(entry))
    found: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            found.append(child)
            stack.append(child)
    return found


def signal_process_tree(pid: int, sig: int, extra_pids: list[int] | None = None) -> list[int]:
    """Signal the process group led by `pid` plus every descendant found via /proc.

    Returns the pids that were targeted (useful for a later liveness sweep).
    """
    targets = set(descendant_pids(pid)) | set(extra_pids or []) | {pid}
    if os.name == "posix":
        try:
            os.killpg(pid, sig)  # started with start_new_session -> pgid == pid
        except OSError:
            pass
        for target in targets:
            try:
                os.kill(target, sig)
            except OSError:
                pass
    else:  # pragma: no cover - Windows: taskkill handles the tree
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    return sorted(targets)


async def terminate_process_tree(proc: asyncio.subprocess.Process, grace: float) -> None:
    """SIGTERM the whole tree, wait `grace` seconds, then SIGKILL survivors."""
    if proc.returncode is not None and not descendant_pids(proc.pid):
        return
    targets = signal_process_tree(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        log.warning("pid %s ignored SIGTERM; escalating to SIGKILL", proc.pid)
    survivors = [p for p in targets if p != proc.pid and _pid_alive(p)]
    if proc.returncode is None:
        survivors.append(proc.pid)
    # Always sweep the (possibly leaderless) group with SIGKILL: harmless if everything is gone.
    signal_process_tree(proc.pid, signal.SIGKILL, extra_pids=survivors)
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(grace, 1.0))
        except asyncio.TimeoutError:
            log.error("pid %s did not exit after SIGKILL", proc.pid)


async def _pump(stream: asyncio.StreamReader | None, buf: _BoundedBuffer) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return
        buf.write(chunk)


async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:  # pragma: no cover - best effort
            pass


async def run_process(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    stdin_data: bytes | None = None,
    env: dict[str, str] | None = None,
    kill_grace: float | None = None,
) -> ProcessResult:
    """Run `argv` asynchronously, capturing output incrementally.

    On timeout the entire process tree is terminated (SIGTERM, then SIGKILL) and the
    output captured so far is returned with `timed_out=True`. If the awaiting task is
    cancelled, the tree is SIGKILLed synchronously before the cancellation propagates.
    """
    run_env = dict(os.environ) if env is None else dict(env)
    run_env.setdefault("NO_COLOR", "1")
    grace = kill_grace_seconds() if kill_grace is None else kill_grace
    popen_kwargs: dict[str, object] = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:  # pragma: no cover - Windows
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    capture_limit = max(max_output_chars() * 4, 256_000)
    out_buf, err_buf = _BoundedBuffer(capture_limit), _BoundedBuffer(capture_limit)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=run_env,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **popen_kwargs,
    )
    log.debug("started pid %s: %s", proc.pid, argv[:1])
    pumps = [
        asyncio.ensure_future(_pump(proc.stdout, out_buf)),
        asyncio.ensure_future(_pump(proc.stderr, err_buf)),
    ]
    if stdin_data is not None:
        pumps.append(asyncio.ensure_future(_feed_stdin(proc, stdin_data)))

    timed_out = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            log.warning("pid %s exceeded %.0fs; terminating process tree", proc.pid, timeout)
            await terminate_process_tree(proc, grace)
        # Drain whatever is left in the pipes (bounded: an escaped grandchild could hold them open).
        try:
            await asyncio.wait_for(asyncio.gather(*pumps, return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            for task in pumps:
                task.cancel()
    except BaseException:
        # Cancellation or unexpected failure: never leave a harness tree behind. This path
        # must not await (anyio re-raises CancelledError at every checkpoint).
        if proc.returncode is None:
            log.warning("delegation interrupted; killing process tree pid %s", proc.pid)
            signal_process_tree(proc.pid, signal.SIGKILL)
        for task in pumps:
            task.cancel()
        raise

    return ProcessResult(
        argv=list(argv),
        returncode=None if timed_out else proc.returncode,
        stdout=out_buf.text(),
        stderr=err_buf.text(),
        duration=time.monotonic() - started,
        timed_out=timed_out,
    )


async def run_process_with_heartbeat(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    stdin_data: bytes | None = None,
    env: dict[str, str] | None = None,
    progress: "ProgressFn | None" = None,
    label: str = "task",
) -> ProcessResult:
    """run_process, plus a periodic progress notification while it runs.

    A client that resets its per-request timeout on progress (OpenCode does; Claude Code
    honours MCP_TOOL_TIMEOUT) then keeps the call alive for the whole delegation instead of
    cutting it off at its own shorter tool-call timeout. The heartbeat is best-effort: if the
    client did not ask for progress, the first send no-ops and the loop stops; it never fails
    the run, and `timeout_seconds` stays the authoritative bound.
    """
    interval = heartbeat_seconds()
    if progress is None or interval <= 0:
        return await run_process(argv, cwd=cwd, timeout=timeout, stdin_data=stdin_data, env=env)

    done = asyncio.Event()

    async def beat() -> None:
        start = time.monotonic()
        ticks = 0
        try:
            await progress(0.0, None, f"{label}: started (budget {int(timeout)}s)")
        except Exception:  # client does not want progress; stop trying
            return
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if done.is_set():
                return
            ticks += 1
            elapsed = time.monotonic() - start
            try:
                await progress(float(ticks), None, f"{label}: running {int(elapsed)}s of {int(timeout)}s budget")
            except Exception:
                return

    task = asyncio.ensure_future(beat())
    try:
        return await run_process(argv, cwd=cwd, timeout=timeout, stdin_data=stdin_data, env=env)
    finally:
        done.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ---------------------------------------------------------------------------
# Harness adapters
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What the harness said, normalised across harnesses."""

    text: str
    structured: bool = False
    conversation_id: str | None = None
    status: str | None = None
    harness_error: bool | None = None
    turns: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Harness:
    key: str
    label: str
    binary: str
    env_prefix: str
    default_auto_approve: str
    prompt_is_positional: bool  # claude: -p is boolean and the prompt positional; agy: -p takes the value
    json_flags: tuple[str, ...]
    output_format_flag: str
    timeout_flag: str | None
    resume_flag: str
    interactive_flags: tuple[str, ...]
    prompt_flags: tuple[str, ...]
    auth_probe: tuple[str, ...]
    credential_path: str
    strip_env: tuple[str, ...]
    usage_error_re: str
    parse_json: Callable[[dict[str, Any]], Outcome]
    parse_auth: Callable[[ProcessResult], tuple[bool, str, str]]

    def binary_setting(self) -> str:
        return _env(f"{self.env_prefix}_BIN", default=self.binary)

    def resolve_binary(self) -> str | None:
        return shutil.which(os.path.expanduser(self.binary_setting()))

    def auto_approve_flags(self) -> list[str]:
        return _env_flags(f"{self.env_prefix}_AUTO_APPROVE_FLAGS", self.default_auto_approve)

    def default_flags(self) -> list[str]:
        return _env_flags(f"{self.env_prefix}_DEFAULT_FLAGS", "")

    def child_env(self) -> dict[str, str]:
        """Parent environment (auth, PATH, HOME) minus session-identity variables, plus the depth marker."""
        env = dict(os.environ)
        for name in (*self.strip_env, *extra_strip_env()):
            env.pop(name, None)
        env[DEPTH_ENV] = str(bridge_depth() + 1)
        env.setdefault("NO_COLOR", "1")
        return env


def _parse_agy_json(data: dict[str, Any]) -> Outcome:
    usage = data.get("usage") or {}
    status = str(data.get("status") or "").strip() or None
    return Outcome(
        text=str(data.get("response") or ""),
        structured=True,
        conversation_id=str(data["conversation_id"]) if data.get("conversation_id") else None,
        status=status,
        harness_error=(status.upper() not in ("SUCCESS", "OK", "COMPLETED")) if status else None,
        turns=_as_int(data.get("num_turns")),
        tokens_in=_as_int(usage.get("input_tokens")),
        tokens_out=_as_int(usage.get("output_tokens")),
    )


def _parse_claude_json(data: dict[str, Any]) -> Outcome:
    usage = data.get("usage") or {}
    subtype = data.get("subtype")
    is_error = data.get("is_error")
    notes: list[str] = []
    denials = data.get("permission_denials") or []
    if denials:
        notes.append(
            f"{len(denials)} tool call(s) were denied by permissions; delegate with auto_approve=true "
            "or pass ['--allowedTools', ...] in flags."
        )
    terminal = data.get("terminal_reason")
    if terminal and terminal != "completed":
        notes.append(f"terminal_reason={terminal}")
    if data.get("api_error_status"):
        notes.append(f"api_error_status={data['api_error_status']}")
    tokens_in = sum(
        _as_int(usage.get(k)) or 0
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    model_usage = data.get("modelUsage") or {}
    return Outcome(
        text=str(data.get("result") or ""),
        structured=True,
        conversation_id=str(data["session_id"]) if data.get("session_id") else None,
        status=str(subtype) if subtype else None,
        harness_error=bool(is_error) if is_error is not None else (subtype not in (None, "success")),
        turns=_as_int(data.get("num_turns")),
        tokens_in=tokens_in if usage else None,
        tokens_out=_as_int(usage.get("output_tokens")),
        cost_usd=data.get("total_cost_usd") if isinstance(data.get("total_cost_usd"), (int, float)) else None,
        model=", ".join(str(k) for k in model_usage) or None,
        notes=notes,
    )


def _auth_agy(result: ProcessResult) -> tuple[bool, str, str]:
    # agy has no `auth` subcommand; `agy models` queries the authenticated backend.
    model_lines = [ln for ln in result.stdout.splitlines() if "\t" in ln]
    if result.returncode == 0 and model_lines:
        return (
            True,
            f"authenticated ({len(model_lines)} models available via `agy models`)",
            "available models (pass via flags: ['--model', '<id>'])\n" + "\n".join(model_lines[:25]),
        )
    detail = (result.stderr or result.stdout).strip()[:400] or "no output"
    return False, f"NOT authenticated or backend unreachable (exit {result.returncode}): {detail}", ""


def _auth_claude(result: ProcessResult) -> tuple[bool, str, str]:
    data = _extract_json_object(result.stdout) or {}
    if result.returncode == 0 and data.get("loggedIn"):
        # Only non-identifying fields are surfaced: the JSON also carries the account email.
        parts = [f"method={data.get('authMethod')}" if data.get("authMethod") else ""]
        parts.append(f"subscription={data.get('subscriptionType')}" if data.get("subscriptionType") else "")
        parts.append(f"provider={data.get('apiProvider')}" if data.get("apiProvider") else "")
        return True, "authenticated (" + ", ".join(p for p in parts if p) + ")", ""
    if data:
        return False, "NOT authenticated: `claude auth status` reports loggedIn=false; run `claude auth login`", ""
    detail = result.stderr.strip()[:400] or "no output"
    return False, f"NOT authenticated or probe failed (exit {result.returncode}): {detail}", ""


HARNESSES: dict[str, Harness] = {
    "antigravity": Harness(
        key="antigravity",
        label="Antigravity CLI (agy)",
        binary="agy",
        env_prefix="AGY",
        default_auto_approve="--dangerously-skip-permissions",
        prompt_is_positional=False,
        json_flags=("--output-format", "json"),
        output_format_flag="--output-format",
        timeout_flag="--print-timeout",
        resume_flag="--conversation",
        interactive_flags=("-i", "--prompt-interactive"),
        prompt_flags=("-p", "--print", "--prompt"),
        auth_probe=("models",),
        credential_path="~/.gemini/antigravity-cli/antigravity-oauth-token",
        strip_env=(),
        usage_error_re=r"unexpected argument|unknown flag|flag provided but not defined|usage of agy",
        parse_json=_parse_agy_json,
        parse_auth=_auth_agy,
    ),
    "claude_code": Harness(
        key="claude_code",
        label="Claude Code (claude)",
        binary="claude",
        env_prefix="CLAUDE",
        default_auto_approve="--dangerously-skip-permissions",
        prompt_is_positional=True,
        json_flags=("--output-format", "json"),
        output_format_flag="--output-format",
        timeout_flag=None,  # no print timeout flag; the bridge timeout is the only limit
        resume_flag="--resume",
        interactive_flags=(),
        prompt_flags=("-p", "--print"),
        auth_probe=("auth", "status", "--json"),
        credential_path="~/.claude/.credentials.json",
        # Session-identity variables of a parent Claude Code session; the child must not
        # attach to the parent's session or messaging socket. Settings such as
        # CLAUDE_CODE_USE_BEDROCK are kept.
        strip_env=(
            "CLAUDECODE",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_ENTRYPOINT",
        ),
        usage_error_re=r"error: unknown option|error: missing required argument|error: option '.*' argument missing|error: too many arguments",
        parse_json=_parse_claude_json,
        parse_auth=_auth_claude,
    ),
}


def parse_outcome(harness: Harness, stdout: str) -> Outcome:
    data = _extract_json_object(stdout)
    if data is not None:
        try:
            return harness.parse_json(data)
        except Exception as exc:  # defensive: never let a schema surprise hide the raw output
            log.warning("could not parse %s JSON output (%s); falling back to raw text", harness.key, exc)
    return Outcome(text=stdout, structured=False)


def classify_failure(harness: Harness, returncode: int | None, stdout: str, stderr: str) -> str:
    blob = f"{stdout}\n{stderr}".lower()
    if re.search(harness.usage_error_re, stderr.lower()):
        return f"CLI usage error: check the `flags` argument against `{harness.binary} --help`."
    if re.search(r"quota|rate.?limit|too many requests|\b429\b|usage limit", blob):
        return (
            f"{harness.label} quota or rate limit reached: wait, reduce task size, pick another model, "
            f"or delegate to the other harness; run check_{harness.key}_health."
        )
    if re.search(r"unauthenticated|unauthori[sz]ed|not logged in|login required|token expired|\b401\b|\b403\b", blob):
        return f"Authentication problem: run check_{harness.key}_health and re-authenticate {harness.binary}."
    if re.search(
        r"traceback \(most recent call last\)|assertionerror|\bfailed\b|\d+ failing|error\[e\d+\]|^\s*panic:",
        blob,
        re.MULTILINE,
    ):
        return "Task-level failure (tests, build or runtime error): see the diagnostics section."
    if re.search(r"print.?timeout (?:exceeded|reached|expired)|timed out (?:waiting|after)|context deadline exceeded", blob):
        return "The harness's own timeout or a network deadline fired: raise timeout_seconds or split the task."
    if returncode is not None and returncode < 0:
        return f"{harness.binary} was killed by signal {_signal_name(-returncode)} (external termination or OOM)."
    if re.search(r"\berror\b|exception", blob):
        return "Task-level failure (tests, build or runtime error): see the diagnostics section."
    return "Unclassified: inspect the harness response, stderr and stdout below."


# ---------------------------------------------------------------------------
# Command construction and validation
# ---------------------------------------------------------------------------


def validate_working_dir(raw: object) -> tuple[Path | None, str | None]:
    if not isinstance(raw, str) or not raw.strip():
        return None, "working_dir must be a non-empty path string."
    path = Path(os.path.expandvars(os.path.expanduser(raw.strip())))
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        return None, f"working_dir does not exist: {path}"
    if not path.is_dir():
        return None, f"working_dir is not a directory: {path}"
    if not os.access(path, os.R_OK | os.X_OK):
        return None, f"working_dir is not accessible (need read+execute permission): {path}"
    return path.resolve(), None


def _has_flag(flags: list[str], name: str) -> bool:
    return any(f == name or f.startswith(name + "=") for f in flags)


def _matches(flag: str, names: tuple[str, ...]) -> bool:
    return any(flag == n or (n.startswith("--") and flag.startswith(n + "=")) for n in names)


def build_argv(
    harness: Harness,
    binary: str,
    prompt: str,
    flags: list[str],
    auto_approve: bool,
    timeout_seconds: int,
    conversation_id: str | None = None,
    *,
    approve_flags: list[str] | None = None,
    extra_flags: list[str] | None = None,
) -> tuple[list[str], bytes | None]:
    """Assemble the harness command line. Returns (argv, stdin payload or None)."""
    for flag in flags:
        if _matches(flag, harness.interactive_flags):
            raise ValueError(f"flag {flag!r} starts an interactive session and would hang a headless run; remove it.")
        if _matches(flag, harness.prompt_flags):
            raise ValueError(f"do not pass {flag!r} in `flags`; the prompt argument is injected automatically.")
        if _matches(flag, (harness.resume_flag,)):
            raise ValueError(f"do not pass {flag!r} in `flags`; use the conversation_id argument instead.")

    approve_flags = harness.auto_approve_flags() if approve_flags is None else approve_flags
    extra_flags = harness.default_flags() if extra_flags is None else extra_flags
    user_flags = [*extra_flags, *flags]

    argv = [binary]
    encoded = prompt.encode("utf-8")
    via_stdin = len(encoded) > PROMPT_ARG_MAX_BYTES or (harness.prompt_is_positional and prompt.startswith("-"))
    stdin_data: bytes | None = None
    if via_stdin:
        if harness.prompt_is_positional:
            argv.append("-p")  # boolean print flag; the prompt arrives on stdin
        stdin_data = encoded
    else:
        argv += ["-p", prompt]
    if conversation_id:
        argv += [harness.resume_flag, conversation_id]
    if auto_approve and approve_flags and not _has_flag(user_flags, approve_flags[0]):
        argv += approve_flags
    if harness.json_flags and not _has_flag(user_flags, harness.output_format_flag):
        argv += harness.json_flags
    if harness.timeout_flag and not _has_flag(user_flags, harness.timeout_flag):
        argv += [harness.timeout_flag, f"{timeout_seconds + PRINT_TIMEOUT_MARGIN}s"]
    argv += user_flags
    return argv, stdin_data


def _display_command(argv: list[str], prompt: str, via_stdin: bool) -> str:
    shown = list(argv)
    if not via_stdin:
        try:
            shown[shown.index(prompt)] = f"<prompt: {len(prompt)} chars>"
        except ValueError:
            pass
    rendered = shlex.join(shown).replace("'<prompt: ", "<prompt: ").replace(" chars>'", " chars>")
    if via_stdin:
        rendered += f"  (prompt: {len(prompt)} chars piped via stdin)"
    return rendered


# ---------------------------------------------------------------------------
# Git change summary
# ---------------------------------------------------------------------------


async def _git(git: str, args: list[str], cwd: Path) -> ProcessResult:
    return await run_process([git, *args], cwd=str(cwd), timeout=GIT_PROBE_TIMEOUT, kill_grace=1)


async def collect_git_summary(cwd: Path, include_diff: bool = False) -> str:
    git = shutil.which("git")
    if not git:
        return "(git not installed; no change summary available)"
    try:
        probe = await _git(git, ["rev-parse", "--is-inside-work-tree"], cwd)
    except OSError as exc:
        return f"(git probe failed: {exc})"
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return "(not a git repository; no change summary available)"

    limit = max_output_chars()
    status = await _git(git, ["status", "--short"], cwd)
    diffstat = await _git(git, ["diff", "--stat", "HEAD"], cwd)
    has_head = diffstat.returncode == 0
    if not has_head:  # repository without any commit yet
        diffstat = await _git(git, ["diff", "--stat"], cwd)
    # rstrip only: the leading column of `git status --short` (staged vs unstaged) is significant.
    status_text = status.stdout.rstrip() or "(clean: no modified or untracked files)"
    diff_text = diffstat.stdout.rstrip() or "(no tracked-file changes)"
    summary = (
        f"$ git status --short\n{truncate(status_text, limit // 4)}\n\n"
        f"$ git diff --stat HEAD\n{truncate(diff_text, limit // 4)}"
    )
    if not include_diff:
        return summary

    pieces: list[str] = []
    full = await _git(git, ["diff", "HEAD"] if has_head else ["diff"], cwd)
    if full.stdout.strip():
        pieces.append(full.stdout.rstrip())
    untracked = await _git(git, ["ls-files", "--others", "--exclude-standard"], cwd)
    files = [ln for ln in untracked.stdout.splitlines() if ln.strip()]
    for path in files[:MAX_UNTRACKED_DIFF_FILES]:
        new_file = await _git(git, ["diff", "--no-index", "--", os.devnull, path], cwd)
        if new_file.stdout.strip():
            pieces.append(new_file.stdout.rstrip())
    if len(files) > MAX_UNTRACKED_DIFF_FILES:
        pieces.append(f"... {len(files) - MAX_UNTRACKED_DIFF_FILES} more untracked files not shown")
    full_text = "\n".join(pieces).strip("\n") or "(no changes)"
    return summary + f"\n\n$ git diff HEAD (plus untracked files via --no-index)\n{truncate(full_text, limit)}"


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render(prefix: str, headline: str, meta: list[tuple[str, str]], sections: list[tuple[str, str]]) -> str:
    lines = [f"{prefix} {headline}"]
    lines.extend(f"{key}: {value}" for key, value in meta)
    lines.append("")
    for title, body in sections:
        lines.append(f"--- {title} ---")
        lines.append(body.strip("\n") if body and body.strip() else "(empty)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _outcome_meta(harness: Harness, outcome: Outcome) -> list[tuple[str, str]]:
    meta: list[tuple[str, str]] = []
    if outcome.conversation_id:
        meta.append(
            (
                "Conversation ID",
                f'{outcome.conversation_id}  (pass conversation_id="{outcome.conversation_id}" to '
                f"delegate_to_{harness.key} to continue this session with its full context)",
            )
        )
    else:
        meta.append(("Conversation ID", "(unavailable: output was not structured JSON; a fix round must restate the context)"))
    stats: list[str] = []
    if outcome.status:
        stats.append(f"status={outcome.status}")
    if outcome.turns is not None:
        stats.append(f"turns={outcome.turns}")
    if outcome.tokens_in is not None or outcome.tokens_out is not None:
        stats.append(f"tokens in/out={outcome.tokens_in or 0}/{outcome.tokens_out or 0}")
    if outcome.cost_usd is not None:
        stats.append(f"cost=${outcome.cost_usd:.4f}")
    if outcome.model:
        stats.append(f"model={outcome.model}")
    if stats:
        meta.append(("Harness stats", " | ".join(stats)))
    for note in outcome.notes:
        meta.append(("Note", note))
    return meta


def _response_section(outcome: Outcome, limit: int) -> tuple[str, str]:
    title = "harness response" if outcome.structured else "harness output (stdout, unstructured)"
    return title, truncate(outcome.text, limit)


def format_success(
    harness: Harness, result: ProcessResult, outcome: Outcome, cwd: Path, command: str, git_summary: str
) -> str:
    limit = max_output_chars()
    sections = [_response_section(outcome, limit)]
    if result.stderr.strip():
        sections.append(("stderr (non-fatal)", truncate(result.stderr, limit // 2)))
    sections.append(("working tree changes (git)", git_summary))
    return _render(
        "[SUCCESS]",
        f"{harness.label} task completed (exit 0) in {_fmt_duration(result.duration)}",
        [("Harness", harness.key), ("Working dir", str(cwd)), ("Command", command), *_outcome_meta(harness, outcome)],
        sections,
    )


def format_failure(
    harness: Harness, result: ProcessResult, outcome: Outcome, cwd: Path, command: str, git_summary: str
) -> str:
    limit = max_output_chars()
    code = result.returncode
    code_text = str(code)
    if code is not None and code < 0:
        code_text = f"{code} (killed by {_signal_name(-code)})"
    if code == 0 and outcome.harness_error:
        headline = (
            f"{harness.label} reported failure (status={outcome.status or 'error'}) despite exit 0, "
            f"after {_fmt_duration(result.duration)}"
        )
    else:
        headline = f"{harness.label} task exited with code {code_text} after {_fmt_duration(result.duration)}"
    diagnostics = extract_diagnostics(f"{outcome.text}\n{result.stdout if not outcome.structured else ''}\n{result.stderr}")
    sections = [_response_section(outcome, limit), ("stderr", truncate(result.stderr, limit))]
    if not outcome.structured and result.stdout.strip() and result.stdout.strip() != outcome.text.strip():
        sections.append(("stdout", truncate(result.stdout, limit)))
    sections += [
        ("detected diagnostics (stack traces / test failures / errors)", diagnostics or "(none matched)"),
        ("working tree state (git)", git_summary),
        (
            "ACTION REQUIRED (orchestrator)",
            f"{harness.label} did not complete this task. Read the diagnostics above and decide whether this is\n"
            f"  (a) an environment roadblock (auth, quota, missing tool) -> run check_{harness.key}_health, fix, retry\n"
            "      (or delegate the same brief to the other harness);\n"
            "  (b) an under-specified or contradictory task -> rewrite the brief with explicit files,\n"
            "      acceptance criteria and the exact test command, then re-delegate;\n"
            "  (c) a genuine code/test failure -> plan the fix and re-delegate a narrower follow-up that\n"
            "      quotes the failing test/trace, passing conversation_id so the harness keeps its context.\n"
            "Check the working tree state before retrying: partial edits may already be on disk.",
        ),
    ]
    return _render(
        "[ROADBLOCK / FAILURE]",
        headline,
        [
            ("Harness", harness.key),
            ("Working dir", str(cwd)),
            ("Command", command),
            ("Exit code", code_text),
            ("Probable cause", classify_failure(harness, code, f"{outcome.text}\n{result.stdout}", result.stderr)),
            *_outcome_meta(harness, outcome),
        ],
        sections,
    )


def format_timeout(
    harness: Harness, result: ProcessResult, cwd: Path, command: str, timeout_seconds: int, git_summary: str
) -> str:
    limit = max_output_chars()
    sections = [
        ("partial stdout (captured before termination)", truncate(result.stdout, limit)),
        ("partial stderr (captured before termination)", truncate(result.stderr, limit)),
        ("working tree state (git)", git_summary),
        (
            "ACTION REQUIRED (orchestrator)",
            f"The {harness.binary} process tree was terminated after {timeout_seconds}s (SIGTERM, then SIGKILL).\n"
            "Inspect the partial logs and the working tree: the task may be partially applied.\n"
            "Either split the work into smaller, independently verifiable tasks, or re-delegate with a\n"
            "larger timeout_seconds if the logs show steady progress.",
        ),
    ]
    return _render(
        "[TIMEOUT_ERROR]",
        f"{harness.label} task exceeded {timeout_seconds}s and was terminated "
        f"(ran {_fmt_duration(result.duration)} including shutdown)",
        [("Harness", harness.key), ("Working dir", str(cwd)), ("Command", command), ("Timeout", f"{timeout_seconds}s")],
        sections,
    )


# ---------------------------------------------------------------------------
# Tool implementations (shared across harnesses)
# ---------------------------------------------------------------------------


async def delegate(
    harness: Harness,
    prompt: str,
    working_dir: str,
    flags: list[str] | None,
    auto_approve: bool,
    timeout_seconds: int,
    conversation_id: str | None,
    include_diff: bool,
    progress: ProgressFn | None = None,
) -> str:
    cwd, error = validate_working_dir(working_dir)
    if error:
        return f"[INVALID_ARGUMENT] {error}"
    if not isinstance(prompt, str) or not prompt.strip():
        return "[INVALID_ARGUMENT] prompt must be a non-empty string."
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not (
        1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        return f"[INVALID_ARGUMENT] timeout_seconds must be an integer between 1 and {MAX_TIMEOUT_SECONDS}."
    flag_list = list(flags or [])
    if not all(isinstance(f, str) for f in flag_list):
        return "[INVALID_ARGUMENT] flags must be a list of strings."
    conversation_id = (conversation_id or "").strip() or None
    if conversation_id and not CONVERSATION_ID_RE.match(conversation_id):
        return "[INVALID_ARGUMENT] conversation_id must be an identifier (letters, digits, . _ : -) of at most 128 chars."

    depth, limit = bridge_depth(), max_depth()
    if depth >= limit:
        return _render(
            "[ROADBLOCK / FAILURE]",
            f"delegation refused: nesting depth {depth} has reached BRIDGE_MAX_DEPTH={limit}",
            [("Harness", harness.key), ("Working dir", str(cwd))],
            [
                (
                    "ACTION REQUIRED (orchestrator)",
                    "This process is itself a delegated harness. Do the work directly instead of delegating "
                    "further, or raise BRIDGE_MAX_DEPTH in the top-level bridge configuration.",
                )
            ],
        )

    binary = harness.resolve_binary()
    if not binary:
        return _render(
            "[ROADBLOCK / FAILURE]",
            f"{harness.label} not found",
            [
                ("Harness", harness.key),
                ("Looked for", f"{harness.env_prefix}_BIN={harness.binary_setting()}"),
                ("PATH", os.environ.get("PATH", "")),
            ],
            [
                (
                    "ACTION REQUIRED (orchestrator)",
                    f"Run check_{harness.key}_health. Install {harness.binary} (or set {harness.env_prefix}_BIN to its "
                    "absolute path in this MCP server's environment) before delegating again, or delegate to the "
                    "other harness.",
                )
            ],
        )

    try:
        argv, stdin_data = build_argv(harness, binary, prompt, flag_list, auto_approve, timeout_seconds, conversation_id)
    except ValueError as exc:
        return f"[INVALID_ARGUMENT] {exc}"

    command = _display_command(argv, prompt, stdin_data is not None)
    log.info("delegating to %s in %s (timeout %ss): %s", harness.key, cwd, timeout_seconds, command)
    try:
        result = await run_process_with_heartbeat(
            argv,
            cwd=str(cwd),
            timeout=timeout_seconds,
            stdin_data=stdin_data,
            env=harness.child_env(),
            progress=progress,
            label=f"{harness.key} delegation",
        )
    except OSError as exc:
        return _render(
            "[ROADBLOCK / FAILURE]",
            f"could not start {harness.binary}: {exc}",
            [("Harness", harness.key), ("Working dir", str(cwd)), ("Command", command)],
            [("ACTION REQUIRED (orchestrator)", f"Run check_{harness.key}_health and fix the environment before retrying.")],
        )

    git_summary = await collect_git_summary(cwd, include_diff=include_diff)
    if result.timed_out:
        log.warning("%s timed out after %ss in %s", harness.key, timeout_seconds, cwd)
        return format_timeout(harness, result, cwd, command, timeout_seconds, git_summary)
    outcome = parse_outcome(harness, result.stdout)
    if result.returncode == 0 and not outcome.harness_error:
        log.info("%s succeeded in %s (%s)", harness.key, cwd, _fmt_duration(result.duration))
        return format_success(harness, result, outcome, cwd, command, git_summary)
    log.warning("%s failed (exit %s, status %s) in %s", harness.key, result.returncode, outcome.status, cwd)
    return format_failure(harness, result, outcome, cwd, command, git_summary)


async def health(harness: Harness) -> str:
    requested = harness.binary_setting()
    binary = harness.resolve_binary()
    config_lines = [
        f"Server: {SERVER_NAME} {__version__} (python {sys.version.split()[0]}, pid {os.getpid()})",
        f"Binary setting: {harness.env_prefix}_BIN={requested}",
        f"Auto-approve flags: {shlex.join(harness.auto_approve_flags()) or '(none)'}",
        f"Default extra flags: {shlex.join(harness.default_flags()) or '(none)'}",
        f"Default timeout: {DEFAULT_TIMEOUT_SECONDS}s"
        + (f" ({harness.timeout_flag} is set to timeout + {PRINT_TIMEOUT_MARGIN}s)" if harness.timeout_flag else ""),
        f"Delegation depth: {bridge_depth()} of max {max_depth()}",
        f"Progress heartbeat: every {heartbeat_seconds():.0f}s"
        + (" (disabled)" if heartbeat_seconds() <= 0 else " (keeps long delegations under the client's tool-call timeout)"),
        f"Env hidden from the harness: {', '.join((*harness.strip_env, *extra_strip_env())) or '(none)'}",
        f"HOME: {os.environ.get('HOME', '(unset)')}",
    ]
    if not binary:
        return _render(
            "[HEALTH: UNAVAILABLE]",
            f"{harness.label} not found",
            [("Harness", harness.key), ("Looked for", requested), ("PATH", os.environ.get("PATH", "(unset)"))],
            [
                (
                    "fix",
                    f"Install {harness.binary} so it lands on PATH, or set {harness.env_prefix}_BIN to the absolute "
                    "binary path in the MCP server's environment (opencode.json -> mcp.<name>.environment).",
                ),
                ("bridge configuration", "\n".join(config_lines)),
            ],
        )

    home = str(Path.home()) if Path.home().is_dir() else os.getcwd()
    env = harness.child_env()
    problems: list[str] = []

    try:
        version = await run_process([binary, "--version"], cwd=home, timeout=HEALTH_PROBE_TIMEOUT, env=env, kill_grace=1)
    except OSError as exc:
        version = ProcessResult([binary, "--version"], 127, "", str(exc), 0.0)
    if version.timed_out:
        version_text = f"(timed out after {HEALTH_PROBE_TIMEOUT}s)"
        problems.append("version check timed out")
    elif version.returncode == 0 and version.stdout.strip():
        version_text = version.stdout.strip().splitlines()[0]
    else:
        version_text = f"(exit {version.returncode}: {(version.stderr or version.stdout).strip()[:300] or 'no output'})"
        problems.append(f"`{harness.binary} --version` failed")

    try:
        auth = await run_process(
            [binary, *harness.auth_probe], cwd=home, timeout=HEALTH_PROBE_TIMEOUT, env=env, kill_grace=1
        )
    except OSError as exc:
        auth = ProcessResult([binary, *harness.auth_probe], 127, "", str(exc), 0.0)
    extra_section = ""
    if auth.timed_out:
        auth_text = f"UNKNOWN: `{harness.binary} {' '.join(harness.auth_probe)}` timed out after {HEALTH_PROBE_TIMEOUT}s"
        problems.append("authentication probe timed out")
    else:
        ok, auth_text, extra_section = harness.parse_auth(auth)
        if not ok:
            problems.append("authentication probe failed")

    cred = Path(harness.credential_path).expanduser()
    if cred.is_file():
        age_h = (time.time() - cred.stat().st_mtime) / 3600
        cred_text = f"present ({cred}, last written {age_h:.1f}h ago)"
    else:
        cred_text = f"absent ({cred}); informational only, the probe above is authoritative"

    status = "[HEALTH: READY]" if not problems else "[HEALTH: DEGRADED]"
    headline = (
        f"{harness.label} is ready for delegation"
        if not problems
        else f"{harness.label} needs attention: " + "; ".join(problems)
    )
    sections = [("bridge configuration", "\n".join(config_lines))]
    if extra_section:
        title, _, body = extra_section.partition("\n")
        sections.append((title, body))
    return _render(
        status,
        headline,
        [
            ("Harness", harness.key),
            ("Binary", f"{binary} (resolved from {requested!r})"),
            ("Version", version_text),
            ("Authentication", auth_text),
            ("Credential file", cred_text),
        ],
        sections,
    )


# ---------------------------------------------------------------------------
# MCP server and per-harness tool registration
# ---------------------------------------------------------------------------

mcp = _ServerImpl(
    SERVER_NAME,
    instructions=(
        "Execution bridge to headless coding harnesses: Antigravity (agy) and Claude Code (claude). "
        "Call check_<harness>_health once before the first delegation. delegate_to_<harness> runs ONE "
        "headless task and returns a report starting with [SUCCESS], [ROADBLOCK / FAILURE], [TIMEOUT_ERROR] "
        "or [INVALID_ARGUMENT]; branch on that prefix. Every report carries a Conversation ID: pass it back as "
        "conversation_id to continue that session for review-fix rounds. Give the harness scoped, verifiable "
        "briefs: files, acceptance criteria, the exact test command. Review every changed file before accepting."
    ),
)

PromptArg = Annotated[
    str,
    Field(
        description=(
            "The brief: task instructions for the harness (implementation, refactor, or tests). Name the target "
            "files, the acceptance criteria and the exact test command to run and report on."
        )
    ),
]
WorkingDirArg = Annotated[
    str, Field(description="Absolute path of the target project. Must exist; the harness runs with it as its working directory.")
]
FlagsArg = Annotated[
    list[str] | None,
    Field(
        description=(
            "Extra CLI flags for the harness, e.g. ['--model', '<id>']. Prompt, resume and interactive flags are "
            "rejected here (use the prompt / conversation_id arguments). Default: none."
        )
    ),
]
AutoApproveArg = Annotated[
    bool,
    Field(
        description=(
            "Inject the headless auto-approval flag (--dangerously-skip-permissions by default) so the harness never "
            "blocks on permission prompts. Default: true."
        )
    ),
]
TimeoutArg = Annotated[
    int,
    Field(description="Hard wall-clock limit in seconds (1-86400). The whole harness process tree is killed on expiry. Default: 900."),
]
ConversationArg = Annotated[
    str | None,
    Field(
        description=(
            "Conversation ID from a previous report of this harness. Resumes that session with its full context, "
            "so a review-fix round only needs the findings and the recommended fix. Default: start a new session."
        )
    ),
]
IncludeDiffArg = Annotated[
    bool,
    Field(
        description=(
            "Append the full `git diff HEAD` plus untracked-file diffs (capped) to the report, so the changes can be "
            "reviewed without extra tool calls. Default: false (status + diffstat only)."
        )
    ),
]


def _register_harness(harness: Harness) -> tuple[Callable[..., Any], Callable[..., Any]]:
    async def delegate_tool(
        prompt: PromptArg,
        working_dir: WorkingDirArg,
        flags: FlagsArg = None,
        auto_approve: AutoApproveArg = True,
        timeout_seconds: TimeoutArg = DEFAULT_TIMEOUT_SECONDS,
        conversation_id: ConversationArg = None,
        include_diff: IncludeDiffArg = False,
        ctx: _Context = None,  # injected by the SDK; excluded from the tool's input schema
    ) -> str:
        progress: ProgressFn | None = None
        if ctx is not None:
            async def progress(value: float, total: float | None, message: str) -> None:
                await ctx.report_progress(value, total, message)

        return await delegate(
            harness, prompt, working_dir, flags, auto_approve, timeout_seconds, conversation_id, include_diff, progress
        )

    async def health_tool() -> str:
        return await health(harness)

    delegate_tool.__name__ = f"delegate_to_{harness.key}"
    delegate_tool.__qualname__ = delegate_tool.__name__
    health_tool.__name__ = f"check_{harness.key}_health"
    health_tool.__qualname__ = health_tool.__name__
    mcp.tool(
        name=delegate_tool.__name__,
        description=(
            f"Execute an implementation, refactoring or testing task headlessly via {harness.label}. Returns a "
            "structured report prefixed with [SUCCESS] (response + git change summary), [ROADBLOCK / FAILURE] "
            "(exit code, stderr, extracted diagnostics, call to action), [TIMEOUT_ERROR] (partial logs after the "
            "process tree was killed) or [INVALID_ARGUMENT]. The report's Conversation ID can be passed back as "
            "conversation_id to continue the same session for a review-fix round."
        ),
    )(delegate_tool)
    mcp.tool(
        name=health_tool.__name__,
        description=(
            f"Pre-flight check for {harness.label}: binary discovery, version and authentication probe. Returns a "
            "report prefixed with [HEALTH: READY], [HEALTH: DEGRADED] or [HEALTH: UNAVAILABLE]."
        ),
    )(health_tool)
    return delegate_tool, health_tool


def enabled_harness_keys() -> list[str]:
    """Harnesses whose tools are exposed: INTERCOM_HARNESSES=antigravity,claude_code (default all)."""
    raw = _env("INTERCOM_HARNESSES")
    if not raw:
        return list(HARNESSES)
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = [k for k in keys if k not in HARNESSES]
    if unknown:
        log.warning("INTERCOM_HARNESSES names unknown harness(es) %s; known: %s", unknown, list(HARNESSES))
    chosen = [k for k in keys if k in HARNESSES]
    return chosen or list(HARNESSES)


REGISTERED_TOOLS = {key: _register_harness(HARNESSES[key]) for key in enabled_harness_keys()}
delegate_to_antigravity, check_antigravity_health = REGISTERED_TOOLS.get("antigravity", (None, None))
delegate_to_claude_code, check_claude_code_health = REGISTERED_TOOLS.get("claude_code", (None, None))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    level_name = _env("BRIDGE_LOG_LEVEL", "AGY_BRIDGE_LOG_LEVEL", default="INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,  # stdout is the MCP transport; never print to it
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        force=True,  # mcp 1.x FastMCP installs its own handler at construction time
    )
    found = {key: HARNESSES[key].resolve_binary() or "NOT FOUND" for key in REGISTERED_TOOLS}
    log.info("%s %s starting on stdio (depth %s): %s", SERVER_NAME, __version__, bridge_depth(), found)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
