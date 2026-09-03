#!/usr/bin/env python3
"""intercom: command-line front end for the harness bridge MCP server.

    intercom setup       choose harnesses and orchestrators, register the server, link the skill
    intercom doctor      health of the enabled harnesses plus registration checks
    intercom serve       run the MCP server (this is what the orchestrator registrations invoke)
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
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SERVER_KEY = "intercom"
SKILL_NAME = "intercom"
SKILL_SRC = HERE / "skills" / SKILL_NAME
HARNESS_KEYS = ("antigravity", "claude_code")
HARNESS_LABELS = {"antigravity": "Antigravity CLI (agy)", "claude_code": "Claude Code (claude)"}
HARNESS_ENV_PREFIX = {"antigravity": "AGY", "claude_code": "CLAUDE"}
ORCHESTRATOR_KEYS = ("opencode", "claude_code")
ORCHESTRATOR_LABELS = {"opencode": "OpenCode", "claude_code": "Claude Code"}
OPENCODE_TIMEOUT_MS = 1_200_000
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

    async def run() -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for key in keys:
            report = await server.health(server.HARNESSES[key])
            first = report.splitlines()[0]
            status = first[first.find(":") + 1 : first.find("]")].strip()
            out[key] = (status, first[first.find("]") + 1 :].strip())
        return out

    return asyncio.run(run())


def claude_bin() -> str | None:
    return shutil.which(os.path.expanduser(os.environ.get("CLAUDE_BIN", "").strip() or "claude"))


def detect_orchestrators() -> dict[str, bool]:
    return {
        "opencode": bool(shutil.which("opencode")) or opencode_config_path().parent.is_dir(),
        "claude_code": claude_bin() is not None,
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
    mcp[SERVER_KEY] = opencode_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return f"registered `{SERVER_KEY}` in {path}"


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


def _claude(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    binary = claude_bin()
    if not binary:
        raise SystemExit("error: `claude` is not on PATH (set CLAUDE_BIN to its absolute path)")
    return subprocess.run([binary, *args], capture_output=True, text=True, check=check)


def register_claude() -> str:
    _claude("mcp", "remove", "-s", "user", SERVER_KEY)  # ignore failure: it may not exist yet
    result = _claude("mcp", "add", "-s", "user", SERVER_KEY, "--", str(launcher_path()), "serve")
    if result.returncode != 0:
        raise SystemExit(f"error: `claude mcp add` failed:\n{(result.stderr or result.stdout).strip()}")
    return f"registered `{SERVER_KEY}` with Claude Code (user scope)"


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


def skill_targets(orchestrators: list[str]) -> list[Path]:
    """Both orchestrators read ~/.claude/skills, so one link serves both when Claude Code is in play."""
    if "claude_code" in orchestrators or not orchestrators:
        return [claude_skills_dir() / SKILL_NAME]
    return [opencode_skills_dir() / SKILL_NAME]


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

    scripted = bool(args.harness or args.orchestrator or args.yes)
    if not interactive() and not scripted:
        raise SystemExit(
            "error: stdin is not a terminal. Run `intercom setup` from a terminal, or pass --yes "
            "(accept detected defaults) and/or explicit --harness / --orchestrator flags."
        )

    if args.harness:
        harnesses = list(dict.fromkeys(args.harness))
    elif args.yes or not interactive():
        harnesses = detected
    else:
        harnesses = ask_multi(
            "Which harnesses should be available for delegation?",
            [(key, HARNESS_LABELS[key], bool(found[key])) for key in HARNESS_KEYS],
        )
    if not harnesses:
        raise SystemExit("error: no harness selected; install agy and/or claude, then rerun `intercom setup`")
    for key in harnesses:
        if not found[key]:
            warn(f"{HARNESS_LABELS[key]} is enabled but its binary is not on PATH; set {HARNESS_ENV_PREFIX[key]}_BIN or install it")

    flags = _parse_flag_overrides(args.flags)
    existing = load_config()
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
    if args.orchestrator:
        orchestrators = list(dict.fromkeys(args.orchestrator))
    elif args.yes or not interactive():
        orchestrators = [key for key in ORCHESTRATOR_KEYS if present[key]]
    else:
        print()
        orchestrators = ask_multi(
            "Which orchestrators should get the MCP server and the skill?",
            [(key, ORCHESTRATOR_LABELS[key], present[key]) for key in ORCHESTRATOR_KEYS],
        )

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
    if "opencode" in orchestrators:
        say(register_opencode())
    if "claude_code" in orchestrators:
        try:
            say(register_claude())
        except SystemExit as exc:
            warn(str(exc))
            problems += 1
    links: list[str] = []
    for target in skill_targets(orchestrators):
        message = link_skill(target)
        say(message)
        if message.startswith(("linked", "skill already")):
            links.append(str(target))
    cfg["skill_links"] = links
    say(f"configuration saved to {save_config(cfg)}")

    print()
    print("Done. Next steps:")
    print("  1. Restart the orchestrator so it picks up the new MCP server and skill.")
    print(f"  2. Ask it to run check_{harnesses[0]}_health; expect a report starting with [HEALTH: READY].")
    print("  3. `intercom doctor` repeats these checks from the shell at any time.")
    if not launcher_on_path():
        print(f"  4. Put {bin_dir()} on PATH so `intercom` resolves in new shells.")
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
    if not orchestrators:
        report(False, "no orchestrator registered; run `intercom setup`")

    print("skill")
    links = cfg.get("skill_links") or []
    if not links:
        report(False, "no skill link recorded; run `intercom setup`")
    for link in links:
        target = Path(link)
        report(target.is_symlink() and target.resolve() == SKILL_SRC.resolve(), f"skill linked at {target}")

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
    for link in cfg.get("skill_links") or [str(t) for t in skill_targets(cfg.get("orchestrators", []))]:
        say(unlink_skill(Path(link)))
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


def cmd_config(_: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"config file:       {config_path()}{'' if config_path().exists() else '  (not written yet)'}")
    print(f"install dir:       {HERE}")
    print(f"launcher:          {launcher_path()}")
    print(f"opencode config:   {opencode_config_path()}")
    print(f"skill source:      {SKILL_SRC}")
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
    setup.add_argument("--max-depth", type=int, default=None, help="delegation depth allowed below the server (default 1)")
    setup.add_argument("--yes", "-y", action="store_true", help="accept detected defaults without prompting")
    setup.set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="check harnesses, registrations and the skill").set_defaults(func=cmd_doctor)
    sub.add_parser("serve", help="run the MCP server on stdio").set_defaults(func=cmd_serve)
    sub.add_parser("test", help="run the test suite").set_defaults(func=cmd_test)
    sub.add_parser("update", help="pull the latest version").set_defaults(func=cmd_update)
    sub.add_parser("config", help="print configuration and paths").set_defaults(func=cmd_config)
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
