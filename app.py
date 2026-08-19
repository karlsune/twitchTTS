"""Run the Twitch chat listener, TTS API, and browser event stream.

The process has three cooperating pieces:

* an asyncio task that reads anonymous Twitch IRC messages;
* a small HTTP server that serves the web UI and synthesizes audio; and
* an asyncio SSE server that sends logs, voices, and accepted chat messages
    to connected browser clients.

Runtime settings are loaded from ``config.json`` next to this file.
"""

import argparse
import asyncio
import concurrent.futures
import http.server
import json
import shutil
import queue
import socketserver
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime
from pathlib import Path

import edge_tts

from emotes import fetch_emote_names
from sanitize import sanitize_chat_text

try:
    from audio import AudioPlayer, init_player

    AUDIO_IMPORT_OK = True
except ImportError:
    # pygame not installed: host audio stays disabled, browser mode still works.
    AudioPlayer = None  # type: ignore[assignment]
    init_player = None  # type: ignore[assignment]
    AUDIO_IMPORT_OK = False

LOG_BUFFER_SIZE = 50

# Application base directory: next to the executable when frozen (PyInstaller),
# next to this file when running from source. Bundled data files (web UI,
# example config) live in the onefile extraction dir (_MEIPASS) when frozen.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = BASE_DIR
CONFIG_PATH = BASE_DIR / "config.json"

# Lightweight runtime status for the standalone shell's tray icon.
ENGINE_STATUS = {
    "started_at": time.time(),
    "irc_connected": False,
    "last_error": None,
    "last_error_at": 0.0,
}

connected_clients: set[asyncio.StreamWriter] = set()
log_buffer: deque[dict] = deque(maxlen=LOG_BUFFER_SIZE)
log_lock = threading.Lock()
main_loop: asyncio.AbstractEventLoop | None = None
cached_voices: list[dict] | None = None
voices_lock = threading.Lock()
third_party_emotes: set[str] = set()
tts_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts")

# Host-audio (server-side playback) state.
server_voice: str = "en-US-JennyNeural"
audio_player: AudioPlayer | None = None
speech_queue: queue.Queue | None = None
audio_enabled = False
audio_state = {"muted": False, "volume": 0.8}
last_speak_time: dict[str, float] = {}
last_speak_lock = threading.Lock()


def load_config() -> dict:
    """Load the JSON configuration located beside the application file.

    On first run, ``config.json`` is created from ``config.example.json``
    so that a fresh checkout works without manual setup.
    """
    if not CONFIG_PATH.exists():
        example_path = CONFIG_PATH.with_name("config.example.json")
        if not example_path.exists():
            bundled = BUNDLE_DIR / "config.example.json"
            if bundled.exists():
                example_path = bundled
        if not example_path.exists():
            raise FileNotFoundError(
                f"Neither {CONFIG_PATH.name} nor {example_path.name} was found. "
                "Copy config.example.json to config.json and edit it."
            )
        shutil.copyfile(example_path, CONFIG_PATH)
    with CONFIG_PATH.open(encoding="utf-8-sig") as config_file:
        return json.load(config_file)


CONFIG = load_config()
TWITCH_CHANNEL = CONFIG["twitch_channel"]
HTTP_PORT = CONFIG["http_port"]
STREAM_PORT = CONFIG["stream_port"]
DEFAULT_TTS_VOICE = CONFIG.get("tts_voice", "en-US-JennyNeural")
COMMAND_PREFIX = CONFIG.get("command_prefix", "!tts")
SPECIAL_USERS = {user.lower() for user in CONFIG.get("special_users", [])}
COOLDOWN_SECONDS = float(CONFIG.get("cooldown_seconds", 0))
MAX_CHARS = int(CONFIG.get("max_chars", 200))
AUDIO_CONFIG = CONFIG.get("audio", {}) if isinstance(CONFIG.get("audio", {}), dict) else {}
AUDIO_ENABLED_BY_DEFAULT = bool(AUDIO_CONFIG.get("enabled", True))
QUEUE_SIZE = max(1, int(AUDIO_CONFIG.get("queue_size", 50)))
audio_state["muted"] = bool(AUDIO_CONFIG.get("muted", False))
audio_state["volume"] = max(0.0, min(1.0, float(AUDIO_CONFIG.get("volume", 0.8))))
server_voice = DEFAULT_TTS_VOICE



