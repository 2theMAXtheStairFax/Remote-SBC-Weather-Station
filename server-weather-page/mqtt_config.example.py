# MQTT configuration for the weather receiver (server side).
#
# Copy this file to mqtt_config.py in the same directory and fill in your
# broker and topic. mqtt_config.py is not committed — keep the topic private so no one else can publish fake data.
#
# Used by: weather_receiver.py (subscriber)
# Must match the mqtt_config.py on the field Pi side.

MQTT_BROKER = "your-broker-hostname-or-ip"   # same broker as the field Pi
MQTT_PORT = 1883
MQTT_TOPIC = "your/private/topic/path"        # must match exactly what the field Pi publishes to
MQTT_CLIENT_ID = "weather_receiver"           # arbitrary unique string; distinct from the publisher's ID
