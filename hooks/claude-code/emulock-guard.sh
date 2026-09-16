#!/usr/bin/env bash
#
# emulator-guard.sh — Claude Code PreToolUse hook (matcher: Bash; wired via
# the git-committed settings.json — no install step, unlike Cursor's
# hook, which is user-level and needs install.sh
# run once per machine. Every contributor gets this one for free just by
# having the repo checked out.)
#
# Hard-blocks shell commands that touch emulator devices this Claude Code
# session has not claimed through emulock. This is a
# straight port of the original Cursor guard's matching logic to Claude Code's
# hook I/O contract — same cases, same lock store:
#
#   deny  adb kill-server                        (drops every agent's devices)
#   deny  adb -s emulator-XXXX ...               unless this session owns the lock
#   deny  device-targeting adb without -s        (bare `adb shell`, `adb install`, ...)
#   deny  emulator @AVD launches                 without -port on a lock we own
#   deny  direct writes to ~/.emulator-locks     (only emulock may manage it)
#   allow everything else (device-agnostic adb, physical-device serials,
#         emulock itself, unrelated commands) — by staying silent
#         (exit 0, no output), which defers to Claude Code's normal permission
#         flow exactly as if this hook hadn't run at all.
#
# On every allowed device command it refreshes the lock's lease (last_used) and
# records what that command is about to do -- a fixed verb plus one narrowly
# extracted target ("Running flow", "live-show/guest-purchase.yaml"), never the
# command itself. See the activity section below for why, and for why the verb
# table matches tools rather than any project's wrapper scripts.
#
# Identity: Claude Code exposes a stable per-session id directly in the shell
# environment (CLAUDE_CODE_SESSION_ID, also present as .session_id in this
# hook's own stdin payload) — no payload-rewriting injection hook needed like
# Cursor's emulator-identity.sh (Cursor's shell has no native visibility into
# its conversation id, so a preToolUse hook has to rewrite the command to
# inject it). emulock's owner_id() picks the same env var up
# automatically, so this guard's notion of "this session" always matches what
# emulock recorded when it claimed the lock — no spoof-check needed
# either (there is no command-text injection here to spoof).
#
# Known limitations: only the top-level command string is inspected. Wrapper
# scripts that call adb internally (e.g. a project's own test wrapper) are not policed;
# they are expected to claim via emulock themselves. Quoted strings and
# heredoc bodies are stripped before matching (so commit messages / PR bodies
# mentioning adb don't false-positive), which also means `bash -c "adb ..."`
# escapes inspection — the guard is anti-accident, not anti-adversarial.

set -uo pipefail

JQ="$(command -v jq || echo /usr/bin/jq)"
LOCK_ROOT="${EMULATOR_LOCK_DIR:-$HOME/.emulator-locks}"

INPUT="$(cat)"
CMD="$("$JQ" -r '.tool_input.command // empty' <<<"$INPUT" 2>/dev/null)" || exit 0
SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
[[ -z "$SESSION_ID" ]] && SESSION_ID="$("$JQ" -r '.session_id // empty' <<<"$INPUT" 2>/dev/null)"
ME="${EMULATOR_LOCK_OWNER:-}"
[[ -z "$ME" && -n "$SESSION_ID" ]] && ME="claude-code:$SESSION_ID"
[[ -z "$ME" ]] && ME="manual:$(id -un)"

allow() { exit 0; }  # silence = defer to Claude Code's normal permission flow

deny() { # deny <agent+user message>
  "$JQ" -n --arg m "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse", permissionDecision:"deny", permissionDecisionReason:$m}}'
  exit 0
}

lock_owner() { sed -n 's/^OWNER_ID=//p' "$LOCK_ROOT/$1/meta" 2>/dev/null; }
lock_branch() { sed -n 's/^OWNER_BRANCH=//p' "$LOCK_ROOT/$1/meta" 2>/dev/null; }
lock_avd() { sed -n 's/^AVD=//p' "$LOCK_ROOT/$1/meta" 2>/dev/null; }
# --- activity ---------------------------------------------------------------
#
# The guard is the only component that sees a device command before it runs, so
# it is the only place that can answer "what is this session actually doing on
# that device". It records a fixed verb plus one narrowly-extracted target --
# never the command itself: ~/.emulator-locks is world-readable on a shared
# machine, and a raw command line carries paths, hostnames, and arguments that
# have no business sitting in it.
#
# This goes into last_used, whose *content* was previously unused -- every
# reader (emulock staleness, device-lab) keys off its mtime alone. So
# the write is backward compatible and refreshes the lease in the same step.
#
# Matching is on the tool, never on a project's wrapper: `maestro test` is
# portable, `a project's own test wrapper` is one repo's convention. Nothing here may
# assume a directory layout, a tracker's id format, or a particular agent
# harness -- a project that has none of those still gets a useful verb.
#
# Classification reads $CMD, not $SCMD: strip_prose removes quoted strings, and
# a flow path or test class is very often quoted. The extraction patterns below
# are narrow enough that this does not widen what reaches disk.

