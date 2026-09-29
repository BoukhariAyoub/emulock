"""What every emulock Python tool shares: identity, the lock store, project config.

Kept in step with bin/emulock by hand. The rules are small enough that a second
copy is cheaper than a bash <-> python bridge, and tests/run.sh pins both.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

LOCK_ROOT = Path(os.environ.get("EMULATOR_LOCK_DIR", Path.home() / ".emulator-locks"))
AVD_HOME = Path(os.environ.get("ANDROID_AVD_HOME", Path.home() / ".android" / "avd"))


def me() -> str:
    """This session's identity, resolved exactly like owner_id() in bin/emulock."""
    if os.environ.get("EMULATOR_LOCK_OWNER"):
        return os.environ["EMULATOR_LOCK_OWNER"]
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return f"claude-code:{os.environ['CLAUDE_CODE_SESSION_ID']}"
    return f"manual:{os.environ.get('USER', 'unknown')}"


def read_meta(serial: str) -> dict[str, str] | None:
    meta = LOCK_ROOT / serial / "meta"
    if not meta.is_file():
        return None
    pairs = (line.split("=", 1) for line in meta.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def claim_problem(serial: str) -> str | None:
    """Why this session may not drive `serial`, or None if it may.

    A tool that runs adb itself is invisible to the guard hook (it inspects only
    the top-level command), so every such tool checks the claim on its own.
    """
    if not serial.startswith("emulator-"):
        return None  # physical devices are not in the lock store
    meta = read_meta(serial)
    if meta is None:
        return f"{serial} is not claimed — emulock claim --pool (or emulock claim)"
    owner = meta.get("OWNER_ID", "")
    if owner and owner != me():
        return f"{serial} belongs to another agent (branch {meta.get('OWNER_BRANCH', '?')}) — never touch a device you did not claim"
    return None


def project_root(start: Path | None = None) -> Path:
    cwd = str(start or Path.cwd())
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path(cwd)


class Config:
    """<repo>/.emulock/config: `key = value` lines, first match wins. `#` starts a
    comment at the start of a line or after whitespace (so `a#b` stays a value).

    EMULOCK_<KEY> in the environment overrides a key (dots and dashes become
    underscores, letters upper-case: pool.avd -> EMULOCK_POOL_AVD).
    """

    def __init__(self, root: Path, values: dict[str, str] | None = None):
        self.root = root
        self.values: dict[str, str] = {}
        if values is not None:
            self.values = dict(values)
            return
        path = root / ".emulock" / "config"
        if path.is_file():
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                # "key = value   # note": a # after whitespace starts a comment.
                key, value = key.strip(), re.sub(r"\s+#.*$", "", value).strip()
                self.values.setdefault(key, value)

    @staticmethod
    def env_name(key: str) -> str:
        return "EMULOCK_" + key.upper().replace(".", "_").replace("-", "_")

    def get(self, key: str, default: str = "") -> str:
        return os.environ.get(self.env_name(key)) or self.values.get(key) or default

    def variants(self) -> list[str]:
        """Variant names declared as package.<variant> keys."""
        return sorted(k.split(".", 1)[1] for k in self.values if k.startswith("package."))

    def for_variant(self, key: str, variant: str | None, default: str = "") -> str:
        if variant:
            specific = self.get(f"{key}.{variant}")
            if specific:
                return specific
        return self.get(key, default)
