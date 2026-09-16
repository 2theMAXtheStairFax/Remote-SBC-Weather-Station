#!/usr/bin/env python3
"""
Remote Weather Station - MQTT -> HTML receiver (webserver side)

Subscribes to the field Pi's MQTT topic, stores every message in a local SQLite
ring buffer (14 days), and renders three self-contained HTML pages:

  weather-widget.html   - compact 310x145 footer widget, shows 45-min AVERAGES
  weather.html          - full page: now + 45-min average + day/4h gust + inline-SVG trends
  weather-history.html  - every stored message, grouped by day, gaps flagged

Design notes:
  - The DB stores RAW values as received. Temperature compensation (Pi self-heat)
    and all derived values are computed at render time, so TEMP_OFFSET_C can be
    retuned later without rewriting history.
  - These pages are self-contained (no external site chrome/navbar/footer/CSS)
    so they can be embedded or linked from anywhere without a style clash, and
    an optional small credit line can be customized via CREDIT_URL/CREDIT_LABEL
    below. If you deploy this alongside an existing site, make sure that site's
    own deploy process doesn't overwrite these generated pages.
"""

import paho.mqtt.client as mqtt
import functools
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone

# systemd captures stdout, which Python block-buffers when it isn't a TTY - so
# `journalctl -u weather-receiver` would show nothing until the buffer fills or the
# process exits. Force every print() in this module to flush immediately.
print = functools.partial(print, flush=True)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# MQTT config loaded from mqtt_config.py (not committed).
# Copy server-weather-page/mqtt_config.example.py → mqtt_config.py next to this script and fill in your details.
try:
    from mqtt_config import MQTT_BROKER, MQTT_PORT, MQTT_TOPIC, MQTT_CLIENT_ID
except ImportError:
    raise SystemExit(
        "ERROR: mqtt_config.py not found.\n"
        "Copy server-weather-page/mqtt_config.example.py to the same directory as this script,\n"
        "rename it mqtt_config.py, and fill in your MQTT broker and topic."
    )

WIDGET_PATH = "/var/www/html/weather-widget.html"
FULL_PAGE_PATH = "/var/www/html/weather.html"
HISTORY_PAGE_PATH = "/var/www/html/weather-history.html"

WEATHER_DB = os.path.expanduser("~/weather-data/weather.db")

TEMP_OFFSET_C = 2.0            # subtracted from every temp reading (Pi self-heat); tune here only
AVG_WINDOW_MIN = 45            # rolling average window
GUST_RECENT_HOURS = 4          # "recent" gust/max window

# The "Offline" and history-page "gap" thresholds ADAPT to whatever cadence the field
# station is actually publishing at - so they stay quiet whether the Pi is on its
# current 5-min interval or the target 30 s (set on a future field visit). No server
# change is needed when the Pi is reconfigured. See observed_interval().
FALLBACK_INTERVAL_SEC = 300    # assumed spacing until enough messages seen to measure the real rate
GAP_FACTOR = 3.0              # flag a history-page gap only after ~this many consecutive misses
GAP_MIN_SEC = 90             #  ...but never flag a gap shorter than this
OFFLINE_FACTOR = 2.5          # show "Offline" (daylight only) after ~this many missed messages
OFFLINE_MIN_SEC = 120
HISTORY_RETENTION_DAYS = 14
HISTORY_MIN_REWRITE_SEC = 300  # history page is large; rewrite it at most this often
TREND_RANGES = (("4h", "4 h", 4), ("24h", "24 h", 24), ("14d", "2 weeks", 14 * 24))  # (id, label, hours)
TREND_DEFAULT = "24h"        # which range is shown before the reader picks one

# Field location - fill in your own station's coordinates. Used only to tell an
# intentional dawn-to-dusk shutdown (common for solar-powered stations that stop
# transmitting overnight) apart from a real daytime outage.
FIELD_LAT = 0.0    # your latitude
FIELD_LON = 0.0    # your longitude
DAWN_GRACE_MIN = 25           # station may start transmitting a bit before sunrise (civil twilight)
DUSK_GRACE_MIN = 12           # ...and keep transmitting a bit after sundown before going quiet

# Optional credit line shown at the bottom of each page. Leave CREDIT_URL empty
# to omit the credit entirely.
CREDIT_LABEL = ""
CREDIT_URL = ""

# Payload contract (see README.md). All numeric except wind_direction (string).
NUMERIC_KEYS = [
    "temp_c", "temp_f", "humidity", "dewpoint_c", "dewpoint_f",
    "pressure_hpa", "pressure_inhg", "light_lux",
    "wind_speed_mph", "rain_rate_iph", "rain_total_in",
    "battery_voltage", "battery_current", "battery_power",
    "timestamp",
]
ALL_KEYS = NUMERIC_KEYS + ["wind_direction"]

# ---------------------------------------------------------------------------
# 16-point compass
# ---------------------------------------------------------------------------
CARDINAL_16 = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]

_NAME_TO_DEG = {name: i * 22.5 for i, name in enumerate(CARDINAL_16)}
_NAME_TO_DEG.update({
    "NORTH": 0.0, "NORTHEAST": 45.0, "EAST": 90.0, "SOUTHEAST": 135.0,
    "SOUTH": 180.0, "SOUTHWEST": 225.0, "WEST": 270.0, "NORTHWEST": 315.0,
})


def name_to_deg(name):
    """Cardinal string ('NE', 'West', ...) -> degrees, or None if unrecognised."""
    if not name:
        return None
    return _NAME_TO_DEG.get(str(name).strip().upper())


def deg_to_16point(deg):
    """Degrees -> nearest 16-point compass label."""
    return CARDINAL_16[int(round(deg / 22.5)) % 16]


def wind_arrow(name, size=14):
    """Inline SVG arrow pointing the way the wind is BLOWING (i.e. 180 deg from the
    reported 'coming from' cardinal). Inherits text colour via currentColor.
    Returns '' for an unrecognised / missing direction."""
    deg = name_to_deg(name)
    if deg is None:
        return ""
    toward = (deg + 180.0) % 360.0
    return (
        f'<svg viewBox="0 0 100 100" width="{size}" height="{size}" '
        f'style="vertical-align:-0.15em" aria-hidden="true">'
        f'<g transform="rotate({toward:.0f} 50 50)">'
        f'<path d="M50 10 L70 62 L50 50 L30 62 Z" fill="currentColor"/></g></svg>'
    )


