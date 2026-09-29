#!/usr/bin/env bash
#
# emulock-guard.sh — Claude Code PreToolUse hook (matcher: Bash).
#
# Refuses shell commands that touch emulator devices this session has not
# claimed through `emulock`. Wire it in settings.json; commit that file and
# every contributor gets enforcement with no install step of their own.
#
#   deny  adb kill-server                     drops every agent's devices at once
#   deny  adb -s emulator-XXXX ...            unless this session owns the lock
#   deny  device-targeting adb without -s     bare `adb shell`, `adb install`, ...
#   deny  emulator @AVD launches              without the -port of a lock we own
#   deny  pool boots with a window or -gpu    the emulator deletes the shared golden snapshot
#   deny  gradlew install*/uninstall*/connected*  unless ANDROID_SERIAL names a lock we own
#   deny  direct writes to the lock store     only `emulock` may manage it
#   allow everything else — by staying silent (exit 0, no output), which defers
#         to the harness's normal permission flow exactly as if this hook had
#         not run at all. Device-agnostic adb, physical serials, `emulock`
#         itself and unrelated commands all fall through here.
#
# On every allowed device command it refreshes the lock's lease (last_used) and
# records what that command is about to do -- a fixed verb plus one narrowly
# extracted target ("Running flow", "live-show/guest-purchase.yaml"), never the
# command itself. See the activity section below for why, and for why the verb
# table matches tools rather than any project's wrapper scripts.
#
# Identity: Claude Code exposes a stable per-session id in the shell environment
# (CLAUDE_CODE_SESSION_ID, also present as .session_id in this hook's stdin
# payload). `emulock` reads the same variable, so the guard's notion of "this
# session" always matches what was recorded when the lock was claimed. Set
# EMULATOR_LOCK_OWNER to give another harness a stable identity; without either,
# identity falls back to manual:$USER and locks are advisory only.
#
# Known limitations: only the top-level command string is inspected. A wrapper
# script that calls adb internally is not policed; it is expected to claim for
# itself. Quoted strings and heredoc bodies are stripped before matching, so a
# commit message mentioning adb does not trip enforcement -- which also means
# `bash -c "adb ..."` escapes inspection. This is anti-accident, not
# anti-adversarial.

set -uo pipefail

JQ="$(command -v jq || echo /usr/bin/jq)"
LOCK_ROOT="${EMULATOR_LOCK_DIR:-$HOME/.emulator-locks}"
AVD_HOME="${ANDROID_AVD_HOME:-$HOME/.android/avd}"
# The guard reads no project config (it runs before every command and must stay
# cheap); a pool AVD is recognised by the marker `emulock pool bake` leaves in it.
POOL_AVD_ENV="${EMULOCK_POOL_AVD:-${EMULATOR_POOL_AVD:-}}"

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
# portable, `./scripts/run-e2e.sh` is one repo's convention. Nothing here may
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

# --- what to do next ---------------------------------------------------------
#
# A refusal is the one message an agent reads at the exact moment it needs
# direction, so it has to carry the answer. The old hint was identical whether
# three devices were free or none -- two situations demanding opposite
# behaviour: claim one, versus stop and tell the human. Given only the generic
# hint, an agent retries in a loop.
#
# Computed from the lock store alone -- directory names and mtimes, no
# subprocesses. This hook runs before *every* shell command, so the work is
# confined to the deny paths, and even there it must not shell out. In
# particular it must never call `adb`: that implicitly starts the adb daemon,
# and a refusal has no business spawning a background server.
#
# It reports a count, never a specific serial. Two agents refused in the same
# instant would both be pointed at the same device and one would lose a race it
# had just been promised. `emulock claim` does the atomic mkdir tie-break.

IDLE_TTL="${EMULATOR_LOCK_IDLE_TTL:-14400}"

mtime_of() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

human_secs() { # 9240 -> "2h 34m"
  local s="$1" h m
  [[ "$s" -lt 60 ]] && { echo "under a minute"; return 0; }
  h=$(( s / 3600 )); m=$(( (s % 3600) / 60 ))
  if [[ "$h" -gt 0 ]]; then echo "${h}h ${m}m"; else echo "${m}m"; fi
}

