#!/usr/bin/env python3
"""Tests for the `intercom` CLI (intercom.py): setup, doctor, serve, uninstall.

Everything runs against a throwaway HOME with the fake harness binaries from
test_bridge.py, so no real configuration is touched and no quota is consumed.

Run:   python test_cli.py     or:   python -m pytest -q test_cli.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import intercom  # noqa: E402
from test_bridge import CONFIG_KEYS, FAKE_HARNESS, result_text  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


class CliFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="intercom-cli-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.fake_dir = self.tmp / "fakebin"
        self.fake_dir.mkdir()
        self.fake_agy = self._install_fake("agy")
        self.fake_claude = self._install_fake("claude")
        self.fake_opencode = self._install_fake("opencode")
        self.fake_pi = self._install_fake("pi")
        self.bin_dir = self.tmp / "bin"
        self.mcp_log = self.tmp / "mcp.log"
        self.mcp_marker = self.tmp / "mcp.marker"
        self.agy_mcp_log = self.tmp / "agy-mcp.log"
        self.agy_mcp_marker = self.tmp / "agy-mcp.marker"

    def _install_fake(self, name: str) -> Path:
        path = self.fake_dir / name
        path.write_text(FAKE_HARNESS)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **overrides: str) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in CONFIG_KEYS and not k.startswith(("BRIDGE_", "FAKE_", "INTERCOM_", "XDG_", "CLAUDE_CODE_"))
        }
        env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "INTERCOM_BIN_DIR": str(self.bin_dir),
                "AGY_BIN": str(self.fake_agy),
                "CLAUDE_BIN": str(self.fake_claude),
                "OPENCODE_BIN": str(self.fake_opencode),
                "PI_BIN": str(self.fake_pi),
                "FAKE_MCP_LOG": str(self.mcp_log),
                "FAKE_MCP_MARKER": str(self.mcp_marker),
                "FAKE_AGY_MCP_LOG": str(self.agy_mcp_log),
                "FAKE_AGY_MCP_MARKER": str(self.agy_mcp_marker),
                "BRIDGE_KILL_GRACE_SECONDS": "1",
            }
        )
        # Detection uses shutil.which("opencode"); drop any PATH dir that ships it so the
        # host's real OpenCode install cannot leak into tests that assert it is absent.
        env["PATH"] = os.pathsep.join(
            d for d in env.get("PATH", "").split(os.pathsep) if d and not (Path(d) / "opencode").exists()
        )
        env.update(overrides)
        return env

    def run_cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HERE / "intercom.py"), *args],
            env=env or self.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )

    @property
    def opencode_json(self) -> Path:
        return self.home / ".config" / "opencode" / "opencode.json"

    @property
    def config_json(self) -> Path:
        return self.home / ".config" / "intercom" / "config.json"

    @property
    def launcher(self) -> Path:
        return self.bin_dir / "intercom"

    @property
    def skill_link(self) -> Path:
        return self.home / ".claude" / "skills" / "intercom"

    @property
    def claude_agent_link(self) -> Path:
        return self.home / ".claude" / "agents" / "intercom-delegate.md"

    @property
    def opencode_agent_link(self) -> Path:
        return self.home / ".config" / "opencode" / "agent" / "intercom-delegate.md"

    @property
    def agents_skill_link(self) -> Path:
        return self.home / ".agents" / "skills" / "intercom"


@unittest.skipUnless(os.name == "posix", "fake harnesses need a POSIX environment")
class TestSetup(CliFixture):
    def test_scripted_setup_registers_everything(self) -> None:
        self.opencode_json.parent.mkdir(parents=True)
        self.opencode_json.write_text(json.dumps({"theme": "keep-me", "mcp": {"other": {"type": "remote", "url": "x"}}}))
        result = self.run_cli(
            "setup", "--harness", "antigravity", "--harness", "claude_code",
            "--orchestrator", "opencode", "--orchestrator", "claude_code",
            "--flags", "claude_code=--model sonnet", "--max-depth", "2", "--yes",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[READY]", result.stdout)
        self.assertIn("Antigravity CLI (agy)", result.stdout)

        cfg = json.loads(self.config_json.read_text())
        self.assertEqual(cfg["harnesses"], ["antigravity", "claude_code"])
        self.assertEqual(cfg["orchestrators"], ["opencode", "claude_code"])
        self.assertEqual(cfg["default_flags"], {"antigravity": "", "claude_code": "--model sonnet"})
        self.assertEqual(cfg["max_depth"], 2)
        self.assertEqual(cfg["install_dir"], str(HERE))
        self.assertEqual(cfg["skill_links"], [str(self.skill_link)])

        self.assertTrue(self.launcher.exists())
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertIn(str(HERE / "intercom.py"), self.launcher.read_text())

        oc = json.loads(self.opencode_json.read_text())
        self.assertEqual(oc["theme"], "keep-me")
        self.assertIn("other", oc["mcp"])
        self.assertEqual(oc["mcp"]["intercom"]["command"], [str(self.launcher), "serve"])
        self.assertEqual(oc["mcp"]["intercom"]["type"], "local")
        self.assertEqual(oc["mcp"]["intercom"]["timeout"], intercom.OPENCODE_TIMEOUT_MS)

        self.assertIn(f"mcp add -s user intercom -- {self.launcher} serve", self.mcp_log.read_text())
        self.assertTrue(self.mcp_marker.exists())

        self.assertTrue(self.skill_link.is_symlink())
        self.assertEqual(self.skill_link.resolve(), intercom.SKILL_SRC.resolve())
        self.assertTrue((self.skill_link / "SKILL.md").exists())

        # the delegating subagent is linked per orchestrator, each to its own format
        self.assertTrue(self.claude_agent_link.is_symlink())
        self.assertEqual(self.claude_agent_link.resolve(), intercom.AGENT_SRC["claude_code"].resolve())
        self.assertIn("mcp__intercom__delegate_to_antigravity", self.claude_agent_link.read_text())
        self.assertTrue(self.opencode_agent_link.is_symlink())
        self.assertEqual(self.opencode_agent_link.resolve(), intercom.AGENT_SRC["opencode"].resolve())
        self.assertIn("mode: subagent", self.opencode_agent_link.read_text())
        self.assertEqual(set(cfg["agent_links"]), {str(self.claude_agent_link), str(self.opencode_agent_link)})

    def test_yes_uses_detected_defaults(self) -> None:
        result = self.run_cli("setup", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cfg = json.loads(self.config_json.read_text())
        self.assertEqual(cfg["harnesses"], ["antigravity", "claude_code", "opencode", "pi"])  # all fakes resolve
        self.assertEqual(cfg["orchestrators"], ["claude_code", "antigravity"])  # opencode scrubbed; agy detected
        self.assertTrue(self.agy_mcp_marker.exists())  # agy registration happened
        self.assertIn("mcp add intercom", self.agy_mcp_log.read_text())
        self.assertFalse(self.opencode_json.exists())
        self.assertIn("mcp add", self.mcp_log.read_text())
        self.assertTrue(self.skill_link.is_symlink())

    def test_setup_is_idempotent(self) -> None:
        first = self.run_cli("setup", "--yes")
        second = self.run_cli("setup", "--yes")
        self.assertEqual((first.returncode, second.returncode), (0, 0), second.stdout + second.stderr)
        self.assertIn("skill already linked", second.stdout)
        log = self.mcp_log.read_text()
        self.assertEqual(log.count("mcp add"), 2)  # re-registered, after a remove each time
        self.assertGreaterEqual(log.count("mcp remove"), 2)

    def test_rerun_preserves_existing_selection(self) -> None:
        first = self.run_cli("setup", "--harness", "claude_code", "--orchestrator", "claude_code", "--yes")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(json.loads(self.config_json.read_text())["harnesses"], ["claude_code"])
        # A plain --yes re-run must NOT expand to all detected harnesses; it keeps the saved choice.
        second = self.run_cli("setup", "--yes")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("preserved as defaults", second.stdout)
        cfg = json.loads(self.config_json.read_text())
        self.assertEqual(cfg["harnesses"], ["claude_code"])
        self.assertEqual(cfg["orchestrators"], ["claude_code"])

    def test_rerun_preserves_default_flags(self) -> None:
        self.run_cli("setup", "--harness", "antigravity", "--orchestrator", "claude_code",
                     "--flags", "antigravity=--model fake-model-high", "--yes")
        self.run_cli("setup", "--yes")
        cfg = json.loads(self.config_json.read_text())
        self.assertEqual(cfg["default_flags"].get("antigravity"), "--model fake-model-high")

    def test_opencode_entry_merge_keeps_custom_environment(self) -> None:
        self.opencode_json.parent.mkdir(parents=True)
        self.opencode_json.write_text(json.dumps({
            "theme": "keep-me",
            "mcp": {"intercom": {"type": "local", "command": ["/old/intercom", "serve"],
                                 "environment": {"CUSTOM": "x"}, "enabled": False}},
        }))
        result = self.run_cli("setup", "--harness", "antigravity", "--orchestrator", "opencode", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        oc = json.loads(self.opencode_json.read_text())
        entry = oc["mcp"]["intercom"]
        self.assertEqual(entry["environment"], {"CUSTOM": "x"})  # user env preserved
        self.assertEqual(entry["command"], [str(self.launcher), "serve"])  # command updated
        self.assertEqual(entry["timeout"], intercom.OPENCODE_TIMEOUT_MS)
        self.assertEqual(oc["theme"], "keep-me")

    def test_antigravity_orchestrator_registers_via_agy_and_links_agents_skill(self) -> None:
        result = self.run_cli("setup", "--harness", "antigravity", "--orchestrator", "antigravity", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # registered through `agy mcp add`, not opencode.json or `claude mcp`
        self.assertTrue(self.agy_mcp_marker.exists())
        self.assertIn("mcp add intercom", self.agy_mcp_log.read_text())
        self.assertFalse(self.opencode_json.exists())
        # skill linked into ~/.agents/skills (agy reads it); no subagent for agy
        self.assertTrue(self.agents_skill_link.is_symlink())
        self.assertEqual(self.agents_skill_link.resolve(), intercom.SKILL_SRC.resolve())
        self.assertFalse(self.claude_agent_link.exists())
        self.assertFalse(self.opencode_agent_link.exists())
        cfg = json.loads(self.config_json.read_text())
        self.assertEqual(cfg["orchestrators"], ["antigravity"])

    def test_extra_claude_profile_is_registered_tracked_and_removed(self) -> None:
        profile = self.tmp / "claude-b"
        profile.mkdir()
        setup = self.run_cli("setup", "--harness", "claude_code", "--orchestrator", "claude_code",
                             "--claude-config-dir", str(profile), "--yes")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        cfg = json.loads(self.config_json.read_text())
        self.assertEqual(cfg["claude_profiles"], [str(profile)])
        # the profile got its own registration marker, skill and subagent links
        self.assertTrue((profile / ".mcp-intercom").exists())
        self.assertTrue((profile / "skills" / "intercom").is_symlink())
        self.assertTrue((profile / "agents" / "intercom-delegate.md").is_symlink())
        # re-run without the flag preserves the profile
        self.run_cli("setup", "--yes")
        self.assertEqual(json.loads(self.config_json.read_text())["claude_profiles"], [str(profile)])
        # uninstall removes the profile registration and links
        self.run_cli("uninstall", "--yes")
        self.assertFalse((profile / ".mcp-intercom").exists())
        self.assertFalse((profile / "skills" / "intercom").exists())
        self.assertFalse((profile / "agents" / "intercom-delegate.md").exists())

    def test_instructions_are_written_updated_and_removed_idempotently(self) -> None:
        claude_md = self.home / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text("# My rules\n\nKeep my existing guidance.\n")
        agents_md = self.home / ".config" / "opencode" / "AGENTS.md"
        agents_md.parent.mkdir(parents=True)
        agents_md.write_text("# OpenCode rules\n")

        result = self.run_cli("setup", "--harness", "claude_code",
                              "--orchestrator", "claude_code", "--orchestrator", "opencode", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = claude_md.read_text()
        self.assertIn("Keep my existing guidance.", body)  # the user's content survives
        self.assertIn(intercom.INSTRUCTION_START, body)
        self.assertIn("intercom-delegate` subagent", body)
        self.assertIn("Delegating with intercom", agents_md.read_text())
        cfg = json.loads(self.config_json.read_text())
        self.assertIn(str(claude_md), cfg["instruction_files"])

        # re-running does not duplicate the block
        self.run_cli("setup", "--yes")
        self.assertEqual(claude_md.read_text().count(intercom.INSTRUCTION_START), 1)

        # uninstall strips the block and leaves the user's content
        self.run_cli("uninstall", "--yes")
        after = claude_md.read_text()
        self.assertNotIn(intercom.INSTRUCTION_START, after)
        self.assertIn("Keep my existing guidance.", after)

    def test_no_instructions_flag_leaves_files_alone(self) -> None:
        claude_md = self.home / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text("# Mine only\n")
        result = self.run_cli("setup", "--harness", "claude_code", "--orchestrator", "claude_code",
                              "--no-instructions", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(claude_md.read_text(), "# Mine only\n")

    def test_antigravity_gets_the_no_subagent_instruction_variant(self) -> None:
        gemini_md = self.home / ".gemini" / "GEMINI.md"
        result = self.run_cli("setup", "--harness", "antigravity", "--orchestrator", "antigravity", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = gemini_md.read_text()
        self.assertIn("Delegating with intercom", body)
        self.assertNotIn("intercom-delegate` subagent", body)  # agy has no subagent

    def test_non_interactive_setup_without_flags_fails_clearly(self) -> None:
        result = self.run_cli("setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--yes", result.stderr)
        self.assertFalse(self.config_json.exists())

    def test_setup_refuses_unknown_flags_target(self) -> None:
        result = self.run_cli("setup", "--yes", "--flags", "bogus=--x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--flags expects", result.stderr)

    def test_opencode_only_links_skill_into_opencode_dir(self) -> None:
        result = self.run_cli("setup", "--harness", "antigravity", "--orchestrator", "opencode", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        link = self.home / ".config" / "opencode" / "skills" / "intercom"
        self.assertTrue(link.is_symlink())
        self.assertFalse(self.skill_link.exists())
        self.assertFalse(self.mcp_log.exists())
        self.assertTrue(self.opencode_agent_link.is_symlink())  # opencode gets its subagent
        self.assertFalse(self.claude_agent_link.exists())  # claude_code not selected


@unittest.skipUnless(os.name == "posix", "fake harnesses need a POSIX environment")
class TestDoctorAndUninstall(CliFixture):
    def test_doctor_passes_after_setup_and_fails_before(self) -> None:
        before = self.run_cli("doctor")
        self.assertNotEqual(before.returncode, 0)
        self.assertIn("no harness enabled", before.stdout)
        on_path = self.env()
        on_path["PATH"] = f"{self.bin_dir}{os.pathsep}{on_path['PATH']}"
        setup = self.run_cli("setup", "--yes", "--orchestrator", "claude_code", env=on_path)
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        after = self.run_cli("doctor", env=on_path)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertIn("all checks passed", after.stdout)
        self.assertIn("[ok] Claude Code: `intercom` registered", after.stdout)
        self.assertIn("[ok] delegating subagent linked", after.stdout)
        self.assertIn("[ok] Antigravity CLI (agy): READY", after.stdout)

    def test_uninstall_reverses_setup(self) -> None:
        self.opencode_json.parent.mkdir(parents=True)
        self.opencode_json.write_text(json.dumps({"theme": "keep-me"}))
        setup = self.run_cli("setup", "--yes", "--orchestrator", "opencode", "--orchestrator", "claude_code")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        result = self.run_cli("uninstall", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        oc = json.loads(self.opencode_json.read_text())
        self.assertEqual(oc, {"theme": "keep-me", "mcp": {}})
        self.assertIn("mcp remove -s user intercom", self.mcp_log.read_text())
        self.assertFalse(self.mcp_marker.exists())
        self.assertFalse(self.skill_link.exists())
        self.assertFalse(self.claude_agent_link.exists())
        self.assertFalse(self.opencode_agent_link.exists())
        self.assertFalse(self.launcher.exists())
        self.assertFalse(self.config_json.exists())
        self.assertTrue(HERE.exists())  # no --purge: the checkout stays

    def test_config_prints_paths(self) -> None:
        result = self.run_cli("config")
        self.assertEqual(result.returncode, 0)
        self.assertIn(str(self.config_json), result.stdout)
        self.assertIn('"harnesses": []', result.stdout)


class TestSelectors(unittest.TestCase):
    def test_multi_select_falls_back_to_numbered_prompt_without_a_tty(self) -> None:
        import builtins

        options = [("a", "Alpha", True), ("b", "Beta", False)]
        answers = iter(["2"])  # pick option 2 -> key "b"
        saved_raw, saved_input = intercom._RAW_TTY, builtins.input
        intercom._RAW_TTY = False  # force the numbered path regardless of the test tty
        builtins.input = lambda prompt="": next(answers)
        try:
            self.assertEqual(intercom.multi_select("pick", options), ["b"])
        finally:
            intercom._RAW_TTY, builtins.input = saved_raw, saved_input

    def test_numbered_prompt_default_is_the_preselected_options(self) -> None:
        import builtins

        options = [("a", "Alpha", True), ("b", "Beta", False), ("c", "Gamma", True)]
        saved_input = builtins.input
        builtins.input = lambda prompt="": ""  # accept defaults
        try:
            self.assertEqual(intercom.ask_multi("pick", options), ["a", "c"])
        finally:
            builtins.input = saved_input


class TestServeEnv(unittest.TestCase):
    def test_build_serve_env_fills_only_unset_values(self) -> None:
        cfg = {"harnesses": ["claude_code"], "default_flags": {"claude_code": "--model sonnet", "antigravity": ""}, "max_depth": 3}
        env = intercom.build_serve_env(cfg, {"PATH": "/bin", "BRIDGE_MAX_DEPTH": "9"})
        self.assertEqual(env["INTERCOM_HARNESSES"], "claude_code")
        self.assertEqual(env["CLAUDE_DEFAULT_FLAGS"], "--model sonnet")
        self.assertNotIn("AGY_DEFAULT_FLAGS", env)
        self.assertEqual(env["BRIDGE_MAX_DEPTH"], "9")  # the caller's value wins
        self.assertEqual(env["PATH"], "/bin")
        empty = intercom.build_serve_env(intercom.default_config(), {})
        self.assertNotIn("INTERCOM_HARNESSES", empty)
        self.assertEqual(empty["BRIDGE_MAX_DEPTH"], "1")


@unittest.skipUnless(os.name == "posix", "fake harnesses need a POSIX environment")
class TestServe(CliFixture, unittest.IsolatedAsyncioTestCase):
    async def test_launcher_serve_exposes_only_enabled_harness(self) -> None:
        setup = self.run_cli("setup", "--harness", "claude_code", "--orchestrator", "claude_code", "--yes")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        params = StdioServerParameters(command=str(self.launcher), args=["serve"], env=self.env(BRIDGE_LOG_LEVEL="WARNING"))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = sorted(t.name for t in (await session.list_tools()).tools)
                health = result_text(await session.call_tool("check_claude_code_health", {}))
        self.assertEqual(names, ["check_claude_code_health", "delegate_to_claude_code"])
        self.assertTrue(health.startswith("[HEALTH: READY]"), health)


if __name__ == "__main__":
    unittest.main(verbosity=2)
