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

## Live admin panel

Click **Admin** (top-right of the overlay bar) to open the admin drawer. Most
settings apply **live** — no restart needed:

- **TTS rules** (live): mode, command prefix, permission tier, per-user cooldown, default neural voice, max characters. Edit and click **Save rules**; changes take effect on the next chat message and are persisted to `config.json`.
- **User overrides**: allow/remove individual users from the commandless and no-cooldown whitelists (shown as chips).
- **Global overrides**: a toggle to force commandless TTS for everyone; a shut-down button.

Ports (`http_port`, `stream_port`) and `twitch_channel` are bound at startup and
still require a restart to change (the panel will tell you).

## Controls

- **Mute** — stops playback and clears the queue
- **Volume** — 0–100%
- **TTS mode** — Server (Neural) uses edge-tts; Browser uses local speech synthesis
- **Voice** — visible in Browser mode only
- **Neural voice** — pick an edge-tts voice when Server mode is selected (English voices shown by default)
- **Engine log** — live status panel at the bottom of the page (replaces CLI output)
- **Click to enable TTS** — required once per page load to unlock browser audio

## Broadcaster admin panel

The page includes a broadcaster admin panel (allow/disallow users, force modes, shutdown). It's protected by a token so a random local webpage can't drive it.

- **No setup required.** On first launch the server generates a random token, saves it to `admin_token.txt` (git-ignored), and the admin panel — served from the same origin — fetches it automatically. You never type or edit anything.
- The token is delivered only to the same-origin UI via `/api/config` (no CORS header), so cross-origin pages can't read it.
- To pin a fixed token instead, set `admin_secret` in `config.json` (any value other than the placeholder `changeme`). Leave it out to use the auto-generated one.

## OBS tips

- Use a browser source pointed at `http://localhost:8080/index.html`
- Set background to transparent if your OBS scene supports it
- Crop the control bar if you only want chat text visible on stream

## Config

| Key | Default | Description |
|-----|---------|-------------|
| `twitch_channel` | `kasunlol` | Twitch channel to join (restart required) |
| `http_port` | `8080` | Web UI and TTS API |
| `stream_port` | `8081` | SSE chat stream |
| `tts_voice` | `en-US-JennyNeural` | edge-tts voice name |
| `tts_mode` | `all` | `all` speaks every message; `command` only speaks `!tts <message>` |
| `tts_command` | `!tts` | Chat prefix that triggers TTS in `command` mode |
| `tts_permission` | `everyone` | Who may use the command: `everyone`, `subscriber`, `vip`, `moderator`, `broadcaster` |
| `tts_cooldown_seconds` | `0` | Per-user cooldown between accepted TTS requests |
| `max_tts_chars` | `200` | Maximum characters accepted for one TTS utterance |

All keys except the ports and `twitch_channel` are editable live from the admin
panel and written back to `config.json` automatically.

Browse voices: `edge-tts --list-voices`

### TTS activation modes

- **`all`** (original behavior): every chat message is read aloud; messages starting with `!` are ignored.
- **`command`**: only messages sent as `!tts your message here` are read aloud. The `!tts` keyword itself is not spoken. Requests are gated by `tts_permission` and rate-limited per user by `tts_cooldown_seconds`.

Flip between them live from the admin panel, or by editing `tts_mode` in `config.json`.

Permission tiers are cumulative (higher tiers include lower ones): `broadcaster` > `moderator` > `vip` > `subscriber` > `everyone`. For example, `tts_permission: "subscriber"` lets subs, VIPs, mods, and the broadcaster use the command.
