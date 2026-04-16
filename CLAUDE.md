# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Spin Doctor

A macOS menu bar app that monitors CPU usage and alerts the user when a process sustains high CPU, offering a one-click kill. Built with Python, `rumps` (menu bar framework), and `psutil` (process monitoring). User config lives at `~/.config/spin_doctor/config.toml`.

## Development Commands

```bash
# Run the app directly (requires macOS)
uv run spin-doctor

# Build macOS .app bundle with py2app
uv run python setup.py py2app

# The .app bundle lands in dist/Spin Doctor.app
```

Uses `uv` for dependency management. Python ≥3.12 required.

## Architecture

The entire app is in `src/spin_doctor/app.py` — a single `SpinDoctorApp(rumps.App)` class:

- **Polling loop**: `rumps.Timer` fires `poll()` every N seconds, scanning `psutil.process_iter()` for processes above the CPU threshold
- **Tracking**: `self.tracked` dict maps PID → `TrackedProcess` dataclass. Processes are tracked once they exceed the threshold and removed when they drop below it for 2 consecutive intervals
- **Notifications**: After a process sustains high CPU for `duration_seconds`, a macOS notification with a "Kill" action button is sent (with a per-process-name cooldown)
- **Kill flow**: `kill_process()` verifies the PID still matches the expected process name (guards against PID recycling), sends SIGTERM, waits 3s, then SIGKILL if needed
- **Menu**: Dynamic menu items show currently busy processes with kill callbacks; updated each poll cycle by directly manipulating the NSMenu via `rumps` internals
- **Launch on Login**: Uses `SMAppService` from the macOS ServiceManagement framework via `objc.loadBundle` into a local dict (to satisfy Ruff). Enabled by default on first launch; state is persisted in `~/.config/spin_doctor/state.json`. Only active when running as a `.app` bundle
- **Logging**: Rotating file log at `~/Library/Logs/Spin Doctor/spin-doctor.log` (1 MB × 3 backups) via `setup_logging()`. Records startup, config load/reload, tracking start, alerts, and kill outcomes. The intent is to accumulate real-world data on which processes frequently spike so we can refine the default `ignored_processes` skip list later — treat the log as the source of truth for that tuning work

## Build & Release

- `setup.py` is only for py2app bundling (not for pip install). It patches py2app's `finalize_options` to skip `install_requires` validation
- `launch.py` is the py2app entry point — just imports and calls `main()`
- Release workflow (`.github/workflows/release.yml`) triggers on `v*` tags: builds with py2app, codesigns inside-out (frameworks/dylibs → executables → app bundle), notarizes with Apple, creates a DMG, and publishes a GitHub Release
- `entitlements.plist` grants hardened runtime entitlements for the main executable
