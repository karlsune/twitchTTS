"""Twitch chat -> neural TTS engine.

Connects anonymously to a Twitch channel's IRC chat, sanitizes each message,
and streams speakable text to a browser overlay over Server-Sent Events. The
browser plays audio either via server-generated edge-tts neural voices
(``/api/tts``) or the local Web Speech API.

Architecture
------------
* An asyncio event loop owns the Twitch IRC connection and the SSE chat stream.
* A plain ``http.server`` thread serves the static UI and the JSON/audio APIs.
* A small thread pool renders edge-tts audio without blocking the loop.

All runtime-adjustable configuration lives in the mutable ``SETTINGS`` store,
guarded by ``settings_lock`` and edited live from the admin panel. Ports and the
Twitch channel are bound at startup and require a restart to change.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime
from pathlib import Path

import edge_tts

from emotes import fetch_emote_names
from sanitize import sanitize_chat_text, shift_emote_ranges

# --------------------------------------------------------------------------- #
# Paths and constants
# --------------------------------------------------------------------------- #

CONFIG_PATH = Path(__file__).with_name("config.json")
ADMIN_STATE_PATH = Path(__file__).with_name("admin_state.json")
ADMIN_TOKEN_PATH = Path(__file__).with_name("admin_token.txt")

LOG_BUFFER_SIZE = 50
TTS_TIMEOUT_SECONDS = 30
VOICE_CACHE_TIMEOUT_SECONDS = 30
IRC_RECONNECT_SECONDS = 5
SSE_HEARTBEAT_SECONDS = 15
BROADCASTER_CONTROL_COMMAND = "!ttsadmin"

# Permission tiers ordered from lowest to highest privilege:
# everyone < subscriber < vip < moderator < broadcaster.
PERMISSION_LEVELS = {
    "everyone": 0,
    "subscriber": 1,
    "vip": 2,
    "moderator": 3,
    "broadcaster": 4,
}
VALID_PERMISSIONS = set(PERMISSION_LEVELS)
VALID_MODES = {"all", "command"}

# --------------------------------------------------------------------------- #
# Shared runtime state
# --------------------------------------------------------------------------- #

connected_clients: set[asyncio.StreamWriter] = set()
log_buffer: deque[dict] = deque(maxlen=LOG_BUFFER_SIZE)
log_lock = threading.Lock()

main_loop: asyncio.AbstractEventLoop | None = None
shutdown_event: asyncio.Event | None = None
httpd_server: socketserver.TCPServer | None = None

cached_voices: list[dict] | None = None
cached_voice_names: set[str] = set()
voices_lock = threading.Lock()

third_party_emotes: set[str] = set()
tts_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts")

# Per-user cooldown tracking: normalized username -> last accepted monotonic time.
cooldown_lock = threading.Lock()
last_request_at: dict[str, float] = {}

# Guards the live SETTINGS store shared between the HTTP thread and chat loop.
settings_lock = threading.Lock()

# Guards broadcaster admin overrides (whitelists + force-all).
admin_lock = threading.Lock()
broadcaster_force_all_mode = False
allowed_without_command: set[str] = set()
allowed_without_cooldown: set[str] = set()
# Users whose messages are never spoken, regardless of mode/permission.
blacklisted_users: set[str] = set()


# --------------------------------------------------------------------------- #
# Logging (defined early so everything below can use it)
# --------------------------------------------------------------------------- #

def app_log(message: str, level: str = "info") -> None:
    """Append a log entry to the ring buffer and push it to SSE clients."""
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


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        return json.load(config_file)


CONFIG = load_config()

# Bound at startup; changing these requires a restart (sockets/channel already open).
HTTP_PORT = int(CONFIG.get("http_port", 8080))
STREAM_PORT = int(CONFIG.get("stream_port", 8081))
RESTART_REQUIRED_KEYS = {"http_port", "stream_port", "twitch_channel"}


# --------------------------------------------------------------------------- #
# Live settings store
# --------------------------------------------------------------------------- #

def _coerce_channel(value) -> str:
    text = str(value).strip().lstrip("#").lower()
    if not text:
        raise ValueError("twitch_channel cannot be empty")
    return text


def _coerce_mode(value) -> str:
    text = str(value).strip().lower()
    if text not in VALID_MODES:
        raise ValueError("tts_mode must be 'all' or 'command'")
    return text


def _coerce_command(value) -> str:
    text = str(value).strip().lower()
    if not text:
        raise ValueError("tts_command cannot be empty")
    if " " in text:
        raise ValueError("tts_command cannot contain spaces")
    return text


def _coerce_permission(value) -> str:
    text = str(value).strip().lower()
    if text not in VALID_PERMISSIONS:
        raise ValueError(f"tts_permission must be one of {sorted(VALID_PERMISSIONS)}")
    return text


def _coerce_cooldown(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError("tts_cooldown_seconds must be a number")
    if seconds < 0:
        raise ValueError("tts_cooldown_seconds cannot be negative")
    return seconds


def _coerce_voice(value) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("tts_voice cannot be empty")
    return text


def _coerce_max_chars(value) -> int:
    try:
        chars = int(value)
    except (TypeError, ValueError):
        raise ValueError("max_tts_chars must be an integer")
    if not 1 <= chars <= 2000:
        raise ValueError("max_tts_chars must be between 1 and 2000")
    return chars


# key -> (default, coercer). The coercer raises ValueError on invalid input,
# which the settings API surfaces verbatim to the admin panel.
SETTINGS_SCHEMA: dict[str, tuple[object, object]] = {
    "tts_voice": ("en-US-JennyNeural", _coerce_voice),
    "tts_mode": ("all", _coerce_mode),
    "tts_command": ("!tts", _coerce_command),
    "tts_permission": ("everyone", _coerce_permission),
    "tts_cooldown_seconds": (0.0, _coerce_cooldown),
    "max_tts_chars": (200, _coerce_max_chars),
}


def _build_initial_settings() -> dict:
    values: dict = {}
    for key, (default, coerce) in SETTINGS_SCHEMA.items():
        try:
            values[key] = coerce(CONFIG.get(key, default))
        except ValueError:
            values[key] = coerce(default)
    return values


SETTINGS: dict = _build_initial_settings()
TWITCH_CHANNEL = _coerce_channel(CONFIG.get("twitch_channel", "kasunlol"))


def settings_get(key: str):
    with settings_lock:
        return SETTINGS[key]


def settings_all() -> dict:
    with settings_lock:
        return dict(SETTINGS)


def persist_settings(snapshot: dict) -> None:
    """Merge the live settings back into config.json via atomic replace.

    Restart-only keys already on disk (ports, channel, admin_secret) are
    preserved; only the mutable settings are overwritten.
    """
    try:
        try:
            with CONFIG_PATH.open(encoding="utf-8") as config_file:
                on_disk = json.load(config_file)
        except Exception:
            on_disk = {}
        on_disk.update(snapshot)
        temp_path = CONFIG_PATH.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as config_file:
            json.dump(on_disk, config_file, indent=2)
        temp_path.replace(CONFIG_PATH)
    except Exception as exc:
        app_log(f"Failed to persist settings: {exc}", level="warn")


def apply_settings(updates: dict) -> tuple[bool, str, dict]:
    """Validate and atomically apply a batch of live settings updates.

    On any validation failure the whole batch is rejected, so the store is
    never left half-updated. A ``tts_voice`` value is checked against the
    cached edge-tts voice list when that list is available.
    """
    coerced: dict = {}
    restart_notes: list[str] = []

    for key, value in updates.items():
        if key in RESTART_REQUIRED_KEYS:
            restart_notes.append(key)
            continue
        if key not in SETTINGS_SCHEMA:
            return False, f"Unknown setting: {key}", settings_all()
        _default, coerce = SETTINGS_SCHEMA[key]
        try:
            coerced[key] = coerce(value)
        except ValueError as exc:
            return False, str(exc), settings_all()

    if "tts_voice" in coerced:
        with voices_lock:
            known = set(cached_voice_names)
        if known and coerced["tts_voice"] not in known:
            return False, f"Unknown voice: {coerced['tts_voice']}", settings_all()

    if not coerced and not restart_notes:
        return False, "No editable settings provided", settings_all()

    with settings_lock:
        SETTINGS.update(coerced)
        snapshot = dict(SETTINGS)

    if coerced:
        persist_settings(snapshot)
        app_log("Settings updated: " + ", ".join(f"{k}={coerced[k]}" for k in sorted(coerced)))

    message = "Updated: " + ", ".join(sorted(coerced)) if coerced else "No changes applied"
    if restart_notes:
        message += f" (restart required for: {', '.join(sorted(restart_notes))})"
    return True, message, snapshot


# --------------------------------------------------------------------------- #
# Admin token
# --------------------------------------------------------------------------- #

def resolve_admin_secret() -> str:
    """Return the admin API token with zero required config editing.

    Precedence: a real ``admin_secret`` in config.json, else a cached token in
    admin_token.txt, else a freshly generated token persisted to that file. The
    token is handed only to the same-origin UI via ``/api/config``.
    """
    configured = str(CONFIG.get("admin_secret", "")).strip()
    if configured and configured.lower() != "changeme":
        return configured

    try:
        if ADMIN_TOKEN_PATH.exists():
            cached = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if cached:
                return cached
    except Exception:
        pass

    token = secrets.token_urlsafe(24)
    try:
        ADMIN_TOKEN_PATH.write_text(token, encoding="utf-8")
    except Exception as exc:
        app_log(f"Could not persist admin token: {exc}", level="warn")
    return token


ADMIN_SECRET = resolve_admin_secret()


# --------------------------------------------------------------------------- #
# Broadcaster admin state (whitelists + force-all)
# --------------------------------------------------------------------------- #

def normalize_username(user: str) -> str:
    """Normalize Twitch usernames for storage and comparison."""
    return user.lstrip("@\n ").strip().lower()


def load_admin_state() -> None:
    """Load broadcaster admin overrides from disk if the file exists."""
    global broadcaster_force_all_mode, allowed_without_command, allowed_without_cooldown, blacklisted_users
    if not ADMIN_STATE_PATH.exists():
        return
    try:
        with ADMIN_STATE_PATH.open(encoding="utf-8") as admin_file:
            data = json.load(admin_file)
    except Exception as exc:
        app_log(f"Failed to read admin state: {exc}", level="warn")
        return
    with admin_lock:
        broadcaster_force_all_mode = bool(data.get("force_all", False))
        allowed_without_command = {normalize_username(u) for u in data.get("commandless", [])}
        allowed_without_cooldown = {normalize_username(u) for u in data.get("nocooldown", [])}
        blacklisted_users = {normalize_username(u) for u in data.get("blacklist", [])}


def save_admin_state() -> None:
    """Persist broadcaster admin overrides to disk (call while holding admin_lock)."""
    state = {
        "force_all": broadcaster_force_all_mode,
        "commandless": sorted(allowed_without_command),
        "nocooldown": sorted(allowed_without_cooldown),
        "blacklist": sorted(blacklisted_users),
    }
    try:
        temp_path = ADMIN_STATE_PATH.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as admin_file:
            json.dump(state, admin_file, indent=2)
        temp_path.replace(ADMIN_STATE_PATH)
    except Exception as exc:
        app_log(f"Failed to save admin state: {exc}", level="warn")


def admin_status_snapshot() -> dict[str, object]:
    """Return a thread-safe snapshot of the current broadcaster admin state."""
    with admin_lock:
        return {
            "broadcaster_force_all_mode": broadcaster_force_all_mode,
            "allowed_without_command": sorted(allowed_without_command),
            "allowed_without_cooldown": sorted(allowed_without_cooldown),
            "blacklist": sorted(blacklisted_users),
        }


def _add_to_whitelist(group: str, user: str) -> str:
    user = normalize_username(user)
    with admin_lock:
        if group == "commandless":
            allowed_without_command.add(user)
            save_admin_state()
            return f"Allowed {user} to use TTS without the command"
        if group == "nocooldown":
            allowed_without_cooldown.add(user)
            save_admin_state()
            return f"Disabled cooldown for {user}"
        if group == "all":
            allowed_without_command.add(user)
            allowed_without_cooldown.add(user)
            save_admin_state()
            return f"Allowed {user} to bypass both command and cooldown"
        if group == "blacklist":
            blacklisted_users.add(user)
            save_admin_state()
            return f"Blacklisted {user} (messages will never be spoken)"
    return f"Unknown allow group: {group}"


def _remove_from_whitelist(group: str, user: str) -> str:
    user = normalize_username(user)
    with admin_lock:
        if group == "commandless":
            allowed_without_command.discard(user)
            save_admin_state()
            return f"Removed {user} from commandless TTS access"
        if group == "nocooldown":
            allowed_without_cooldown.discard(user)
            save_admin_state()
            return f"Enabled cooldown for {user}"
        if group == "all":
            allowed_without_command.discard(user)
            allowed_without_cooldown.discard(user)
            save_admin_state()
            return f"Removed {user} from all TTS bypass groups"
        if group == "blacklist":
            blacklisted_users.discard(user)
            save_admin_state()
            return f"Removed {user} from the blacklist"
    return f"Unknown disallow group: {group}"


def parse_admin_group(group: str) -> str:
    text = group.lower()
    if text in {"commandless", "no_prefix", "without_command", "command"}:
        return "commandless"
    if text in {"nocooldown", "no_cooldown", "cooldown_exempt"}:
        return "nocooldown"
    if text in {"all", "both", "every"}:
        return "all"
    if text in {"blacklist", "block", "ban", "banned"}:
        return "blacklist"
    return text


def process_broadcaster_control(body: str) -> tuple[bool, str]:
    """Execute a broadcaster control string; return (success, message)."""
    global broadcaster_force_all_mode

    if not body:
        return False, "Missing command; available: mode, allow, disallow, nocooldown, cooldown, status"

    parts = body.split()
    action = parts[0].lower()
    args = parts[1:]

    if action == "mode":
        if len(args) != 1 or args[0] not in {"all", "command"}:
            return False, "Usage: !ttsadmin mode all|command"
        with admin_lock:
            broadcaster_force_all_mode = args[0] == "all"
            save_admin_state()
        return True, f"Set TTS mode override to '{args[0]}'"

    if action == "enable_all":
        with admin_lock:
            broadcaster_force_all_mode = True
            save_admin_state()
        return True, "Enabled commandless TTS for everyone"

    if action == "disable_all":
        with admin_lock:
            broadcaster_force_all_mode = False
            save_admin_state()
        return True, "Disabled commandless TTS for everyone"

    if action == "shutdown":
        return True, initiate_shutdown()

    if action in {"allow", "disallow"}:
        if not args:
            return False, "Usage: !ttsadmin allow [commandless|nocooldown|all] <user>"
        if len(args) == 1:
            group, target = "commandless", args[0]
        else:
            group, target = parse_admin_group(args[0]), args[1]
        if not target:
            return False, "Missing user for whitelist command"
        if action == "allow":
            return True, _add_to_whitelist(group, target)
        return True, _remove_from_whitelist(group, target)

    if action == "nocooldown" and args:
        return True, _add_to_whitelist("nocooldown", args[0])

    if action == "cooldown" and args:
        return True, _remove_from_whitelist("nocooldown", args[0])

    if action == "status":
        snapshot = admin_status_snapshot()
        return True, (
            f"Status: force_all={snapshot['broadcaster_force_all_mode']}, "
            f"allowed_without_command={snapshot['allowed_without_command']}, "
            f"allowed_without_cooldown={snapshot['allowed_without_cooldown']}"
        )

    return False, f"Unknown command: {body}"


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #

def initiate_shutdown() -> str:
    """Trigger a graceful shutdown of the HTTP server and async event loop."""
    app_log("Shutdown requested", level="info")
    if main_loop and shutdown_event:
        main_loop.call_soon_threadsafe(shutdown_event.set)
    if httpd_server:
        try:
            httpd_server.shutdown()
        except Exception as exc:
            app_log(f"Failed to stop HTTP server: {exc}", level="warn")
    return "Shutdown sequence started"


def print_console_banner() -> None:
    """Print an interactive CLI banner to the terminal window."""
    line = "=" * 52
    print(f"\n{line}")
    print("  Twitch TTS engine")
    print(f"  Channel : #{TWITCH_CHANNEL}")
    print(f"  Overlay : http://localhost:{HTTP_PORT}/index.html")
    print(f"{line}")
    print("  Commands: status | reload | quit  (Ctrl+C also quits)")
    print("  Closing this window shuts the engine down and")
    print("  tells the overlay page to close itself.")
    print(f"{line}\n", flush=True)


def console_shutdown_listener() -> None:
    """Interactive console loop: status/reload/quit and shutdown handling."""
    print_console_banner()
    while True:
        try:
            line = input("tts> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            initiate_shutdown()
            break
        if not line:
            continue
        if line in {"shutdown", "quit", "exit", "q"}:
            initiate_shutdown()
            break
        if line in {"status", "s"}:
            settings = settings_all()
            snapshot = admin_status_snapshot()
            print(f"  mode={settings['tts_mode']} permission={settings['tts_permission']} "
                  f"cooldown={settings['tts_cooldown_seconds']:.0f}s voice={settings['tts_voice']}")
            print(f"  blacklist={snapshot['blacklist']}")
            print(f"  clients={len(connected_clients)}", flush=True)
            continue
        if line in {"reload", "r"}:
            load_admin_state()
            print("  Reloaded admin state from disk.", flush=True)
            continue
        print("  Unknown command. Try: status | reload | quit", flush=True)


# --------------------------------------------------------------------------- #
# TTS + voices
# --------------------------------------------------------------------------- #

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
    """Render TTS audio on the shared event loop when possible.

    Reusing ``main_loop`` avoids spinning up and tearing down a fresh event
    loop per request; falls back to a private loop only if the main loop is
    unavailable (e.g. during early startup).
    """
    if main_loop and main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(generate_tts_audio(text, voice), main_loop)
        return future.result(timeout=TTS_TIMEOUT_SECONDS)
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


def _cache_voices(voices: list[dict]) -> None:
    global cached_voices, cached_voice_names
    with voices_lock:
        cached_voices = voices
        cached_voice_names = {v["name"] for v in voices}


def get_cached_voices() -> list[dict]:
    with voices_lock:
        if cached_voices is not None:
            return cached_voices
    if main_loop and main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(list_voices(), main_loop)
        voices = future.result(timeout=VOICE_CACHE_TIMEOUT_SECONDS)
    else:
        voices = asyncio.run(list_voices())
    _cache_voices(voices)
    return voices


async def preload_voices() -> None:
    """Warm up the voice list cache before handling HTTP requests."""
    try:
        voices = await list_voices()
        _cache_voices(voices)
        app_log(f"Loaded {len(voices)} neural voices")
    except Exception as exc:
        app_log(f"Failed to preload voices: {exc}", level="warn")


def english_voices(voices: list[dict]) -> list[dict]:
    filtered = [v for v in voices if v["locale"].startswith("en-")]
    return filtered if filtered else voices


async def load_third_party_emotes() -> None:
    """Fetch BTTV/FFZ/7TV emote names off the event loop, best-effort."""
    global third_party_emotes
    try:
        names, info = await asyncio.to_thread(fetch_emote_names, TWITCH_CHANNEL)
        third_party_emotes = names
        app_log(f"Emote filter ready: {info}")
    except Exception as exc:
        app_log(f"Could not load third-party emotes: {exc}", level="warn")


# --------------------------------------------------------------------------- #
# SSE broadcast helpers
# --------------------------------------------------------------------------- #

def quiet_connection_errors(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


def close_writer(writer: asyncio.StreamWriter) -> None:
    if not writer.is_closing():
        writer.close()


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
        writer.write(f"data: {json.dumps(entry)}\n\n".encode("utf-8"))
    await writer.drain()


async def send_voice_list(writer: asyncio.StreamWriter) -> None:
    with voices_lock:
        voices = list(cached_voices) if cached_voices else []
    if not voices:
        return
    payload = json.dumps({"type": "voices", "voices": english_voices(voices)})
    writer.write(f"data: {payload}\n\n".encode("utf-8"))
    await writer.drain()


async def send_settings(writer: asyncio.StreamWriter) -> None:
    payload = json.dumps({"type": "settings", "settings": settings_all()})
    writer.write(f"data: {payload}\n\n".encode("utf-8"))
    await writer.drain()


async def broadcast_settings() -> None:
    """Push the current settings to all overlay clients (keeps UI in sync)."""
    await broadcast(json.dumps({"type": "settings", "settings": settings_all()}))


# --------------------------------------------------------------------------- #
# Twitch IRC parsing + permission logic
# --------------------------------------------------------------------------- #

def parse_irc_line(line: str) -> dict:
    """Parse a single IRCv3 line into tags/prefix/command/params/trailing."""
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
    """Parse the Twitch ``emotes`` tag into a flat list of inclusive (start, end)."""
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


def user_permission_level(tags: dict[str, str]) -> int:
    """Derive the highest privilege tier a chatter holds from IRC tags."""
    badges = tags.get("badges", "")
    badge_names = {b.partition("/")[0] for b in badges.split(",") if b}
    level = PERMISSION_LEVELS["everyone"]
    if tags.get("subscriber") == "1" or "subscriber" in badge_names or "founder" in badge_names:
        level = max(level, PERMISSION_LEVELS["subscriber"])
    if "vip" in badge_names:
        level = max(level, PERMISSION_LEVELS["vip"])
    if tags.get("mod") == "1" or "moderator" in badge_names:
        level = max(level, PERMISSION_LEVELS["moderator"])
    if "broadcaster" in badge_names:
        level = max(level, PERMISSION_LEVELS["broadcaster"])
    return level


def is_broadcaster(tags: dict[str, str]) -> bool:
    badge_names = {b.partition("/")[0] for b in tags.get("badges", "").split(",") if b}
    return "broadcaster" in badge_names


def is_control_command(raw_text: str, command: str) -> bool:
    text = raw_text.strip()
    if not text.lower().startswith(command):
        return False
    remainder = text[len(command):]
    return remainder == "" or remainder[0].isspace()


def handle_broadcaster_command(user: str, raw_text: str, tags: dict[str, str]) -> None:
    if not is_broadcaster(tags):
        app_log(f"{user} attempted broadcaster command but is not broadcaster", level="warn")
        return
    body = raw_text.strip()[len(BROADCASTER_CONTROL_COMMAND):].strip()
    success, message = process_broadcaster_control(body)
    app_log(message, level="info" if success else "warn")


def is_permitted(tags: dict[str, str], required_permission: str) -> bool:
    required = PERMISSION_LEVELS.get(required_permission, PERMISSION_LEVELS["everyone"])
    return user_permission_level(tags) >= required


def cooldown_remaining(user: str, cooldown_seconds: float) -> float:
    """Return seconds left on this user's cooldown, 0 if ready."""
    if cooldown_seconds <= 0:
        return 0.0
    user = normalize_username(user)
    now = time.monotonic()
    with cooldown_lock:
        last = last_request_at.get(user)
    if last is None:
        return 0.0
    remaining = cooldown_seconds - (now - last)
    return remaining if remaining > 0 else 0.0


