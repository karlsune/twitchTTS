import asyncio
import concurrent.futures
import http.server
import json
import socketserver
import sys
import threading
import urllib.parse
from collections import deque
from datetime import datetime
from pathlib import Path

import edge_tts

from emotes import fetch_emote_names
from sanitize import sanitize_chat_text

CONFIG_PATH = Path(__file__).with_name("config.json")
LOG_BUFFER_SIZE = 50

connected_clients: set[asyncio.StreamWriter] = set()
log_buffer: deque[dict] = deque(maxlen=LOG_BUFFER_SIZE)
log_lock = threading.Lock()
main_loop: asyncio.AbstractEventLoop | None = None
cached_voices: list[dict] | None = None
voices_lock = threading.Lock()
third_party_emotes: set[str] = set()
tts_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts")


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        return json.load(config_file)


CONFIG = load_config()
TWITCH_CHANNEL = CONFIG["twitch_channel"]
HTTP_PORT = CONFIG["http_port"]
STREAM_PORT = CONFIG["stream_port"]
DEFAULT_TTS_VOICE = CONFIG.get("tts_voice", "en-US-JennyNeural")



def quiet_connection_errors(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


def close_writer(writer: asyncio.StreamWriter) -> None:
    if writer.is_closing():
        return
    writer.close()


def app_log(message: str, level: str = "info") -> None:
    entry = {
        "type": "log",
        "level": level,
        "message": message,
        "time": datetime.now().strftime("%H:%M:%S"),
    }

    with log_lock:
        log_buffer.append(entry)

    if main_loop and main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast(json.dumps(entry)), main_loop)


async def generate_tts_audio(text: str, voice: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            data = chunk.get("data")
            if data is not None:
                audio.extend(data)
    return bytes(audio)


def generate_tts_audio_sync(text: str, voice: str) -> bytes:
    return asyncio.run(generate_tts_audio(text, voice))


async def list_voices() -> list[dict]:
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
    await _write_to_clients(f"data: {payload}\n\n".encode("utf-8"))


async def send_log_history(writer: asyncio.StreamWriter) -> None:
    with log_lock:
        history = list(log_buffer)

    for entry in history:
        message = f"data: {json.dumps(entry)}\n\n".encode("utf-8")
        writer.write(message)
    await writer.drain()


def english_voices(voices: list[dict]) -> list[dict]:
    filtered = [voice for voice in voices if voice["locale"].startswith("en-")]
    return filtered if filtered else voices


async def send_voice_list(writer: asyncio.StreamWriter) -> None:
    with voices_lock:
        voices = list(cached_voices) if cached_voices else []

    if not voices:
        return

    payload = json.dumps({"type": "voices", "voices": english_voices(voices)})
    writer.write(f"data: {payload}\n\n".encode("utf-8"))
    await writer.drain()


async def twitch_listener() -> None:
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
        # Special users can send chat without the !tts prefix.
        # Other users must start messages with !tts.
        is_special_user = user.lower() in ("shitemike", "dexfer")
        if is_special_user:
            if not text or len(text) > 200:
                continue
        else:
            if not raw_text.lower().startswith("!tts"):
                continue
            if text.lower().startswith("!tts"):
                text = text[4:].lstrip()
            if not text or len(text) > 200:
                continue

        payload = json.dumps({"type": "chat", "user": user, "text": text})
        await broadcast(payload)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
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
    while True:
        await asyncio.sleep(15)
        await _write_to_clients(b": keepalive\n\n")


def start_http_server() -> None:
    web_root = Path(__file__).parent

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

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/tts":
                self._handle_tts_request(parsed)
                return
            if parsed.path == "/api/voices":
                self._handle_voices_request()
                return
            self.path = parsed.path
            super().do_GET()

        def _handle_voices_request(self):
            try:
                voices = get_cached_voices()
            except Exception as exc:
                app_log(f"Failed to load voices: {exc}", level="error")
                self.send_error(500, "Failed to load voices")
                return

            payload = json.dumps(voices).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _handle_tts_request(self, parsed):
            params = urllib.parse.parse_qs(parsed.query)
            text = sanitize_chat_text(params.get("text", [""])[0])
            voice = params.get("voice", [DEFAULT_TTS_VOICE])[0] or DEFAULT_TTS_VOICE

            if not text:
                self.send_error(400, "Missing or empty text parameter")
                return

            if len(text) > 200:
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

    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer(("127.0.0.1", HTTP_PORT), Handler) as httpd:
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
    global main_loop
    main_loop = asyncio.get_running_loop()
    main_loop.set_exception_handler(quiet_connection_errors)

    app_log("TTS engine starting...")
    threading.Thread(target=start_http_server, daemon=True).start()
    await load_third_party_emotes()

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