availability_hint() {
  local now held=0 expired=0 soonest="" d idle left stamp
  now="$(date +%s)"
  for d in "$LOCK_ROOT"/emulator-*; do
    [[ -d "$d" ]] || continue
    # Only other sessions' locks say anything about what is left for you.
    [[ "$(lock_owner "${d##*/}")" == "$ME" ]] && continue
    held=$(( held + 1 ))
    stamp="$(mtime_of "$d/last_used")"
    idle=$(( now - stamp ))
    if [[ "$idle" -ge "$IDLE_TTL" ]]; then
      expired=$(( expired + 1 ))
    else
      left=$(( IDLE_TTL - idle ))
      if [[ -z "$soonest" || "$left" -lt "$soonest" ]]; then soonest="$left"; fi
    fi
  done

  # The hook cannot see which emulators are running or which AVDs are free (that
  # needs adb), so it never decides the machine is full: `emulock claim` does, and
  # says so itself. What the lock store can add is how busy it is.
  if [[ "$held" -eq 0 ]]; then
    echo "No other session holds a device — claim your own: emulock claim --pool (or emulock claim)"
  elif [[ "$expired" -gt 0 ]]; then
    echo "$held held by other sessions, $expired with an expired lease and reclaimable now — claim your own: emulock claim --pool (or emulock claim)"
  elif [[ -n "$soonest" ]]; then
    echo "$held held by other sessions; the earliest lease frees in $(human_secs "$soonest"). Claim your own: emulock claim --pool (or emulock claim) finds a free one, or says when none is left — then tell the user rather than taking a device that isn't yours."
  else
    echo "Claim a device first: emulock claim --pool (or emulock claim)"
  fi
}

CLAIM_HINT="Claim a device first: emulock claim --pool (or emulock claim). Check owners with: emulock status"

# require_owned <emulator-serial>: deny unless this session holds its lock,
# otherwise refresh the lease. An owner-less lock predates the hook and gets a
# grace lease until it ages out.
require_owned() {
  local serial="$1" owner
  if [[ ! -d "$LOCK_ROOT/$serial" ]]; then
    deny "$serial is not claimed by anyone — you may not use an unclaimed device. $(availability_hint)"
  fi
  owner="$(lock_owner "$serial")"
  if [[ -n "$owner" && "$owner" != "$ME" ]]; then
    deny "$serial belongs to another agent (branch: $(lock_branch "$serial")). Never touch a device you didn't claim. $(availability_hint)"
  fi
  touch_lease "$serial"
}

[[ -z "$CMD" ]] && allow

# Strip heredoc bodies and quoted strings before matching: prose in commit
# messages, PR bodies, and echo strings legitimately mentions adb/emulator and
# must not trip enforcement. Real device commands live outside quotes.
#
# The newlines are joined before the quote strip because sed works a line at a
# time: a multi-line "..." argument would otherwise have only its first line
# removed, and the rest matched as if it were a command. A `git commit -m` with
# a multi-paragraph message mentioning adb was refused this way -- the exact
# false positive the stripping exists to prevent. Matching does not depend on
# line structure, so folding to one line costs nothing.
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
  ' | tr '\n' ' ' | sed -e "s/'[^']*'//g" -e 's/"[^"]*"//g'
}
SCMD="$(printf '%s\n' "$CMD" | strip_prose)"

has_adb=0
echo "$SCMD" | grep -qE '(^|[/[:space:];&|(])adb([[:space:]]|$)' && has_adb=1

# --- adb kill-server: never allowed (drops every agent's device connections) ---
if echo "$SCMD" | grep -qE '(^|[/[:space:];&|(])adb[[:space:]]+kill-server'; then
  deny "adb kill-server is forbidden — it disconnects every agent's devices at once. Retry 'adb -s <serial> wait-for-device' instead."
fi

# --- lock-store tampering: only emulock manages ~/.emulator-locks ---
if echo "$SCMD" | grep -q '\.emulator-locks' && ! [[ "$SCMD" =~ ^[[:space:]]*([^[:space:]]*/)?emulock[[:space:]] ]]; then
  if echo "$SCMD" | grep -qE '(^|[[:space:];&|(])(rm|mv|touch|mkdir|cp)[[:space:]]'; then
    deny "Do not modify ~/.emulator-locks directly — use emulock (claim/release/reap)."
  fi
fi

# --- pool boots are headless only. `golden` is saved by a headless boot with
# the default GPU, and a snapshot only loads under the renderer it was saved
# with. Load it with a window or a -gpu flag and the emulator logs "different
# renderer configured", DELETES the shared snapshot and exits
# (-force-snapshot-load does not stop it), so `claim --pool` breaks for every
# agent until a rebake. Only a boot that loads a snapshot (-read-only or
# -snapshot) is held to this: the bake's own cold boot has neither. The flags
# are read from the pool boot's own segment, comments removed, and this runs
# before the emulock allow below so a boot chained after a claim is still
# checked. ---
join_continuations() { awk '{ if (sub(/\\$/, "")) printf "%s ", $0; else print }'; }
has_flag() { # has_flag <text> <flag regex>: the flag as a whole word (-x, --x, -x=v)
  printf '%s\n' "$1" | grep -qE -- "(^|[[:space:]])$2([[:space:]=)]|\$)"
}
is_pool_avd() { # is_pool_avd <avd>: named by env, or marked by `emulock pool bake`
  [[ -n "$1" ]] || return 1
  [[ "$1" == "$POOL_AVD_ENV" || -f "$AVD_HOME/$1.avd/emulock-pool" || -f "$AVD_HOME/$1.avd/golden.json" ]]
}
boots="$(printf '%s\n' "$SCMD" | sed -E 's/(^|[[:space:]])#.*$//' | join_continuations \
  | sed -E 's/[0-9]*>&[0-9-]*//g; s/&>>?//g' | tr ';|&' '\n\n\n' \
  | grep -E -- '(^|[/[:space:](])emulator[[:space:]].*(@|-avd[[:space:]]+)[A-Za-z0-9_.-]+' || true)"
while IFS= read -r boot; do
  [[ -n "$boot" ]] || continue
  boot_avd="$(printf '%s\n' "$boot" | grep -oE '@[A-Za-z0-9_.-]+' | head -n1 | tr -d '@')"
  [[ -z "$boot_avd" ]] && boot_avd="$(printf '%s\n' "$boot" | grep -oE -- '-avd[[:space:]]+[A-Za-z0-9_.-]+' | awk '{print $2}' | head -n1)"
  is_pool_avd "$boot_avd" || continue
  has_flag "$boot" '--?(read-only|snapshot)' || continue
  if ! has_flag "$boot" '--?no-window' || has_flag "$boot" '--?gpu'; then
    deny "Pool instances are headless only. '$boot_avd' boots from a snapshot saved headless with the default GPU; dropping -no-window or adding -gpu makes the emulator delete that shared snapshot and exit, which breaks claim --pool for every agent until a rebake. Run the boot command from emulock claim --pool verbatim. If someone must see or type on the device, release the pool claim and claim a named AVD instead, which boots with a window: emulock claim --avd <name>."
  fi
done <<<"$boots"

# --- emulock itself: always allowed. Nothing to spoof-check here —
# nothing rewrites this command on its way here, so EMULATOR_LOCK_OWNER (if set
# at all) comes from this session's own environment. ---
# Only a command that IS one emulock call: `*emulock*` let anything through
# that merely mentioned the word, such as `cd ~/src/emulock && adb -s <theirs> ...`.
# Anything chained falls through to the checks below, which an emulock call passes.
RE_EMULOCK_ONLY='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*([^[:space:];&|]*/)?emulock([[:space:]][^;&|]*)?$'
if [[ "$SCMD" =~ $RE_EMULOCK_ONLY ]]; then
  allow
fi

# --- explicit serials: adb -s emulator-XXXX must be a lock this session owns ---
if (( has_adb )); then
  serials="$(echo "$SCMD" | grep -oE -- '(-s|--serial)[[:space:]=]+emulator-[0-9]+' | grep -oE 'emulator-[0-9]+' | sort -u)"
  for serial in $serials; do
    require_owned "$serial"
  done

  # --- device-targeting adb without any -s: ambiguous and dangerous ---
  if [[ -z "$serials" ]] && ! echo "$SCMD" | grep -qE -- '(-s|--serial)[[:space:]=]'; then
    if echo "$SCMD" | grep -qE 'adb[[:space:]]+(wait-for-[a-z]+|shell|install|install-multiple|uninstall|push|pull|logcat|exec-out|emu|reboot|sideload|forward|reverse|backup|restore|root|unroot|remount|tcpip|usb|jdwp|bugreport|get-state|get-serialno|snapshot)([[:space:]]|$)'; then
      deny "Bare device-targeting adb is forbidden — always target your claimed serial explicitly: adb -s <your-serial> ... $(availability_hint)"
    fi
  fi
fi

# --- Gradle device tasks: install*/uninstall*/connected* do their own device
# discovery and act on EVERY connected device ("Installed on 2 devices" -- one
# of them another session's). AGP narrows them to specific devices only via
# ANDROID_SERIAL (comma-separated), so require it inline and hold each emulator
# serial to the -s rule above. The serial is read from the raw command because
# a quoted value is stripped from SCMD; a $VAR value can't be checked here, so
# it must be spelled out. Task names are matched only in the gradle command's
# own segment, so `./gradlew tasks | grep installDebug` stays allowed. ---
gradle_segments="$(echo "$SCMD" | tr ';|&' '\n\n\n' | grep -E '(^|[/[:space:](])gradlew?([[:space:]]|$)' || true)"
if [[ -n "$gradle_segments" ]] \
  && echo "$gradle_segments" | grep -qE '(^|[[:space:]:])((un)?install|connected)[A-Z][A-Za-z]*'; then
  gradle_serials="$(echo "$CMD" | grep -oE "ANDROID_SERIAL=[\"']?[^[:space:]\"';&|]+" \
    | sed -E "s/^ANDROID_SERIAL=[\"']?//" | tr ',' '\n' | sort -u)"
  if [[ -z "$gradle_serials" ]]; then
    deny "Gradle install/uninstall/connected* tasks run on EVERY connected device, including other agents' emulators. Target your claimed serial: ANDROID_SERIAL=<your-serial> ./gradlew <task>, or ./gradlew assemble<Variant> then adb -s <your-serial> install -r <apk>. $CLAIM_HINT"
  fi
  for serial in $gradle_serials; do
    case "$serial" in
      \$*) deny "Spell ANDROID_SERIAL out literally (ANDROID_SERIAL=emulator-NNNN) — a \$variable can't be checked against your claim. $CLAIM_HINT" ;;
      emulator-*) require_owned "$serial" ;;
    esac
  done
fi

# --- emulator launches: must use the -port of a lock this session owns ---
if echo "$SCMD" | grep -qE '(^|[/[:space:];&|(])emulator[[:space:]]+[^;|&]*(@[A-Za-z0-9_.-]+|-avd[[:space:]]+[A-Za-z0-9_.-]+)'; then
  port="$(echo "$SCMD" | grep -oE -- '-port[[:space:]]+[0-9]+' | grep -oE '[0-9]+' | head -n1)"
  if [[ -z "$port" ]]; then
    deny "Emulator launches must use the -port printed by emulock claim (a portless launch grabs an arbitrary port and collides with other agents). $CLAIM_HINT"
  fi
  serial="emulator-$port"
  if [[ ! -d "$LOCK_ROOT/$serial" ]]; then
    deny "Port $port is not reserved. $(availability_hint)"
  fi
  owner="$(lock_owner "$serial")"
  if [[ -n "$owner" && "$owner" != "$ME" ]]; then
    deny "Port $port is reserved by another agent (branch: $(lock_branch "$serial")). $(availability_hint)"
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
