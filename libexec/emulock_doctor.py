#!/usr/bin/env python3
"""emulock doctor <serial>: check a claimed device before on-device work.

Most lost on-device sessions are the device, not the code: the wrong APK
installed (a shell whose cwd reset installed another checkout's build),
opportunistic Private DNS killing name resolution on the emulator's NAT, a
locale that fails English assertions, a permission dialog or the keyboard
swallowing taps a UI driver reports as successful, and a loaded host turning a
43 ms call into ~4 s. Each is usually found only after the time is gone. This
runs every check in one pass, right after `emulock claim`.

Read-only by default. `--fix` applies only safe, reversible device settings
(Private DNS off on emulators, grant POST_NOTIFICATIONS, approve App Link
domains). It never clears data, installs, or reboots.

The lock check runs first and stops everything if this session does not hold
the serial: the guard hook only inspects top-level commands, so a tool that
runs adb itself has to police itself.

Project checks: if <repo>/.emulock/doctor.py exists (config: doctor.plugin), its
`checks(device, ctx)` function runs after the built-in checks and returns more
Check objects. Import them with `from emulock_doctor import Check, OK, WARN, ...`.
It is the repo's own code, so it runs only when you run the doctor there; pass
--no-project to skip it.

Config (<repo>/.emulock/config), all optional:
  package[.<variant>]         app id to check          (no package: app checks are skipped)
  apk.glob[.<variant>]        build outputs, relative  (default: */build/outputs/apk/*/*/*.apk)
  build.command[.<variant>]   shown as the fix         (default: ./gradlew assembleDebug)
  doctor.ignore               paths whose edits never reach the APK (default: .claude/ .github/ docs/ .emulock/)
  doctor.plugin               project checks           (default: .emulock/doctor.py)
  locale                      expected locale          (default: en-US; "any" skips it)
  pool.max_age_days           golden snapshot age that warns (default: 7)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emulock_common as common  # noqa: E402

# A project plugin imports this module by name; make that the running copy, not a second one.
sys.modules.setdefault("emulock_doctor", sys.modules[__name__])

DEFAULT_APK_GLOB = "*/build/outputs/apk/*/*/*.apk"
DEFAULT_IGNORE = ".claude/ .github/ docs/ .emulock/"
# App Link states that mean "the app, not the browser, opens this domain".
APP_LINK_OK = {"verified", "approved", "system_configured", "migrated", "restored"}
# Load per core above which on-device timings stop meaning anything: a 43 ms
# FirebaseApp.initializeApp measured ~3950 ms on a host running two emulators and Gradle.
LOAD_PER_CORE_WARN = 1.5

OK, INFO, WARN, FAIL, SKIP = "ok", "info", "warn", "fail", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str | None = None
    # adb argument lists that --fix runs for this check (safe, reversible settings only).
    fix_cmds: list[list[str]] = field(default_factory=list)


class Stop(Exception):
    """A check found a state in which no further device command should run."""

    def __init__(self, check: Check):
        super().__init__(check.detail)
        self.check = check


Runner = Callable[[list[str]], str]


def subprocess_runner(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return ""
    return (out.stdout + out.stderr).replace("\r", "")


class Device:
    def __init__(self, serial: str, run: Runner = subprocess_runner, dry_run: bool = False):
        self.serial = serial
        self._run = run
        self.dry_run = dry_run
        self.planned: list[str] = []

    def adb(self, *args: str) -> str:
        argv = ["adb", "-s", self.serial, *args]
        if self.dry_run:
            self.planned.append(" ".join(argv))
            return ""
        return self._run(argv).strip()

    def shell(self, command: str) -> str:
        return self.adb("shell", command)

    @property
    def is_emulator(self) -> bool:
        return self.serial.startswith("emulator-")


# --- lock + identity -----------------------------------------------------------------


def check_lock(device: Device) -> Check:
    if not device.is_emulator:
        return Check("lock", SKIP, "physical device — not in the emulator lock store")
    meta = common.read_meta(device.serial)
    claim = "emulock claim --pool (or emulock claim)"
    if meta is None:
        raise Stop(Check("lock", FAIL, f"{device.serial} is not claimed by anyone", fix=claim))
    owner = meta.get("OWNER_ID", "")
    if owner and owner != common.me():
        raise Stop(Check(
            "lock", FAIL,
            f"{device.serial} belongs to another agent (branch {meta.get('OWNER_BRANCH', '?')})",
            fix=f"{claim} — never touch a device you did not claim",
        ))
    if not owner:
        return Check("lock", WARN, "legacy lock with no owner — reclaim it so the guard can protect it", fix=claim)
    return Check("lock", OK, f"held by this session (lock AVD {meta.get('AVD', '?')})")


def check_boot(device: Device) -> tuple[Check, dict[str, str]]:
    identity: dict[str, str] = {}
    state = device.adb("get-state")
    if state != "device" and not device.dry_run:
        raise Stop(Check("boot", FAIL, f"adb reports '{state or 'nothing'}' for {device.serial}",
                         fix="emulock reclaim, then run the boot command it prints"))
    booted = device.shell("getprop sys.boot_completed")
    identity["api"] = device.shell("getprop ro.build.version.sdk")
    if device.is_emulator:
        identity["avd"] = device.adb("emu", "avd", "name").splitlines()[0] if not device.dry_run else ""
    if booted != "1" and not device.dry_run:
        raise Stop(Check("boot", FAIL, "sys.boot_completed is not 1 — still booting",
                         fix="wait for boot (poll getprop sys.boot_completed), then rerun"))
    detail = f"booted, API {identity['api'] or '?'}"
    locked_avd = (common.read_meta(device.serial) or {}).get("AVD")
    avd = identity.get("avd")
    if avd and locked_avd and locked_avd not in ("unknown", avd):
        return Check("boot", WARN,
                     f"{device.serial} now serves AVD {avd}, but the lock is for {locked_avd} — the serial is not the identity",
                     fix="emulock reclaim"), identity
    return Check("boot", OK, detail + (f", AVD {avd}" if avd else "")), identity


def check_golden(device: Device, max_age_days: int) -> Check | None:
    """Only on pool instances: how old is the golden phone this copy was booted from?"""
    meta = common.read_meta(device.serial) or {}
    if meta.get("POOL") != "1":
        return None
    avd_dir = common.AVD_HOME / f"{meta.get('AVD', '')}.avd"
    name = meta.get("POOL_SNAPSHOT") or "golden"   # golden-window for claim --pool --window
    record = avd_dir / f"{name}.json"
    snapshot = avd_dir / "snapshots" / name
    commit = ""
    if record.is_file():
        try:
            data = json.loads(record.read_text())
            baked = datetime.fromisoformat(data["baked_at"].replace("Z", "+00:00")).timestamp()
            commit = data.get("commit", "")
        except (ValueError, KeyError):
            baked = record.stat().st_mtime
    elif snapshot.is_dir():
        baked = snapshot.stat().st_mtime
    else:
        return Check("golden", WARN, f"no '{name}' snapshot found for this pool instance",
                     fix="emulock pool bake" + (" --window" if name == "golden-window" else ""))
    age = int((time.time() - baked) // 86400)
    source = f" from {commit}" if commit and commit != "unknown" else ""
    if age > max_age_days:
        return Check("golden", WARN,
                     f"'{name}' is {age} days old{source} — installs over it get slower and its app data "
                     "drifts from what the code expects",
                     fix="emulock pool rebake when the pool is idle")
    return Check("golden", OK, f"'{name}' baked {age} day(s) ago{source}")


# --- the installed build -------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apk_outputs(worktree: Path, pattern: str) -> list[Path]:
    return sorted(p for p in worktree.glob(pattern) if p.is_file())


def git(worktree: Path, *args: str) -> str:
    # rstrip, not strip: porcelain status lines start with a meaningful space (" M path").
    try:
        return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True,
                              timeout=30).stdout.rstrip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def other_worktrees(worktree: Path) -> list[Path]:
    listing = git(worktree, "worktree", "list", "--porcelain")
    paths = [Path(line.split(" ", 1)[1]) for line in listing.splitlines() if line.startswith("worktree ")]
    return [p for p in paths if p.resolve() != worktree.resolve()]


def find_match(candidates: list[Path], size: int | None, digest: str) -> Path | None:
    for apk in candidates:
        if size is not None and apk.stat().st_size != size:
            continue  # cheap filter: only hash files of the right size
        if sha256_file(apk) == digest:
            return apk
    return None


def affects_build(path: str, ignore: tuple[str, ...]) -> bool:
    """Tooling, docs and CI config never reach the APK, so editing them doesn't make a build stale."""
    return not (path.startswith(ignore) or path.endswith(".md"))


