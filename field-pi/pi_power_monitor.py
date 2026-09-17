#!/usr/bin/env python3
"""
Pi-side companion to the ESP32 power manager.

Watches a GPIO input pin for a shutdown request from the ESP32, then
initiates a clean OS shutdown so the filesystem is safely synced before
the ESP32 cuts the 5V relay.

Optionally drives an output pin HIGH while running so the ESP32 knows
the Pi is alive without relying on a fixed timeout.  If your HAT leaves
no GPIO header pin accessible, set ALIVE_PIN = None and the ESP32 will
fall back to its SHUTDOWN_WAIT_SECS timeout instead.

GPIO pin choices (both physically free on the Pimoroni Weather HAT):
  SHUTDOWN_REQUEST_PIN = 17   (header pin 11)   ESP32 GPIO 25 → Pi GPIO 17
  ALIVE_PIN            = 27   (header pin 13)   Pi GPIO 27 → ESP32 GPIO 33
  GND                         (header pin 14)   shared with ESP32 GND

Hardware access options when the HAT blocks the header:
  - Easiest:  40-pin stacking/pass-through header between Pi and HAT
  - Single-wire: solder one wire (GPIO 17 / pin 11) to the Pi board back;
    set ALIVE_PIN = None and the ESP32 uses a fixed timeout instead.

Install:
  sudo cp pi_power_monitor.py /home/<USER>/weather-station/
  sudo cp pi_power_monitor.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now pi_power_monitor
"""

import sys
import time
import logging
import subprocess
import RPi.GPIO as GPIO

# ── Configuration ─────────────────────────────────────────────────────────────
SHUTDOWN_REQUEST_PIN = 17   # INPUT: goes HIGH when ESP32 requests shutdown
ALIVE_PIN            = 27   # OUTPUT: held HIGH while this service is running
                             # Set to None if this pin is not wired / accessible

POLL_INTERVAL_S = 0.25      # How often to check the shutdown pin

# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [pi_power] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    GPIO.setup(SHUTDOWN_REQUEST_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    if ALIVE_PIN is not None:
        GPIO.setup(ALIVE_PIN, GPIO.OUT)
        GPIO.output(ALIVE_PIN, GPIO.HIGH)
        log("Alive signal asserted on GPIO %d.", ALIVE_PIN)

    log("Watching GPIO %d for shutdown request.", SHUTDOWN_REQUEST_PIN)

    try:
        while True:
            if GPIO.input(SHUTDOWN_REQUEST_PIN):
                log("Shutdown request received from ESP32. Initiating clean shutdown.")
                # Signal ESP32 that we are about to go down before the OS kills us
                if ALIVE_PIN is not None:
                    GPIO.output(ALIVE_PIN, GPIO.LOW)
                subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)
                # Block here until the OS kills this process during shutdown
                time.sleep(120)

            time.sleep(POLL_INTERVAL_S)

    except (KeyboardInterrupt, SystemExit):
        log("Service stopping (Pi still running).")
    finally:
        # Do NOT pull ALIVE_PIN low on a normal service stop — only do that
        # during a real shutdown (above).  Leaving it HIGH is correct here.
        GPIO.cleanup()


if __name__ == "__main__":
    main()
