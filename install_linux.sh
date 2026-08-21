#!/usr/bin/env bash
# Install the Twitch TTS Engine as a headless systemd user service (Linux).
# Runs the engine only (no tray window): chat -> TTS -> SSE stream. Audio
# plays in the browser UI (http://localhost:8080/index.html) on servers
# without a sound device.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="twitchtts"
VENV_DIR="${TWITCHTTS_VENV:-$HOME/.local/share/twitchtts/venv}"
UNIT="$HOME/.config/systemd/user/$SERVICE_NAME.service"

echo "==> Creating virtualenv at $VENV_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements-linux.txt"

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
echo "==> Installed. Config: $REPO_DIR/config.json (auto-created on first run)"
echo "    Edit it, then: systemctl --user restart $SERVICE_NAME"
systemctl --user status "$SERVICE_NAME" --no-pager | head -n 8