def quiet_connection_errors(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Ignore normal browser disconnects while preserving other loop errors."""
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close an asyncio stream unless it has already started closing."""
    if writer.is_closing():
        return
    writer.close()


def app_log(message: str, level: str = "info") -> None:
    """Store a log entry and broadcast it to currently connected browsers."""
    entry = {
        "type": "log",
        "level": level,
        "message": message,
        "time": datetime.now().strftime("%H:%M:%S"),
    }

    with log_lock:
        log_buffer.append(entry)

    print(f"[{entry['time']}] {message}", flush=True)

    # Feed the status tracker used by /api/status and the tray shell.
    if level == "error":
        ENGINE_STATUS["last_error"] = message
        ENGINE_STATUS["last_error_at"] = time.time()
    elif "Connected anonymously" in message:
        ENGINE_STATUS["irc_connected"] = True
    elif "IRC connection lost" in message:
        ENGINE_STATUS["irc_connected"] = False

    if main_loop and main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast(json.dumps(entry)), main_loop)


def origin_allowed(origin: str | None) -> bool:
    """Allow empty origins (curl/CLI) and loopback browser origins.

    This prevents random websites from triggering TTS or reading chat through
    the user's browser while keeping same-machine tooling working.
    """
    if not origin:
        return True
    try:
        host = urllib.parse.urlparse(origin).hostname or ""
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def header_origin(request_text: str) -> str | None:
    """Extract the Origin (or Referer) header from a raw HTTP request."""
    for line in request_text.split("\r\n")[1:]:
        name, _, value = line.partition(":")
        if name.lower() in ("origin", "referer"):
            return value.strip()
    return None


def check_cooldown(user: str) -> bool:
    """Return True when a user may speak, enforcing a per-user cooldown."""
    if COOLDOWN_SECONDS <= 0:
        return True
    now = time.monotonic()
    with last_speak_lock:
        last = last_speak_time.get(user)
        if last is not None and now - last < COOLDOWN_SECONDS:
            return False
        last_speak_time[user] = now
        if len(last_speak_time) > 1000:
            for name, last_at in list(last_speak_time.items()):
                if now - last_at > 600:
                    del last_speak_time[name]
    return True


def broadcast_control_state() -> None:
    """Broadcast the current host-audio state and queue depth to browsers."""
    if not main_loop or not main_loop.is_running():
        return
    payload = json.dumps(
        {
            "type": "control",
            "muted": audio_state["muted"],
            "volume": audio_state["volume"],
            "queue_size": speech_queue.qsize() if speech_queue else 0,
        }
    )
    asyncio.run_coroutine_threadsafe(broadcast(payload), main_loop)


def set_muted(muted: bool) -> None:
    """Mute or unmute host audio and notify connected browsers."""
    audio_state["muted"] = bool(muted)
    if audio_player is not None:
        audio_player.set_muted(audio_state["muted"])
    broadcast_control_state()


def adjust_volume(delta: float) -> None:
    """Adjust the host-audio volume by a step and notify connected browsers."""
    audio_state["volume"] = max(0.0, min(1.0, audio_state["volume"] + delta))
    if audio_player is not None:
        audio_player.set_volume(audio_state["volume"])
    broadcast_control_state()


def enqueue_speech(user: str, text: str) -> None:
    """Add a chat message to the host-audio queue, dropping the oldest when full."""
    if speech_queue is None:
        return
    if speech_queue.full():
        try:
            speech_queue.get_nowait()
        except queue.Empty:
            pass
        app_log("Audio queue full: dropped oldest message", level="warn")
    speech_queue.put_nowait((user, text))
    broadcast_control_state()


