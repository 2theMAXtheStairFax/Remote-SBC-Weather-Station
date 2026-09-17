# Deployment

This code is a reference implementation. Before running anything, you'll
need to supply your own MQTT broker/topic, field coordinates, and file
paths — see below for what to configure.

## MQTT configuration (required before running anything)

Both the field script and the receiver read their MQTT broker and topic from
a local `mqtt_config.py` file (not committed — keep the broker address and
topic private so no one else can publish fake readings).

**Field SBC:**
```bash
cp field-pi/mqtt_config.example.py /path/to/mqtt_config.py
# edit mqtt_config.py: set MQTT_BROKER, MQTT_PORT, MQTT_TOPIC, MQTT_CLIENT_ID
```

**Receiver host:**
```bash
cp server-weather-page/mqtt_config.example.py /path/to/mqtt_config.py
# edit mqtt_config.py: same broker + topic as the field SBC, different MQTT_CLIENT_ID
```

Both scripts exit with a clear error message if `mqtt_config.py` is missing.

## Dependencies (field SBC)

```bash
sudo apt update
sudo apt install -y python3-pip python3-smbus i2c-tools python3-rpi.gpio fonts-dejavu-core
pip3 install -r field-pi/requirements.txt
# Debian 13+ (PEP 668): pip3 install --break-system-packages -r field-pi/requirements.txt
```

`field-pi/requirements.txt` includes the optional EPEVER/Modbus and MeshCore packages — skip those lines if you're not using those components.

Enable I2C and SPI via `sudo raspi-config` → Interface Options (required for the BME280/LTR559 sensors and the ST7789 display).

## Configuration to fill in before running

In `field-pi/weather_station.py`:
- `LATEST_READING_PATH` — where to write the local JSON reading file (used by `mesh_bot.py`, if you run it).
- `EPEVER_PORT` — your Modbus/RS485 adapter's device path (`ls /dev/serial/by-id/`), if using a charge controller.

In `server-weather-page/weather_receiver.py`:
- `FIELD_LAT` / `FIELD_LON` — your station's coordinates, used to compute local sunrise/sunset for the dawn-to-dusk "Asleep" status.
- `CREDIT_LABEL` / `CREDIT_URL` — optional attribution line at the bottom of each page; leave blank to omit.
- `TEMP_OFFSET_C`, `AVG_WINDOW_MIN`, `GUST_RECENT_HOURS`, `HISTORY_RETENTION_DAYS`, and the gap/offline thresholds — tunable, see the file for descriptions.

## Running it

```bash
python3 weather_station.py
```

## Running as a service (systemd)

Below is an example service file. Replace the `<USER>` sections with your username and adjust file paths accordingly.

```ini
# /etc/systemd/system/weather-station.service
[Unit]
Description=Weather Station
After=network.target

[Service]
Type=simple
User=<USER>
WorkingDirectory=/home/<USER>/weather-station
ExecStart=/usr/bin/python3 /home/<USER>/weather-station/weather_station.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now weather-station
sudo systemctl status weather-station
```

`Restart=always` keeps the station running after reboots and crashes — important for an unattended deployment.

## Server side — MQTT → HTML receiver

`server-weather-page/weather_receiver.py` runs as a systemd service on a web
server. It subscribes to the MQTT topic and renders three HTML pages to
`/var/www/html/`.

**Dependencies:**
```bash
pip3 install -r server-weather-page/requirements.txt   # sqlite3 is stdlib
mkdir -p ~/weather-data                                 # storage for the SQLite history ring buffer
```

**Deploy:**
```bash
scp server-weather-page/weather_receiver.py \
    server-weather-page/healthcheck.sh \
    server-weather-page/deploy-receiver.sh  <user>@<host>:~/deploy/
ssh <user>@<host> "cd ~/deploy && bash deploy-receiver.sh"
```

`deploy-receiver.sh` locates the path the systemd unit runs, refuses to install an older copy over a newer one, backs up the running file, installs the new receiver and `healthcheck.sh`, restarts the service, and prints the journal and a health check.

`healthcheck.sh` is a read-only status script — safe to run any time.

**Output pages** (written to `/var/www/html/`):
- `weather-widget.html` — compact embed, shows averages
- `weather.html` — full page with current readings, averages, gust, and inline-SVG trend charts
- `weather-history.html` — 14-day message log grouped by day (generated at runtime, not committed)

All pages are fully self-contained with no external JS, CSS, or CDN dependencies.

## Field side — MeshCore query-response bot (optional)

`field-pi/mesh_bot.py` runs alongside `weather_station.py`. It answers DMs
containing "wx" or "weather", and responds to a configurable command word on
a configurable channel, with a one-line weather summary. If you deploy this
to a community mesh network, check that network's own bot etiquette
guidelines (many publish one, e.g. Austin Mesh's at
[austinmesh.org/join/bot-guidelines](https://austinmesh.org/join/bot-guidelines/))
and adjust the constants at the top of the file accordingly.

Find the MeshCore node's serial device:
```bash
ls /dev/serial/by-id/
```
Set that path as `MESHCORE_SERIAL_PORT` at the top of `mesh_bot.py`. Confirm your channel's index with `get_channel()` before setting `BOT_CHANNEL_IDX` — indices and names are per-node.

**Install dependency:** included in `field-pi/requirements.txt` — if you installed that already, no separate step is needed.
```bash
# Debian 13+ (PEP 668) standalone install if needed:
pip3 install --break-system-packages meshcore
```

**Example service file:**
```ini
# /etc/systemd/system/mesh-weather-bot.service
[Unit]
Description=Weather Station MeshCore Bot
After=network.target

[Service]
Type=simple
User=<USER>
WorkingDirectory=/home/<USER>/weather-station
ExecStart=/usr/bin/python3 /home/<USER>/weather-station/mesh_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-weather-bot
journalctl -u mesh-weather-bot -f
```
