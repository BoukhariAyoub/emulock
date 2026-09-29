#!/usr/bin/env bash
#
# Test suite for the emulator reservation tools. Zero dependencies: bash and
# python3, both already required to run the tools themselves. No bats, no pip.
#
#   tests/run.sh              # run everything
#   tests/run.sh guard        # run one group (shells|guard|state|lock|py)
#
# `py` runs the unittest suites next to this file (pool, guard rules, doctor,
# evidence, CLI). They need nothing but python3 either.
#
# Every test runs against a scratch EMULATOR_LOCK_DIR under $TMPDIR. Nothing
# here reads or writes the real ~/.emulator-locks, so it is safe to run while
# agents hold live devices -- which is exactly when you want to run it.
#
# Why this exists: emulator-guard.sh is a PreToolUse hook on *every* shell
# command. A syntax error in it does not just break adb, it blocks all shell
# access for every session on the machine, and agents cannot repair it because
# editing hooks is self-modification. A regression here is a machine-wide
# outage, so the guard gets tested under the oldest bash it must run on.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
GUARD="$ROOT/hooks/claude-code/emuriad-guard.sh"
LOCK="$ROOT/bin/emuriad"
LAB="$ROOT/libexec/device_lab.py"
POOL_SH="$ROOT/libexec/emuriad-pool.sh"

PASS=0
FAIL=0
FAILED_NAMES=()

