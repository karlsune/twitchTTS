# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Tray app voice dropdown was always empty: it parsed the `/api/voices`
  response using the keys `ShortName`/`Name`, but the API returns
  `name`/`label`. The dropdown now lists every voice and preselects the
  engine's current voice from `/api/config`.
- Selecting a voice in the tray app now sends the voice ShortName (e.g.
  `en-US-JennyNeural`) instead of the display label, so the change
  actually applies.
- The frozen `twitchTTS.exe` crashed at startup with
  `ModuleNotFoundError: pystray`: `build.bat` used the system Python
  (which lacks pystray/PIL) and PyInstaller silently skipped the missing
  imports. `build.bat` now prefers `.venv\Scripts\python.exe`, and
  `--engine` mode no longer imports GUI dependencies at all.
- The frozen app now resolves `config.json` and `LICENSE` next to the
  executable (previously it looked inside the onefile temp extraction
  directory).

### Changed

- About dialog now states the MIT license and offers a "View MIT License"
  button (opens the local `LICENSE` file) plus a repository link.
- New **Options...** window in the tray app: edit channel, voice, command
  prefix, cooldown, max length, special users and audio settings from the
  GUI. "Save & Restart" applies channel/prefix/port changes immediately.
- The standalone exe now stores `config.json` in `%APPDATA%\TwitchTTS`
  (per-user app data, Windows convention) instead of next to the binary,
  so no JSON files appear where the exe is placed. Existing configs next
  to the exe are migrated automatically on first run.
- Added `THIRD-PARTY-NOTICES.md` (LGPL notices for edge-tts and pygame-ce
  plus MIT texts) and linked it from the About dialog; project stays MIT
  while remaining compliant with its LGPL dependencies.

### Added

- Standalone tray shell (`twitchtts_app.py`): native window + system tray
  with status color (green ok / red error / grey offline) and an
  open/mute/about/exit menu; minimize-to-tray keeps the engine running.
- Engine `/api/status` endpoint reporting connection and error state for
  the tray icon.
- PyInstaller build support (`build.bat`): the frozen executable hosts the
  engine via a `--engine` flag.

## [0.1.1] - 2026-08-19

### Fixed

- Host audio played synthesized speech as loud static noise. pygame's
  `Sound()` cannot decode MP3 from a buffer, so host audio is now decoded
  with `miniaudio` and re-wrapped as WAV before playback.
- `config.json` written with a UTF-8 BOM (Windows editors) no longer
  crashes the engine at startup.
- `start_tts.bat` now prefers the project virtual environment when present,
  so host audio is enabled on setups where the system Python lacks
  pygame-ce.

## [0.1.0] - 2026-08-19

### Added

- First public release of the Twitch TTS Engine.
- Anonymous Twitch IRC chat listener (no API keys required).
- Neural text-to-speech via [edge-tts](https://github.com/rany2/edge-tts)
  with fallback to browser-based synthesis.
- Host audio playback (pygame-ce) with a bounded server-side queue,
  skip/clear, live volume and mute.
- Browser control panel (OBS-ready) with mute, volume, skip, voice selection,
  "now playing" and engine log.
- Server-sent events (SSE) stream with automatic reconnection.
- Content sanitization: strips Twitch/BTTV/FFZ/7TV emotes, emojis, URLs and
  excess whitespace.
- Config-driven rules: command prefix, special users, per-user cooldown,
  message length limit.
- Local-only HTTP API with loopback origin protection.
- `--no-audio` / `--audio` command-line overrides.
- First-run auto-creation of `config.json` from `config.example.json`.
- MIT license, contributing guidelines, changelog.
