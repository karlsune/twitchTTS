"""Play synthesized MP3 bytes on the host machine with pygame.

``pygame-ce`` (imported as ``pygame``) is used so the engine is
self-contained — no external player binary is required. pygame's
``Sound()`` cannot decode MP3 from a buffer (it plays static noise), so
MP3 bytes are decoded with ``miniaudio`` and re-wrapped as WAV before
being handed to pygame. If pygame is missing or no audio device is
available, :func:`init_player` returns ``None`` and the application falls
back to browser-based playback.
"""

from __future__ import annotations

import array
import io
import os
import queue
import threading
import time
import wave

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import miniaudio  # noqa: E402
import pygame  # noqa: E402

MIXER_FREQUENCY = 44100
MIXER_SIZE = -16
MIXER_CHANNELS = 1

# pygame's mixer emits a short noise burst (~40-50 ms, near full scale)
# whenever a Channel starts playing, regardless of the buffer contents
# (measured: an all-silence WAV still "plays" a 0.7-amplitude burst at
# start). The channel is kept at volume 0 for this window and then faded
# in, so the burst never reaches the speakers. TTS clips start with
# 200-400 ms of leading silence, so the guard cuts nothing audible.
BURST_GUARD_MS = 80


def _apply_fades(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """Fade the first/last 5 ms of PCM to silence.

    Decoded MP3 streams often start with a tiny discontinuity (encoder delay
    / padding) that pygame plays back as an audible click or pop at the
    start of each message. A 5 ms linear ramp is inaudible on speech but
    removes the click. Same treatment at the end avoids end-of-message pops.
    """
    fade_frames = max(1, int(sample_rate * 0.005))
    if isinstance(pcm, array.array):
        data = pcm  # miniaudio already returns a signed-16 array
    else:
        data = array.array("h")
        data.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n_frames = len(data) // channels
    if n_frames == 0:
        return pcm

    for frame in range(min(fade_frames, n_frames)):
        gain = frame / fade_frames
        for c in range(channels):
            data[frame * channels + c] = int(data[frame * channels + c] * gain)

    for frame in range(max(0, n_frames - fade_frames), n_frames):
        gain = (n_frames - 1 - frame) / fade_frames
        for c in range(channels):
            data[frame * channels + c] = int(data[frame * channels + c] * gain)

    return data.tobytes()


def _mp3_to_wav(mp3_bytes: bytes) -> bytes:
    """Decode MP3 bytes and wrap the PCM in a WAV container.

    pygame's ``Sound(buffer=...)`` decodes WAV reliably but turns MP3
    buffers into loud static noise, so host audio is converted here first.
    A short fade-in/out is applied to avoid clicks at playback boundaries.
    """
    decoded = miniaudio.decode(
        mp3_bytes, output_format=miniaudio.SampleFormat.SIGNED16
    )
    samples = _apply_fades(decoded.samples, decoded.sample_rate, decoded.nchannels)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(decoded.nchannels)
        wav.setsampwidth(2)
        wav.setframerate(decoded.sample_rate)
        wav.writeframes(samples)
    return buf.getvalue()


class AudioPlayer:
    """Sequential MP3 player backed by pygame.mixer.

    Playback runs on a dedicated worker thread so callers can hand over MP3
    bytes without blocking. Volume and mute changes apply live to whatever
    sound is currently playing.
    """

    def __init__(
        self,
        volume: float = 0.8,
        muted: bool = False,
        queue_size: int = 50,
    ) -> None:
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._volume = max(0.0, min(1.0, volume))
        self._muted = bool(muted)
        self._current_sound: pygame.mixer.Sound | None = None
        self._current_channel: pygame.mixer.Channel | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin the playback worker thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker, name="audio-player", daemon=True
        )
        self._thread.start()

    def play(self, mp3_bytes: bytes) -> bool:
        """Queue MP3 bytes for playback; returns False when the queue is full."""
        try:
            self._queue.put_nowait(mp3_bytes)
            return True
        except queue.Full:
            return False

    def skip(self) -> None:
        """Stop the sound that is currently playing."""
        if self._current_channel is not None:
            self._current_channel.stop()

    def clear(self) -> None:
        """Stop playback and drop any queued sounds."""
        self.skip()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def set_volume(self, volume: float) -> None:
        """Set the playback volume (0.0–1.0), applied live."""
        self._volume = max(0.0, min(1.0, volume))
        self._apply_volume()

    def set_muted(self, muted: bool) -> None:
        """Mute or unmute the currently playing and future sounds."""
        self._muted = bool(muted)
        self._apply_volume()

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def pending(self) -> int:
        """Number of MP3 blobs waiting in the playback queue."""
        return self._queue.qsize()

    def shutdown(self) -> None:
        """Drop queued audio, stop playback, and release the mixer."""
        self.clear()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        pygame.mixer.quit()

    def _apply_volume(self) -> None:
        effective = 0.0 if self._muted else self._volume
        if self._current_sound is not None:
            self._current_sound.set_volume(effective)

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                mp3_bytes = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue

            try:
                sound = pygame.mixer.Sound(buffer=_mp3_to_wav(mp3_bytes))
            except (pygame.error, miniaudio.MiniaudioError, OSError):
                # Un-decodable or truncated MP3 from a failed synthesis; drop it.
                continue

            self._current_sound = sound
            sound.set_volume(0.0 if self._muted else self._volume)
            self._current_channel = sound.play()

            if self._current_channel is not None:
                # Swallow pygame's start-of-playback noise burst.
                self._current_channel.set_volume(0.0)
                threading.Thread(target=self._ramp_channel, daemon=True).start()
                while self._current_channel.get_busy():
                    if self._stop_event.wait(0.05):
                        self._current_channel.stop()
                        break

            self._current_sound = None
            self._current_channel = None

    def _ramp_channel(self) -> None:
        """Restore the channel volume after the burst-guard window."""
        time.sleep(BURST_GUARD_MS / 1000.0)
        try:
            if self._current_channel is not None:
                self._current_channel.set_volume(1.0)
        except pygame.error:
            pass


def init_player(
    volume: float = 0.8,
    muted: bool = False,
    queue_size: int = 50,
) -> AudioPlayer | None:
    """Create and start an :class:`AudioPlayer`, or ``None`` when unavailable."""
    try:
        pygame.mixer.pre_init(
            frequency=MIXER_FREQUENCY, size=MIXER_SIZE, channels=MIXER_CHANNELS
        )
        pygame.mixer.init()
    except (pygame.error, OSError):
        return None
    player = AudioPlayer(volume=volume, muted=muted, queue_size=queue_size)
    player.start()
    return player
