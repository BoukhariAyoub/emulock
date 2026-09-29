#!/usr/bin/env python3
"""emulock evidence: record proof of an on-device test, for the people reviewing the change.

When an agent tests on a (virtual) phone, the only record is usually its own word,
and reviewers ask for more ("did you run it on a device? I didn't see it"). This
captures what they would want to see, while the agent works:

  start  records the screen (low bit rate, 3-minute parts), clears logcat, runs the
         pre-flight (emulock doctor) so a broken device shows up before testing
  shot   a full screenshot with a label ("cart shows 1 item")
  note   one line of what the agent did or checked, in its own words
  stop   pulls the video, collects warnings and the crash buffer, runs the pre-flight
         again (it proves the installed build is this checkout's), and writes
         summary.md + manifest.json
  render fills the uploaded files' URLs into summary.md, ready to post

Everything lands in <checkout>/.evidence/<timestamp>-<serial>/ (config: evidence.dir;
keep it gitignored). The checkout is the current directory's, or `start --worktree`;
later commands find the recording by serial, wherever they run from. Posting is up to you: upload each file listed in manifest.json
wherever reviewers look (a ticket, the PR), then `render` the summary with their URLs.

Like the doctor, this runs adb itself, which the guard hook cannot see, so it
checks the device claim first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emulock_common as common  # noqa: E402

REPO_ROOT = Path.cwd()
CONFIG = common.Config(REPO_ROOT, values={})
EVIDENCE_ROOT = REPO_ROOT / ".evidence"


def use_worktree(root: Path) -> None:
    """Everything a recording reads from its checkout: config, git, the evidence folder."""
    global REPO_ROOT, CONFIG, EVIDENCE_ROOT
    REPO_ROOT = common.project_root(root).resolve()
    CONFIG = common.Config(REPO_ROOT)
    EVIDENCE_ROOT = Path(os.environ.get("EMULOCK_EVIDENCE_DIR")
                         or os.environ.get("DEVICE_EVIDENCE_DIR")
                         or REPO_ROOT / CONFIG.get("evidence.dir", ".evidence"))
# Tests swap in a fake doctor script; normally the doctor runs in-process.
DOCTOR = os.environ.get("EMULOCK_EVIDENCE_DOCTOR") or os.environ.get("DEVICE_EVIDENCE_DOCTOR")
DEVICE_DIR = "/sdcard/evidence"
BIT_RATE = 1_500_000          # ~11 MB a minute: small enough to attach
PART_SECONDS = 180            # screenrecord's own ceiling per file
MAX_PARTS = 10                # 30 minutes, then it stops by itself
WARN_LINES = 400
CONTENT_TYPES = {".png": "image/png", ".mp4": "video/mp4", ".txt": "text/plain", ".json": "application/json",
                 ".md": "text/markdown"}


def die(message: str) -> None:
    print(f"emulock evidence: {message}", file=sys.stderr)
    sys.exit(1)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def adb(serial: str, *args: str, binary: bool = False, timeout: int = 60):
    out = subprocess.run(["adb", "-s", serial, *args], capture_output=True, timeout=timeout)
    return out.stdout if binary else out.stdout.decode(errors="replace").replace("\r", "")


def require_device(serial: str) -> None:
    problem = common.claim_problem(serial)
    if problem:
        die(problem)


def not_ignored_warning() -> str | None:
    """The folder holds screenshots of whatever was on screen; it must never be committed."""
    try:
        inside = EVIDENCE_ROOT.resolve().is_relative_to(REPO_ROOT.resolve())
    except (OSError, ValueError):
        return None
    if not inside:
        return None
    probe = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", str(EVIDENCE_ROOT / "probe.png")],
                           capture_output=True)
    if probe.returncode == 1:
        return (f"{EVIDENCE_ROOT} is not gitignored — add `/{EVIDENCE_ROOT.relative_to(REPO_ROOT)}/` to "
                ".gitignore so screenshots never end up in a commit")
    return None


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def pointer(serial: str) -> Path:
    # Keyed by serial, not by checkout: a shell whose cwd moved still finds the recording.
    return common.LOCK_ROOT / ".evidence" / serial


def active(serial: str) -> Path:
    path = pointer(serial)
    if not path.is_file():
        die(f"nothing is being recorded on {serial} — start with: emulock evidence start {serial}")
    folder = Path(path.read_text().strip())
    try:
        use_worktree(Path(json.loads((folder / "session.json").read_text())["worktree"]))
    except (OSError, KeyError, ValueError):
        pass
    return folder


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "shot"


def append(path: Path, record: dict) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def run_doctor(serial: str, package: str, variant: str) -> dict:
    try:
        if DOCTOR:
            argv = [sys.executable, DOCTOR, serial, *([variant] if variant else []), "--package", package,
                    "--json", "--worktree", str(REPO_ROOT)]
            out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            return json.loads(out.stdout)
        import emulock_doctor
        return emulock_doctor.report(serial, variant or None, package or None, REPO_ROOT)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, Exception):  # noqa: BLE001
        return {"checks": [], "error": "pre-flight did not run"}


def problems(report: dict) -> list[dict]:
    return [c for c in report.get("checks", []) if c.get("status") in ("warn", "fail")]


# --- commands ------------------------------------------------------------------------


def start(serial: str, label: str, package: str, variant: str, video: bool) -> None:
    require_device(serial)
    if pointer(serial).exists():
        die(f"already recording on {serial} — stop it first: emulock evidence stop {serial}")
    warning = not_ignored_warning()
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    folder = EVIDENCE_ROOT / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{serial}"
    folder.mkdir(parents=True)
    report = run_doctor(serial, package, variant)
    (folder / "doctor-start.json").write_text(json.dumps(report, indent=2))
    session = {
        "serial": serial, "label": label, "package": package, "variant": variant, "video": video,
        "started_at": now(), "worktree": str(REPO_ROOT),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"), "commit": git("rev-parse", "--short", "HEAD"),
        "avd": report.get("avd", ""), "api": report.get("api", ""),
    }
    (folder / "session.json").write_text(json.dumps(session, indent=2))
    adb(serial, "logcat", "-c")
    if video:
        adb(serial, "shell", f"rm -rf {DEVICE_DIR} && mkdir -p {DEVICE_DIR} && touch {DEVICE_DIR}/.recording")
        loop = (f"i=1; while [ -f {DEVICE_DIR}/.recording ] && [ $i -le {MAX_PARTS} ]; do "
                f"screenrecord --bit-rate {BIT_RATE} --time-limit {PART_SECONDS} {DEVICE_DIR}/part-$i.mp4; "
                f"i=$((i+1)); done")
        adb(serial, "shell", f"nohup sh -c '{loop}' >/dev/null 2>&1 &")
    pointer(serial).parent.mkdir(parents=True, exist_ok=True)
    pointer(serial).write_text(str(folder))
    for issue in problems(report):
        print(f"pre-flight {issue['status']}: {issue['name']} — {issue['detail']}")
    print(f"recording {serial}{' (screen + logs)' if video else ' (logs only)'} → {folder}")
    print(f"next: emulock evidence shot {serial} \"<what it shows>\" · note {serial} \"<what you checked>\" · "
          f"stop {serial}")


def shot(serial: str, label: str) -> None:
    folder = active(serial)
    index = len(read_lines(folder / "shots.jsonl")) + 1
    name = f"{index:02d}-{slug(label)}.png"
    image = adb(serial, "exec-out", "screencap", "-p", binary=True)
    if not image.startswith(b"\x89PNG"):
        die(f"screencap returned no image — is {serial} booted?")
    (folder / name).write_bytes(image)
    append(folder / "shots.jsonl", {"file": name, "label": label, "at": now()})
    print(f"saved {name}")


def note(serial: str, text: str) -> None:
    folder = active(serial)
    append(folder / "notes.jsonl", {"text": text, "at": now()})
    print("noted")


def stop(serial: str) -> None:
    folder = active(serial)
    session = json.loads((folder / "session.json").read_text())
    parts: list[str] = []
    if session.get("video"):
        adb(serial, "shell", f"rm -f {DEVICE_DIR}/.recording; pkill -INT screenrecord")
        for _ in range(20):  # SIGINT lets screenrecord finish writing the file
            if not adb(serial, "shell", "pidof screenrecord").strip():
                break
            time.sleep(0.5)
        listing = adb(serial, "shell", f"ls {DEVICE_DIR}")
        parts = sorted((p for p in listing.split() if p.endswith(".mp4")),
                       key=lambda p: int(re.sub(r"\D", "", p) or 0))
        for part in parts:
            adb(serial, "pull", f"{DEVICE_DIR}/{part}", str(folder / part), timeout=300)
        adb(serial, "shell", f"rm -rf {DEVICE_DIR}")
    crashes = adb(serial, "logcat", "-d", "-b", "crash").strip()
    (folder / "crashes.txt").write_text(crashes + "\n" if crashes else "")
    warnings = adb(serial, "logcat", "-d", "*:W").splitlines()[-WARN_LINES:]
    (folder / "warnings.txt").write_text("\n".join(warnings) + "\n")
    report = run_doctor(serial, session.get("package", ""), session.get("variant", ""))
    (folder / "doctor.json").write_text(json.dumps(report, indent=2))
    session["ended_at"] = now()
    session["parts"] = [p for p in parts if (folder / p).exists()]
    (folder / "session.json").write_text(json.dumps(session, indent=2))
    (folder / "summary.md").write_text(summary(folder, session, report, crashes))
    (folder / "manifest.json").write_text(json.dumps(manifest(folder, session), indent=2))
    pointer(serial).unlink()
    print(f"evidence ready in {folder}")
    print("post it: upload each file in manifest.json where reviewers look, then "
          f"emulock evidence render {folder} --assets <urls.json> and paste the result")


def duration(session: dict) -> str:
    try:
        start_at = datetime.fromisoformat(session["started_at"].replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(session["ended_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return "?"
    seconds = int((end_at - start_at).total_seconds())
    return f"{seconds // 60}m {seconds % 60:02d}s"


def summary(folder: Path, session: dict, report: dict, crashes: str) -> str:
    checks = report.get("checks", [])
    issues = problems(report)
    apk = next((c for c in checks if c.get("name") == "apk"), None)
    device = " · ".join(filter(None, [session.get("avd"), f"API {session['api']}" if session.get("api") else ""]))
    lines = [
        f"## Device check — {session.get('label') or 'on-device verification'}",
        "",
        f"{device or session['serial']} · `{session.get('branch')}` @ `{session.get('commit')}` · "
        f"{session['started_at'][:16].replace('T', ' ')} UTC · {duration(session)}",
        "",
    ]
    if apk:
        mark = "✅" if apk.get("status") == "ok" else "⚠️"
        lines.append(f"{mark} **Build on the device:** {apk.get('detail')}")
    lines.append(f"{'✅' if not crashes else '⚠️'} **Crashes during the session:** "
                 f"{'none' if not crashes else str(crashes.count('FATAL EXCEPTION') or 'see crashes.txt')}")
    ok_count = sum(1 for c in checks if c.get("status") == "ok")
    lines.append(f"{'✅' if not issues else '⚠️'} **Pre-flight:** {ok_count} checks ok"
                 + (f", {len(issues)} to note" if issues else ""))
    for issue in issues:
        lines.append(f"   - {issue['status']} · {issue['name']}: {issue['detail']}")
    notes = read_lines(folder / "notes.jsonl")
    if notes:
        lines += ["", "**What was checked**"]
        lines += [f"{i}. {n['text']}" for i, n in enumerate(notes, 1)]
    shots = read_lines(folder / "shots.jsonl")
    if shots:
        lines += ["", "**Screenshots**", ""]
        for s in shots:
            lines += [f"**{s['label']}**", f"![{s['label']}]({{{{asset:{s['file']}}}}})", ""]
    if session.get("parts"):
        links = " · ".join(f"[{p}]({{{{asset:{p}}}}})" for p in session["parts"])
        lines += ["", f"**Screen recording:** {links}"]
    lines += ["", "---", "Captured with `emulock evidence`; warnings.txt and crashes.txt are "
                         "in the evidence folder."]
    return "\n".join(lines) + "\n"


def manifest(folder: Path, session: dict) -> dict:
    files = []
    for s in read_lines(folder / "shots.jsonl"):
        files.append({"file": s["file"], "kind": "image", "label": s["label"]})
    for part in session.get("parts", []):
        files.append({"file": part, "kind": "video", "label": "screen recording"})
    for entry in files:
        path = folder / entry["file"]
        entry["bytes"] = path.stat().st_size
        entry["content_type"] = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        entry["path"] = str(path)
    return {"folder": str(folder), "summary": str(folder / "summary.md"), "files": files}


def render(folder: Path, assets_path: Path | None) -> None:
    text = (folder / "summary.md").read_text()
    assets = json.loads(assets_path.read_text()) if assets_path else {}
    missing = sorted(set(re.findall(r"\{\{asset:([^}]+)\}\}", text)) - set(assets))
    for name, url in assets.items():
        text = text.replace(f"{{{{asset:{name}}}}}", url)
    if missing:
        print(f"emulock evidence: no URL for {', '.join(missing)} — left as placeholders", file=sys.stderr)
    print(text, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="emulock evidence", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("start", help="begin recording a claimed device")
    p.add_argument("serial")
    p.add_argument("--label", default="", help="what is being verified, e.g. 'PROJ-1234 cart badge'")
    p.add_argument("--variant", default="", help="a variant from .emulock/config (package.<variant>)")
    p.add_argument("--package", default="", help="app id, instead of the configured one")
    p.add_argument("--no-video", action="store_true", help="screenshots, notes and logs only")
    p.add_argument("--worktree", type=Path, default=None,
                   help="the checkout under test (default: the current directory's)")
    p = sub.add_parser("shot", help="save a labelled screenshot")
    p.add_argument("serial")
    p.add_argument("label")
    p = sub.add_parser("note", help="log what you did or checked")
    p.add_argument("serial")
    p.add_argument("text")
    p = sub.add_parser("stop", help="finish: pull the recording, collect logs, write the summary")
    p.add_argument("serial")
    p = sub.add_parser("render", help="print summary.md with the uploaded URLs filled in")
    p.add_argument("folder", type=Path)
    p.add_argument("--assets", type=Path, help='JSON {"01-cart.png": "https://…", …}')
    args = parser.parse_args(argv)

    if args.command == "start":
        use_worktree(args.worktree or Path.cwd())
        package = args.package or CONFIG.for_variant("package", args.variant or None)
        start(args.serial, args.label, package, args.variant, video=not args.no_video)
    elif args.command == "shot":
        shot(args.serial, args.label)
    elif args.command == "note":
        note(args.serial, args.text)
    elif args.command == "stop":
        stop(args.serial)
    elif args.command == "render":
        render(args.folder, args.assets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
