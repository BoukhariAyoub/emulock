#!/usr/bin/env python3
"""emulock init: install the agent skill and wire the guard hook, once per machine.

  emulock init               ~/.claude: every project on this machine is covered
  emulock init --project     this repo's .claude/settings.json (commit it: every contributor is covered)
  emulock init --dry-run     show what would change, change nothing

It shows the exact change and asks before editing a settings file. A PreToolUse
hook decides which commands an agent may run, so this is a step for a person at
a terminal, not for an agent: without a terminal it prints the change and stops,
unless you pass --yes.

The skill teaches agents the protocol (claim, target your serial, release), so
they claim correctly the first time instead of learning it by being refused.
User-level it is a link into the installed copy, so upgrades reach it; with
--project it is a copy you commit.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOK_TIMEOUT = 10


def project_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else Path.cwd()


def guard_command(project: bool) -> str:
    if project:
        # Committed for everyone: resolve on each contributor's PATH, not this machine's layout.
        return "emulock guard"
    on_path = shutil.which("emulock")
    return f"{on_path or os.environ.get('EMULOCK_BIN', 'emulock')} guard"


def mentions_emulock(settings: dict) -> bool:
    for entry in settings.get("hooks", {}).get("PreToolUse", []) or []:
        for hook in entry.get("hooks", []) or []:
            command = str(hook.get("command", ""))
            if "emulock" in command:
                return True
    return False


def plan_settings(path: Path, command: str) -> tuple[dict | None, str]:
    """(new settings, message); new settings is None when nothing needs to change."""
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as error:
            raise SystemExit(f"emulock init: {path} is not valid JSON ({error}) — fix it first; nothing was changed")
    if mentions_emulock(settings):
        return None, f"guard already wired in {path}"
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]}
    updated = json.loads(json.dumps(settings))
    updated.setdefault("hooks", {}).setdefault("PreToolUse", []).append(entry)
    return updated, json.dumps({"hooks": {"PreToolUse": [entry]}}, indent=2)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def install_skill(source: Path | None, target: Path, copy: bool, dry_run: bool) -> str:
    if source is None or not source.is_dir():
        return "skill: not found in this install — skipped"
    if target.is_symlink() and target.resolve() == source.resolve():
        return f"skill: already linked at {target}"
    if target.exists() or target.is_symlink():
        if copy and (target / "SKILL.md").is_file():
            if not dry_run:
                shutil.copy2(source / "SKILL.md", target / "SKILL.md")
            return f"skill: {'would update' if dry_run else 'updated'} {target}/SKILL.md"
        return f"skill: {target} already exists and is not emulock's link — left alone"
    if dry_run:
        return f"skill: would {'copy' if copy else 'link'} {source} -> {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(source, target)
    else:
        target.symlink_to(source)
    return f"skill: {'copied' if copy else 'linked'} {target}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="emulock init", description=__doc__.split("\n\n")[0],
                                     epilog=__doc__.split("\n\n", 1)[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", action="store_true", help="wire this repo instead of ~/.claude")
    parser.add_argument("--dry-run", action="store_true", help="show the change, make none")
    parser.add_argument("--yes", action="store_true", help="do not ask (you have read the change)")
    parser.add_argument("--no-skill", action="store_true", help="only wire the hook")
    parser.add_argument("--no-hook", action="store_true", help="only install the skill")
    args = parser.parse_args(argv)

    home = Path(os.environ.get("EMULOCK_CLAUDE_HOME", Path.home() / ".claude"))
    base = project_root() / ".claude" if args.project else home
    skill_source = Path(os.environ["EMULOCK_SKILL_DIR"]) if os.environ.get("EMULOCK_SKILL_DIR") else None

    if not args.no_skill:
        print(install_skill(skill_source, base / "skills" / "emulock", copy=args.project, dry_run=args.dry_run))

    status = 0
    if not args.no_hook:
        settings_path = base / "settings.json"
        updated, message = plan_settings(settings_path, guard_command(args.project))
        if updated is None:
            print(f"hook: {message}")
        else:
            print(f"hook: this adds the guard to {settings_path}:\n{message}")
            if args.dry_run:
                print("hook: dry run — nothing written")
            elif confirm(f"Write {settings_path}?", args.yes):
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                if settings_path.exists():
                    shutil.copy2(settings_path, settings_path.with_suffix(".json.bak"))
                settings_path.write_text(json.dumps(updated, indent=2) + "\n")
                print(f"hook: written (previous version kept as {settings_path.name}.bak)")
                if args.project:
                    print("hook: commit .claude/settings.json so every contributor gets it")
            else:
                print("hook: not written — run `emulock init` yourself in a terminal, or add the block above by hand")
                status = 2

    print("next: emulock doctor   (checks that enforcement is live)")
    return status


if __name__ == "__main__":
    sys.exit(main())
