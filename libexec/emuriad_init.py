#!/usr/bin/env python3
"""emuriad init: install the agent skill and wire the guard hook, once per machine.

  emuriad init               ~/.claude: every project on this machine is covered
  emuriad init --project     this repo's .claude/settings.json (commit it: every contributor is covered)
  emuriad init --dry-run     show what would change, change nothing

It shows the exact change and asks before editing a settings file. A PreToolUse
hook decides which commands an agent may run, so this is a step for a person at
a terminal, not for an agent: without a terminal it prints the change and stops,
unless you pass --yes.

The skill teaches agents the protocol (claim, target your serial, release), so
they claim correctly the first time instead of learning it by being refused.
User-level it is a link into the installed copy, so upgrades reach it; with
--project it is a copy you commit.

emuriad was called emulock before 0.3. An install from then (the skill at
skills/emulock, a hook running `emulock guard` or emulock-guard.sh) keeps working
through the emulock shim; init offers to move it to the new names, and asks first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOOK_TIMEOUT = 10
# A hook command that runs the pre-0.3 guard: `.../emulock guard` or `.../emulock-guard.sh`.
LEGACY_GUARD = re.compile(r"(^|[\s/])emulock(-guard\.sh\b|\s+guard\b)")


def project_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else Path.cwd()


def guard_command(project: bool) -> str:
    if project:
        # Committed for everyone: resolve on each contributor's PATH, not this machine's layout.
        return "emuriad guard"
    on_path = shutil.which("emuriad")
    return f"{on_path or os.environ.get('EMURIAD_BIN', 'emuriad')} guard"


def pre_tool_hooks(settings: dict):
    for entry in settings.get("hooks", {}).get("PreToolUse", []) or []:
        for hook in entry.get("hooks", []) or []:
            yield hook


def mentions_emuriad(settings: dict) -> bool:
    return any("emuriad" in str(hook.get("command", "")) for hook in pre_tool_hooks(settings))


def legacy_hooks(settings: dict) -> list[dict]:
    return [hook for hook in pre_tool_hooks(settings) if LEGACY_GUARD.search(str(hook.get("command", "")))]


def plan_settings(path: Path, command: str) -> tuple[dict | None, str]:
    """(new settings, message); new settings is None when nothing needs to change."""
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as error:
            raise SystemExit(f"emuriad init: {path} is not valid JSON ({error}) — fix it first; nothing was changed")
    if mentions_emuriad(settings):
        return None, f"guard already wired in {path}"
    updated = json.loads(json.dumps(settings))
    old = legacy_hooks(updated)
    if old:
        lines = [f"this moves the pre-0.3 emulock guard in {path} to emuriad:"]
        for hook in old:
            lines.append(f"  - {hook['command']}\n  + {command}")
            hook["command"] = command
        return updated, "\n".join(lines)
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]}
    updated.setdefault("hooks", {}).setdefault("PreToolUse", []).append(entry)
    return updated, f"this adds the guard to {path}:\n" + json.dumps({"hooks": {"PreToolUse": [entry]}}, indent=2)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def legacy_skill(skills: Path) -> Path | None:
    """skills/emulock from before the rename: emulock's link, or a copy of its SKILL.md."""
    old = skills / "emulock"
    if old.is_symlink():
        return old
    skill_md = old / "SKILL.md"
    if skill_md.is_file() and re.search(r"^name:\s*emulock\s*$", skill_md.read_text(), re.MULTILINE):
        return old
    return None


def migrate_skill(skills: Path, dry_run: bool, assume_yes: bool) -> str | None:
    """Offer to remove the pre-0.3 skill; None when there is none."""
    old = legacy_skill(skills)
    if old is None:
        return None
    what = f"the pre-0.3 emulock skill at {old}" + (f" (a link to {os.readlink(old)})" if old.is_symlink() else "")
    print(f"skill: found {what}; emuriad's skill replaces it at {skills / 'emuriad'}")
    if dry_run:
        return "skill: dry run — would remove it"
    if not confirm(f"Remove {old}?", assume_yes):
        return f"skill: kept {old} — agents will see both skills until you remove it"
    if old.is_symlink():
        old.unlink()
    else:
        shutil.rmtree(old)
    return f"skill: removed {old}"


def install_skill(source: Path | None, target: Path, copy: bool, dry_run: bool) -> str:
    if source is None or not source.is_dir():
        return "skill: not found in this install — skipped"
    if target.is_symlink() and not copy and Path(os.readlink(target)) != source:
        # An older link (e.g. into a versioned Cellar dir): point it at the stable path.
        if not dry_run:
            target.unlink()
            target.symlink_to(source)
        return f"skill: {'would relink' if dry_run else 'relinked'} {target} -> {source}"
    if target.is_symlink() and target.resolve() == source.resolve():
        return f"skill: already linked at {target}"
    if target.exists() or target.is_symlink():
        if copy and (target / "SKILL.md").is_file():
            if not dry_run:
                shutil.copy2(source / "SKILL.md", target / "SKILL.md")
            return f"skill: {'would update' if dry_run else 'updated'} {target}/SKILL.md"
        return f"skill: {target} already exists and is not emuriad's link — left alone"
    if dry_run:
        return f"skill: would {'copy' if copy else 'link'} {source} -> {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(source, target)
    else:
        target.symlink_to(source)
    return f"skill: {'copied' if copy else 'linked'} {target}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="emuriad init", description=__doc__.split("\n\n")[0],
                                     epilog=__doc__.split("\n\n", 1)[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", action="store_true", help="wire this repo instead of ~/.claude")
    parser.add_argument("--dry-run", action="store_true", help="show the change, make none")
    parser.add_argument("--yes", action="store_true", help="do not ask (you have read the change)")
    parser.add_argument("--no-skill", action="store_true", help="only wire the hook")
    parser.add_argument("--no-hook", action="store_true", help="only install the skill")
    args = parser.parse_args(argv)

    home = Path(os.environ.get("EMURIAD_CLAUDE_HOME", Path.home() / ".claude"))
    base = project_root() / ".claude" if args.project else home
    skill_source = Path(os.environ["EMURIAD_SKILL_DIR"]) if os.environ.get("EMURIAD_SKILL_DIR") else None

    if not args.no_skill:
        migrated = migrate_skill(base / "skills", args.dry_run, args.yes)
        if migrated:
            print(migrated)
        print(install_skill(skill_source, base / "skills" / "emuriad", copy=args.project, dry_run=args.dry_run))

    status = 0
    if not args.no_hook:
        settings_path = base / "settings.json"
        updated, message = plan_settings(settings_path, guard_command(args.project))
        if updated is None:
            print(f"hook: {message}")
        else:
            print(f"hook: {message}")
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
                print("hook: not written — run `emuriad init` yourself in a terminal, or add the block above by hand")
                status = 2

    print("next: emuriad doctor   (checks that enforcement is live)")
    return status


if __name__ == "__main__":
    sys.exit(main())