def staleness(worktree: Path, apk: Path, ignore: tuple[str, ...]) -> str | None:
    built = apk.stat().st_mtime
    head_time = git(worktree, "log", "-1", "--format=%ct")
    if head_time.isdigit() and int(head_time) > built:
        return f"the build predates HEAD ({git(worktree, 'log', '-1', '--format=%h %s')})"
    dirty = [line[3:] for line in git(worktree, "status", "--porcelain", "--untracked-files=no").splitlines()
             if affects_build(line[3:], ignore)]
    newer = [p for p in dirty if (worktree / p).is_file() and (worktree / p).stat().st_mtime > built]
    if newer:
        return f"{len(newer)} uncommitted file(s) changed after the build, e.g. {newer[0]}"
    return None


def check_apk(device: Device, ctx: SimpleNamespace) -> Check:
    package, worktree = ctx.package, ctx.worktree
    pattern = ctx.config.for_variant("apk.glob", ctx.variant, DEFAULT_APK_GLOB)
    build = (f"{ctx.config.for_variant('build.command', ctx.variant, './gradlew assembleDebug')}, "
             f"then adb -s {device.serial} install -r <apk>")
    ignore = tuple(ctx.config.get("doctor.ignore", DEFAULT_IGNORE).split())
    paths = [line.removeprefix("package:") for line in device.shell(f"pm path {package}").splitlines()
             if line.startswith("package:")]
    if device.dry_run:
        return Check("apk", SKIP, "dry run")
    if not paths:
        return Check("apk", FAIL, f"{package} is not installed", fix=build)
    base = next((p for p in paths if p.endswith("/base.apk")), paths[0])
    size_text = device.shell(f"stat -c %s {base}")
    size = int(size_text) if size_text.isdigit() else None
    digest = device.shell(f"sha256sum {base}").split(" ")[0]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return Check("apk", WARN, "could not hash the installed APK (no sha256sum on this image)")

    local = apk_outputs(worktree, pattern)
    match = find_match(local, size, digest)
    if match:
        age_min = int((time.time() - match.stat().st_mtime) / 60)
        detail = f"installed APK is the {match.parent.name} build in {worktree} ({age_min} min old)"
        stale = staleness(worktree, match, ignore)
        return Check("apk", WARN, f"{detail}, but {stale}", fix=build) if stale else Check("apk", OK, detail)

    for other in other_worktrees(worktree):
        match = find_match(apk_outputs(other, pattern), size, digest)
        if match:
            branch = git(other, "rev-parse", "--abbrev-ref", "HEAD")
            return Check("apk", FAIL,
                         f"installed APK was built in {other} (branch {branch}), not in {worktree} — "
                         "pass --worktree if that is the checkout you meant", fix=build)
    if not local:
        return Check("apk", WARN, f"no build output matching {pattern} in {worktree}, so the installed code "
                                  "can't be identified", fix=build)
    return Check("apk", FAIL, "installed APK matches no local build output (a CI build, or rebuilt since)", fix=build)