ok()   { PASS=$((PASS + 1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); FAILED_NAMES+=("$1"); printf '  \033[31mFAIL\033[0m %s\n' "$1"
         printf '         expected: %s\n         actual:   %s\n' "$2" "$3"; }
is()   { [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "$3" "$2"; }
has()  { [[ "$2" == *"$3"* ]] && ok "$1" || bad "$1" "contains '$3'" "$2"; }
group() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# --- scratch lock store ------------------------------------------------------
# MINE is owned by the fake session the tests run as; THEIRS by someone else.
SESSION="testsession"
MINE="emulator-5599"
THEIRS="emulator-5601"

setup_store() {
  STORE="$(mktemp -d)"
  mkdir -p "$STORE/$MINE" "$STORE/$THEIRS"
  cat >"$STORE/$MINE/meta" <<EOF
SERIAL=$MINE
AVD=test_avd
PORT=5599
STATE=booting
OWNER_ID=claude-code:$SESSION
OWNER_BRANCH=main
EOF
  cat >"$STORE/$THEIRS/meta" <<EOF
SERIAL=$THEIRS
AVD=other_avd
PORT=5601
STATE=running
OWNER_ID=claude-code:someone-else
OWNER_BRANCH=feature/theirs
EOF
}
teardown_store() { [[ -n "${STORE:-}" && "$STORE" == /*/* ]] && rm -rf "$STORE"; }

# Feed a command to the guard exactly as Claude Code would, and capture both
# the permission decision and whatever activity got recorded.
hook() { # hook <command> [bash-binary]; echoes "<decision>|<activity>"
  local cmd="$1" shell="${2:-/bin/bash}" out decision activity
  : >"$STORE/$MINE/last_used"
  out="$(printf '%s' "$cmd" | python3 -c '
import json, sys
print(json.dumps({"session_id": "'"$SESSION"'",
                  "tool_input": {"command": sys.stdin.read()}}))' \
    | EMULATOR_LOCK_DIR="$STORE" CLAUDE_CODE_SESSION_ID="$SESSION" \
      "$shell" "$GUARD" 2>/dev/null)"
  decision=allow
  [[ "$out" == *'"deny"'* ]] && decision=deny
  activity="$(tr '\t' '|' <"$STORE/$MINE/last_used" 2>/dev/null | head -1)"
  printf '%s|%s' "$decision" "$activity"
}

act() { hook "$1" | cut -d'|' -f2-; }   # just the recorded activity
dec() { hook "$1" | cut -d'|' -f1; }    # just allow/deny

# =============================================================================
test_shells() {
  group "parses under every bash it must run on"
  local sh
  for sh in /bin/bash bash; do
    if command -v "$sh" >/dev/null 2>&1; then
      local v; v="$("$sh" --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1)"
      "$sh" -n "$GUARD" 2>/dev/null && ok "guard parses under $sh ($v)" \
        || bad "guard parses under $sh ($v)" "clean parse" "syntax error"
      "$sh" -n "$LOCK" 2>/dev/null && ok "lock parses under $sh ($v)" \
        || bad "lock parses under $sh ($v)" "clean parse" "syntax error"
      "$sh" -n "$POOL_SH" 2>/dev/null && ok "pool parses under $sh ($v)" \
        || bad "pool parses under $sh ($v)" "clean parse" "syntax error"
    fi
  done
  local py
  for py in "$ROOT"/libexec/*.py; do
    python3 -c "import ast, sys; ast.parse(open(sys.argv[1]).read())" "$py" \
      && ok "$(basename "$py") parses" || bad "$(basename "$py") parses" "clean parse" "syntax error"
  done
}

# =============================================================================
test_guard() {
  setup_store
  group "guard: enforcement (allow / deny)"
  is "own serial is allowed"            "$(dec "adb -s $MINE shell ls")"    allow
  is "another session's serial denied"  "$(dec "adb -s $THEIRS shell ls")"  deny
  is "unclaimed serial denied"          "$(dec "adb -s emulator-9999 shell ls")" deny
  is "kill-server always denied"        "$(dec "adb kill-server")"          deny
  is "bare device adb denied"           "$(dec "adb shell pm list packages")" deny
  is "portless emulator launch denied"  "$(dec "emulator @some_avd")"       deny
  is "non-device command untouched"     "$(dec "git status")"               allow
  is "unit tests untouched"             "$(dec "./gradlew testDebugUnitTest")" allow
  is "lock script itself allowed"       "$(dec "emuriad status")"  allow
  is "pre-rename emulock shim allowed"  "$(dec "emulock status")"  allow
  is "direct lock-store writes denied"  "$(dec "rm -rf ~/.emulator-locks/emulator-5599")" deny

  group "guard: prose must not trip enforcement"
  is "adb inside a quoted string"  "$(dec "git commit -m 'never run adb kill-server'")" allow
  is "adb inside a heredoc"        "$(dec "$(printf 'cat <<EOF\nadb kill-server\nEOF')")" allow
  # Regression: sed strips quotes a line at a time, so a multi-line quoted
  # argument used to keep everything after its first line -- a git commit whose
  # message mentioned adb kill-server was refused by its own guard.
  is "adb in a multi-line commit message" \
     "$(dec "$(printf 'git commit -m "Fix the thing\n\nNever run adb kill-server here.\n\nIt drops every device."')")" allow
  is "adb in a multi-line echo" \
     "$(dec "$(printf 'echo "line one\nadb -s emulator-5601 shell ls\nline three"')")" allow

  group "guard: activity classification"
  is "maestro flow"      "$(act "maestro test --device $MINE flows/live-show/guest.yaml")" "Running flow|live-show/guest.yaml"
  is "instrumented test" "$(act "adb -s $MINE shell am instrument -w -e class co.x.CheckoutTest#guest co.x/androidx.test.runner.AndroidJUnitRunner")" "Running tests|co.x.CheckoutTest#guest"
  is "gradle connected"  "$(act "ANDROID_SERIAL=$MINE ./gradlew :app:connectedDebugAndroidTest")" "Running tests|:app:connectedDebugAndroidTest"
  is "install"           "$(act "adb -s $MINE install build/outputs/app-debug.apk")" "Installing|outputs/app-debug.apk"
  is "launch component"  "$(act "adb -s $MINE shell am start -n com.example/.MainActivity")" "Launching|com.example"
  is "deep link"         "$(act "adb -s $MINE shell am start -a android.intent.action.VIEW -d https://example.com/s/x")" "Opening link|https://example.com/s/x"
  is "logcat"            "$(act "adb -s $MINE logcat -d")"                   "Reading logs"
  is "screencap"         "$(act "adb -s $MINE exec-out screencap -p")"       "Inspecting UI"
  is "input"             "$(act "adb -s $MINE shell input tap 10 20")"       "Driving UI"
  is "pm clear"          "$(act "adb -s $MINE shell pm clear com.example")"  "Resetting app|com.example"
  is "pull"              "$(act "adb -s $MINE pull /sdcard/x .")"            "Copying files"
  is "wait-for-device"   "$(act "adb -s $MINE wait-for-device")"             "Waiting for device"
  is "unclassifiable leaves no verb" "$(act "adb -s $MINE forward tcp:1 tcp:2")" ""
  is "non-device command records nothing" "$(act "git status")"              ""

  group "guard: activity must not leak command contents"
  local leaky
  leaky="$(act "adb -s $MINE shell am start -n com.example/.Main -e token SUPERSECRET123 --es pw hunter2")"
  case "$leaky" in
    *SUPERSECRET123*|*hunter2*) bad "secrets never reach the lock store" "no secret" "$leaky" ;;
    *) ok "secrets never reach the lock store" ;;
  esac
  local homey
  homey="$(act "adb -s $MINE install /Users/somebody/private/build/app.apk")"
  case "$homey" in
    */Users/somebody*) bad "absolute home paths are trimmed" "trimmed path" "$homey" ;;
    *) ok "absolute home paths are trimmed" ;;
  esac

  group "guard: activity respects ownership"
  : >"$STORE/$THEIRS/last_used"
  hook "maestro test --device $THEIRS flows/x.yaml" >/dev/null
  is "no recording on another session's device" "$(cat "$STORE/$THEIRS/last_used")" ""
  hook "adb -s $THEIRS shell ls" >/dev/null
  is "denied command records nothing"           "$(cat "$STORE/$THEIRS/last_used")" ""

  group "guard: a refusal says what to do next"
  # The message an agent reads at the moment it needs direction must differ by
  # pool state -- "claim one" and "stop and tell the human" are opposite actions.
  local hint_probe hint now
  hint_probe="$(mktemp -d)"
  now="$(date +%s)"
  mk_lock() { # mk_lock <serial> <idle-seconds>
    mkdir -p "$hint_probe/$1"
    printf 'SERIAL=%s\nOWNER_ID=claude-code:someone\nOWNER_BRANCH=feat/x\n' "$1" >"$hint_probe/$1/meta"
    : >"$hint_probe/$1/last_used"
    touch -t "$(date -r $((now - $2)) +%Y%m%d%H%M.%S)" "$hint_probe/$1/last_used"
  }
  ask_hint() {
    printf '%s' "adb -s emulator-5599 shell ls" \
      | python3 -c 'import json,sys; print(json.dumps({"session_id":"'"$SESSION"'","tool_input":{"command":sys.stdin.read()}}))' \
      | EMULATOR_LOCK_DIR="$hint_probe" CLAUDE_CODE_SESSION_ID="$SESSION" /bin/bash "$GUARD" 2>/dev/null \
      | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["hookSpecificOutput"]["permissionDecisionReason"])
except Exception: print("")'
  }

  hint="$(ask_hint)"
  has "empty store tells you to claim" "$hint" "No other session holds a device"

  mk_lock emulator-5599 600
  mk_lock emulator-5601 9240
  hint="$(ask_hint)"
  has "a busy store still sends you to claim" "$hint" "Check in to your own: emuriad check-in"
  has "...says when a lease frees"             "$hint" "frees in"
  has "...and not to take someone else's"      "$hint" "tell the user rather than taking"

  mk_lock emulator-5603 20000
  hint="$(ask_hint)"
  has "expired lease is surfaced as reclaimable" "$hint" "expired lease and reclaimable now"

  # Your own locks are not "held" as far as what is left for you goes.
  local own_probe="$hint_probe"
  hint_probe="$(mktemp -d)"
  mkdir -p "$hint_probe/emulator-5605"
  printf 'SERIAL=emulator-5605\nOWNER_ID=claude-code:%s\n' "$SESSION" >"$hint_probe/emulator-5605/meta"
  : >"$hint_probe/emulator-5605/last_used"
  hint="$(ask_hint)"
  has "your own lock does not count as held" "$hint" "No other session holds a device"
  rm -rf "$hint_probe"
  hint_probe="$own_probe"

  # Never name a serial: two agents refused at once would race for it.
  case "$hint" in
    *"emuriad check-in emulator-"*|*"check-in emulator-5"*|*"claim emulator-5"*)
      bad "hint never names a specific serial" "a count, not a serial" "$hint" ;;
    *) ok "hint never names a specific serial" ;;
  esac
  rm -rf "$hint_probe"

  group "guard: degrades safely when jq is missing"
  # A hook that cannot parse its input must allow, not deny: denying on its own
  # breakage would block every shell command on the machine with no way to
  # repair it. The cost is that enforcement disappears silently, which is why
  # `emuriad doctor` checks for jq explicitly.
  local nojq_probe nojq_out
  nojq_probe="$(mktemp -d)"
  sed 's|^JQ=.*|JQ="/nonexistent/jq"|' "$GUARD" >"$nojq_probe/guard.sh"
  nojq_out="$(printf '%s' "adb -s $THEIRS shell ls" \
    | python3 -c 'import json,sys; print(json.dumps({"session_id":"'"$SESSION"'","tool_input":{"command":sys.stdin.read()}}))' \
    | EMULATOR_LOCK_DIR="$STORE" CLAUDE_CODE_SESSION_ID="$SESSION" /bin/bash "$nojq_probe/guard.sh" 2>&1)"
  is "no jq means allow, never a machine-wide deny" "$nojq_out" ""
  rm -rf "$nojq_probe"

  group "guard: lease"
  : >"$STORE/$MINE/last_used"
  local before after
  before="$(python3 -c "import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))" "$STORE/$MINE/last_used")"
  sleep 1
  hook "adb -s $MINE logcat -d" >/dev/null
  after="$(python3 -c "import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))" "$STORE/$MINE/last_used")"
  [[ "$after" -gt "$before" ]] && ok "allowed command refreshes the lease" \
    || bad "allowed command refreshes the lease" ">$before" "$after"

  teardown_store
}

