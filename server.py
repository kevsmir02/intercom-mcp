#!/usr/bin/env python3
"""
intercom: a lightweight MCP server (stdio) that lets an orchestrator such as
OpenCode or Claude Code delegate implementation, refactoring and testing tasks to a
headless coding harness and get a structured, reviewable report back.

Supported harnesses
-------------------
antigravity   Google Antigravity CLI `agy`      delegate_to_antigravity, consult_antigravity, check_antigravity_health
claude_code   Anthropic Claude Code `claude`    delegate_to_claude_code,  consult_claude_code,  check_claude_code_health
opencode      OpenCode `opencode`               delegate_to_opencode,     consult_opencode,     check_opencode_health
pi            pi coding agent `pi`              delegate_to_pi,           consult_pi,           check_pi_health

Shared tools: list_runs, get_run (the run journal; every delegation is recorded).

Each harness runs in its JSON print mode, so every report carries the harness's
conversation/session ID. Passing that ID back as `conversation_id` resumes the same
session with its full context: the review -> fix loop costs one short prompt per round.

Change attribution
------------------
The working tree is snapshotted (HEAD + porcelain status) before the harness starts, so
the report separates what THIS delegation changed from what was already dirty, and notices
when the harness commits instead of leaving the work in the tree.

Result prefixes (branch on these)
---------------------------------
[SUCCESS]              exit 0 and the harness reported success; response + attributed change summary
[ROADBLOCK / FAILURE]  non-zero exit, harness-reported error, or a busy working tree
[TIMEOUT_ERROR]        process tree killed after timeout_seconds; partial logs attached
[INVALID_ARGUMENT]     bad input (missing working_dir, interactive flags, malformed conversation_id)
[HEALTH: READY|DEGRADED|UNAVAILABLE]  from the check_*_health tools

Configuration (environment variables, all optional)
---------------------------------------------------
Shared
  INTERCOM_HARNESSES         comma-separated harness keys to expose            (default: all)
  INTERCOM_STATE_DIR         run journal location            (default $XDG_STATE_HOME/intercom)
  BRIDGE_ALLOWED_DIRS        os.pathsep-separated roots delegation is confined to (default: anywhere)
  BRIDGE_MAX_OUTPUT_CHARS    per-stream cap in tool results                    (default 60000)
  BRIDGE_KILL_GRACE_SECONDS  SIGTERM -> SIGKILL escalation delay               (default 5)
  BRIDGE_HEARTBEAT_SECONDS   progress-notification interval during a run       (default 15; 0 disables)
  BRIDGE_MAX_DEPTH           delegation depth allowed below this server        (default 1)
  BRIDGE_STRIP_ENV           extra comma-separated env names hidden from every harness
  BRIDGE_REDACT_ENV          regex for env names whose values are masked in reports
  BRIDGE_KEEP_RUNS           run records kept on disk                          (default 200)
  BRIDGE_LOG_LEVEL           DEBUG | INFO | WARNING | ERROR                    (default INFO)
  BRIDGE_DEPTH               set automatically on children; do not set by hand
Per harness (prefix AGY_ antigravity, CLAUDE_ claude_code, OPENCODE_ opencode, PI_ pi)
  <PREFIX>_BIN                binary name or absolute path
  <PREFIX>_AUTO_APPROVE_FLAGS flags injected when auto_approve=true   (default --dangerously-skip-permissions)
  <PREFIX>_DEFAULT_FLAGS      flags appended to every delegation, e.g. "--model gemini-3.1-pro-high"

Verified against agy 1.1.25, Claude Code 2.1.259, opencode 1.18.x and pi 0.84.x (see docs/reference.md).
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
import uuid
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

__version__ = "0.1.0"  # 0.x: the tool surface and the harness adapters are still moving
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
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
# A single argv entry cannot exceed MAX_ARG_STRLEN (128 KiB on Linux); stay well under it.
ARGV_PROMPT_MAX_BYTES = 120_000
DEFAULT_RUNS_KEPT = 200
ACTIVITY_LINE_MAX = 120

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


def stream_progress() -> bool:
    """Ask agy/claude for an event stream so the heartbeat can show live activity.

    Their single-object JSON mode prints nothing until the run ends, which makes a long
    delegation a black box. Set BRIDGE_STREAM_PROGRESS=0 to fall back to it.
    """
    return _env("BRIDGE_STREAM_PROGRESS", default="1").lower() not in ("0", "false", "no", "off")


def keep_runs() -> int:
    return _env_int("BRIDGE_KEEP_RUNS", DEFAULT_RUNS_KEPT, 0)


def state_dir() -> Path:
    """Where run records live. XDG_STATE_HOME, or ~/.local/state, unless overridden."""
    override = _env("INTERCOM_STATE_DIR")
    if override:
        return Path(os.path.expanduser(override))
    base = _env("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(os.path.expanduser(base)) / SERVER_NAME


def allowed_dirs() -> list[Path]:
    """Roots that `working_dir` must sit inside. Empty means anywhere (the default)."""
    raw = _env("BRIDGE_ALLOWED_DIRS")
    roots: list[Path] = []
    for part in re.split(r"[%s,]" % re.escape(os.pathsep), raw):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(os.path.expanduser(part)).resolve())
        except OSError:  # pragma: no cover - unresolvable root
            log.warning("ignoring unresolvable BRIDGE_ALLOWED_DIRS entry %r", part)
    return roots


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def clean_text(text: str) -> str:
    """Strip ANSI escapes and normalise line endings."""
    text = _ANSI_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


_SECRET_NAME_RE_DEFAULT = r"(?:API_?KEY|_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)"
_MIN_SECRET_LEN = 8


def secret_values() -> list[tuple[str, str]]:
    """(name, value) for environment variables that look like credentials.

    Reports quote the harness's stdout/stderr and the command line verbatim; a harness that
    echoes its environment (or a flag carrying a key) would otherwise leak it to the caller
    and into the run journal.
    """
    pattern = re.compile(_env("BRIDGE_REDACT_ENV", default=_SECRET_NAME_RE_DEFAULT), re.IGNORECASE)
    found: list[tuple[str, str]] = []
    for name, value in os.environ.items():
        if value and len(value) >= _MIN_SECRET_LEN and pattern.search(name):
            found.append((name, value))
    # Longest first so a value that contains another is masked whole.
    return sorted(found, key=lambda item: len(item[1]), reverse=True)


def redact(text: str) -> str:
    for name, value in secret_values():
        if value in text:
            text = text.replace(value, f"[redacted:${name}]")
    return text


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
        r"\b(?!0\b)\d+ (?:failed|failing|errors?)\b",  # "0 failed" is a pass, not a diagnostic
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


_VERSION_RE = re.compile(r"\b(\d+(?:\.\d+)+)")


def _version_number(text: str) -> str:
    """First dotted version in a `--version` line ("2.1.259 (Claude Code)" -> "2.1.259")."""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else ""


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


def _children_from_proc() -> dict[int, list[int]] | None:
    """parent pid -> child pids, read from /proc (Linux)."""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return None
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
    return children


def _children_from_ps() -> dict[int, list[int]] | None:
    """parent pid -> child pids, via `ps` (macOS and other systems without /proc)."""
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return children


def descendant_pids(root: int) -> list[int]:
    """Every descendant of `root`, so a timeout kills the whole harness tree.

    /proc where it exists, `ps` otherwise: without this a grandchild that left the process
    group (a test runner, a language server) survives the kill and holds the pipes open.
    """
    children = _children_from_proc()
    if children is None:
        children = _children_from_ps()
    if not children:
        return []
    found: list[int] = []
    stack = [root]
    seen = {root}
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child in seen:  # defensive: a pid table read mid-change must not loop
                continue
            seen.add(child)
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


class _ActivityTracker:
    """Turns a harness's live output into one short line for progress notifications.

    Harnesses stream NDJSON events (opencode, pi) or prose (agy, claude in text mode); either
    way the newest meaningful line tells the caller the run is alive and roughly where it is.
    """

    __slots__ = ("_partial", "_latest", "_last_tool", "lines")

    def __init__(self) -> None:
        self._partial = b""
        self._latest = ""
        self._last_tool = ""
        self.lines = 0

    def feed(self, chunk: bytes) -> None:
        data = self._partial + chunk
        *complete, self._partial = data.split(b"\n")
        if len(self._partial) > 1_000_000:  # a single unterminated line: do not grow forever
            self._partial = self._partial[-65_536:]
        for raw in complete:
            line = clean_text(raw.decode("utf-8", errors="replace")).strip()
            if not line:
                continue
            self.lines += 1
            summary = self._summarise(line)
            if summary:
                self._latest = summary

    def _summarise(self, line: str) -> str:
        if line.startswith("{") and len(line) < 200_000:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict):
                return self._describe_event(event)
        return line[:ACTIVITY_LINE_MAX]

    # Events that say nothing about progress and would crowd out the ones that do.
    _NOISE = {("system", "hook_started"), ("system", "hook_response"), ("system", "informational")}

    @staticmethod
    def _tool_name(event: dict[str, Any], message: dict[str, Any]) -> str:
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        for candidate in (part.get("tool"), event.get("tool"), event.get("name")):
            if isinstance(candidate, str) and candidate:
                return candidate
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                    return str(block["name"])
        return ""

    def _describe_event(self, event: dict[str, Any]) -> str:
        """A short phrase for what the harness is doing, across the four event dialects.

        Returns "" for events that carry no progress signal, so the heartbeat keeps showing
        the last thing that did.
        """
        # agy: {"event": "step_update", "step_update": {...}}
        kind = event.get("event")
        if isinstance(kind, str):
            payload = event.get(kind) if isinstance(event.get(kind), dict) else {}
            tool = payload.get("tool") or payload.get("tool_name")
            if isinstance(tool, str) and tool:
                self._last_tool = tool
                return f"running {tool}"[:ACTIVITY_LINE_MAX]
            bits = [str(payload[k]) for k in ("step_type", "state") if payload.get(k)]
            return " ".join([kind, *bits])[:ACTIVITY_LINE_MAX]

        # claude / opencode / pi: {"type": ..., ...}
        kind = str(event.get("type") or "event")
        subtype = str(event.get("subtype") or "")
        if kind == "rate_limit_event" or (kind, subtype) in self._NOISE:
            return ""
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        tool = self._tool_name(event, message)
        if tool:
            self._last_tool = tool
            return f"running {tool}"[:ACTIVITY_LINE_MAX]
        content = message.get("content")
        if kind == "user" and isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return f"{self._last_tool} finished" if self._last_tool else "tool finished"
        if kind in ("assistant", "text"):
            return "writing"
        if kind == "system" and subtype == "init":
            return "starting"
        if kind == "result":
            return "finishing"
        return f"{kind} {subtype}".strip()[:ACTIVITY_LINE_MAX]

    def describe(self) -> str:
        return redact(self._latest)


async def _pump(
    stream: asyncio.StreamReader | None, buf: _BoundedBuffer, activity: "_ActivityTracker | None" = None
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return
        buf.write(chunk)
        if activity is not None:
            activity.feed(chunk)


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
    activity: "_ActivityTracker | None" = None,
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
        asyncio.ensure_future(_pump(proc.stdout, out_buf, activity)),
        asyncio.ensure_future(_pump(proc.stderr, err_buf, activity)),
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
    activity = _ActivityTracker()

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
            # The newest output line turns "still running" into "still running, and here is what on".
            latest = activity.describe()
            message = f"{label}: {int(elapsed)}s of {int(timeout)}s budget"
            message += f" | {latest}" if latest else " | no output yet"
            try:
                await progress(float(ticks), None, message)
            except Exception:
                return

    task = asyncio.ensure_future(beat())
    try:
        return await run_process(
            argv, cwd=cwd, timeout=timeout, stdin_data=stdin_data, env=env, activity=activity
        )
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
    # Prompt placement. prompt_style "value": the flag carries the prompt (agy `-p <prompt>`);
    # "positional": the prompt is a positional argument, optionally preceded by print_flag.
    prompt_style: str
    print_flag: str | None  # boolean/carrier flag before the prompt ("-p"); None for opencode
    prompt_last: bool  # place the positional prompt after all flags (opencode/pi)
    end_of_options: bool  # guard a positional prompt with `--` (pi)
    subcommand: tuple[str, ...]  # inserted after the binary (opencode: ("run",))
    stdin_prompt: bool  # can the prompt be piped instead of passed on the command line?
    readonly_flags: tuple[str, ...]  # put the harness in a plan/no-edit mode (consult_* tools)
    verified_version: str  # the release this adapter's flags were checked against
    json_flags: tuple[str, ...]
    # Flags for the harness's event-stream mode, when it has one that differs from json_flags.
    stream_flags: tuple[str, ...]
    output_format_flag: str
    timeout_flag: str | None
    resume_flag: str
    interactive_flags: tuple[str, ...]
    prompt_flags: tuple[str, ...]
    auth_probe: tuple[str, ...]
    credential_path: str
    strip_env: tuple[str, ...]
    usage_error_re: str
    parse_auth: Callable[[ProcessResult], tuple[bool, str, str]]
    parse_json: Callable[[dict[str, Any]], Outcome] | None = None  # single-object JSON (agy/claude)
    parse_stream: Callable[[str], Outcome | None] | None = None  # NDJSON event stream (opencode/pi)

    def binary_setting(self) -> str:
        return _env(f"{self.env_prefix}_BIN", default=self.binary)

    def resolve_binary(self) -> str | None:
        return shutil.which(os.path.expanduser(self.binary_setting()))

    def auto_approve_flags(self) -> list[str]:
        return _env_flags(f"{self.env_prefix}_AUTO_APPROVE_FLAGS", self.default_auto_approve)

    def default_flags(self) -> list[str]:
        return _env_flags(f"{self.env_prefix}_DEFAULT_FLAGS", "")

    def output_flags(self) -> tuple[str, ...]:
        """The structured-output flags to use: the streaming pair when it exists and is wanted."""
        if self.stream_flags and stream_progress():
            return self.stream_flags
        return self.json_flags

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


def _parse_claude_stream(stdout: str) -> Outcome | None:
    """claude --output-format stream-json: NDJSON ending in the same object `json` mode prints."""
    final: dict[str, Any] | None = None
    for event in _iter_json_lines(stdout):
        if event.get("type") == "result":
            final = event
    return _parse_claude_json(final) if final is not None else None


def _parse_agy_stream(stdout: str) -> Outcome | None:
    """agy --output-format stream-json: events shaped {"event": <name>, <name>: {...}}.

    The terminal result payload is byte-for-byte what `--output-format json` prints.
    """
    final: dict[str, Any] | None = None
    for event in _iter_json_lines(stdout):
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            final = event["result"]
    return _parse_agy_json(final) if final is not None else None


def _auth_agy(result: ProcessResult) -> tuple[bool, str, str]:
    # agy has no `auth` subcommand; `agy models` queries the authenticated backend.
    model_lines = [ln for ln in result.stdout.splitlines() if "\t" in ln]
    if result.returncode == 0 and model_lines:
        return (
            True,
            f"authenticated ({len(model_lines)} models available via `agy models`)",
            "available models (informational; this harness uses its own configured default unless the user "
            "explicitly asks for a specific model)\n" + "\n".join(model_lines[:25]),
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


def _iter_json_lines(stdout: str):
    """Yield each JSON object from an NDJSON stream, tolerating blank/non-JSON lines."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("message", "data", "error", "name"):
            if isinstance(value.get(key), str):
                return value[key]
    return json.dumps(value)[:300]