# --- device settings -----------------------------------------------------------------


def check_dns(device: Device) -> Check:
    mode = device.shell("settings get global private_dns_mode")
    if mode == "off":
        return Check("dns", OK, "private DNS off")
    if not device.is_emulator:
        return Check("dns", INFO, f"private DNS '{mode}' on a physical device — left alone")
    fix_cmd = ["shell", "settings", "put", "global", "private_dns_mode", "off"]
    resolves = "unknown host" not in device.shell("ping -c 1 -W 2 google.com")
    if resolves:
        return Check("dns", WARN, f"private DNS '{mode}' (opportunistic) — resolving now, but it fails on the "
                                  "emulator's NAT without warning", fix="--fix", fix_cmds=[fix_cmd])
    return Check("dns", FAIL, f"private DNS '{mode}' and names do not resolve — every backend looks down",
                 fix="--fix", fix_cmds=[fix_cmd])


def check_locale(device: Device, expected: str) -> Check:
    if expected == "any":
        return Check("locale", SKIP, "no expected locale (locale = any)")
    locale = device.shell("settings get system system_locales")
    if locale in ("", "null"):
        locale = device.shell("getprop persist.sys.locale") or device.shell("getprop ro.product.locale")
    first = locale.split(",")[0]
    if first.replace("_", "-") == expected:
        return Check("locale", OK, expected)
    return Check("locale", WARN, f"'{first or 'unknown'}', not {expected} — apps follow it, and UI tests that "
                                 "assert copy fail", fix=f"Settings → System → Languages, or pick a {expected} AVD")