# =============================================================================
# derive_state is the fix for the bug where a lock claimed via the reserve-a-port
# path reported STATE=booting forever -- 21 hours, on a device adb had been
# reporting healthy the whole time. These cases pin the rule: live observation
# beats anything stored at claim time.
test_state() {
  group "device-lab: derived state ignores the stale stored STATE"
  python3 - "$LAB" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("lab", sys.argv[1])
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
TTL, GRACE, STALL = lab.IDLE_TTL, lab.ABSENT_GRACE, lab.BOOT_STALL
cases = [
    # (adb_state, idle, age, mine)              -> expected
    (("device",  60,        3600,  False), "held",    "healthy device reads held, not booting"),
    (("device",  60,        3600,  True),  "mine",    "my healthy device reads as mine"),
    (("device",  TTL + 60,  3600,  False), "expired", "idle past the TTL reads expired"),
    (("device",  TTL + 60,  3600,  True),  "expired", "expired wins over mine"),
    (("absent",  10,        10,    False), "booting", "just-claimed and not yet attached is booting"),
    (("absent",  10,        STALL + 60, False), "stalled", "never attached past the stall window"),
    (("absent",  GRACE + 60, STALL + 60, False), "ghost", "gone and idle past the grace is a ghost"),
    (("offline", 10,        3600,  False), "stalled", "attached but unhealthy is stalled"),
    (("unauthorized", 10,   3600,  False), "stalled", "unauthorized is stalled"),
]
fails = 0
for args, expected, name in cases:
    got = lab.derive_state(*args)
    if got == expected:
        print(f"  \033[32mok\033[0m   {name}")
    else:
        fails += 1
        print(f"  \033[31mFAIL\033[0m {name}\n         expected: {expected}\n         actual:   {got}")
# The exact shape of the real 21-hour bug.
got = lab.derive_state("device", 249 * 60, 21 * 3600, False)
if got == "expired":
    print("  \033[32mok\033[0m   the real 21h 'booting' lock derives as expired")
else:
    fails += 1
    print(f"  \033[31mFAIL\033[0m the real 21h 'booting' lock derives as expired\n         actual: {got}")
sys.exit(1 if fails else 0)
PY
  if [[ $? -eq 0 ]]; then PASS=$((PASS + 10)); else FAIL=$((FAIL + 1)); FAILED_NAMES+=("derive_state"); fi
}

