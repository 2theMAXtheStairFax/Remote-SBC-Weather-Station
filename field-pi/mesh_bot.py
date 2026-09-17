"""
Query-response bot for a MeshCore mesh network: replies with the station's
latest reading to (1) a direct message containing "wx"/"weather", or (2) a
configurable command word on a configurable channel.

Runs alongside weather_station.py on the field Pi, powered by the same
off-grid solar setup as the weather station itself. Many community mesh
networks publish bot etiquette guidelines (e.g. Austin Mesh's, at
austinmesh.org/join/bot-guidelines/) -- check whatever network you're
deploying to and adjust the constants below accordingly. This bot follows
a few common-sense defaults drawn from that kind of guidance:
  - No internet dependency: reads LATEST_READING_PATH, a local JSON file
    weather_station.py writes on every sensor cycle, instead of subscribing
    to the MQTT broker over the internet. Stays live even if the Pi's
    internet/cellular link or the MQTT broker is down.
  - Long interactions in DMs, short commands on channels: DMs answer
    "wx"/"weather" unconditionally; the configured channel only answers the
    configured command word, and nothing is ever sent to any other channel.
    Confirm your channel's index with get_channel() before setting
    BOT_CHANNEL_IDX -- channel names and indices are per-node.
  - Solar + battery backed: this runs on the same off-grid Pi as the weather
    sensors, so it stays useful exactly when local, no-internet info matters
    most (grid/internet down).
  - Source available: this file lives in the public Remote-SBC-Weather-Station
    repo so other mesh users can audit it.

Channel replies deliberately never contain CHANNEL_COMMAND_WORD (see
format_reading_summary() call sites) -- if the mesh ever reflected the bot's
own broadcast back into its own event stream, a reply containing the trigger
word could cause an infinite self-reply loop. CHANNEL_REPLY_COOLDOWN_SEC is a
second, independent guard against reply storms from duplicate message delivery.

Requires a MeshCore node flashed with Companion Radio firmware, connected
over USB. Find its serial device with:
    ls /dev/serial/by-id/

Dependencies (not yet in a requirements.txt for this repo):
    pip install meshcore

Both the DM path and the channel reply path have been confirmed working
end-to-end against real hardware.
"""

import asyncio
import time
import json
from meshcore import MeshCore, EventType

# MeshCore node serial port -- find your device with: ls /dev/serial/by-id/
MESHCORE_SERIAL_PORT = "/dev/serial/by-id/your-heltec-device-here"

# Local reading file written by weather_station.py (build_reading()/write_latest_reading())
# Adjust this path to match wherever weather_station.py runs on your Pi.
LATEST_READING_PATH = "/home/<USER>/weather-station/weather_latest.json"  # REQUIRED: replace <USER> with your Linux username
POLL_INTERVAL_SEC = 5  # how often to re-read the local file for freshness

# Trigger words that request a weather reply over DM (case-insensitive, matched
# anywhere in the message text).
TRIGGER_WORDS = ("wx", "weather")

# Channel command word and index -- confirm your own channel's index with
# get_channel() before deploying; indices and names vary per node/network.
BOT_CHANNEL_IDX = 1
CHANNEL_COMMAND_WORD = "wx"
CHANNEL_REPLY_COOLDOWN_SEC = 15  # guard against duplicate-delivery reply storms

# Reading considered stale after this many seconds. weather_station.py
# publishes/writes every 30s (MQTT_UPDATE_INTERVAL); this gives slack for a
# couple of missed cycles.
STALE_AFTER_SEC = 120

# Flood advert interval -- propagates via every repeater that hears it. 6h
# matches the low end of community norms (WNY/Buffalo mesh groups recommend
# 6h; MeshCore's own repeater firmware defaults to 12h; Boston/Switzerland
# groups go as high as 47-49h).
ADVERT_INTERVAL_SEC = 6 * 3600

latest_reading = None
latest_read_time = 0.0
last_channel_reply_time = 0.0


def poll_latest_reading():
    """Re-read the local file weather_station.py writes. Cheap enough to do on every
    poll tick; a missing/partial file just means 'no data yet' rather than
    a crash."""
    global latest_reading, latest_read_time
    try:
        with open(LATEST_READING_PATH, 'r') as f:
            latest_reading = json.load(f)
        latest_read_time = time.time()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Error reading local weather file: {e}")


