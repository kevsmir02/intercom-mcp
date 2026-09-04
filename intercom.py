#!/usr/bin/env python3
"""intercom: command-line front end for the harness bridge MCP server.

    intercom setup       choose harnesses and orchestrators, register the server, link the skill
    intercom doctor      health of the enabled harnesses plus registration checks
    intercom serve       run the MCP server (this is what the orchestrator registrations invoke)
    intercom runs        list recent delegations (harness, status, duration, tokens, cost)
    intercom show        print the stored report or patch of one run
    intercom apply       apply an isolated run's patch to a working tree
    intercom test        run the hermetic test suite
    intercom update      pull the latest version and reinstall the dependency
    intercom uninstall   remove registrations, skill links, launcher and configuration
    intercom config      print the effective configuration and paths

Configuration lives in $XDG_CONFIG_HOME/intercom/config.json (default ~/.config/intercom).
`serve` turns it into the environment variables server.py understands, so values set
explicitly by an orchestrator's own environment block still win.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys

try:  # raw-terminal checkbox input; POSIX only
    import select as _select
    import termios as _termios
    import tty as _tty

    _RAW_TTY = True
except ImportError:  # pragma: no cover - Windows
    _RAW_TTY = False
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SERVER_KEY = "intercom"
SKILL_NAME = "intercom"
SKILL_SRC = HERE / "skills" / SKILL_NAME
AGENT_NAME = "intercom-delegate"
AGENT_SRC = {
    "claude_code": HERE / "agents" / "claude" / f"{AGENT_NAME}.md",
    "opencode": HERE / "agents" / "opencode" / f"{AGENT_NAME}.md",
}
HARNESS_KEYS = ("antigravity", "claude_code", "opencode", "pi")
HARNESS_LABELS = {
    "antigravity": "Antigravity CLI (agy)",
    "claude_code": "Claude Code (claude)",
    "opencode": "OpenCode (opencode)",
    "pi": "pi (pi-coding-agent)",
}
HARNESS_ENV_PREFIX = {"antigravity": "AGY", "claude_code": "CLAUDE", "opencode": "OPENCODE", "pi": "PI"}
ORCHESTRATOR_KEYS = ("opencode", "claude_code", "antigravity")
ORCHESTRATOR_LABELS = {"opencode": "OpenCode", "claude_code": "Claude Code", "antigravity": "Antigravity CLI (agy)"}
OPENCODE_TIMEOUT_MS = 10_800_000
CONFIG_VERSION = 1


# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or home() / ".config")


def config_dir() -> Path:
    return xdg_config_home() / "intercom"


def config_path() -> Path:
    return config_dir() / "config.json"


def bin_dir() -> Path:
    return Path(os.environ.get("INTERCOM_BIN_DIR") or home() / ".local" / "bin")


def launcher_path() -> Path:
    return bin_dir() / "intercom"


def opencode_config_path() -> Path:
    return xdg_config_home() / "opencode" / "opencode.json"


def claude_skills_dir() -> Path:
    return home() / ".claude" / "skills"


def opencode_skills_dir() -> Path:
    return xdg_config_home() / "opencode" / "skills"


def claude_agents_dir() -> Path:
    return home() / ".claude" / "agents"


def opencode_agent_dir() -> Path:
    return xdg_config_home() / "opencode" / "agent"


def agents_skills_dir() -> Path:
    """Shared ~/.agents/skills, which the Antigravity CLI (and OpenCode) read."""
    return home() / ".agents" / "skills"


def agy_bin() -> str | None:
    return shutil.which(os.path.expanduser(os.environ.get("AGY_BIN", "").strip() or "agy"))


def venv_python() -> str:
    candidate = HERE / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def default_config() -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "install_dir": str(HERE),
        "harnesses": [],
        "orchestrators": [],
        "default_flags": {},
        "max_depth": 1,
        "skill_links": [],
        "agent_links": [],
        "claude_profiles": [],
        "instruction_files": [],
        "env": {},
    }


def load_config() -> dict[str, Any]:
    cfg = default_config()
    path = config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {path} is not valid JSON ({exc}); fix or delete it and rerun `intercom setup`")
        if isinstance(data, dict):
            cfg.update(data)
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path


def build_serve_env(cfg: dict[str, Any], base_env: dict[str, str]) -> dict[str, str]:
    """Environment for server.py: configuration fills in only what the caller left unset."""
    env = dict(base_env)
    harnesses = [h for h in cfg.get("harnesses", []) if h in HARNESS_KEYS]
    if harnesses:
        env.setdefault("INTERCOM_HARNESSES", ",".join(harnesses))
    for key, flags in (cfg.get("default_flags") or {}).items():
        if key in HARNESS_ENV_PREFIX and str(flags).strip():
            env.setdefault(f"{HARNESS_ENV_PREFIX[key]}_DEFAULT_FLAGS", str(flags).strip())
    env.setdefault("BRIDGE_MAX_DEPTH", str(cfg.get("max_depth", 1)))
    # Anything else the user configured (BRIDGE_ALLOWED_DIRS, <H>_BIN, BRIDGE_KEEP_RUNS, ...).
    for name, value in (cfg.get("env") or {}).items():
        if isinstance(name, str) and name.strip() and str(value).strip():
            env.setdefault(name.strip(), str(value).strip())
    return env


# ---------------------------------------------------------------------------
# Output and prompts
# ---------------------------------------------------------------------------


def say(msg: str) -> None:
    print(f"==> {msg}")


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def ask_multi(title: str, options: list[tuple[str, str, bool]]) -> list[str]:
    """Numbered multi-select. options = [(key, label, selected_by_default)]."""
    print(title)
    for idx, (_, label, selected) in enumerate(options, 1):
        print(f"  {idx}. {label}{'  (default)' if selected else ''}")
    defaults = [str(i) for i, (_, _, sel) in enumerate(options, 1) if sel]
    while True:
        raw = input(f"Choose numbers separated by commas [{','.join(defaults) or 'none'}]: ").strip()
        if not raw:
            chosen = defaults
        else:
            chosen = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
        try:
            picks = [options[int(p) - 1][0] for p in chosen]
            if all(1 <= int(p) <= len(options) for p in chosen):
                return list(dict.fromkeys(picks))
        except (ValueError, IndexError):
            pass
        print("  please enter numbers from the list, e.g. 1,2")


def _read_key(fd: int) -> str:
    """Read one keypress from a cbreak-mode terminal, normalised to a token."""
    ch = os.read(fd, 1)
    if ch == b"\x1b":  # escape: an arrow sequence, or a bare Esc (cancel)
        ready, _, _ = _select.select([fd], [], [], 0.05)
        if not ready:
            return "CANCEL"
        seq = os.read(fd, 2)
        return {b"[A": "UP", b"OA": "UP", b"[B": "DOWN", b"OB": "DOWN"}.get(seq, seq[-1:].decode("latin-1"))
    return {
        b"\r": "ENTER", b"\n": "ENTER", b" ": "SPACE", b"\x03": "CANCEL",
        b"q": "CANCEL", b"Q": "CANCEL", b"k": "UP", b"j": "DOWN", b"a": "ALL", b"A": "ALL",
    }.get(ch, ch.decode("latin-1", "replace"))


def checkbox_select(title: str, options: list[tuple[str, str, bool]]) -> list[str]:
    """Interactive checkbox list. Up/down move, space toggles, a = all/none, enter confirms,
    q/Esc cancels (raises KeyboardInterrupt). Returns the selected keys, order preserved."""
    keys = [key for key, _, _ in options]
    labels = [label for _, label, _ in options]
    selected = [bool(default) for _, _, default in options]
    cursor = 0
    hint = "  (up/down move . space toggle . a all/none . enter confirm . q cancel)"
    fd = sys.stdin.fileno()
    block = len(options) + 1  # option rows + hint row

    def draw(first: bool = False) -> None:
        if not first:
            sys.stdout.write(f"\x1b[{block}A")  # move cursor back to the top of the block
        for i, label in enumerate(labels):
            pointer = ">" if i == cursor else " "
            box = "[x]" if selected[i] else "[ ]"
            sys.stdout.write(f"\r\x1b[K  {pointer} {box} {label}\n")
        sys.stdout.write(f"\r\x1b[K{hint}\n")
        sys.stdout.flush()

    print(title)
    old = _termios.tcgetattr(fd)
    try:
        _tty.setcbreak(fd)
        draw(first=True)
        while True:
            key = _read_key(fd)
            if key == "UP":
                cursor = (cursor - 1) % len(options)
            elif key == "DOWN":
                cursor = (cursor + 1) % len(options)
            elif key == "SPACE":
                selected[cursor] = not selected[cursor]
            elif key == "ALL":
                fill = not all(selected)
                selected = [fill] * len(options)
            elif key == "ENTER":
                break
            elif key == "CANCEL":
                raise KeyboardInterrupt
            else:
                continue
            draw()
    finally:
        _termios.tcsetattr(fd, _termios.TCSADRAIN, old)
    return [keys[i] for i, on in enumerate(selected) if on]


def multi_select(title: str, options: list[tuple[str, str, bool]]) -> list[str]:
    """Checkbox picker on a capable terminal; the numbered prompt otherwise."""
    if _RAW_TTY and interactive() and sys.stdin.isatty():
        try:
            chosen = checkbox_select(title, options)
        except (KeyboardInterrupt, OSError, _termios.error):
            raise
        labels = {key: label for key, label, _ in options}
        print("  selected: " + (", ".join(labels[k] for k in chosen) if chosen else "none"))
        return chosen
    return ask_multi(title, options)


# ---------------------------------------------------------------------------
# Harness and orchestrator detection
# ---------------------------------------------------------------------------


def _server_module():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        import server  # noqa: WPS433 (lazy: needs the venv's mcp package)
    except ImportError as exc:
        raise SystemExit(
            f"error: cannot import the server ({exc}). Run this through the launcher or the project venv: "
            f"{HERE}/.venv/bin/python intercom.py ..."
        )
    return server


def detect_harnesses() -> dict[str, str | None]:
    server = _server_module()
    return {key: server.HARNESSES[key].resolve_binary() for key in HARNESS_KEYS}


def harness_health(keys: list[str]) -> dict[str, tuple[str, str]]:
    """key -> (status word, headline) from the server's own health probes."""
    server = _server_module()

    async def one(key: str) -> tuple[str, tuple[str, str]]:
        report = await server.health(server.HARNESSES[key])
        first = report.splitlines()[0]
        status = first[first.find(":") + 1 : first.find("]")].strip()
        return key, (status, first[first.find("]") + 1 :].strip())

    async def run() -> dict[str, tuple[str, str]]:
        # Each probe can burn a 45s timeout; run every harness at once so doctor stays quick.
        return dict(await asyncio.gather(*(one(key) for key in keys)))

    return asyncio.run(run())