def check_permissions(device: Device, package: str, api: int) -> list[Check]:
    checks = []
    if api >= 33:
        granted = "POST_NOTIFICATIONS: granted=true" in device.shell(f"dumpsys package {package}")
        if granted:
            checks.append(Check("notifications", OK, "POST_NOTIFICATIONS granted"))
        else:
            checks.append(Check(
                "notifications", WARN,
                "not granted — the system dialog pops over the app and eats taps a UI driver reports as successful",
                fix="--fix", fix_cmds=[["shell", "pm", "grant", package, "android.permission.POST_NOTIFICATIONS"]],
            ))
    if api >= 31:
        states = re.findall(r"^\s+([a-z0-9.-]+\.[a-z]+): (\S+)$", device.shell(f"pm get-app-links {package}"), re.M)
        pending = [domain for domain, state in states if state not in APP_LINK_OK]
        if not states:
            checks.append(Check("app-links", SKIP, "no verifiable domains reported"))
        elif pending:
            checks.append(Check(
                "app-links", WARN,
                f"{', '.join(pending)} not approved — https links open in the browser unless you pass -p {package}",
                fix="--fix", fix_cmds=[["shell", "pm", "set-app-links", "--package", package, "2", "all"]],
            ))
        else:
            checks.append(Check("app-links", OK, f"{len(states)} domain(s) open in the app"))
    return checks


def check_on_top(device: Device) -> list[Check]:
    focus = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}]+)\}", device.shell("dumpsys window"))
    focus_name = focus.group(1) if focus else ""
    ime_up = "mInputShown=true" in device.shell("dumpsys input_method")

    checks = []
    if "permissioncontroller" in focus_name.lower() or "GrantPermissions" in focus_name:
        checks.append(Check("on-top", WARN, "a permission dialog has focus — taps land on it, not the app",
                            fix="answer it, or grant ahead of time with --fix"))
    elif "NotificationShade" in focus_name:
        checks.append(Check("on-top", WARN, "the notification shade is open — taps land in it",
                            fix=f"adb -s {device.serial} shell input keyevent 4"))
    if ime_up:
        checks.append(Check("on-top", WARN, "the keyboard is up and covers what a UI driver still reports as tappable",
                            fix=f"adb -s {device.serial} shell input keyevent 4 (ESC does not dismiss it)"))
    if not checks:
        checks.append(Check("on-top", OK, f"focus: {focus_name or 'unknown'}"))
    return checks


def check_host_load(load: tuple[float, float, float] | None = None, cores: int | None = None) -> Check:
    load = load or os.getloadavg()
    cores = cores or os.cpu_count() or 1
    per_core = load[0] / cores
    detail = f"load {load[0]:.1f} on {cores} cores"
    if per_core > LOAD_PER_CORE_WARN:
        return Check("host-load", WARN, f"{detail} — on-device timings are unreliable (a 43 ms call measured ~4 s)",
                     fix="compare ratios against a known slice, never raw ms; re-measure when the host is quiet")
    return Check("host-load", OK, detail)


# --- project checks ------------------------------------------------------------------


