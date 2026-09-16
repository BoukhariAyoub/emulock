---
name: emulock
description: Reserve an Android emulator before using it, on a machine where several agents run at once. Use whenever a task needs a device - installing an APK, launching the app, running Maestro or instrumented tests, reading logcat, taking a screenshot, driving the UI - and whenever a device command is refused, a device dies mid-task, or you need to know who holds what. Triggers on adb, emulator, AVD, "claim a device", "the emulator", "device is busy", "blocked", "not claimed by anyone".
---

# emulock

This machine's emulators are shared by several agent sessions at once. Every device
is reserved before use, and the reservation is **enforced** — a command aimed at a
device you do not own is refused by a hook before it runs.

Work with it, not around it. There is no flag that disables it, and retrying a
refused command in a different shape will not help.

## The protocol

**1. Claim before you touch anything.**

```bash
emulock claim
```

It prints the serial you now own. `--avd <name>` reserves a specific AVD;
`--note "<what you are doing>"` records your intent so a human watching the
dashboard can see why the device is busy. Pass the note — it costs nothing and it
is the only human-readable signal of what a held device is for.

**2. Target that serial explicitly, every time.**

```bash
adb -s emulator-5556 shell am start -n com.example/.MainActivity
```

Never bare `adb shell`, `adb install`, or `adb logcat`. With several devices
attached, an untargeted command silently picks one — possibly someone else's. It is
refused for that reason.

**3. Release when the task is done.**

```bash
emulock release emulator-5556
```

Leave the emulator running. The pool stays warm and the next session boots nothing.

## When something goes wrong

**A command was refused.** Read the message — it names the owner and the branch.
Either you never claimed, or you are using a serial that belongs to another session.
Run `emulock status` to see the truth, then claim your own device.

**The device died mid-task.**

```bash
emulock reclaim
```

Never re-run a remembered `emulator -port 5554` command. The serial is assigned at
boot; that port may now be a different AVD entirely. `reclaim` re-reserves the AVD
you were actually using.

**Nothing is free.** Report that to the user. Do not release, reap, or kill a device
another session holds in order to take it.

**Something looks misconfigured.**

```bash
emulock doctor
```

It reports whether enforcement is actually wired up and prints the exact fix. It
changes nothing.

**Never install or edit the hook yourself.** If `doctor` says the guard is not wired
in, tell the user and show them the block it printed — do not add it, and do not
edit `settings.json` or anything under `hooks/`. That file is what constrains which
commands you may run; a hook you can install is a hook you can remove, which would
make the whole mechanism pointless. Your harness will refuse the edit anyway.

## Rules that have no exceptions

- **Never `adb kill-server`.** It drops every session's connections at once, not
  just yours. It is always refused.
- **Never** `emu kill`, `pm clear`, `install`, `uninstall`, or reboot a serial you
  do not own.
- **Never** edit the lock store by hand. `emulock reap` is the only sanctioned
  cleanup, and it removes only provably dead locks.
- **Never** work around a refusal by wrapping the command in `bash -c`, a script, or
  a heredoc. The guard inspects the top-level command only; evading it is not a
  clever fix, it is how two agents end up installing onto the same device.

## Checking state

```bash
emulock status     # who owns what, in the terminal
emulock-lab        # live dashboard on 127.0.0.1:7337, read-only
```

The dashboard shows each device's state, the branch it was claimed on, and what its
owner is currently doing — `Running flow · checkout.yaml`, `Installing`,
`Driving UI` — so you can tell a busy device from an abandoned one.

A lease expires after 4 hours idle. Your own commands refresh it automatically, so a
device stays yours while you are actually using it. If you hold one through a long
silent stretch, `emulock heartbeat <serial>` keeps it.

## Identity is the AVD, not the port

`emulator-5554` is the serial that bound port 5554 *this boot*. Next boot the same
port may host a different AVD. Copy the serial from the most recent `claim`,
`reclaim`, or `status` output. Never reuse one from earlier in the conversation
without checking it is still yours.
