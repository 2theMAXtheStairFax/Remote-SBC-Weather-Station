"""
ESP32 power manager — MicroPython

Sits between the 12V battery and the Raspberry Pi's 5V supply.
Handles graceful Pi shutdown before cutting power (prevents SD card
corruption) and checks battery voltage before morning power-on
(prevents brownout boot failures).

State persists across deep sleeps via RTC memory.  Each wake cycle runs
this script from scratch, checks the state, acts, then goes back to sleep
until the next scheduled event.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WIRING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  12V battery (+) ── 3.3V buck converter ── ESP32 3V3/VIN   (always-on supply)
  12V battery (-) ────────────────────────── GND (shared with Pi)

  Pi 5V power rail (controlled):
    12V battery (+) ── 5V buck converter ── Relay COM
    Relay NO ────────────────────────────── Pi GPIO header pin 2 or 4  (5V)
    ESP32 GPIO 26 ───────────────────────── Relay module IN pin
    (HIGH = relay closed = Pi powered on)

  Shutdown handshake (two-wire, recommended):
    ESP32 GPIO 25 ─────────── Pi GPIO 17  (BCM, header pin 11)   shutdown request
    Pi  GPIO 27  (BCM, header pin 13) ─── ESP32 GPIO 33  (add 10kΩ pull-down to GND)  alive signal

  Single-wire mode (if only one pin is accessible):
    ESP32 GPIO 25 ─────────── Pi GPIO 17  (BCM, header pin 11)   shutdown request only
    Set PI_ALIVE_PIN = None below; ESP32 will wait SHUTDOWN_WAIT_SECS after signaling.
    Also set ALIVE_PIN = None in pi_power_monitor.py on the Pi side.

  Header pin access options when the Pimoroni Weather HAT covers the header:
    - Stacking header: insert a 40-pin pass-through/stacking header between Pi and HAT
      (both pins remain accessible with no soldering — easiest option).
    - Solder: attach magnet wire to the via pads for GPIO 17 (pin 11) and/or
      GPIO 27 (pin 13) on the underside of the Pi PCB.
    - Slip wire: push a bare wire alongside the header pin inside the HAT socket
      (works, but less reliable — prefers the soldered or stacking approach).

  Battery voltage sense (voltage divider — adjust resistors to keep Node A < 3.3 V):
    12V battery (+) ── 100 kΩ ─── Node A ── 33 kΩ ─── GND
    Node A ─────────────────────── ESP32 GPIO 34  (ADC-only input)
    Full charge ~13.8 V → Node A = 13.8 × 33/133 ≈ 3.42 V  ← just under 3.6 V ADC max
    (Use 100 kΩ + 36 kΩ if you prefer a larger safety margin)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIRST-TIME SETUP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Flash MicroPython to the ESP32 (https://micropython.org/download/ESP32_GENERIC/)
  2. Copy this file to the ESP32 as main.py (Thonny or ampy).
  3. Set WIFI_SSID / WIFI_PASSWORD for NTP time sync (or leave blank and
     set the RTC manually — see below).
  4. Edit WAKE_HOUR, SLEEP_HOUR and the voltage thresholds to match your site.

  Manual RTC set (if no WiFi):
    >>> import machine
    >>> machine.RTC().datetime((2025, 6, 1, 6, 7, 30, 0, 0))
    #                           year  mo day wday hr min sec ms

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import machine
import time
import ujson
from machine import Pin, ADC, RTC, deepsleep, wake_reason

# ── Hardware pins ────────────────────────────────────────────────────────────
RELAY_PIN       = 26   # OUTPUT  HIGH = Pi powered on
SHUTDOWN_PIN    = 25   # OUTPUT  HIGH = request Pi to shut down
PI_ALIVE_PIN    = 33   # INPUT   HIGH while Pi is running (pulled LOW by 10 kΩ when Pi tristates)
                        # Set to None for single-wire mode (fixed SHUTDOWN_WAIT_SECS timeout)
VBAT_ADC_PIN    = 34   # INPUT   ADC — battery voltage via divider

# ── Voltage divider ───────────────────────────────────────────────────────────
# Ratio = R_low / (R_high + R_low).  Default: 33kΩ / (100kΩ + 33kΩ) = 0.2481
VDIV_RATIO      = 0.2481
ADC_MAX_V       = 3.6    # ADC full-scale with ATTN_11DB

# ── Battery thresholds ────────────────────────────────────────────────────────
VBAT_MIN_BOOT   = 12.0   # Don't power on Pi below this voltage
VBAT_EMERGENCY  = 11.5   # Emergency shutdown if battery drops this low while Pi is on

# ── Daily schedule (24-hour, local time) ─────────────────────────────────────
WAKE_HOUR       = 7
WAKE_MINUTE     = 0
SLEEP_HOUR      = 20
SLEEP_MINUTE    = 0

# ── Timing ────────────────────────────────────────────────────────────────────
SHUTDOWN_SIGNAL_SECS  = 3    # How long to pulse the shutdown signal HIGH
SHUTDOWN_WAIT_SECS    = 60   # Max wait for Pi to halt after signaling
BOOT_WAIT_SECS        = 90   # Max wait for Pi alive signal after relay on
BATTERY_CHECK_MINS    = 10   # How often to wake for emergency battery check while Pi is on

# ── WiFi / NTP (optional — leave blank to skip NTP sync) ─────────────────────
WIFI_SSID       = ""
WIFI_PASSWORD   = ""
NTP_HOST        = "pool.ntp.org"

# ── State keys stored in RTC memory ──────────────────────────────────────────
STATE_OFF       = "off"
STATE_ON        = "on"
STATE_BOOTING   = "booting"
STATE_HALTING   = "halting"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def log(msg):
    t = machine.RTC().datetime()
    print(f"[{t[0]}-{t[1]:02d}-{t[2]:02d} {t[4]:02d}:{t[5]:02d}:{t[6]:02d}] {msg}")


def read_state():
    try:
        raw = RTC().memory()
        if raw:
            return ujson.loads(raw.decode()).get("state", STATE_OFF)
    except Exception:
        pass
    return STATE_OFF


def write_state(state):
    RTC().memory(ujson.dumps({"state": state}).encode())


def read_battery_v():
    adc = ADC(Pin(VBAT_ADC_PIN))
    adc.atten(ADC.ATTN_11DB)
    # Average 8 samples to reduce noise
    raw = sum(adc.read() for _ in range(8)) // 8
    return (raw / 4095.0) * ADC_MAX_V / VDIV_RATIO


def relay_on():
    Pin(RELAY_PIN, Pin.OUT).value(1)


def relay_off():
    Pin(RELAY_PIN, Pin.OUT).value(0)


def pi_is_alive():
    if PI_ALIVE_PIN is None:
        return True   # Single-wire mode: assume alive, rely on timeout
    return Pin(PI_ALIVE_PIN, Pin.IN).value() == 1


def ms_until(hour, minute):
    """Return milliseconds until the next occurrence of hour:minute (local RTC time)."""
    t = RTC().datetime()  # (year, month, day, weekday, hour, min, sec, subsec)
    now_m = t[4] * 60 + t[5]
    tgt_m = hour * 60 + minute
    delta_m = tgt_m - now_m
    if delta_m <= 0:
        delta_m += 24 * 60
    return delta_m * 60 * 1000


def ntp_sync():
    if not WIFI_SSID:
        return
    try:
        import network, ntptime
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        for _ in range(20):
            if wlan.isconnected():
                break
            time.sleep(0.5)
        if wlan.isconnected():
            ntptime.host = NTP_HOST
            ntptime.settime()
            log("NTP synced.")
        wlan.disconnect()
        wlan.active(False)
    except Exception as e:
        log(f"NTP sync failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Actions
# ─────────────────────────────────────────────────────────────────────────────

def power_on_pi():
    """Turn on the relay and wait for the Pi to assert its alive signal."""
    vbat = read_battery_v()
    log(f"Battery: {vbat:.2f} V")

    if vbat < VBAT_MIN_BOOT:
        log(f"Battery too low to boot Pi ({vbat:.2f} V < {VBAT_MIN_BOOT} V). Retrying in 30 min.")
        write_state(STATE_OFF)
        deepsleep(30 * 60 * 1000)

    log("Closing relay — Pi 5V on.")
    relay_on()
    write_state(STATE_BOOTING)

    log(f"Waiting up to {BOOT_WAIT_SECS} s for Pi alive signal...")
    for _ in range(BOOT_WAIT_SECS * 2):
        if pi_is_alive():
            log("Pi alive signal detected — boot confirmed.")
            write_state(STATE_ON)
            # Schedule next battery check; primary wake is SLEEP_HOUR
            deepsleep(min(BATTERY_CHECK_MINS * 60 * 1000, ms_until(SLEEP_HOUR, SLEEP_MINUTE)))
        time.sleep(0.5)

    log("WARNING: Pi alive signal never appeared after relay on. Pi may have failed to boot.")
    # Leave relay on — Pi might still be booting slowly. Check again in 5 min.
    deepsleep(5 * 60 * 1000)


def shutdown_pi():
    """Signal the Pi to shut down, wait for halt, then open the relay."""
    if not pi_is_alive():
        log("Pi already halted (alive pin LOW). Cutting power.")
        relay_off()
        write_state(STATE_OFF)
        deepsleep(ms_until(WAKE_HOUR, WAKE_MINUTE))

    log("Asserting shutdown signal to Pi...")
    shutdown_pin = Pin(SHUTDOWN_PIN, Pin.OUT)
    shutdown_pin.value(1)
    time.sleep(SHUTDOWN_SIGNAL_SECS)
    shutdown_pin.value(0)
    write_state(STATE_HALTING)

    log(f"Waiting up to {SHUTDOWN_WAIT_SECS} s for Pi to halt...")
    for _ in range(SHUTDOWN_WAIT_SECS * 2):
        if not pi_is_alive():
            log("Pi halted (alive pin LOW). Cutting power.")
            relay_off()
            write_state(STATE_OFF)
            deepsleep(ms_until(WAKE_HOUR, WAKE_MINUTE))
        time.sleep(0.5)

    # Timeout — Pi didn't halt. Cut power anyway (better than leaving it on all night).
    log("WARNING: Pi did not halt within timeout. Cutting power anyway.")
    relay_off()
    write_state(STATE_OFF)
    deepsleep(ms_until(WAKE_HOUR, WAKE_MINUTE))


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ntp_sync()

    state = read_state()
    t = RTC().datetime()
    now_m = t[4] * 60 + t[5]
    wake_m = WAKE_HOUR * 60 + WAKE_MINUTE
    sleep_m = SLEEP_HOUR * 60 + SLEEP_MINUTE
    log(f"Wake — state={state}  time={t[4]:02d}:{t[5]:02d}")

    # ── Pi is off ─────────────────────────────────────────────────────────────
    if state == STATE_OFF:
        if wake_m <= now_m < sleep_m:
            # Within operating window — power on
            power_on_pi()
        else:
            # Outside window — sleep until next wake time
            ms = ms_until(WAKE_HOUR, WAKE_MINUTE)
            log(f"Outside operating window. Sleeping {ms // 60000} min until {WAKE_HOUR:02d}:{WAKE_MINUTE:02d}.")
            deepsleep(ms)

    # ── Pi is on — periodic battery check ────────────────────────────────────
    elif state in (STATE_ON, STATE_BOOTING):
        vbat = read_battery_v()
        log(f"Battery: {vbat:.2f} V  Pi alive: {pi_is_alive()}")

        if vbat < VBAT_EMERGENCY:
            log(f"Emergency shutdown — battery critical ({vbat:.2f} V).")
            shutdown_pi()

        if now_m >= sleep_m or now_m < wake_m:
            log("Reached sleep window — initiating shutdown.")
            shutdown_pi()

        # Still within operating window — check again soon
        ms = min(BATTERY_CHECK_MINS * 60 * 1000, ms_until(SLEEP_HOUR, SLEEP_MINUTE))
        log(f"All OK. Next check in {ms // 60000} min.")
        deepsleep(ms)

    # ── Halting in progress (resumed after reboot / unexpected wake) ──────────
    elif state == STATE_HALTING:
        if not pi_is_alive():
            log("Pi halted. Cutting power.")
            relay_off()
            write_state(STATE_OFF)
            deepsleep(ms_until(WAKE_HOUR, WAKE_MINUTE))
        else:
            # Still waiting — check again in 10 s
            log("Pi still halting. Rechecking in 10 s.")
            deepsleep(10 * 1000)

    else:
        log(f"Unknown state '{state}'. Resetting to off.")
        relay_off()
        write_state(STATE_OFF)
        deepsleep(ms_until(WAKE_HOUR, WAKE_MINUTE))


main()
