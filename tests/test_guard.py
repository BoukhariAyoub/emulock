"""Tests for the guard rules that need more than tests/run.sh's one-liners.

Run: python3 tests/test_guard.py   (no pytest needed; tests/run.sh runs it too)

The hook gates EVERY shell command on the machine, and a parse error in it blocks
all shell access for every session. So these run it exactly as Claude Code does
(a JSON payload on stdin) under /bin/bash 3.2, the oldest bash it must survive,
against a scratch lock store and a scratch AVD home.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "hooks" / "claude-code" / "emulock-guard.sh"
SESSION = "test-session"
ME = f"claude-code:{SESSION}"
MINE, THEIRS = "emulator-5599", "emulator-5601"
EMULATOR = "~/Library/Android/sdk/emulator/emulator"
POOL = "agent_pool"
# What `emulock claim --pool` prints, on this session's port.
POOL_BOOT = (f"{EMULATOR} @{POOL} -port 5599 -no-boot-anim -read-only -snapshot golden "
             "-force-snapshot-load -no-snapshot-save -no-window")
# What `emulock pool bake` runs: a writable cold boot that loads no snapshot.
BAKE_BOOT = f"{EMULATOR} @{POOL} -port 5599 -no-snapshot-load -no-snapshot-save -no-boot-anim -no-window"


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.locks = self.tmp / "locks"
        for serial, owner in ((MINE, ME), (THEIRS, "claude-code:someone-else")):
            path = self.locks / serial
            path.mkdir(parents=True)
            (path / "meta").write_text(f"SERIAL={serial}\nOWNER_ID={owner}\nOWNER_BRANCH=feature/theirs\n")
            (path / "last_used").touch()
        self.avd_home = self.tmp / "avd"
        (self.avd_home / f"{POOL}.avd").mkdir(parents=True)
        (self.avd_home / f"{POOL}.avd" / "emulock-pool").touch()   # left by `emulock pool bake`
        (self.avd_home / f"{POOL}_old.avd").mkdir(parents=True)    # an ordinary AVD

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def decide(self, command: str, **env: str) -> str:
        payload = json.dumps({"session_id": SESSION, "tool_input": {"command": command}})
        base = {k: v for k, v in os.environ.items() if not k.startswith(("EMULOCK_", "EMULATOR_POOL"))}
        out = subprocess.run(
            ["/bin/bash", str(GUARD)], input=payload, capture_output=True, text=True,
            env={**base, "EMULATOR_LOCK_DIR": str(self.locks), "ANDROID_AVD_HOME": str(self.avd_home),
                 "CLAUDE_CODE_SESSION_ID": SESSION, **env},
        )
        self.assertEqual(0, out.returncode, out.stderr)
        if not out.stdout.strip():
            return "allow"
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        self.last_reason = decision["permissionDecisionReason"]
        return decision["permissionDecision"]

    # --- pool boots are headless only -------------------------------------------------

    def test_the_printed_pool_boot_is_allowed(self):
        self.assertEqual("allow", self.decide(POOL_BOOT))

    def test_a_pool_boot_with_a_window_is_denied(self):
        self.assertEqual("deny", self.decide(POOL_BOOT.replace(" -no-window", "")))
        self.assertIn("headless only", self.last_reason)
        self.assertIn("emulock claim --avd", self.last_reason)

    def test_a_pool_boot_with_a_gpu_flag_is_denied(self):
        for gpu in ("-gpu host", "-gpu swiftshader_indirect", "--gpu host", "-gpu=host"):
            with self.subTest(gpu=gpu):
                self.assertEqual("deny", self.decide(f"{POOL_BOOT} {gpu}"))

    def test_every_windowed_pool_boot_that_loads_a_snapshot_is_denied(self):
        cases = [
            POOL_BOOT.replace(f"@{POOL}", f"-avd {POOL}").replace(" -no-window", ""),
            f"emulator @{POOL} -port 5599 -read-only",
            f"{EMULATOR} @{POOL} -port 5599 -snapshot golden",  # writable, still loads golden
        ]
        for command in cases:
            with self.subTest(command=command):
                self.assertEqual("deny", self.decide(command))

    def test_the_bake_boot_stays_allowed(self):
        self.assertEqual("allow", self.decide(BAKE_BOOT))

    def test_a_boot_chained_after_a_claim_is_still_checked(self):
        command = f"emulock claim --pool && {POOL_BOOT.replace(' -no-window', '')}"
        self.assertEqual("deny", self.decide(command))

    def test_flags_count_only_in_the_pool_boots_own_segment(self):
        windowed = POOL_BOOT.replace(" -no-window", "")
        self.assertEqual("deny", self.decide(f"{windowed}; echo -no-window"))
        self.assertEqual("deny", self.decide(f"{windowed}  # dropped -no-window so someone can watch"))
        self.assertEqual("deny", self.decide(f"{windowed} > /tmp/emu.log 2>&1"))
        self.assertEqual("allow", self.decide(f"{POOL_BOOT} > /tmp/emu.log 2>&1"))
        self.assertEqual("allow", self.decide(f"({POOL_BOOT})"))

    def test_a_boot_split_over_continuation_lines_is_read_whole(self):
        head, tail = POOL_BOOT.split(" -read-only ")
        self.assertEqual("allow", self.decide(f"{head} \\\n  -read-only {tail}"))
        self.assertEqual("deny", self.decide(f"{head} \\\n  -read-only {tail.replace(' -no-window', '')}"))

    def test_other_avds_and_prose_are_not_pool_boots(self):
        cases = [
            f"{EMULATOR} @medium_phone -port 5599 -no-snapshot-load -no-boot-anim",
            f"{EMULATOR} @{POOL}_old -port 5599 -read-only",
            f"git commit -m 'never boot emulator @{POOL} -read-only without -no-window'",
        ]
        for command in cases:
            with self.subTest(command=command):
                self.assertEqual("allow", self.decide(command))

    def test_a_pool_avd_is_recognised_by_its_golden_record_or_the_environment(self):
        legacy = self.avd_home / "legacy_pool.avd"
        legacy.mkdir()
        (legacy / "golden.json").write_text("{}")
        self.assertEqual("deny", self.decide(f"emulator @legacy_pool -port 5599 -read-only"))
        self.assertEqual("deny", self.decide(f"emulator @named_pool -port 5599 -read-only",
                                             EMULOCK_POOL_AVD="named_pool"))

    # --- Gradle device tasks ------------------------------------------------------------

    def test_gradle_device_tasks_need_an_owned_serial(self):
        cases = {
            "./gradlew :app:installDebug": "deny",
            "./gradlew uninstallAll": "deny",
            "./gradlew connectedDebugAndroidTest": "deny",
            f"ANDROID_SERIAL={MINE} ./gradlew :app:installDebug": "allow",
            f"ANDROID_SERIAL={THEIRS} ./gradlew :app:installDebug": "deny",
            f"ANDROID_SERIAL={MINE},{THEIRS} ./gradlew :app:installDebug": "deny",
            "ANDROID_SERIAL=$SERIAL ./gradlew :app:installDebug": "deny",
            "./gradlew :app:assembleDebug": "allow",
            "./gradlew testDebugUnitTest": "allow",
            "./gradlew tasks | grep installDebug": "allow",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(expected, self.decide(command))

    # --- emulock itself -----------------------------------------------------------------

    def test_only_a_command_that_is_one_emulock_call_skips_the_checks(self):
        self.assertEqual("allow", self.decide("emulock status"))
        self.assertEqual("allow", self.decide("/opt/homebrew/bin/emulock release emulator-5599"))
        self.assertEqual("allow", self.decide("EMULATOR_LOCK_OWNER=x emulock claim --pool"))
        # Merely mentioning the word used to let anything through.
        self.assertEqual("deny", self.decide(f"cd ~/src/emulock && adb -s {THEIRS} shell ls"))
        self.assertEqual("deny", self.decide(f"emulock status; adb -s {THEIRS} shell ls"))

    # --- rules that must keep holding ------------------------------------------------

    def test_existing_device_rules_still_hold(self):
        cases = {
            "adb kill-server": "deny",
            f"adb -s {MINE} shell ls": "allow",
            f"adb -s {THEIRS} shell ls": "deny",
            "adb shell pm list packages": "deny",
            "git status": "allow",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(expected, self.decide(command))


if __name__ == "__main__":
    unittest.main(verbosity=2)
