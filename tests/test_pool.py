"""Tests for `emuriad check-in --pool`.

Run: python3 tests/test_pool.py   (no pytest needed; tests/run.sh runs it too)
     python3 -m pytest tests -q            (if pytest is installed)

The lock script runs for real, under /bin/bash (3.2, the oldest bash it must
support), against a scratch lock store, a scratch AVD home and fake `adb` /
`emulator` binaries whose devices come from the FAKE_DEVICES environment
variable. Nothing touches ~/.emulator-locks, ~/.android or a real device.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

LOCK_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "emuriad"
ME = "claude-code:test-session"
POOL = "agent_pool"

FAKE_ADB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # FAKE_DEVICES="emulator-5554=device=medium_phone;emulator-5556=offline=agent_pool"
    import os, sys
    devices = [d.split("=") for d in os.environ.get("FAKE_DEVICES", "").split(";") if d]
    args = sys.argv[1:]
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a") as log:
            log.write(" ".join(args) + "\\n")
    if args == ["devices"]:
        print("List of devices attached")
        for serial, state, _ in devices:
            print(f"{serial}\\t{state}")
    elif len(args) >= 4 and args[0] == "-s" and args[2:4] == ["emu", "avd"]:
        print(next((avd for s, _, avd in devices if s == args[1]), ""))
        print("OK")
    elif len(args) >= 3 and args[0] == "-s" and args[2:4] == ["emu", "kill"]:
        print("OK")
""")