def audio_worker() -> None:
    """Synthesize queued chat messages and play them on the host speakers."""
    while True:
        if speech_queue is None:
            return
        item = speech_queue.get()
        if item is None:
            return
        user, text = item
        try:
            mp3 = generate_tts_audio_sync(text, server_voice)
        except Exception as exc:
            app_log(f"TTS generation failed for {user}: {exc}", level="error")
            continue
        if audio_player is None:
            continue
        audio_player.play(mp3)
        app_log(f"Now speaking: {user}: {text}")
        payload = json.dumps({"type": "now_playing", "user": user, "text": text})
        if main_loop and main_loop.is_running():
            asyncio.run_coroutine_threadsafe(broadcast(payload), main_loop)


def _console_key(key: str) -> None:
    """Handle a single console keypress for host-audio controls."""
    if key == "m":
        set_muted(not audio_state["muted"])
        app_log(f"{'Muted' if audio_state['muted'] else 'Unmuted'} host audio")
    elif key in ("+", "="):
        adjust_volume(0.05)
        app_log(f"Volume {int(audio_state['volume'] * 100)}%")
    elif key == "-":
        adjust_volume(-0.05)
        app_log(f"Volume {int(audio_state['volume'] * 100)}%")
    elif key == "s":
        if audio_player is not None:
            audio_player.skip()
        app_log("Skipped current message")
    elif key == "q":
        app_log("Press Ctrl+C to stop the engine")


def console_controls_windows() -> None:
    """Poll Windows console keys for host-audio controls."""
    import msvcrt

    app_log("Audio keys: [m] mute  [+/-] volume  [s] skip  [q] quit")
    while True:
        if msvcrt.kbhit():
            _console_key(msvcrt.getwch().lower())
        time.sleep(0.05)


