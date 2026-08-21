"""Standalone tray shell for the Twitch TTS Engine (Windows primary).

Runs the engine (``app.py``) as a local subprocess and provides:

* a system tray icon whose color reflects engine state
  (green = ok, red = error, grey = offline) with an
  Open / Mute / About / Exit menu;
* a simple native window (mute, skip, voice, volume, now playing, log);
* minimize-to-tray: closing the window keeps the engine running.

When frozen with PyInstaller the same executable re-runs itself with
``--engine`` to host the engine.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from tkinter import messagebox, ttk

from config import get_config_path

VERSION = "0.2.0"

MODE_LABELS = {"Neural (edge-tts)": "edge", "System (offline)": "system"}
MODE_KEYS = {key: label for label, key in MODE_LABELS.items()}
# Next to the executable when frozen (PyInstaller), next to this file otherwise.
BASE_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)

COLORS = {
    "ok": (46, 160, 67),
    "error": (200, 50, 50),
    "offline": (140, 140, 140),
}


def _read_config() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def engine_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--engine"]
    return [sys.executable, os.path.join(BASE_DIR, "app.py")]


class TwitchTTSShell:
    def __init__(self) -> None:
        self.engine: subprocess.Popen | None = None
        self.state = "offline"
        self.muted = False
        self.close_to_tray = True
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.port = 8080
        self.stream_port = 8081
        self._stop = threading.Event()
        self._voice_by_label: dict[str, str] = {}

        self._build_window()
        self._build_tray()
        self._start_engine()
        threading.Thread(target=self._poll_status, daemon=True).start()
        threading.Thread(target=self._sse_reader, daemon=True).start()
        self.root.after(100, self._drain_log_queue)

    # ------------------------------------------------------------------ tray
    def _build_tray(self) -> None:
        import pystray  # GUI dep; only needed in shell mode (not --engine)

        menu = pystray.Menu(
            pystray.MenuItem("Open", self.open_window, default=True),
            pystray.MenuItem("Mute", self.toggle_mute, checked=lambda item: self.muted),
            pystray.MenuItem("About", self.about),
            pystray.MenuItem("Exit", self.exit_app),
        )
        self.tray = pystray.Icon(
            "twitchtts", self._tray_image("offline"), "Twitch TTS Engine", menu
        )
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _tray_image(self, state: str, muted: bool = False) -> Image.Image:
        from PIL import Image, ImageDraw

        color = COLORS.get(state, COLORS["offline"])
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=color, outline=(30, 30, 30), width=3)
        d.polygon([(24, 25), (34, 25), (46, 15), (46, 49), (34, 39), (24, 39)], fill=(255, 255, 255))
        if muted:
            d.line((12, 12, 52, 52), fill=(220, 40, 40), width=7)
        return img

    def _refresh_tray(self) -> None:
        suffix = " — muted" if self.muted else ""
        self.tray.icon = self._tray_image(self.state, self.muted)
        self.tray.title = f"Twitch TTS Engine — {self.state}{suffix}"
        self.tray.update_menu()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._refresh_tray()

    # ---------------------------------------------------------------- window
    def _build_window(self) -> None:
        self.root = tk.Tk()
        self.root.title("Twitch TTS Engine")
        self.root.geometry("560x440")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        pad = {"padx": 8, "pady": 4}
        frame = ttk.Frame(self.root, padding=8)
        frame.pack(fill="both", expand=True)

        controls = ttk.Frame(frame)
        controls.pack(fill="x", **pad)
        self.mute_btn = ttk.Button(controls, text="Mute", command=self.toggle_mute, width=8)
        self.mute_btn.pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="Skip", command=self.skip, width=8).pack(side="left", padx=(0, 12))

        ttk.Label(controls, text="Voice:").pack(side="left")
        self.voice_var = tk.StringVar()
        self.voice_box = ttk.Combobox(
            controls, textvariable=self.voice_var, state="readonly", width=28
        )
        self.voice_box.pack(side="left", padx=6)
        self.voice_box.bind("<<ComboboxSelected>>", self._voice_selected)

        ttk.Label(controls, text="Engine:").pack(side="left")
        self.mode_var = tk.StringVar()
        self.mode_box = ttk.Combobox(
            controls,
            textvariable=self.mode_var,
            state="readonly",
            width=17,
            values=list(MODE_LABELS.keys()),
        )
        self.mode_box.pack(side="left", padx=6)
        self.mode_box.bind("<<ComboboxSelected>>", self._mode_selected)

        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, **pad)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        now = ttk.Frame(left)
        now.pack(fill="x")
        ttk.Label(now, text="Now Playing", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.now_label = ttk.Label(now, text="—", wraplength=440, justify="left")
        self.now_label.pack(anchor="w", pady=(2, 0))
        self.queue_label = ttk.Label(now, text="", foreground="#666666")
        self.queue_label.pack(anchor="w")

        log_frame = ttk.Frame(left)
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scroll.set)

        right = ttk.Frame(body)
        right.pack(side="right", fill="y", padx=(10, 0))
        ttk.Label(right, text="Volume").pack()
        self.volume_var = tk.DoubleVar(value=80)
        self.volume_scale = ttk.Scale(
            right,
            from_=100,
            to=0,
            orient="vertical",
            variable=self.volume_var,
            command=self._volume_drag,
        )
        self.volume_scale.pack(fill="y", expand=True)
        self.volume_label = ttk.Label(right, text="80%", width=5)
        self.volume_label.pack(pady=(2, 0))

        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(bottom, text="About", command=self.about, width=8).pack(side="left")
        self.status_label = ttk.Label(bottom, text="● offline", foreground="#8c8c8c")
        self.status_label.pack(side="left", padx=(8, 0))
        ttk.Button(bottom, text="Options...", command=self._open_options, width=10).pack(side="right")

    def hide_window(self) -> None:
        self.root.withdraw()

    def open_window(self, icon=None, item=None) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _log(self, line: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _drain_log_queue(self) -> None:
        while True:
            try:
                self._log(self.log_queue.get_nowait())
            except queue.Empty:
                break
        self.root.after(100, self._drain_log_queue)

    # ---------------------------------------------------------------- engine
    def _start_engine(self) -> None:
        self._log("Starting engine...")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
        self.engine = subprocess.Popen(
            engine_command(),
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        threading.Thread(target=self._engine_reader, daemon=True).start()
        # Re-read config: the engine creates config.json from the example.
        cfg = _read_config()
        self.port = int(cfg.get("http_port", 8080))
        self.stream_port = int(cfg.get("stream_port", 8081))
        self.close_to_tray = bool(cfg.get("close_to_tray", True))
        self._apply_close_behavior()
        threading.Thread(target=self._load_voices, daemon=True).start()

    def _apply_close_behavior(self) -> None:
        """Close button minimizes to tray or exits the whole app, per config."""
        if self.close_to_tray:
            self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        else:
            self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

    def _engine_reader(self) -> None:
        assert self.engine is not None and self.engine.stdout is not None
        for line in self.engine.stdout:
            stripped = line.rstrip()
            # app_log() lines arrive over SSE too; only surface unexpected
            # stdout here (tracebacks, warnings) to avoid double logging.
            if re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", stripped):
                continue
            self.log_queue.put(stripped)
        self.log_queue.put("[engine exited]")

    def _api(self, path: str, timeout: float = 4.0) -> dict | None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=timeout
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None

    def _post(self, path: str, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}{path}", data=data,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=4.0).read()
        except (OSError, urllib.error.URLError):
            pass

    # ------------------------------------------------------------- statusing
    def _poll_status(self) -> None:
        while not self._stop.is_set():
            if self.engine is not None and self.engine.poll() is not None:
                self._set_state("offline")
                self.root.after(0, lambda: self.status_label.config(text="● offline", foreground="#8c8c8c"))
            else:
                status = self._api("/api/status")
                if status is None:
                    self._set_state("offline")
                    self.root.after(0, lambda: self.status_label.config(text="● offline", foreground="#8c8c8c"))
                else:
                    state = status.get("status", "error")
                    self._set_state(state)
                    text = {
                        "ok": "● engine running",
                        "error": "● error — check log",
                    }.get(state, "● offline")
                    color = {"ok": "#2ea043", "error": "#c83232"}.get(state, "#8c8c8c")
                    self.root.after(0, lambda t=text, c=color: self.status_label.config(text=t, foreground=c))
            self._stop.wait(2.0)

    def _sse_reader(self) -> None:
        url = f"http://127.0.0.1:{self.stream_port}/"
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        self._handle_event(evt)
            except (OSError, urllib.error.URLError):
                self._stop.wait(3.0)

    def _handle_event(self, evt: dict) -> None:
        etype = evt.get("type")
        if etype == "log":
            self.log_queue.put(f"[{evt.get('time', '')}] {evt.get('message', '')}")
        elif etype == "voice":
            voice = evt.get("voice")
            mode = evt.get("mode")
            if mode:
                self.root.after(0, lambda m=mode: self._set_mode_var(m))
                self.root.after(0, self._load_voices)
            if voice:
                self.root.after(0, lambda v=voice: self._select_voice(v))
        elif etype == "now_playing":
            self.root.after(0, lambda: self.now_label.config(text=f"{evt.get('user', '')}: {evt.get('text', '')}"))
        elif etype == "control":
            self.root.after(0, self._apply_control_event, evt)

    def _apply_control_event(self, evt: dict) -> None:
        if "queue" in evt:
            self.queue_label.config(text=f"queue: {evt['queue']}")
        if "volume" in evt:
            self.volume_var.set(int(float(evt["volume"]) * 100))
            self.volume_label.config(text=f"{int(float(evt['volume']) * 100)}%")
        if "muted" in evt:
            self.muted = bool(evt["muted"])
            self.mute_btn.config(text="Unmute" if self.muted else "Mute")
            self._refresh_tray()

    # -------------------------------------------------------------- controls
    def toggle_mute(self, icon=None, item=None) -> None:
        # Ask the engine to flip state; update locally optimistically and let
        # the SSE control events re-sync if the request failed.
        self._post("/api/control", {"action": "toggle_mute"})
        self.muted = not self.muted
        self.mute_btn.config(text="Unmute" if self.muted else "Mute")
        self._refresh_tray()

    def skip(self) -> None:
        self._post("/api/control", {"action": "skip"})

    def _voice_selected(self, _event=None) -> None:
        # The combobox shows labels like "en-US-JennyNeural (Female, en-US)";
        # the engine expects the ShortName.
        label = self.voice_var.get()
        voice = self._voice_by_label.get(label, label)
        self._post("/api/voice", {"voice": voice})

    def _set_mode_var(self, key: str) -> None:
        self.mode_var.set(MODE_KEYS.get(key, "Neural (edge-tts)"))

    def _mode_selected(self, _event=None) -> None:
        key = MODE_LABELS.get(self.mode_var.get(), "edge")
        self._post("/api/voice", {"mode": key})
        # Engine switched mode; reload the matching voice list.
        self.root.after(300, self._load_voices)

    def _volume_drag(self, _value=None) -> None:
        self.volume_label.config(text=f"{int(self.volume_var.get())}%")

    def _volume_released(self, _event=None) -> None:
        self._post("/api/control", {"action": "volume", "value": self.volume_var.get() / 100.0})

    def _load_voices(self) -> None:
        data = self._api("/api/voices")
        if not data:
            return
        names = [
            v.get("ShortName") or v.get("Name")
            for v in data
            if isinstance(v, dict) and (v.get("ShortName") or v.get("Name"))
        ]
        if names:
            self.root.after(0, lambda: self.voice_box.config(values=names))

    def _load_voices(self) -> None:
        data = self._api("/api/voices")
        if not data:
            return
        options = []
        by_label: dict[str, str] = {}
        for v in data:
            if not isinstance(v, dict):
                continue
            # /api/voices returns {name, gender, locale, label}.
            name = v.get("name") or v.get("ShortName") or v.get("Name")
            if not name:
                continue
            label = v.get("label") or name
            by_label[label] = name
            options.append(label)
        if not options:
            return
        self._voice_by_label = by_label
        self.root.after(0, lambda: self.voice_box.config(values=options))
        # Preselect the engine's current voice.
        cfg = self._api("/api/config")
        if cfg:
            current = cfg.get("tts_voice")
            if current:
                self.root.after(0, lambda c=current: self._select_voice(c))
            self.root.after(0, lambda m=cfg.get("tts_mode", "edge"): self._set_mode_var(m))

    def _select_voice(self, name: str) -> None:
        for label, voice in self._voice_by_label.items():
            if voice == name:
                self.voice_var.set(label)
                return
        self.voice_var.set(name)

    def _restart_engine(self) -> None:
        """Stop the engine subprocess and start it again (picks up config)."""
        if self.engine is not None and self.engine.poll() is None:
            self.engine.terminate()
            try:
                self.engine.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.engine.kill()
        self._log("Restarting engine...")
        self._start_engine()

    def _open_options(self) -> None:
        """Options window: edit config.json from the GUI instead of by hand."""
        self.root.deiconify()  # dialogs need a viewable parent (tray-only case)
        cfg = self._api("/api/config") or {}
        voices = self._api("/api/voices") or []

        win = tk.Toplevel(self.root)
        win.title("Twitch TTS Options")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        rows = []

        def add_row(label: str, widget: tk.Widget) -> None:
            rows.append((label, widget))

        channel_var = tk.StringVar(value=cfg.get("twitch_channel", ""))
        add_row("Twitch channel", ttk.Entry(frame, textvariable=channel_var, width=28))

        voice_var = tk.StringVar(value=cfg.get("tts_voice", ""))
        voice_box = ttk.Combobox(frame, textvariable=voice_var, state="readonly", width=28)
        by_label: dict[str, str] = {}
        voice_options = []
        for v in voices:
            if not isinstance(v, dict):
                continue
            name = v.get("name") or v.get("ShortName")
            if not name:
                continue
            label = v.get("label") or name
            by_label[label] = name
            voice_options.append(label)
        voice_box.config(values=voice_options)
        for label, name in by_label.items():
            if name == voice_var.get():
                voice_var.set(label)
        add_row("Neural voice", voice_box)

        prefix_var = tk.StringVar(value=cfg.get("command_prefix", "!tts"))
        add_row("Command prefix", ttk.Entry(frame, textvariable=prefix_var, width=28))

        cooldown_var = tk.StringVar(value=str(cfg.get("cooldown_seconds", 3)))
        add_row("Cooldown (seconds)", ttk.Spinbox(frame, from_=0, to=120, textvariable=cooldown_var, width=8))

        maxchars_var = tk.StringVar(value=str(cfg.get("max_chars", 200)))
        add_row("Max message length", ttk.Spinbox(frame, from_=10, to=500, textvariable=maxchars_var, width=8))

        special_var = tk.StringVar(value=", ".join(cfg.get("special_users", [])))
        add_row("Special users (comma separated)", ttk.Entry(frame, textvariable=special_var, width=28))

        audio = cfg.get("audio") if isinstance(cfg.get("audio"), dict) else {}
        volume_var = tk.DoubleVar(value=float(audio.get("volume", 0.8)) * 100)
        volume_scale = ttk.Scale(frame, from_=0, to=100, variable=volume_var, length=200)
        add_row("Audio volume", volume_scale)

        muted_var = tk.BooleanVar(value=bool(audio.get("muted", False)))
        add_row("Muted by default", ttk.Checkbutton(frame, variable=muted_var))

        queue_var = tk.StringVar(value=str(audio.get("queue_size", 50)))
        add_row("Audio queue size", ttk.Spinbox(frame, from_=1, to=500, textvariable=queue_var, width=8))

        close_to_tray_var = tk.BooleanVar(value=bool(cfg.get("close_to_tray", True)))
        add_row("Close button minimizes to tray", ttk.Checkbutton(frame, variable=close_to_tray_var))

        for i, (label, widget) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=3)
            widget.grid(row=i, column=1, sticky="w", pady=3)

        ttk.Label(
            frame,
            text="All changes are saved to config.json and apply after restart. Use Save & Restart to apply them now.",
            foreground="#666666",
        ).grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(
            frame,
            text=f"Config file: {get_config_path()}",
            foreground="#888888",
        ).grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        def collect() -> dict:
            special = [u.strip() for u in special_var.get().split(",") if u.strip()]
            return {
                "twitch_channel": channel_var.get().strip(),
                "tts_voice": by_label.get(voice_var.get(), voice_var.get()),
                "command_prefix": prefix_var.get().strip() or "!tts",
                "cooldown_seconds": int(float(cooldown_var.get() or 0)),
                "max_chars": int(float(maxchars_var.get() or 200)),
                "special_users": special,
                "close_to_tray": close_to_tray_var.get(),
                "audio": {
                    "volume": max(0.0, min(1.0, volume_var.get() / 100.0)),
                    "muted": muted_var.get(),
                    "queue_size": int(float(queue_var.get() or 50)),
                },
            }

        def save() -> bool:
            try:
                payload = collect()
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/api/config",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=6.0) as resp:
                    resp.read()
            except Exception as exc:  # noqa: BLE001 - surface any failure to the user
                self._log(f"Could not save options: {exc}")
                messagebox.showerror(
                    "Twitch TTS Options",
                    f"Could not save options:\n{exc}",
                    parent=win,
                )
                return False
            self._log("Options saved.")
            return True

        def on_save() -> None:
            if save():
                self.close_to_tray = close_to_tray_var.get()
                self._apply_close_behavior()
                win.destroy()

        def on_save_restart() -> None:
            if save():
                self.close_to_tray = close_to_tray_var.get()
                self._apply_close_behavior()
                win.destroy()
                self._restart_engine()

        btns = ttk.Frame(frame)
        btns.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Save", command=on_save).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Save & Restart", command=on_save_restart).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left")

    def _open_license(self) -> None:
        license_path = os.path.join(BASE_DIR, "LICENSE")
        opener = getattr(os, "startfile", None)
        if opener is not None and os.path.isfile(license_path):
            try:
                opener(license_path)
                return
            except OSError:
                pass
        webbrowser.open("https://github.com/karlsune/twitchTTS/blob/main/LICENSE")

    def _open_notices(self) -> None:
        notices_path = os.path.join(BASE_DIR, "THIRD-PARTY-NOTICES.md")
        opener = getattr(os, "startfile", None)
        if opener is not None and os.path.isfile(notices_path):
            try:
                opener(notices_path)
                return
            except OSError:
                pass
        webbrowser.open("https://github.com/karlsune/twitchTTS/blob/main/THIRD-PARTY-NOTICES.md")

    def _open_repo(self) -> None:
        webbrowser.open("https://github.com/karlsune/twitchTTS")

    def about(self, icon=None, item=None) -> None:
        self.root.deiconify()  # dialogs need a viewable parent (tray-only case)
        win = tk.Toplevel(self.root)
        win.title("About Twitch TTS Engine")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Twitch TTS Engine", font=("Segoe UI", 13, "bold")).pack()
        ttk.Label(
            frame,
            text=(
                f"Version {VERSION}\n\n"
                "Real-time text-to-speech for Twitch chat.\n\n"
                "Licensed under the MIT License.\n"
                f"Config: {get_config_path()}"
            ),
            justify="center",
        ).pack(pady=(4, 10))

        btns = ttk.Frame(frame)
        btns.pack()
        ttk.Button(btns, text="View MIT License", command=self._open_license).pack(side="left", padx=4)
        ttk.Button(btns, text="Third-Party Notices", command=self._open_notices).pack(side="left", padx=4)
        ttk.Button(btns, text="Repository", command=self._open_repo).pack(side="left", padx=4)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="left", padx=4)

    def exit_app(self, icon=None, item=None) -> None:
        self._stop.set()
        if self.engine is not None and self.engine.poll() is None:
            self.engine.terminate()
            try:
                self.engine.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.engine.kill()
        self.tray.stop()
        self.root.destroy()

    def run(self) -> None:
        self.volume_scale.bind("<ButtonRelease-1>", self._volume_released)
        self.root.mainloop()


def main() -> None:
    if "--engine" in sys.argv:
        # Strip our flag before importing app: its argparse runs at import
        # time and would reject the unknown argument.
        sys.argv = [arg for arg in sys.argv if arg != "--engine"]
        import app

        try:
            import asyncio

            asyncio.run(app.main())
        except KeyboardInterrupt:
            pass
        return
    TwitchTTSShell().run()


if __name__ == "__main__":
    main()