def claude_bin() -> str | None:
    return shutil.which(os.path.expanduser(os.environ.get("CLAUDE_BIN", "").strip() or "claude"))


def detect_orchestrators() -> dict[str, bool]:
    return {
        "opencode": bool(shutil.which("opencode")) or opencode_config_path().parent.is_dir(),
        "claude_code": claude_bin() is not None,
        "antigravity": agy_bin() is not None,
    }


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


def ensure_launcher() -> Path:
    path = launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f'#!/usr/bin/env bash\nexec "{venv_python()}" "{HERE / "intercom.py"}" "$@"\n'
    if not path.exists() or path.read_text() != content:
        path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def launcher_on_path() -> bool:
    entries = [Path(p).expanduser() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return any(str(bin_dir()) == str(entry) for entry in entries)


def path_hint() -> None:
    if not launcher_on_path():
        warn(f'{bin_dir()} is not on PATH; add   export PATH="{bin_dir()}:$PATH"   to your shell profile')


# ---------------------------------------------------------------------------
# Orchestrator registration
# ---------------------------------------------------------------------------


def opencode_entry() -> dict[str, Any]:
    return {
        "type": "local",
        "command": [str(launcher_path()), "serve"],
        "enabled": True,
        "timeout": OPENCODE_TIMEOUT_MS,
    }


def register_opencode(path: Path | None = None) -> str:
    path = path or opencode_config_path()
    jsonc = path.with_suffix(".jsonc")
    if not path.exists() and jsonc.exists():
        snippet = json.dumps({"mcp": {SERVER_KEY: opencode_entry()}}, indent=2)
        return f"{jsonc} is JSONC, which this tool leaves untouched; merge this block yourself:\n{snippet}"
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {path} is not valid JSON ({exc}); fix it and rerun `intercom setup`")
        if not isinstance(data, dict):
            raise SystemExit(f"error: {path} does not contain a JSON object")
    else:
        data["$schema"] = "https://opencode.ai/config.json"
    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise SystemExit(f"error: the `mcp` key in {path} is not an object")
    # Merge onto any existing entry so a hand-added `environment` or other keys survive;
    # only type/command/enabled/timeout are updated.
    current = mcp.get(SERVER_KEY)
    entry = dict(current) if isinstance(current, dict) else {}
    entry.update(opencode_entry())
    existed = isinstance(current, dict)
    mcp[SERVER_KEY] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    verb = "updated" if existed else "registered"
    return f"{verb} `{SERVER_KEY}` in {path} (other settings left untouched)"


def unregister_opencode(path: Path | None = None) -> str:
    path = path or opencode_config_path()
    if not path.exists():
        return "OpenCode config absent; nothing to remove"
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return f"{path} is not valid JSON; remove the `{SERVER_KEY}` entry yourself"
    mcp = data.get("mcp") if isinstance(data, dict) else None
    if not isinstance(mcp, dict) or SERVER_KEY not in mcp:
        return f"`{SERVER_KEY}` was not registered in {path}"
    del mcp[SERVER_KEY]
    path.write_text(json.dumps(data, indent=2) + "\n")
    return f"removed `{SERVER_KEY}` from {path}"


def _claude(*args: str, config_dir: str | None = None) -> subprocess.CompletedProcess[str]:
    binary = claude_bin()
    if not binary:
        raise SystemExit("error: `claude` is not on PATH (set CLAUDE_BIN to its absolute path)")
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return subprocess.run([binary, *args], capture_output=True, text=True, env=env)


def _agy(*args: str) -> subprocess.CompletedProcess[str]:
    binary = agy_bin()
    if not binary:
        raise SystemExit("error: `agy` is not on PATH (set AGY_BIN to its absolute path)")
    return subprocess.run([binary, *args], capture_output=True, text=True)


def register_antigravity() -> str:
    # `agy mcp add` is add-or-update, so it is idempotent.
    result = _agy("mcp", "add", SERVER_KEY, str(launcher_path()), "serve")
    if result.returncode != 0:
        raise SystemExit(f"error: `agy mcp add` failed:\n{(result.stderr or result.stdout).strip()}")
    return f"registered `{SERVER_KEY}` with the Antigravity CLI"


def unregister_antigravity() -> str:
    result = _agy("mcp", "remove", SERVER_KEY)
    if result.returncode != 0:
        return f"`{SERVER_KEY}` was not registered with the Antigravity CLI"
    return f"removed `{SERVER_KEY}` from the Antigravity CLI"


def antigravity_registered() -> bool:
    if not agy_bin():
        return False
    result = _agy("mcp", "list")
    return result.returncode == 0 and SERVER_KEY in result.stdout


def register_claude_profile(config_dir: str) -> str:
    """Register and link intercom for an additional Claude profile (CLAUDE_CONFIG_DIR)."""
    _claude_add(config_dir, f"Claude profile {config_dir}")
    base = Path(os.path.expanduser(config_dir))
    link_skill(base / "skills" / SKILL_NAME)
    link_agent(base / "agents" / f"{AGENT_NAME}.md", AGENT_SRC["claude_code"])
    return f"registered `{SERVER_KEY}` for Claude profile {config_dir} (server, skill, subagent)"


def unregister_claude_profile(config_dir: str) -> str:
    _claude("mcp", "remove", "-s", "user", SERVER_KEY, config_dir=config_dir)
    base = Path(os.path.expanduser(config_dir))
    unlink_skill(base / "skills" / SKILL_NAME)
    unlink_agent(base / "agents" / f"{AGENT_NAME}.md")
    return f"removed `{SERVER_KEY}` from Claude profile {config_dir}"


def claude_profile_registered(config_dir: str) -> bool:
    if not claude_bin():
        return False
    return _claude("mcp", "get", SERVER_KEY, config_dir=config_dir).returncode == 0


def _claude_add(config_dir: str | None, label: str) -> str:
    """Register the launcher with Claude, replacing an existing entry only when it differs.

    `claude mcp add` refuses a name that exists, so the old code removed first -- which left the
    user with no registration at all whenever the add then failed.
    """
    existing = _claude("mcp", "get", SERVER_KEY, config_dir=config_dir)
    launcher = str(launcher_path())
    if existing.returncode == 0 and launcher in existing.stdout:
        return f"`{SERVER_KEY}` already registered with {label} and pointing at {launcher}"
    if existing.returncode == 0:
        _claude("mcp", "remove", "-s", "user", SERVER_KEY, config_dir=config_dir)
    result = _claude("mcp", "add", "-s", "user", SERVER_KEY, "--", launcher, "serve", config_dir=config_dir)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        recovery = "" if existing.returncode != 0 else (
            f"\nthe previous registration was removed first; restore it with:\n"
            f"  claude mcp add -s user {SERVER_KEY} -- {launcher} serve"
        )
        raise SystemExit(f"error: `claude mcp add` for {label} failed:\n{detail}{recovery}")
    return f"registered `{SERVER_KEY}` with {label}"


def register_claude() -> str:
    return _claude_add(None, "Claude Code (user scope)")


def unregister_claude() -> str:
    result = _claude("mcp", "remove", "-s", "user", SERVER_KEY)
    if result.returncode != 0:
        return f"`{SERVER_KEY}` was not registered with Claude Code"
    return f"removed `{SERVER_KEY}` from Claude Code"


def claude_registered() -> bool:
    if not claude_bin():
        return False
    return _claude("mcp", "get", SERVER_KEY).returncode == 0


# ---------------------------------------------------------------------------
# Skill links
# ---------------------------------------------------------------------------


INSTRUCTION_START = "<!-- INTERCOM_START -->"
INSTRUCTION_END = "<!-- INTERCOM_END -->"

# Kept deliberately short: these files are loaded into every turn of every session.
INSTRUCTIONS_WITH_SUBAGENT = """## Delegating with intercom

Hand implementation work to a harness through the `intercom-delegate` subagent rather than calling the
`intercom` delegate tools from this thread: the subagent holds the harness's long report and returns a
short summary, keeping your context clean. Leave the tool's `flags` empty unless the user names a
model — the harness's own configured model is the tested path."""

INSTRUCTIONS_NO_SUBAGENT = """## Delegating with intercom

When delegating implementation work through the `intercom` tools, leave `flags` empty unless the user
names a model — the harness's own configured model is the tested path. Give scoped briefs: name the
files to read, the acceptance criteria, and the exact test command to run and quote."""


def instruction_file(orchestrator: str) -> Path | None:
    """The always-loaded instruction file each orchestrator reads."""
    return {
        "claude_code": home() / ".claude" / "CLAUDE.md",
        "opencode": xdg_config_home() / "opencode" / "AGENTS.md",
        "antigravity": home() / ".gemini" / "GEMINI.md",
    }.get(orchestrator)


def instruction_body(orchestrator: str) -> str:
    # agy has no subagent mechanism, so it gets the model/brief rules only.
    return INSTRUCTIONS_NO_SUBAGENT if orchestrator == "antigravity" else INSTRUCTIONS_WITH_SUBAGENT


def write_instructions(path: Path, body: str) -> str:
    """Insert or update our marked block, leaving the rest of the file untouched."""
    marked = f"{INSTRUCTION_START}\n{body}\n{INSTRUCTION_END}"
    existing = path.read_text() if path.exists() else ""
    if INSTRUCTION_START in existing and INSTRUCTION_END in existing:
        head, _, rest = existing.partition(INSTRUCTION_START)
        _, _, tail = rest.partition(INSTRUCTION_END)
        updated = head + marked + tail
        if updated == existing:
            return f"instructions already current in {path}"
        verb = "updated"
    else:
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        updated = existing + separator + marked + "\n"
        verb = "added"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated)
    return f"{verb} intercom instructions in {path}"


