# Twitch TTS Engine

Reads Twitch chat aloud. Anonymous IRC connection, no API keys, runs in the
tray, plays through your speakers — made for stream overlays and local
monitoring.

![License: MIT](https://img.shields.io/badge/license-MIT-blue)
![Version](https://img.shields.io/badge/version-0.2.1-green)
![Platform](https://img.shields.io/badge/platform-Windows-important)

## Features

- **Anonymous Twitch IRC** — no Twitch developer account or tokens needed
- **Two TTS engines** — cloud neural voices via [edge-tts](https://github.com/rany2/edge-tts)
  (default, needs internet) or fully offline Windows SAPI voices; switch live
  in the app
- **Windows app** — control panel with mute/skip, voice + engine selector,
  volume, now-playing, live log, and an Options page for configuration
- **Minimizable** to tray!
- **Viewer voice command** — `!voice <name>` lets viewers pick their own
  voice, remembered per user across restarts
- **Host audio playback** — bounded queue, skip, live volume/mute
- **Content sanitization** — strips Twitch/BTTV/FFZ/7TV emotes, emojis, and
  URLs before synthesis
- **Config-driven rules** — command prefix, special users, per-user cooldown,
  message length limit
- **Local-only API** — all endpoints reject non-loopback origins

## Quick start

### Standalone exe (recommended)

Download `twitchTTS.exe` from the [latest release](https://github.com/karlsune/twitchTTS/releases)
and run it. First launch creates `config.json` in `%APPDATA%\TwitchTTS`;
open the window and use **Options...** to set your channel.

### From source

```bash
pip install -r requirements.txt
python twitchtts_app.py
```

Or double-click `start_tts.bat` (prefers the project venv). At minimum set
your channel in `config.json` (auto-created from `config.example.json`):

```json
{ "twitch_channel": "your_channel_name" }
```

> **One instance only.** Launching a second copy focuses the already-running
> window instead of starting a second engine. Closing the window minimizes to
> tray by default — use **Exit** from the tray menu to quit.

## The app window

| Control | Function |
|---|---|
| **Mute / Skip** | Mute host audio / skip the current message |
| **Voice** | Select the active voice (neural or system) |
| **Engine** | Switch between Neural (edge-tts, cloud) and System (SAPI, offline) |
| **Volume** | Vertical slider; adjusts host playback volume |
| **Now Playing** | Current message + queue depth |
| **Engine Log** | Real-time status |
| **About** | Version, license, third-party software |
| **Options...** | Edit config from the GUI ("Save & Restart" applies immediately) |

## Tray Icon

 - **Left click** maximizes program
 - **Right click** small curtain menu
 - Tray icon changes color to represent different states of operation.

| Icon | Meaning |
|-----|----------|
| 🟢 | Engine is OK (normal operation) |
| 🔴 | Engine error |
| ⚪ | Engine is offline |
| 🔇 |	Muted active |

## Configuration

`config.json` lives next to the sources when running from a checkout, or in
`%APPDATA%\TwitchTTS\config.json` for the exe. Edit it via **Options...**
instead of by hand.

| Key | Default | Description |
|---|---|---|
| `twitch_channel` | `your_channel_name` | Target Twitch channel |
| `http_port` / `stream_port` | `8080` / `8081` | API / SSE stream ports |
| `tts_voice` | `en-US-JennyNeural` | Default voice |
| `tts_mode` | `edge` | `edge` (cloud) or `system` (offline SAPI) |
| `command_prefix` | `!tts` | Prefix for speech-eligible messages |
| `special_users` | `[]` | Users who speak without prefix and bypass cooldown |
| `cooldown_seconds` | `3` | Per-user cooldown between messages |
| `max_chars` | `200` | Max accepted message length |
| `close_to_tray` | `true` | Close button minimizes to tray |
| `audio.*` | `enabled: true, volume: 0.8, muted: false, queue_size: 50` | Host playback |

## Chat commands

| Command | Who | Effect |
|---|---|---|
| `!tts <text>` | everyone | Speaks `<text>` (prefix configurable) |
| `!voice <name>` | everyone | Sets your personal voice |
| `!voice` | everyone | Reports your current voice |
| `!voice reset` | everyone | Clears your personal voice |

Voice names match loosely (`en-us-aria-neural` == `en-US-AriaNeural`).
Per-user voices are stored in `user_voices.json` next to `config.json` and
apply to host-audio playback only.

## Building the exe

Run `build.bat` → produces `dist/twitchTTS.exe`. The same binary hosts the
engine via a hidden `--engine` flag.

## Linux / headless server

The app runs on Linux: the full tray app on desktop (needs an X display,
python3-tk and a tray backend), or a headless engine service on servers.
Windows-only bits: SAPI system voices (the engine falls back to edge and
the mode selector hides "System (offline)") and host-audio playback on
headless machines (audio plays in the browser UI instead).

```bash
# installs everything into a venv; on a headless box it also sets up a
# systemd user service (Ubuntu/Debian-ish):
./install_linux.sh

# desktop: run the tray app
~/.local/share/twitchtts/venv/bin/python twitchtts_app.py

# headless logs:
journalctl --user -u twitchtts -f
```

Config is `config.json` next to the sources (auto-created on first run).
The engine binds to `127.0.0.1` — from another machine, use an SSH tunnel
and open `http://localhost:8080/index.html`:

```bash
ssh -L 8080:localhost:8080 -L 8081:localhost:8081 user@server
```

## Architecture

- `app.py` — asyncio service: anonymous Twitch IRC, HTTP API (`http_port`),
  SSE event stream (`stream_port`)
- `audio.py` — `AudioPlayer` queue thread (pygame-ce) with skip/clear, live
  volume/mute, and 5 ms fade in/out
- `emotes.py` / `sanitize.py` — BTTV/FFZ/7TV emote loading and text cleaning
- `config.py` — config path resolution (repo dir for source, `%APPDATA%` for exe)
- `twitchtts_app.py` — tray shell / control window; single-instance guard

SSE events: `log`, `voices`, `voice`, `chat`, `now_playing`, `control`.
API routes (loopback-only): `/api/voices`, `/api/tts`, `/api/config` (GET/POST),
`/api/status`, `/api/control`, `/api/voice`.

## License

MIT — see [LICENSE.md](LICENSE.md). Bundled dependencies and their licenses
are listed in [THIRD-PARTY-SOFTWARE.md](THIRD-PARTY-SOFTWARE.md).