# ---------------------------------------------------------------------------
# Sunrise / sunset  (NOAA approximation, ~1 min accuracy, pure stdlib)
# Only used to distinguish the station's intentional overnight shutdown from a
# real daytime outage - not for anything the reader sees as a precise time.
# ---------------------------------------------------------------------------
def _sun_events(ts):
    """(sunrise_epoch, sunset_epoch) in UTC for the UTC calendar day of ts.

    Almanac for Computers (1990) method, ~2 min accuracy - plenty for telling the
    station's dawn-to-dusk downtime apart from a daytime outage. Returns
    (None, None) if the sun does not rise/set that day (never happens at 30N).
    """
    ZENITH = 90.833  # official sunrise/sunset altitude (refraction + solar radius)
    d = datetime.fromtimestamp(ts, timezone.utc)
    doy = d.timetuple().tm_yday
    lng_hour = FIELD_LON / 15.0
    midnight_utc = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()

    def _event(is_rise):
        t = doy + ((6.0 if is_rise else 18.0) - lng_hour) / 24.0
        mm = 0.9856 * t - 3.289
        ll = (mm + 1.916 * math.sin(math.radians(mm))
              + 0.020 * math.sin(math.radians(2 * mm)) + 282.634) % 360.0
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(ll)))) % 360.0
        ra += (math.floor(ll / 90.0) * 90.0) - (math.floor(ra / 90.0) * 90.0)
        ra /= 15.0
        sin_dec = 0.39782 * math.sin(math.radians(ll))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = ((math.cos(math.radians(ZENITH)) - sin_dec * math.sin(math.radians(FIELD_LAT)))
                 / (cos_dec * math.cos(math.radians(FIELD_LAT))))
        if cos_h > 1.0 or cos_h < -1.0:
            return None
        h = (360.0 - math.degrees(math.acos(cos_h))) if is_rise else math.degrees(math.acos(cos_h))
        h /= 15.0
        ut = (h + ra - 0.06571 * t - 6.622 - lng_hour) % 24.0
        return midnight_utc + ut * 3600.0

    return _event(True), _event(False)


def is_expected_offline(ts):
    """True when the field station is intentionally down: before dawn or after dusk
    (dusk = sundown + DUSK_GRACE_MIN).

    Checks both ts's UTC day and the day before, so a daylight window that runs past
    00:00 UTC (as it does for a US-Central longitude) is handled at the boundary.
    """
    for base in (ts, ts - 86400.0):
        rise, sett = _sun_events(base)
        if rise is None:
            continue
        if sett < rise:
            sett += 86400.0
        if rise - DAWN_GRACE_MIN * 60 <= ts <= sett + DUSK_GRACE_MIN * 60:
            return False
    return True


