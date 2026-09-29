"""Tests for the emulock -> emuriad rename (0.3): everything written for emulock keeps working.

Run: python3 tests/test_rename.py   (no pytest needed; tests/run.sh runs it too)

The shims, the old config directory and variables, old hook wiring, old pool AVD
markers, old doctor plugins, and `emuriad init` moving an old install over.
Everything runs against a scratch HOME, lock store, AVD home and git repo, with a
fake `adb` on PATH. Nothing touches ~/.claude, ~/.emulator-locks or a device.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMURIAD = ROOT / "bin" / "emuriad"
EMULOCK = ROOT / "bin" / "emulock"
GUARD_SHIM = ROOT / "hooks" / "claude-code" / "emulock-guard.sh"
sys.path.insert(0, str(ROOT / "libexec"))
import emuriad_common as common  # noqa: E402
import emuriad_doctor as doctor  # noqa: E402

FAKE_ADB = "#!/bin/sh\n[ \"$1\" = devices ] && echo 'List of devices attached'\nexit 0\n"
SESSION = "test-session"
THEIRS = "emulator-5601"


def payload(command: str) -> str:
    return json.dumps({"session_id": SESSION, "tool_input": {"command": command}})


class RenameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "adb").write_text(FAKE_ADB)
        (self.bin / "adb").chmod(0o755)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        self.locks = self.tmp / "locks"
        (self.locks / THEIRS).mkdir(parents=True)
        (self.locks / THEIRS / "meta").write_text(f"SERIAL={THEIRS}\nOWNER_ID=claude-code:someone-else\n")
        (self.locks / THEIRS / "last_used").touch()
        self.avd_home = self.tmp / "avd"
        self.avd_home.mkdir()
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
            "EMULATOR_LOCK_DIR": str(self.locks), "ANDROID_AVD_HOME": str(self.avd_home),
            "ANDROID_HOME": str(self.tmp / "sdk"), "CLAUDE_CODE_SESSION_ID": SESSION,
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, tool: Path, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", str(tool), *args], cwd=self.repo, input=stdin,
                              capture_output=True, text=True, env={**self.env, **env})

    def decision(self, out: subprocess.CompletedProcess) -> str:
        self.assertEqual(0, out.returncode, out.stderr)
        if not out.stdout.strip():
            return "allow"
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        self.last_reason = decision["permissionDecisionReason"]
        return decision["permissionDecision"]

    # --- the emulock shim --------------------------------------------------------------

    def test_shim_runs_emuriad_with_the_same_arguments_and_says_so(self):
        out = self.run_tool(EMULOCK, "version")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(self.run_tool(EMURIAD, "version").stdout, out.stdout)
        self.assertIn("renamed to emuriad", out.stderr)
        self.assertEqual(1, out.stderr.count("\n"), out.stderr)  # one line, not a banner
        status = self.run_tool(EMULOCK, "status")
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn(THEIRS, status.stdout)

    def test_shim_is_silent_as_the_guard_and_still_enforces(self):
        out = self.run_tool(EMULOCK, "guard", stdin=payload(f"adb -s {THEIRS} shell ls"))
        self.assertEqual("deny", self.decision(out))
        self.assertEqual("", out.stderr)
        self.assertEqual("allow", self.decision(self.run_tool(EMULOCK, "guard", stdin=payload("git status"))))

    def test_shim_linked_alone_onto_a_path_finds_emuriad_next_to_its_target(self):
        # An older install.sh linked only emulock into the prefix, pointing into a checkout.
        (self.bin / "emulock").symlink_to(EMULOCK)
        out = subprocess.run(["emulock", "version"], cwd=self.repo, capture_output=True, text=True, env=self.env)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertRegex(out.stdout, r"^emuriad \d+\.\d+\.\d+")

    def test_old_guard_script_path_still_enforces(self):
        out = self.run_tool(GUARD_SHIM, stdin=payload(f"adb -s {THEIRS} shell ls"))
        self.assertEqual("deny", self.decision(out))
        self.assertEqual("", out.stderr)

    # --- the guard -----------------------------------------------------------------------

    def test_guard_lets_either_name_manage_the_lock_store(self):
        guard = ROOT / "hooks" / "claude-code" / "emuriad-guard.sh"
        for command in ("emulock status", "/opt/homebrew/bin/emulock release emulator-5599",
                        "emuriad status", "emulock reap ~/.emulator-locks"):
            with self.subTest(command=command):
                self.assertEqual("allow", self.decision(self.run_tool(guard, stdin=payload(command))))
        # ...but only a command that IS one call: chaining does not smuggle adb through.
        chained = f"emulock status && adb -s {THEIRS} shell ls"
        self.assertEqual("deny", self.decision(self.run_tool(guard, stdin=payload(chained))))

    def test_guard_recognises_a_pool_avd_marked_before_the_rename(self):
        guard = ROOT / "hooks" / "claude-code" / "emuriad-guard.sh"
        (self.avd_home / "old_pool.avd").mkdir()
        (self.avd_home / "old_pool.avd" / "emulock-pool").touch()
        mine = self.locks / "emulator-5599"
        mine.mkdir()
        (mine / "meta").write_text(f"SERIAL=emulator-5599\nOWNER_ID=claude-code:{SESSION}\nAVD=old_pool\n")
        (mine / "last_used").touch()
        windowed = "~/Library/Android/sdk/emulator/emulator @old_pool -port 5599 -read-only -snapshot golden"
        self.assertEqual("deny", self.decision(self.run_tool(guard, stdin=payload(windowed))))
        self.assertIn("snapshot is headless", self.last_reason)

    # --- project config ------------------------------------------------------------------

    def old_config(self, text: str):
        (self.repo / ".emulock").mkdir(exist_ok=True)
        (self.repo / ".emulock" / "config").write_text(text)

    def test_a_project_still_on_dot_emulock_is_read_by_bash_and_python(self):
        self.old_config("pool.avd = legacy_pool\n")
        self.assertEqual("legacy_pool", common.Config(self.repo).get("pool.avd"))
        self.assertIn("no AVD legacy_pool yet", self.run_tool(EMURIAD, "pool", "status").stdout)
        self.assertEqual(self.repo / ".emulock", common.config_dir(self.repo))
        self.assertEqual(".emulock/doctor.py", doctor.default_plugin(self.repo))
        out = self.run_tool(EMURIAD, "doctor").stdout
        self.assertIn(".emulock/config (pre-rename name", out)

    def test_dot_emuriad_wins_when_both_exist(self):
        self.old_config("pool.avd = legacy_pool\n")
        (self.repo / ".emuriad").mkdir()
        (self.repo / ".emuriad" / "config").write_text("pool.avd = new_pool\n")
        self.assertEqual("new_pool", common.Config(self.repo).get("pool.avd"))
        self.assertIn("no AVD new_pool yet", self.run_tool(EMURIAD, "pool", "status").stdout)
        self.assertEqual(".emuriad/doctor.py", doctor.default_plugin(self.repo))

    def test_old_environment_overrides_still_work_and_the_new_ones_win(self):
        self.assertIn("no AVD old_env yet",
                      self.run_tool(EMURIAD, "pool", "status", EMULOCK_POOL_AVD="old_env").stdout)
        both = {"EMULOCK_POOL_AVD": "old_env", "EMURIAD_POOL_AVD": "new_env"}
        self.assertIn("no AVD new_env yet", self.run_tool(EMURIAD, "pool", "status", **both).stdout)
        saved = {k: os.environ.get(k) for k in both}
        try:
            os.environ["EMULOCK_POOL_AVD"] = "old_env"
            self.assertEqual("old_env", common.Config(self.repo).get("pool.avd"))
            os.environ["EMURIAD_POOL_AVD"] = "new_env"
            self.assertEqual("new_env", common.Config(self.repo).get("pool.avd"))
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)

    def test_a_doctor_plugin_importing_the_old_module_name_loads(self):
        plugin = self.tmp / "doctor.py"
        plugin.write_text("from emulock_doctor import OK, Check\n\n"
                          "def checks(device, ctx):\n    return [Check('legacy', OK, 'fine')]\n")
        module = doctor.load_plugin(plugin)
        self.assertEqual("legacy", module.checks(None, None)[0].name)

    # --- doctor (install) ----------------------------------------------------------------

    def test_doctor_counts_an_old_emulock_hook_as_wired(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "/opt/homebrew/bin/emulock guard"}]}]}}))
        self.assertIn("wired into", self.run_tool(EMURIAD, "doctor").stdout)

    # --- init moves an old install -------------------------------------------------------

    def old_install(self) -> Path:
        """A pre-0.3 user install: the emulock hook and a link to emulock's skill."""
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [
            {"matcher": "Edit", "hooks": [{"command": "other"}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/homebrew/bin/emulock guard",
                                           "timeout": 10}]}]}}))
        skills = self.home / ".claude" / "skills"
        skills.mkdir()
        (skills / "emulock").symlink_to("/opt/homebrew/share/emulock/skills/emulock")  # dangling once uninstalled
        return skills

    def settings(self) -> dict:
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def test_init_moves_an_old_install_after_asking(self):
        skills = self.old_install()
        out = self.run_tool(EMURIAD, "init", "--yes")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("pre-0.3 emulock skill", out.stdout)
        self.assertIn("- /opt/homebrew/bin/emulock guard", out.stdout)
        self.assertFalse((skills / "emulock").is_symlink())
        self.assertTrue((skills / "emuriad" / "SKILL.md").is_file())
        settings = self.settings()
        self.assertEqual("x", settings["model"])
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertEqual(2, len(commands), commands)  # replaced in place, not added next to it
        self.assertEqual("other", commands[0])
        self.assertTrue(commands[1].endswith("emuriad guard"), commands)
        self.assertEqual(10, settings["hooks"]["PreToolUse"][1]["hooks"][0]["timeout"])
        self.assertTrue((self.home / ".claude" / "settings.json.bak").exists())
        self.assertIn("already", self.run_tool(EMURIAD, "init", "--yes").stdout)

    def test_init_without_consent_leaves_an_old_install_alone(self):
        skills = self.old_install()
        before = self.settings()
        for args in (("init",), ("init", "--dry-run")):  # no terminal, then a dry run
            with self.subTest(args=args):
                out = self.run_tool(EMURIAD, *args)
                self.assertIn("pre-0.3 emulock skill", out.stdout)
                self.assertTrue((skills / "emulock").is_symlink())
                self.assertEqual(before, self.settings())

    def test_init_leaves_a_skill_named_emulock_that_is_not_ours(self):
        skills = self.home / ".claude" / "skills"
        (skills / "emulock").mkdir(parents=True)
        (skills / "emulock" / "SKILL.md").write_text("---\nname: something-else\n---\n")
        self.run_tool(EMURIAD, "init", "--yes", "--no-hook")
        self.assertTrue((skills / "emulock" / "SKILL.md").is_file())

    def test_init_project_replaces_an_old_skill_copy(self):
        old = self.repo / ".claude" / "skills" / "emulock"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("---\nname: emulock\ndescription: old\n---\n")
        out = self.run_tool(EMURIAD, "init", "--project", "--yes")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertFalse(old.exists())
        self.assertTrue((self.repo / ".claude" / "skills" / "emuriad" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
