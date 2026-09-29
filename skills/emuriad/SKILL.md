---
name: emuriad
description: Reserve an Android emulator before using it, on a machine where several agents run at once. Use whenever a task needs a device - installing an APK, launching the app, running Maestro or instrumented tests, reading logcat, taking a screenshot, driving the UI, checking a device is ready, recording proof of an on-device test - and whenever a device command is refused, a device dies mid-task, or you need to know who holds what. Triggers on adb, emulator, AVD, "check in", "check out", "claim a device", "the emulator", "device pool", "device is busy", "blocked", "not claimed by anyone", "doctor", "evidence".
---

# emuriad

This machine's emulators are shared by several agent sessions at once. Every device
is reserved before use, and the reservation is **enforced** — a command aimed at a
device you do not own is refused by a hook before it runs.

Work with it, not around it. There is no flag that disables it, and retrying a
refused command in a different shape will not help.

## The protocol

**1. Check in before you touch anything.** Prefer the pool:

```bash
emuriad check-in --pool --note "<what you are doing>"
```

A pool instance is a disposable copy of a prepared device, booted from a `golden`
snapshot: settings already fixed, nothing another agent did survives in it. The
claim prints a boot command — run it **verbatim** as a long-lived background
process, then `adb -s <serial> wait-for-device`. Never drop `-no-window` or add a
`-gpu` flag: the snapshot only loads headless, and a boot that cannot load it makes
the emulator delete it for everyone (the guard refuses that boot).

When the user wants to **watch or touch** the device, claim `emuriad check-in --pool --window`
instead: the same disposable device, booted in a window from its own `golden-window`
snapshot (if it says the snapshot is missing, tell the user: baking it is theirs to run,
`emuriad pool bake --window`). Its boot command has no `-no-window`; run it verbatim too.
`emuriad check-in` (no `--pool`) reserves an ordinary AVD instead, and `--avd <name>` a
specific one.
`--additional` gets a second device for a two-device test. The `--note` records your
intent so a human can see why the device is busy; pass it.

**Then check the device** before spending time on it:

```bash
emuriad doctor <serial>            # --fix applies the safe settings fixes
```

It checks the claim, the boot, the installed build (is it *this* checkout's?),
DNS, locale, permission dialogs or the keyboard covering the screen, and host
load, plus any checks the project adds. Run it again whenever a result looks
impossible. Pass `--worktree <path>` if your shell's working directory is not the
checkout you built.

**2. Target that serial explicitly, every time.**

```bash
adb -s emulator-5556 shell am start -n com.example/.MainActivity
```

Never bare `adb shell`, `adb install`, or `adb logcat`. With several devices
attached, an untargeted command silently picks one — possibly someone else's. It is
refused for that reason.

Gradle's `install*`, `uninstall*` and `connected*` tasks act on **every** connected
device, so they are refused unless you name your serial inline:
`ANDROID_SERIAL=emulator-5556 ./gradlew installDebug` (spelled out, not a `$VAR`).

**3. Record what you verified, if a reviewer will ask.**

```bash
emuriad evidence start <serial> --label "<ticket> <what>"   # after installing your build
emuriad evidence shot  <serial> "<what the screen shows>"
emuriad evidence note  <serial> "<what you did or checked>"
emuriad evidence stop  <serial>      # writes summary.md + manifest.json
```

It records the screen, screenshots, crashes and the doctor's report into the
project's evidence folder. Upload the files in `manifest.json` where reviewers look,
then `emuriad evidence render <folder> --assets <urls.json>` for the summary.

**4. Check out when the task is done.**

```bash
emuriad check-out emulator-5556
```

A pool instance is shut down on check-out, so nothing you changed reaches the next
agent. An ordinary AVD is left running, warm for the next session.

`claim`, `release` and `heartbeat` are the older names of `check-in`, `check-out` and
`extend-stay`; they still work.

## When something goes wrong

**A command was refused.** Read the message — it names the owner and the branch.
Either you never claimed, or you are using a serial that belongs to another session.
Run `emuriad status` to see the truth, then check in to your own device.

**The device died mid-task.**

```bash
emuriad reclaim
```

Never re-run a remembered `emulator -port 5554` command. The serial is assigned at
boot; that port may now be a different AVD entirely. `reclaim` re-reserves the AVD
you were actually using.

`reclaim` of a pool instance just boots a fresh one: any instance is as good as
another.

**Nothing is free.** Report that to the user. Do not release, reap, or kill a device
another session holds in order to take it.

**Something looks misconfigured.**

```bash
emuriad doctor
```

With no serial it reports whether emuriad itself is installed and enforcement is
actually wired up, and prints the fix. It changes nothing.

**Never install or edit the hook yourself.** If `doctor` says the guard is not wired
in, tell the user to run `emuriad init` themselves — do not run it, and do not edit
`settings.json` or anything under `hooks/`. That file is what constrains which
commands you may run; a hook you can install is a hook you can remove, which would
make the whole mechanism pointless. Your harness will refuse the edit anyway.

## Rules that have no exceptions

- **Never `adb kill-server`.** It drops every session's connections at once, not
  just yours. It is always refused.
- **Never** `emu kill`, `pm clear`, `install`, `uninstall`, or reboot a serial you
  do not own.
- **Never** bake or rebake the pool (`emuriad pool bake|rebake`) unless the user
  asks: it shuts every pool instance out while it runs.
- **Never** edit the lock store by hand. `emuriad reap` is the only sanctioned
  cleanup, and it removes only provably dead locks.
- **Never** work around a refusal by wrapping the command in `bash -c`, a script, or
  a heredoc. The guard inspects the top-level command only; evading it is not a
  clever fix, it is how two agents end up installing onto the same device.

## Checking state

```bash
emuriad status     # who owns what, in the terminal
emuriad-lab        # live dashboard on 127.0.0.1:7337, read-only
```

The dashboard shows each device's state, the branch it was claimed on, and what its
owner is currently doing — `Running flow · checkout.yaml`, `Installing`,
`Driving UI` — so you can tell a busy device from an abandoned one.

A lease expires after 4 hours idle. Your own commands refresh it automatically, so a
device stays yours while you are actually using it. If you hold one through a long
silent stretch, `emuriad extend-stay <serial>` keeps it.

## Identity is the AVD, not the port

`emulator-5554` is the serial that bound port 5554 *this boot*. Next boot the same
port may host a different AVD. Copy the serial from the most recent `claim`,
`reclaim`, or `status` output. Never reuse one from earlier in the conversation
without checking it is still yours.
