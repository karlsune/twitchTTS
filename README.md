# Twitch TTS Engine

A text-to-speech system that monitors Twitch chat and synthesizes messages into audio for stream overlays and local monitoring.

## Overview

Twitch TTS Engine provides real-time voice synthesis for Twitch chat messages. It connects to Twitch IRC anonymously and reads accepted messages aloud on the machine running the app. Two synthesis engines are available: cloud neural voices (edge-tts) and offline system voices (Windows SAPI).

## Features

- **Anonymous IRC Connection** — No Twitch API keys required
- **Two TTS engines** — Cloud neural voices via [edge-tts](https://github.com/rany2/edge-tts) (internet required) or offline system voices via Windows SAPI; switchable live from the app window
- **Standalone desktop app** — Native tray app with a control window: mute, skip, voice + engine selection, volume, now-playing, live log, and an Options window for configuration
- **Host Audio Playback** — Audio plays on the machine running the engine (pygame), with a bounded server-side queue that prevents flooding
- **Content Sanitization** — Automatically removes emotes, emojis, and URLs from chat text before synthesis
- **Third-Party Emote Support** — Detects and strips BTTV, FFZ, and 7TV emotes for the configured Twitch channel
- **Config-Driven Rules** — Command prefix, special users, per-user cooldown, and message length limits are all set in `config.json` (or via the Options window)
- **Viewer voice command** — `!voice <name>` lets viewers pick their own TTS voice, remembered per user
- **Local-Only API** — API endpoints reject requests from non-loopback origins

## Installation

### Prerequisites

- Python 3.10 or later (when running from source)
- pip package manager

### Steps

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Configure the application by editing `config.json`. It is created
   automatically from `config.example.json` on first run. At minimum, set
   your channel name:

```json
{
  "twitch_channel": "your_channel_name"
}
```

3. Start the application:

```bash
python twitchtts_app.py
```

Or double-click `start_tts.bat`. For the standalone executable, just run
`twitchTTS.exe` — config is created automatically in `%APPDATA%\TwitchTTS`.

## System Requirements

| Requirement | Minimum |
|-------------|---------|
| OS | Windows 10/11 (system voices need Windows) or Linux/macOS with Python 3.10+ |
| CPU | 2 cores, any architecture (a Raspberry Pi-class device is fine) |
| RAM | 1 GB (the engine itself uses ~100 MB) |
| Disk | 100 MB |
| Network | Only needed for Neural (edge-tts) mode |
| Audio device | Only needed for host-audio playback |

- **Neural mode** (default) — synthesis happens on Microsoft's Edge TTS
  cloud service, so local CPU/GPU performance is irrelevant. Latency per
  message is dominated by the network round trip (~0.5–2 s). Requires an
  internet connection.
- **System mode** — fully offline synthesis using the Windows built-in SAPI
  voices (e.g. Microsoft David / Zira). Zero network, zero latency, instant;
  voice quality is simpler than the neural voices.
- **Host audio playback** — additionally requires a working audio output
  device on the machine running the engine (played via pygame-ce).

No special hardware is recommended; the heavy lifting is cloud-side in
Neural mode, and System mode needs nothing at all.

## The App Window

The tray app's main window is the control center:

| Control | Function |
|---------|----------|
| **Mute / Skip** | Mute host audio / skip the currently playing message |
| **Voice** | Selects the active voice (neural or system, depending on the engine) |
| **Engine** | Switches between Neural (edge-tts, cloud) and System (SAPI, offline) |
| **Volume** | Vertical slider beside the log; adjusts host playback volume |
| **Now Playing** | Shows the message currently being spoken and queue depth |
| **Engine Log** | Real-time status display |
| **About** (bottom-left) | Version, license, third-party notices |
| **Options...** (bottom-right) | Edits `config.json` from the GUI |

The tray icon shows engine state (green ok / red error / grey offline) with
a red slash while muted. Closing the window minimizes to tray by default;
change this in Options (`close_to_tray`).

## Configuration

Configuration is managed via `config.json` — next to the sources when
running from a checkout, in `%APPDATA%\TwitchTTS\config.json` for the
standalone exe. It is created automatically from `config.example.json` on
first run, and can be edited with the **Options...** button in the app
window instead of by hand. All changes are saved to `config.json` and apply
after restart — use "Save & Restart" to apply immediately.

| Key | Default | Description |
|-----|---------|-------------|
| `twitch_channel` | `your_channel_name` | Target Twitch channel to monitor |
| `http_port` | `8080` | TTS API port (legacy web UI is served here too) |
| `stream_port` | `8081` | Server-sent events (SSE) chat stream port |
| `tts_voice` | `en-US-JennyNeural` | Default voice identifier (neural or system) |
| `tts_mode` | `edge` | TTS engine: `edge` (cloud neural) or `system` (offline SAPI) |
| `command_prefix` | `!tts` | Prefix required before speech-eligible text |
| `special_users` | `[]` | Users who can speak without the prefix and bypass cooldowns |
| `cooldown_seconds` | `3` | Per-user cooldown between accepted messages (non-special users) |
| `max_chars` | `200` | Maximum message length accepted for TTS |
| `close_to_tray` | `true` | Close button minimizes to tray; set `false` to exit the whole app instead |
| `audio.enabled` | `true` | Play audio on the host machine via pygame |
| `audio.volume` | `0.8` | Host playback volume (0.0–1.0) |
| `audio.muted` | `false` | Mute host playback without dropping the queue |
| `audio.queue_size` | `50` | Maximum number of pending messages buffered for synthesis |

## Chat Commands

| Command | Who | Effect |
|---------|-----|--------|
| `!tts <text>` | everyone | Speaks `<text>` (prefix configurable via `command_prefix`) |
| `!voice <name>` | everyone (host-audio mode) | Sets your personal TTS voice; your messages are then spoken with it |
| `!voice` | everyone (host-audio mode) | Tells you which voice you currently have |
| `!voice reset` | everyone (host-audio mode) | Clears your personal voice override |

Voice names are matched loosely (case and hyphens don't matter): `!voice en-us-aria-neural`
and `!voice en-US-AriaNeural` both work. Per-user voices are remembered across
restarts (`user_voices.json` next to `config.json`) and only apply to host-audio
playback.

## Special Users

Users listed in `special_users` can trigger voice synthesis without requiring a command prefix and bypass the per-user cooldown. Their messages are still subject to content sanitization and the `max_chars` limit. All other users must prefix their message with `command_prefix`.

## Building the Executable

Run `build.bat` (produces `dist/twitchTTS.exe`). The same binary hosts the
engine via a hidden `--engine` flag. The standalone executable stores its
configuration in the per-user app data folder (`%APPDATA%\TwitchTTS`) and
creates it from the bundled example on first run — you can drop the exe
anywhere without JSON files appearing next to it.

## Application Architecture

`app.py` runs the service using one asyncio event loop plus a thread-pool-backed
HTTP server:

- Twitch IRC is read anonymously on port `6667`.
- The HTTP API is served on `http_port` (default `8080`).
- Clients connect to the server-sent event stream on `stream_port`
  (default `8081`).
- `emotes.py` loads third-party emote names from public BTTV, FFZ, and 7TV
  endpoints. A failure only disables that emote source; TTS continues running.
- `sanitize.py` removes Twitch emotes, URLs, emoji, third-party emotes, and
  excess whitespace before text is sent to TTS.
- `audio.py` provides the `AudioPlayer` queue thread for host playback using
  `pygame-ce` (imported as `pygame`). It handles skip/clear, live volume and
  mute, and applies a short fade-in/out to each message so playback starts
  without clicks.
- `config.py` resolves the configuration path: repo dir for source runs,
  `%APPDATA%\TwitchTTS` for the frozen exe.
- Accepted messages are synthesized in a dedicated worker thread (edge-tts or
  SAPI, per `tts_mode`) and handed to `AudioPlayer`.

### Event stream (SSE)

Events are JSON objects with one of these types:

| Type | Purpose |
|------|---------|
| `log` | Engine status message |
| `voices` | The active voice list |
| `voice` | The active voice (and engine mode) changed |
| `chat` | An accepted Twitch message |
| `now_playing` | The message currently being spoken |
| `control` | Mute/volume state and queue depth |

### HTTP API

All API routes reject requests whose `Origin`/`Referer` is not loopback
(`localhost`, `127.0.0.1`, `::1`).

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/voices` | GET | List voices for the active engine mode |
| `/api/tts?text=...&voice=...` | GET | Synthesize audio (MP3 for neural, WAV for system) |
| `/api/config` | GET | Engine state: ports, voice, mode, chat rules, audio settings |
| `/api/voice` | POST | Change voice and/or engine mode (`{ "voice": "...", "mode": "edge"\|"system" }`) |
| `/api/config` | POST | Persist configuration; voice/mode/volume/mute apply immediately |
| `/api/control` | POST | Mute/volume/skip (`{ "action": "mute" \| "unmute" \| "toggle_mute" \| "skip" \| "volume", "value": 0..1 }`) |

Text is sanitized and limited to `max_chars` characters before synthesis.

## Legacy web UI (obsolete)

An older browser-based control page (`index.html`, served on `http_port`)
is still bundled for backwards compatibility but is considered obsolete —
the desktop app is the supported interface. It may be removed in a future
release.

## Support

For issues or feature requests, please refer to the project repository.

## License

Released under the [MIT License](LICENSE). See also
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for bundled dependencies.
