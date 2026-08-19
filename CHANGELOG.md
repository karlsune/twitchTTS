# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Host audio played synthesized speech as loud static noise. pygame's
  `Sound()` cannot decode MP3 from a buffer, so host audio is now decoded
  with `miniaudio` and re-wrapped as WAV before playback.

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