def mark_request(user: str) -> None:
    user = normalize_username(user)
    with cooldown_lock:
        last_request_at[user] = time.monotonic()


# --------------------------------------------------------------------------- #
# Twitch listener
# --------------------------------------------------------------------------- #

async def twitch_listener() -> None:
    while True:
        try:
            await _listen_to_twitch_chat()
        except Exception as exc:
            app_log(
                f"IRC connection lost ({exc}). Reconnecting in {IRC_RECONNECT_SECONDS}s...",
                level="warn",
            )
            await asyncio.sleep(IRC_RECONNECT_SECONDS)


async def _listen_to_twitch_chat() -> None:
    reader, writer = await asyncio.open_connection("irc.chat.twitch.tv", 6667)

    writer.write(b"CAP REQ :twitch.tv/tags\r\n")
    writer.write(b"NICK justinfan12345\r\n")
    writer.write(f"JOIN #{TWITCH_CHANNEL}\r\n".encode("utf-8"))
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

        spoken = evaluate_message(parsed)
        if spoken is None:
            continue

        user, text = spoken
        await broadcast(json.dumps({"type": "chat", "user": user, "text": text}))


def evaluate_message(parsed: dict) -> tuple[str, str] | None:
    """Decide whether a chat message should be spoken.

    Returns ``(display_user, sanitized_text)`` when the message should be
    spoken, or ``None`` when it should be ignored. All gating reads the live
    settings so admin edits take effect on the very next message.
    """
    tags = parsed["tags"]
    user = parsed["prefix"].split("!")[0] or tags.get("display-name", "chat")
    normalized_user = normalize_username(user)
    raw_text = parsed["trailing"]
    emote_ranges = parse_emote_ranges(tags.get("emotes", ""))

    # Snapshot live settings once per message.
    settings = settings_all()
    mode = settings["tts_mode"]
    command = settings["tts_command"]
    permission = settings["tts_permission"]
    cooldown_seconds = settings["tts_cooldown_seconds"]
    max_chars = settings["max_tts_chars"]

    # Broadcaster control command is handled inline and never spoken.
    if is_control_command(raw_text, BROADCASTER_CONTROL_COMMAND) and is_broadcaster(tags):
        handle_broadcaster_command(user, raw_text, tags)
        return None

    with admin_lock:
        force_all_mode = broadcaster_force_all_mode
        allowed_commandless = normalized_user in allowed_without_command
        allowed_nocooldown = normalized_user in allowed_without_cooldown
        is_blacklisted = normalized_user in blacklisted_users

    # Blacklisted users are silenced regardless of mode or permission.
    if is_blacklisted:
        return None

    if mode == "command":
        if is_control_command(raw_text, command):
            leading_ws = len(raw_text) - len(raw_text.lstrip())
            body = raw_text[leading_ws:]
            first_word, sep, remainder = body.partition(" ")
            if first_word.lower() != command:
                return None

            content_offset = leading_ws + len(first_word) + len(sep)

            if not is_permitted(tags, permission):
                app_log(f"{user} used {command} but lacks '{permission}' permission", level="warn")
                return None

            if not allowed_nocooldown:
                remaining = cooldown_remaining(normalized_user, cooldown_seconds)
                if remaining > 0:
                    app_log(f"{user} on cooldown ({remaining:.0f}s left)", level="warn")
                    return None

            command_emote_ranges = shift_emote_ranges(emote_ranges, content_offset)
            text = sanitize_chat_text(remainder, command_emote_ranges, third_party_emotes)
            if not text or len(text) > max_chars:
                return None

            if not allowed_nocooldown:
                mark_request(normalized_user)
            return user, text

        if force_all_mode or allowed_commandless:
            if raw_text.startswith("!"):
                return None
            text = sanitize_chat_text(raw_text, emote_ranges, third_party_emotes)
            if not text or len(text) > max_chars:
                return None
            return user, text

        return None

    # "all" mode: speak every message, ignoring raw commands and empty text.
    text = sanitize_chat_text(raw_text, emote_ranges, third_party_emotes)
    if not text or raw_text.startswith("!") or len(text) > max_chars:
        return None
    return user, text