def remove_instructions(path: Path) -> str:
    if not path.exists():
        return f"no instruction file at {path}"
    existing = path.read_text()
    if INSTRUCTION_START not in existing or INSTRUCTION_END not in existing:
        return f"no intercom instructions in {path}"
    head, _, rest = existing.partition(INSTRUCTION_START)
    _, _, tail = rest.partition(INSTRUCTION_END)
    path.write_text((head.rstrip("\n") + "\n" + tail.lstrip("\n")).strip("\n") + "\n")
    return f"removed intercom instructions from {path}"


def has_instructions(path: Path) -> bool:
    return path.exists() and INSTRUCTION_START in path.read_text()


def skill_targets(orchestrators: list[str]) -> list[Path]:
    """Skill link dirs for the chosen orchestrators. ~/.claude/skills serves Claude Code and
    OpenCode; ~/.agents/skills serves the Antigravity CLI.

    When both Claude Code and OpenCode are selected only ~/.claude/skills is linked, on purpose:
    OpenCode reads that directory too, so linking ~/.config/opencode/skills as well would show
    OpenCode the same skill twice.
    """
    targets: list[Path] = []
    if "claude_code" in orchestrators or not orchestrators:
        targets.append(claude_skills_dir() / SKILL_NAME)
    elif "opencode" in orchestrators:
        targets.append(opencode_skills_dir() / SKILL_NAME)
    if "antigravity" in orchestrators:
        targets.append(agents_skills_dir() / SKILL_NAME)
    return targets


