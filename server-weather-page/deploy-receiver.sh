#!/usr/bin/env bash
# Deploy the Weather receiver onto THIS server and restart the service.
#
# NOTE: the site deploy.sh does NOT touch the receiver - this is separate.
#
# Run ON the webserver, from the folder that holds the freshly
# uploaded files (e.g. ~/deploy/ after an scp upload, or a checkout's
# server-weather-page/):
#
#     cd ~/deploy && bash deploy-receiver.sh
#
# What it does:
#   1. finds the exact weather_receiver.py path the systemd unit runs
#   2. refuses to install an OLDER copy over a newer one (checks a marker string)
#   3. backs up the running file, installs the new one, installs healthcheck.sh
#   4. restarts weather-receiver, prints the journal, runs healthcheck.sh

set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_PY="$SRC_DIR/weather_receiver.py"
SRC_HC="$SRC_DIR/healthcheck.sh"
SERVICE=weather-receiver
MARKER='write_waiting_full_page'      # string that only exists in the current version

echo "== Weather receiver deploy =="
echo "source dir:   $SRC_DIR"

[ -f "$SRC_PY" ] || { echo "!! $SRC_PY not found - run this from the folder with the new files." >&2; exit 1; }
if ! grep -q "$MARKER" "$SRC_PY"; then
    echo "!! $SRC_PY is missing '$MARKER' - that is an OLD copy. Aborting so it does not" >&2
    echo "   get installed over a newer one. Re-upload the current weather_receiver.py." >&2
    exit 1
fi
echo "source check: OK (contains '$MARKER')"

# --- where does the service actually run the script from? ---
DEST_PY=$(systemctl cat "$SERVICE" 2>/dev/null \
    | sed -n 's/^ExecStart=.*[[:space:]]\([^[:space:]]*weather_receiver\.py\).*/\1/p' \
    | head -n1 || true)
WD=$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null || true)
case "${DEST_PY:-}" in
    /*) : ;;                                                   # absolute - keep
    "") DEST_PY="$HOME/weather_receiver.py" ;;  # unit not parseable - default to home dir
    *)  DEST_PY="${WD:-$HOME}/$DEST_PY" ;;         # relative - prepend WorkingDirectory
esac
SVC_USER=$(systemctl show -p User --value "$SERVICE" 2>/dev/null || true)
SVC_USER=${SVC_USER:-root}
echo "service runs: $DEST_PY  (as ${SVC_USER})"

# --- back up the running file ---
if [ -f "$DEST_PY" ]; then
    BAK="$DEST_PY.bak.$(date +%Y%m%d%H%M%S)"
    sudo cp -p "$DEST_PY" "$BAK"
    echo "backup:       $BAK"
fi

# --- install new receiver ---
sudo mkdir -p "$(dirname "$DEST_PY")"
sudo cp "$SRC_PY" "$DEST_PY"
sudo chown "${SVC_USER}:${SVC_USER}" "$DEST_PY" 2>/dev/null || true
if grep -q "$MARKER" "$DEST_PY"; then
    echo "installed:    $DEST_PY  (verified)"
else
    echo "!! post-install verify failed - $DEST_PY does not contain '$MARKER'" >&2
    exit 1
fi

# --- install healthcheck.sh into the user's home (best effort) ---
if [ -f "$SRC_HC" ]; then
    { cp "$SRC_HC" "$HOME/healthcheck.sh" && chmod +x "$HOME/healthcheck.sh" \
        && echo "installed:    $HOME/healthcheck.sh" ; } || echo "(could not install healthcheck.sh - skipped)"
fi

# --- restart + report ---
echo
echo "== restarting $SERVICE =="
sudo systemctl restart "$SERVICE"
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    echo "service:      active"
else
    echo "service:      NOT active"
fi

echo
echo "== journal (last 40 lines) =="
journalctl -u "$SERVICE" -n 40 --no-pager || true

echo
echo "== healthcheck =="
if [ -x "$HOME/healthcheck.sh" ]; then
    bash "$HOME/healthcheck.sh" || true
elif [ -f "$SRC_HC" ]; then
    bash "$SRC_HC" || true
fi

echo
echo "Expected on a good restart: banner + 'DB ready' + 'writing placeholder pages'"
echo "+ three 'updated' lines + 'Connected to MQTT broker' + 'Subscribed'."
echo "Row count stays 0 and pages show 'WAITING' until the field station wakes at dawn."
