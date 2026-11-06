# Keeping Virtual Worker awake (always-on agents)

Virtual Worker runs always-on background work — the scheduler, the trigger engine, and live
channels like chat channel. If the Mac sleeps, those WebSockets drop and scheduled
runs stall. This is how to keep the machine (and its network) awake for each way
of running Virtual Worker.

## Desktop app — automatic

As of **v0.2.2**, the desktop app keeps the Mac awake on its own via a built-in
Electron power blocker (`prevent-app-suspension`), held for the app's lifetime.
**Just launch Virtual Worker and leave it running** — there's nothing to configure.

- Keeps the **system + network** awake → chat channel, scheduler, and triggers keep
  running while the Mac is idle or locked.
- The **display is still allowed to sleep** (saves power, avoids burn-in) — the
  network stays up regardless.
- Releases automatically when you quit Virtual Worker.
- **Opt out** (rarely needed): launch with `VW_KEEP_AWAKE=0`.

## Stable service (`vw`) — awake by default

The service launcher keeps the Mac awake by default while the detached stable
instance is running:

```bash
./vw start
```

Override with `KEEP_AWAKE=0 ./vw start` when sleep prevention is not wanted.

## Web app / terminal (`dev.sh`) — one flag

When you run from a terminal instead of the packaged app, add the flag:

```bash
KEEP_AWAKE=1 ./dev.sh
```

This re-execs everything under `caffeinate -dimsu` (blocks idle/display/disk/
system sleep) for as long as it runs. Ctrl-C restores normal power behavior.
macOS-only; on other OSes the flag is ignored with a warning.

## Two things that override BOTH of the above

These are macOS-level and neither the app's power blocker nor `caffeinate` can
beat them:

1. **Keep it plugged into AC power.** On battery, system-sleep prevention is
   weaker and the battery drains fast. For always-on, keep the charger in.
2. **Do NOT close the lid.** Closing a MacBook's lid forces sleep regardless —
   unless it's connected to *both* external power *and* an external display
   (clamshell mode). If you're leaving it on a desk, **leave the lid open**.

## Optional: persistent Mac setting

To make the Mac never idle-sleep while plugged in, independent of any app:

```bash
sudo pmset -c disablesleep 1     # undo later with: sudo pmset -c disablesleep 0
```

## TL;DR

Run `./vw start` for the stable service, or the **v0.2.2+ desktop app**;
keep it **plugged in**, and **leave the lid open**. For the foreground dev
profile, `KEEP_AWAKE=1 ./dev.sh` opts into the same protection.
