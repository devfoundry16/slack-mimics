#!/usr/bin/env bash
# Install Slack Mimic as a systemd user service (Linux).
# Usage: scripts/install_service.sh [--uninstall]
set -euo pipefail

NAME=slack-mimic
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/$NAME.service"

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl --user daemon-reload
    echo "Removed $UNIT_FILE"
    exit 0
fi

BIN="$PROJECT_DIR/.venv/bin/slack-mimic"
if [[ ! -x "$BIN" ]]; then
    echo "Missing $BIN — run 'uv sync' in $PROJECT_DIR first." >&2
    exit 1
fi
for f in config.yaml .env; do
    if [[ ! -f "$PROJECT_DIR/$f" ]]; then
        echo "Missing $PROJECT_DIR/$f — finish setup before installing the service." >&2
        exit 1
    fi
done

mkdir -p "$UNIT_DIR"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Slack Mimic mirror
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$PROJECT_DIR
ExecStart=$BIN --config $PROJECT_DIR/config.yaml --env $PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
# SIGINT lets the app shut down cleanly (it handles KeyboardInterrupt).
KillSignal=SIGINT
TimeoutStopSec=20
Restart=always
RestartSec=10
# Exit code 2 = HeartStamp auth failed; don't hammer Slack with a dead token.
RestartPreventExitStatus=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$NAME"

# Keep the user service running while logged out and start it at boot.
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
    sudo loginctl enable-linger "$USER"
fi

echo "Installed $UNIT_FILE"
systemctl --user --no-pager status "$NAME" || true
