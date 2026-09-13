#!/usr/bin/env bash
# Stop the Slack Mimic systemd user service (Linux).
# Usage: scripts/stop_service.sh [--disable]
#   (no flag)   stop now; starts again on next boot
#   --disable   stop now and don't start on boot
set -euo pipefail

NAME=slack-mimic

case "${1:-}" in
    "")        ACTION=stop ;;
    --disable) ACTION=disable ;;
    *)
        echo "Usage: $0 [--disable]" >&2
        exit 1
        ;;
esac

if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "Can't reach your user systemd (Failed to connect to bus)." >&2
    echo "Run this as the user who installed the service, not from sudo/su." >&2
    exit 1
fi

if ! systemctl --user cat "$NAME" >/dev/null 2>&1; then
    echo "Service $NAME is not installed." >&2
    exit 1
fi

if [[ "$ACTION" == disable ]]; then
    systemctl --user disable --now "$NAME"
    echo "Stopped $NAME and disabled start on boot."
else
    systemctl --user stop "$NAME"
    echo "Stopped $NAME (it will start again on next boot; use --disable to prevent that)."
fi

systemctl --user --no-pager status "$NAME" || true
