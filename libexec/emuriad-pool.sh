#!/usr/bin/env bash
#
# emuriad pool — build and inspect the agent emulator pool.
#
# Agents claim pool instances with `emuriad claim --pool`: read-only copies of
# one AVD, each booted from its `golden` snapshot. This script makes that
# snapshot. Everything an agent would otherwise fix by hand on a fresh device is
# already done in it:
#
#   device  locale checked, Private DNS off, animations off, screen always on,
#           no lock screen, hardware keyboard (the IME never covers the screen)
#   app     optional: pool.apk installed, then the project's own setup hook
#           (.emuriad/pool-setup.sh) — sign-out state, first-run flags, permissions
#
# Agents install their own build over it (`adb install -r` keeps that app
# state). The snapshot's app build is only a starting state, so re-bake when
# the setup above changes, or weekly with `rebake`.
#
# Usage:
#   emuriad pool status
#   emuriad pool bake [--window] [--apk PATH] [--dry-run]
#   emuriad pool rebake [--dry-run]     bake from a fresh build of pool.ref in a throwaway checkout
#
# --window bakes `golden-window` instead of `golden`: the same setup, saved from
# a boot with a window, for `claim --pool --window` (a device someone can
# watch). A window opens on the screen while it bakes. rebake refreshes
# golden-window too once it exists.
#
# bake creates the AVD if missing, claims it writable by name, cold-boots it
# headless, applies the setup, checks the result with `emuriad doctor` and only
# then saves `golden`, shuts the emulator down and releases the lock. It refuses
# while any pool instance is claimed or running. About 3-5 minutes.
#
# golden is read-only on disk between bakes. A boot that cannot load it, because
# a window or a -gpu flag picked another renderer, makes the emulator delete the
# snapshot and exit, and -force-snapshot-load does not stop that. Without write
# permission the delete fails and golden survives. bake lifts the protection only
# for the save. To delete the AVD by hand: chmod -R u+w <avd>/snapshots/golden.
#
# Config (<repo>/.emuriad/config, or EMURIAD_<KEY> in the environment; the
# pre-rename .emulock/config and EMULOCK_<KEY> still work):
#   pool.avd        AVD name                        (default: emulock_pool)
#   pool.max        pool instances at once          (default: 3, ~2 GB RAM each)
#   pool.image      system image, API < 36          (default: android-34 google_apis, host arch)
#   pool.device     avdmanager device profile       (default: medium_phone)
#   pool.apk        APK installed before setup, relative to the repo (optional)
#   pool.setup      setup hook, run as: bash <hook> <serial>   (default: .emuriad/pool-setup.sh)
#   pool.verify     doctor checks that must be ok before saving (default: lock boot dns locale on-top)
#   pool.build      rebake: the command that builds pool.apk    (required for rebake)
#   pool.ref        rebake: what to build            (default: origin/main)
#   locale          the locale the image must boot in (default: en-US; "any" skips it)
#
# The setup hook gets EMURIAD_SERIAL, EMURIAD_APK and ANDROID_SERIAL (and the
# pre-rename EMULOCK_SERIAL, EMULOCK_APK). It runs
# adb itself, which the guard cannot see, so it must only touch that serial.
#
set -euo pipefail

SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
[[ -d "$SDK" ]] || SDK="$HOME/Android/Sdk"
EMU_BIN="$SDK/emulator/emulator"
AVDMANAGER="$SDK/cmdline-tools/latest/bin/avdmanager"
AVD_HOME="${ANDROID_AVD_HOME:-$HOME/.android/avd}"
LOCK_ROOT="${EMULATOR_LOCK_DIR:-$HOME/.emulator-locks}"
POOL_SNAPSHOT="golden"
POOL_SNAPSHOT_WINDOW="golden-window"
SNAP="$POOL_SNAPSHOT"   # the snapshot this bake writes
WINDOW=0
LOCK="${EMURIAD_BIN:-emuriad}"

DRY_RUN="${EMURIAD_DRY_RUN:-0}"
SERIAL=""
EMU_PID=""
DONE=0
REBAKE_DIR=""
APK=""

