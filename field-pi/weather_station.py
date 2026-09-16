import time
import datetime
import os
import weatherhat
import st7789
from PIL import Image, ImageDraw, ImageFont
import RPi.GPIO as GPIO
import paho.mqtt.client as mqtt
import json
import minimalmodbus
import serial

# Configuration
HTML_PATH = "/var/www/html/index.html"
# Local copy of the latest reading -- no internet/MQTT dependency, read directly
# by on-Pi consumers such as mesh_bot.py (some mesh-network bot guidelines
# ask bots to avoid internet dependencies when a local path is available).
LATEST_READING_PATH = "/home/<USER>/weather-station/weather_latest.json"
SENSOR_UPDATE_INTERVAL = 1.0   # seconds - read sensors frequently
HTML_UPDATE_INTERVAL = 10.0    # seconds
DISPLAY_UPDATE_INTERVAL = 0.5  # seconds (for display refresh)
DISPLAY_TIMEOUT = 10.0         # seconds - auto turn off display after inactivity
MQTT_UPDATE_INTERVAL = 30.0    # seconds - send data via MQTT

# MQTT Configuration — loaded from mqtt_config.py (not committed).
# Copy field-pi/mqtt_config.example.py → mqtt_config.py next to this script and fill in your details.
try:
    from mqtt_config import MQTT_BROKER, MQTT_PORT, MQTT_TOPIC, MQTT_CLIENT_ID
except ImportError:
    raise SystemExit(
        "ERROR: mqtt_config.py not found.\n"
        "Copy field-pi/mqtt_config.example.py to the same directory as this script,\n"
        "rename it mqtt_config.py, and fill in your MQTT broker and topic."
    )

# EPEVER Charge Controller Configuration
EPEVER_PORT = '/dev/serial/by-id/usb-YOUR_RS485_ADAPTER-if00'  # find yours with: ls /dev/serial/by-id/
EPEVER_SLAVE_ADDRESS = 1       # Default Modbus address
EPEVER_BAUDRATE = 115200       # Default for EPEVER

# Battery voltage thresholds (for display status)
BATTERY_FULL_VOLTAGE = 13.6    # Fully charged 12V lead acid
BATTERY_LOW_VOLTAGE = 11.8     # Low battery warning
BATTERY_CRITICAL_VOLTAGE = 11.0  # Critical battery level

# Display configuration
DISPLAY_WIDTH = 240
DISPLAY_HEIGHT = 240
SPI_SPEED_MHZ = 80

# Button configuration
BUTTONS = [5, 6, 16, 24]
LABELS = ["A", "B", "X", "Y"]

# Colors
COLOR_WHITE = (255, 255, 255)
COLOR_BLUE = (31, 137, 251)
COLOR_GREEN = (99, 255, 124)
COLOR_YELLOW = (254, 219, 82)
COLOR_RED = (247, 0, 63)
COLOR_BLACK = (0, 0, 0)
COLOR_GREY = (100, 100, 100)