def _pick(usage: dict[str, Any], *names: str) -> int | None:
    for name in names:
        if name in usage:
            got = _as_int(usage[name])
            if got is not None:
                return got
    return None


def _content_text(content: Any) -> str:
    """Extract text from an assistant message `content` field (string or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "output_text", None):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return ""


def _opencode_error_text(err: Any) -> str:
    """Match opencode's own error rendering: prefer error.data.message, then error.name."""
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return data["message"]
        if isinstance(err.get("name"), str):
            return err["name"]
    return _stringify(err)


def _parse_opencode_stream(stdout: str) -> Outcome | None:
    """Parse opencode's `run --format json` NDJSON: {type, timestamp, sessionID, ...}."""
    session_id: str | None = None
    texts: list[str] = []
    error: str | None = None
    seen = False
    for ev in _iter_json_lines(stdout):
        seen = True
        if ev.get("sessionID"):
            session_id = str(ev["sessionID"])
        etype = ev.get("type")
        if etype == "text":
            part = ev.get("part") or {}
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        elif etype == "error":
            error = _opencode_error_text(ev.get("error"))
    if not seen and session_id is None:
        return None
    return Outcome(
        text="\n".join(texts) or "(no text output)",
        structured=True,
        conversation_id=session_id,
        status="error" if error else "success",
        harness_error=bool(error),
        notes=[error] if error else [],
    )


def _parse_pi_stream(stdout: str) -> Outcome | None:
    """Parse pi's `--mode json` NDJSON: a `session` header then agent/turn/message events."""
    session_id: str | None = None
    final: str | None = None
    usage: dict[str, Any] = {}
    seen = False
    for ev in _iter_json_lines(stdout):
        seen = True
        etype = ev.get("type")
        if etype == "session" and ev.get("id"):
            session_id = str(ev["id"])
        elif etype == "message_update" and isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        elif etype == "message_end":
            msg = ev.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = _content_text(msg.get("content"))
                if text:
                    final = text
        elif etype == "agent_end":
            for msg in reversed(ev.get("messages") or []):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    text = _content_text(msg.get("content"))
                    if text:
                        final = final or text
                    break
    if not seen and session_id is None:
        return None
    return Outcome(
        text=final or "(no text output)",
        structured=True,
        conversation_id=session_id,
        status="success",
        harness_error=None,
        tokens_in=_pick(usage, "input_tokens", "inputTokens", "promptTokens", "input"),
        tokens_out=_pick(usage, "output_tokens", "outputTokens", "completionTokens", "output"),
    )


def _auth_opencode(result: ProcessResult) -> tuple[bool, str, str]:
    # `opencode auth list` prints one bullet (●) per configured credential.
    count = result.stdout.count("\u25cf")
    if result.returncode == 0 and count:
        return True, f"authenticated ({count} provider credential(s) via `opencode auth list`)", ""
    detail = (result.stderr or result.stdout).strip()[:400] or "no output"
    return False, f"NOT authenticated or probe failed (exit {result.returncode}): {detail}", ""