# --------------------------------------------------------------------------- #
# SSE client handling
# --------------------------------------------------------------------------- #

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
        await send_settings(writer)
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
        await asyncio.sleep(SSE_HEARTBEAT_SECONDS)
        await _write_to_clients(b": keepalive\n\n")


# --------------------------------------------------------------------------- #
# HTTP server (static files + JSON/audio API)
# --------------------------------------------------------------------------- #

STATIC_FILES = {"/", "/index.html", "/styles.css", "/app.js"}


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

        # -- helpers ----------------------------------------------------- #

        def _send_json(self, obj: dict, status: int = 200, cors: bool = False) -> None:
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _token_ok(self, token: str) -> bool:
            return not ADMIN_SECRET or secrets.compare_digest(token, ADMIN_SECRET)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        # -- routing ----------------------------------------------------- #

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            routes = {
                "/api/admin": lambda: self._handle_admin_request(parsed),
                "/api/tts": lambda: self._handle_tts_request(parsed),
                "/api/voices": self._handle_voices_request,
                "/api/config": self._handle_config_request,
                "/api/settings": lambda: self._handle_settings_get(parsed),
            }
            handler = routes.get(parsed.path)
            if handler:
                handler()
                return
            if parsed.path in STATIC_FILES:
                self.path = parsed.path
                super().do_GET()
                return
            self.send_error(404, "Not found")

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/settings":
                self._handle_settings_post(parsed)
                return
            self.send_error(404, "Not found")

        # -- settings API ------------------------------------------------ #

        def _handle_settings_get(self, parsed):
            params = urllib.parse.parse_qs(parsed.query)
            token = params.get("token", [""])[0]
            if not self._token_ok(token):
                self.send_error(403, "Invalid admin token")
                return
            self._send_json({
                "success": True,
                "settings": settings_all(),
                "restart_required_keys": sorted(RESTART_REQUIRED_KEYS),
                "channel": TWITCH_CHANNEL,
            })

        def _handle_settings_post(self, parsed):
            body = self._read_body()
            token = str(body.get("token", ""))
            if not self._token_ok(token):
                self.send_error(403, "Invalid admin token")
                return
            updates = body.get("settings")
            if not isinstance(updates, dict):
                self.send_error(400, "Missing settings object")
                return
            success, message, snapshot = apply_settings(updates)
            if success and main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(broadcast_settings(), main_loop)
            self._send_json(
                {"success": success, "message": message, "settings": snapshot},
                status=200 if success else 400,
            )

        # -- admin API --------------------------------------------------- #

        def _handle_admin_request(self, parsed):
            params = urllib.parse.parse_qs(parsed.query)
            action = params.get("action", [""])[0].lower()
            target_user = params.get("user", [""])[0]
            group = params.get("group", [""])[0]
            value = params.get("value", [""])[0]
            token = params.get("token", [""])[0]

            if not self._token_ok(token):
                self.send_error(403, "Invalid admin token")
                return

            if action == "status":
                self._send_json({"success": True, "message": "Admin status", "status": admin_status_snapshot()})
                return

            if not action:
                self.send_error(400, "Missing admin action")
                return

            if action == "shutdown":
                self._send_json({"success": True, "message": initiate_shutdown()})
                return

            if action == "mode":
                body = f"mode {value}"
            elif action in {"enable_all", "disable_all"}:
                body = action
            elif action in {"allow", "disallow"}:
                if not target_user:
                    self.send_error(400, "Missing user")
                    return
                body = f"{action} {group or 'commandless'} {target_user}"
            elif action in {"nocooldown", "cooldown"}:
                if not target_user:
                    self.send_error(400, "Missing user")
                    return
                body = f"{action} {target_user}"
            else:
                self.send_error(400, "Invalid admin action")
                return

            success, message = process_broadcaster_control(body)
            self._send_json({"success": success, "message": message}, status=200 if success else 400)

        # -- config (token handoff) ------------------------------------- #

        def _handle_config_request(self):
            # Hand the admin token to the same-origin UI only (no CORS header).
            self._send_json({"admin_token": ADMIN_SECRET or ""})

        # -- voices ------------------------------------------------------ #

        def _handle_voices_request(self):
            try:
                voices = get_cached_voices()
            except Exception as exc:
                app_log(f"Failed to load voices: {exc}", level="error")
                self.send_error(500, "Failed to load voices")
                return
            self._send_json_raw(json.dumps(voices).encode("utf-8"))

        def _send_json_raw(self, payload: bytes):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        # -- TTS audio --------------------------------------------------- #

        def _handle_tts_request(self, parsed):
            params = urllib.parse.parse_qs(parsed.query)
            text = sanitize_chat_text(params.get("text", [""])[0])
            voice = params.get("voice", [""])[0] or settings_get("tts_voice")
            max_chars = settings_get("max_tts_chars")

            if not text:
                self.send_error(400, "Missing or empty text parameter")
                return
            if len(text) > max_chars:
                self.send_error(400, "Text too long")
                return

            with voices_lock:
                known = set(cached_voice_names)
            if known and voice not in known:
                self.send_error(400, "Unknown voice")
                return

            try:
                future = tts_executor.submit(generate_tts_audio_sync, text, voice)
                audio = future.result(timeout=TTS_TIMEOUT_SECONDS)
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
        global httpd_server
        with socketserver.TCPServer(("127.0.0.1", HTTP_PORT), Handler) as httpd:
            httpd_server = httpd
            app_log(f"Web interface serving at http://localhost:{HTTP_PORT}")
            app_log(f"Serving files from {web_root}")
            httpd.serve_forever()
    except OSError as exc:
        app_log(
            f"Could not start web server on port {HTTP_PORT} ({exc}). "
            "Close any old TTS Engine window and try again.",
            level="error",
        )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

