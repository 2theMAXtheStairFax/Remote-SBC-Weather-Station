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
