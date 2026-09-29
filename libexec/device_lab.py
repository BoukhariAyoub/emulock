#!/usr/bin/env python3
"""Device Lab — a live browser view of this machine's emulator reservations.

Read-only. It enumerates the shared lock store (~/.emulator-locks), asks adb
which emulators are actually attached, and lists the AVDs installed in the SDK.
It never claims, releases, boots, or targets a device, so it is safe to run
alongside any number of agent sessions.

Served on 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# EMULATOR_LOCK_DIR is the documented name and what the emuriad CLI reads;
# EMULATOR_LOCK_ROOT is accepted because this file used to read only that, and
# a dashboard silently watching a different store than the CLI writes to is the
# most confusing failure this tool could have.
LOCK_ROOT = Path(
    os.environ.get("EMULATOR_LOCK_DIR")
    or os.environ.get("EMULATOR_LOCK_ROOT")
    or Path.home() / ".emulator-locks"
)
IDLE_TTL = int(os.environ.get("EMULATOR_LOCK_IDLE_TTL", "14400"))  # 4h, matches emuriad
ABSENT_GRACE = 600  # 10 min boot grace, matches emuriad
POLL_CACHE_SECONDS = 1.5

_cache: dict = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


def _sdk_root() -> Path:
    for candidate in (
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        Path.home() / "Library/Android/sdk",
        Path.home() / "Android/Sdk",
    ):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path.home() / "Library/Android/sdk"


def _tool(name: str, *subdirs: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for sub in subdirs:
        candidate = _sdk_root() / sub / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def adb_devices() -> dict[str, dict]:
    """serial -> {state, model} for every attached device. Enumeration only."""
    adb = _tool("adb", "platform-tools")
    if not adb:
        return {}
    devices: dict[str, dict] = {}
    for line in _run([adb, "devices", "-l"]).splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        model = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token.split(":", 1)[1]
        devices[serial] = {"state": state, "model": model}
    return devices


def installed_avds() -> list[str]:
    emulator = _tool("emulator", "emulator")
    if not emulator:
        return []
    return [ln.strip() for ln in _run([emulator, "-list-avds"]).splitlines() if ln.strip()]


def _parse_meta(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                meta[key.strip()] = value.strip()
    except OSError:
        pass
    return meta


def _abbrev_home(path) -> str:
    """Render a path under $HOME as ~/... so the page is safe to screenshot."""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def _short_owner(owner_id: str) -> str:
    if ":" in owner_id:
        harness, ident = owner_id.split(":", 1)
        return f"{harness}:{ident[:8]}"
    return owner_id


def read_locks() -> tuple[list[dict], list[str]]:
    """Returns (device locks, AVD-name locks)."""
    locks: list[dict] = []
    avd_locks: list[str] = []
    if not LOCK_ROOT.is_dir():
        return locks, avd_locks
    for entry in sorted(LOCK_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("avd-"):
            avd_locks.append(entry.name[len("avd-") :])
            continue
        if not entry.name.startswith("emulator-"):
            continue
        meta = _parse_meta(entry / "meta")
        last_used_path = entry / "last_used"
        try:
            last_used = last_used_path.stat().st_mtime
        except OSError:
            last_used = 0.0
        # The guard hook writes "<verb>\t<target>" here when it can classify the
        # command it is about to allow, and leaves the file empty otherwise. The
        # mtime is the lease either way, so an empty file is a normal state, not
        # a missing one -- older locks and unclassifiable commands both land here.
        activity, activity_target = "", ""
        try:
            first = last_used_path.read_text(errors="replace").split("\n", 1)[0]
            if first.strip():
                verb, _, tgt = first.partition("\t")
                activity, activity_target = verb.strip()[:40], tgt.strip()[:64]
        except OSError:
            pass
        locks.append(
            {
                "serial": meta.get("SERIAL", entry.name),
                "avd": meta.get("AVD", ""),
                "port": meta.get("PORT", ""),
                "lock_state": meta.get("STATE", ""),
                "owner_id": meta.get("OWNER_ID", ""),
                "owner_short": _short_owner(meta.get("OWNER_ID", "")),
                "branch": meta.get("OWNER_BRANCH", ""),
                "worktree": meta.get("OWNER_WORKTREE", ""),
                "note": meta.get("NOTE", ""),
                "created_at": int(meta.get("CREATED_AT", "0") or 0),
                "last_used": last_used,
                "activity": activity,
                "activity_target": activity_target,
            }
        )
    return locks, avd_locks


# Pulling a ticket id out of a branch name is a common convention but not a
# universal one, and every team spells it differently (ALL-123, JIRA-4, #17).
# So it is opt-in: set EMULATOR_LOCK_TICKET_RE to a pattern whose first group
# is the id. Unset -- the default -- means no ticket is ever shown, which is
# the correct behaviour for a project that has no such convention.
TICKET_RE = os.environ.get("EMULATOR_LOCK_TICKET_RE", "")


def _ticket(branch: str) -> str:
    if not TICKET_RE or not branch:
        return ""
    try:
        match = re.search(TICKET_RE, branch, re.IGNORECASE)
    except re.error:
        # A malformed pattern is the user's typo, not a reason to take the
        # whole dashboard down.
        return ""
    if not match:
        return ""
    return (match.group(1) if match.groups() else match.group(0)).upper()


# Urgency order for the device list: what needs a human first comes first.
STATE_ORDER = {
    "ghost": 0,
    "expired": 1,
    "stalled": 2,
    "booting": 3,
    "mine": 4,
    "held": 5,
}

# How long a claim may sit without the device appearing in adb before we stop
# calling it "booting" and start calling it stalled.
BOOT_STALL = 180


def derive_state(adb_state: str, idle, age, mine: bool) -> str:
    """Resolve one device to a single state.

    The lock's own STATE field is NOT consulted. It is written once at claim
    time -- "running" when claiming an already-booted device, "booting" when
    reserving a port to boot into -- and there is no third write site, so it
    never transitions. A lock created the second way reports "booting" forever;
    one device on this machine carried that label for 21 hours while adb had
    been reporting it as a healthy `device` the whole time.

    Caching a fact that `adb devices` answers authoritatively, for free, on
    every poll can only ever drift. So treat the stored field as claim-time
    intent and derive liveness here instead. The fix belongs on this side
    precisely because Device Lab is read-only: it must never repair the lock
    store, only describe it accurately.
    """
    if adb_state == "device":
        # Live observation wins over anything recorded at claim time.
        if idle is not None and idle > IDLE_TTL:
            return "expired"
        return "mine" if mine else "held"
    if adb_state == "absent":
        if age is not None and age > BOOT_STALL:
            return "ghost" if (idle is not None and idle > ABSENT_GRACE) else "stalled"
        return "booting"
    # Attached but unhealthy (offline, unauthorized): not usable, not gone.
    return "stalled"


def snapshot() -> dict:
    now = time.time()
    attached = adb_devices()
    locks, avd_locks = read_locks()
    me = os.environ.get("EMULATOR_LOCK_OWNER") or (
        f"claude-code:{os.environ['CLAUDE_CODE_SESSION_ID']}"
        if os.environ.get("CLAUDE_CODE_SESSION_ID")
        else ""
    )

    rows = []
    for lock in locks:
        serial = lock["serial"]
        adb_info = attached.get(serial)
        idle = max(0.0, now - lock["last_used"]) if lock["last_used"] else None
        warnings = []
        if adb_info is None and (idle is None or idle > ABSENT_GRACE):
            warnings.append("orphan lock — device not attached, past the 10m boot grace")
        elif adb_info is None:
            warnings.append("device not attached yet (within boot grace)")
        if idle is not None and idle > IDLE_TTL:
            warnings.append(f"lease expired — idle over {IDLE_TTL // 3600}h, reapable")
        if adb_info and adb_info["state"] != "device":
            warnings.append(f"adb reports '{adb_info['state']}'")
        adb_state = adb_info["state"] if adb_info else "absent"
        age = (now - lock["created_at"]) if lock["created_at"] else None
        mine = bool(me) and lock["owner_id"] == me
        rows.append(
            {
                **lock,
                "adb_state": adb_state,
                "model": adb_info["model"] if adb_info else "",
                "idle_seconds": idle,
                "age_seconds": age,
                "mine": mine,
                "ticket": _ticket(lock["branch"]),
                "state": derive_state(adb_state, idle, age, mine),
                "warnings": warnings,
            }
        )
    rows.sort(key=lambda r: (STATE_ORDER.get(r["state"], 99), r["serial"]))

    locked_serials = {r["serial"] for r in rows}
    unmanaged = [
        {"serial": serial, **info}
        for serial, info in sorted(attached.items())
        if serial not in locked_serials
    ]

    avds = installed_avds()
    busy = set(avd_locks)
    return {
        "generated_at": now,
        # Abbreviated, and the session id shortened the way owners already are
        # on the cards. This page gets screenshotted and screenshared; neither
        # the operator's home directory nor a full session uuid tells a reader
        # anything useful, and both are theirs to keep.
        "lock_root": _abbrev_home(LOCK_ROOT),
        "me": me,
        "me_short": _short_owner(me),
        "idle_ttl": IDLE_TTL,
        "devices": rows,
        "unmanaged": unmanaged,
        "avds": [{"name": name, "claimed": name in busy} for name in avds],
        "avd_locks": sorted(avd_locks),
        "adb_available": bool(_tool("adb", "platform-tools")),
    }


def cached_snapshot() -> dict:
    with _cache_lock:
        if _cache["data"] is not None and (time.time() - _cache["at"]) < POLL_CACHE_SECONDS:
            return _cache["data"]
    data = snapshot()
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["data"] = data
    return data


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Device Lab</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#14161a; --muted:#6b7280; --line:#e4e7eb;
  --ok:#15803d; --ok-bg:#dcfce7; --warn:#b45309; --warn-bg:#fef3c7;
  --bad:#b91c1c; --bad-bg:#fee2e2; --mine:#1d4ed8; --mine-bg:#dbeafe;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0f1115; --panel:#171a21; --ink:#e8eaed; --muted:#9aa2ae; --line:#272b34;
  --ok:#4ade80; --ok-bg:#12301f; --warn:#fbbf24; --warn-bg:#3a2c0a;
  --bad:#f87171; --bad-bg:#3b1414; --mine:#93b4fd; --mine-bg:#152244;
}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
h1{font-size:19px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:12.5px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);display:inline-block;
  margin-right:6px;vertical-align:middle}
section{margin-top:22px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
  margin:0 0 9px;font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;margin-bottom:10px}
.card.mine{border-color:var(--mine);box-shadow:inset 3px 0 0 var(--mine)}
.row1{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.serial{font:600 14px ui-monospace,SFMono-Regular,Menlo,monospace}
.avd{color:var(--muted);font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace}
.tag{font-size:11px;font-weight:600;padding:2px 7px;border-radius:5px;white-space:nowrap}
.t-ok{background:var(--ok-bg);color:var(--ok)}
.t-warn{background:var(--warn-bg);color:var(--warn)}
.t-bad{background:var(--bad-bg);color:var(--bad)}
.t-mine{background:var(--mine-bg);color:var(--mine)}
.t-act{background:transparent;color:var(--muted);border:1px solid var(--line)}
.meta{margin-top:8px;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:6px 18px}
.meta div{font-size:12.5px;color:var(--muted);min-width:0}
.meta b{color:var(--ink);font-weight:500;display:block;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.note{margin-top:8px;font-size:12.5px;color:var(--muted);font-style:italic}
.warn{margin-top:8px;font-size:12.5px;color:var(--warn);background:var(--warn-bg);
  padding:6px 9px;border-radius:6px}
.avds{display:flex;flex-wrap:wrap;gap:6px}
.pill{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;padding:4px 9px;
  border:1px solid var(--line);border-radius:999px;background:var(--panel)}
.pill.busy{background:var(--warn-bg);color:var(--warn);border-color:transparent}
.empty{color:var(--muted);font-size:13px;padding:10px 2px}
footer{margin-top:26px;color:var(--muted);font-size:11.5px;border-top:1px solid var(--line);
  padding-top:12px}
code{font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace}
</style></head><body>
<header><h1>Device Lab</h1>
<span class="sub"><span class="dot"></span><span id="stamp">connecting…</span></span></header>
<div class="sub" id="root"></div>
<section><h2>Reserved devices</h2><div id="devices"></div></section>
<section><h2>Attached without a lock</h2><div id="unmanaged"></div></section>
<section><h2>Installed AVDs</h2><div class="avds" id="avds"></div></section>
<footer>Read-only view of <code>emuriad</code> state. Check in with
<code>emuriad check-in</code>, check out with <code>emuriad check-out &lt;serial&gt;</code>.
Never touch a serial you did not check in to.</footer>
<script>
const dur = s => {
  if (s == null) return '—';
  s = Math.floor(s);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  const h = Math.floor(s / 3600);
  return h + 'h' + String(Math.floor((s % 3600) / 60)).padStart(2, '0');
};
const esc = t => String(t ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function deviceCard(d) {
  const tags = [];
  if (d.mine) tags.push('<span class="tag t-mine">this session</span>');
  // One derived state, not the raw adb_state plus the stale stored one. The
  // lock's STATE is claim-time intent and never transitions, so rendering it
  // put a permanent "booting" badge on healthy devices -- see derive_state().
  const LABEL = {ghost: 'ghost', expired: 'lease expired', stalled: 'stalled',
                 booting: 'booting', mine: 'yours', held: 'held'};
  const TONE  = {ghost: 't-bad', expired: 't-warn', stalled: 't-warn',
                 booting: 't-warn', mine: 't-mine', held: 't-ok'};
  tags.push(`<span class="tag ${TONE[d.state] || 't-ok'}">${esc(LABEL[d.state] || d.state)}</span>`);
  if (d.activity) {
    const what = d.activity + (d.activity_target ? ' · ' + d.activity_target : '');
    tags.push(`<span class="tag t-act">${esc(what)}</span>`);
  }
  if (d.ticket) tags.push(`<span class="tag t-ok">${esc(d.ticket)}</span>`);
  return `<div class="card${d.mine ? ' mine' : ''}">
    <div class="row1"><span class="serial">${esc(d.serial)}</span>
      <span class="avd">${esc(d.avd)}</span>${tags.join('')}</div>
    <div class="meta">
      <div>owner<b title="${esc(d.owner_id)}">${esc(d.owner_short || '—')}</b></div>
      <div>branch<b title="${esc(d.branch)}">${esc(d.branch || '—')}</b></div>
      <div>idle<b>${dur(d.idle_seconds)}</b></div>
      <div>held for<b>${dur(d.age_seconds)}</b></div>
      <div>model<b>${esc(d.model || '—')}</b></div>
    </div>
    ${d.note ? `<div class="note">${esc(d.note)}</div>` : ''}
    ${d.warnings.map(w => `<div class="warn">${esc(w)}</div>`).join('')}
  </div>`;
}

async function tick() {
  try {
    const s = await (await fetch('/api/state', {cache: 'no-store'})).json();
    document.getElementById('stamp').textContent =
      `${s.devices.length} reserved · ${s.unmanaged.length} unlocked · updated ` +
      new Date(s.generated_at * 1000).toLocaleTimeString();
    document.getElementById('root').textContent =
      s.lock_root + (s.me_short ? '  ·  this session: ' + s.me_short : '');
    document.getElementById('devices').innerHTML =
      s.devices.length ? s.devices.map(deviceCard).join('')
                       : '<div class="empty">No devices reserved.</div>';
    document.getElementById('unmanaged').innerHTML =
      s.unmanaged.length
        ? s.unmanaged.map(u => `<div class="card"><div class="row1">
            <span class="serial">${esc(u.serial)}</span>
            <span class="avd">${esc(u.model)}</span>
            <span class="tag t-warn">no lock</span></div>
            <div class="warn">Attached but held by no lock — check in before use.</div>
          </div>`).join('')
        : '<div class="empty">Every attached device is accounted for.</div>';
    document.getElementById('avds').innerHTML =
      s.avds.length
        ? s.avds.map(a => `<span class="pill${a.claimed ? ' busy' : ''}">${esc(a.name)}</span>`).join('')
        : '<span class="empty">No AVDs found (emulator binary missing?).</span>';
  } catch (e) {
    document.getElementById('stamp').textContent = 'watcher unreachable — is it still running?';
  }
}
tick(); setInterval(tick, 2000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/state"):
            self._send(json.dumps(cached_snapshot()).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def log_message(self, *args) -> None:  # silence per-request logging
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="device-lab.sh",
        description="Live browser view of this machine's emulator reservations (read-only).",
    )
    parser.add_argument("--port", type=int, default=7337, help="listen port (default 7337)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--json", action="store_true", help="print one snapshot as JSON and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the snapshot and the URL it would serve, without starting the server",
    )
    args = parser.parse_args()

    if args.json or args.dry_run:
        print(json.dumps(snapshot(), indent=2))
        if args.dry_run:
            print(f"\n[dry-run] would serve http://127.0.0.1:{args.port}/", file=sys.stderr)
        return 0

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"device-lab: cannot bind 127.0.0.1:{args.port} — {exc}", file=sys.stderr)
        print("device-lab: pass --port to pick another.", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    print(f"device-lab: watching {LOCK_ROOT}")
    print(f"device-lab: serving {url}  (ctrl-c to stop)")
    if not args.no_open:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ndevice-lab: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
