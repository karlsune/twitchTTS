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
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import messagebox, ttk

import pystray
from PIL import Image, ImageDraw

VERSION = "0.2.0-dev"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.port = 8080
        self.stream_port = 8081
        self._stop = threading.Event()

        self._build_window()
        self._build_tray()
        self._start_engine()
        threading.Thread(target=self._poll_status, daemon=True).start()
        threading.Thread(target=self._sse_reader, daemon=True).start()
        self.root.after(100, self._drain_log_queue)

    # ------------------------------------------------------------------ tray
    def _build_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("Open", self.open_window, default=True),
            pystray.MenuItem("Mute", self.toggle_mute),
            pystray.MenuItem("About", self.about),
            pystray.MenuItem("Exit", self.exit_app),
        )
        self.tray = pystray.Icon(
            "twitchtts", self._tray_image("offline"), "Twitch TTS Engine", menu
        )
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _tray_image(self, state: str) -> Image.Image:
        color = COLORS.get(state, COLORS["offline"])
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=color, outline=(30, 30, 30), width=3)
        d.polygon([(24, 25), (34, 25), (46, 15), (46, 49), (34, 39), (24, 39)], fill=(255, 255, 255))
        return img

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.tray.icon = self._tray_image(state)
            self.tray.title = f"Twitch TTS Engine — {state}"
            self.tray.update_menu()

    # ---------------------------------------------------------------- window
    def _build_window(self) -> None:
        self.root = tk.Tk()
        self.root.title("Twitch TTS Engine")
        self.root.geometry("520x420")
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

        vol = ttk.Frame(frame)
        vol.pack(fill="x", **pad)
        ttk.Label(vol, text="Volume").pack(side="left")
        self.volume_var = tk.DoubleVar(value=80)
        self.volume_scale = ttk.Scale(
            vol, from_=0, to=100, variable=self.volume_var, command=self._volume_drag
        )
        self.volume_scale.pack(side="left", fill="x", expand=True, padx=8)
        self.volume_label = ttk.Label(vol, text="80%", width=5)
        self.volume_label.pack(side="left")

        now = ttk.Frame(frame)
        now.pack(fill="x", **pad)
        ttk.Label(now, text="Now Playing", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.now_label = ttk.Label(now, text="—", wraplength=480, justify="left")
        self.now_label.pack(anchor="w", pady=(2, 0))
        self.queue_label = ttk.Label(now, text="", foreground="#666666")
        self.queue_label.pack(anchor="w")

        log_frame = ttk.Frame(frame)
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scroll.set)

        self.status_label = ttk.Label(frame, text="● offline", foreground="#8c8c8c")
        self.status_label.pack(anchor="w", **pad)

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
        threading.Thread(target=self._load_voices, daemon=True).start()

    def _engine_reader(self) -> None:
        assert self.engine is not None and self.engine.stdout is not None
        for line in self.engine.stdout:
            self.log_queue.put(line.rstrip())
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

    # -------------------------------------------------------------- controls
    def toggle_mute(self, icon=None, item=None) -> None:
        self.muted = not self.muted
        self._post("/api/control", {"action": "unmute" if self.muted else "mute"})
        self.mute_btn.config(text="Unmute" if self.muted else "Mute")

    def skip(self) -> None:
        self._post("/api/control", {"action": "skip"})

    def _voice_selected(self, _event=None) -> None:
        self._post("/api/voice", {"voice": self.voice_var.get()})

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

    def about(self, icon=None, item=None) -> None:
        messagebox.showinfo(
            "About Twitch TTS Engine",
            f"Twitch TTS Engine {VERSION}\n\n"
            "Real-time TTS for Twitch chat.\n"
            "https://github.com/karlsune/twitchTTS",
            parent=self.root,
        )

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
