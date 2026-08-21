"""Configuration path handling shared by the engine (app.py) and tray shell.

Convention
----------
* Development (running from a checkout): ``config.json`` lives next to the
  sources, as before.
* Frozen executable (PyInstaller): ``config.json`` lives in the per-user
  application data directory (``%APPDATA%\\TwitchTTS`` on Windows), so the
  exe can be dropped anywhere (desktop, USB stick) without scattering JSON
  files next to it. On first run the file is created from the bundled
  example, and an existing ``config.json`` next to the exe (from older
  builds) is migrated automatically.
"""

from __future__ import annotations

import json
import os
import sys

APP_NAME = "TwitchTTS"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_config_dir() -> str:
    """Per-user data dir when frozen, repo dir otherwise.

    Windows: ``%APPDATA%\TwitchTTS``. Linux/macOS: ``$XDG_CONFIG_HOME/TwitchTTS``
    (default ``~/.config/TwitchTTS``).
    """
    if is_frozen():
        if os.name == "nt":
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
        else:
            base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, APP_NAME)
    return os.path.dirname(os.path.abspath(__file__))


def get_config_path() -> str:
    return os.path.join(get_config_dir(), "config.json")


def _find_example() -> str:
    """The bundled example (frozen) or the one next to the sources (dev)."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        bundled = os.path.join(bundle, "config.example.json")
        if os.path.exists(bundled):
            return bundled
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json")


def load_config() -> dict:
    """Load config.json, creating it from the example on first run."""
    path = get_config_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Migrate a config.json left next to the exe by older frozen builds.
        if is_frozen():
            legacy = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "config.json")
            if os.path.exists(legacy):
                with open(legacy, encoding="utf-8-sig") as src, open(path, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
        if not os.path.exists(path):
            example = _find_example()
            if os.path.exists(example):
                with open(example, encoding="utf-8") as src, open(path, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    """Persist the configuration to disk (creates the directory if needed)."""
    path = get_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