# HTML template
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Remote Weather Station</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        .weather-data {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .timestamp {{
            color: #666;
            font-size: 0.9em;
        }}
        .metric {{
            margin: 8px 0;
        }}
    </style>
</head>
<body>
    <div class="weather-data">
        <h1>Remote Weather Station</h1>
        <p class="timestamp">Current conditions as of: {timestamp}</p>
        <div class="metric"><strong>Temperature:</strong> {temperature:.2f} °C ({temperature_f:.2f} °F)</div>
        <div class="metric"><strong>Humidity:</strong> {humidity:.2f} %</div>
        <div class="metric"><strong>Dewpoint:</strong> {dewpoint:.2f} °C ({dewpoint_f:.2f} °F)</div>
        <div class="metric"><strong>Pressure:</strong> {pressure:.2f} inHg ({pressure_hpa:.2f} hPa)</div>
        <div class="metric"><strong>Light:</strong> {lux:.2f} Lux</div>
        <div class="metric"><strong>Wind Speed (avg):</strong> {wind_speed:.2f} mph</div>
        <div class="metric"><strong>Wind Direction (avg):</strong> {wind_direction}</div>
        <div class="metric"><strong>Rain Rate:</strong> {rain_rate:.2f} in/hr</div>
        <div class="metric"><strong>Rain Total:</strong> {rain_total:.2f} in</div>
        <div class="metric"><strong>Battery Voltage:</strong> {battery_voltage:.2f} V</div>
    </div>
</body>
</html>"""


def celsius_to_fahrenheit(celsius):
    """Convert Celsius to Fahrenheit."""
    return (celsius * 9/5) + 32


def hpa_to_inhg(hpa):
    """Convert hPa to inches of mercury."""
    return hpa * 0.02953


def mm_to_inches(mm):
    """Convert millimeters to inches."""
    return mm * 0.0393701


def get_battery_status(voltage):
    """Determine battery status from voltage."""
    if voltage >= BATTERY_FULL_VOLTAGE:
        return "FULL", COLOR_GREEN
    elif voltage >= BATTERY_LOW_VOLTAGE:
        return "GOOD", COLOR_GREEN
    elif voltage >= BATTERY_CRITICAL_VOLTAGE:
        return "LOW", COLOR_YELLOW
    else:
        return "CRITICAL", COLOR_RED


def get_battery_percentage(voltage):
    """Calculate approximate battery percentage for 12V lead acid."""
    if voltage >= BATTERY_FULL_VOLTAGE:
        return 100
    elif voltage <= BATTERY_CRITICAL_VOLTAGE:
        return 0
    else:
        percentage = ((voltage - BATTERY_CRITICAL_VOLTAGE) /
                     (BATTERY_FULL_VOLTAGE - BATTERY_CRITICAL_VOLTAGE)) * 100
        return int(percentage)


def connect_epever():
    """Connect to EPEVER charge controller via Modbus."""
    try:
        controller = minimalmodbus.Instrument(EPEVER_PORT, EPEVER_SLAVE_ADDRESS)
        controller.serial.baudrate = EPEVER_BAUDRATE
        controller.serial.bytesize = 8
        controller.serial.parity = serial.PARITY_NONE
        controller.serial.stopbits = 1
        controller.serial.timeout = 1
        controller.mode = minimalmodbus.MODE_RTU

        # Test connection by reading battery voltage - USE FUNCTION CODE 4
        test_voltage = controller.read_register(0x3104, number_of_decimals=1, functioncode=4) / 10.0
        print(f"EPEVER connected - Battery: {test_voltage:.2f}V")
        return controller
    except Exception as e:
        print(f"EPEVER connection failed: {e}")
        return None


def read_epever_data(controller):
    """Read battery, solar, and charging data from EPEVER controller."""
    try:
        if controller is None:
            return {
                'battery_voltage': 0.0,
                'battery_current': 0.0,
                'battery_power': 0.0,
                'battery_temp': 0.0,
                'battery_soc': 0,
                'solar_voltage': 0.0,
                'solar_current': 0.0,
                'solar_power': 0.0,
                'load_voltage': 0.0,
                'load_current': 0.0,
                'load_power': 0.0
            }

        # Read battery data - Use number_of_decimals parameter and functioncode=4
        battery_voltage = controller.read_register(0x3104, number_of_decimals=1, functioncode=4) / 10.0
        battery_current = controller.read_register(0x3105, number_of_decimals=1, functioncode=4) / 10.0
        battery_temp = controller.read_register(0x3110, number_of_decimals=1, functioncode=4, signed=True) / 10.0
        battery_soc = controller.read_register(0x311A, number_of_decimals=0, functioncode=4)

        # Read solar panel data
        solar_voltage = controller.read_register(0x3100, number_of_decimals=1, functioncode=4) / 10.0
        solar_current = controller.read_register(0x3101, number_of_decimals=1, functioncode=4) / 10.0

        # Read load data
        load_voltage = controller.read_register(0x310C, number_of_decimals=1, functioncode=4) / 10.0
        load_current = controller.read_register(0x310D, number_of_decimals=1, functioncode=4) / 10.0

        # Calculate power values (V * A = W)
        battery_power = battery_voltage * battery_current
        solar_power = solar_voltage * solar_current
        load_power = load_voltage * load_current

        return {
            'battery_voltage': battery_voltage,
            'battery_current': battery_current,
            'battery_power': battery_power,
            'battery_temp': battery_temp,
            'battery_soc': battery_soc,
            'solar_voltage': solar_voltage,
            'solar_current': solar_current,
            'solar_power': solar_power,
            'load_voltage': load_voltage,
            'load_current': load_current,
            'load_power': load_power
        }

    except Exception as e:
        print(f"Error reading EPEVER: {e}")
        return {
            'battery_voltage': 0.0,
            'battery_current': 0.0,
            'battery_power': 0.0,
            'battery_temp': 0.0,
            'battery_soc': 0,
            'solar_voltage': 0.0,
            'solar_current': 0.0,
            'solar_power': 0.0,
            'load_voltage': 0.0,
            'load_current': 0.0,
            'load_power': 0.0
        }


def connect_mqtt():
    """Connect to MQTT broker."""
    try:
        client = mqtt.Client(MQTT_CLIENT_ID)
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        print(f"MQTT connected to {MQTT_BROKER}")
        return client
    except Exception as e:
        print(f"MQTT connection failed: {e}")
        return None


def build_reading(sensor, battery_data):
    """Assemble the current reading as the shared dict shape used by both the
    MQTT payload and the local file (LATEST_READING_PATH) that on-Pi
    consumers like mesh_bot.py read from directly."""
    wind_speed_mph = sensor.wind_speed * 2.23694
    rain_total_in = mm_to_inches(sensor.rain_total)
    rain_rate_iph = sensor.rain * 0.0393701 * 3600
    pressure_inhg = hpa_to_inhg(sensor.pressure)
    temp_f = celsius_to_fahrenheit(sensor.temperature)
    dewpoint_f = celsius_to_fahrenheit(sensor.dewpoint)
    wind_direction = sensor.degrees_to_cardinal(sensor.wind_direction)

    return {
        'temp_c': round(sensor.temperature, 2),
        'temp_f': round(temp_f, 2),
        'humidity': round(sensor.humidity, 2),
        'dewpoint_c': round(sensor.dewpoint, 2),
        'dewpoint_f': round(dewpoint_f, 2),
        'pressure_hpa': round(sensor.pressure, 2),
        'pressure_inhg': round(pressure_inhg, 2),
        'light_lux': round(sensor.lux, 2),
        'wind_speed_mph': round(wind_speed_mph, 2),
        'wind_direction': wind_direction,
        'rain_rate_iph': round(rain_rate_iph, 2),
        'rain_total_in': round(rain_total_in, 2),
        'battery_voltage': round(battery_data.get('battery_voltage', 0), 2),
        'battery_current': round(battery_data.get('battery_current', 0), 2),
        'battery_power': round(battery_data.get('battery_power', 0), 1),
        'battery_temp': round(battery_data.get('battery_temp', 0), 1),
        'battery_soc': battery_data.get('battery_soc', 0),
        'solar_voltage': round(battery_data.get('solar_voltage', 0), 2),
        'solar_current': round(battery_data.get('solar_current', 0), 2),
        'solar_power': round(battery_data.get('solar_power', 0), 1),
        'load_voltage': round(battery_data.get('load_voltage', 0), 2),
        'load_current': round(battery_data.get('load_current', 0), 2),
        'load_power': round(battery_data.get('load_power', 0), 1),
        'timestamp': int(time.time())
    }


def write_latest_reading(data, path):
    """Write the reading to a local file, atomically, so a concurrent reader
    (e.g. the MeshCore bot) never sees a half-written file. This is the
    on-Pi data source that has no internet dependency -- it stays fresh
    even if the MQTT broker or the Pi's internet link is down."""
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"Error writing local reading file: {e}")


