import re

"""Sanitize chat text by stripping URLs, emojis, Twitch/BTTV/FFZ/7TV emotes,
and extra whitespace before it is spoken aloud."""

URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]+",
    flags=re.UNICODE,
)
WHITESPACE_PATTERN = re.compile(r"\s+")


def strip_twitch_emotes(text: str, emote_ranges: list[tuple[int, int]]) -> str:
    """Remove first-party Twitch emotes using IRC emote character ranges.

    ``emote_ranges`` are inclusive (start, end) indices into the ORIGINAL
    message string, as provided by the Twitch IRC ``emotes`` tag. Twitch
    indexes by Unicode code point, which matches Python string indexing.
    """
    if not emote_ranges:
        return text

    keep = [True] * len(text)
    for start, end in emote_ranges:
        for i in range(max(0, start), min(len(text), end + 1)):
            keep[i] = False
    return "".join(ch for ch, k in zip(text, keep) if k)


def shift_emote_ranges(
    emote_ranges: list[tuple[int, int]], offset: int
) -> list[tuple[int, int]]:
    """Re-base Twitch emote ranges after slicing ``offset`` chars off the front.

    Twitch emote indices point into the ORIGINAL message. When we speak only
    the part after a command prefix (e.g. ``!tts ``), the substring starts at
    ``offset``, so each range must be shifted left by that many characters.
    Ranges that fall entirely inside the removed prefix are dropped; ranges
    that straddle the boundary are clamped to 0.
    """
    shifted: list[tuple[int, int]] = []
    for start, end in emote_ranges:
        if end < offset:
            continue
        shifted.append((max(0, start - offset), end - offset))
    return shifted


def strip_named_emotes(text: str, emote_names: set[str] | None) -> str:
    """Remove whole words that match known BTTV/FFZ/7TV emote names."""
    if not emote_names:
        return text
    words = text.split(" ")
    kept = [w for w in words if w and w not in emote_names]
    return " ".join(kept)


def sanitize_chat_text(
    text: str,
    emote_ranges: list[tuple[int, int]] | None = None,
    emote_names: set[str] | None = None,
) -> str:
    """Remove unsafe or noisy text from Twitch chat messages.

    Order matters: strip first-party Twitch emotes by character range FIRST
    (while indices still line up with the original text), then URLs, unicode
    emoji, named third-party emotes, and finally collapse whitespace.
    """
    cleaned = strip_twitch_emotes(text, emote_ranges or [])
    cleaned = URL_PATTERN.sub("", cleaned)
    cleaned = EMOJI_PATTERN.sub("", cleaned)
    cleaned = strip_named_emotes(cleaned, emote_names)
    cleaned = WHITESPACE_PATTERN.sub(" ", cleaned).strip()
    return cleaned
