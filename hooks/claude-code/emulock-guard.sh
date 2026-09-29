#!/usr/bin/env bash
#
# emulock-guard.sh — the pre-0.3 name of emuriad-guard.sh, for settings.json files
# that still point here. Silent: it runs before every shell command.
exec /bin/bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/emuriad-guard.sh" "$@"