die() { echo "emuriad pool: $*" >&2; exit 1; }
say() { echo "emuriad pool: $*"; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

# The project's config directory: .emuriad/, or .emulock/ from before the rename.
CONF_DIR=.emuriad
[[ ! -d "$PROJECT_ROOT/.emuriad" && -d "$PROJECT_ROOT/.emulock" ]] && CONF_DIR=.emulock

conf_get() { # conf_get <key> [default] — same rules as bin/emuriad
  local key="$1" def="${2:-}" env_name val="" file="$PROJECT_ROOT/$CONF_DIR/config"
  env_name="$(printf '%s' "$key" | tr '[:lower:].-' '[:upper:]__')"
  val="$(printenv "EMURIAD_$env_name" || printenv "EMULOCK_$env_name" || true)"
  if [[ -z "$val" && -f "$file" ]]; then
    val="$(awk -v k="$key" '
      { line = $0; sub(/^[ \t]+/, "", line) }
      line == "" || line ~ /^#/ { next }
      { i = index(line, "="); if (!i) next
        name = substr(line, 1, i - 1); v = substr(line, i + 1)
        sub(/[ \t]+#.*$/, "", v)
        gsub(/^[ \t]+|[ \t]+$/, "", name); gsub(/^[ \t]+|[ \t]+$/, "", v)
        if (name == k) { print v; exit } }' "$file")"
  fi
  printf '%s' "${val:-$def}"
}

host_abi() { case "$(uname -m)" in arm64|aarch64) echo arm64-v8a ;; *) echo x86_64 ;; esac; }

POOL_AVD="${EMULATOR_POOL_AVD:-$(conf_get pool.avd emulock_pool)}"
POOL_MAX="${EMULATOR_POOL_MAX:-$(conf_get pool.max 3)}"
POOL_IMAGE="$(conf_get pool.image "system-images;android-34;google_apis;$(host_abi)")"
DEVICE_PROFILE="$(conf_get pool.device medium_phone)"
LOCALE="$(conf_get locale en-US)"
SETUP="$(conf_get pool.setup "$CONF_DIR/pool-setup.sh")"
VERIFY="$(conf_get pool.verify "lock boot dns locale on-top")"
BUILD="$(conf_get pool.build)"
REF="$(conf_get pool.ref origin/main)"
CONF_APK="$(conf_get pool.apk)"

