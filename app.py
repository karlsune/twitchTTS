import asyncio
import concurrent.futures
import http.server
import json
import socketserver
import threading
import urllib.parse
from pathlib import Path

import edge_tts

from sanitize import sanitize_chat_text

CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        return json.load(config_file)


CONFIG = load_config()
TWITCH_CHANNEL = CONFIG["twitch_channel"]
HTTP_PORT = CONFIG["http_port"]
STREAM_PORT = CONFIG["stream_port"]
TTS_VOICE = CONFIG.get("tts_voice", "en-US-JennyNeural")

connected_clients: set[asyncio.StreamWriter] = set()
tts_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts")


async def generate_tts_audio(text: str) -> bytes:
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


def generate_tts_audio_sync(text: str) -> bytes:
    return asyncio.run(generate_tts_audio(text))


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
            print(f"Removing disconnected client: {exc}")
            dead_clients.append(client)

    for client in dead_clients:
        connected_clients.discard(client)
        try:
            client.close()
        except Exception:
            pass


async def broadcast(payload: str) -> None:
    await _write_to_clients(f"data: {payload}\n\n".encode("utf-8"))


async def twitch_listener() -> None:
    while True:
        try:
            await _listen_to_twitch_chat()
        except Exception as exc:
            print(f"IRC connection lost ({exc}). Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def _listen_to_twitch_chat() -> None:
    reader, writer = await asyncio.open_connection("irc.chat.twitch.tv", 6667)

    writer.write(b"NICK justinfan12345\r\n")
    writer.write(f"JOIN #{TWITCH_CHANNEL.lower()}\r\n".encode("utf-8"))
    await writer.drain()

    print(f"Connected anonymously to #{TWITCH_CHANNEL}")

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

        parts = message.split(":", 2)
        if len(parts) < 3:
            continue

        user = parts[1].split("!")[0]
        text = sanitize_chat_text(parts[2])

        if not text or text.startswith("!") or len(text) > 200:
            continue

        payload = json.dumps({"user": user, "text": text})
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
    print(f"SSE client connected: {peer}")

    try:
        while True:
            data = await reader.read(1024)
            if not data:
                break
            await asyncio.sleep(0)
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
        pass
    finally:
        connected_clients.discard(writer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        print(f"SSE client disconnected: {peer}")


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

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/tts":
                self._handle_tts_request(parsed)
                return
            super().do_GET()

        def _handle_tts_request(self, parsed):
            params = urllib.parse.parse_qs(parsed.query)
            text = sanitize_chat_text(params.get("text", [""])[0])

            if not text:
                self.send_error(400, "Missing or empty text parameter")
                return

            if len(text) > 200:
                self.send_error(400, "Text too long")
                return

            try:
                future = tts_executor.submit(generate_tts_audio_sync, text)
                audio = future.result(timeout=30)
            except concurrent.futures.TimeoutError:
                self.send_error(504, "TTS generation timed out")
                return
            except Exception as exc:
                print(f"TTS generation failed: {exc}")
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
    with socketserver.TCPServer(("127.0.0.1", HTTP_PORT), Handler) as httpd:
        print(f"Web interface serving at http://localhost:{HTTP_PORT}")
        httpd.serve_forever()


async def main() -> None:
    threading.Thread(target=start_http_server, daemon=True).start()

    server = await asyncio.start_server(handle_client, "127.0.0.1", STREAM_PORT)
    print(f"Chat stream serving at http://localhost:{STREAM_PORT}")

    async with server:
        await asyncio.gather(
            twitch_listener(),
            sse_heartbeat(),
            server.serve_forever(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess terminated.")
    finally:
        tts_executor.shutdown(wait=False)
