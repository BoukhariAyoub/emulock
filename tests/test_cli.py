"""Tests for the CLI surface added in 0.2: config, `guard`, `init`, `pool`, `version`.

Run: python3 tests/test_cli.py   (no pytest needed; tests/run.sh runs it too)

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
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMULOCK = ROOT / "bin" / "emulock"
sys.path.insert(0, str(ROOT / "libexec"))
import emulock_common as common  # noqa: E402

FAKE_ADB = "#!/bin/sh\n[ \"$1\" = devices ] && echo 'List of devices attached'\nexit 0\n"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "adb").write_text(FAKE_ADB)
        (bin_dir / "adb").chmod(0o755)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        self.avd_home = self.tmp / "avd"
        self.avd_home.mkdir()
        emulator = self.tmp / "sdk" / "emulator" / "emulator"
        emulator.parent.mkdir(parents=True)
        emulator.write_text("#!/bin/sh\n[ \"$1\" = -list-avds ] && ls \"$ANDROID_AVD_HOME\" | sed -n 's/\\.avd$//p'\nexit 0\n")
        emulator.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.home),
            "EMULATOR_LOCK_DIR": str(self.tmp / "locks"), "ANDROID_AVD_HOME": str(self.avd_home),
            "ANDROID_HOME": str(self.tmp / "sdk"), "CLAUDE_CODE_SESSION_ID": "test-session",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", str(EMULOCK), *args], cwd=self.repo, input=stdin,
                              capture_output=True, text=True, env={**self.env, **env})

    def config(self, text: str):
        (self.repo / ".emulock").mkdir(exist_ok=True)
        (self.repo / ".emulock" / "config").write_text(textwrap.dedent(text))

    # --- version + config -------------------------------------------------------------

    def test_version(self):
        self.assertRegex(self.run_cli("version").stdout, r"^emulock \d+\.\d+\.\d+")
        self.assertEqual(self.run_cli("version").stdout, self.run_cli("--version").stdout)

    def test_bash_and_python_read_the_config_the_same_way(self):
        self.config("""\
            # comment
              pool.avd =  spaced_pool   
            pool.build = ./gradlew :app:assembleDebug -Pa=b   # trailing note
            pool.ref = origin/main#not-a-comment
            pool.apk = app/build/app.apk
            pool.avd = second_wins_not
            """)
        cfg = common.Config(self.repo)
        self.assertEqual("spaced_pool", cfg.get("pool.avd"))
        self.assertEqual("./gradlew :app:assembleDebug -Pa=b", cfg.get("pool.build"))
        self.assertEqual("origin/main#not-a-comment", cfg.get("pool.ref"))
        out = self.run_cli("pool", "status")
        self.assertIn("no AVD spaced_pool yet", out.stdout)
        out = self.run_cli("pool", "rebake", "--dry-run")
        self.assertIn("run: ./gradlew :app:assembleDebug -Pa=b\n", out.stdout)
        self.assertIn("fetch origin/main#not-a-comment", out.stdout)
        # The environment beats the file, in both.
        self.assertIn("no AVD env_pool yet", self.run_cli("pool", "status", EMULOCK_POOL_AVD="env_pool").stdout)
        os.environ["EMULOCK_POOL_AVD"] = "env_pool"
        try:
            self.assertEqual("env_pool", common.Config(self.repo).get("pool.avd"))
        finally:
            del os.environ["EMULOCK_POOL_AVD"]

    # --- guard -----------------------------------------------------------------------

    def test_guard_subcommand_is_the_hook(self):
        payload = json.dumps({"session_id": "test-session", "tool_input": {"command": "adb kill-server"}})
        out = self.run_cli("guard", stdin=payload)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual("deny", json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"])
        payload = json.dumps({"session_id": "test-session", "tool_input": {"command": "git status"}})
        self.assertEqual("", self.run_cli("guard", stdin=payload).stdout)

    def test_guard_needs_no_adb(self):
        payload = json.dumps({"session_id": "test-session", "tool_input": {"command": "ls"}})
        out = self.run_cli("guard", stdin=payload, PATH="/usr/bin:/bin")
        self.assertEqual((0, ""), (out.returncode, out.stdout), out.stderr)

    # --- doctor (install) -------------------------------------------------------------

    def test_doctor_sees_the_guard_wired_user_level_or_through_a_project_hook(self):
        self.assertIn("NOT wired", self.run_cli("doctor").stdout)
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "/opt/homebrew/bin/emulock guard"}]}]}}))
        self.assertIn("wired into", self.run_cli("doctor").stdout)
        (self.home / ".claude" / "settings.json").unlink()
        (self.repo / ".claude" / "hooks").mkdir(parents=True)
        (self.repo / ".claude" / "hooks" / "device-guard.sh").write_text("#!/bin/bash\nemulock guard\n")
        (self.repo / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"hooks": [{"command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/device-guard.sh"}]}]}}))
        self.assertIn("through a hook", self.run_cli("doctor").stdout)

    # --- init ------------------------------------------------------------------------

    def settings(self) -> dict:
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def test_init_without_a_terminal_changes_nothing_and_says_why(self):
        out = self.run_cli("init")
        self.assertEqual(2, out.returncode)
        self.assertIn("not written", out.stdout)
        self.assertFalse((self.home / ".claude" / "settings.json").exists())

    def test_init_dry_run_changes_nothing(self):
        out = self.run_cli("init", "--dry-run")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn('"command"', out.stdout)
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
        self.assertFalse((self.home / ".claude" / "skills" / "emulock").exists())

    def test_init_yes_links_the_skill_and_keeps_existing_settings(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"model": "x", "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"command": "other"}]}]}}))
        out = self.run_cli("init", "--yes")
        self.assertEqual(0, out.returncode, out.stderr)
        settings = self.settings()
        self.assertEqual("x", settings["model"])
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertEqual("other", commands[0])
        self.assertTrue(commands[1].endswith("emulock guard"), commands)
        skill = self.home / ".claude" / "skills" / "emulock"
        self.assertTrue(skill.is_symlink())
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertTrue((self.home / ".claude" / "settings.json.bak").exists())
        # Twice is a no-op.
        again = self.run_cli("init", "--yes")
        self.assertIn("already", again.stdout)
        self.assertEqual(settings, self.settings())

    def homebrew_layout(self) -> Path:
        """A Cellar keg plus the prefix symlinks Homebrew makes for it; returns prefix/bin/emulock."""
        keg = self.tmp / "Cellar" / "emulock" / "9.9.9"
        shutil.copytree(ROOT / "bin", keg / "bin")
        shutil.copytree(ROOT / "libexec", keg / "libexec", ignore=shutil.ignore_patterns("__pycache__"))
        (keg / "share" / "emulock").mkdir(parents=True)
        shutil.copytree(ROOT / "hooks", keg / "share" / "emulock" / "hooks")
        shutil.copytree(ROOT / "skills", keg / "share" / "emulock" / "skills")
        prefix = self.tmp / "prefix"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "share").mkdir()
        (prefix / "bin" / "emulock").symlink_to("../../Cellar/emulock/9.9.9/bin/emulock")
        (prefix / "share" / "emulock").symlink_to("../../Cellar/emulock/9.9.9/share/emulock")
        return prefix / "bin" / "emulock"

    def test_init_links_the_skill_through_the_unversioned_prefix(self):
        emulock = self.homebrew_layout()
        out = subprocess.run(["/bin/bash", str(emulock), "init", "--yes", "--no-hook"], cwd=self.repo,
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(0, out.returncode, out.stderr)
        link = os.readlink(self.home / ".claude" / "skills" / "emulock")
        self.assertIn("prefix/share/emulock/skills/emulock", link)
        self.assertNotIn("9.9.9", link)  # an upgrade removes the versioned keg

    def test_init_relinks_a_skill_left_pointing_into_an_old_keg(self):
        emulock = self.homebrew_layout()
        skills = self.home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "emulock").symlink_to(self.tmp / "Cellar" / "emulock" / "0.1.0" / "share" / "emulock" / "skills" / "emulock")
        out = subprocess.run(["/bin/bash", str(emulock), "init", "--yes", "--no-hook"], cwd=self.repo,
                             capture_output=True, text=True, env=self.env)
        self.assertIn("relinked", out.stdout)
        self.assertTrue((skills / "emulock" / "SKILL.md").is_file())

    def test_init_project_copies_the_skill_and_uses_the_path_command(self):
        out = self.run_cli("init", "--project", "--yes")
        self.assertEqual(0, out.returncode, out.stderr)
        settings = json.loads((self.repo / ".claude" / "settings.json").read_text())
        self.assertEqual("emulock guard", settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        skill = self.repo / ".claude" / "skills" / "emulock"
        self.assertFalse(skill.is_symlink())
        self.assertTrue((skill / "SKILL.md").is_file())

    def test_init_refuses_invalid_json(self):
        (self.home / ".claude" / "settings.json").write_text("{ nope")
        out = self.run_cli("init", "--yes", "--no-skill")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("not valid JSON", out.stdout + out.stderr)
        self.assertEqual("{ nope", (self.home / ".claude" / "settings.json").read_text())

    # --- pool ------------------------------------------------------------------------

    def test_pool_bake_dry_run_names_the_setup_hook_and_checks(self):
        self.config("pool.avd = test_pool\npool.verify = lock boot tutorial\n")
        (self.avd_home / "test_pool.avd").mkdir()
        (self.repo / ".emulock" / "pool-setup.sh").write_text("#!/bin/bash\n")
        out = self.run_cli("pool", "bake", "--dry-run")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("pool-setup.sh", out.stdout)
        self.assertIn("verify (lock boot tutorial)", out.stdout)
        self.assertFalse((self.avd_home / "test_pool.avd" / "emulock-pool").exists())  # dry run: no marker

    def test_pool_bake_needs_the_configured_apk(self):
        self.config("pool.apk = app/build/app.apk\npool.build = ./gradlew assembleDebug\n")
        out = self.run_cli("pool", "bake")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("no APK at", out.stderr)
        self.assertIn("./gradlew assembleDebug", out.stderr)

    def test_pool_rebake_needs_a_build_command(self):
        out = self.run_cli("pool", "rebake", "--dry-run")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("set pool.build", out.stderr)

    def test_pool_status_flags_a_writable_snapshot(self):
        snap = self.avd_home / "emulock_pool.avd" / "snapshots" / "golden"
        snap.mkdir(parents=True)
        (self.avd_home / "emulock_pool.avd" / "config.ini").write_text(
            "image.sysdir.1=system-images/android-34/google_apis/arm64-v8a/\n")
        out = self.run_cli("pool", "status")
        self.assertIn("is writable", out.stdout)
        os.chmod(snap, 0o555)
        try:
            self.assertNotIn("is writable", self.run_cli("pool", "status").stdout)
        finally:
            os.chmod(snap, 0o755)


if __name__ == "__main__":
    unittest.main(verbosity=2)
