# Remote SBC Weather Station

A solar-powered, off-grid weather station built around a Raspberry Pi and a
Pimoroni Weather HAT, publishing readings over MQTT to a receiver that
renders self-contained HTML weather pages. Designed for sites with no wired
power or internet — for example, a remote field, rooftop, or outbuilding —
using solar + battery for power and a cellular modem for connectivity.

This repo is a showcase / reference implementation: fork it and adapt the
constants (MQTT broker/topic, field coordinates, file paths) to your own
deployment.

## Architecture

```
FIELD SBC  (field-pi/weather_station.py)     RECEIVER HOST  (server-weather-page/)
────────────────────────────────────────     ─────────────────────────────────────
Raspberry Pi (or similar SBC)                weather-receiver service  (systemd)
+ Pimoroni Weather HAT                       └─ weather_receiver.py
  – BME280: temp / humidity / pressure            MQTT subscriber
  – LTR559: ambient light                         stores readings → SQLite (14-day ring buffer)
  – anemometer: wind speed                        derives: 45-min averages, 16-pt wind direction,
  – wind vane: direction                                   gust/max, SBC-heat temp correction
  – tipping-bucket: rain                          writes self-contained pages to /var/www/html/:
+ MPPT charge controller (Modbus, optional)         weather-widget.html  (compact embed)
+ ST7789 240×240 LCD  (paging display UI)           weather.html         (full page, inline-SVG trends)
+ cellular modem (optional, for off-grid sites)     weather-history.html (14-day log, generated)
+ MeshCore radio (optional)                   Apache/nginx → your-domain/weather.html
  └─ mesh_bot.py  (mesh network query bot)
         │
         ▼  publishes JSON on an interval you choose
    <broker>:<port>  topic <topic>
    (configured in mqtt_config.py — not committed to repo)
```

If deployed off-grid, the station can be configured to only transmit during
daylight hours to conserve power. The receiver computes local sunrise/sunset
from the coordinates you provide and shows "Asleep" instead of "Offline"
during that expected quiet window, so an overnight power-down never gets
mistaken for an outage.

## Repo layout

| Path | What it is |
|---|---|
| `field-pi/weather_station.py` | Field-side script. Reads sensors, drives the LCD, publishes MQTT, and writes a local JSON file for on-device consumers like the mesh bot. |
| `field-pi/night_mode.py` | Sunrise/sunset scheduler. Stops the weather service at sunset and restarts it at sunrise to conserve battery overnight. Run via cron or as a daemon. |
| `field-pi/pi_power_monitor.py` | Systemd service that watches a GPIO pin for an ESP32 shutdown request and initiates a clean OS shutdown before the relay cuts power. |
| `field-pi/pi_power_monitor.service` | Systemd unit file for the above. |
| `esp32/power_manager.py` | MicroPython sketch for an ESP32 that controls a relay on the Pi's 5V supply, sends a graceful-shutdown signal before cutting power, and checks battery voltage before morning power-on. Prevents the SD card corruption that causes boot failures. |
| `field-pi/mesh_bot.py` | Optional MeshCore query-response bot. Answers "wx"/"weather" DMs and a configurable command word on a configurable channel with a one-line weather summary. Reads the local JSON file — no internet dependency. |
| `server-weather-page/weather_receiver.py` | MQTT→HTML receiver. Runs as a systemd service on your web server. |
| `server-weather-page/deploy-receiver.sh` | Deploy script: installs the receiver on the web server, restarts the service. |
| `server-weather-page/healthcheck.sh` | Read-only status checker: service, broker reachability, DB, rendered pages, dawn/dusk window. |
| `ARCHITECTURE.md` | Hardware + data-flow details. |
| `DEPLOYMENT.md` | Install / systemd / deploy instructions. |

## Hardware (example deployment)

| Component | Part |
|---|---|
| SBC | Raspberry Pi 3B (or similar) |
| Sensor HAT | Pimoroni Weather HAT |
| Display | ST7789 240×240 SPI LCD (onboard HAT display) |
| Charge controller (optional) | EPEVER MPPT (Modbus RTU via USB-RS485) |
| Connectivity (optional) | Cellular modem, for sites with no wired internet |
| Mesh radio (optional) | Any MeshCore Companion Radio device |
| Power (optional, off-grid) | Solar panel + charge controller + 12V lead-acid battery |

## Known issues / open items in this reference implementation

- Rain gauge tip-counting relies entirely on the `weatherhat` library's
  interrupt-driven counters — verify your gauge's reed switch/magnet
  alignment during bench testing before trusting rain readings.
- Low-battery clean shutdown is not yet implemented — worth adding for an
  unattended off-grid deployment (read battery SOC from EPEVER; call
  `sudo shutdown -h now` below a safe threshold).

## Deployment

See [DEPLOYMENT.md](./DEPLOYMENT.md) for full instructions. Quick summary:

**Field SBC:** `weather_station.py` and (optionally) `mesh_bot.py` run as systemd services with `Restart=always`.

**Receiver host:** `weather_receiver.py` runs as a systemd service. To deploy an update:
```bash
scp server-weather-page/weather_receiver.py \
    server-weather-page/healthcheck.sh \
    server-weather-page/deploy-receiver.sh  <user>@<host>:~/deploy/
ssh <user>@<host> "cd ~/deploy && bash deploy-receiver.sh"
```
