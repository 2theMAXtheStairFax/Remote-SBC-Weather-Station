#!/usr/bin/env python3
"""
Night-mode controller for the weather station field Pi.

Stops the weather_station systemd service at sunset and restarts it at
sunrise, reducing power draw during hours when solar charging is unavailable.
The server-side receiver already knows about the sleep window (it uses the
same sunrise/sunset logic to show "Asleep" instead of "Offline"), so an
overnight stop never registers as an outage.

Usage:
  python3 night_mode.py          # single check — run from cron every 5 minutes
  python3 night_mode.py --status # print today's times and current state, then exit
  python3 night_mode.py --daemon # loop forever, re-checking every 5 minutes

Cron setup (run as root so systemctl works without sudo):
  sudo crontab -e
  # Add this line:
  */5 * * * * /usr/bin/python3 /home/<USER>/weather-station/night_mode.py >> /var/log/weather_night_mode.log 2>&1

Prerequisites:
  pip3 install astral
  Add STATION_LATITUDE, STATION_LONGITUDE, STATION_TIMEZONE to mqtt_config.py.
  See mqtt_config.example.py for the exact field names.

Power notes (Pi 3B, approximate):
  Service stopped, Pi idle:      ~1–2 W  (vs ~3–4 W with sensors running)
  Pi fully halted (OS stopped):  ~0.5–1 W, but needs a hardware wake signal
                                  to restart (GPIO 3 / pin 5 pulled low, or
                                  power cycle) — not doable in pure software.
  EPEVER load-output timer:       0 W overnight — the best option if the Pi is
                                  wired to the EPEVER's load terminal.
                                  Configure via the EPEVER display:
                                    Load → Timer Control → set on/off times.
                                  No code changes needed.
"""

import subprocess
import sys
import time
import datetime
import logging

try:
    from zoneinfo import ZoneInfo                       # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo             # older Raspberry Pi OS

try:
    from astral import LocationInfo
    from astral.sun import sun
except ImportError:
    raise SystemExit(
        "ERROR: astral library not installed.\n"
        "Run: pip3 install astral"
    )

try:
    from mqtt_config import STATION_LATITUDE, STATION_LONGITUDE, STATION_TIMEZONE
except ImportError:
    raise SystemExit(
        "ERROR: mqtt_config.py not found, or missing STATION_LATITUDE /\n"
        "STATION_LONGITUDE / STATION_TIMEZONE.  See mqtt_config.example.py."
    )

SERVICE_NAME = "weather_station"
CHECK_INTERVAL_SECONDS = 300   # daemon mode only

logging.basicConfig(
    format="%(asctime)s [night_mode] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def _sun_times(date=None):
    tz = ZoneInfo(STATION_TIMEZONE)
    loc = LocationInfo(
        latitude=STATION_LATITUDE,
        longitude=STATION_LONGITUDE,
        timezone=STATION_TIMEZONE,
    )
    if date is None:
        date = datetime.datetime.now(tz).date()
    s = sun(loc.observer, date=date, tzinfo=tz)
    return s["sunrise"], s["sunset"]


def _is_daytime():
    tz = ZoneInfo(STATION_TIMEZONE)
    now = datetime.datetime.now(tz)
    sunrise, sunset = _sun_times(now.date())
    return sunrise <= now <= sunset


def _service_active():
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE_NAME],
        capture_output=True,
    ).returncode == 0


def _ctl(action):
    result = subprocess.run(
        ["systemctl", action, SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("systemctl %s %s failed: %s", action, SERVICE_NAME, result.stderr.strip())
    return result.returncode == 0


def check_and_act():
    """One-shot: start or stop the service based on current time of day."""
    tz = ZoneInfo(STATION_TIMEZONE)
    now = datetime.datetime.now(tz)
    sunrise, sunset = _sun_times(now.date())
    daytime = _is_daytime()
    active = _service_active()

    log.info(
        "%s | sunrise %s  sunset %s | %s | service %s",
        now.strftime("%H:%M %Z"),
        sunrise.strftime("%H:%M"),
        sunset.strftime("%H:%M"),
        "DAY" if daytime else "NIGHT",
        "active" if active else "inactive",
    )

    if daytime and not active:
        log.info("Starting %s (sunrise).", SERVICE_NAME)
        _ctl("start")
    elif not daytime and active:
        log.info("Stopping %s (sunset).", SERVICE_NAME)
        _ctl("stop")


def print_status():
    tz = ZoneInfo(STATION_TIMEZONE)
    now = datetime.datetime.now(tz)
    sunrise, sunset = _sun_times(now.date())
    print(f"Time:    {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Sunrise: {sunrise.strftime('%H:%M')}")
    print(f"Sunset:  {sunset.strftime('%H:%M')}")
    print(f"Period:  {'DAY' if _is_daytime() else 'NIGHT'}")
    print(f"Service: {'active' if _service_active() else 'inactive'}")


def main():
    if "--status" in sys.argv:
        print_status()
        return

    if "--daemon" in sys.argv:
        log.info("Daemon mode: checking every %d s.", CHECK_INTERVAL_SECONDS)
        while True:
            try:
                check_and_act()
            except Exception as exc:
                log.error("Unhandled error: %s", exc)
            time.sleep(CHECK_INTERVAL_SECONDS)
    else:
        check_and_act()


if __name__ == "__main__":
    main()
