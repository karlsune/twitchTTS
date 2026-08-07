"""Fetch third-party emote names (BTTV, FFZ, 7TV) for a Twitch channel.

All endpoints used here are public and require no authentication. The channel
user ID is resolved via the ivr.fi public helper API so no Twitch client
credentials are needed.
"""

import json
import urllib.request

TIMEOUT = 10


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "twitchTTS/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "twitchTTS/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8").strip()


def resolve_user_id(channel: str) -> str | None:
    """Resolve a Twitch login name to its numeric user ID (no auth).

    Tries ivr.fi first, then falls back to decapi.
    """
    login = channel.lower()
    try:
        data = _get_json(f"https://api.ivr.fi/v2/twitch/user?login={login}")
        if isinstance(data, list) and data and data[0].get("id"):
            return str(data[0]["id"])
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
    except Exception:
        pass
    try:
        text = _get_text(f"https://decapi.me/twitch/id/{login}")
        if text.isdigit():
            return text
    except Exception:
        pass
    return None


def _bttv_names(user_id: str | None) -> set[str]:
    names: set[str] = set()
    try:
        glob = _get_json("https://api.betterttv.net/3/cached/emotes/global")
        for e in glob:
            if e.get("code"):
                names.add(e["code"])
    except Exception:
        pass
    if user_id:
        try:
            ch = _get_json(f"https://api.betterttv.net/3/cached/users/twitch/{user_id}")
            for key in ("channelEmotes", "sharedEmotes"):
                for e in ch.get(key, []) or []:
                    if e.get("code"):
                        names.add(e["code"])
        except Exception:
            pass
    return names


def _ffz_names(channel: str) -> set[str]:
    names: set[str] = set()
    try:
        glob = _get_json("https://api.frankerfacez.com/v1/set/global")
        for _sid, s in (glob.get("sets") or {}).items():
            for e in s.get("emoticons", []) or []:
                if e.get("name"):
                    names.add(e["name"])
    except Exception:
        pass
    try:
        room = _get_json(f"https://api.frankerfacez.com/v1/room/{channel.lower()}")
        for _sid, s in (room.get("sets") or {}).items():
            for e in s.get("emoticons", []) or []:
                if e.get("name"):
                    names.add(e["name"])
    except Exception:
        pass
    return names


def _seventv_names(user_id: str | None) -> set[str]:
    names: set[str] = set()
    try:
        glob = _get_json("https://7tv.io/v3/emote-sets/global")
        for e in glob.get("emotes", []) or []:
            if e.get("name"):
                names.add(e["name"])
    except Exception:
        pass
    if user_id:
        try:
            user = _get_json(f"https://7tv.io/v3/users/twitch/{user_id}")
            emote_set = (user.get("emote_set") or {})
            for e in emote_set.get("emotes", []) or []:
                if e.get("name"):
                    names.add(e["name"])
        except Exception:
            pass
    return names


def fetch_emote_names(channel: str):
    """Return (emote_name_set, info_string) for logging.

    Never raises; on total failure returns an empty set so TTS still runs.
    """
    user_id = resolve_user_id(channel)
    names: set[str] = set()
    names |= _bttv_names(user_id)
    names |= _ffz_names(channel)
    names |= _seventv_names(user_id)
    info = (
        f"user_id={user_id or 'unknown'}, "
        f"loaded {len(names)} BTTV/FFZ/7TV emote names"
    )
    return names, info