def _auth_pi(result: ProcessResult) -> tuple[bool, str, str]:
    data = _extract_json_object(result.stdout) or {}
    status = str(data.get("status") or "")
    if result.returncode == 0 and status == "ready":
        return True, f"authenticated (provider={data.get('provider', 'google')} ready via `pi auth check`)", ""
    if status:
        return (
            False,
            f"provider={data.get('provider', 'google')} {status} ({data.get('reason', 'unknown')}); "
            "pi checks one provider (google by default) — another configured provider may still work",
            "",
        )
    detail = (result.stderr or result.stdout).strip()[:400] or "no output"
    return False, f"probe failed (exit {result.returncode}): {detail}", ""


HARNESSES: dict[str, Harness] = {
    "antigravity": Harness(
        key="antigravity",
        stream_flags=("--output-format", "stream-json"),
        # Verified 2026-09-04: `-p` is a *value* flag; `-p ""` with a piped prompt returns
        # "Error: empty prompt", so agy cannot take the brief on stdin.
        stdin_prompt=False,
        readonly_flags=("--mode", "plan"),
        verified_version="1.1.25",
        label="Antigravity CLI (agy)",
        binary="agy",
        env_prefix="AGY",
        default_auto_approve="--dangerously-skip-permissions",
        prompt_style="value",  # agy: -p carries the prompt
        print_flag="-p",
        prompt_last=False,
        end_of_options=False,
        subcommand=(),
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
        parse_auth=_auth_agy,
        parse_json=_parse_agy_json,
        parse_stream=_parse_agy_stream,
    ),
    "claude_code": Harness(
        key="claude_code",
        # Verified 2026-09-04: print mode refuses stream-json without --verbose.
        stream_flags=("--output-format", "stream-json", "--verbose"),
        stdin_prompt=True,  # `-p` is boolean; the prompt may be a positional or piped
        readonly_flags=("--permission-mode", "plan"),
        verified_version="2.1.259",
        label="Claude Code (claude)",
        binary="claude",
        env_prefix="CLAUDE",
        default_auto_approve="--dangerously-skip-permissions",
        prompt_style="positional",  # claude: -p is boolean, the prompt is positional
        print_flag="-p",
        prompt_last=False,
        end_of_options=False,
        subcommand=(),
        json_flags=("--output-format", "json"),
        output_format_flag="--output-format",
        timeout_flag=None,
        resume_flag="--resume",
        interactive_flags=(),
        prompt_flags=("-p", "--print"),
        auth_probe=("auth", "status", "--json"),
        credential_path="~/.claude/.credentials.json",
        strip_env=(
            "CLAUDECODE",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_ENTRYPOINT",
        ),
        usage_error_re=r"error: unknown option|error: missing required argument|error: option '.*' argument missing|error: too many arguments",
        parse_auth=_auth_claude,
        parse_json=_parse_claude_json,
        parse_stream=_parse_claude_stream,
    ),
    "opencode": Harness(
        key="opencode",
        stream_flags=(),  # `--format json` is already a newline-delimited event stream
        stdin_prompt=True,  # `opencode run` with no message argument reads stdin
        readonly_flags=("--agent", "plan"),  # the built-in read-only agent
        verified_version="1.18",
        label="OpenCode (opencode)",
        binary="opencode",
        env_prefix="OPENCODE",
        default_auto_approve="--auto",  # auto-approve permissions not explicitly denied
        prompt_style="positional",
        print_flag=None,  # `opencode run <prompt>`: prompt is a bare positional
        prompt_last=True,
        end_of_options=False,
        subcommand=("run",),
        json_flags=("--format", "json"),
        output_format_flag="--format",
        timeout_flag=None,
        resume_flag="--session",  # resume a session by id
        interactive_flags=("-i", "--interactive", "--mini"),
        prompt_flags=("--prompt",),
        auth_probe=("auth", "list"),
        credential_path="~/.local/share/opencode/auth.json",
        strip_env=(),
        usage_error_re=r"Unknown argument|Not enough non-option arguments|Missing required argument|Unknown command",
        parse_auth=_auth_opencode,
        parse_stream=_parse_opencode_stream,
    ),
    "pi": Harness(
        key="pi",
        stream_flags=(),  # `--mode json` is already a JSON-lines event stream
        stdin_prompt=True,  # `pi -p` reads the prompt from stdin when no positional is given
        readonly_flags=("--exclude-tools", "edit,write"),
        verified_version="0.84",
        label="pi (pi-coding-agent)",
        binary="pi",
        env_prefix="PI",
        default_auto_approve="--approve",  # trust project-local resources for this headless run
        prompt_style="positional",
        print_flag="-p",  # --print: non-interactive
        prompt_last=True,
        end_of_options=True,  # guard a dash-leading prompt with `--`
        subcommand=(),
        json_flags=("--mode", "json"),
        output_format_flag="--mode",
        timeout_flag=None,
        resume_flag="--session",  # resume by (partial) session UUID
        interactive_flags=(),
        prompt_flags=("-p", "--print"),
        auth_probe=("auth", "check", "--provider", "google", "--json"),
        credential_path="~/.pi/agent/auth.json",
        strip_env=(),
        usage_error_re=r"[Uu]nknown option|[Uu]nknown flag|unexpected argument|[Ii]nvalid option",
        parse_auth=_auth_pi,
        parse_stream=_parse_pi_stream,
    ),
}


def parse_outcome(harness: Harness, stdout: str) -> Outcome:
    """Event stream first, then a single result object, then the raw text.

    Both are tried for every harness, so a report is still structured when streaming is
    turned off (BRIDGE_STREAM_PROGRESS=0) or when a harness answers in the other shape.
    """
    if harness.parse_stream is not None:
        try:
            outcome = harness.parse_stream(stdout)
            if outcome is not None:
                return outcome
        except Exception as exc:  # defensive: never let a schema surprise hide the raw output
            log.warning("could not parse %s event stream (%s); trying the object parser", harness.key, exc)
    if harness.parse_json is not None:
        data = _extract_json_object(stdout)
        if data is not None:
            try:
                return harness.parse_json(data)
            except Exception as exc:
                log.warning("could not parse %s JSON output (%s); falling back to raw text", harness.key, exc)
    return Outcome(text=stdout, structured=False)


