"""Tests for `emulock evidence`.

Run: python3 tests/test_evidence.py   (no pytest needed; tests/run.sh runs it too)

The script runs for real against a fake `adb` (it records every call and answers
screencap, ls, pull and logcat like a device would), a fake pre-flight doctor, a
scratch lock store and a scratch evidence folder. No device, no network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

EMULOCK = Path(__file__).resolve().parent.parent / "bin" / "emulock"
SERIAL = "emulator-5558"
SESSION = "test-session"

FAKE_ADB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(" ".join(args) + "\\n")
    rest = args[2:]
    if rest[:2] == ["exec-out", "screencap"]:
        sys.stdout.buffer.write(b"\\x89PNG\\r\\n\\x1a\\nfake-image")
    elif rest[:1] == ["pull"]:
        with open(rest[2], "wb") as out:
            out.write(b"fake-mp4-" + rest[1].encode())
    elif rest[:1] == ["shell"] and rest[1].startswith("ls "):
        print(os.environ.get("FAKE_PARTS", "part-2.mp4\\npart-1.mp4\\n.recording"))
    elif rest[:4] == ["logcat", "-d", "-b", "crash"]:
        print(os.environ.get("FAKE_CRASH", ""))
    elif rest[:2] == ["logcat", "-d"]:
        print("W Choreographer: skipped 31 frames")
""")

FAKE_DOCTOR = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json
    print(json.dumps({"avd": "agent_pool", "api": "34", "checks": [
        {"name": "lock", "status": "ok", "detail": "held"},
        {"name": "apk", "status": "ok", "detail": "installed APK is this worktree's debug build (2 min old)"},
        {"name": "locale", "status": "warn", "detail": "'pt-BR'"},
    ]}))