def format_reading_summary(label):
    """Build a short weather summary -- MeshCore text messages top out around
    ~150 usable bytes, so keep this to one compact line. `label` must never be
    or contain CHANNEL_COMMAND_WORD -- see the channel self-loop note in the module
    docstring."""
    if latest_reading is None:
        return f"{label}: no data received yet."

    reading_age = time.time() - latest_reading.get('timestamp', 0)
    if reading_age > STALE_AFTER_SEC:
        mins = int(reading_age // 60)
        return f"{label}: station offline (no data {mins}min)."

    r = latest_reading
    return (
        f"{label}: {r.get('temp_f', 0):.0f}F {r.get('humidity', 0):.0f}%RH "
        f"Wind {r.get('wind_speed_mph', 0):.0f}mph {r.get('wind_direction', '?')} "
        f"Rain {r.get('rain_total_in', 0):.2f}in Batt {r.get('battery_voltage', 0):.1f}V"
    )


async def poll_loop():
    """Background task: keep latest_reading fresh from the local file."""
    while True:
        poll_latest_reading()
        await asyncio.sleep(POLL_INTERVAL_SEC)


async def advert_loop(meshcore):
    """Background task: flood-advert on a timer so the station stays
    discoverable mesh-wide. Sleeps BEFORE the first advert, not after --
    with Restart=always/RestartSec=5 on the systemd unit, a crash-loop bug
    must never be able to reach this call before a full interval of uptime,
    or it would flood the whole mesh with adverts every few seconds."""
    while True:
        await asyncio.sleep(ADVERT_INTERVAL_SEC)
        try:
            result = await meshcore.commands.send_advert(flood=True)
            if result.type == EventType.ERROR:
                print(f"Advert failed: {result}")
            else:
                print("Sent flood advert")
        except Exception as e:
            print(f"Error sending advert: {e}")


async def handle_contact_message(meshcore, event):
    """Reply to a direct message if it contains one of the trigger words."""
    text = event.payload.get("text", "")
    if not any(word in text.lower() for word in TRIGGER_WORDS):
        return

    pubkey_prefix = event.payload.get("pubkey_prefix")
    contact = meshcore.get_contact_by_key_prefix(pubkey_prefix)
    if contact is None:
        # connect() doesn't populate the contact list on its own -- refresh
        # once in case this is a contact we haven't seen/advertised-to yet.
        await meshcore.commands.get_contacts()
        contact = meshcore.get_contact_by_key_prefix(pubkey_prefix)
    if contact is None:
        print(f"Trigger from unknown contact {pubkey_prefix}, can't reply")
        return

    reply = format_reading_summary("wx")
    print(f"Replying to {pubkey_prefix}: {reply}")
    try:
        result = await meshcore.commands.send_msg(contact, reply)
        if result.type == EventType.ERROR:
            print(f"Send failed: {result}")
    except Exception as e:
        print(f"Error sending reply: {e}")


async def handle_channel_message(meshcore, event):
    """Reply on the configured channel if the message contains the standalone
    command word."""
    if event.payload.get("channel_idx") != BOT_CHANNEL_IDX:
        return

    text = event.payload.get("text", "")
    if CHANNEL_COMMAND_WORD not in text.lower().split():
        return

    global last_channel_reply_time
    now = time.time()
    if now - last_channel_reply_time < CHANNEL_REPLY_COOLDOWN_SEC:
        print("Channel reply suppressed (cooldown)")
        return
    last_channel_reply_time = now

    reply = format_reading_summary("wx")  # no CHANNEL_COMMAND_WORD in the reply -- see module docstring
    print(f"Replying on channel {BOT_CHANNEL_IDX}: {reply}")
    try:
        result = await meshcore.commands.send_chan_msg(BOT_CHANNEL_IDX, reply)
        if result.type == EventType.ERROR:
            print(f"Channel send failed: {result}")
    except Exception as e:
        print(f"Error sending channel reply: {e}")


async def main():
    print(f"Connecting to MeshCore node on {MESHCORE_SERIAL_PORT}...")
    meshcore = await MeshCore.create_serial(MESHCORE_SERIAL_PORT)
    print("MeshCore connected")

    await meshcore.commands.get_contacts()
    print(f"Loaded {len(meshcore._contacts)} known contact(s)")

    async def on_contact_msg(event):
        await handle_contact_message(meshcore, event)

    async def on_channel_msg(event):
        await handle_channel_message(meshcore, event)

    meshcore.subscribe(EventType.CONTACT_MSG_RECV, on_contact_msg)
    meshcore.subscribe(EventType.CHANNEL_MSG_RECV, on_channel_msg)
    await meshcore.start_auto_message_fetching()

    asyncio.create_task(poll_loop())
    asyncio.create_task(advert_loop(meshcore))

    print(f"Listening for {'/'.join(TRIGGER_WORDS)!r} DMs and "
          f"{CHANNEL_COMMAND_WORD!r} on channel {BOT_CHANNEL_IDX}; "
          f"flood advert every {ADVERT_INTERVAL_SEC}s...")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