abs_in() { # abs_in <root> <path>: <path> as given if absolute, else under <root>
  case "$2" in /*) printf '%s' "$2" ;; *) printf '%s/%s' "$1" "$2" ;; esac
}

avd_dir() { echo "$AVD_HOME/$POOL_AVD.avd"; }
snapshot_dir() { echo "$(avd_dir)/snapshots/${1:-$SNAP}"; }
golden_record() { echo "$(avd_dir)/${1:-$SNAP}.json"; }
dev() { adb -s "$SERIAL" "$@"; }
dev_sh() { adb -s "$SERIAL" shell "$@" | tr -d '\r'; }

# Read-only between bakes so a boot that cannot load it cannot delete it (header).
protect_snapshot() { [[ ! -d "$(snapshot_dir)" ]] || chmod -R a-w "$(snapshot_dir)"; }
unprotect_snapshot() { [[ ! -d "$(snapshot_dir)" ]] || chmod -R u+w "$(snapshot_dir)"; }

set_config() { # set_config <key> <value> in the AVD's config.ini (portable: no sed -i)
  local ini tmp
  ini="$(avd_dir)/config.ini"
  tmp="$(mktemp)"
  grep -v "^$1 *=" "$ini" > "$tmp" || true
  echo "$1=$2" >> "$tmp"
  cat "$tmp" > "$ini"
  rm -f "$tmp"
}

image_dir() { # system-images;android-34;google_apis;arm64-v8a -> $SDK/system-images/android-34/google_apis/arm64-v8a
  printf '%s/%s' "$SDK" "$(printf '%s' "$POOL_IMAGE" | tr ';' '/')"
}

create_avd() {
  [[ -d "$(avd_dir)" ]] && return 0
  [[ -d "$(image_dir)" ]] || die "system image missing — install it: sdkmanager \"$POOL_IMAGE\""
  [[ -x "$AVDMANAGER" ]] || die "avdmanager not found at $AVDMANAGER — install the SDK command-line tools"
  if [[ "$DRY_RUN" == 1 ]]; then
    say "[dry-run] would create AVD $POOL_AVD ($POOL_IMAGE, $DEVICE_PROFILE) with a hardware keyboard, 2 GB RAM, 4 cores"
    return 0
  fi
  say "creating AVD $POOL_AVD ($POOL_IMAGE)"
  "$AVDMANAGER" create avd -n "$POOL_AVD" -k "$POOL_IMAGE" -d "$DEVICE_PROFILE" <<<"no" >/dev/null
  set_config hw.keyboard yes          # with a hardware keyboard the soft IME stays hidden
  set_config hw.ramSize 2048
  set_config hw.cpu.ncore 4
  set_config disk.dataPartition.size 6G
  set_config showDeviceFrame no
}

# The marker that makes every emuriad component treat this AVD as a pool AVD:
# plain claims skip it, reap shuts down unowned instances, the guard holds its
# snapshot boots to -no-window.
mark_pool_avd() { [[ "$DRY_RUN" == 1 ]] || : > "$(avd_dir)/emuriad-pool"; }

pool_in_use() { # 0 = some instance of the pool AVD is claimed or running
  local d serial
  for d in "$LOCK_ROOT"/emulator-*; do
    [[ -f "$d/meta" ]] && grep -qx "AVD=$POOL_AVD" "$d/meta" && return 0
  done
  for serial in $(adb devices 2>/dev/null | awk 'NR > 1 && $1 ~ /^emulator-/ {print $1}'); do
    [[ "$(adb -s "$serial" emu avd name 2>/dev/null | head -n1 | tr -d '\r')" == "$POOL_AVD" ]] && return 0
  done
  return 1
}

wait_for_boot() {
  local i
  for i in $(seq 1 180); do
    [[ "$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == 1 ]] && return 0
    [[ -n "$EMU_PID" ]] && ! kill -0 "$EMU_PID" 2>/dev/null && die "the emulator exited during boot"
    sleep 2
  done
  die "$SERIAL did not finish booting in 6 minutes"
}

write_golden_record() { # what was baked, from where — doctor reads it to warn when it goes stale
  local commit="unknown" apk_sha=""
  if [[ -n "$APK" ]]; then
    commit="$(git -C "$(dirname "$APK")" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    apk_sha="$(shasum -a 256 "$APK" 2>/dev/null | cut -d' ' -f1 || true)"
  fi
  printf '{"baked_at": "%s", "commit": "%s", "apk_sha256": "%s", "image": "%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$commit" "$apk_sha" "$POOL_IMAGE" > "$(golden_record)"
}

remove_rebake_checkout() {
  [[ -n "$REBAKE_DIR" && -d "$REBAKE_DIR" ]] || return 0
  git -C "$PROJECT_ROOT" worktree remove "$REBAKE_DIR" >/dev/null 2>&1 \
    || echo "emuriad pool: could not remove the rebake checkout $REBAKE_DIR — remove it by hand" >&2
}

on_exit() { cleanup; remove_rebake_checkout; }

cleanup() { # on any exit before the bake finished: never leave a writable pool AVD running
  [[ "$DONE" == 1 || -z "$SERIAL" ]] && return 0
  echo "emuriad pool: bake did not finish — shutting $SERIAL down and releasing it; '$SNAP' was not changed" >&2
  adb -s "$SERIAL" emu kill >/dev/null 2>&1 || true
  [[ -n "$EMU_PID" ]] && wait "$EMU_PID" 2>/dev/null || true
  protect_snapshot || true
  "$LOCK" release "$SERIAL" >/dev/null 2>&1 || true
}

setup_device() {
  local locale
  locale="$(dev_sh getprop persist.sys.locale)"
  [[ -n "$locale" ]] || locale="$(dev_sh getprop ro.product.locale)"
  if [[ "$LOCALE" != any && "$locale" != "$LOCALE" ]]; then
    die "the image boots in '$locale', not $LOCALE (set locale = any in $CONF_DIR/config to accept it)"
  fi
  dev_sh settings put global private_dns_mode off
  dev_sh settings put global window_animation_scale 0
  dev_sh settings put global transition_animation_scale 0
  dev_sh settings put global animator_duration_scale 0
  dev_sh svc power stayon true
  dev_sh settings put system screen_off_timeout 2147483647
  dev_sh locksettings set-disabled true >/dev/null || true   # already off on a fresh image
  dev_sh wm dismiss-keyguard
  # hw.keyboard alone does not keep the IME away; this does.
  dev_sh settings put secure show_ime_with_hard_keyboard 0
}

setup_app() {
  local hook
  if [[ -n "$APK" ]]; then
    say "installing $(basename "$APK")"
    dev install -r "$APK" >/dev/null
  fi
  hook="$(abs_in "$PROJECT_ROOT" "$SETUP")"
  if [[ -f "$hook" ]]; then
    say "running the project's setup hook: $hook"
    EMURIAD_SERIAL="$SERIAL" EMURIAD_APK="$APK" EMULOCK_SERIAL="$SERIAL" EMULOCK_APK="$APK" \
      ANDROID_SERIAL="$SERIAL" /bin/bash "$hook" "$SERIAL" \
      || die "the setup hook failed: $hook"
  fi
  dev_sh input keyevent 3
}

verify() { # the snapshot is only saved if the doctor agrees the device is clean
  local report built_in
  # Compare against the checkout that built the APK (a rebake builds in a throwaway one).
  built_in="$PROJECT_ROOT"
  [[ -n "$APK" ]] && built_in="$(git -C "$(dirname "$APK")" rev-parse --show-toplevel 2>/dev/null || echo "$PROJECT_ROOT")"
  report="$("$LOCK" doctor "$SERIAL" --json --worktree "$built_in" || true)"
  "$LOCK" doctor "$SERIAL" --worktree "$built_in" || true
  python3 - "$report" "$VERIFY" <<'PY'
import json, sys
required = set(sys.argv[2].split())
try:
    checks = json.loads(sys.argv[1])["checks"]
except (ValueError, KeyError):
    sys.exit("emuriad pool: not saving the snapshot — the doctor produced no report")
bad = [f"{c['name']}: {c['detail']}" for c in checks if c["name"] in required and c["status"] != "ok"]
missing = required - {c["name"] for c in checks}
if missing:
    bad.append("checks that did not run: " + ", ".join(sorted(missing)))
if bad:
    sys.exit("emuriad pool: not saving the snapshot — " + "; ".join(bad))
PY
}

cmd_bake() {
  SERIAL=""; EMU_PID=""; DONE=0; WINDOW=0; SNAP="$POOL_SNAPSHOT"
  while [[ $# -gt 0 ]]; do
    case "$1" in
    --apk) APK="$2"; shift 2 ;;
    --window) WINDOW=1; SNAP="$POOL_SNAPSHOT_WINDOW"; shift ;;
    *) die "bake: unknown option $1" ;;
    esac
  done
  [[ -z "$APK" && -n "$CONF_APK" ]] && APK="$(abs_in "$PROJECT_ROOT" "$CONF_APK")"
  if [[ -n "$APK" ]]; then
    [[ -f "$APK" ]] || die "no APK at $APK — build it${BUILD:+ ($BUILD)}, or pass --apk"
    APK="$(cd "$(dirname "$APK")" && pwd)/$(basename "$APK")"
  fi
  create_avd
  pool_in_use && die "pool instances are claimed or running — release them first (emuriad status); a writable boot would fight them"
  mark_pool_avd

  # --additional: `claim --avd` alone hands back a lock this session already holds.
  local claim_out
  if [[ "$DRY_RUN" == 1 ]]; then
    "$LOCK" --dry-run claim --additional --avd "$POOL_AVD" --note "pool bake"
    say "[dry-run] would cold-boot $( ((WINDOW)) && echo 'with a window' || echo headless), set up the device${APK:+, install $(basename "$APK")}, run $(abs_in "$PROJECT_ROOT" "$SETUP") if present,"
    say "[dry-run] verify ($VERIFY) with emuriad doctor, save '$SNAP', shut down, release"
    return 0
  fi
  claim_out="$("$LOCK" claim --additional --avd "$POOL_AVD" --note "pool bake")"
  SERIAL="$(sed -n 's/^claimed: \(emulator-[0-9]*\).*/\1/p' <<<"$claim_out")"
  [[ -n "$SERIAL" ]] || die "could not read the claimed serial from: $claim_out"
  trap on_exit EXIT

  # -no-snapshot-save: the only snapshot this boot writes is the explicit one below.
  # No -gpu flag, and the same window setting as the pool boot that will load it:
  # a snapshot only loads under the renderer and features it was saved with.
  local display="-no-window"
  if (( WINDOW )); then
    display=""
    say "cold-booting $POOL_AVD on $SERIAL with a window (it closes when the bake is done)"
  else
    say "cold-booting $POOL_AVD on $SERIAL (headless)"
  fi
  # shellcheck disable=SC2086  # $display is one flag or none
  "$EMU_BIN" @"$POOL_AVD" -port "${SERIAL#emulator-}" -no-snapshot-load -no-snapshot-save -no-boot-anim $display \
    >/dev/null 2>&1 &
  EMU_PID=$!
  wait_for_boot   # polls, and notices if the emulator dies instead of hanging in wait-for-device
  say "booted — applying device setup"
  setup_device
  setup_app
  sleep 2   # let the launcher take focus before the on-top check
  verify
  say "saving snapshot '$SNAP'"
  local saved
  unprotect_snapshot   # the save replaces it; read-only again once the emulator is down
  saved="$(dev emu avd snapshot save "$SNAP" | tr -d '\r')"
  [[ "$saved" == *OK* && -d "$(snapshot_dir)" ]] || die "snapshot save failed: $saved"
  write_golden_record
  dev emu kill >/dev/null 2>&1 || true
  wait "$EMU_PID" 2>/dev/null || true
  protect_snapshot
  "$LOCK" release "$SERIAL" >/dev/null
  DONE=1
  if (( WINDOW )); then
    say "done — a watchable device is now: emuriad claim --pool --window"
  else
    say "done — agents can now: emuriad claim --pool"
  fi
}