""")


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "adb").write_text(FAKE_ADB)
        (bin_dir / "adb").chmod(0o755)
        (self.tmp / "doctor.py").write_text(FAKE_DOCTOR)
        self.locks = self.tmp / "locks"
        (self.locks / SERIAL).mkdir(parents=True)
        (self.locks / SERIAL / "meta").write_text(f"SERIAL={SERIAL}\nOWNER_ID=claude-code:{SESSION}\n")
        self.evidence = self.tmp / "evidence"
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(self.tmp / "adb.log"),
                    "EMULATOR_LOCK_DIR": str(self.locks), "CLAUDE_CODE_SESSION_ID": SESSION,
                    "DEVICE_EVIDENCE_DIR": str(self.evidence), "DEVICE_EVIDENCE_DOCTOR": str(self.tmp / "doctor.py")}
        self.env.pop("EMULATOR_LOCK_OWNER", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(EMULOCK), "evidence", *args], capture_output=True, text=True, cwd=self.tmp,
                              env={**self.env, **env})

    def calls(self) -> str:
        log = self.tmp / "adb.log"
        return log.read_text() if log.exists() else ""

    def folder(self) -> Path:
        return next(p for p in self.evidence.iterdir() if p.is_dir())

    def record_session(self, **env: str) -> Path:
        self.run_script("start", SERIAL, "--label", "PROJ-1234 cart badge")
        self.run_script("note", SERIAL, "tapped Add to cart twice")
        self.run_script("shot", SERIAL, "Cart shows 2 items!")
        out = self.run_script("stop", SERIAL, **env)
        self.assertEqual(0, out.returncode, out.stderr)
        return self.folder()

    def test_it_refuses_a_device_this_session_has_not_claimed(self):
        (self.locks / SERIAL / "meta").write_text(f"SERIAL={SERIAL}\nOWNER_ID=claude-code:someone-else\n")
        out = self.run_script("start", SERIAL)
        self.assertNotEqual(0, out.returncode)
        self.assertIn("another agent", out.stderr)
        self.assertEqual("", self.calls())

    def test_start_records_the_screen_in_bounded_parts_and_reports_preflight_issues(self):
        out = self.run_script("start", SERIAL, "--label", "cart")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("pre-flight warn: locale", out.stdout)
        calls = self.calls()
        self.assertIn(f"-s {SERIAL} logcat -c", calls)
        self.assertIn("screenrecord --bit-rate 1500000 --time-limit 180", calls)
        self.assertIn("[ $i -le 10 ]", calls)
        self.assertTrue((self.evidence / f".active-{SERIAL}").exists())

    def test_a_second_start_on_the_same_device_is_refused(self):
        self.run_script("start", SERIAL)
        out = self.run_script("start", SERIAL)
        self.assertIn("already recording", out.stderr)

    def test_no_video_skips_the_recorder(self):
        self.run_script("start", SERIAL, "--no-video")
        self.assertNotIn("screenrecord", self.calls())

    def test_shot_and_note_need_an_active_recording(self):
        out = self.run_script("shot", SERIAL, "x")
        self.assertIn("nothing is being recorded", out.stderr)

    def test_stop_pulls_parts_in_order_and_writes_a_summary(self):
        folder = self.record_session()
        self.assertEqual(b"fake-mp4-/sdcard/evidence/part-1.mp4", (folder / "part-1.mp4").read_bytes())
        self.assertTrue((folder / "01-cart-shows-2-items.png").read_bytes().startswith(b"\x89PNG"))
        self.assertIn("pkill -INT screenrecord", self.calls())
        summary = (folder / "summary.md").read_text()
        self.assertIn("## Device check — PROJ-1234 cart badge", summary)
        self.assertIn("installed APK is this worktree's debug build", summary)
        self.assertIn("1. tapped Add to cart twice", summary)
        self.assertIn("![Cart shows 2 items!]({{asset:01-cart-shows-2-items.png}})", summary)
        self.assertIn("[part-1.mp4]({{asset:part-1.mp4}}) · [part-2.mp4]({{asset:part-2.mp4}})", summary)
        self.assertIn("**Crashes during the session:** none", summary)
        self.assertIn("warn · locale", summary)
        self.assertFalse((self.evidence / f".active-{SERIAL}").exists())

    def test_crashes_during_the_session_are_flagged(self):
        folder = self.record_session(FAKE_CRASH="E AndroidRuntime: FATAL EXCEPTION: main\nE AndroidRuntime: boom")
        self.assertIn("⚠️ **Crashes during the session:** 1", (folder / "summary.md").read_text())
        self.assertIn("FATAL EXCEPTION", (folder / "crashes.txt").read_text())

    def test_the_manifest_lists_every_file_to_upload(self):
        folder = self.record_session()
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(["01-cart-shows-2-items.png", "part-1.mp4", "part-2.mp4"],
                         [f["file"] for f in manifest["files"]])
        self.assertEqual({"image/png", "video/mp4"}, {f["content_type"] for f in manifest["files"]})

    def test_the_package_comes_from_the_project_config(self):
        (self.tmp / ".emulock").mkdir()
        (self.tmp / ".emulock" / "config").write_text("package = com.example.app\npackage.beta = com.example.beta\n")
        self.run_script("start", SERIAL, "--variant", "beta", "--no-video")
        folder = self.folder()
        session = json.loads((folder / "session.json").read_text())
        self.assertEqual(("com.example.beta", "beta"), (session["package"], session["variant"]))

    def test_render_fills_in_uploaded_urls(self):
        folder = self.record_session()
        assets = self.tmp / "assets.json"
        assets.write_text(json.dumps({"01-cart-shows-2-items.png": "https://uploads.example/1.png",
                                      "part-1.mp4": "https://uploads.example/p1.mp4"}))
        out = self.run_script("render", str(folder), "--assets", str(assets))
        self.assertIn("![Cart shows 2 items!](https://uploads.example/1.png)", out.stdout)
        self.assertIn("[part-1.mp4](https://uploads.example/p1.mp4)", out.stdout)
        self.assertIn("no URL for part-2.mp4", out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
