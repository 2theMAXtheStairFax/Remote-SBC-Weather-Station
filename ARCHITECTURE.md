# Architecture

## Overview

```
 ┌─────────────────────────────────────────────────────┐
 │                Pimoroni Weather HAT                  │
 │                                                       │
 │  BME280 (I2C 0x76) ── temp / humidity / pressure     │
 │  LTR559 (I2C 0x23)  ── ambient light (lux)           │
 │  Anemometer (GPIO)  ── wind speed pulses              │
 │  Rain gauge (GPIO)  ── tipping-bucket pulses           │
 │  Wind vane (ADC)    ── wind direction                  │
 │  ST7789 LCD (SPI, 240x240) ── onboard status display  │
 │  4x buttons (labeled A/B/X/Y)                          │
 └─────────────────────┬─────────────────────────────────┘
                        │ I2C / SPI / GPIO
 ┌─────────────────────▼─────────────────────────────────┐
 │              SBC main loop (Python, weather_station.py)│
 │                                                         │
 │  - Reads sensors via the official `weatherhat` library │
 │  - Optionally reads a charge controller over Modbus    │
 │  - Renders paged UI to the LCD                          │
 │  - Publishes a JSON reading over MQTT on an interval    │
 └─────────────────────┬───────────────────────────────────┘
                        │ MQTT publish
                        ▼
              <broker>:<port>  topic <topic>
                        │
                        ▼
              Receiver host (see below)
```

## Server side — MQTT → HTML receiver (`server-weather-page/weather_receiver.py`)

Runs as a systemd service on a web server. Subscriber only — no sensor code.

```
 field SBC ──MQTT──▶  <broker>:<port>  topic <topic>
                      (configured in mqtt_config.py — not committed to repo)
                          │
                          ▼
             weather_receiver.py  (paho-mqtt subscriber)
                          │  on each message:
                          ├─▶ store RAW reading ──▶ SQLite  ~/weather-data/weather.db
                          │                          (one row per message, 14-day ring buffer)
                          │
                          └─▶ read back from DB, derive at render time:
                                • TEMP_OFFSET_C  (SBC self-heat compensation; dewpoint recomputed via Magnus)
                                • rolling averages (AVG_WINDOW_MIN)
                                • 16-point wind direction (speed-weighted vector avg of raw cardinals)
                                • gust/max: since local midnight + last few hours
                                • sunrise/sunset (built-in almanac) → "Asleep" vs "Offline",
                                  and nightly gaps shown as one "overnight shutdown" row
                              then write self-contained pages:
                                /var/www/html/weather-widget.html   ── averages only (compact embed),  every msg
                                /var/www/html/weather.html          ── now + avg + gust + inline-SVG trends,  every msg
                                /var/www/html/weather-history.html  ── every stored message, grouped by day;  rate-limited (large file)
```

The pages are fully self-contained (trend charts are server-rendered inline
SVG; no external JS/CSS/CDN dependencies), so they can be embedded or linked
from any site regardless of that site's own Content-Security-Policy.

## Power & connectivity (off-grid deployment)

- Solar PV panel + charge controller + battery bank can power the whole
  station where there's no wired power.
- Profile the station's component power draw to size the battery for
  extended cloud cover.
- A cellular modem provides the uplink where there's no wired internet.
  Mind the data plan's cap — publish cadence and payload size should be
  chosen with that budget in mind (see `MQTT_UPDATE_INTERVAL` in
  `weather_station.py`).

## Data flow inside the field script

1. **Sensor read loop** (`SENSOR_UPDATE_INTERVAL`) — reads the Weather HAT via the `weatherhat` library.
2. **Display loop** (`DISPLAY_UPDATE_INTERVAL`) — redraws the current page on the LCD for button responsiveness; independent of the sensor interval so the UI stays snappy even if sensor reads are slower.
3. **MQTT publish loop** (`MQTT_UPDATE_INTERVAL`) — builds the current reading, writes it locally, and publishes it.
4. **Button polling** — cycles the paged display; debounced with a short sleep after each press.

All loops run inside a single `while True` in `main()`, gated by `time.time()` deltas rather than separate threads — a slow sensor read can delay display/publish timing slightly. Worth revisiting with real threading if display responsiveness ever feels laggy.
