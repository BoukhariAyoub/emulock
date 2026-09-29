#!/usr/bin/env bash
#
# install.sh — symlink emuriad onto your PATH (the no-Homebrew install).
#
# Deliberately boring: it creates four symlinks (emuriad, emuriad-lab and their
# pre-0.3 emulock shims) and one directory, prints every action before taking
# it, and never edits a settings file. Wiring the
# PreToolUse hook is `emuriad init`, which shows the change and asks first.
#
#   ./install.sh                 install to ~/.local/bin
#   ./install.sh --prefix ~/bin  install elsewhere
#   ./install.sh --dry-run       print what would happen, change nothing
#   ./install.sh --uninstall     remove the symlinks and the shared dir
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$HOME/.local/bin"
SHARE="$HOME/.local/share/emuriad"
DRY=0
MODE=install

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    -h|--help) awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

run() { if (( DRY )); then echo "  would: $*"; else echo "  $*"; "$@"; fi; }

if [[ "$MODE" == uninstall ]]; then
  echo "Removing emuriad:"
  run rm -f "$PREFIX/emuriad" "$PREFIX/emuriad-lab" "$PREFIX/emulock" "$PREFIX/emulock-lab"
  run rm -rf "$SHARE"
  echo
  echo "Remove the PreToolUse hook from your settings.json by hand — this script"
  echo "never edited it, so it will not guess which block was yours."
  exit 0
fi

for dep in python3 adb; do
  command -v "$dep" >/dev/null 2>&1 || echo "  warning: '$dep' not on PATH — emuriad needs it at runtime" >&2
done

echo "Installing emuriad:"
run mkdir -p "$PREFIX" "$SHARE"
run ln -sf "$HERE/bin/emuriad" "$PREFIX/emuriad"
run ln -sf "$HERE/bin/emuriad-lab" "$PREFIX/emuriad-lab"
# The pre-0.3 names, as shims: hooks and scripts that still call emulock keep working.
run ln -sf "$HERE/bin/emulock" "$PREFIX/emulock"
run ln -sf "$HERE/bin/emulock-lab" "$PREFIX/emulock-lab"
run ln -sfn "$HERE/hooks" "$SHARE/hooks"
run ln -sfn "$HERE/skills" "$SHARE/skills"

echo
if [[ -e "$HOME/.local/share/emulock" ]]; then
  echo "  note: $HOME/.local/share/emulock is from before the rename (emulock -> emuriad)."
  echo "      Once 'emuriad init' has moved your hook, remove it: rm -rf ~/.local/share/emulock"; echo
fi
case ":$PATH:" in
  *":$PREFIX:"*) ;;
  *) echo "  note: $PREFIX is not on your PATH. Add it:"
     echo "      export PATH=\"$PREFIX:\$PATH\""; echo ;;
esac

cat <<EOF
Now wire the guard and install the agent skill — it shows each change and asks:

    emuriad init              # ~/.claude: every project on this machine
    emuriad init --project    # or this repo's .claude/ (commit it for your team)

Then check it:  emuriad doctor
Run the tests:  $HERE/tests/run.sh
EOF