def console_controls_prompt() -> None:
    """Fallback console controls using line input (non-Windows)."""
    app_log("Audio keys: [m] mute  [+/-] volume  [s] skip  [q] quit")
    while True:
        try:
            key = input("audio> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if key:
            _console_key(key[0])


def start_console_controls() -> None:
    """Start the host-audio console control loop in a background thread."""
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        threading.Thread(
            target=console_controls_prompt, name="console-controls", daemon=True
        ).start()
    else:
        threading.Thread(
            target=console_controls_windows, name="console-controls", daemon=True
        ).start()


async def generate_tts_audio(text: str, voice: str) -> bytes:
    """Generate an MP3 byte stream with the requested Edge TTS voice."""
    communicate = edge_tts.Communicate(text, voice)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            data = chunk.get("data")
            if data is not None:
                audio.extend(data)
    return bytes(audio)


def generate_tts_audio_sync(text: str, voice: str) -> bytes:
    """Expose async TTS generation to the thread-pool HTTP handler."""
    return asyncio.run(generate_tts_audio(text, voice))


async def list_voices() -> list[dict]:
    """Fetch Edge TTS voices and convert them to the browser-facing format."""
    voices = await edge_tts.list_voices()
    return [
        {
            "name": voice["ShortName"],
            "gender": voice["Gender"],
            "locale": voice["Locale"],
            "label": f"{voice['ShortName']} ({voice['Gender']}, {voice['Locale']})",
        }
        for voice in voices
    ]


def get_cached_voices() -> list[dict]:
    """Return the cached voice list, loading it safely from any server thread."""
    global cached_voices
    with voices_lock:
        if cached_voices is not None:
            return cached_voices

    if main_loop and main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(list_voices(), main_loop)
        voices = future.result(timeout=30)
    else:
        voices = asyncio.run(list_voices())

    with voices_lock:
        cached_voices = voices
    return voices


async def load_third_party_emotes() -> None:
    """Fetch BTTV/FFZ/7TV emote names off the event loop, best-effort."""
    global third_party_emotes
    try:
        names, info = await asyncio.to_thread(fetch_emote_names, TWITCH_CHANNEL)
        third_party_emotes = names
        app_log(f"Emote filter ready: {info}")
    except Exception as exc:
        app_log(f"Could not load third-party emotes: {exc}", level="warn")


async def preload_voices() -> None:
    """Warm up the voice list cache before handling HTTP requests."""
    global cached_voices
    try:
        voices = await list_voices()
        with voices_lock:
            cached_voices = voices
        app_log(f"Loaded {len(voices)} neural voices")
    except Exception as exc:
        app_log(f"Failed to preload voices: {exc}", level="warn")


async def _write_to_clients(message: bytes) -> None:
    """Write an SSE frame to each client and remove failed connections."""
    if not connected_clients:
        return

    dead_clients: list[asyncio.StreamWriter] = []

    for client in list(connected_clients):
        try:
            client.write(message)
            await client.drain()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            dead_clients.append(client)
        except Exception as exc:
            app_log(f"Removing disconnected client: {exc}", level="warn")
            dead_clients.append(client)

    for client in dead_clients:
        connected_clients.discard(client)


async def broadcast(payload: str) -> None:
    """Send a JSON payload as a server-sent event to all browser clients."""
    await _write_to_clients(f"data: {payload}\n\n".encode("utf-8"))


async def send_log_history(writer: asyncio.StreamWriter) -> None:
    """Replay buffered log entries to a newly connected browser."""
    with log_lock:
        history = list(log_buffer)

    for entry in history:
        message = f"data: {json.dumps(entry)}\n\n".encode("utf-8")
        writer.write(message)
    await writer.drain()


def english_voices(voices: list[dict]) -> list[dict]:
    """Prefer English voices, falling back to all voices if none are available."""
    filtered = [voice for voice in voices if voice["locale"].startswith("en-")]
    return filtered if filtered else voices


async def send_voice_list(writer: asyncio.StreamWriter) -> None:
    """Send the cached, browser-filtered voice list to one client."""
    with voices_lock:
        voices = list(cached_voices) if cached_voices else []

    if not voices:
        return

    payload = json.dumps({"type": "voices", "voices": english_voices(voices)})
    writer.write(f"data: {payload}\n\n".encode("utf-8"))
    await writer.drain()


async def twitch_listener() -> None:
    """Keep the Twitch IRC connection alive and reconnect after failures."""
    while True:
        try:
            await _listen_to_twitch_chat()
        except Exception as exc:
            app_log(f"IRC connection lost ({exc}). Reconnecting in 5 seconds...", level="warn")
            await asyncio.sleep(5)


def parse_irc_line(line: str) -> dict:
    """Parse a single IRCv3 line into tags/prefix/command/params.

    Handles the optional ``@tag=value;...`` prefix that Twitch sends once
    ``twitch.tv/tags`` capability is requested.
    """
    tags: dict[str, str] = {}
    rest = line

    if rest.startswith("@"):
        tag_str, _, rest = rest[1:].partition(" ")
        for pair in tag_str.split(";"):
            key, _, value = pair.partition("=")
            tags[key] = value

    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")

    command, _, params = rest.partition(" ")

    trailing = ""
    if " :" in params:
        params, _, trailing = params.partition(" :")
    elif params.startswith(":"):
        trailing = params[1:]
        params = ""

    return {
        "tags": tags,
        "prefix": prefix,
        "command": command,
        "params": params,
        "trailing": trailing,
    }


def parse_emote_ranges(emotes_tag: str) -> list[tuple[int, int]]:
    """Parse the Twitch ``emotes`` tag into a flat list of (start, end).

    Format: ``id:start-end,start-end/id:start-end`` with inclusive indices.
    """
    ranges: list[tuple[int, int]] = []
    if not emotes_tag:
        return ranges
    for chunk in emotes_tag.split("/"):
        _id, _, positions = chunk.partition(":")
        if not positions:
            continue
        for pos in positions.split(","):
            start_s, _, end_s = pos.partition("-")
            try:
                ranges.append((int(start_s), int(end_s)))
            except ValueError:
                continue
    return ranges


async def _listen_to_twitch_chat() -> None:
    """Read Twitch IRC, filter eligible messages, and publish chat events."""
    reader, writer = await asyncio.open_connection("irc.chat.twitch.tv", 6667)

    # Request IRCv3 tags so Twitch tells us the exact emote character ranges.
    writer.write(b"CAP REQ :twitch.tv/tags\r\n")
    writer.write(b"NICK justinfan12345\r\n")
    writer.write(f"JOIN #{TWITCH_CHANNEL.lower()}\r\n".encode("utf-8"))
    await writer.drain()

    app_log(f"Connected anonymously to #{TWITCH_CHANNEL} (tags enabled)")

    while True:
        line = await reader.readline()
        if not line:
            raise ConnectionError("IRC connection closed")

        message = line.decode("utf-8", errors="replace").strip()

        if message.startswith("PING"):
            writer.write(b"PONG :tmi.twitch.tv\r\n")
            await writer.drain()
            continue

        if "PRIVMSG" not in message:
            continue

        parsed = parse_irc_line(message)
        if parsed["command"] != "PRIVMSG":
            continue

        user = parsed["prefix"].split("!")[0] or parsed["tags"].get("display-name", "chat")
        raw_text = parsed["trailing"]
        emote_ranges = parse_emote_ranges(parsed["tags"].get("emotes", ""))

        text = sanitize_chat_text(raw_text, emote_ranges, third_party_emotes)

        # Ignore empty or overly long messages.
        # Special users can speak without the command prefix; everyone else
        # must start messages with the configured prefix (default "!tts").
        is_special_user = user.lower() in SPECIAL_USERS
        if is_special_user:
            if not text or len(text) > MAX_CHARS:
                continue
        else:
            if not raw_text.lower().startswith(COMMAND_PREFIX):
                continue
            if text.lower().startswith(COMMAND_PREFIX):
                text = text[len(COMMAND_PREFIX):].lstrip()
            if not text or len(text) > MAX_CHARS:
                continue

        # Enforce a per-user cooldown so spammers cannot flood the queue.
        if not is_special_user and not check_cooldown(user):
            continue

        payload = json.dumps({"type": "chat", "user": user, "text": text})
        await broadcast(payload)

        if audio_enabled:
            enqueue_speech(user, text)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Serve one long-lived SSE client connection on the stream port."""
    peer = writer.get_extra_info("peername")

    # Read the HTTP request so we can enforce the loopback-origin policy.
    try:
        request_data = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=10
        )
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError):
        close_writer(writer)
        return

    origin = header_origin(request_data.decode("utf-8", errors="replace"))
    if not origin_allowed(origin):
        app_log(f"Blocked SSE client from origin: {origin}", level="warn")
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        close_writer(writer)
        return

    header = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: keep-alive\r\n"
        "Access-Control-Allow-Origin: *\r\n\r\n"
    )
    writer.write(header.encode("utf-8"))
    await writer.drain()

    connected_clients.add(writer)
    app_log(f"SSE client connected: {peer}")

    try:
        await send_log_history(writer)
        await send_voice_list(writer)
        while True:
            data = await reader.read(1024)
            if not data:
                break
            await asyncio.sleep(0)
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
        pass
    finally:
        connected_clients.discard(writer)
        close_writer(writer)
        app_log(f"SSE client disconnected: {peer}")


async def sse_heartbeat() -> None:
    """Keep idle SSE connections alive through proxies and browser networking."""
    while True:
        await asyncio.sleep(15)
        await _write_to_clients(b": keepalive\n\n")


def start_http_server() -> None:
    """Serve static files and the HTTP endpoints for voices and TTS audio."""
    web_root = BUNDLE_DIR

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_root), **kwargs)

        def log_message(self, format, *args):
            return

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            super().end_headers()

        def _api_origin_allowed(self) -> bool:
            origin = self.headers.get("Origin") or self.headers.get("Referer")
            return origin_allowed(origin)

        def _reject_cross_origin(self) -> bool:
            """Send 403 when the request does not come from the local UI."""
            if self._api_origin_allowed():
                return False
            self.send_error(403, "Forbidden")
            return True

        def _send_json(self, data) -> None:
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _read_json_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                return None
            if length <= 0 or length > 4096:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None

        def do_GET(self):
            """Route API requests before delegating static files to the base handler."""
            parsed = urllib.parse.urlparse(self.path)
            if (
                parsed.path in ("/api/tts", "/api/voices", "/api/config", "/api/status")
                and self._reject_cross_origin()
            ):
                return
            if parsed.path == "/api/tts":
                self._handle_tts_request(parsed)
                return
            if parsed.path == "/api/voices":
                self._handle_voices_request()
                return
            if parsed.path == "/api/status":
                now = time.time()
                recent_error = (
                    ENGINE_STATUS["last_error"] is not None
                    and now - ENGINE_STATUS["last_error_at"] < 60
                )
                status = "error" if (not ENGINE_STATUS["irc_connected"] or recent_error) else "ok"
                body = json.dumps(
                    {
                        "status": status,
                        "irc_connected": ENGINE_STATUS["irc_connected"],
                        "last_error": ENGINE_STATUS["last_error"],
                        "uptime": int(now - ENGINE_STATUS["started_at"]),
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/config":
                self._handle_config_request()
                return
            self.path = parsed.path
            super().do_GET()

        def do_POST(self):
            """Handle host-audio control endpoints (voice, mute, volume, skip)."""
            parsed = urllib.parse.urlparse(self.path)
            if (
                parsed.path in ("/api/voice", "/api/control")
                and self._reject_cross_origin()
            ):
                return
            if parsed.path == "/api/voice":
                self._handle_voice_request()
                return
            if parsed.path == "/api/control":
                self._handle_control_request()
                return
            self.send_error(404, "Not found")

        def _handle_voices_request(self):
            """Return the cached voice list as JSON."""
            try:
                voices = get_cached_voices()
            except Exception as exc:
                app_log(f"Failed to load voices: {exc}", level="error")
                self.send_error(500, "Failed to load voices")
                return
            self._send_json(voices)

        def _handle_config_request(self):
            """Return runtime settings so the browser can adapt to them."""
            self._send_json(
                {
                    "twitch_channel": TWITCH_CHANNEL,
                    "http_port": HTTP_PORT,
                    "stream_port": STREAM_PORT,
                    "tts_voice": server_voice,
                    "command_prefix": COMMAND_PREFIX,
                    "special_users": sorted(SPECIAL_USERS),
                    "cooldown_seconds": COOLDOWN_SECONDS,
                    "max_chars": MAX_CHARS,
                    "audio_enabled": audio_enabled,
                    "audio": {
                        "enabled": AUDIO_ENABLED_BY_DEFAULT,
                        "volume": audio_state["volume"],
                        "muted": audio_state["muted"],
                        "queue_size": QUEUE_SIZE,
                    },
                }
            )

        def _handle_voice_request(self):
            """Set the voice used for host-audio synthesis."""
            global server_voice
            body = self._read_json_body()
            if not isinstance(body, dict) or not isinstance(body.get("voice"), str):
                self.send_error(400, "Missing voice parameter")
                return
            voice = body["voice"].strip()
            if not voice:
                self.send_error(400, "Missing voice parameter")
                return
            server_voice = voice
            app_log(f"Host audio voice set to {server_voice}")
            if main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    broadcast(json.dumps({"type": "voice", "voice": server_voice})),
                    main_loop,
                )
            self._send_json({"voice": server_voice})

        def _handle_control_request(self):
            """Apply mute/volume/skip controls to host audio."""
            body = self._read_json_body()
            if not isinstance(body, dict) or not isinstance(body.get("action"), str):
                self.send_error(400, "Missing action parameter")
                return
            action = body["action"].lower()
            if action == "mute":
                set_muted(True)
            elif action == "unmute":
                set_muted(False)
            elif action == "toggle_mute":
                set_muted(not audio_state["muted"])
            elif action == "volume":
                try:
                    volume = float(body.get("value"))
                except (TypeError, ValueError):
                    self.send_error(400, "Invalid volume value")
                    return
                audio_state["volume"] = max(0.0, min(1.0, volume))
                if audio_player is not None:
                    audio_player.set_volume(audio_state["volume"])
                broadcast_control_state()
            elif action == "skip":
                if audio_player is not None:
                    audio_player.skip()
                app_log("Skipped current message (remote)")
            else:
                self.send_error(400, f"Unknown action: {action}")
                return
            self._send_json(
                {"muted": audio_state["muted"], "volume": audio_state["volume"]}
            )

        def _handle_tts_request(self, parsed):
            """Validate query parameters, synthesize audio, and return MP3 data."""
            params = urllib.parse.parse_qs(parsed.query)
            text = sanitize_chat_text(params.get("text", [""])[0])
            voice = params.get("voice", [DEFAULT_TTS_VOICE])[0] or DEFAULT_TTS_VOICE

            if not text:
                self.send_error(400, "Missing or empty text parameter")
                return

            if len(text) > MAX_CHARS:
                self.send_error(400, "Text too long")
                return

            try:
                future = tts_executor.submit(generate_tts_audio_sync, text, voice)
                audio = future.result(timeout=30)
            except concurrent.futures.TimeoutError:
                app_log("TTS generation timed out", level="warn")
                self.send_error(504, "TTS generation timed out")
                return
            except Exception as exc:
                app_log(f"TTS generation failed: {exc}", level="error")
                self.send_error(500, "TTS generation failed")
                return

            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(audio)

    class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        with ThreadedHTTPServer(("127.0.0.1", HTTP_PORT), Handler) as httpd:
            app_log(f"Web interface serving at http://localhost:{HTTP_PORT}")
            app_log(f"Serving files from {web_root}")
            httpd.serve_forever()
    except OSError as exc:
        app_log(
            f"Could not start web server on port {HTTP_PORT} ({exc}). "
            "Close any old TTS Engine window and try again.",
            level="error",
        )


async def main() -> None:
    """Start the HTTP/SSE services and run the Twitch listener indefinitely."""
    global main_loop, audio_enabled, audio_player, speech_queue

    parser = argparse.ArgumentParser(description="Twitch TTS engine with host audio")
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="disable host-audio playback; the browser plays audio instead",
    )
    parser.add_argument(
        "--audio",
        action="store_true",
        help="force host-audio playback even when config.json disables it",
    )
    args = parser.parse_args()

    main_loop = asyncio.get_running_loop()
    main_loop.set_exception_handler(quiet_connection_errors)

    app_log("TTS engine starting...")
    threading.Thread(target=start_http_server, daemon=True).start()
    await load_third_party_emotes()

    # Host audio is enabled by default; config and CLI flags can override it.
    audio_requested = AUDIO_ENABLED_BY_DEFAULT
    if args.no_audio:
        audio_requested = False
    if args.audio:
        audio_requested = True

    if audio_requested:
        if not AUDIO_IMPORT_OK:
            app_log(
                "pygame not installed - host audio disabled, browser mode active",
                level="warn",
            )
        else:
            try:
                player = init_player(
                    volume=audio_state["volume"],
                    muted=audio_state["muted"],
                    queue_size=QUEUE_SIZE,
                )
            except Exception as exc:
                app_log(f"Could not initialize audio device: {exc}", level="warn")
                player = None
            if player is None:
                app_log(
                    "No audio output available - host audio disabled, browser mode active",
                    level="warn",
                )
            else:
                audio_player = player
                audio_enabled = True
                speech_queue = queue.Queue(maxsize=QUEUE_SIZE)
                app_log("Host audio enabled - the browser page is now a remote control")
                start_console_controls()
                threading.Thread(
                    target=audio_worker, name="audio-worker", daemon=True
                ).start()

    server = await asyncio.start_server(handle_client, "127.0.0.1", STREAM_PORT)
    app_log(f"Chat stream serving at http://localhost:{STREAM_PORT}")

    async with server:
        await preload_voices()
        await asyncio.gather(
            twitch_listener(),
            sse_heartbeat(),
            server.serve_forever(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        app_log("Process terminated.")
    finally:
        tts_executor.shutdown(wait=False)
        if audio_player is not None:
            audio_player.shutdown()