cmd_rebake() { # bake from a fresh build of pool.ref, in a throwaway checkout
  [[ -n "$BUILD" ]] || die "rebake: set pool.build in $CONF_DIR/config (the command that builds the pool APK)"
  [[ -n "$CONF_APK" ]] || die "rebake: set pool.apk in $CONF_DIR/config (where that build puts the APK)"
  pool_in_use && die "pool instances are claimed or running — rebake when the pool is idle (emuriad status)"
  local dir log remote="${REF%%/*}" branch="${REF#*/}"
  dir="$(mktemp -d -t emuriad-rebake)"
  if [[ "$DRY_RUN" == 1 ]]; then
    rmdir "$dir"
    say "[dry-run] would fetch $REF, check it out detached in a temporary directory, run: $BUILD"
    say "[dry-run] then bake the golden phone from $CONF_APK (recording golden.json)$([[ -d "$(snapshot_dir "$POOL_SNAPSHOT_WINDOW")" ]] && echo ', then golden-window with a window'), and remove the checkout"
    return 0
  fi
  rmdir "$dir"
  [[ "$remote" != "$REF" ]] && git -C "$PROJECT_ROOT" fetch -q "$remote" "$branch"
  git -C "$PROJECT_ROOT" worktree add -q --detach "$dir" "$REF"
  REBAKE_DIR="$dir"
  trap on_exit EXIT
  # Android builds need the SDK path, which lives in an untracked file.
  [[ -f "$PROJECT_ROOT/local.properties" ]] && cp "$PROJECT_ROOT/local.properties" "$dir/"
  log="$(mktemp -t emuriad-rebake-log)"
  say "building $REF ($(git -C "$dir" rev-parse --short HEAD)) — a cold build takes a few minutes; log: $log"
  (cd "$dir" && /bin/bash -c "$BUILD") >"$log" 2>&1 \
    || { tail -n 20 "$log" >&2; die "the build failed — full log: $log"; }
  local apk had_window=0
  apk="$(abs_in "$dir" "$CONF_APK")"
  [[ -d "$(snapshot_dir "$POOL_SNAPSHOT_WINDOW")" ]] && had_window=1
  cmd_bake --apk "$apk"
  # golden-window, once someone has baked one, goes stale exactly like golden.
  (( had_window )) && cmd_bake --window --apk "$apk"
  return 0
}

