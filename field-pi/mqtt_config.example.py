# MQTT configuration for the field station.
#
# Copy this file to mqtt_config.py in the same directory and fill in your
# own broker and topic. mqtt_config.py is not committed — keep the topic private so no one else can publish fake data.
#
# Used by: weather_station.py (publisher)

MQTT_BROKER = "your-broker-hostname-or-ip"   # e.g. a private broker, or your own HiveMQ instance
MQTT_PORT = 1883
MQTT_TOPIC = "your/private/topic/path"        # keep this secret — anyone who knows it can spoof readings
MQTT_CLIENT_ID = "weather_station"            # arbitrary unique string; change if running multiple clients

# Station location — used by night_mode.py to compute local sunrise/sunset.
# Find your coordinates at maps.google.com (right-click → copy coordinates).
STATION_LATITUDE  = 0.0              # decimal degrees, positive = North
STATION_LONGITUDE = 0.0              # decimal degrees, positive = East
STATION_TIMEZONE  = "America/New_York"  # IANA timezone name — see https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
