"""Tests for `emuriad doctor <serial>` against a fake adb.

Run: python3 tests/test_doctor.py   (no pytest needed; tests/run.sh runs it too)

Nothing here touches a real device, the real lock store, or a real worktree: the
fake adb answers like a healthy device unless a test overrides an answer,
and every test gets a scratch lock store plus a throwaway git repo with two
worktrees so the apk check can tell "built here" from "built elsewhere".
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "libexec"))
import emuriad_common as common  # noqa: E402
import emuriad_doctor as doctor  # noqa: E402

SERIAL = "emulator-5599"
PKG = "com.example.app"
DEVICE_APK = "/data/app/~~a==/com.example.app-b==/base.apk"
APK_GLOB = "app/build/outputs/apk/*/*/*.apk"
SESSION = "test-session"
QUIET_HOST = lambda: doctor.Check("host-load", doctor.OK, "load 1.0 on 12 cores")  # noqa: E731


class FakeAdb:
    """Answers `adb -s SERIAL <args>` from a table; an unknown command fails the test."""

    def __init__(self, apk_bytes: bytes, serial: str = SERIAL, **overrides: str):
        import hashlib

        self.serial = serial
        self.calls: list[str] = []
        self.answers = {
            "get-state": "device",
            "shell getprop sys.boot_completed": "1",
            "shell getprop ro.build.version.sdk": "35",
            "emu avd name": "test_avd\nOK",
            f"shell pm path {PKG}": f"package:{DEVICE_APK}",
            f"shell stat -c %s {DEVICE_APK}": str(len(apk_bytes)),
            f"shell sha256sum {DEVICE_APK}": f"{hashlib.sha256(apk_bytes).hexdigest()}  {DEVICE_APK}",
            "shell settings get global private_dns_mode": "off",
            "shell ping -c 1 -W 2 google.com": "PING google.com (142.250.1.1) 56(84) bytes of data.",
            "shell settings get system system_locales": "en-US",
            f"shell dumpsys package {PKG}": "    android.permission.POST_NOTIFICATIONS: granted=true, flags=[ USER_SET ]",
            f"shell pm get-app-links {PKG}": (
                f"  {PKG}:\n    ID: 1234\n    Signatures: [AA:BB]\n    Domain verification state:\n"
                "      example.com: approved\n      www.example.com: verified"
            ),
            "shell dumpsys window": f"  mCurrentFocus=Window{{a1b2 u0 {PKG}/com.example.app.MainActivity}}",
            "shell dumpsys input_method": "  mInputShown=false",
        }
        self.answers.update(overrides)

    def __call__(self, argv: list[str]) -> str:
        assert argv[:3] == ["adb", "-s", self.serial], argv
        key = " ".join(argv[3:])
        self.calls.append(key)
        if key not in self.answers:
            if key.startswith(("shell settings put", "shell pm grant", "shell pm set-app-links")):
                return ""  # --fix writes
            raise AssertionError(f"unexpected adb call: {key}")
        return self.answers[key]


def sh(*argv: str, cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # scratch lock store: SERIAL held by this session
        self.locks = self.tmp / "locks"
        self.write_lock(SERIAL, owner=f"claude-code:{SESSION}", avd="test_avd")
        common.LOCK_ROOT = self.locks
        os.environ["CLAUDE_CODE_SESSION_ID"] = SESSION
        os.environ.pop("EMULATOR_LOCK_OWNER", None)

        # throwaway repo + a second worktree, each with its own staging-debug build output
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        sh("git", "config", "user.email", "t@t", cwd=self.repo)
        sh("git", "config", "user.name", "t", cwd=self.repo)
        (self.repo / "Feature.kt").write_text("v1\n")
        sh("git", "add", "Feature.kt", cwd=self.repo)
        sh("git", "commit", "-q", "-m", "init", cwd=self.repo)
        self.other = self.tmp / "other-branch"
        sh("git", "worktree", "add", "-q", "-b", "other-branch", str(self.other), cwd=self.repo)

        self.here_apk = self.write_apk(self.repo, b"this worktree's build")
        self.other_apk = self.write_apk(self.other, b"the other branch's build, longer")

    def write_lock(self, serial: str, owner: str, avd: str, branch: str = "main"):
        (self.locks / serial).mkdir(parents=True, exist_ok=True)
        (self.locks / serial / "meta").write_text(
            f"SERIAL={serial}\nAVD={avd}\nOWNER_ID={owner}\nOWNER_BRANCH={branch}\n")

    def write_apk(self, worktree: Path, content: bytes) -> Path:
        apk = worktree / "app/build/outputs/apk/staging/debug/app-staging-debug.apk"
        apk.parent.mkdir(parents=True)
        apk.write_bytes(content)
        future = time.time() + 60  # built after HEAD
        os.utime(apk, (future, future))
        return apk

    def context(self, serial: str = SERIAL, **config: str):
        values = {"package": PKG, "apk.glob": APK_GLOB, **config}
        return doctor.build_context(serial, None, None, self.repo, common.Config(self.repo, values=values))

    def run_doctor(self, serial: str = SERIAL, apk: bytes | None = None, plugin: Path | None = None,
                   config: dict | None = None, **overrides: str):
        fake = FakeAdb(apk if apk is not None else self.here_apk.read_bytes(), **overrides)
        device = doctor.Device(serial, run=fake)
        checks, identity = doctor.run_checks(device, self.context(serial, **(config or {})), plugin,
                                             host_load=QUIET_HOST)
        return {c.name: c for c in checks}, checks, fake, device

    # --- lock + boot ------------------------------------------------------------------

    def test_a_healthy_device_reports_nothing_to_fix(self):
        by_name, checks, _, _ = self.run_doctor()
        self.assertEqual([], [(c.name, c.status, c.detail) for c in checks if c.status in ("warn", "fail")])
        self.assertIn("the debug build in", by_name["apk"].detail)

    def test_an_unclaimed_serial_stops_before_any_adb_call(self):
        (self.locks / SERIAL / "meta").unlink()
        by_name, checks, fake, _ = self.run_doctor()
        self.assertEqual(["lock"], [c.name for c in checks])
        self.assertEqual("fail", by_name["lock"].status)
        self.assertEqual([], fake.calls)

    def test_another_agents_serial_stops_before_any_adb_call(self):
        self.write_lock(SERIAL, owner="claude-code:someone-else", avd="test_avd", branch="feature/theirs")
        by_name, checks, fake, _ = self.run_doctor()
        self.assertEqual("fail", by_name["lock"].status)
        self.assertIn("feature/theirs", by_name["lock"].detail)
        self.assertEqual([], fake.calls)

    def test_a_serial_now_serving_a_different_avd_is_flagged(self):
        by_name, *_ = self.run_doctor(**{"emu avd name": "Pixel_9\nOK"})
        self.assertEqual("warn", by_name["boot"].status)
        self.assertIn("reclaim", by_name["boot"].fix)

    def test_a_device_still_booting_stops_the_run(self):
        by_name, checks, *_ = self.run_doctor(**{"shell getprop sys.boot_completed": ""})
        self.assertEqual("fail", by_name["boot"].status)
        self.assertEqual(["lock", "boot"], [c.name for c in checks])

    # --- the golden phone behind a pool instance --------------------------------------

    def pool_instance(self, baked_days_ago: float | None, commit: str = "427b4ed", record: bool = True):
        (self.locks / SERIAL / "meta").write_text(
            f"SERIAL={SERIAL}\nAVD=agent_pool\nOWNER_ID=claude-code:{SESSION}\nPOOL=1\n")
        avd = self.tmp / "avd" / "agent_pool.avd"
        (avd / "snapshots" / "golden").mkdir(parents=True, exist_ok=True)
        common.AVD_HOME = self.tmp / "avd"
        if baked_days_ago is None:
            return
        when = time.time() - baked_days_ago * 86400
        if record:
            from datetime import datetime, timezone
            stamp = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            (avd / "golden.json").write_text(json.dumps({"baked_at": stamp, "commit": commit}))
        else:
            os.utime(avd / "snapshots" / "golden", (when, when))

    def test_a_pool_instance_reports_its_golden_phone(self):
        self.pool_instance(baked_days_ago=1)
        by_name, *_ = self.run_doctor(**{"emu avd name": "agent_pool\nOK"})
        self.assertEqual("ok", by_name["golden"].status)
        self.assertIn("'golden' baked 1 day(s) ago from 427b4ed", by_name["golden"].detail)

    def test_a_stale_golden_phone_warns_with_the_rebake_fix(self):
        self.pool_instance(baked_days_ago=10)
        by_name, *_ = self.run_doctor(**{"emu avd name": "agent_pool\nOK"})
        self.assertEqual("warn", by_name["golden"].status)
        self.assertIn("10 days old", by_name["golden"].detail)
        self.assertIn("emuriad pool rebake", by_name["golden"].fix)

    def test_without_a_record_the_snapshot_age_counts(self):
        self.pool_instance(baked_days_ago=9, record=False)
        by_name, *_ = self.run_doctor(**{"emu avd name": "agent_pool\nOK"})
        self.assertEqual("warn", by_name["golden"].status)

    def test_a_windowed_instance_reports_golden_window(self):
        self.pool_instance(baked_days_ago=None)
        meta = self.locks / SERIAL / "meta"
        meta.write_text(meta.read_text() + "POOL_SNAPSHOT=golden-window\n")
        by_name, *_ = self.run_doctor(**{"emu avd name": "agent_pool\nOK"})
        self.assertEqual("warn", by_name["golden"].status)  # only golden exists on disk
        self.assertIn("'golden-window'", by_name["golden"].detail)
        self.assertEqual("emuriad pool bake --window", by_name["golden"].fix)

    def test_a_named_avd_gets_no_golden_check(self):
        by_name, *_ = self.run_doctor()
        self.assertNotIn("golden", by_name)

    # --- the installed build ----------------------------------------------------------

    def test_an_apk_built_in_another_worktree_is_named(self):
        by_name, *_ = self.run_doctor(apk=self.other_apk.read_bytes())
        self.assertEqual("fail", by_name["apk"].status)
        self.assertIn("other-branch", by_name["apk"].detail)

    def test_a_build_older_than_head_is_stale(self):
        past = time.time() - 3600
        os.utime(self.here_apk, (past, past))
        by_name, *_ = self.run_doctor()
        self.assertEqual("warn", by_name["apk"].status)
        self.assertIn("predates HEAD", by_name["apk"].detail)

    def test_uncommitted_edits_after_the_build_are_stale(self):
        (self.repo / "Feature.kt").write_text("v2\n")
        later = time.time() + 120
        os.utime(self.repo / "Feature.kt", (later, later))
        by_name, *_ = self.run_doctor()
        self.assertEqual("warn", by_name["apk"].status)
        self.assertIn("Feature.kt", by_name["apk"].detail)

    def test_tooling_and_doc_edits_do_not_make_a_build_stale(self):
        for rel in (".claude/scripts/tool.sh", "CLAUDE.md"):
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("v1\n")
            sh("git", "add", rel, cwd=self.repo)
        sh("git", "commit", "-q", "-m", "tooling", cwd=self.repo)
        later = time.time() + 120
        os.utime(self.here_apk, (later - 60, later - 60))  # built after that commit
        for rel in (".claude/scripts/tool.sh", "CLAUDE.md"):
            (self.repo / rel).write_text("v2\n")
            os.utime(self.repo / rel, (later, later))
        by_name, *_ = self.run_doctor()
        self.assertEqual("ok", by_name["apk"].status, by_name["apk"].detail)

    def test_an_app_that_is_not_installed_fails(self):
        by_name, *_ = self.run_doctor(**{f"shell pm path {PKG}": ""})
        self.assertEqual("fail", by_name["apk"].status)

    def test_an_apk_matching_no_local_build_fails(self):
        by_name, *_ = self.run_doctor(apk=b"something from Firebase App Distribution")
        self.assertEqual("fail", by_name["apk"].status)
        self.assertIn("matches no local build", by_name["apk"].detail)

    # --- device settings --------------------------------------------------------------

    def test_broken_dns_fails_and_fix_turns_private_dns_off(self):
        by_name, checks, fake, device = self.run_doctor(**{
            "shell settings get global private_dns_mode": "null",
            "shell ping -c 1 -W 2 google.com": "ping: unknown host google.com",
        })
        self.assertEqual("fail", by_name["dns"].status)
        self.assertEqual(["dns"], doctor.apply_fixes(device, checks))
        self.assertIn("shell settings put global private_dns_mode off", fake.calls)
        self.assertEqual("ok", by_name["dns"].status)

    def test_a_physical_devices_dns_is_never_touched(self):
        fake = FakeAdb(self.here_apk.read_bytes(), serial="R58M123ABC",
                       **{"shell settings get global private_dns_mode": "hostname"})
        checks, _ = doctor.run_checks(doctor.Device("R58M123ABC", run=fake), self.context("R58M123ABC"),
                                      host_load=QUIET_HOST)
        by_name = {c.name: c for c in checks}
        self.assertNotIn("emu avd name", fake.calls)
        self.assertEqual("skip", by_name["lock"].status)
        self.assertEqual("info", by_name["dns"].status)
        self.assertEqual([], by_name["dns"].fix_cmds)

    def test_a_non_us_locale_warns_and_the_first_preference_wins(self):
        for locales, status in (("pt-BR", "warn"), ("pt-BR,en-US", "warn"), ("en-US,pt-BR", "ok")):
            with self.subTest(locales=locales):
                by_name, *_ = self.run_doctor(**{"shell settings get system system_locales": locales})
                self.assertEqual(status, by_name["locale"].status)

    def test_an_unset_locale_list_falls_back_to_the_persisted_one(self):
        by_name, *_ = self.run_doctor(**{
            "shell settings get system system_locales": "null",
            "shell getprop persist.sys.locale": "pt-BR",
        })
        self.assertEqual("warn", by_name["locale"].status)

    def test_missing_notification_permission_warns_with_a_grant_fix(self):
        by_name, *_ = self.run_doctor(**{
            f"shell dumpsys package {PKG}": "    android.permission.POST_NOTIFICATIONS: granted=false, flags=[]",
        })
        self.assertEqual("warn", by_name["notifications"].status)
        self.assertEqual([["shell", "pm", "grant", PKG, "android.permission.POST_NOTIFICATIONS"]],
                         by_name["notifications"].fix_cmds)

    def test_notification_permission_is_not_checked_below_api_33(self):
        by_name, *_ = self.run_doctor(**{"shell getprop ro.build.version.sdk": "32"})
        self.assertNotIn("notifications", by_name)

    def test_unapproved_app_link_domains_warn(self):
        by_name, *_ = self.run_doctor(**{
            f"shell pm get-app-links {PKG}": "  x:\n    Domain verification state:\n      example.com: 1024\n"
                                             "      www.example.com: verified",
        })
        self.assertEqual("warn", by_name["app-links"].status)
        self.assertIn("example.com", by_name["app-links"].detail)
        self.assertNotIn("www.example.com", by_name["app-links"].detail)


    # --- what is on top ---------------------------------------------------------------

    def test_overlays_that_eat_taps_are_flagged(self):
        cases = {
            "permission dialog": {"shell dumpsys window": "  mCurrentFocus=Window{9 u0 com.google.android."
                                  "permissioncontroller/com.android.permissioncontroller.permission.ui."
                                  "GrantPermissionsActivity}"},
            "shade": {"shell dumpsys window": "  mCurrentFocus=Window{9 u0 NotificationShade}"},
            "keyboard": {"shell dumpsys input_method": "  mInputShown=true"},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                _, checks, *_ = self.run_doctor(**overrides)
                on_top = [c for c in checks if c.name == "on-top"]
                self.assertEqual(["warn"], [c.status for c in on_top])

    def test_host_load_above_the_threshold_warns(self):
        self.assertEqual("warn", doctor.check_host_load((30.0, 20.0, 10.0), cores=12).status)
        self.assertEqual("ok", doctor.check_host_load((4.0, 4.0, 4.0), cores=12).status)

    # --- configuration ----------------------------------------------------------------

    def test_without_a_package_the_app_checks_are_skipped(self):
        by_name, *_ = self.run_doctor(config={"package": ""})
        for name in ("apk", "notifications", "app-links"):
            self.assertNotIn(name, by_name)
        self.assertIn("dns", by_name)

    def test_the_expected_locale_is_configurable(self):
        overrides = {"shell settings get system system_locales": "pt-BR"}
        by_name, *_ = self.run_doctor(config={"locale": "pt-BR"}, **overrides)
        self.assertEqual("ok", by_name["locale"].status)
        by_name, *_ = self.run_doctor(config={"locale": "any"}, **overrides)
        self.assertEqual("skip", by_name["locale"].status)

    def test_a_variant_picks_its_own_package_and_build_glob(self):
        config = common.Config(self.repo, values={
            "package": "com.example.other", "package.staging": PKG,
            "apk.glob.staging": "app/build/outputs/apk/staging/*/*.apk",
        })
        ctx = doctor.build_context(SERIAL, "staging", None, self.repo, config)
        self.assertEqual(PKG, ctx.package)
        checks, _ = doctor.run_checks(doctor.Device(SERIAL, run=FakeAdb(self.here_apk.read_bytes())), ctx,
                                      host_load=QUIET_HOST)
        self.assertEqual("ok", {c.name: c for c in checks}["apk"].status)
        self.assertEqual(["staging"], config.variants())

    # --- project checks ---------------------------------------------------------------

    def write_plugin(self, body: str) -> Path:
        path = self.repo / ".emuriad" / "doctor.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
        return path

    def test_a_project_plugin_adds_its_checks_before_host_load(self):
        plugin = self.write_plugin(
            "from emuriad_doctor import Check, WARN\n"
            "def checks(device, ctx):\n"
            "    focus = device.shell('dumpsys window')\n"
            "    return [Check('tutorial', WARN, f'{ctx.package} api {ctx.api}: ' + ('main' if 'Main' in focus else '?'))]\n")
        by_name, checks, *_ = self.run_doctor(plugin=plugin)
        self.assertEqual(f"{PKG} api 35: main", by_name["tutorial"].detail)
        self.assertEqual(["tutorial", "host-load"], [c.name for c in checks][-2:])

    def test_a_broken_plugin_is_reported_and_the_rest_still_runs(self):
        plugin = self.write_plugin("def checks(device, ctx):\n    raise RuntimeError('boom')\n")
        by_name, *_ = self.run_doctor(plugin=plugin)
        self.assertEqual("warn", by_name["project"].status)
        self.assertIn("boom", by_name["project"].detail)
        self.assertIn("dns", by_name)

    def test_a_missing_plugin_is_fine(self):
        by_name, *_ = self.run_doctor(plugin=self.repo / ".emuriad" / "doctor.py")
        self.assertNotIn("project", by_name)

    # --- modes ------------------------------------------------------------------------

    def test_dry_run_plans_commands_without_running_any(self):
        def refuse(argv):
            raise AssertionError(f"dry run executed {argv}")

        device = doctor.Device(SERIAL, run=refuse, dry_run=True)
        doctor.run_checks(device, self.context(), host_load=QUIET_HOST)
        self.assertIn(f"adb -s {SERIAL} get-state", device.planned)
        self.assertIn(f"adb -s {SERIAL} shell pm path {PKG}", device.planned)

    def test_json_output_and_exit_code(self):
        (self.locks / SERIAL / "meta").unlink()
        out = StringIO()
        with redirect_stdout(out):
            code = doctor.main([SERIAL, "--json", "--worktree", str(self.repo)])
        payload = json.loads(out.getvalue())
        self.assertEqual(1, code)
        self.assertEqual("fail", payload["checks"][0]["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
