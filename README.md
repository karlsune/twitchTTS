# Twitch TTS Engine

A text-to-speech system that monitors Twitch chat and synthesizes messages into audio for stream overlays and local monitoring.

## Overview

Twitch TTS Engine provides real-time voice synthesis for Twitch chat messages. It connects to Twitch IRC anonymously and uses neural text-to-speech to generate natural-sounding audio, which can be integrated directly into streaming workflows via OBS or other broadcast software.

## Features

- **Anonymous IRC Connection** — No Twitch API keys required
- **Neural Text-to-Speech** — Server-side synthesis via [edge-tts](https://github.com/rany2/edge-tts) with fallback to browser-based TTS
- **Content Sanitization** — Automatically removes emotes, emojis, and URLs from chat text before synthesis
- **Third-Party Emote Support** — Detects and strips BTTV, FFZ, and 7TV emotes for the configured Twitch channel without requiring users to set separate emote IDs
- **Web-Based Controls** — Mute, volume control, and voice selection via browser interface
- **Persistent Settings** — User preferences stored in browser local storage
- **Reliable Streaming** — Server-sent events (SSE) with automatic reconnection and heartbeat monitoring
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

2. Configure the application by editing `config.json`:

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
| **Mute** | Stops audio playback and clears the message queue |
| **Volume** | Adjusts output volume from 0–100% |
| **TTS Mode** | Selects synthesis engine: Server (Neural) or Browser |
| **Neural Voice** | Selects an edge-tts voice when Server mode is active (English voices displayed by default) |
| **Engine Log** | Real-time status display showing connection and processing events |
| **Audio Unlock** | Required once per page load to enable browser audio playback |

## Configuration

Configuration is managed via `config.json` in the application root directory.

| Key | Default | Description |
|-----|---------|-------------|
| `twitch_channel` | `kasunlol` | Target Twitch channel to monitor |
| `http_port` | `8080` | Web UI and TTS API port |
| `stream_port` | `8081` | Server-sent events (SSE) chat stream port |
| `tts_voice` | `en-US-JennyNeural` | Default edge-tts voice identifier |

To list available voices, run:

```bash
edge-tts --list-voices
```

## Special Users

Designated users (is_special_user) can trigger voice synthesis without requiring a command prefix. Their chat messages will be automatically read aloud, subject to content sanitization and length restrictions.

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

### Browser API

The frontend connects to `http://localhost:8081` using SSE. Events are JSON
objects with one of these types:

| Type | Purpose |
|------|---------|
| `log` | Displays an engine status message |
| `voices` | Populates the server voice selector |
| `chat` | Displays and queues an accepted Twitch message |

The HTTP API exposes `GET /api/voices` for the available voice list and
`GET /api/tts?text=...&voice=...` for MP3 synthesis. Text is sanitized and
limited to 200 characters before synthesis.

### Startup and Shutdown

`start_tts.bat` closes stale listeners on ports `8080` and `8081`, starts
`app.py`, waits briefly for the HTTP server, and opens the browser UI. Stop the
Python process with `Ctrl+C`; the TTS thread pool is then shut down without
blocking application exit.

## Support

For issues or feature requests, please refer to the project repository.