FAKE_EMULATOR = textwrap.dedent("""\
    #!/usr/bin/env bash
    [ "$1" = "-list-avds" ] && printf '%s\\n' medium_phone agent_pool Pixel_9
""")


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "adb").write_text(FAKE_ADB)
        sdk_emulator = self.tmp / "sdk" / "emulator"
        sdk_emulator.mkdir(parents=True)
        (sdk_emulator / "emulator").write_text(FAKE_EMULATOR)
        for exe in (bin_dir / "adb", sdk_emulator / "emulator"):
            exe.chmod(0o755)

        self.avd_home = self.tmp / "avd"
        for avd, api in ((POOL, 34), ("medium_phone", 36), ("Pixel_9", 37)):
            (self.avd_home / f"{avd}.avd").mkdir(parents=True)
            (self.avd_home / f"{avd}.avd" / "config.ini").write_text(
                f"image.sysdir.1=system-images/android-{api}/google_apis/arm64-v8a/\n")
        self.snapshot = self.avd_home / f"{POOL}.avd" / "snapshots" / "golden"
        self.snapshot.mkdir(parents=True)

        self.locks = self.tmp / "locks"
        self.locks.mkdir()
        # The project names its pool AVD in .emuriad/config (the tests run from self.tmp).
        (self.tmp / ".emuriad").mkdir()
        (self.tmp / ".emuriad" / "config").write_text("# test project\npool.avd = agent_pool\n")
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "ANDROID_HOME": str(self.tmp / "sdk"),
            "ANDROID_AVD_HOME": str(self.avd_home),
            "EMULATOR_LOCK_DIR": str(self.locks),
            "CLAUDE_CODE_SESSION_ID": "test-session",
            "FAKE_DEVICES": "",
            "FAKE_LOG": str(self.tmp / "adb.log"),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lock(self, *args: str, devices: str = "", **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/bash", str(LOCK_SCRIPT), *args], cwd=self.tmp, capture_output=True, text=True,
            env={**self.env, "FAKE_DEVICES": devices, **env},
        )

    def adb_calls(self) -> str:
        log = self.tmp / "adb.log"
        return log.read_text() if log.exists() else ""

    def meta(self, serial: str) -> dict[str, str]:
        text = (self.locks / serial / "meta").read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def write_lock(self, serial: str, owner: str, avd: str, pool: bool):
        (self.locks / serial).mkdir()
        (self.locks / serial / "meta").write_text(
            f"SERIAL={serial}\nAVD={avd}\nOWNER_ID={owner}\nOWNER_BRANCH=feature/x\n"
            f"POOL={1 if pool else 0}\nCREATED_AT=9999999999\n")
        (self.locks / serial / "last_used").touch()

    # --- claiming ---------------------------------------------------------------------

    def test_a_pool_claim_reserves_a_port_and_prints_a_read_only_snapshot_boot(self):
        out = self.lock("claim", "--pool")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("claimed: emulator-5554 (boot needed, pool instance", out.stdout)
        for flag in ("@agent_pool", "-port 5554", "-read-only", "-snapshot golden",
                     "-force-snapshot-load", "-no-snapshot-save", "-no-window"):
            self.assertIn(flag, out.stdout)
        self.assertNotIn("-GuestAngle", out.stdout)  # API 34 boots without the workaround
        self.assertIn("copy it verbatim", out.stdout)  # a window or -gpu deletes golden
        meta = self.meta("emulator-5554")
        self.assertEqual(("1", POOL, ME), (meta["POOL"], meta["AVD"], meta["OWNER_ID"]))
        self.assertFalse((self.locks / f"avd-{POOL}").exists(), "pool claims must not take the AVD lock")

    def test_a_leftover_pool_instance_is_never_reused_and_gets_shut_down(self):
        # running with no lock: its last owner's state is still inside it
        out = self.lock("claim", "--pool", devices=f"emulator-5560=device={POOL}")
        self.assertIn("claimed: emulator-5554 (boot needed, pool instance", out.stdout)
        self.assertFalse((self.locks / "emulator-5560").exists())
        self.assertIn("-s emulator-5560 emu kill", self.adb_calls())

    def test_additional_gets_a_second_instance_for_two_device_tests(self):
        self.write_lock("emulator-5554", ME, POOL, pool=True)
        out = self.lock("claim", "--pool", "--additional", devices=f"emulator-5554=device={POOL}")
        self.assertIn("claimed: emulator-5556 (boot needed, pool instance", out.stdout)

    def test_two_pool_instances_share_the_avd(self):
        self.write_lock("emulator-5554", "claude-code:someone-else", POOL, pool=True)
        out = self.lock("claim", "--pool", devices=f"emulator-5554=device={POOL}")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("claimed: emulator-5556 (boot needed, pool instance", out.stdout)

    def test_the_pool_is_capped(self):
        for port in (5554, 5556, 5558):
            self.write_lock(f"emulator-{port}", f"claude-code:agent-{port}", POOL, pool=True)
        devices = ";".join(f"emulator-{p}=device={POOL}" for p in (5554, 5556, 5558))
        out = self.lock("claim", "--pool", devices=devices)
        self.assertNotEqual(0, out.returncode)
        self.assertIn("the pool is full (3/3)", out.stderr)
        self.assertFalse((self.locks / "emulator-5560").exists())

    def test_the_cap_is_configurable(self):
        self.write_lock("emulator-5554", "claude-code:other", POOL, pool=True)
        out = self.lock("claim", "--pool", devices=f"emulator-5554=device={POOL}", EMULATOR_POOL_MAX="1")
        self.assertIn("the pool is full (1/1)", out.stderr)

    def test_no_golden_snapshot_means_no_boot(self):
        shutil.rmtree(self.snapshot)
        out = self.lock("claim", "--pool")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("emuriad pool bake", out.stderr)
        self.assertEqual([], list(self.locks.glob("emulator-*")))

    def test_a_held_booted_pool_instance_is_returned(self):
        self.write_lock("emulator-5554", ME, POOL, pool=True)
        out = self.lock("claim", "--pool", devices=f"emulator-5554=device={POOL}")
        self.assertIn("claimed: emulator-5554 (already held, pool instance", out.stdout)

    def test_pool_and_avd_are_exclusive(self):
        out = self.lock("claim", "--pool", "--avd", "medium_phone")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("exclusive", out.stderr)

    # --- keeping plain claims and pool claims apart ------------------------------------

    def test_a_plain_claim_never_reuses_a_pool_instance(self):
        out = self.lock("claim", devices=f"emulator-5560=device={POOL}")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertNotIn("emulator-5560", out.stdout)
        self.assertNotIn(POOL, out.stdout)

    def test_a_plain_claim_never_picks_the_pool_avd(self):
        for _ in range(2):  # take medium_phone, then Pixel_9; the pool AVD stays out
            out = self.lock("claim", "--additional")
            self.assertNotIn(f"avd: {POOL}", out.stdout)
        out = self.lock("claim", "--additional")
        self.assertIn("every installed AVD is in use", out.stderr)

    def test_a_plain_claim_while_holding_a_dead_pool_instance_stays_in_the_pool(self):
        self.write_lock("emulator-5554", ME, POOL, pool=True)  # device gone: not in FAKE_DEVICES
        out = self.lock("claim")
        self.assertIn("pool instance emulator-5554 is gone", out.stderr)
        self.assertIn("-read-only", out.stdout)
        self.assertFalse((self.locks / f"avd-{POOL}").exists(), "must not boot the pool AVD writable")

    def test_reclaim_of_a_pool_lock_takes_any_instance(self):
        self.write_lock("emulator-5554", ME, POOL, pool=True)
        out = self.lock("reclaim")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("reclaiming a pool instance", out.stdout)
        self.assertIn("(boot needed, pool instance", out.stdout)

    def test_releasing_a_pool_instance_shuts_it_down(self):
        self.write_lock("emulator-5554", ME, POOL, pool=True)
        out = self.lock("release", "emulator-5554", devices=f"emulator-5554=device={POOL}")
        self.assertIn("shut down pool instance emulator-5554", out.stdout)
        self.assertIn("-s emulator-5554 emu kill", self.adb_calls())

    def test_releasing_a_named_avd_does_not_kill_it(self):
        self.write_lock("emulator-5554", ME, "medium_phone", pool=False)
        self.lock("release", "emulator-5554", devices="emulator-5554=device=medium_phone")
        self.assertNotIn("emu kill", self.adb_calls())

    def test_reaping_a_stale_pool_lock_shuts_the_instance_down(self):
        self.write_lock("emulator-5554", "claude-code:gone", POOL, pool=True)
        os.utime(self.locks / "emulator-5554" / "last_used", (1, 1))
        out = self.lock("reap", devices=f"emulator-5554=device={POOL}", EMULATOR_LOCK_IDLE_TTL="60")
        self.assertIn("reaped stale lock: emulator-5554", out.stdout)
        self.assertIn("-s emulator-5554 emu kill", self.adb_calls())

    def test_releasing_a_pool_instance_leaves_any_avd_lock_alone(self):
        self.write_lock("emulator-5554", ME, POOL, pool=True)
        (self.locks / f"avd-{POOL}").mkdir()  # e.g. a bake in progress holding the AVD
        out = self.lock("release", "emulator-5554")
        self.assertIn("released: emulator-5554", out.stdout)
        self.assertTrue((self.locks / f"avd-{POOL}").exists())

    def test_status_marks_pool_locks_and_reports_the_pool(self):
        self.write_lock("emulator-5554", ME, POOL, pool=True)
        out = self.lock("status", devices=f"emulator-5554=device={POOL}")
        self.assertIn(f"{POOL} (pool)", out.stdout)
        self.assertIn("agent pool: 1/3 claimed", out.stdout)
        self.assertIn("snapshot 'golden' ready", out.stdout)

    def test_a_marked_pool_avd_is_skipped_even_without_config(self):
        (self.tmp / ".emuriad" / "config").unlink()
        (self.avd_home / f"{POOL}.avd" / "emuriad-pool").touch()
        out = self.lock("claim", devices=f"emulator-5560=device={POOL}")
        self.assertNotIn("emulator-5560", out.stdout)  # never handed out by a plain claim

    def test_the_pool_avd_comes_from_the_environment_too(self):
        out = self.lock("--dry-run", "claim", "--pool", EMURIAD_POOL_AVD="other_pool")
        self.assertIn("no 'golden' snapshot on AVD other_pool", out.stderr)

    # --- the windowed snapshot ------------------------------------------------------

    def test_a_window_claim_needs_golden_window(self):
        out = self.lock("claim", "--pool", "--window")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("emuriad pool bake --window", out.stderr)
        self.assertFalse(any(self.locks.iterdir()))

    def test_a_window_claim_boots_golden_window_with_a_window(self):
        (self.avd_home / f"{POOL}.avd" / "snapshots" / "golden-window").mkdir()
        out = self.lock("claim", "--pool", "--window")
        self.assertEqual(0, out.returncode, out.stderr)
        boot = next(line for line in out.stdout.splitlines() if "-read-only" in line)
        self.assertIn("-snapshot golden-window", boot)
        self.assertNotIn("-no-window", boot)
        self.assertNotIn("-gpu", boot)
        self.assertEqual("golden-window", self.meta("emulator-5554")["POOL_SNAPSHOT"])
        self.assertIn("(pool, window)", self.lock("status").stdout)

    def test_reclaim_keeps_the_window_mode(self):
        (self.avd_home / f"{POOL}.avd" / "snapshots" / "golden-window").mkdir()
        self.lock("claim", "--pool", "--window")
        out = self.lock("reclaim")  # the instance is gone (no devices)
        self.assertIn("-snapshot golden-window", out.stdout)

    def test_a_headless_claim_records_golden(self):
        self.lock("claim", "--pool")
        self.assertEqual("golden", self.meta("emulator-5554")["POOL_SNAPSHOT"])

    def test_a_held_headless_instance_is_not_passed_off_as_a_windowed_one(self):
        (self.avd_home / f"{POOL}.avd" / "snapshots" / "golden-window").mkdir()
        self.lock("claim", "--pool")
        out = self.lock("claim", "--pool", "--window", devices=f"emulator-5554=device={POOL}")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("you hold emulator-5554, booted from 'golden'", out.stderr)
        both = self.lock("claim", "--pool", "--window", "--additional", devices=f"emulator-5554=device={POOL}")
        self.assertIn("-snapshot golden-window", both.stdout)

    def test_window_without_pool_is_refused(self):
        out = self.lock("claim", "--window")
        self.assertIn("--window is for pool instances", out.stderr)

    def test_dry_run_claims_nothing(self):
        out = self.lock("claim", "--pool", "--dry-run")
        self.assertIn("[dry-run] boot command:", out.stdout)
        self.assertEqual([], list(self.locks.glob("emulator-*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