cmd_status() {
  if [[ ! -d "$(avd_dir)" ]]; then
    echo "agent pool: no AVD $POOL_AVD yet — emuriad pool bake"
    return 0
  fi
  local snap claimed=0 d name how
  for d in "$LOCK_ROOT"/emulator-*; do
    [[ -f "$d/meta" ]] && grep -qx "POOL=1" "$d/meta" && claimed=$((claimed + 1))
  done
  echo "agent pool: AVD $POOL_AVD ($(sed -n 's|^image\.sysdir\.1 *= *system-images/\([^/]*\)/\([^/]*\)/.*|\1 \2|p' "$(avd_dir)/config.ini"))"
  for name in "$POOL_SNAPSHOT" "$POOL_SNAPSHOT_WINDOW"; do
    snap="$(snapshot_dir "$name")"
    how="headless: emuriad claim --pool"
    [[ "$name" == "$POOL_SNAPSHOT_WINDOW" ]] && how="with a window: emuriad claim --pool --window"
    if [[ -d "$snap" ]]; then
      echo "  snapshot '$name' ($how): $(du -sh "$snap" | cut -f1)"
      [[ -w "$snap" ]] && echo "    writable: a boot that cannot load it deletes it (the next bake makes it read-only)"
      if [[ -f "$(golden_record "$name")" ]]; then
        local baked commit age
        baked="$(sed -n 's/.*"baked_at": "\([^"]*\)".*/\1/p' "$(golden_record "$name")")"
        commit="$(sed -n 's/.*"commit": "\([^"]*\)".*/\1/p' "$(golden_record "$name")")"
        age="$(python3 -c 'import sys, datetime as d; t = d.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")); print((d.datetime.now(d.timezone.utc) - t).days)' "$baked" 2>/dev/null || echo "?")"
        echo "    baked from $commit, $age day(s) old (refresh: emuriad pool rebake)"
      fi
    elif [[ "$name" == "$POOL_SNAPSHOT" ]]; then
      echo "  snapshot '$name': missing — emuriad pool bake"
    else
      echo "  snapshot '$name': not baked (for a watchable device: emuriad pool bake --window)"
    fi
  done
  echo "  claimed: $claimed/$POOL_MAX   (emuriad status for owners)"
}

main() {
  local args=() a cmd=""
  for a in "$@"; do
    case "$a" in
    --help|-h) usage; exit 0 ;;
    --dry-run) DRY_RUN=1 ;;
    *) args+=("$a") ;;
    esac
  done
  set -- ${args[@]+"${args[@]}"}
  cmd="${1:-}"
  [[ -n "$cmd" ]] || { usage; exit 1; }
  shift
  command -v adb >/dev/null 2>&1 || die "adb not on PATH"
  case "$cmd" in
  bake) cmd_bake "$@" ;;
  rebake) cmd_rebake "$@" ;;
  status) cmd_status "$@" ;;
  *) die "unknown command: $cmd (bake|rebake|status)" ;;
  esac
}

main "$@"