# Keep only what is safe to write: printable, no whitespace, bounded.
safe_target() {
  printf '%s' "$1" | LC_ALL=C tr -cd '[:alnum:]@._/:#=-' | cut -c1-64
}

# Keep the last two path segments -- "flows/live-show/guest.yaml" reads better
# than a basename and far better than an absolute path out of someone's $HOME.
tail_path() {
  local p="$1"
  [[ "$p" == */*/* ]] && p="$(printf '%s' "$p" | rev | cut -d/ -f1-2 | rev)"
  printf '%s' "$p"
}

ACTIVITY=""
ACTIVITY_DONE=0

# Regexes live in variables: an unquoted [[ =~ ]] pattern containing ; | & is
# parsed as shell syntax, not as part of the regex.
RE_MAESTRO='(^|[[:space:]/])maestro[[:space:]]+(test|record)([[:space:]]|$)'
RE_YAML='([A-Za-z0-9_./-]+[.]ya?ml)'
RE_INSTRUMENT='am[[:space:]]+instrument'
RE_CLASS='-e[[:space:]]+class[[:space:]]+([A-Za-z0-9_.]+(#[A-Za-z0-9_]+)?)'
RE_GRADLE_TEST='connected[A-Za-z0-9]*(AndroidTest|Test)([[:space:]]|$)'
RE_GRADLE_TASK='([A-Za-z0-9_:]*connected[A-Za-z0-9]*(AndroidTest|Test))'
RE_INSTALL='(adb[[:space:]]|gradlew)[^&]*[[:space:]](install(-multiple)?([[:space:]]|$)|install[A-Z])'
RE_APK='([A-Za-z0-9_./-]+[.]apk)'
RE_AMSTART='am[[:space:]]+start'
RE_DEEPLINK='-d[[:space:]]+["]?([a-zA-Z][a-zA-Z0-9+.-]*:/?/?[A-Za-z0-9_./-]*)'
RE_COMPONENT='-n[[:space:]]+["]?([A-Za-z0-9_.]+)/'
RE_RESET='(pm[[:space:]]+clear|am[[:space:]]+force-stop)[[:space:]]+([A-Za-z0-9_.]+)'
RE_RESET_ANY='(pm[[:space:]]+clear|am[[:space:]]+force-stop)'
RE_UNINSTALL='adb[^&]*[[:space:]]uninstall([[:space:]]|$)'
RE_LOGS='(logcat|bugreport)([[:space:]]|$)'
RE_INSPECT='(screencap|screenrecord|uiautomator[[:space:]]+dump|exec-out)'
RE_DRIVE='(shell[[:space:]]+input[[:space:]]|monkey[[:space:]])'
RE_COPY='adb[^&]*[[:space:]](push|pull)([[:space:]]|$)'
RE_BOOT='(^|[[:space:]/])emulator[[:space:]]'
RE_CONTROL='adb[^&]*[[:space:]](emu|reboot|root|unroot|remount)([[:space:]]|$)'
RE_WAIT='wait-for-'

classify_activity() {
  (( ACTIVITY_DONE )) && return 0
  ACTIVITY_DONE=1
  local c="$CMD" verb="" target=""

  # Test runners first: they subsume the install/launch they perform.
  if [[ "$c" =~ $RE_MAESTRO ]]; then
    verb="Running flow"
    [[ "$c" =~ $RE_YAML ]] && target="$(tail_path "${BASH_REMATCH[1]}")"
  elif [[ "$c" =~ $RE_INSTRUMENT ]]; then
    verb="Running tests"
    [[ "$c" =~ $RE_CLASS ]] && target="${BASH_REMATCH[1]}"
  elif [[ "$c" =~ $RE_GRADLE_TEST ]]; then
    verb="Running tests"
    [[ "$c" =~ $RE_GRADLE_TASK ]] && target="${BASH_REMATCH[1]}"

  # Install / launch / reset.
  elif [[ "$c" =~ $RE_INSTALL ]]; then
    verb="Installing"
    [[ "$c" =~ $RE_APK ]] && target="$(tail_path "${BASH_REMATCH[1]}")"
  elif [[ "$c" =~ $RE_AMSTART ]]; then
    if [[ "$c" =~ $RE_DEEPLINK ]]; then
      verb="Opening link"; target="${BASH_REMATCH[1]}"
    else
      verb="Launching"
      [[ "$c" =~ $RE_COMPONENT ]] && target="${BASH_REMATCH[1]}"
    fi
  elif [[ "$c" =~ $RE_RESET_ANY ]]; then
    verb="Resetting app"
    [[ "$c" =~ $RE_RESET ]] && target="${BASH_REMATCH[2]}"
  elif [[ "$c" =~ $RE_UNINSTALL ]]; then
    verb="Uninstalling"

  # Observation and input.
  elif [[ "$c" =~ $RE_LOGS ]]; then
    verb="Reading logs"
  elif [[ "$c" =~ $RE_INSPECT ]]; then
    verb="Inspecting UI"
  elif [[ "$c" =~ $RE_DRIVE ]]; then
    verb="Driving UI"
  elif [[ "$c" =~ $RE_COPY ]]; then
    verb="Copying files"

  # Device lifecycle.
  elif [[ "$c" =~ $RE_BOOT ]]; then
    verb="Booting"
  elif [[ "$c" =~ $RE_CONTROL ]]; then
    verb="Controlling device"
  elif [[ "$c" =~ $RE_WAIT ]]; then
    verb="Waiting for device"
  fi

  [[ -z "$verb" ]] && return 0
  target="$(safe_target "$target")"
  if [[ -n "$target" ]]; then
    ACTIVITY="$verb	$target"
  else
    ACTIVITY="$verb"
  fi
  return 0
}

# Refresh the lease and record what this command is about to do. PreToolUse
# fires before execution, so this is intent, not outcome -- indistinguishable
# on a dashboard that polls every couple of seconds, and it means a command
# that hangs still shows what it is hanging on.
touch_lease() {
  [[ -d "$LOCK_ROOT/$1" ]] || return 0
  classify_activity
  if [[ -n "$ACTIVITY" ]]; then
    # One short line, one write: the redirect sets mtime exactly as touch did.
    printf '%s\n' "$ACTIVITY" >"$LOCK_ROOT/$1/last_used" 2>/dev/null || touch "$LOCK_ROOT/$1/last_used" 2>/dev/null
  else
    touch "$LOCK_ROOT/$1/last_used" 2>/dev/null
  fi
  return 0
}

CLAIM_HINT="Claim a device first: emulock claim (protocol: the README). Check owners with: emulock status"

[[ -z "$CMD" ]] && allow

# Strip heredoc bodies and quoted strings before matching: prose in commit
# messages, PR bodies, and echo strings legitimately mentions adb/emulator and
# must not trip enforcement. Real device commands live outside quotes.
strip_prose() {
  awk '
    skip {
      if ($0 ~ ("^[[:space:]]*" delim "[[:space:]]*$")) skip = 0
      next
    }
    match($0, /<<-?[[:space:]]*["'\'']?[A-Za-z_][A-Za-z0-9_]*/) {
      delim = substr($0, RSTART, RLENGTH)
      sub(/<<-?[[:space:]]*["'\'']?/, "", delim)
      skip = 1
      print
      next
    }
    { print }
  ' | sed -e "s/'[^']*'//g" -e 's/"[^"]*"//g'
}
SCMD="$(printf '%s\n' "$CMD" | strip_prose)"

has_adb=0
echo "$SCMD" | grep -qE '(^|[/[:space:];&|(])adb([[:space:]]|$)' && has_adb=1

# --- adb kill-server: never allowed (drops every agent's device connections) ---
if echo "$SCMD" | grep -qE '(^|[/[:space:];&|(])adb[[:space:]]+kill-server'; then
  deny "adb kill-server is forbidden — it disconnects every agent's devices at once. Retry 'adb -s <serial> wait-for-device' instead."
fi

# --- lock-store tampering: only emulock manages ~/.emulator-locks ---
if echo "$SCMD" | grep -q '\.emulator-locks' && [[ "$CMD" != *emulock* ]]; then
  if echo "$SCMD" | grep -qE '(^|[[:space:];&|(])(rm|mv|touch|mkdir|cp)[[:space:]]'; then
    deny "Do not modify ~/.emulator-locks directly — use emulock (claim/release/reap)."
  fi
fi

# --- emulock itself: always allowed. Nothing to spoof-check here —
# unlike Cursor, there is no injection hook rewriting this command, so
# EMULATOR_LOCK_OWNER (if set at all) is this session's own real environment. ---
if [[ "$CMD" == *emulock* ]]; then
  allow
fi

# --- explicit serials: adb -s emulator-XXXX must be a lock this session owns ---
if (( has_adb )); then
  serials="$(echo "$SCMD" | grep -oE -- '(-s|--serial)[[:space:]=]+emulator-[0-9]+' | grep -oE 'emulator-[0-9]+' | sort -u)"
  for serial in $serials; do
    if [[ ! -d "$LOCK_ROOT/$serial" ]]; then
      deny "$serial is not claimed by anyone — you may not use unclaimed devices. $CLAIM_HINT"
    fi
    owner="$(lock_owner "$serial")"
    if [[ -z "$owner" ]]; then
      touch_lease "$serial"   # legacy pre-hook lock: grace until it ages out
      continue
    fi
    if [[ "$owner" != "$ME" ]]; then
      deny "$serial belongs to another agent (branch: $(lock_branch "$serial")). Never touch a device you didn't claim. $CLAIM_HINT"
    fi
    touch_lease "$serial"
  done

  # --- device-targeting adb without any -s: ambiguous and dangerous ---
  if [[ -z "$serials" ]] && ! echo "$SCMD" | grep -qE -- '(-s|--serial)[[:space:]=]'; then
    if echo "$SCMD" | grep -qE 'adb[[:space:]]+(wait-for-[a-z]+|shell|install|install-multiple|uninstall|push|pull|logcat|exec-out|emu|reboot|sideload|forward|reverse|backup|restore|root|unroot|remount|tcpip|usb|jdwp|bugreport|get-state|get-serialno|snapshot)([[:space:]]|$)'; then
      deny "Bare device-targeting adb is forbidden — always target your claimed serial explicitly: adb -s <your-serial> ... $CLAIM_HINT"
    fi
  fi
fi

# --- emulator launches: must use the -port of a lock this session owns ---
if echo "$SCMD" | grep -qE '(^|[/[:space:];&|(])emulator[[:space:]]+[^;|&]*(@[A-Za-z0-9_.-]+|-avd[[:space:]]+[A-Za-z0-9_.-]+)'; then
  port="$(echo "$SCMD" | grep -oE -- '-port[[:space:]]+[0-9]+' | grep -oE '[0-9]+' | head -n1)"
  if [[ -z "$port" ]]; then
    deny "Emulator launches must use the -port printed by emulock claim (a portless launch grabs an arbitrary port and collides with other agents). $CLAIM_HINT"
  fi
  serial="emulator-$port"
  if [[ ! -d "$LOCK_ROOT/$serial" ]]; then
    deny "Port $port is not reserved. $CLAIM_HINT"
  fi
  owner="$(lock_owner "$serial")"
  if [[ -n "$owner" && "$owner" != "$ME" ]]; then
    deny "Port $port is reserved by another agent (branch: $(lock_branch "$serial")). $CLAIM_HINT"
  fi
  launched_avd="$(echo "$SCMD" | grep -oE '@[A-Za-z0-9_.-]+' | head -n1 | tr -d '@')"
  [[ -z "$launched_avd" ]] && launched_avd="$(echo "$SCMD" | grep -oE -- '-avd[[:space:]]+[A-Za-z0-9_.-]+' | awk '{print $2}' | head -n1)"
  locked_avd="$(lock_avd "$serial")"
  if [[ -n "$launched_avd" && -n "$locked_avd" && "$locked_avd" != "unknown" && "$launched_avd" != "$locked_avd" ]]; then
    deny "Port $port is reserved for AVD '$locked_avd', not '$launched_avd'. Boot the AVD your claim printed, or reclaim: emulock reclaim"
  fi
  touch_lease "$serial"
fi

# --- activity for tools that select a device without adb ---------------------
#
# Test runners take their own device flag (`maestro --device`, ANDROID_SERIAL,
# MAESTRO_DEVICE), so the adb branches above never see them -- which meant the
# single most interesting thing a session does with a device, running a suite,
# was the one thing that recorded nothing.
#
# Recording only. Enforcement is deliberately unchanged: these commands were
# already allowed (the guard inspects only the top-level command, and a wrapper
# that shells out to adb is expected to claim for itself), and a lock this
# session does not own is skipped in silence rather than denied. Widening
# observability is a separate decision from widening enforcement.
if [[ -z "${serials:-}" ]]; then
  for other_serial in $(printf '%s\n' "$CMD" \
      | grep -oE '(--device|--serial|ANDROID_SERIAL=|MAESTRO_DEVICE=)[[:space:]=]*emulator-[0-9]+' \
      | grep -oE 'emulator-[0-9]+' | sort -u); do
    [[ -d "$LOCK_ROOT/$other_serial" ]] || continue
    other_owner="$(lock_owner "$other_serial")"
    [[ -n "$other_owner" && "$other_owner" != "$ME" ]] && continue
    touch_lease "$other_serial"
  done
fi

allow
