#!/usr/bin/env bash
#
# install.sh — symlink emulock onto your PATH and print the hook wiring.
#
# Deliberately boring: it creates two symlinks and one directory, prints every
# action before taking it, and never edits a settings file for you. Wiring a
# PreToolUse hook changes what commands your agent may run, so that edit stays
# yours to make and to read.
#
#   ./install.sh                 install to ~/.local/bin
#   ./install.sh --prefix ~/bin  install elsewhere
#   ./install.sh --dry-run       print what would happen, change nothing
#   ./install.sh --uninstall     remove the symlinks and the shared dir
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$HOME/.local/bin"
SHARE="$HOME/.local/share/emulock"
DRY=0
MODE=install

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

run() { if (( DRY )); then echo "  would: $*"; else echo "  $*"; "$@"; fi; }

if [[ "$MODE" == uninstall ]]; then
  echo "Removing emulock:"
  run rm -f "$PREFIX/emulock" "$PREFIX/emulock-lab"
  run rm -rf "$SHARE"
  echo
  echo "Remove the PreToolUse hook from your settings.json by hand — this script"
  echo "never edited it, so it will not guess which block was yours."
  exit 0
fi

for dep in python3 adb; do
  command -v "$dep" >/dev/null 2>&1 || echo "  warning: '$dep' not on PATH — emulock needs it at runtime" >&2
done

echo "Installing emulock:"
run mkdir -p "$PREFIX" "$SHARE"
run ln -sf "$HERE/bin/emulock" "$PREFIX/emulock"
run ln -sf "$HERE/bin/emulock-lab" "$PREFIX/emulock-lab"
run ln -sfn "$HERE/hooks" "$SHARE/hooks"

echo
case ":$PATH:" in
  *":$PREFIX:"*) ;;
  *) echo "  note: $PREFIX is not on your PATH. Add it:"
     echo "      export PATH=\"$PREFIX:\$PATH\""; echo ;;
esac

cat <<JSON
Now wire the guard into Claude Code. Add this to .claude/settings.json in each
repo where you want unclaimed devices refused — commit it, and every contributor
gets enforcement with no install step of their own:

{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "$SHARE/hooks/claude-code/emulock-guard.sh" }]
      }
    ]
  }
}

Then check it:  emulock status
Run the tests:  $HERE/tests/run.sh
JSON
