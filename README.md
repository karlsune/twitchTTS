# Twitch TTS Engine

A text-to-speech system that monitors Twitch chat and synthesizes messages into audio for stream overlays and local monitoring.

## Overview

Twitch TTS Engine provides real-time voice synthesis for Twitch chat messages. It connects to Twitch IRC anonymously and uses neural text-to-speech to generate natural-sounding audio, which can be integrated directly into streaming workflows via OBS or other broadcast software.

## Features

- **Anonymous IRC Connection** — No Twitch API keys required
- **Neural Text-to-Speech** — Server-side synthesis via [edge-tts](https://github.com/rany2/edge-tts) with fallback to browser-based TTS
- **Host Audio Playback** — Audio plays on the machine running the engine (pygame), so playback survives browser/overlay reconnects; a bounded server-side queue prevents flooding
- **Content Sanitization** — Automatically removes emotes, emojis, and URLs from chat text before synthesis
- **Third-Party Emote Support** — Detects and strips BTTV, FFZ, and 7TV emotes for the configured Twitch channel without requiring users to set separate emote IDs
- **Config-Driven Rules** — Command prefix, special users, per-user cooldown, and message length limits are all set in `config.json`
- **Web-Based Controls** — Mute, volume, skip, and voice selection via the browser, which doubles as a remote control in host-audio mode
- **Persistent Settings** — Browser preferences stored in local storage; host-audio settings live in `config.json`
- **Reliable Streaming** — Server-sent events (SSE) with automatic reconnection and heartbeat monitoring
- **Local-Only API** — API endpoints reject requests from non-loopback origins
- **Special User Support** — Designated users can trigger voice synthesis without command prefix

## Installation

### Prerequisites

- Python 3.10 or later
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
python app.py
```

Alternatively on Windows, double-click `start_tts.bat`.

4. Open the web interface:

```
http://localhost:8080/index.html
```

You can use this URL directly in a browser or as an OBS browser source.

## User Interface

### Controls

| Control | Function |
|---------|----------|
| **Mute** | Stops audio playback and clears the queue (in host-audio mode, mutes the server) |
| **Skip** | Skips the currently playing message and advances the queue |
| **Volume** | Adjusts output volume from 0–100% |
| **Mode Badge** | Shows whether audio plays on the host (`Host audio`) or in the browser (`Browser mode`) |
| **TTS Mode** | Selects synthesis engine: Server (Neural) or Browser (hidden in host-audio mode) |
| **Neural Voice** | Selects an edge-tts voice (English voices displayed by default) |
| **Now Playing** | Displays the currently spoken message and pending queue depth |
| **Engine Log** | Real-time status display showing connection and processing events |
| **Audio Unlock** | Required once per page load to enable browser audio playback (browser mode only) |

## Configuration

Configuration is managed via `config.json` in the application root directory.

| Key | Default | Description |
|-----|---------|-------------|
| `twitch_channel` | `kasunlol` | Target Twitch channel to monitor |
| `http_port` | `8080` | Web UI and TTS API port |
| `stream_port` | `8081` | Server-sent events (SSE) chat stream port |
| `tts_voice` | `en-US-JennyNeural` | Default edge-tts voice identifier |
| `command_prefix` | `!tts` | Prefix required before speech-eligible text |
| `special_users` | `[]` | Users who can speak without the prefix and bypass cooldowns |
| `cooldown_seconds` | `3` | Per-user cooldown between accepted messages (non-special users) |
| `max_chars` | `200` | Maximum message length accepted for TTS (longer text is truncated) |
| `audio.enabled` | `true` | Play audio on the host machine via pygame |
| `audio.volume` | `0.8` | Host playback volume (0.0–1.0) |
| `audio.muted` | `false` | Mute host playback without dropping the queue |
| `audio.queue_size` | `50` | Maximum number of pending messages buffered for synthesis |

To list available voices, run:

```bash
edge-tts --list-voices
```

## Special Users

Users listed in `special_users` can trigger voice synthesis without requiring a command prefix and bypass the per-user cooldown. Their messages are still subject to content sanitization and the `max_chars` limit. All other users must prefix their message with `command_prefix`.

## OBS Integration

To integrate Twitch TTS Engine into OBS:

1. Add a new Browser source to your scene
2. Set the URL to: `http://localhost:8080/index.html`
3. Configure source dimensions to fit your layout
4. Enable transparent background if your scene design supports it
5. Optionally crop the control panel if only chat text should be visible on stream

## Application Architecture

`app.py` runs the service using one asyncio event loop plus a thread-pool-backed
HTTP server:

- Twitch IRC is read anonymously on port `6667`.
- The web UI and HTTP API are served on `http_port` (default `8080`).
- Browser clients connect to the server-sent event stream on `stream_port`
  (default `8081`).
- `emotes.py` loads third-party emote names from public BTTV, FFZ, and 7TV
  endpoints. A failure only disables that emote source; TTS continues running.
- `sanitize.py` removes Twitch emotes, URLs, emoji, third-party emotes, and
  excess whitespace before text is sent to TTS.
- `audio.py` provides the `AudioPlayer` queue thread for host playback using
  `pygame-ce` (imported as `pygame`). It handles skip/clear, live volume and
  mute, exposes a `pending` count, and shuts down cleanly.
- Host-audio mode synthesizes accepted messages with edge-tts in a dedicated
  worker thread and hands the resulting MP3 to `AudioPlayer`. If pygame or an
  audio device is unavailable, the engine falls back to browser-mode playback.

### Browser API

The frontend reads the stream port from `GET /api/config` and connects to that
port using SSE (no hardcoded URL). Events are JSON objects with one of these
types:

| Type | Purpose |
|------|---------|
| `log` | Displays an engine status message |
| `voices` | Populates the server voice selector |
| `voice` | Notifies clients that the active voice changed |
| `chat` | Displays an accepted Twitch message (queued locally in browser mode) |
| `now_playing` | Shows which message is currently being spoken (host-audio mode) |
| `control` | Broadcasts mute/volume state and queue depth (host-audio mode) |

### HTTP API

All API routes reject requests whose `Origin`/`Referer` is not loopback
(`localhost`, `127.0.0.1`, `::1`), which stops random websites from driving the
engine through the user's browser.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/voices` | GET | List available edge-tts voices |
| `/api/tts?text=...&voice=...` | GET | Synthesize MP3 for browser-mode playback |
| `/api/config` | GET | Engine state: ports, voice, chat rules, and audio settings |
| `/api/voice` | POST | Change the active edge-tts voice (`{ "voice": "..." }`) |
| `/api/control` | POST | Mute/volume/skip (`{ "action": "mute" \| "unmute" \| "skip" \| "volume", "value": 0..1 }`) |

Text is sanitized and limited to `max_chars` characters before synthesis.

### Startup and Shutdown

`start_tts.bat` reads `http_port` and `stream_port` from `config.json`, closes
stale listeners on those ports, starts `app.py`, waits briefly for the HTTP
server, and opens the browser UI. Stop the Python process with `Ctrl+C`; the
audio worker, console thread, and TTS thread pool are then shut down without
blocking application exit.

### Console controls (host-audio mode)

While `app.py` runs in a terminal, these keys control host playback:

| Key | Action |
|-----|--------|
| `m` | Toggle mute |
| `+` / `-` | Raise / lower volume by 5% |
| `s` | Skip the current message |
| `q` | Show a reminder to press `Ctrl+C` to stop |

### Command-line flags

| Flag | Effect |
|------|--------|
| `--no-audio` | Force browser-mode playback even when `audio.enabled` is true |
| `--audio` | Force host-audio playback even when `audio.enabled` is false |

## Support

For issues or feature requests, please refer to the project repository.

## License

Released under the [MIT License](LICENSE).