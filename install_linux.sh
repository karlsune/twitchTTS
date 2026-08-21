#!/usr/bin/env bash
# Install the Twitch TTS Engine on Linux.
#   - Desktop: runs the full tray app (needs a display).
#   - Headless server: installs a systemd user service for the engine only;
#     audio plays in the browser UI (http://localhost:8080/index.html).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${TWITCHTTS_VENV:-$HOME/.local/share/twitchtts/venv}"

echo "==> Creating virtualenv at $VENV_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements-linux.txt"

# The tray window needs Tk; it is a system package (not pip-installable).
if command -v apt-get >/dev/null 2>&1 && ! python3 -c "import tkinter" 2>/dev/null; then
    echo "==> Installing python3-tk (needed for the tray window)"
    if sudo -n true 2>/dev/null; then
        sudo apt-get install -y python3-tk >/dev/null 2>&1 || echo "    (could not install python3-tk; tray GUI needs it)"
    else
        echo "    sudo not available passwordlessly - install manually: sudo apt-get install python3-tk"
    fi
fi

if [ -n "${DISPLAY:-}" ]; then
    echo "==> Desktop detected. Run the tray app with:"
    echo "    $VENV_DIR/bin/python $REPO_DIR/twitchtts_app.py"
    echo "    (config.json is auto-created next to the sources on first run)"
else
    SERVICE_NAME="twitchtts"
    UNIT="$HOME/.config/systemd/user/$SERVICE_NAME.service"
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<EOF
[Unit]
Description=Twitch TTS Engine (headless)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=$VENV_DIR/bin/python $REPO_DIR/app.py
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    echo "==> Headless service installed. Config: $REPO_DIR/config.json"
    echo "    Edit it, then: systemctl --user restart $SERVICE_NAME"
    systemctl --user status "$SERVICE_NAME" --no-pager | head -n 8
fi