def send_mqtt_data(mqtt_client, sensor, battery_data):
    """Build the current reading, write it locally, and publish via MQTT."""
    try:
        data = build_reading(sensor, battery_data)
    except Exception as e:
        print(f"Error building reading: {e}")
        return

    write_latest_reading(data, LATEST_READING_PATH)

    if mqtt_client is None:
        return

    try:
        payload = json.dumps(data)
        result = mqtt_client.publish(MQTT_TOPIC, payload, qos=1)

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(f"MQTT sent: {len(payload)} bytes - Temp: {data['temp_f']:.1f}°F, Battery: {data['battery_voltage']:.2f}V, Solar: {data['solar_power']:.1f}W")
        else:
            print(f"MQTT send failed with code: {result.rc}")

    except Exception as e:
        print(f"MQTT send error: {e}")


def draw_battery_page(draw, font, battery_data):
    """Draw battery and solar monitoring page."""
    draw.rectangle((0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT), fill=COLOR_BLACK)

    battery_voltage = battery_data.get('battery_voltage', 0.0)
    battery_soc = battery_data.get('battery_soc', 0)
    battery_status, battery_color = get_battery_status(battery_voltage)

    # Draw title bar
    draw.rectangle((0, 0, DISPLAY_WIDTH, 30), fill=battery_color)
    draw.text((10, 5), "Power Monitor", font=font, fill=COLOR_BLACK)

    # Draw timestamp
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    draw.text((10, 35), timestamp, font=font, fill=COLOR_GREY)

    # Draw battery and solar data
    y_position = 65
    draw.text((10, y_position), f"Batt: {battery_voltage:.2f}V {battery_soc}%", font=font, fill=battery_color)
    y_position += 30
    draw.text((10, y_position), f"Solar: {battery_data.get('solar_voltage', 0):.1f}V {battery_data.get('solar_power', 0):.1f}W", font=font, fill=COLOR_YELLOW)
    y_position += 30
    draw.text((10, y_position), f"Load: {battery_data.get('load_voltage', 0):.1f}V {battery_data.get('load_power', 0):.1f}W", font=font, fill=COLOR_GREEN)
    y_position += 30
    draw.text((10, y_position), f"Temp: {battery_data.get('battery_temp', 0):.1f}°C", font=font, fill=COLOR_WHITE)

    # Draw button hint
    draw.text((10, DISPLAY_HEIGHT - 25), "Y: Exit Power View", font=font, fill=COLOR_GREY)


