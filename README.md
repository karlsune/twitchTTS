# Twitch TTS

Reads Twitch chat and speaks messages aloud for stream overlays or local monitoring.

## Features

- Anonymous Twitch IRC connection (no API keys)
- Server-side neural TTS via [edge-tts](https://github.com/rany2/edge-tts) with browser TTS fallback
- Strips URLs and emojis before speaking
- Mute button and volume slider (settings persist in browser)
- SSE chat stream with reconnect and heartbeat

## Setup

1. Install Python 3.10+.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Edit `config.json` and set your Twitch channel:

```json
{
  "twitch_channel": "yourchannel"
}
```

4. Start the app:

```bash
python app.py
```

Or double-click `start_tts.bat` on Windows.

5. Open `http://localhost:8080/index.html` in a browser or OBS browser source.

## Controls

- **Mute** — stops playback and clears the queue
- **Volume** — 0–100%
- **TTS mode** — Server (Neural) uses edge-tts; Browser uses local speech synthesis
- **Voice** — visible in Browser mode only

## OBS tips

- Use a browser source pointed at `http://localhost:8080/index.html`
- Set background to transparent if your OBS scene supports it
- Crop the control bar if you only want chat text visible on stream

## Config

| Key | Default | Description |
|-----|---------|-------------|
| `twitch_channel` | `kasunlol` | Twitch channel to join |
| `http_port` | `8080` | Web UI and TTS API |
| `stream_port` | `8081` | SSE chat stream |
| `tts_voice` | `en-US-JennyNeural` | edge-tts voice name |

Browse voices: `edge-tts --list-voices`