def link_skill(target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == SKILL_SRC.resolve():
            return f"skill already linked at {target}"
        target.unlink()
    elif target.exists():
        return f"{target} exists and is not a symlink; left untouched (delete it and rerun setup to link)"
    target.symlink_to(SKILL_SRC, target_is_directory=True)
    return f"linked skill at {target}"


def unlink_skill(target: Path) -> str:
    if target.is_symlink() and target.resolve() == SKILL_SRC.resolve():
        target.unlink()
        return f"removed skill link {target}"
    return f"no intercom skill link at {target}"


def agent_targets(orchestrators: list[str]) -> list[tuple[Path, Path]]:
    """The delegating subagent, per orchestrator, as (link target, source file). Formats differ
    (Claude uses mcp__intercom__* tool names, OpenCode uses intercom_*), so each gets its own file."""
    pairs: list[tuple[Path, Path]] = []
    if "claude_code" in orchestrators:
        pairs.append((claude_agents_dir() / f"{AGENT_NAME}.md", AGENT_SRC["claude_code"]))
    if "opencode" in orchestrators:
        pairs.append((opencode_agent_dir() / f"{AGENT_NAME}.md", AGENT_SRC["opencode"]))
    return pairs


def link_agent(target: Path, src: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == src.resolve():
            return f"delegating subagent already linked at {target}"
        target.unlink()
    elif target.exists():
        return f"{target} exists and is not a symlink; left untouched (delete it and rerun setup to link)"
    target.symlink_to(src)
    return f"linked delegating subagent at {target}"


def unlink_agent(target: Path) -> str:
    known = {src.resolve() for src in AGENT_SRC.values()}
    if target.is_symlink() and target.resolve() in known:
        target.unlink()
        return f"removed delegating subagent link {target}"
    return f"no intercom delegating subagent link at {target}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_install_launcher(_: argparse.Namespace) -> int:
    say(f"launcher written to {ensure_launcher()}")
    path_hint()
    return 0


def _parse_flag_overrides(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or key not in HARNESS_KEYS:
            raise SystemExit(f"error: --flags expects <harness>=<flags> with harness in {HARNESS_KEYS}, got {item!r}")
        out[key] = value.strip()
    return out


def cmd_setup(args: argparse.Namespace) -> int:
    print(f"intercom setup  (install dir: {HERE})\n")
    launcher = ensure_launcher()
    say(f"launcher: {launcher}")
    path_hint()

    found = detect_harnesses()
    say("harness detection")
    detected = [key for key, path in found.items() if path]
    health = harness_health(detected) if detected else {}
    for key in HARNESS_KEYS:
        if found[key]:
            status, headline = health[key]
            print(f"  {HARNESS_LABELS[key]:<28} {found[key]}  [{status}] {headline}")
        else:
            print(f"  {HARNESS_LABELS[key]:<28} not found on PATH")
    print()

    existing = load_config()
    previous = {
        "orchestrators": [o for o in existing.get("orchestrators", []) if o in ORCHESTRATOR_KEYS],
        "skill_links": [str(link) for link in existing.get("skill_links") or []],
        "agent_links": [str(link) for link in existing.get("agent_links") or []],
        "instruction_files": [str(path) for path in existing.get("instruction_files") or []],
    }
    has_config = config_path().exists()
    if has_config:
        say(f"existing configuration found at {config_path()}; your current selections are preserved as defaults")

    scripted = bool(args.harness or args.orchestrator or args.yes)
    if not interactive() and not scripted:
        raise SystemExit(
            "error: stdin is not a terminal. Run `intercom setup` from a terminal, or pass --yes "
            "(accept detected defaults) and/or explicit --harness / --orchestrator flags."
        )

    saved_harnesses = [h for h in existing.get("harnesses", []) if h in HARNESS_KEYS]
    default_harnesses = saved_harnesses or detected  # first run: detected; re-run: keep your selection
    if args.harness:
        harnesses = list(dict.fromkeys(args.harness))
    elif args.yes or not interactive():
        harnesses = default_harnesses
    else:
        harnesses = multi_select(
            "Which harnesses should be available for delegation?",
            [(key, HARNESS_LABELS[key], key in default_harnesses) for key in HARNESS_KEYS],
        )
    if not harnesses:
        raise SystemExit(
            "error: no harness selected; install at least one of agy, claude, opencode or pi, "
            "then rerun `intercom setup`"
        )
    for key in harnesses:
        if not found[key]:
            warn(f"{HARNESS_LABELS[key]} is enabled but its binary is not on PATH; set {HARNESS_ENV_PREFIX[key]}_BIN or install it")

    flags = _parse_flag_overrides(args.flags)
    if not flags and interactive() and not args.yes:
        print()
        for key in harnesses:
            current = (existing.get("default_flags") or {}).get(key, "")
            flags[key] = ask_text(
                f"Default flags for {HARNESS_LABELS[key]} (e.g. --model sonnet; empty for none)", current
            )
    else:
        for key in harnesses:
            flags.setdefault(key, (existing.get("default_flags") or {}).get(key, ""))

    present = detect_orchestrators()
    saved_orch = [o for o in existing.get("orchestrators", []) if o in ORCHESTRATOR_KEYS]
    default_orch = saved_orch or [key for key in ORCHESTRATOR_KEYS if present[key]]
    if args.orchestrator:
        orchestrators = list(dict.fromkeys(args.orchestrator))
    elif args.yes or not interactive():
        orchestrators = default_orch
    else:
        print()
        orchestrators = multi_select(
            "Which orchestrators should get the MCP server and the skill?",
            [(key, ORCHESTRATOR_LABELS[key], key in default_orch) for key in ORCHESTRATOR_KEYS],
        )

    if args.allowed_dir is not None:
        roots = [str(Path(os.path.expanduser(d)).resolve()) for d in args.allowed_dir]
        env_overrides = dict(existing.get("env") or {})
        if roots:
            env_overrides["BRIDGE_ALLOWED_DIRS"] = os.pathsep.join(roots)
        else:
            env_overrides.pop("BRIDGE_ALLOWED_DIRS", None)
        existing["env"] = env_overrides

    cfg = existing
    cfg.update(
        {
            "version": CONFIG_VERSION,
            "install_dir": str(HERE),
            "harnesses": harnesses,
            "orchestrators": orchestrators,
            "default_flags": {k: v for k, v in flags.items() if k in harnesses},
            "max_depth": int(args.max_depth if args.max_depth is not None else cfg.get("max_depth", 1)),
        }
    )

    print()
    problems = 0
    for dropped in [o for o in previous["orchestrators"] if o not in orchestrators]:
        # A re-run that deselects an orchestrator used to leave its registration, links and
        # instruction block behind, untracked and impossible to uninstall.
        say(f"{ORCHESTRATOR_LABELS[dropped]} was deselected; removing its registration")
        try:
            if dropped == "opencode":
                say(unregister_opencode())
            elif dropped == "claude_code":
                say(unregister_claude())
            else:
                say(unregister_antigravity())
        except SystemExit as exc:
            warn(str(exc))
            problems += 1
    if "opencode" in orchestrators:
        say(register_opencode())
    if "claude_code" in orchestrators:
        try:
            say(register_claude())
        except SystemExit as exc:
            warn(str(exc))
            problems += 1
    if "antigravity" in orchestrators:
        try:
            say(register_antigravity())
        except SystemExit as exc:
            warn(str(exc))
            problems += 1
    links: list[str] = []
    for target in skill_targets(orchestrators):
        message = link_skill(target)
        say(message)
        if message.startswith(("linked", "skill already")):
            links.append(str(target))
    for stale in [link for link in previous["skill_links"] if link not in links]:
        say(unlink_skill(Path(stale)))
    cfg["skill_links"] = links
    agent_links: list[str] = []
    for target, src in agent_targets(orchestrators):
        message = link_agent(target, src)
        say(message)
        if message.startswith(("linked", "delegating subagent already")):
            agent_links.append(str(target))
    for stale in [link for link in previous["agent_links"] if link not in agent_links]:
        say(unlink_agent(Path(stale)))
    cfg["agent_links"] = agent_links

    saved_profiles = [d for d in existing.get("claude_profiles", []) if isinstance(d, str)]
    new_profiles = [os.path.abspath(os.path.expanduser(d)) for d in (args.claude_config_dir or [])]
    profiles = list(dict.fromkeys(saved_profiles + new_profiles))
    kept: list[str] = []
    for profile in profiles:
        try:
            say(register_claude_profile(profile))
            kept.append(profile)
        except SystemExit as exc:
            warn(str(exc))
            problems += 1
    cfg["claude_profiles"] = kept

    instruction_files: list[str] = []
    if not args.no_instructions:
        for orchestrator in orchestrators:
            path = instruction_file(orchestrator)
            if path is None:
                continue
            say(write_instructions(path, instruction_body(orchestrator)))
            instruction_files.append(str(path))
        for profile in kept:
            path = Path(os.path.expanduser(profile)) / "CLAUDE.md"
            say(write_instructions(path, INSTRUCTIONS_WITH_SUBAGENT))
            instruction_files.append(str(path))
        for stale in [path for path in previous["instruction_files"] if path not in instruction_files]:
            say(remove_instructions(Path(stale)))
        cfg["instruction_files"] = instruction_files
    else:
        cfg["instruction_files"] = previous["instruction_files"]

    say(f"configuration saved to {save_config(cfg)}")

    print()
    print("Done. Next steps:")
    print("  1. Restart the orchestrator so it picks up the new MCP server and skill.")
    print(f"  2. Ask it to run check_{harnesses[0]}_health; expect a report starting with [HEALTH: READY].")
    print("  3. Ask it to \"delegate <task> to <harness>\"; the intercom-delegate subagent runs the")
    print("     delegation and returns a summary, keeping the main thread's context clean.")
    print("  4. `intercom doctor` repeats these checks from the shell at any time.")
    if not launcher_on_path():
        print(f"  5. Put {bin_dir()} on PATH so `intercom` resolves in new shells.")
    return 1 if problems else 0


def cmd_doctor(_: argparse.Namespace) -> int:
    cfg = load_config()
    problems = 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {text}")

    print(f"intercom doctor  (install dir: {HERE})")
    print("launcher")
    report(launcher_path().exists(), f"launcher present at {launcher_path()}")
    report(launcher_on_path(), f"{bin_dir()} on PATH")
    report(config_path().exists(), f"configuration at {config_path()}")

    print("harnesses")
    harnesses = [h for h in cfg.get("harnesses", []) if h in HARNESS_KEYS]
    if not harnesses:
        report(False, "no harness enabled; run `intercom setup`")
    else:
        for key, (status, headline) in harness_health(harnesses).items():
            report(status == "READY", f"{HARNESS_LABELS[key]}: {status}: {headline}")

    print("orchestrators")
    orchestrators = cfg.get("orchestrators", [])
    if "opencode" in orchestrators:
        path = opencode_config_path()
        registered = False
        if path.exists():
            try:
                entry = json.loads(path.read_text()).get("mcp", {}).get(SERVER_KEY)
                registered = isinstance(entry, dict) and entry.get("command") == opencode_entry()["command"]
            except (json.JSONDecodeError, AttributeError):
                registered = False
        report(registered, f"OpenCode: `{SERVER_KEY}` registered in {path}")
    if "claude_code" in orchestrators:
        report(claude_registered(), f"Claude Code: `{SERVER_KEY}` registered (claude mcp get {SERVER_KEY})")
    if "antigravity" in orchestrators:
        report(antigravity_registered(), f"Antigravity CLI: `{SERVER_KEY}` registered (agy mcp list)")
    for profile in cfg.get("claude_profiles", []):
        report(claude_profile_registered(profile), f"Claude profile {profile}: `{SERVER_KEY}` registered")
    if not orchestrators and not cfg.get("claude_profiles"):
        report(False, "no orchestrator registered; run `intercom setup`")

    print("skill")
    links = cfg.get("skill_links") or []
    if not links:
        report(False, "no skill link recorded; run `intercom setup`")
    for link in links:
        target = Path(link)
        report(target.is_symlink() and target.resolve() == SKILL_SRC.resolve(), f"skill linked at {target}")

    print("subagent")
    agent_links = cfg.get("agent_links") or []
    known = {src.resolve() for src in AGENT_SRC.values()}
    if not agent_links:
        report(False, "no delegating subagent link recorded; run `intercom setup`")
    for link in agent_links:
        target = Path(link)
        report(target.is_symlink() and target.resolve() in known, f"delegating subagent linked at {target}")

    print("state")
    try:
        server = _server_module()
        journal = server.journal_path()
        state = server.state_dir()
        try:
            state.mkdir(parents=True, exist_ok=True)
            probe = state / ".doctor-write-test"
            probe.write_text("")
            probe.unlink()
            writable = True
        except OSError:
            writable = False
        report(writable, f"state directory writable: {state}")
        runs = server.read_journal()
        size = sum(f.stat().st_size for f in server.runs_dir().glob("*") if f.is_file()) if server.runs_dir().is_dir() else 0
        print(f"  [ok] {len(runs)} run(s) journalled in {journal} ({size / 1024:.0f} KiB of reports)")
        roots = server.allowed_dirs()
        print(f"  [ok] delegation confined to: {', '.join(str(r) for r in roots) if roots else 'anywhere (BRIDGE_ALLOWED_DIRS unset)'}")
        # A crash during an isolated run leaves its worktree on disk and registered in the repo.
        leftovers = sorted((state / "worktrees").glob("*")) if (state / "worktrees").is_dir() else []
        report(not leftovers, f"no leftover isolation worktrees ({len(leftovers)} found)")
        for path in leftovers[:5]:
            print(f"         {path}  -> remove with: git -C <repo> worktree remove --force {path}")
    except SystemExit as exc:
        report(False, f"run journal unavailable: {exc}")

    instruction_files = cfg.get("instruction_files") or []
    if instruction_files:
        print("instructions")
        for path in instruction_files:
            report(has_instructions(Path(path)), f"delegation instructions present in {path}")

    print()
    print("all checks passed" if not problems else f"{problems} check(s) failed")
    return 1 if problems else 0


def cmd_serve(_: argparse.Namespace) -> int:
    env = build_serve_env(load_config(), dict(os.environ))
    os.execve(sys.executable, [sys.executable, str(HERE / "server.py")], env)
    return 0  # pragma: no cover - execve does not return


def cmd_test(_: argparse.Namespace) -> int:
    status = 0
    for suite in ("test_bridge.py", "test_cli.py"):
        say(f"running {suite}")
        status |= subprocess.call([sys.executable, str(HERE / suite)])
    return status


def cmd_update(_: argparse.Namespace) -> int:
    if not (HERE / ".git").exists():
        raise SystemExit(f"error: {HERE} is not a git checkout; update it by hand")
    say(f"updating {HERE}")
    subprocess.run(["git", "-C", str(HERE), "pull", "--ff-only"], check=True)
    say("reinstalling dependency")
    subprocess.run([venv_python(), "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt")], check=True)
    ensure_launcher()
    say("updated; restart the orchestrator to load the new server")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not args.yes and interactive() and not confirm("Remove intercom registrations, skill links, launcher and config?", False):
        return 1
    if "opencode" in cfg.get("orchestrators", []) or opencode_config_path().exists():
        say(unregister_opencode())
    if claude_bin():
        say(unregister_claude())
    if agy_bin():
        say(unregister_antigravity())
    for profile in cfg.get("claude_profiles", []):
        say(unregister_claude_profile(profile))
    for link in cfg.get("skill_links") or [str(t) for t in skill_targets(cfg.get("orchestrators", []))]:
        say(unlink_skill(Path(link)))
    for link in cfg.get("agent_links") or [str(t) for t, _ in agent_targets(cfg.get("orchestrators", []))]:
        say(unlink_agent(Path(link)))
    for path in cfg.get("instruction_files") or []:
        say(remove_instructions(Path(path)))
    if launcher_path().exists():
        launcher_path().unlink()
        say(f"removed launcher {launcher_path()}")
    if config_path().exists():
        config_path().unlink()
        say(f"removed {config_path()}")
    if args.purge:
        if args.yes or (interactive() and confirm(f"Delete the install directory {HERE}?", False)):
            shutil.rmtree(HERE, ignore_errors=True)
            say(f"deleted {HERE}")
        else:
            say(f"kept {HERE}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    server = _server_module()
    records = server.read_journal()
    if args.harness:
        records = [r for r in records if r.get("harness") == args.harness]
    if args.status:
        records = [r for r in records if r.get("status") == args.status]
    print(server._format_runs(records[: args.limit]))
    if not records:
        print(f"(journal: {server.journal_path()})")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    server = _server_module()
    entry, report, patch = server.read_run(args.run_id)
    if entry is None and not report and not patch:
        raise SystemExit(f"error: no run {args.run_id} in {server.journal_path()}; list them with `intercom runs`")
    if args.patch:
        if not patch:
            raise SystemExit(f"error: run {args.run_id} stored no patch (patches are kept for isolate=true runs)")
        print(patch, end="" if patch.endswith("\n") else "\n")
    else:
        print(report or "(the stored report for this run is gone)")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Apply the patch an isolate=true delegation produced to a real working tree."""
    server = _server_module()
    _, _, patch = server.read_run(args.run_id)
    patch_file = server.runs_dir() / f"{args.run_id}.patch"
    if not patch or not patch_file.is_file():
        raise SystemExit(f"error: run {args.run_id} stored no patch (patches are kept for isolate=true runs)")
    repo = Path(os.path.expanduser(args.repo)).resolve()
    check = subprocess.run(["git", "-C", str(repo), "apply", "--check", str(patch_file)], capture_output=True, text=True)
    if check.returncode != 0:
        raise SystemExit(
            f"error: the patch does not apply cleanly to {repo}:\n{(check.stderr or check.stdout).strip()}\n"
            f"inspect it with `intercom show {args.run_id} --patch`"
        )
    result = subprocess.run(["git", "-C", str(repo), "apply", str(patch_file)], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"error: `git apply` failed:\n{(result.stderr or result.stdout).strip()}")
    say(f"applied run {args.run_id} to {repo}; review with `git -C {repo} diff`")
    return 0


def cmd_config(_: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"config file:       {config_path()}{'' if config_path().exists() else '  (not written yet)'}")
    print(f"install dir:       {HERE}")
    print(f"launcher:          {launcher_path()}")
    print(f"opencode config:   {opencode_config_path()}")
    print(f"skill source:      {SKILL_SRC}")
    try:
        server = _server_module()
        print(f"run journal:       {server.journal_path()}")
        print(f"allowed dirs:      {', '.join(str(d) for d in server.allowed_dirs()) or '(anywhere)'}")
    except SystemExit as exc:  # the venv is not usable; paths are still worth printing
        warn(str(exc))
    print()
    print(json.dumps(cfg, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="intercom", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="configure harnesses, orchestrators and the skill")
    setup.add_argument("--harness", action="append", choices=HARNESS_KEYS, help="enable a harness (repeatable)")
    setup.add_argument("--orchestrator", action="append", choices=ORCHESTRATOR_KEYS, help="register with an orchestrator (repeatable)")
    setup.add_argument("--flags", action="append", metavar="HARNESS=FLAGS", help='default flags, e.g. --flags claude_code="--model sonnet"')
    setup.add_argument("--claude-config-dir", action="append", metavar="DIR",
                       help="also register for a Claude profile at this CLAUDE_CONFIG_DIR (repeatable)")
    setup.add_argument("--allowed-dir", action="append", metavar="DIR",
                       help="confine delegation to this directory tree (repeatable; pass none to clear)")
    setup.add_argument("--no-instructions", action="store_true",
                       help="do not add the intercom delegation guidance to CLAUDE.md/AGENTS.md/GEMINI.md")
    setup.add_argument("--max-depth", type=int, default=None, help="delegation depth allowed below the server (default 1)")
    setup.add_argument("--yes", "-y", action="store_true", help="accept detected defaults without prompting")
    setup.set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="check harnesses, registrations and the skill").set_defaults(func=cmd_doctor)
    sub.add_parser("serve", help="run the MCP server on stdio").set_defaults(func=cmd_serve)
    sub.add_parser("test", help="run the test suite").set_defaults(func=cmd_test)
    sub.add_parser("update", help="pull the latest version").set_defaults(func=cmd_update)
    sub.add_parser("config", help="print configuration and paths").set_defaults(func=cmd_config)

    runs = sub.add_parser("runs", help="list recent delegations from the run journal")
    runs.add_argument("--limit", type=int, default=20, help="how many runs to show (default 20)")
    runs.add_argument("--harness", choices=HARNESS_KEYS, help="only runs on this harness")
    runs.add_argument("--status", choices=("success", "failure", "timeout"), help="only runs with this status")
    runs.set_defaults(func=cmd_runs)

    show = sub.add_parser("show", help="print the stored report (or patch) of one run")
    show.add_argument("run_id")
    show.add_argument("--patch", action="store_true", help="print the run's patch instead of its report")
    show.set_defaults(func=cmd_show)

    apply_cmd = sub.add_parser("apply", help="apply an isolated run's patch to a working tree")
    apply_cmd.add_argument("run_id")
    apply_cmd.add_argument("--repo", default=".", help="repository to apply into (default: current directory)")
    apply_cmd.set_defaults(func=cmd_apply)
    sub.add_parser("install-launcher", help=argparse.SUPPRESS).set_defaults(func=cmd_install_launcher)

    uninstall = sub.add_parser("uninstall", help="remove registrations, skill links, launcher and config")
    uninstall.add_argument("--purge", action="store_true", help="also delete the install directory")
    uninstall.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    uninstall.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