async def main() -> None:
    global main_loop, shutdown_event
    main_loop = asyncio.get_running_loop()
    main_loop.set_exception_handler(quiet_connection_errors)
    shutdown_event = asyncio.Event()

    load_admin_state()
    app_log("TTS engine starting...")
    app_log("Admin API secured; token auto-loaded by the local admin panel.")

    settings = settings_all()
    if settings["tts_mode"] == "command":
        cooldown = settings["tts_cooldown_seconds"]
        note = f", {cooldown:.0f}s cooldown" if cooldown > 0 else ""
        app_log(
            f"TTS mode: command ('{settings['tts_command']} <message>', "
            f"permission: {settings['tts_permission']}{note})"
        )
    else:
        app_log("TTS mode: all (every chat message is spoken)")

    threading.Thread(target=start_http_server, daemon=True).start()
    threading.Thread(target=console_shutdown_listener, daemon=True).start()
    await load_third_party_emotes()

    server = await asyncio.start_server(handle_client, "127.0.0.1", STREAM_PORT)
    app_log(f"Chat stream serving at http://localhost:{STREAM_PORT}")

    await preload_voices()
    listener_task = asyncio.create_task(twitch_listener())
    heartbeat_task = asyncio.create_task(sse_heartbeat())
    serve_task = asyncio.create_task(server.serve_forever())

    await shutdown_event.wait()
    app_log("Shutting down async services...", level="info")

    # Tell overlay pages to close/blank themselves before we drop the stream.
    try:
        await broadcast(json.dumps({"type": "shutdown"}))
        await asyncio.sleep(0.2)
    except Exception:
        pass

    for task in (listener_task, heartbeat_task, serve_task):
        task.cancel()

    server.close()
    await server.wait_closed()
    await asyncio.gather(listener_task, heartbeat_task, serve_task, return_exceptions=True)

    for client in list(connected_clients):
        close_writer(client)
        connected_clients.discard(client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        app_log("Process terminated.")
    finally:
        tts_executor.shutdown(wait=False)
        # The console listener thread may be blocked in input(); on Windows that
        # can stall interpreter exit until the user presses Enter. Force-exit so
        # a browser-triggered shutdown closes the terminal window immediately.
        print("Engine stopped.", flush=True)
        os._exit(0)
