#!/usr/bin/env bash
# Weather receiver - health check.
# Run ON the webserver:   bash ~/healthcheck.sh
#
# Checks the weather-receiver service, its MQTT broker DNS/reachability, the SQLite
# history DB, the three rendered pages, and whether the field station is even
# expected to be transmitting right now (it runs dawn to dusk only).

set -u

SERVICE=weather-receiver
DB=$(python3 -c "import os; print(os.path.expanduser('~/weather-data/weather.db'))")
WEBROOT=/var/www/html
SCRIPT=$(systemctl show -p ExecStart --value weather-receiver 2>/dev/null \
    | grep -oP '(?<=path=)[^ ;]+weather_receiver\.py' | head -n1 || echo "$HOME/weather_receiver.py")
BROKER=$(python3 -c "import sys; sys.path.insert(0, sys.argv[1]); from mqtt_config import MQTT_BROKER; print(MQTT_BROKER)" "$(dirname "$SCRIPT")" 2>/dev/null || echo "(mqtt_config.py not found)")

g=$'\033[32m'; y=$'\033[33m'; r=$'\033[31m'; z=$'\033[0m'
ok(){   printf "  ${g}OK${z}   %s\n" "$1"; }
warn(){ printf "  ${y}WARN${z} %s\n" "$1"; }
bad(){  printf "  ${r}FAIL${z} %s\n" "$1"; }
hr(){   printf -- "----------------------------------------------------------\n"; }

echo "Weather receiver health check - $(date)"
hr

echo "[1] systemd service"
if systemctl is-active --quiet "$SERVICE"; then
  ok "$SERVICE is active"
else
  bad "$SERVICE is $(systemctl is-active "$SERVICE") - see journal below"
fi
echo "     restarts since boot: $(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || echo '?')"
hr

echo "[2] last 15 journal lines"
journalctl -u "$SERVICE" -n 15 --no-pager 2>/dev/null | sed 's/^/     /'
hr

echo "[3] MQTT broker: $BROKER"
if getent hosts "$BROKER" >/dev/null 2>&1; then
  ok "DNS resolves -> $(getent hosts "$BROKER" | awk '{print $1; exit}')"
  if command -v nc >/dev/null 2>&1; then
    nc -z -w5 "$BROKER" 1883 >/dev/null 2>&1 && ok "TCP 1883 reachable" || bad "TCP 1883 NOT reachable"
  else
    warn "nc not installed - skipped port check"
  fi
else
  bad "DNS does NOT resolve $BROKER  <-- this is the [Errno -2] failure; fix /etc/resolv.conf"
fi
hr

echo "[4] history database: $DB"
if [ -f "$DB" ]; then
  ok "exists ($(du -h "$DB" 2>/dev/null | awk '{print $1}'))"
  python3 - "$DB" <<'PY'
import sqlite3, sys, time, datetime
try:
    c = sqlite3.connect(sys.argv[1])
    n, newest = c.execute("SELECT COUNT(*), COALESCE(MAX(recv_time),0) FROM readings").fetchone()
    print("     rows: %d" % n)
    if newest:
        age = int(time.time() - newest)
        stamp = datetime.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S")
        tag = "fresh" if age < 900 else ("stale - %d min" % (age // 60))
        print("     newest reading: %s  (%ds ago, %s)" % (stamp, age, tag))
    else:
        print("     no readings stored yet (expected on a fresh install / before dawn)")
except Exception as e:
    print("     query failed: %s" % e)
PY
else
  bad "DB missing - init_db() never ran; check [2] for a traceback"
fi
hr

echo "[5] rendered pages: $WEBROOT"
now=$(date +%s)
for f in weather-widget.html weather.html weather-history.html; do
  p="$WEBROOT/$f"
  if [ -f "$p" ]; then
    m=$(stat -c %Y "$p" 2>/dev/null || echo 0)
    printf "     %-22s mtime %s  (%ss ago)\n" "$f" "$(date -d "@$m" '+%H:%M:%S' 2>/dev/null)" "$((now - m))"
    grep -q 'id="navbar"\|class="nav-inner"' "$p" && warn "  ^ still carries external site chrome (old pre-rework file)"
  else
    bad "$f missing"
  fi
done
if grep -qi "waiting for first report\|has no.*stored readings" "$WEBROOT/weather.html" 2>/dev/null; then
  echo "     full page mode: WAITING (no data yet - normal on fresh install or before dawn)"
else
  echo "     full page mode: live data"
fi
hr

echo "[6] is the field station expected to be transmitting right now?"
python3 - <<PY
import sys, time, datetime
sys.path.insert(0, "$(dirname "$SCRIPT")")
try:
    import weather_receiver as h
    now = time.time()
    rise, sett = h._sun_events(now)
    f = lambda t: datetime.datetime.fromtimestamp(t).strftime("%H:%M")
    print("     sunrise ~%s   sunset ~%s   (server local time)" % (f(rise), f(sett)))
    if h.is_expected_offline(now):
        print("     -> OFFLINE window: station is asleep, no messages expected until dawn.")
    else:
        print("     -> ONLINE window: messages should be arriving; if [4] shows none, check [2]/[3].")
except Exception as e:
    print("     could not compute (%s)" % e)
PY
hr
echo "done."