def classify_failure(harness: Harness, returncode: int | None, stdout: str, stderr: str) -> str:
    blob = f"{stdout}\n{stderr}".lower()
    if re.search(harness.usage_error_re, stderr.lower()):
        return f"CLI usage error: check the `flags` argument against `{harness.binary} --help`."
    if re.search(r"quota|rate.?limit|too many requests|\b429\b|usage limit", blob):
        return (
            f"{harness.label} quota or rate limit reached: wait, reduce task size, pick another model, "
            f"or delegate the same brief to another harness; run check_{harness.key}_health."
        )
    if re.search(r"unauthenticated|unauthori[sz]ed|not logged in|login required|token expired|\b401\b|\b403\b", blob):
        return f"Authentication problem: run check_{harness.key}_health and re-authenticate {harness.binary}."
    if re.search(
        r"traceback \(most recent call last\)|assertionerror|(?<!0 )\bfailed\b|(?<!\b0 )\d+ failing|"
        r"error\[e\d+\]|^\s*panic:",
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
    resolved = path.resolve()
    roots = allowed_dirs()
    if roots and not any(resolved == root or root in resolved.parents for root in roots):
        return None, (
            f"working_dir {resolved} is outside BRIDGE_ALLOWED_DIRS "
            f"({', '.join(str(r) for r in roots)}); delegation is confined to those roots."
        )
    return resolved, None


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
    read_only: bool = False,
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

    argv = [binary, *harness.subcommand]
    encoded = prompt.encode("utf-8")
    dash = prompt.startswith("-")
    # A prompt that cannot go on the command line goes on stdin: too large, or dash-leading
    # where we cannot guard it with `--` (positional harnesses that read stdin).
    needs_stdin = len(encoded) > PROMPT_ARG_MAX_BYTES or (
        harness.prompt_style == "positional" and dash and not harness.end_of_options
    )
    via_stdin = needs_stdin and harness.stdin_prompt
    if needs_stdin and not harness.stdin_prompt:
        # Never build a command with no prompt in it: that used to hang until timeout_seconds.
        if len(encoded) > ARGV_PROMPT_MAX_BYTES:
            raise ValueError(
                f"prompt is {len(encoded)} bytes; {harness.binary} only accepts it on the command line "
                f"(max {ARGV_PROMPT_MAX_BYTES}). Shorten the brief -- point the harness at files to read "
                f"instead of pasting their contents -- or delegate to a harness that reads stdin."
            )
        if dash and harness.prompt_style == "positional":  # pragma: no cover - no such harness today
            raise ValueError(f"a prompt starting with '-' cannot be passed to {harness.binary}; rephrase it.")
    stdin_data: bytes | None = encoded if via_stdin else None

    if harness.prompt_style == "value":
        if not via_stdin:
            argv += [harness.print_flag or "-p", prompt]
    else:  # positional
        if harness.print_flag:
            argv.append(harness.print_flag)
        if not harness.prompt_last and not via_stdin:
            argv.append(prompt)

    if conversation_id:
        argv += [harness.resume_flag, conversation_id]
    if auto_approve and approve_flags and not _has_flag(user_flags, approve_flags[0]):
        argv += approve_flags
    if read_only and harness.readonly_flags and not _has_flag(user_flags, harness.readonly_flags[0]):
        argv += list(harness.readonly_flags)
    output_flags = harness.output_flags()
    if output_flags and not _has_flag(user_flags, harness.output_format_flag):
        argv += output_flags
    if harness.timeout_flag and not _has_flag(user_flags, harness.timeout_flag):
        argv += [harness.timeout_flag, f"{timeout_seconds + PRINT_TIMEOUT_MARGIN}s"]
    argv += user_flags

    if harness.prompt_last and not via_stdin:
        if harness.end_of_options:
            argv.append("--")
        argv.append(prompt)
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
# Working-tree snapshots and attributed change summaries
# ---------------------------------------------------------------------------


async def _git(git: str, args: list[str], cwd: Path) -> ProcessResult:
    return await run_process([git, *args], cwd=str(cwd), timeout=GIT_PROBE_TIMEOUT, kill_grace=1)


@dataclass
class GitSnapshot:
    """The state of a working tree at one instant, used to attribute later changes."""

    git: str | None = None
    is_repo: bool = False
    head: str | None = None
    # path -> (porcelain status code, mtime_ns, size); mtime/size are 0 when the file is gone.
    entries: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    note: str = ""

    def paths(self) -> set[str]:
        return set(self.entries)


def _parse_porcelain_z(payload: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain -z` into [(code, path)], resolving rename sources."""
    records = [r for r in payload.split("\0") if r]
    out: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        if code[0] in ("R", "C"):
            index += 1  # the following record is the rename/copy source; not a change of its own
        out.append((code, path))
    return out


def _stat_of(cwd: Path, rel: str) -> tuple[int, int]:
    try:
        st = (cwd / rel).stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


async def snapshot_git(cwd: Path) -> GitSnapshot:
    """Record HEAD and every dirty path (with mtime/size) before a harness runs."""
    git = shutil.which("git")
    if not git:
        return GitSnapshot(note="(git not installed; no change summary available)")
    try:
        probe = await _git(git, ["rev-parse", "--is-inside-work-tree"], cwd)
    except OSError as exc:
        return GitSnapshot(git=git, note=f"(git probe failed: {exc})")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return GitSnapshot(git=git, note="(not a git repository; no change summary available)")

    head_result = await _git(git, ["rev-parse", "HEAD"], cwd)
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    status = await _git(git, ["status", "--porcelain", "-z"], cwd)
    entries = {
        path: (code, *_stat_of(cwd, path)) for code, path in _parse_porcelain_z(status.stdout)
    }
    return GitSnapshot(git=git, is_repo=True, head=head, entries=entries)


@dataclass
class ChangeSet:
    """What a delegation did to a working tree, separated from what was already there."""

    attributed: dict[str, str] = field(default_factory=dict)  # path -> status code
    pre_existing: dict[str, str] = field(default_factory=dict)
    reverted: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    head_moved: bool = False

    def touched(self) -> list[str]:
        return sorted(self.attributed)


async def diff_snapshots(cwd: Path, before: GitSnapshot) -> tuple[ChangeSet, GitSnapshot]:
    """Compare the tree against `before`, attributing each dirty path to the run or to the past."""
    after = await snapshot_git(cwd)
    changes = ChangeSet()
    if not (before.is_repo and after.is_repo and after.git):
        return changes, after

    for path, (code, mtime, size) in after.entries.items():
        old = before.entries.get(path)
        if old is None or old[0] != code or (old[1], old[2]) != (mtime, size):
            changes.attributed[path] = code
        else:
            changes.pre_existing[path] = code
    changes.reverted = sorted(before.paths() - after.paths())

    if before.head and after.head and before.head != after.head:
        changes.head_moved = True
        changes.reverted = []  # the commit below explains why those paths are no longer dirty
        log_result = await _git(after.git, ["log", "--oneline", f"{before.head}..{after.head}"], cwd)
        changes.commits = [ln for ln in log_result.stdout.splitlines() if ln.strip()]
        named = await _git(
            after.git, ["diff", "--name-only", f"{before.head}..{after.head}"], cwd
        )
        for path in named.stdout.splitlines():
            changes.attributed.setdefault(path.strip(), "committed")
    return changes, after


async def _untracked_diffs(git: str, cwd: Path, paths: list[str]) -> list[str]:
    """Diff new files concurrently: one `git diff --no-index` each, bounded."""
    gate = asyncio.Semaphore(8)

    async def one(path: str) -> str:
        async with gate:
            result = await _git(git, ["diff", "--no-index", "--", os.devnull, path], cwd)
        return result.stdout.rstrip() if result.stdout.strip() else ""

    return [text for text in await asyncio.gather(*(one(p) for p in paths)) if text]


async def summarize_changes(
    cwd: Path, before: GitSnapshot, changes: ChangeSet, after: GitSnapshot, include_diff: bool
) -> str:
    """Render the change summary, keeping the delegation's work separate from pre-existing edits."""
    if not after.is_repo:
        return after.note or before.note or "(no change summary available)"
    git = after.git
    assert git is not None  # is_repo implies a resolved binary
    limit = max_output_chars()

    sections: list[str] = []
    if changes.attributed:
        listing = "\n".join(f"{code} {path}" for path, code in sorted(changes.attributed.items()))
        sections.append(f"changed by this delegation ({len(changes.attributed)} path(s)):\n{truncate(listing, limit // 4)}")
    else:
        sections.append("changed by this delegation: (nothing -- the working tree is unchanged since the run started)")
    if changes.pre_existing:
        listing = "\n".join(f"{code} {path}" for path, code in sorted(changes.pre_existing.items()))
        sections.append(
            f"already modified before this delegation, untouched by it ({len(changes.pre_existing)} path(s)):\n"
            f"{truncate(listing, limit // 8)}"
        )
    if changes.reverted:
        sections.append(
            "no longer modified (the harness reverted or committed these): " + ", ".join(changes.reverted[:20])
        )
    if changes.commits:
        sections.append(
            "WARNING: the harness committed during this run -- the work is not sitting in the working tree:\n"
            + "\n".join(changes.commits[:20])
        )
    summary = "\n\n".join(sections)
    if not include_diff:
        return summary

    base = before.head
    tracked = [p for p, code in changes.attributed.items() if not code.startswith("??")]
    pieces: list[str] = []
    if tracked:
        args = ["diff", base, "--"] if base else ["diff", "--"]
        full = await _git(git, [*args, *tracked], cwd)
        if full.stdout.strip():
            pieces.append(full.stdout.rstrip())
    new_files = [p for p, code in changes.attributed.items() if code.startswith("??")]
    pieces += await _untracked_diffs(git, cwd, sorted(new_files)[:MAX_UNTRACKED_DIFF_FILES])
    if len(new_files) > MAX_UNTRACKED_DIFF_FILES:
        pieces.append(f"... {len(new_files) - MAX_UNTRACKED_DIFF_FILES} more new files not shown")
    body = "\n".join(pieces).strip("\n") or "(no textual diff)"
    scope = f"$ git diff {base[:12]} -- <files changed by this delegation>" if base else "$ git diff -- <files changed by this delegation>"
    return summary + f"\n\n{scope}\n{truncate(body, limit)}"


# ---------------------------------------------------------------------------
# Working-tree serialisation and isolated worktrees
# ---------------------------------------------------------------------------


class TreeBusy(RuntimeError):
    """Raised when another delegation already holds this working tree."""

    def __init__(self, holder: str) -> None:
        super().__init__(holder)
        self.holder = holder


_TREE_LOCKS: dict[str, asyncio.Lock] = {}
_TREE_HOLDERS: dict[str, str] = {}


@contextlib.asynccontextmanager
async def working_tree_lock(cwd: Path, holder: str):
    """Serialise delegations per working tree; two harnesses editing one tree collide."""
    key = str(cwd)
    lock = _TREE_LOCKS.setdefault(key, asyncio.Lock())
    if lock.locked():
        raise TreeBusy(_TREE_HOLDERS.get(key, "another delegation"))
    await lock.acquire()
    _TREE_HOLDERS[key] = holder
    try:
        yield
    finally:
        _TREE_HOLDERS.pop(key, None)
        lock.release()


async def create_worktree(before: GitSnapshot, repo: Path, run_id: str) -> tuple[Path | None, str]:
    """Check out a throwaway worktree at HEAD so a delegation cannot touch the real tree."""
    if not before.is_repo or not before.git:
        return None, "isolate=true needs a git repository; running in the working tree instead"
    if not before.head:
        return None, "isolate=true needs at least one commit to branch from; running in the working tree instead"
    path = state_dir() / "worktrees" / run_id
    path.parent.mkdir(parents=True, exist_ok=True)
    result = await _git(before.git, ["worktree", "add", "--detach", str(path), before.head], repo)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        return None, f"could not create an isolated worktree ({detail}); running in the working tree instead"
    return path, ""


async def remove_worktree(git: str, repo: Path, path: Path) -> None:
    result = await _git(git, ["worktree", "remove", "--force", str(path)], repo)
    if result.returncode != 0:
        log.warning("could not remove worktree %s: %s", path, (result.stderr or result.stdout).strip()[:200])
        shutil.rmtree(path, ignore_errors=True)
        await _git(git, ["worktree", "prune"], repo)


async def capture_patch(git: str, worktree: Path, changes: ChangeSet) -> str:
    """A single `git apply`-able patch for everything the harness did in the worktree."""
    pieces: list[str] = []
    tracked = [p for p, code in changes.attributed.items() if not code.startswith("??")]
    if tracked:
        result = await _git(git, ["diff", "HEAD", "--", *sorted(tracked)], worktree)
        if result.stdout.strip():
            pieces.append(result.stdout.rstrip())
    new_files = sorted(p for p, code in changes.attributed.items() if code.startswith("??"))
    pieces += await _untracked_diffs(git, worktree, new_files[:MAX_UNTRACKED_DIFF_FILES])
    return "\n".join(pieces).strip("\n")


# ---------------------------------------------------------------------------
# Run journal
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def runs_dir() -> Path:
    return state_dir() / "runs"


def journal_path() -> Path:
    return state_dir() / "runs.jsonl"


def record_run(record: dict[str, Any], report: str, patch: str = "") -> None:
    """Append one run to the journal and store its full report. Never fails a delegation."""
    try:
        directory = runs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        run_id = record["run_id"]
        (directory / f"{run_id}.txt").write_text(report)
        if patch:
            (directory / f"{run_id}.patch").write_text(patch if patch.endswith("\n") else patch + "\n")
        with journal_path().open("a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        prune_runs(keep_runs())
    except OSError as exc:
        log.warning("could not record run %s: %s", record.get("run_id"), exc)


def read_journal(limit: int | None = None) -> list[dict[str, Any]]:
    path = journal_path()
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                records.append(entry)
    except OSError as exc:
        log.warning("could not read the run journal: %s", exc)
        return []
    records.reverse()  # newest first
    return records[:limit] if limit else records


def read_run(run_id: str) -> tuple[dict[str, Any] | None, str, str]:
    """(journal entry, report text, patch text) for one run id."""
    entry = next((r for r in read_journal() if r.get("run_id") == run_id), None)
    report = patch = ""
    for suffix, target in ((".txt", "report"), (".patch", "patch")):
        path = runs_dir() / f"{run_id}{suffix}"
        if path.is_file():
            try:
                text = path.read_text(errors="replace")
            except OSError:  # pragma: no cover - unreadable artefact
                continue
            if target == "report":
                report = text
            else:
                patch = text
    return entry, report, patch


def prune_runs(keep: int) -> None:
    """Keep the newest `keep` run artefacts and journal lines; 0 disables pruning."""
    if keep <= 0:
        return
    records = read_journal()
    if len(records) <= keep:
        return
    kept = records[:keep]
    survivors = {r.get("run_id") for r in kept}
    try:
        with journal_path().open("w") as handle:
            for entry in reversed(kept):  # restore chronological order on disk
                handle.write(json.dumps(entry, default=str) + "\n")
        for artefact in runs_dir().glob("*"):
            if artefact.stem not in survivors:
                artefact.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - best effort
        log.warning("could not prune the run journal: %s", exc)


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
    harness: Harness,
    result: ProcessResult,
    outcome: Outcome,
    cwd: Path,
    command: str,
    git_summary: str,
    extra_meta: list[tuple[str, str]] | None = None,
) -> str:
    limit = max_output_chars()
    sections = [_response_section(outcome, limit)]
    if result.stderr.strip():
        sections.append(("stderr (non-fatal)", truncate(result.stderr, limit // 2)))
    sections.append(("working tree changes (git)", git_summary))
    return _render(
        "[SUCCESS]",
        f"{harness.label} task completed (exit 0) in {_fmt_duration(result.duration)}",
        [
            ("Harness", harness.key),
            ("Working dir", str(cwd)),
            ("Command", command),
            *(extra_meta or []),
            *_outcome_meta(harness, outcome),
        ],
        sections,
    )


def format_failure(
    harness: Harness,
    result: ProcessResult,
    outcome: Outcome,
    cwd: Path,
    command: str,
    git_summary: str,
    extra_meta: list[tuple[str, str]] | None = None,
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
            "      (or delegate the same brief to another harness);\n"
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
            *(extra_meta or []),
            *_outcome_meta(harness, outcome),
        ],
        sections,
    )


def format_timeout(
    harness: Harness,
    result: ProcessResult,
    cwd: Path,
    command: str,
    timeout_seconds: int,
    git_summary: str,
    extra_meta: list[tuple[str, str]] | None = None,
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
        [
            ("Harness", harness.key),
            ("Working dir", str(cwd)),
            ("Command", command),
            ("Timeout", f"{timeout_seconds}s"),
            *(extra_meta or []),
        ],
        sections,
    )


# ---------------------------------------------------------------------------
# Tool implementations (shared across harnesses)
# ---------------------------------------------------------------------------


_WORKTREE_ADMIN_LOCKS: dict[str, asyncio.Lock] = {}


def worktree_admin_lock(repo: Path) -> asyncio.Lock:
    """Serialise `git worktree add/remove` per repository; they all write .git/worktrees."""
    return _WORKTREE_ADMIN_LOCKS.setdefault(str(repo), asyncio.Lock())


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
    *,
    read_only: bool = False,
    isolate: bool = False,
    record: dict[str, Any] | None = None,
) -> str:
    """Run one headless task and return its report.

    `record`, when given, is filled with this run's journal entry plus the harness's response
    text, so an aggregating caller (consult_many) gets structured results without parsing the
    rendered report. It stays empty when the call fails before the harness starts.
    """
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
                    "absolute path in this MCP server's environment) before delegating again, or delegate to "
                    "another harness.",
                )
            ],
        )

    run_id = new_run_id()
    mode = "consult" if read_only else "delegation"
    holder = f"{mode} {run_id} ({harness.key}), started {time.strftime('%H:%M:%SZ', time.gmtime())}"
    started_at = time.time()

    async with contextlib.AsyncExitStack() as stack:
        # Only work that writes needs the tree: a read-only consultation must neither wait for a
        # delegation nor block one, or a panel of consults would run one at a time.
        if not isolate and not read_only:
            try:
                await stack.enter_async_context(working_tree_lock(cwd, holder))
            except TreeBusy as exc:
                return _render(
                    "[ROADBLOCK / FAILURE]",
                    "working tree busy: another delegation is already running here",
                    [
                        ("Harness", harness.key),
                        ("Working dir", str(cwd)),
                        ("Probable cause", f"held by {exc.holder}"),
                    ],
                    [
                        (
                            "ACTION REQUIRED (orchestrator)",
                            "Two harnesses editing one working tree overwrite each other. Wait for the running "
                            "delegation to return, delegate against a different working_dir, or re-send this "
                            "delegation with isolate=true to run it in its own throwaway git worktree.",
                        )
                    ],
                )

        repo_state = await snapshot_git(cwd)
        run_dir, worktree, isolation_note = cwd, None, ""
        if isolate:
            async with worktree_admin_lock(cwd):
                worktree, isolation_note = await create_worktree(repo_state, cwd, run_id)
            if worktree is not None:
                run_dir = worktree

                async def _cleanup(git: str = repo_state.git or "git", path: Path = worktree) -> None:
                    async with worktree_admin_lock(cwd):
                        await remove_worktree(git, cwd, path)

                stack.push_async_callback(_cleanup)

        before = repo_state if run_dir == cwd else await snapshot_git(run_dir)

        try:
            argv, stdin_data = build_argv(
                harness,
                binary,
                prompt,
                flag_list,
                auto_approve,
                timeout_seconds,
                conversation_id,
                read_only=read_only,
            )
        except ValueError as exc:
            return f"[INVALID_ARGUMENT] {exc}"

        command = _display_command(argv, prompt, stdin_data is not None)
        log.info("%s %s to %s in %s (timeout %ss): %s", mode, run_id, harness.key, run_dir, timeout_seconds, command)
        try:
            result = await run_process_with_heartbeat(
                argv,
                cwd=str(run_dir),
                timeout=timeout_seconds,
                stdin_data=stdin_data,
                env=harness.child_env(),
                progress=progress,
                label=f"{harness.key} {mode}",
            )
        except OSError as exc:
            return _render(
                "[ROADBLOCK / FAILURE]",
                f"could not start {harness.binary}: {exc}",
                [("Harness", harness.key), ("Working dir", str(run_dir)), ("Command", command)],
                [("ACTION REQUIRED (orchestrator)", f"Run check_{harness.key}_health and fix the environment before retrying.")],
            )

        changes, after = await diff_snapshots(run_dir, before)
        patch = ""
        if worktree is not None and before.git:
            patch = await capture_patch(before.git, worktree, changes)
        git_summary = await summarize_changes(run_dir, before, changes, after, include_diff)

        extra_meta: list[tuple[str, str]] = [
            ("Run ID", f'{run_id}  (full report: get_run("{run_id}"); recent runs: list_runs())')
        ]
        if read_only:
            # State the mechanism, not the outcome: the change summary below is the evidence.
            extra_meta.append(
                (
                    "Mode",
                    f"read-only consultation ({shlex.join(harness.readonly_flags) or 'no edit flags'}, "
                    "permission auto-approval off). This restricts the harness's own edit tools; it is "
                    "not a sandbox, and the harness may still run commands.",
                )
            )
            if changes.attributed:
                extra_meta.append(
                    (
                        "WARNING",
                        f"this consultation was supposed to change nothing, but {len(changes.attributed)} "
                        "path(s) changed during it. Review them before trusting this answer.",
                    )
                )
        if worktree is not None:
            patch_path = runs_dir() / f"{run_id}.patch"
            extra_meta.append(
                (
                    "Isolation",
                    f"ran in a throwaway git worktree at {worktree} (now removed). The working tree at {cwd} was "
                    f"NOT modified. Apply the work with:  git -C {cwd} apply {patch_path}"
                    if patch
                    else f"ran in a throwaway git worktree at {worktree} (now removed); it produced no changes",
                )
            )
        elif isolation_note:
            extra_meta.append(("Isolation", isolation_note))

        outcome = Outcome(text="")
        if result.timed_out:
            log.warning("%s timed out after %ss in %s", harness.key, timeout_seconds, run_dir)
            report = format_timeout(harness, result, cwd, command, timeout_seconds, git_summary, extra_meta)
            status = "timeout"
        else:
            outcome = parse_outcome(harness, result.stdout)
            if result.returncode == 0 and not outcome.harness_error:
                log.info("%s %s succeeded in %s (%s)", harness.key, run_id, run_dir, _fmt_duration(result.duration))
                report = format_success(harness, result, outcome, cwd, command, git_summary, extra_meta)
                status = "success"
            else:
                log.warning("%s %s failed (exit %s, status %s) in %s", harness.key, run_id, result.returncode, outcome.status, run_dir)
                report = format_failure(harness, result, outcome, cwd, command, git_summary, extra_meta)
                status = "failure"

        report = redact(report)
        entry = {
            "run_id": run_id,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
            "mode": "consult" if read_only else "delegate",
            "harness": harness.key,
            "working_dir": str(cwd),
            "isolated": worktree is not None,
            "status": status,
            "exit_code": result.returncode,
            "duration_seconds": round(result.duration, 2),
            "timeout_seconds": timeout_seconds,
            "conversation_id": outcome.conversation_id,
            "model": outcome.model,
            "tokens_in": outcome.tokens_in,
            "tokens_out": outcome.tokens_out,
            "cost_usd": outcome.cost_usd,
            "files_changed": changes.touched(),
            "commits": changes.commits,
            "prompt_chars": len(prompt),
        }
        if status != "success":
            entry["cause"] = classify_failure(
                harness, result.returncode, f"{outcome.text}\n{result.stdout}", result.stderr
            )
        record_run(entry, report, patch)
        if record is not None:
            record.update(entry)
            record["response"] = redact(outcome.text)
        return report


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

    async def probe(args: list[str]) -> ProcessResult:
        try:
            return await run_process([binary, *args], cwd=home, timeout=HEALTH_PROBE_TIMEOUT, env=env, kill_grace=1)
        except OSError as exc:
            return ProcessResult([binary, *args], 127, "", str(exc), 0.0)

    # Both probes are independent: run them together so health costs one timeout, not two.
    version, auth = await asyncio.gather(probe(["--version"]), probe(list(harness.auth_probe)))
    if version.timed_out:
        version_text = f"(timed out after {HEALTH_PROBE_TIMEOUT}s)"
        problems.append("version check timed out")
    elif version.returncode == 0 and version.stdout.strip():
        version_text = version.stdout.strip().splitlines()[0]
    else:
        version_text = f"(exit {version.returncode}: {(version.stderr or version.stdout).strip()[:300] or 'no output'})"
        problems.append(f"`{harness.binary} --version` failed")

    extra_section = ""
    if auth.timed_out:
        auth_text = f"UNKNOWN: `{harness.binary} {' '.join(harness.auth_probe)}` timed out after {HEALTH_PROBE_TIMEOUT}s"
        problems.append("authentication probe timed out")
    else:
        ok, auth_text, extra_section = harness.parse_auth(auth)
        if not ok:
            problems.append("authentication probe failed")

    installed = _version_number(version_text)
    drift = ""
    if installed and installed != harness.verified_version and not installed.startswith(harness.verified_version):
        drift = (
            f"installed {installed}, adapter verified against {harness.verified_version}: if delegations start "
            f"failing with usage errors, re-check the flags in `{harness.binary} --help`"
        )  # informational only: a newer CLI usually still works, so this must not mark the harness DEGRADED

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
            ("Version", version_text + (f"  [{drift}]" if drift else f"  (verified: {harness.verified_version})")),
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
        "Execution bridge to headless coding harnesses: Antigravity (agy), Claude Code (claude), OpenCode (opencode) and pi. "
        "Call check_<harness>_health once before the first delegation. delegate_to_<harness> runs ONE "
        "headless task and returns a report starting with [SUCCESS], [ROADBLOCK / FAILURE], [TIMEOUT_ERROR] "
        "or [INVALID_ARGUMENT]; branch on that prefix. Every report carries a Conversation ID (pass it back as "
        "conversation_id to continue that session for review-fix rounds) and a Run ID (pass it to get_run to "
        "re-read the full report later). consult_<harness> asks a harness a question in its read-only mode, for "
        "a second opinion that cannot touch the tree. One delegation at a time per working tree is enforced; use "
        "isolate=true to run several harnesses on one brief in throwaway git worktrees. Give the harness scoped, "
        "verifiable briefs: files, acceptance criteria, the exact test command. Review every changed file before "
        "accepting."
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
            "Extra CLI flags for the harness. Leave this empty in almost every case: the harness then runs "
            "with its own configured model and settings, which is what you want. Do NOT pass '--model' "
            "unless the user explicitly named a model for this delegation; overriding a harness's configured "
            "default has produced worse results, including fabricated file contents. Prompt, resume and "
            "interactive flags are rejected here (use the prompt / conversation_id arguments). Default: none."
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
IsolateArg = Annotated[
    bool,
    Field(
        description=(
            "Run in a throwaway `git worktree` checked out at HEAD instead of the real working tree. The "
            "delegation cannot touch the caller's files; the report links a patch to apply if you accept the "
            "work. Use it to run several harnesses on the same brief at once, or to keep a dirty tree safe. "
            "Default: false."
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


def _progress_sink(ctx: "_Context | None") -> ProgressFn | None:
    if ctx is None:
        return None

    async def progress(value: float, total: float | None, message: str) -> None:
        await ctx.report_progress(value, total, message)

    return progress


def _register_harness(harness: Harness) -> tuple[Callable[..., Any], Callable[..., Any]]:
    async def delegate_tool(
        prompt: PromptArg,
        working_dir: WorkingDirArg,
        flags: FlagsArg = None,
        auto_approve: AutoApproveArg = True,
        timeout_seconds: TimeoutArg = DEFAULT_TIMEOUT_SECONDS,
        conversation_id: ConversationArg = None,
        include_diff: IncludeDiffArg = False,
        isolate: IsolateArg = False,
        ctx: _Context = None,  # injected by the SDK; excluded from the tool's input schema
    ) -> str:
        return await delegate(
            harness,
            prompt,
            working_dir,
            flags,
            auto_approve,
            timeout_seconds,
            conversation_id,
            include_diff,
            _progress_sink(ctx),
            isolate=isolate,
        )

    async def consult_tool(
        prompt: PromptArg,
        working_dir: WorkingDirArg,
        flags: FlagsArg = None,
        timeout_seconds: TimeoutArg = DEFAULT_TIMEOUT_SECONDS,
        conversation_id: ConversationArg = None,
        ctx: _Context = None,
    ) -> str:
        return await delegate(
            harness,
            prompt,
            working_dir,
            flags,
            False,  # no auto-approval: a consultation must not be able to accept an edit
            timeout_seconds,
            conversation_id,
            False,
            _progress_sink(ctx),
            read_only=True,
        )

    async def health_tool() -> str:
        return await health(harness)

    delegate_tool.__name__ = f"delegate_to_{harness.key}"
    delegate_tool.__qualname__ = delegate_tool.__name__
    consult_tool.__name__ = f"consult_{harness.key}"
    consult_tool.__qualname__ = consult_tool.__name__
    health_tool.__name__ = f"check_{harness.key}_health"
    health_tool.__qualname__ = health_tool.__name__
    mcp.tool(
        name=delegate_tool.__name__,
        description=(
            f"Execute an implementation, refactoring or testing task headlessly via {harness.label}. Returns a "
            "structured report prefixed with [SUCCESS] (response + the changes THIS delegation made, separated "
            "from edits that were already in the tree), [ROADBLOCK / FAILURE] (exit code, stderr, extracted "
            "diagnostics, call to action), [TIMEOUT_ERROR] (partial logs after the process tree was killed) or "
            "[INVALID_ARGUMENT]. The report's Conversation ID can be passed back as conversation_id to continue "
            "the same session for a review-fix round; its Run ID retrieves the full report later via get_run."
        ),
    )(delegate_tool)
    mcp.tool(
        name=consult_tool.__name__,
        description=(
            f"Ask {harness.label} a question about the code instead of asking it to change the code: it runs in "
            f"the harness's read-only mode ({shlex.join(harness.readonly_flags) or 'no-edit flags'}) with "
            "permission auto-approval off, so its edit tools are disabled. Use it for a second opinion, a design "
            "review, a diff review or a bug hunt from a model other than your own. Same report prefixes as "
            "delegate. The report still carries a change summary, and says so if anything did change."
        ),
    )(consult_tool)
    mcp.tool(
        name=health_tool.__name__,
        description=(
            f"Pre-flight check for {harness.label}: binary discovery, version and authentication probe. Returns a "
            "report prefixed with [HEALTH: READY], [HEALTH: DEGRADED] or [HEALTH: UNAVAILABLE]."
        ),
    )(health_tool)
    return delegate_tool, health_tool


PANEL_MIN_RESPONSE_CHARS = 2_000
DEFAULT_PANEL_GRACE_SECONDS = 120


def panel_grace_seconds() -> float:
    """How long a panel keeps waiting for stragglers after its first answer arrives.

    Without this one hung harness holds every answer hostage for the whole timeout_seconds.
    0 disables the grace, leaving timeout_seconds as the only bound.
    """
    return float(_env_int("BRIDGE_PANEL_GRACE", DEFAULT_PANEL_GRACE_SECONDS, 0))


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _panel_table(rows: list[dict[str, Any]]) -> str:
    header = f"{'harness':12} {'status':9} {'time':>8} {'tokens':>9} {'cost':>9}  {'run id':30} conversation id"
    lines = [header, "-" * len(header)]
    for row in rows:
        tokens = (row.get("tokens_in") or 0) + (row.get("tokens_out") or 0)
        cost = row.get("cost_usd") or 0.0
        lines.append(
            f"{row['harness']:12} {row['status']:9} "
            f"{str(row.get('duration_seconds', '?')) + 's':>8} {tokens or '-':>9} "
            f"{('$%.4f' % cost) if cost else '-':>9}  {str(row.get('run_id') or '-'):30} "
            f"{row.get('conversation_id') or '-'}"
        )
    return "\n".join(lines)


async def consult_many(
    prompt: Annotated[
        str,
        Field(
            description=(
                "The question, in full. The harnesses share this repository but NOT this "
                "conversation, so paste the plan or decision itself and say what you want attacked: "
                "'here is the plan ... what breaks, what did I miss, where am I wrong?'"
            )
        ),
    ],
    working_dir: WorkingDirArg,
    harnesses: Annotated[
        list[str] | None,
        Field(description="Harness keys to ask, e.g. ['opencode', 'antigravity']. Default: every enabled harness."),
    ] = None,
    timeout_seconds: TimeoutArg = DEFAULT_TIMEOUT_SECONDS,
    conversation_ids: Annotated[
        dict[str, str] | None,
        Field(
            description=(
                "Per harness, the Conversation ID from an earlier round: {'antigravity': 'conv-1'}. "
                "Each named harness then answers with its previous critique still in context, so a "
                "follow-up round costs one short rebuttal instead of restating the plan."
            )
        ),
    ] = None,
    ctx: _Context = None,
) -> str:
    """Put one question to several harnesses at once and return their answers side by side."""
    keys = list(REGISTERED_TOOLS) if not harnesses else [str(k).strip() for k in harnesses if str(k).strip()]
    unknown = [k for k in keys if k not in REGISTERED_TOOLS]
    if unknown:
        return (
            f"[INVALID_ARGUMENT] unknown or disabled harness(es) {unknown}; "
            f"available: {', '.join(REGISTERED_TOOLS)}."
        )
    keys = list(dict.fromkeys(keys))
    if not keys:
        return "[INVALID_ARGUMENT] no harness is enabled."
    ids = conversation_ids or {}
    if not isinstance(ids, dict):
        return "[INVALID_ARGUMENT] conversation_ids must be an object mapping harness key to conversation id."

    started = time.monotonic()
    finished: dict[str, str] = {}

    async def one(key: str) -> tuple[str, str, dict[str, Any]]:
        entry: dict[str, Any] = {}
        report = await delegate(
            HARNESSES[key],
            prompt,
            working_dir,
            None,
            False,  # a consultation never auto-approves
            timeout_seconds,
            ids.get(key),
            False,
            None,  # per-harness heartbeats would interleave; the panel reports progress instead
            read_only=True,
            record=entry,
        )
        finished[key] = entry.get("status") or ("success" if report.startswith("[SUCCESS]") else "failure")
        return key, report, entry

    progress = _progress_sink(ctx)
    done = asyncio.Event()

    async def beat() -> None:
        if progress is None:
            return
        try:
            await progress(0.0, None, f"consulting {len(keys)} harness(es): {', '.join(keys)}")
        except Exception:
            return
        ticks = 0
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=heartbeat_seconds() or 15)
            except asyncio.TimeoutError:
                pass
            if done.is_set():
                return
            ticks += 1
            waiting = [k for k in keys if k not in finished]
            try:
                await progress(
                    float(ticks),
                    None,
                    f"{int(time.monotonic() - started)}s | done: {', '.join(finished) or 'none'} "
                    f"| still thinking: {', '.join(waiting)}",
                )
            except Exception:
                return

    beat_task = asyncio.ensure_future(beat())
    tasks = {asyncio.ensure_future(one(key)): key for key in keys}
    results: list[tuple[str, str, dict[str, Any]]] = []
    abandoned: list[str] = []
    grace = panel_grace_seconds()
    try:
        # N harnesses think at the same time, so the panel costs the slowest answer rather
        # than the sum. Once the first one is back the rest get `grace` seconds, so a single
        # hung harness cannot hold the answers that did arrive for the whole timeout.
        pending = set(tasks)
        deadline = started + timeout_seconds
        grace_applied = False
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            finished_now, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not finished_now:
                break  # deadline hit with nothing new
            for task in finished_now:
                key = tasks[task]
                try:
                    results.append(task.result())
                except Exception as exc:  # a crash in the bridge itself, not in the harness
                    log.warning("consult of %s raised: %s", key, exc)
                    results.append((key, f"[ROADBLOCK / FAILURE] consulting {key} raised {exc!r}", {}))
            if pending and grace > 0 and not grace_applied:
                grace_applied = True  # the first answer is in; the rest are on the clock
                deadline = min(deadline, time.monotonic() + grace)
        for task in pending:
            abandoned.append(tasks[task])
            task.cancel()  # cancellation kills that harness's process tree
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        done.set()
        beat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await beat_task
        for task in tasks:
            if not task.done():  # pragma: no cover - belt and braces on an early exit
                task.cancel()
    elapsed = time.monotonic() - started
    # Report in the order asked, not the order they finished.
    order = {key: index for index, key in enumerate(keys)}
    results.sort(key=lambda item: order.get(item[0], len(order)))

    budget = max(PANEL_MIN_RESPONSE_CHARS, max_output_chars() // max(1, len(keys)))
    rows: list[dict[str, Any]] = []
    sections: list[tuple[str, str]] = []
    answered = 0
    for key, report, entry in results:
        ok = report.startswith("[SUCCESS]")
        answered += 1 if ok else 0
        rows.append(
            {
                "harness": key,
                "status": "answered" if ok else (entry.get("status") or "failed"),
                "duration_seconds": entry.get("duration_seconds"),
                "tokens_in": entry.get("tokens_in"),
                "tokens_out": entry.get("tokens_out"),
                "cost_usd": entry.get("cost_usd"),
                "run_id": entry.get("run_id"),
                "conversation_id": entry.get("conversation_id"),
            }
        )
        body = entry.get("response") or ""
        if not ok or not body.strip():
            # Hand back the failure report itself: the caller still needs to know why it is missing.
            body = (body + "\n\n" if body.strip() else "") + truncate(report, budget)
        sections.append((f"{key} says", truncate(body, budget)))

    for key in abandoned:
        rows.append({"harness": key, "status": "gave up", "duration_seconds": round(elapsed, 2)})
        sections.append(
            (
                f"{key} says",
                f"(no answer: {key} was still working after the panel's grace period "
                f"({int(panel_grace_seconds())}s past the first answer) and was stopped. Ask it on its own "
                f"with consult_{key}, or raise BRIDGE_PANEL_GRACE.)",
            )
        )

    panel_id = new_run_id()
    prefix = "[SUCCESS]" if answered else "[ROADBLOCK / FAILURE]"
    headline = (
        f"{answered} of {len(keys)} harness(es) answered in {_fmt_duration(elapsed)} (run in parallel)"
        + (f"; {len(abandoned)} did not finish in time: {', '.join(abandoned)}" if abandoned else "")
    )
    sections.insert(0, ("panel", _panel_table(rows)))
    sections.append(
        (
            "NEXT STEP (orchestrator)",
            "Read the answers against each other, do not average them.\n"
            "  - Where two harnesses independently raise the same objection, treat it as real.\n"
            "  - Where they disagree, that is the part of the plan you have not settled; decide it\n"
            "    yourself or put the disagreement back to them.\n"
            "  - To push back on one answer, call consult_<harness> with that row's Conversation ID,\n"
            "    or call consult_many again with conversation_ids to re-ask the whole panel.\n"
            f"  - Full untruncated text of any answer: get_run(\"<run id>\"). This panel: "
            f'get_run("{panel_id}").\n'
            "None of these harnesses could change a file: this was a read-only consultation.",
        )
    )
    report = redact(
        _render(
            prefix,
            headline,
            [
                ("Panel run ID", panel_id),
                ("Working dir", str(working_dir)),
                ("Asked", _one_line(prompt, 300)),
            ],
            sections,
        )
    )
    record_run(
        {
            "run_id": panel_id,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": "consult_many",
            "harness": ",".join(keys),
            "working_dir": str(working_dir),
            "status": "success" if answered else "failure",
            "duration_seconds": round(elapsed, 2),
            "timeout_seconds": timeout_seconds,
            "answered": answered,
            "of": len(keys),
            "abandoned": abandoned,
            "child_runs": [row.get("run_id") for row in rows if row.get("run_id")],
            "tokens_in": sum(row.get("tokens_in") or 0 for row in rows) or None,
            "tokens_out": sum(row.get("tokens_out") or 0 for row in rows) or None,
            "cost_usd": sum(row.get("cost_usd") or 0.0 for row in rows) or None,
            "files_changed": [],
            "prompt_chars": len(prompt),
        },
        report,
    )
    return report


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _harness_rollup(records: list[dict[str, Any]]) -> str:
    """Per-harness record, so 'pick by remaining quota' can be read off instead of guessed."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for entry in records:
        for key in str(entry.get("harness", "?")).split(","):  # a panel names several
            by_key.setdefault(key.strip(), []).append(entry)
    if not by_key:
        return ""
    header = f"{'harness':12} {'runs':>5} {'ok':>4} {'fail':>5} {'timeout':>8} {'median':>8} {'cost':>9}  recent trouble"
    lines = [header, "-" * len(header)]
    for key in sorted(by_key):
        runs = by_key[key]
        ok = sum(1 for r in runs if r.get("status") == "success")
        failed = sum(1 for r in runs if r.get("status") == "failure")
        timed_out = sum(1 for r in runs if r.get("status") == "timeout")
        median = _median([float(r.get("duration_seconds") or 0) for r in runs])
        cost = sum(float(r.get("cost_usd") or 0) for r in runs)
        # `records` arrives newest-first, so the first few are the most recent.
        blockers = [
            str(r.get("cause", ""))
            for r in runs[:5]
            if re.search(r"quota|rate limit|Authentication", str(r.get("cause", "")), re.IGNORECASE)
        ]
        trouble = ""
        if blockers:
            kind = "quota/rate limit" if re.search(r"quota|rate", blockers[0], re.I) else "authentication"
            trouble = f"{len(blockers)} of the last {min(5, len(runs))} runs hit {kind} -> check_{key}_health"
        lines.append(
            f"{key:12} {len(runs):>5} {ok:>4} {failed:>5} {timed_out:>8} "
            f"{f'{median:.1f}s':>8} {('$%.4f' % cost) if cost else '-':>9}  {trouble}"
        )
    return "\n".join(lines)


def _format_runs(records: list[dict[str, Any]]) -> str:
    if not records:
        return "(no runs recorded yet)"
    header = f"{'run id':30} {'started':21} {'harness':12} {'mode':9} {'status':8} {'dur':>8} {'tokens':>13} {'cost':>9}  files"
    rows = [header, "-" * len(header)]
    cost = 0.0
    tokens = 0
    for entry in records:
        run_cost = entry.get("cost_usd") or 0.0
        cost += run_cost if isinstance(run_cost, (int, float)) else 0.0
        run_tokens = (entry.get("tokens_in") or 0) + (entry.get("tokens_out") or 0)
        tokens += run_tokens
        files = entry.get("files_changed") or []
        rows.append(
            f"{str(entry.get('run_id', '?')):30} {str(entry.get('started', '?')):21} "
            f"{str(entry.get('harness', '?')):12} {str(entry.get('mode', '?')):9} "
            f"{str(entry.get('status', '?')):8} {str(entry.get('duration_seconds', '?')) + 's':>8} "
            f"{run_tokens or '-':>13} {('$%.4f' % run_cost) if run_cost else '-':>9}  {len(files)}"
        )
    statuses: dict[str, int] = {}
    for entry in records:
        statuses[str(entry.get("status"))] = statuses.get(str(entry.get("status")), 0) + 1
    rows.append("")
    rows.append(
        f"{len(records)} run(s): " + ", ".join(f"{count} {name}" for name, count in sorted(statuses.items()))
        + f" | {tokens} tokens | ${cost:.4f} recorded cost"
    )
    rollup = _harness_rollup(records)
    if rollup:
        rows += ["", "by harness (use this to choose one: recent success rate, speed and cost)", rollup]
    return "\n".join(rows)


async def list_runs(
    limit: Annotated[int, Field(description="How many of the most recent runs to list (1-200). Default: 20.")] = 20,
    harness: Annotated[str | None, Field(description="Only runs on this harness key. Default: all.")] = None,
    working_dir: Annotated[str | None, Field(description="Only runs whose working_dir is this path. Default: all.")] = None,
    status: Annotated[str | None, Field(description="Only runs with this status: success, failure or timeout.")] = None,
) -> str:
    """Recent delegations and consultations, with duration, tokens, cost and files touched."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        return "[INVALID_ARGUMENT] limit must be an integer between 1 and 200."
    records = read_journal()
    if harness:
        records = [r for r in records if r.get("harness") == harness]
    if working_dir:
        wanted = str(Path(os.path.expanduser(working_dir)).resolve())
        records = [r for r in records if r.get("working_dir") == wanted]
    if status:
        records = [r for r in records if r.get("status") == status]
    return _format_runs(records[:limit])


async def get_run(
    run_id: Annotated[str, Field(description="Run ID from a delegation report or from list_runs.")],
    section: Annotated[
        str,
        Field(description="'report' (the full stored report), 'patch' (an isolated run's diff) or 'summary'."),
    ] = "report",
) -> str:
    """Re-read a stored run: the full report, its patch, or a one-line summary."""
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id.strip()):
        return "[INVALID_ARGUMENT] run_id must look like 20260904T101500Z-1a2b3c4d (see list_runs)."
    if section not in ("report", "patch", "summary"):
        return "[INVALID_ARGUMENT] section must be 'report', 'patch' or 'summary'."
    entry, report, patch = read_run(run_id.strip())
    if entry is None and not report:
        return f"[INVALID_ARGUMENT] no run {run_id} in the journal at {journal_path()}."
    if section == "summary":
        return _format_runs([entry]) if entry else "(run artefact present but no journal entry)"
    if section == "patch":
        if not patch:
            return f"(run {run_id} stored no patch; patches are only kept for isolate=true runs)"
        return truncate(patch, max_output_chars())
    return truncate(report, max_output_chars() * 2) if report else "(the stored report for this run is gone)"


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
    if not chosen:
        # Failing open here used to expose every harness on a single typo.
        raise SystemExit(
            f"intercom: INTERCOM_HARNESSES={raw!r} names no known harness. "
            f"Use a comma-separated subset of: {', '.join(HARNESSES)}."
        )
    return chosen


REGISTERED_TOOLS = {key: _register_harness(HARNESSES[key]) for key in enabled_harness_keys()}

mcp.tool(
    name="list_runs",
    description=(
        "List recent intercom delegations and consultations from the run journal: when, which harness, status, "
        "duration, tokens, cost and how many files each touched. Use it to pick a harness by recent success and "
        "remaining budget, or to find the Run ID of an earlier delegation."
    ),
)(list_runs)
mcp.tool(
    name="consult_many",
    description=(
        "Put ONE question to several harnesses at once, read-only, and get their answers side by "
        "side in a single result. This is the tool for 'what does another model think of this "
        "plan?': the harnesses run in parallel, so a panel costs the slowest answer rather than the "
        "sum, and none of them can change a file. They do not see this conversation, so put the "
        "plan itself in the prompt. Each answer comes back with its own Conversation ID, so you can "
        "push back on one of them, or re-ask the whole panel, without restating the plan."
    ),
)(consult_many)
mcp.tool(
    name="get_run",
    description=(
        "Re-read a stored run by its Run ID: the full report (including the diff, if the run captured one), the "
        "patch of an isolate=true run, or a one-line summary. Lets a delegation report stay short while the "
        "detail remains one call away."
    ),
)(get_run)


@mcp.resource(
    "intercom://runs/{run_id}",
    name="intercom run report",
    description="The stored report of one intercom delegation, by Run ID.",
    mime_type="text/plain",
)
def run_resource(run_id: str) -> str:
    _, report, _ = read_run(run_id)
    return report or f"(no stored report for run {run_id})"
delegate_to_antigravity, check_antigravity_health = REGISTERED_TOOLS.get("antigravity", (None, None))
delegate_to_claude_code, check_claude_code_health = REGISTERED_TOOLS.get("claude_code", (None, None))
delegate_to_opencode, check_opencode_health = REGISTERED_TOOLS.get("opencode", (None, None))
delegate_to_pi, check_pi_health = REGISTERED_TOOLS.get("pi", (None, None))


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
    log.info("run journal: %s | allowed dirs: %s", journal_path(), allowed_dirs() or "(anywhere)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