# =============================================================================
test_lock() {
  setup_store
  group "emuriad"
  local out
  out="$(EMULATOR_LOCK_DIR="$STORE" CLAUDE_CODE_SESSION_ID="$SESSION" "$LOCK" status 2>&1)"
  has "status lists a claimed serial" "$out" "$MINE"
  has "status lists the other owner"  "$out" "$THEIRS"
  out="$(EMULATOR_LOCK_DIR="$STORE" "$LOCK" --help 2>&1)"
  has "--help works"                  "$out" "claim"
  out="$(EMULATOR_LOCK_DIR="$STORE" "$LOCK" bogus-subcommand 2>&1)"
  has "unknown subcommand is rejected" "$out" "unknown command"

  group "emuriad doctor (read-only diagnostics)"
  local probe; probe="$(mktemp -d)"
  out="$(cd "$probe" && HOME="$probe" EMULATOR_LOCK_DIR="$STORE" CLAUDE_CODE_SESSION_ID="$SESSION" \
         "$LOCK" doctor 2>&1)"
  has "doctor finds the guard"          "$out" "guard hook present"
  has "doctor checks for jq"            "$out" "jq"
  has "doctor reports session identity" "$out" "claude-code:$SESSION"
  has "doctor flags an unwired hook"    "$out" "NOT wired"
  case "$out" in
    */../*) bad "doctor prints a pasteable path" "no ../ in the path" "$out" ;;
    *) ok "doctor prints a pasteable path" ;;
  esac

  # doctor must never mutate anything -- an agent is expected to run it freely.
  local before after
  before="$(ls -R "$STORE" 2>/dev/null; cat "$STORE"/*/meta 2>/dev/null)"
  (cd "$probe" && HOME="$probe" EMULATOR_LOCK_DIR="$STORE" "$LOCK" doctor >/dev/null 2>&1) || true
  after="$(ls -R "$STORE" 2>/dev/null; cat "$STORE"/*/meta 2>/dev/null)"
  is "doctor changes nothing" "$after" "$before"

  # ...and must detect a wired hook rather than always warning.
  mkdir -p "$probe/.claude"
  printf '{"hooks":{"PreToolUse":[{"hooks":[{"command":"x/emuriad-guard.sh"}]}]}}\n' \
    >"$probe/.claude/settings.json"
  out="$(cd "$probe" && HOME="$probe" EMULATOR_LOCK_DIR="$STORE" CLAUDE_CODE_SESSION_ID="$SESSION" \
         "$LOCK" doctor 2>&1)"
  has "doctor detects a wired hook" "$out" "wired into"
  rm -rf "$probe"
  teardown_store
}

# =============================================================================
test_py() {
  group "python suites (tests/test_*.py)"
  local suite out
  for suite in "$HERE"/test_*.py; do
    if out="$(python3 "$suite" 2>&1)"; then
      ok "$(basename "$suite") ($(printf '%s\n' "$out" | sed -n 's/^Ran \([0-9]*\) tests.*/\1/p') tests)"
    else
      bad "$(basename "$suite")" "all tests pass" "$(printf '%s\n' "$out" | grep -E '^(FAIL|ERROR):' | head -5 | tr '\n' ' ')"
    fi
  done
}

# =============================================================================
main() {
  local want="${1:-all}"
  printf '\033[1memulator-lock test suite\033[0m  (%s)\n' "$ROOT"
  case "$want" in
    all)    test_shells; test_guard; test_state; test_lock; test_py ;;
    shells) test_shells ;;
    guard)  setup_store; test_guard ;;
    state)  test_state ;;
    lock)   test_lock ;;
    py)     test_py ;;
    *) echo "unknown group: $want (all|shells|guard|state|lock|py)"; exit 2 ;;
  esac
  printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
  if (( FAIL )); then
    printf 'failed: %s\n' "${FAILED_NAMES[*]}"
    exit 1
  fi
}

main "$@"