def get_display_pages(sensor):
    """Return list of display pages with sensor data."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")

    temp_f = celsius_to_fahrenheit(sensor.temperature)
    dewpoint_f = celsius_to_fahrenheit(sensor.dewpoint)
    pressure_inhg = hpa_to_inhg(sensor.pressure)
    wind_speed_mph = sensor.wind_speed * 2.23694
    rain_rate_iph = sensor.rain * 0.0393701 * 3600
    rain_total_in = mm_to_inches(sensor.rain_total)
    wind_direction = sensor.degrees_to_cardinal(sensor.wind_direction)

    pages = [
        {
            "title": "Temperature",
            "data": [
                f"{sensor.temperature:.1f}°C",
                f"{temp_f:.1f}°F",
                f"",
                f"Humidity: {sensor.humidity:.1f}%"
            ],
            "color": COLOR_RED
        },
        {
            "title": "Pressure",
            "data": [
                f"{pressure_inhg:.2f} inHg",
                f"{sensor.pressure:.1f} hPa",
                f"",
                f"Light: {sensor.lux:.0f} Lux"
            ],
            "color": COLOR_YELLOW
        },
        {
            "title": "Wind",
            "data": [
                f"Speed:",
                f"{wind_speed_mph:.1f} mph",
                f"",
                f"Dir: {wind_direction}"
            ],
            "color": COLOR_BLUE
        },
        {
            "title": "Rain",
            "data": [
                f"Rate:",
                f"{rain_rate_iph:.2f} in/hr",
                f"",
                f"Total: {rain_total_in:.2f} in"
            ],
            "color": COLOR_GREEN
        },
        {
            "title": "Summary",
            "data": [
                f"{sensor.temperature:.0f}°C {temp_f:.0f}°F H:{sensor.humidity:.0f}%",
                f"{pressure_inhg:.2f}inHg",
                f"W:{wind_speed_mph:.1f}mph {wind_direction}",
                f"R:{rain_total_in:.2f}in"
            ],
            "color": COLOR_WHITE
        }
    ]

    return pages


def draw_display_page(draw, font, page, page_num, total_pages, timestamp):
    """Draw a single page on the display."""
    draw.rectangle((0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT), fill=COLOR_BLACK)

    # Draw title bar
    draw.rectangle((0, 0, DISPLAY_WIDTH, 30), fill=page["color"])
    draw.text((10, 5), page["title"], font=font, fill=COLOR_BLACK)

    # Draw page indicator
    page_text = f"{page_num + 1}/{total_pages}"
    draw.text((DISPLAY_WIDTH - 50, 5), page_text, font=font, fill=COLOR_BLACK)

    # Draw timestamp
    draw.text((10, 35), timestamp, font=font, fill=COLOR_GREY)

    # Draw data lines
    y_position = 70
    for line in page["data"]:
        draw.text((10, y_position), line, font=font, fill=COLOR_WHITE)
        y_position += 35

    # Draw button hints
    draw.text((10, DISPLAY_HEIGHT - 25), "A/B: Prev/Next", font=font, fill=COLOR_GREY)


def write_html(sensor, battery_data, path):
    """Write current sensor data to HTML file."""
    return
    try:
        timestamp = datetime.datetime.now().strftime("%x %X")
        temp_f = celsius_to_fahrenheit(sensor.temperature)
        dewpoint_f = celsius_to_fahrenheit(sensor.dewpoint)
        pressure_inhg = hpa_to_inhg(sensor.pressure)
        wind_speed_mph = sensor.wind_speed * 2.23694
        rain_rate_iph = sensor.rain * 0.0393701 * 3600
        rain_total_in = mm_to_inches(sensor.rain_total)
        wind_direction = sensor.degrees_to_cardinal(sensor.wind_direction)

        html_content = HTML_TEMPLATE.format(
            timestamp=timestamp,
            temperature=sensor.temperature,
            temperature_f=temp_f,
            humidity=sensor.humidity,
            dewpoint=sensor.dewpoint,
            dewpoint_f=dewpoint_f,
            pressure=pressure_inhg,
            pressure_hpa=sensor.pressure,
            lux=sensor.lux,
            wind_speed=wind_speed_mph,
            wind_direction=wind_direction,
            rain_rate=rain_rate_iph,
            rain_total=rain_total_in,
            battery_voltage=battery_data.get('battery_voltage', 0)
        )

        with open(path, 'w') as file:
            file.write(html_content)

        # print(f"Updated HTML at {timestamp}")

    except Exception as e:
        print(f"Error writing HTML: {e}")


def main():
    """Main loop to update sensor, display, and HTML file."""
    current_page = 0
    last_sensor_update = 0
    last_html_update = 0
    last_display_update = 0
    last_mqtt_update = 0
    last_activity_time = time.time()
    display_on = True
    showing_battery = False

    # Battery data storage
    battery_data = {
        'battery_voltage': 0.0,
        'battery_current': 0.0,
        'battery_power': 0.0,
        'battery_temp': 0.0,
        'solar_voltage': 0.0,
        'solar_current': 0.0,
        'solar_power': 0.0,
        'load_voltage': 0.0,
        'load_current': 0.0,
        'load_power': 0.0
    }

    try:
        # Initialize Weather HAT
        print("Initializing Weather HAT...")
        sensor = weatherhat.WeatherHAT()
        print("Weather HAT initialized successfully")

        # Do initial sensor update
        print("Reading initial sensor data...")
        sensor.update(interval=5.0)
        print(f"Initial temp: {sensor.temperature:.1f}°C, Pressure: {sensor.pressure:.1f}hPa")

        # Initialize EPEVER charge controller
        try:
            print("Connecting to EPEVER charge controller...")
            epever = connect_epever()
            if epever:
                battery_data = read_epever_data(epever)
                print(f"EPEVER data: Battery {battery_data['battery_voltage']:.2f}V, Solar {battery_data['solar_voltage']:.1f}V, Charging {battery_data['battery_current']:.2f}A")
                battery_available = True
            else:
                print("EPEVER not available - battery monitoring disabled")
                battery_available = False
        except Exception as e:
            print(f"EPEVER initialization failed: {e}")
            epever = None
            battery_available = False

        # Connect to MQTT
        print("Connecting to MQTT broker...")
        mqtt_client = connect_mqtt()

        # Initialize display
        print("Initializing ST7789 display...")
        display = st7789.ST7789(
            height=DISPLAY_WIDTH,
            width=DISPLAY_HEIGHT,
            rotation=90,
            port=0,
            cs=1,
            dc=9,
            backlight=12,
            spi_speed_hz=SPI_SPEED_MHZ * 1000000
        )
        display.begin()
        print("Display initialized successfully")

        # Test display
        print("Drawing test pattern...")
        test_img = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), color=COLOR_RED)
        test_draw = ImageDraw.Draw(test_img)
        test_draw.rectangle((20, 20, 220, 220), fill=COLOR_BLUE)
        test_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
        test_draw.text((60, 100), "TESTING", font=test_font, fill=COLOR_WHITE)
        display.display(test_img)
        print("Test pattern displayed")
        time.sleep(3)

        # Setup GPIO for buttons
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for button in BUTTONS:
            GPIO.setup(button, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # Create image and drawing objects
        image = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), color=COLOR_BLACK)
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)

        print("Starting main loop...")
        print("Button A: Previous page")
        print("Button B: Next page")
        print("Button X: Toggle display ON/OFF")
        print("Button Y: Show power monitor" if battery_available else "Button Y: Power monitor not available")
        print(f"Display auto-sleep: {DISPLAY_TIMEOUT}s")
        print(f"MQTT update interval: {MQTT_UPDATE_INTERVAL}s ({MQTT_UPDATE_INTERVAL/60}min)")

        while True:
            try:
                current_time = time.time()

                # Update sensor readings periodically
                if current_time - last_sensor_update >= SENSOR_UPDATE_INTERVAL:
                    sensor.update(interval=1.0)

                    # Update battery data if available
                    if battery_available:
                        battery_data = read_epever_data(epever)

                    last_sensor_update = current_time

                # Update HTML periodicalinaly
                if current_time - last_html_update >= HTML_UPDATE_INTERVAL:
                    write_html(sensor, battery_data, HTML_PATH)
                    last_html_update = current_time

                # Send MQTT data periodically
                if current_time - last_mqtt_update >= MQTT_UPDATE_INTERVAL:
                    send_mqtt_data(mqtt_client, sensor, battery_data)
                    last_mqtt_update = current_time

                # Auto-sleep display after timeout
                if display_on and not showing_battery and (current_time - last_activity_time >= DISPLAY_TIMEOUT):
                    display_on = False
                    blank_img = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), color=COLOR_BLACK)
                    display.display(blank_img)
                    GPIO.setup(12, GPIO.OUT)
                    GPIO.output(12, GPIO.LOW)
                    print("Display auto-sleep (10s inactivity)")

                # Update display
                if current_time - last_display_update >= DISPLAY_UPDATE_INTERVAL:
                    if display_on:
                        if showing_battery:
                            draw_battery_page(draw, font, battery_data)
                            display.display(image)
                        else:
                            pages = get_display_pages(sensor)
                            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                            draw_display_page(draw, font, pages[current_page], current_page, len(pages), timestamp)
                            display.display(image)
                    last_display_update = current_time

                # Check for button presses
                if not GPIO.input(5):  # Button A
                    last_activity_time = current_time
                    if not display_on:
                        display_on = True
                        GPIO.setup(12, GPIO.OUT)
                        GPIO.output(12, GPIO.HIGH)
                        print("Display woken up")
                    elif display_on and not showing_battery:
                        current_page = (current_page - 1) % len(get_display_pages(sensor))
                        print(f"Switched to page {current_page + 1}")
                    time.sleep(0.2)

                if not GPIO.input(6):  # Button B
                    last_activity_time = current_time
                    if not display_on:
                        display_on = True
                        GPIO.setup(12, GPIO.OUT)
                        GPIO.output(12, GPIO.HIGH)
                        print("Display woken up")
                    elif display_on and not showing_battery:
                        current_page = (current_page + 1) % len(get_display_pages(sensor))
                        print(f"Switched to page {current_page + 1}")
                    time.sleep(0.2)

                if not GPIO.input(16):  # Button X
                    last_activity_time = current_time
                    display_on = not display_on
                    if display_on:
                        GPIO.setup(12, GPIO.OUT)
                        GPIO.output(12, GPIO.HIGH)
                        print("Display turned ON")
                        if showing_battery:
                            draw_battery_page(draw, font, battery_data)
                        else:
                            pages = get_display_pages(sensor)
                            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                            draw_display_page(draw, font, pages[current_page], current_page, len(pages), timestamp)
                        display.display(image)
                    else:
                        blank_img = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), color=COLOR_BLACK)
                        display.display(blank_img)
                        GPIO.setup(12, GPIO.OUT)
                        GPIO.output(12, GPIO.LOW)
                        print("Display turned OFF")
                    time.sleep(0.3)

                if not GPIO.input(24):  # Button Y
                    last_activity_time = current_time
                    if battery_available:
                        if not display_on:
                            display_on = True
                            GPIO.setup(12, GPIO.OUT)
                            GPIO.output(12, GPIO.HIGH)
                        showing_battery = not showing_battery
                        if showing_battery:
                            print("Showing power monitor")
                        else:
                            print("Returning to weather pages")
                    else:
                        print("Power monitor not available")
                    time.sleep(0.3)

                time.sleep(0.05)

            except KeyboardInterrupt:
                print("\nShutting down gracefully...")
                break
            except Exception as e:
                print(f"Error in main loop: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(1.0)

    except Exception as e:
        print(f"Failed to initialize: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            if mqtt_client:
                mqtt_client.loop_stop()
                mqtt_client.disconnect()
                print("MQTT disconnected")
        except:
            pass
        try:
            GPIO.cleanup()
            print("GPIO cleaned up")
        except:
            pass


if __name__ == "__main__":
    main()