def load_plugin(path: Path):
    spec = importlib.util.spec_from_file_location("emulock_project_doctor", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project_checks(device: Device, ctx: SimpleNamespace, plugin: Path | None) -> list[Check]:
    if plugin is None or not plugin.is_file():
        return []
    try:
        module = load_plugin(plugin)
        found = module.checks(device, ctx)
    except Stop:
        raise
    except Exception as error:  # a broken project plugin must not hide the built-in results
        return [Check("project", WARN, f"{plugin} failed: {type(error).__name__}: {error}")]
    return [c for c in (found or []) if isinstance(c, Check) or hasattr(c, "status")]


# --- driver --------------------------------------------------------------------------


def run_checks(
    device: Device, ctx: SimpleNamespace, plugin: Path | None = None,
    host_load: Callable[[], Check] = check_host_load,
) -> tuple[list[Check], dict[str, str]]:
    checks: list[Check] = []
    identity: dict[str, str] = {}
    try:
        checks.append(check_lock(device))
        boot, identity = check_boot(device)
        checks.append(boot)
    except Stop as stop:
        checks.append(stop.check)
        return checks, identity
    ctx.api = int(identity["api"]) if identity.get("api", "").isdigit() else 0
    ctx.avd = identity.get("avd", "")
    golden = check_golden(device, int(ctx.config.get("pool.max_age_days", "7") or 7))
    if golden:
        checks.append(golden)
    if ctx.package:
        checks.append(check_apk(device, ctx))
    checks.append(check_dns(device))
    checks.append(check_locale(device, ctx.config.get("locale", "en-US")))
    if ctx.package:
        checks.extend(check_permissions(device, ctx.package, ctx.api))
    checks.extend(check_on_top(device))
    try:
        checks.extend(project_checks(device, ctx, plugin))
    except Stop as stop:
        checks.append(stop.check)
        return checks, identity
    checks.append(host_load())
    return checks, identity


def apply_fixes(device: Device, checks: list[Check]) -> list[str]:
    applied = []
    for check in checks:
        if check.status in (WARN, FAIL) and check.fix_cmds:
            for cmd in check.fix_cmds:
                device.adb(*cmd)
            check.status, check.detail, check.fix = OK, f"fixed: {check.detail}", None
            applied.append(check.name)
    return applied


def render(serial: str, variant: str, identity: dict[str, str], checks: list[Check], applied: list[str]) -> str:
    header = " · ".join(filter(None, [
        f"emulock doctor {serial}",
        f"AVD {identity['avd']}" if identity.get("avd") else None,
        f"API {identity['api']}" if identity.get("api") else None,
        variant or None,
    ]))
    lines = [header]
    for check in checks:
        lines.append(f"  {check.status:<5} {check.name:<13} {check.detail}")
        if check.fix and check.status in (WARN, FAIL):
            lines.append(f"  {'':<5} {'':<13} fix: {check.fix}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    fixable = [c.name for c in checks if c.status in (WARN, FAIL) and c.fix_cmds]
    summary = f"{fails} fail · {warns} warn"
    if applied:
        summary += f" — applied: {', '.join(applied)}"
    elif fixable:
        summary += f" — --fix would apply: {', '.join(fixable)}"
    lines.append(summary)
    return "\n".join(lines)


def build_context(serial: str, variant: str | None, package: str | None, worktree: Path,
                  config: common.Config) -> SimpleNamespace:
    return SimpleNamespace(
        serial=serial,
        variant=variant or "",
        package=package or config.for_variant("package", variant),
        worktree=worktree,
        config=config,
        api=0,
        avd="",
    )


def report(serial: str, variant: str | None = None, package: str | None = None, worktree: Path | None = None,
           use_project: bool = True) -> dict:
    """The --json report, for tools that want it in-process (emulock evidence)."""
    root = worktree or common.project_root()
    config = common.Config(root)
    ctx = build_context(serial, variant, package, root, config)
    plugin = (root / config.get("doctor.plugin", ".emulock/doctor.py")) if use_project else None
    checks, identity = run_checks(Device(serial), ctx, plugin)
    return {"serial": serial, "variant": ctx.variant, "package": ctx.package, "worktree": str(root), **identity,
            "checks": [asdict(c) for c in checks]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emulock doctor",
        description="Check a claimed device before on-device work: lock, boot, golden age, installed build, "
                    "DNS, locale, permissions, app links, what is on top, project checks, host load.",
        epilog="Config (" + __doc__.split("Config (", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("serial", help="the serial you claimed (emulock status)")
    parser.add_argument("variant", nargs="?", default=None,
                        help="a variant from .emulock/config (package.<variant>), e.g. staging")
    parser.add_argument("--package", help="app id to check, instead of the configured one")
    parser.add_argument("--fix", action="store_true", help="apply the safe, reversible fixes the checks suggest")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--dry-run", action="store_true", help="print the device commands instead of running them")
    parser.add_argument("--worktree", type=Path, default=None,
                        help="checkout whose build output the installed APK should match (default: the current one)")
    parser.add_argument("--no-project", action="store_true", help="skip the project's .emulock/doctor.py")
    args = parser.parse_args(argv)

    worktree = (args.worktree or common.project_root()).resolve()
    config = common.Config(worktree)
    variants = config.variants()
    if args.variant and variants and args.variant not in variants:
        parser.error(f"unknown variant '{args.variant}' — .emulock/config declares: {', '.join(variants)}")
    ctx = build_context(args.serial, args.variant, args.package, worktree, config)
    plugin = None if args.no_project else worktree / config.get("doctor.plugin", ".emulock/doctor.py")

    device = Device(args.serial, dry_run=args.dry_run)
    checks, identity = run_checks(device, ctx, plugin)
    applied = apply_fixes(device, checks) if args.fix else []

    if args.dry_run:
        print(f"emulock doctor {args.serial} (dry run) — lock: {checks[0].status}, {checks[0].detail}")
        print("device commands it would run (later ones depend on earlier output):")
        print("\n".join(f"  {cmd}" for cmd in device.planned) or "  none — stopped at the lock check")
        return 0
    if args.json:
        print(json.dumps({"serial": args.serial, "variant": ctx.variant, "package": ctx.package,
                          "worktree": str(worktree), **identity,
                          "checks": [asdict(c) for c in checks], "applied": applied}, indent=2))
    else:
        print(render(args.serial, ctx.variant, identity, checks, applied))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