# ---------------------------------------------------------------------------
# SQLite storage (raw values, 14-day ring buffer)
# ---------------------------------------------------------------------------
def _connect():
    conn = sqlite3.connect(WEATHER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the data directory and readings table if they don't exist."""
    os.makedirs(os.path.dirname(WEATHER_DB), exist_ok=True)
    cols = ", ".join(f'"{k}" REAL' for k in NUMERIC_KEYS)
    with _connect() as conn:
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS readings ('
            f'recv_time INTEGER PRIMARY KEY, {cols}, wind_direction TEXT)'
        )
    print(f"✓ DB ready: {WEATHER_DB}")


def store_reading(data, recv_time):
    """Insert one raw reading and prune anything past the retention window."""
    row = {k: data.get(k) for k in ALL_KEYS}
    row["recv_time"] = int(recv_time)
    placeholders = ", ".join(f":{k}" for k in ["recv_time"] + ALL_KEYS)
    cutoff = int(time.time() - HISTORY_RETENTION_DAYS * 86400)
    with _connect() as conn:
        conn.execute(
            f'INSERT OR REPLACE INTO readings '
            f'(recv_time, {", ".join(ALL_KEYS)}) VALUES ({placeholders})',
            row,
        )
        conn.execute("DELETE FROM readings WHERE recv_time < ?", (cutoff,))


def load_rows_since(seconds_ago):
    return load_rows_between(time.time() - seconds_ago, time.time() + 60)


def load_rows_between(start_ts, end_ts):
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM readings WHERE recv_time BETWEEN ? AND ? ORDER BY recv_time",
            (int(start_ts), int(end_ts)),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------------------
def compensate(row):
    """Return a copy of row with temperature corrected for Pi self-heat.

    temp_c/temp_f are shifted down by TEMP_OFFSET_C; dewpoint is recomputed from
    the corrected temperature + humidity via the Magnus formula.
    """
    r = dict(row)
    tc = r.get("temp_c")
    if tc is not None:
        tc = tc - TEMP_OFFSET_C
        r["temp_c"] = tc
        r["temp_f"] = tc * 9.0 / 5.0 + 32.0
        rh = r.get("humidity")
        if rh and rh > 0:
            a, b = 17.62, 243.12
            gamma = math.log(rh / 100.0) + (a * tc) / (b + tc)
            dp = (b * gamma) / (a - gamma)
            r["dewpoint_c"] = dp
            r["dewpoint_f"] = dp * 9.0 / 5.0 + 32.0
        elif r.get("dewpoint_c") is not None:
            # no humidity to recompute from - shift the reported dewpoint the same way
            r["dewpoint_c"] = r["dewpoint_c"] - TEMP_OFFSET_C
            r["dewpoint_f"] = r["dewpoint_c"] * 9.0 / 5.0 + 32.0
    return r


def window_avg(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def avg_wind_direction(rows):
    """Speed-weighted vector average of recent wind directions -> 16-point label.

    Turns the vane's 8 raw cardinals into sub-directions (NNE, SSW, ...).
    Returns None when there is no usable direction data or the vector cancels out.
    """
    sx = sy = 0.0
    seen = False
    for r in rows:
        deg = name_to_deg(r.get("wind_direction"))
        if deg is None:
            continue
        seen = True
        w = r.get("wind_speed_mph") or 0.0
        if w <= 0:
            w = 1.0  # keep calm readings in the average with unit weight
        rad = math.radians(deg)
        sx += w * math.sin(rad)
        sy += w * math.cos(rad)
    if not seen:
        return None
    if abs(sx) < 1e-9 and abs(sy) < 1e-9:
        return None
    return deg_to_16point((math.degrees(math.atan2(sx, sy)) + 360.0) % 360.0)


def gust_since(rows, start_ts, key="wind_speed_mph"):
    vals = [r[key] for r in rows if r.get(key) is not None and r["recv_time"] >= start_ts]
    return max(vals) if vals else None


def local_midnight_ts():
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def observed_interval(rows, lookback=240):
    """Median spacing (s) of recent messages, so the gap/offline thresholds track the
    station's actual publish rate (5 min today, 30 s after the Pi is reconfigured).
    Overnight gaps are excluded; result is clamped to a sane range. Falls back to
    FALLBACK_INTERVAL_SEC until there are enough messages to measure."""
    recent = rows[-lookback:] if len(rows) > lookback else rows
    deltas = sorted(
        b["recv_time"] - a["recv_time"]
        for a, b in zip(recent, recent[1:])
        if 0 < (b["recv_time"] - a["recv_time"]) <= 3600
    )
    if len(deltas) < 5:
        return float(FALLBACK_INTERVAL_SEC)
    return min(900.0, max(20.0, float(deltas[len(deltas) // 2])))


def gap_spans(rows, interval=None):
    """Yield (gap_start_ts, gap_end_ts, approx_missed, kind) between consecutive rows.

    kind is "overnight" when the gap begins at/after the station's intentional dusk
    shutdown, else "missed" (a real daytime dropout). The flag threshold scales with
    the observed publish cadence, so a healthy station on any interval stays quiet.
    """
    if interval is None:
        interval = observed_interval(rows)
    threshold = max(GAP_MIN_SEC, interval * GAP_FACTOR)
    for prev, cur in zip(rows, rows[1:]):
        delta = cur["recv_time"] - prev["recv_time"]
        if delta <= threshold:
            continue
        # "overnight" only when BOTH the middle of the gap AND a point ~30 min after
        # the last message land in the station's expected dawn-to-dusk downtime. The
        # second check keeps a real afternoon outage that isn't recovered until the
        # next morning from being written off as a normal night. The margins absorb
        # the last/first message sitting up to a publish-interval from dusk/dawn.
        mid = (prev["recv_time"] + cur["recv_time"]) / 2.0
        after_last = prev["recv_time"] + 2 * interval + 1800
        if is_expected_offline(mid) and is_expected_offline(after_last):
            yield prev["recv_time"], cur["recv_time"], 0, "overnight"
        else:
            missed = max(1, int(round(delta / interval)) - 1)
            yield prev["recv_time"], cur["recv_time"], missed, "missed"


def _thin(pts, cap):
    """Evenly downsample a list of (ts, value) tuples to at most `cap` points."""
    if len(pts) <= cap:
        return pts
    step = len(pts) / cap
    return [pts[int(i * step)] for i in range(cap)]


def svg_line_chart(rows, key, color, hours, unit="", vfmt="{:.1f}"):
    """A self-contained inline <svg> line chart for `key` over the last `hours`.

    Server-rendered so the page has no external JS/CDN dependency (the site CSP
    blocks cdn.jsdelivr.net) and scales cleanly on mobile via viewBox.
    """
    cut = time.time() - hours * 3600
    pts = [(r["recv_time"], r[key]) for r in rows
           if r.get(key) is not None and r["recv_time"] >= cut]
    W, H = 320.0, 120.0
    PADL, PADR, PADT, PADB = 6.0, 46.0, 12.0, 20.0   # right pad leaves room for y labels
    font = 'font-family="Segoe UI,Arial,sans-serif"'

    if len(pts) < 2:
        return (f'<svg viewBox="0 0 {W:.0f} {H:.0f}" style="width:100%;height:auto" role="img">'
                f'<text x="{W/2:.0f}" y="{H/2:.0f}" text-anchor="middle" fill="#999" '
                f'font-size="11" {font}>not enough data yet</text></svg>')

    pts = _thin(pts, 180)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = xs[0], xs[-1]
    ymin, ymax = min(ys), max(ys)
    if ymax - ymin < 1e-9:
        ymin, ymax = ymin - 1.0, ymax + 1.0
    span = ymax - ymin
    ymin -= span * 0.10
    ymax += span * 0.10

    def sx(t):
        return PADL + (t - x0) / (x1 - x0) * (W - PADL - PADR) if x1 > x0 else PADL

    def sy(v):
        return PADT + (ymax - v) / (ymax - ymin) * (H - PADT - PADB)

    poly = " ".join(f"{sx(t):.1f},{sy(v):.1f}" for t, v in pts)

    grid = ""
    for gv in (ymax - span * 0.10, (ymin + ymax) / 2.0, ymin + span * 0.10):
        gy = sy(gv)
        grid += (f'<line x1="{PADL:.1f}" y1="{gy:.1f}" x2="{W-PADR:.1f}" y2="{gy:.1f}" '
                 f'stroke="#eee" stroke-width="1"/>'
                 f'<text x="{W-PADR+3:.1f}" y="{gy+3:.1f}" fill="#999" font-size="9" {font}>'
                 f'{vfmt.format(gv)}</text>')

    tick_fmt = "%m/%d" if hours > 48 else "%H:%M"
    ticks = ""
    for tt, anc in ((x0, "start"), ((x0 + x1) / 2.0, "middle"), (x1, "end")):
        ticks += (f'<text x="{sx(tt):.1f}" y="{H-6:.1f}" text-anchor="{anc}" fill="#999" '
                  f'font-size="9" {font}>{datetime.fromtimestamp(tt).strftime(tick_fmt)}</text>')

    nx, nv = sx(x1), ys[-1]
    ny = sy(nv)
    return (
        f'<svg viewBox="0 0 {W:.0f} {H:.0f}" style="width:100%;height:auto" role="img" '
        f'aria-label="{key} last {hours} h">{grid}'
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.8" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="2.6" fill="{color}"/>'
        f'<text x="{nx-4:.1f}" y="{ny-5:.1f}" text-anchor="end" fill="{color}" font-size="10" '
        f'font-weight="bold" {font}>{vfmt.format(nv)}{unit}</text>{ticks}</svg>'
    )


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------
def get_battery_status(voltage):
    """Determine battery status and class."""
    if voltage is None:
        return "UNKNOWN", "low", 0
    if voltage >= 13.6:
        return "FULL", "good", 100
    elif voltage >= 11.8:
        percent = int(((voltage - 11.0) / (13.6 - 11.0)) * 100)
        return "GOOD", "good", percent
    elif voltage >= 11.0:
        percent = int(((voltage - 11.0) / (13.6 - 11.0)) * 100)
        return "LOW", "low", percent
    else:
        return "CRITICAL", "critical", 0


def get_status_info(last_recv_ts, interval=None):
    """('online'|'asleep'|'offline', label).

    'asleep' = no recent message but the station is in its expected dawn-to-dusk
    downtime, so this is by design, not a fault. The silence that counts as
    "Offline" scales with the observed publish cadence, so a slow-but-healthy
    station (e.g. the current 5-min interval) is not marked down between messages.
    """
    if last_recv_ts is None:
        return "offline", "Waiting..."
    if interval is None:
        interval = FALLBACK_INTERVAL_SEC
    offline_after = max(OFFLINE_MIN_SEC, interval * OFFLINE_FACTOR)
    if time.time() - last_recv_ts < offline_after:
        return "online", "Online"
    if is_expected_offline(time.time()):
        return "asleep", "Asleep"
    return "offline", "Offline"


def fmt(value, spec="{:.1f}", dash="--"):
    try:
        return spec.format(value)
    except (ValueError, TypeError):
        return dash


# ---------------------------------------------------------------------------
# Shared page pieces (chrome-free; optional credit line only)
# ---------------------------------------------------------------------------
def _build_credit():
    """Empty by default. Set CREDIT_LABEL/CREDIT_URL above to add a small
    credit line to the bottom of each page."""
    if not CREDIT_URL:
        return ""
    label = CREDIT_LABEL or CREDIT_URL
    return (
        f'<div class="credit">Powered by '
        f'<a href="{CREDIT_URL}" target="_blank" rel="noopener">{label}</a>'
        f' &nbsp;<a class="credit-btn" href="{CREDIT_URL}" target="_blank" rel="noopener">'
        f'{CREDIT_URL} &rarr;</a></div>'
    )


PAGE_CREDIT = _build_credit()


def compensation_note():
    return (
        f'<p class="note">Temperatures shown are reduced by {TEMP_OFFSET_C:.0f}&nbsp;&deg;C '
        f'to compensate for heat generated by the Raspberry Pi enclosure. '
        f'Averages are over the last {AVG_WINDOW_MIN} minutes; gust/max figures are the '
        f'highest wind speed recorded today and in the last {GUST_RECENT_HOURS} hours. '
        f'The station is solar-powered and runs from dawn until about {DUSK_GRACE_MIN}&nbsp;minutes '
        f'after sundown.</p>'
    )


# ---------------------------------------------------------------------------
# Widget (45-min averages)
# ---------------------------------------------------------------------------
WIDGET_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=310, initial-scale=1.0">
    <meta http-equiv="refresh" content="60">
    <title>Remote Weather Widget</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            width: 310px; height: 145px;
            font-family: 'Segoe UI', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white; padding: 10px; overflow: hidden;
        }}
        .header {{ font-size: 11px; font-weight: bold; margin-bottom: 6px;
                  display: flex; justify-content: space-between; align-items: center; }}
        .status {{ font-size: 9px; padding: 2px 6px; border-radius: 10px; background: rgba(255,255,255,0.2); }}
        .status.online {{ background: #4CAF50; }}
        .status.offline {{ background: #f44336; }}
        .status.asleep {{ background: #5c6bc0; }}
        .main {{ display: flex; gap: 8px; margin-bottom: 6px; }}
        .temp-display {{ flex: 0 0 85px; text-align: center; }}
        .temp-big {{ font-size: 36px; font-weight: bold; line-height: 1; }}
        .temp-unit {{ font-size: 15px; opacity: 0.8; }}
        .stats {{ flex: 1; display: grid; grid-template-columns: 1fr 1fr; gap: 5px; font-size: 10px; }}
        .stat {{ background: rgba(255,255,255,0.15); padding: 3px 5px; border-radius: 4px; }}
        .stat-label {{ font-size: 8px; opacity: 0.8; margin-bottom: 2px; }}
        .stat-value {{ font-size: 12px; font-weight: bold; }}
        .footer {{ display: flex; justify-content: space-between; align-items: center;
                  font-size: 8px; opacity: 0.85; }}
        a {{ color: white; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="header">
        <span>&#127774; Weather Station &middot; {window}-min avg</span>
        <span class="status {status_class}">{status_text}</span>
    </div>
    <div class="main">
        <div class="temp-display">
            <div class="temp-big">{temp_f}<span class="temp-unit">&deg;F</span></div>
            <div style="font-size: 9px; opacity: 0.8;">{temp_c} &deg;C</div>
        </div>
        <div class="stats">
            <div class="stat"><div class="stat-label">Humidity</div><div class="stat-value">{humidity}%</div></div>
            <div class="stat"><div class="stat-label">Pressure</div><div class="stat-value">{pressure_inhg}"</div></div>
            <div class="stat"><div class="stat-label">Wind avg</div><div class="stat-value">{wind_speed} {wind_dir}{wind_arrow}</div></div>
            <div class="stat"><div class="stat-label">Rain today</div><div class="stat-value">{rain_total}"</div></div>
        </div>
    </div>
    <div class="footer">
        <span>{battery_voltage}V &middot; {update_time}</span>
        <span><a href="weather.html" target="_blank">Details &rarr;</a>{credit_link}</span>
    </div>
</body>
</html>"""


def write_widget(recent_rows, last_recv_ts, interval=None):
    try:
        latest = compensate(recent_rows[-1]) if recent_rows else {}
        comp_rows = [compensate(r) for r in recent_rows]
        status_class, status_text = get_status_info(last_recv_ts, interval)
        wdir = avg_wind_direction(recent_rows)
        credit_link = (
            f' &nbsp;<a href="{CREDIT_URL}" target="_blank" rel="noopener">{CREDIT_LABEL or CREDIT_URL}</a>'
            if CREDIT_URL else ""
        )

        html = WIDGET_TEMPLATE.format(
            window=AVG_WINDOW_MIN,
            status_class=status_class,
            status_text=status_text,
            temp_f=fmt(window_avg(comp_rows, "temp_f"), "{:.0f}"),
            temp_c=fmt(window_avg(comp_rows, "temp_c")),
            humidity=fmt(window_avg(comp_rows, "humidity"), "{:.0f}"),
            pressure_inhg=fmt(window_avg(comp_rows, "pressure_inhg"), "{:.2f}"),
            wind_speed=fmt(window_avg(comp_rows, "wind_speed_mph"), "{:.0f}"),
            wind_dir=wdir or "--",
            wind_arrow=(" " + wind_arrow(wdir, 11)) if wdir else "",
            rain_total=fmt(latest.get("rain_total_in"), "{:.2f}"),
            battery_voltage=fmt(latest.get("battery_voltage")),
            update_time=datetime.now().strftime("%H:%M"),
            credit_link=credit_link,
        )
        with open(WIDGET_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"✓ Widget updated ({AVG_WINDOW_MIN}-min avg)")
    except Exception as e:
        print(f"✗ Error writing widget: {e}")


# ---------------------------------------------------------------------------
# Full page (now + average + gust + inline-SVG trends)
# ---------------------------------------------------------------------------
FULL_PAGE_CSS = """
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0;
           padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
           min-height: 100vh; }
    .container { max-width: 900px; margin: 0 auto; }
    .card { background: white; padding: 26px 30px; border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3); margin-bottom: 20px; }
    h1 { margin: 0 0 6px 0; color: #333; font-size: 1.9em; }
    h2 { margin: 4px 0 14px 0; color: #444; font-size: 1.1em; text-transform: uppercase;
         letter-spacing: 0.05em; }
    .timestamp { color: #666; font-size: 0.9em; margin-bottom: 14px; }
    .status { display: inline-block; padding: 5px 15px; border-radius: 20px; font-size: 0.85em;
              font-weight: bold; margin-bottom: 8px; }
    .status.online { background: #4CAF50; color: white; }
    .status.offline { background: #f44336; color: white; }
    .status.asleep { background: #5c6bc0; color: white; }
    .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
               gap: 16px; margin-top: 12px; }
    .metric { padding: 14px; background: #f8f9fa; border-radius: 8px; border-left: 4px solid #667eea; }
    .metric.avg { border-left-color: #26a69a; }
    .metric.gust { border-left-color: #ef6c00; }
    .metric-label { font-size: 0.82em; color: #666; margin-bottom: 5px; }
    .metric-value { font-size: 1.7em; font-weight: bold; color: #333; }
    .metric-unit { font-size: 0.55em; color: #999; margin-left: 4px; }
    .sub { font-size: 0.85em; color: #666; margin-top: 3px; }
    .battery { margin-top: 18px; padding: 15px; background: #f0f0f0; border-radius: 8px; }
    .battery-bar { width: 100%; height: 26px; background: #ddd; border-radius: 13px;
                   overflow: hidden; margin-top: 10px; }
    .battery-fill { height: 100%; transition: width 0.3s, background-color 0.3s; }
    .battery-good { background: #4CAF50; } .battery-low { background: #FF9800; }
    .battery-critical { background: #f44336; }
    .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: 16px; }
    .chart-box { background: #fff; border: 1px solid #eee; border-radius: 8px; padding: 10px; min-width: 0; }
    .chart-box h3 { margin: 0 0 6px 4px; font-size: 0.9em; color: #444; }
    .chart-box svg { display: block; width: 100%; height: auto; }
    /* CSS-only range selector: radios (visually hidden) drive which .rng-* SVG shows */
    .rsel { position: absolute; width: 0; height: 0; opacity: 0; }
    .range-tabs { margin: 4px 0 14px 0; }
    .range-tabs label { display: inline-block; border: 1px solid #667eea; color: #667eea;
        padding: 4px 12px; border-radius: 14px; font-size: 0.8em; margin-right: 6px; cursor: pointer; }
    .chart-box .rng { display: none; }
    #r4h:checked ~ .charts .rng-4h,
    #r24h:checked ~ .charts .rng-24h,
    #r14d:checked ~ .charts .rng-14d { display: block; }
    #r4h:checked ~ .range-tabs label[for="r4h"],
    #r24h:checked ~ .range-tabs label[for="r24h"],
    #r14d:checked ~ .range-tabs label[for="r14d"] { background: #667eea; color: #fff; }
    .note { color: #777; font-size: 0.82em; line-height: 1.5; margin-top: 6px; }
    @media (max-width: 480px) {
        body { padding: 10px; }
        .card { padding: 20px 16px; }
        .metrics { grid-template-columns: 1fr 1fr; gap: 10px; }
        .metric-value { font-size: 1.4em; }
    }
    .credit { text-align: center; color: #fff; margin-top: 10px; font-size: 0.9em; }
    .credit a { color: #fff; }
    .credit-btn { display: inline-block; margin-left: 6px; padding: 3px 10px; border: 1px solid #fff;
        border-radius: 14px; text-decoration: none; font-size: 0.85em; }
    a.history-link { color: #667eea; font-weight: bold; text-decoration: none; }
"""

FULL_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="60">
    <title>Remote Weather Station</title>
    <style>{css}</style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>&#127774; Remote Weather Station</h1>
            <div class="timestamp">Last update: {update_time} &nbsp;|&nbsp; Data timestamp: {data_time}</div>
            <span class="status {status_class}">{status_text}</span>

            <h2>Now</h2>
            <div class="metrics">
                <div class="metric"><div class="metric-label">Temperature</div>
                    <div class="metric-value">{now_temp_f}<span class="metric-unit">&deg;F</span></div>
                    <div class="sub">{now_temp_c} &deg;C</div></div>
                <div class="metric"><div class="metric-label">Humidity</div>
                    <div class="metric-value">{now_humidity}<span class="metric-unit">%</span></div></div>
                <div class="metric"><div class="metric-label">Pressure</div>
                    <div class="metric-value">{now_pressure_inhg}<span class="metric-unit">inHg</span></div>
                    <div class="sub">{now_pressure_hpa} hPa</div></div>
                <div class="metric"><div class="metric-label">Dewpoint</div>
                    <div class="metric-value">{now_dewpoint_f}<span class="metric-unit">&deg;F</span></div>
                    <div class="sub">{now_dewpoint_c} &deg;C</div></div>
                <div class="metric"><div class="metric-label">Wind</div>
                    <div class="metric-value">{now_wind}<span class="metric-unit">mph</span></div>
                    <div class="sub">Vane: {now_wind_dir} {now_wind_arrow}</div></div>
                <div class="metric"><div class="metric-label">Rain (today)</div>
                    <div class="metric-value">{now_rain_total}<span class="metric-unit">in</span></div>
                    <div class="sub">Rate: {now_rain_rate} in/hr</div></div>
                <div class="metric"><div class="metric-label">Light</div>
                    <div class="metric-value">{now_light}<span class="metric-unit">lux</span></div></div>
            </div>

            <h2>Average &mdash; last {window} min</h2>
            <div class="metrics">
                <div class="metric avg"><div class="metric-label">Temperature</div>
                    <div class="metric-value">{avg_temp_f}<span class="metric-unit">&deg;F</span></div>
                    <div class="sub">{avg_temp_c} &deg;C</div></div>
                <div class="metric avg"><div class="metric-label">Humidity</div>
                    <div class="metric-value">{avg_humidity}<span class="metric-unit">%</span></div></div>
                <div class="metric avg"><div class="metric-label">Pressure</div>
                    <div class="metric-value">{avg_pressure_inhg}<span class="metric-unit">inHg</span></div></div>
                <div class="metric avg"><div class="metric-label">Dewpoint</div>
                    <div class="metric-value">{avg_dewpoint_f}<span class="metric-unit">&deg;F</span></div></div>
                <div class="metric avg"><div class="metric-label">Wind speed</div>
                    <div class="metric-value">{avg_wind}<span class="metric-unit">mph</span></div></div>
                <div class="metric avg"><div class="metric-label">Wind direction</div>
                    <div class="metric-value">{avg_wind_dir} {avg_wind_arrow}</div>
                    <div class="sub">16-point &middot; blowing direction</div></div>
            </div>

            <h2>Gust / Max</h2>
            <div class="metrics">
                <div class="metric gust"><div class="metric-label">Today's max wind</div>
                    <div class="metric-value">{gust_today}<span class="metric-unit">mph</span></div></div>
                <div class="metric gust"><div class="metric-label">Max wind, last {gust_hours} h</div>
                    <div class="metric-value">{gust_recent}<span class="metric-unit">mph</span></div></div>
            </div>

            <div class="battery">
                <strong>Battery</strong>
                <div style="margin-top: 5px;">{battery_voltage} V &bull; {battery_current} mA &bull; {battery_power} mW</div>
                <div class="battery-bar"><div class="battery-fill battery-{battery_class}" style="width: {battery_percent}%"></div></div>
                <div style="margin-top: 5px; font-size: 0.9em; color: #666;">{battery_status} ({battery_percent}%)</div>
            </div>
        </div>

        <div class="card">
            <h2>Trends</h2>
            {trend_radios}
            <div class="range-tabs">{trend_labels}</div>
            <div class="charts">
                <div class="chart-box"><h3>Temperature (&deg;F)</h3>{chart_temp}</div>
                <div class="chart-box"><h3>Humidity (%)</h3>{chart_hum}</div>
                <div class="chart-box"><h3>Wind speed (mph)</h3>{chart_wind}</div>
                <div class="chart-box"><h3>Pressure (inHg)</h3>{chart_press}</div>
                <div class="chart-box"><h3>Rain total (in)</h3>{chart_rain}</div>
            </div>
            <p class="note">Full 14-day detail is on the <a class="history-link" href="weather-history.html">history page</a>.</p>
        </div>

        <div class="card">
            {compensation_note}
            <p><a class="history-link" href="weather-history.html">View full 2-week message history &rarr;</a></p>
        </div>

        {page_credit}
    </div>
</body>
</html>"""


def write_waiting_full_page(last_recv_ts):
    """Minimal chrome-free full page for when the DB has no readings yet (fresh
    install, or restarted while the station is in its overnight shutdown)."""
    status_class, status_text = get_status_info(last_recv_ts)
    if status_class == "offline":
        status_text = "Waiting for first report"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="30">
    <title>Remote Weather Station</title>
    <style>{FULL_PAGE_CSS}</style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>&#127774; Remote Weather Station</h1>
            <span class="status {status_class}">{status_text}</span>
            <p class="note" style="margin-top: 14px;">The receiver is running and has no
               stored readings yet. The field station is solar-powered and publishes from
               dawn to dusk &mdash; this page fills in when the first report arrives.</p>
            <p><a class="history-link" href="weather-history.html">Full history &rarr;</a></p>
        </div>
        <div class="card">{compensation_note()}</div>
        {PAGE_CREDIT}
    </div>
</body>
</html>"""
    with open(FULL_PAGE_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print("✓ Full page updated (waiting for first report)")


def write_full_page(all_rows, last_recv_ts, interval=None):
    try:
        if not all_rows:
            write_waiting_full_page(last_recv_ts)
            return
        comp_all = [compensate(r) for r in all_rows]
        raw_latest = all_rows[-1]
        latest = comp_all[-1]

        window_cut = time.time() - AVG_WINDOW_MIN * 60
        avg_rows_raw = [r for r in all_rows if r["recv_time"] >= window_cut]
        avg_rows = [compensate(r) for r in avg_rows_raw]

        gust_cut = time.time() - GUST_RECENT_HOURS * 3600
        gust_today = gust_since(all_rows, local_midnight_ts())
        gust_recent = gust_since(all_rows, gust_cut)

        battery_status, battery_class, battery_percent = get_battery_status(raw_latest.get("battery_voltage"))
        status_class, status_text = get_status_info(last_recv_ts, interval)

        ts = raw_latest.get("timestamp")
        data_time = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "--"

        # Trend charts: for each metric render all 3 ranges, wrapped in span.rng-<id>;
        # the CSS-only radio selector shows one at a time (no JS -> nothing for the CSP
        # to block, and it can't get stuck like the old Chart.js tabs did).
        radios = "".join(
            f'<input type="radio" name="range" id="r{rid}" class="rsel"'
            f'{" checked" if rid == TREND_DEFAULT else ""}>'
            for rid, _lbl, _hrs in TREND_RANGES
        )
        labels = "".join(f'<label for="r{rid}">{lbl}</label>' for rid, lbl, _hrs in TREND_RANGES)

        def trend(source_rows, key, color, unit, vfmt):
            return "".join(
                f'<span class="rng rng-{rid}">'
                f'{svg_line_chart(source_rows, key, color, hrs, unit, vfmt)}</span>'
                for rid, _lbl, hrs in TREND_RANGES
            )

        html = FULL_PAGE_TEMPLATE.format(
            css=FULL_PAGE_CSS,
            window=AVG_WINDOW_MIN,
            gust_hours=GUST_RECENT_HOURS,
            trend_radios=radios,
            trend_labels=labels,
            chart_temp=trend(comp_all, "temp_f", "#e53935", "&#176;", "{:.0f}"),
            chart_hum=trend(all_rows, "humidity", "#0097a7", "%", "{:.0f}"),
            chart_wind=trend(all_rows, "wind_speed_mph", "#1e88e5", "", "{:.0f}"),
            chart_press=trend(all_rows, "pressure_inhg", "#8e24aa", "", "{:.2f}"),
            chart_rain=trend(all_rows, "rain_total_in", "#00897b", "&quot;", "{:.2f}"),
            update_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            data_time=data_time,
            status_class=status_class,
            status_text=status_text,
            now_temp_f=fmt(latest.get("temp_f")),
            now_temp_c=fmt(latest.get("temp_c")),
            now_humidity=fmt(latest.get("humidity"), "{:.0f}"),
            now_pressure_inhg=fmt(latest.get("pressure_inhg"), "{:.2f}"),
            now_pressure_hpa=fmt(latest.get("pressure_hpa"), "{:.0f}"),
            now_dewpoint_f=fmt(latest.get("dewpoint_f")),
            now_dewpoint_c=fmt(latest.get("dewpoint_c")),
            now_wind=fmt(latest.get("wind_speed_mph")),
            now_wind_dir=raw_latest.get("wind_direction") or "--",
            now_wind_arrow=wind_arrow(raw_latest.get("wind_direction"), 13),
            now_rain_total=fmt(latest.get("rain_total_in"), "{:.2f}"),
            now_rain_rate=fmt(latest.get("rain_rate_iph"), "{:.2f}"),
            now_light=fmt(latest.get("light_lux"), "{:.0f}"),
            avg_temp_f=fmt(window_avg(avg_rows, "temp_f")),
            avg_temp_c=fmt(window_avg(avg_rows, "temp_c")),
            avg_humidity=fmt(window_avg(avg_rows, "humidity"), "{:.0f}"),
            avg_pressure_inhg=fmt(window_avg(avg_rows, "pressure_inhg"), "{:.2f}"),
            avg_dewpoint_f=fmt(window_avg(avg_rows, "dewpoint_f")),
            avg_wind=fmt(window_avg(avg_rows, "wind_speed_mph")),
            avg_wind_dir=avg_wind_direction(avg_rows_raw) or "--",
            avg_wind_arrow=wind_arrow(avg_wind_direction(avg_rows_raw), 16),
            gust_today=fmt(gust_today),
            gust_recent=fmt(gust_recent),
            battery_voltage=fmt(raw_latest.get("battery_voltage"), "{:.2f}"),
            battery_current=fmt(raw_latest.get("battery_current"), "{:.0f}"),
            battery_power=fmt(raw_latest.get("battery_power"), "{:.0f}"),
            battery_status=battery_status,
            battery_class=battery_class,
            battery_percent=battery_percent,
            compensation_note=compensation_note(),
            page_credit=PAGE_CREDIT,
        )
        with open(FULL_PAGE_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        print("✓ Full page updated")
    except Exception as e:
        print(f"✗ Error writing full page: {e}")


# ---------------------------------------------------------------------------
# History page (every stored message, grouped by day, gaps flagged)
# ---------------------------------------------------------------------------
HISTORY_CSS = """
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px;
           background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
    .container { max-width: 1100px; margin: 0 auto; }
    .card { background: #fff; padding: 24px 26px; border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3); margin-bottom: 20px; }
    h1 { margin: 0 0 6px 0; color: #333; font-size: 1.7em; }
    .meta { color: #666; font-size: 0.9em; margin-bottom: 16px; }
    details { border: 1px solid #eee; border-radius: 8px; margin-bottom: 10px; }
    summary { cursor: pointer; padding: 10px 14px; font-weight: bold; color: #444; background: #f8f9fa;
              border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.82em; }
    th, td { padding: 6px 8px; text-align: right; border-bottom: 1px solid #f0f0f0; white-space: nowrap; }
    th { position: sticky; top: 0; background: #fff; color: #666; text-align: right; }
    th:first-child, td:first-child { text-align: left; }
    .tbl-wrap { overflow-x: auto; max-height: 70vh; overflow-y: auto; }
    tr.missed td { text-align: center; color: #b0653c; background: #fff5ef; font-size: 0.75em;
                   font-style: italic; padding: 3px; }
    tr.overnight td { text-align: center; color: #5c6bc0; background: #f2f3fb; font-size: 0.72em;
                      font-style: italic; padding: 2px; }
    .note { color: #777; font-size: 0.82em; line-height: 1.5; }
    .credit { text-align: center; color: #fff; margin-top: 10px; font-size: 0.9em; }
    .credit a { color: #fff; }
    .credit-btn { display: inline-block; margin-left: 6px; padding: 3px 10px; border: 1px solid #fff;
        border-radius: 14px; text-decoration: none; font-size: 0.85em; }
    a.back { color: #667eea; font-weight: bold; text-decoration: none; }
"""

HISTORY_COLS = [
    ("Time", None), ("Temp °F", "temp_f"), ("Temp °C", "temp_c"), ("Hum %", "humidity"),
    ("Dew °F", "dewpoint_f"), ("Press inHg", "pressure_inhg"), ("Wind mph", "wind_speed_mph"),
    ("Dir", "wind_direction"), ("Rain in", "rain_total_in"), ("Light lux", "light_lux"),
    ("Batt V", "battery_voltage"),
]


def _history_row(r):
    t = datetime.fromtimestamp(r["recv_time"]).strftime("%H:%M:%S")
    cells = [f"<td>{t}</td>"]
    for _, key in HISTORY_COLS[1:]:
        v = r.get(key)
        if key == "wind_direction":
            cells.append(f"<td>{v or '--'}</td>")
        elif key == "light_lux":
            cells.append(f"<td>{fmt(v, '{:.0f}')}</td>")
        elif key == "battery_voltage":
            cells.append(f"<td>{fmt(v, '{:.2f}')}</td>")
        elif key in ("temp_f", "temp_c", "dewpoint_f", "wind_speed_mph"):
            cells.append(f"<td>{fmt(v)}</td>")
        else:
            cells.append(f"<td>{fmt(v, '{:.2f}')}</td>")
    return "<tr>" + "".join(cells) + "</tr>"


def write_history_page(all_rows, last_recv_ts, interval=None):
    try:
        comp_rows = [compensate(r) for r in all_rows]
        # gap markers keyed by the recv_time of the row that FOLLOWS the gap
        gaps = {end: (kind, missed) for (_s, end, missed, kind) in gap_spans(all_rows, interval)}

        by_day = {}
        for r in comp_rows:
            day = datetime.fromtimestamp(r["recv_time"]).strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(r)

        today = datetime.now().strftime("%Y-%m-%d")
        header = "".join(f"<th>{label}</th>" for label, _ in HISTORY_COLS)
        blocks = []
        for day in sorted(by_day, reverse=True):
            rows = by_day[day]
            open_attr = " open" if day == today else ""
            body = []
            for r in rows:
                if r["recv_time"] in gaps:
                    kind, n = gaps[r["recv_time"]]
                    if kind == "overnight":
                        body.append(
                            f'<tr class="overnight"><td colspan="{len(HISTORY_COLS)}">'
                            f'&#127769; overnight shutdown &mdash; station runs dawn to dusk</td></tr>'
                        )
                    else:
                        body.append(
                            f'<tr class="missed"><td colspan="{len(HISTORY_COLS)}">'
                            f'&#8987; gap &mdash; ~{n} report(s) not received</td></tr>'
                        )
                body.append(_history_row(r))
            blocks.append(
                f'<details{open_attr}><summary>{day} &nbsp; ({len(rows)} reports)</summary>'
                f'<div class="tbl-wrap"><table><thead><tr>{header}</tr></thead>'
                f'<tbody>{"".join(body)}</tbody></table></div></details>'
            )

        status_class, status_text = get_status_info(last_recv_ts, interval)
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="60">
    <title>Remote Weather &mdash; Full History</title>
    <style>{HISTORY_CSS}</style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>&#128202; Remote Weather &mdash; 2-Week Message History</h1>
            <div class="meta">Station: {status_text} &nbsp;|&nbsp; {len(all_rows)} reports stored &nbsp;|&nbsp;
                retention {HISTORY_RETENTION_DAYS} days &nbsp;|&nbsp; generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
            <p><a class="back" href="weather.html">&larr; Back to live weather</a></p>
            {"".join(blocks) if blocks else "<p>No data stored yet.</p>"}
        </div>
        <div class="card">
            {compensation_note()}
        </div>
        {PAGE_CREDIT}
    </div>
</body>
</html>"""
        with open(HISTORY_PAGE_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        print("✓ History page updated")
    except Exception as e:
        print(f"✗ Error writing history page: {e}")


# ---------------------------------------------------------------------------
# MQTT plumbing
# ---------------------------------------------------------------------------
last_update_time = None
_last_history_write = 0.0


def render_all(force_history=False):
    """Read the DB and rewrite the pages.

    Widget + full page rewrite on every message; the history page is large, so it
    rewrites at most every HISTORY_MIN_REWRITE_SEC (or when forced).
    """
    global _last_history_write
    all_rows = load_rows_since(HISTORY_RETENTION_DAYS * 86400)
    recent = load_rows_since(AVG_WINDOW_MIN * 60)
    interval = observed_interval(all_rows)   # adapts to the station's real cadence
    write_widget(recent, last_update_time, interval)
    write_full_page(all_rows, last_update_time, interval)
    if force_history or time.time() - _last_history_write >= HISTORY_MIN_REWRITE_SEC:
        write_history_page(all_rows, last_update_time, interval)
        _last_history_write = time.time()


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"✓ Connected to MQTT broker: {MQTT_BROKER}")
        client.subscribe(MQTT_TOPIC)
        print(f"✓ Subscribed to topic: {MQTT_TOPIC}")
    else:
        print(f"✗ Connection failed with code: {rc}")


def on_message(client, userdata, msg):
    try:
        global last_update_time
        data = json.loads(msg.payload.decode())
        recv_time = time.time()
        last_update_time = recv_time

        missing = [k for k in ALL_KEYS if k not in data]
        if missing:
            print(f"⚠ Payload missing keys (rendered with placeholders): {missing}")

        store_reading(data, recv_time)

        tf = data.get("temp_f")
        print(f"\n{'='*60}")
        print(f"Weather Update: {datetime.now().strftime('%H:%M:%S')}  "
              f"raw temp {fmt(tf)}°F  batt {fmt(data.get('battery_voltage'), '{:.2f}')}V")
        print(f"{'='*60}")

        render_all()
    except Exception as e:
        print(f"✗ Error processing message: {e}")


def main():
    print("=" * 60)
    print("Remote Weather Station - MQTT -> HTML receiver")
    print("=" * 60)
    print(f"MQTT Broker:  {MQTT_BROKER}:{MQTT_PORT}")
    print(f"DB:           {WEATHER_DB}")
    print(f"Widget:       {WIDGET_PATH}")
    print(f"Full Page:    {FULL_PAGE_PATH}")
    print(f"History Page: {HISTORY_PAGE_PATH}")
    print("=" * 60)

    init_db()

    # Always render once at startup so the deployed pages reflect the new code
    # immediately - from stored history if there is any, otherwise placeholder
    # "waiting" pages (rather than leaving a stale page live until the first message).
    global last_update_time
    existing = load_rows_since(HISTORY_RETENTION_DAYS * 86400)
    if existing:
        last_update_time = existing[-1]["recv_time"]
        print(f"Priming pages from {len(existing)} stored reports...")
    else:
        print("No stored history yet - writing placeholder pages.")
    render_all(force_history=True)

    client = mqtt.Client(MQTT_CLIENT_ID)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        print("✓ Connection initiated")
    except Exception as e:
        print(f"✗ Failed to connect: {e}")
        return

    print("\n\U0001f4e1 Listening for weather data... Ctrl+C to exit\n")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n✓ Shutting down...")
        client.disconnect()


if __name__ == "__main__":
    main()
