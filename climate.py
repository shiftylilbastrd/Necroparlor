#!/usr/bin/env python3
"""
Dermestid beetle enclosure climate control.

Controls heating, cooling/ventilation and dehumidification inside a
converted chest freezer based on the currently selected activity mode
(dormant / ready / cleaning). The active mode and per-mode setpoints
live in config.json, managed via shared_state.py, and can be changed
live from the web dashboard (webapp.py) - this script re-reads
config.json every control cycle, so changes take effect within one
LOOP_INTERVAL.

Run this under systemd (see systemd/dermestid-climate.service) so it
restarts automatically if the process ever dies outright.
"""
import time
import logging
from logging.handlers import RotatingFileHandler
import threading
import os
import signal

import RPi.GPIO as GPIO
import board
# busio / adafruit_sht31d are NOT imported here - they're imported lazily
# inside _get_sht31_sensor()/_get_sht31_sensor_external() below, same as
# always (no behavior change) - kept lazy since this script should still
# start cleanly even with one of the two SHT31s not actually connected yet.

import shared_state as state

# === LOGGING ===
LOG_DIR = os.path.join(state.BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(os.path.join(LOG_DIR, "climate.log"),
                             maxBytes=2_000_000, backupCount=5),
        logging.StreamHandler()
    ]
)

# === SAFETY / TIMING CONSTANTS ===
# These are intentionally NOT exposed to the web UI - they're the
# guardrails, not the thing you tune day-to-day.
LOOP_INTERVAL = 15                 # seconds between control cycles
COOL_HYSTERESIS = 2.0
HEAT_HYSTERESIS = 2.0
HUMIDITY_HYSTERESIS = 3.0
MAX_DELTA_TEMP = 5.0                # max plausible degF jump between reads
MAX_DELTA_HUMIDITY = 15.0           # max plausible %RH jump between reads
SENSOR_FAIL_TIMEOUT = 90            # seconds of bad/missing reads -> failsafe
HEATER_MAX_ON_SECONDS = 20 * 60     # hard cutoff regardless of temp reached
HEATER_LOCKOUT_SECONDS = 5 * 60     # cooldown before heater may retry after a cutoff
FAN_MAX_ON_SECONDS = 60 * 60        # motor protection / stuck-sensor guard
FAN_LOCKOUT_SECONDS = 5 * 60
EXTREME_TEMP_LOW_F = 45.0
EXTREME_TEMP_HIGH_F = 95.0

# SHT31 condensation recovery: if internal humidity reads pegged near
# 100% for a while, it's more likely condensation on the sensor than
# reality, so pulse the SHT31's onboard heater to dry it off.
HUMIDITY_SATURATION_THRESHOLD = 99.0   # %RH considered "pegged"
SATURATION_RECOVERY_DELAY = 60         # seconds pegged before we act
HEATER_RECOVERY_COOLDOWN = 600         # don't re-trigger more than once per 10 min
HEATER_PULSE_SECONDS = 10              # how long to run the sensor's self-heater

# --- Servo ---
SERVO_DELAY = 5
SERVO_CLOSED_ANGLE = 0
SERVO_OPEN_ANGLE = 180

# --- GPIO Assignments ---
PIN_FAN = 17
PIN_HEATER = 22
PIN_HUMIDITY = 23
PIN_SERVO = 18            # PWM-capable pin
# PIN_INTERNAL_TEMP (GPIO27) / PIN_EXTERNAL_TEMP (GPIO5) - the old wired
# DHT22 pins - are retired now that both sensors are SHT31s (see
# PROJECT_STATUS.md for the removal note, and for the GPIO4-is-dead-on-
# this-board history that led to GPIO5 in the first place). Both pins
# are unused/free for future wiring.
PIN_LIGHT = 26            # moved off GPIO15 (was sharing UART0 RXD - see README)
PIN_SWITCH = 24

# === GPIO SETUP ===
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

for pin in [PIN_FAN, PIN_HEATER, PIN_HUMIDITY]:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, True)  # inverted logic: True = off

GPIO.setup(PIN_SERVO, GPIO.OUT)
GPIO.setup(PIN_LIGHT, GPIO.OUT)
GPIO.output(PIN_LIGHT, True)
GPIO.setup(PIN_SWITCH, GPIO.IN, pull_up_down=GPIO.PUD_UP)

servo = GPIO.PWM(PIN_SERVO, 50)
servo.start(0)

# === INTERNAL SENSOR (SHT31, I2C) ===
# Needs I2C enabled via raspi-config - see README. Wiring: VIN->3.3V,
# GND->GND, SCL->GPIO3 (pin 5), SDA->GPIO2 (pin 3).
# Created lazily on first use, not here at import time, so a missing/
# not-yet-wired sensor doesn't crash the script at startup.
_i2c_bus = None
_sht31_sensor = None


def _get_sht31_sensor():
    global _i2c_bus, _sht31_sensor
    if _sht31_sensor is None:
        import busio             # noqa: PLC0415 - deliberately deferred, see note above
        import adafruit_sht31d   # noqa: PLC0415 - deliberately deferred, see note above
        _i2c_bus = busio.I2C(board.SCL, board.SDA)
        _sht31_sensor = adafruit_sht31d.SHT31D(_i2c_bus)
    return _sht31_sensor


# === EXTERNAL/FALLBACK SENSOR (second SHT31, I2C) ===
# Two identical SHT31 breakouts both default to I2C address 0x44, so this
# one can't just share the internal sensor's bus (board.SCL/board.SDA,
# hardware I2C1 on GPIO2/3) - it needs its own bus entirely. That's what
# the "i2c5 overlay" wiring note on the Settings page's Fallback card is
# about: the Pi 4/CM4's extra built-in hardware I2C controller (BCM2711
# has more than one), enabled via `dtoverlay=i2c5` in config.txt, defaults
# to GPIO12/13, exposed to Linux as /dev/i2c-5 - a second REAL hardware
# bus, not a bit-banged one, independent of the one the internal sensor
# uses - see README for the exact overlay line. Needs the
# `adafruit-extended-bus` package (only matters if you're actually using
# this), since Blinka's busio.I2C only auto-detects the board's *default*
# hardware I2C bus (GPIO2/3), not this second one.
_i2c_bus_external = None
_sht31_sensor_external = None


def _get_sht31_sensor_external():
    global _i2c_bus_external, _sht31_sensor_external
    if _sht31_sensor_external is None:
        from adafruit_extended_bus import ExtendedI2C   # noqa: PLC0415 - deliberately deferred, see note above
        import adafruit_sht31d                          # noqa: PLC0415 - deliberately deferred, see note above
        _i2c_bus_external = ExtendedI2C(5)  # /dev/i2c-5, see the overlay note above
        _sht31_sensor_external = adafruit_sht31d.SHT31D(_i2c_bus_external)
    return _sht31_sensor_external

# === MUTABLE STATE ===
current_servo_angle = SERVO_CLOSED_ANGLE
fan_on = False
heater_on = False
humidity_on = False
servo_open = False

thermal_cool_request = False
heat_request = False
heater_on_since = None
heater_lockout_until = 0
fan_on_since = None
fan_lockout_until = 0

vent_active = False
vent_start = 0
last_vent_start = 0

last_good_internal_temp = None
last_good_internal_humidity = None
last_good_external_temp = None
last_good_external_humidity = None
last_active_external_source = None  # tracks automatic failover transitions
last_external_sensor_ok = None  # tracks BLE+wired-both-down transitions (see run_cycle)
sensor_fail_since = None
alarm_active = False

humidity_saturated_since = None
last_heater_recovery = 0

# Dwell-time tracking for the "internal reading is out of acceptable range"
# alert below - mirrors the door_open_timeout pattern in light_loop(): the
# _since timestamp marks when the current out-of-range episode started
# (None while in range), and _alert_sent makes sure each episode logs at
# most one event instead of one every cycle for as long as it stays out of
# range.
temp_range_since = None
temp_range_alert_sent = False
humidity_range_since = None
humidity_range_alert_sent = False


class GracefulExit(Exception):
    """Raised from the SIGTERM handler so `finally` always runs and GPIO
    gets released cleanly when systemd stops/restarts this service."""
    pass


def _handle_sigterm(signum, frame):
    raise GracefulExit()


signal.signal(signal.SIGTERM, _handle_sigterm)


def turn_on(pin):
    GPIO.output(pin, False)


def turn_off(pin):
    GPIO.output(pin, True)


def set_servo_angle(angle):
    global current_servo_angle
    if current_servo_angle != angle:
        duty = 2 + (angle / 18)
        servo.ChangeDutyCycle(duty)
        time.sleep(0.6)
        servo.ChangeDutyCycle(0)
        current_servo_angle = angle


def read_internal_sht31_f():
    """Internal SHT31 over I2C. Returns (None, None) on any I2C/CRC failure,
    or if it's not actually wired up/responding - either way it flows into
    the same validation/failsafe pipeline as any other failed read, rather
    than crashing the script."""
    try:
        sensor = _get_sht31_sensor()
        temp_c = sensor.temperature
        humidity = sensor.relative_humidity
    except (OSError, RuntimeError, ValueError, ImportError):
        return None, None
    if temp_c is None or humidity is None:
        return None, None
    return temp_c * 9.0 / 5.0 + 32.0, humidity


def read_external_sht31_f():
    """External/fallback SHT31 over its own I2C bus (see
    _get_sht31_sensor_external above). Returns (None, None) on any I2C/CRC
    failure, or if the overlay/hardware isn't actually present - either way
    it flows into the same validation/failsafe pipeline as any other
    failed read, rather than crashing the script."""
    try:
        sensor = _get_sht31_sensor_external()
        temp_c = sensor.temperature
        humidity = sensor.relative_humidity
    except (OSError, RuntimeError, ValueError, ImportError):
        return None, None
    if temp_c is None or humidity is None:
        return None, None
    return temp_c * 9.0 / 5.0 + 32.0, humidity


def _plausible(value, low, high):
    return value is not None and low <= value <= high


# Tracks provisional "maybe this is real, not a glitch" streaks per
# metric (keyed by label). Without this, a rejected reading's reference
# point (last_val) never updates - so a genuine, gradual, sustained
# change gets rejected forever, since every future comparison measures
# against an ever-more-stale frozen point that can never catch up. This
# happened in practice: a real multi-hour temperature drift got
# permanently locked out this way, triggering a full emergency shutdown
# that then never recovered on its own.
_pending_streaks = {}

STREAK_CONFIRM_COUNT = 3            # consecutive self-consistent readings
                                     # needed before accepting a big jump
STREAK_CONSISTENCY_TOLERANCE = 2.0  # max spread allowed within a streak


def apply_offset(value, offset):
    """Applies a calibration offset to a raw reading. A failed read (None)
    has nothing to correct, so it passes through unchanged rather than
    becoming a meaningless 'offset applied to nothing'."""
    return value + offset if value is not None else None


def validate_reading(new_val, last_val, max_delta, label):
    """Reject physically-implausible sensor glitches: an implausible jump
    from the last known-good reading. (Absolute-range checks happen
    before this is called.)

    A rejected reading isn't discarded and forgotten, though - if
    several consecutive readings keep landing consistently close to
    *each other* (even though they differ from the old last-good
    value), that's strong evidence of a genuine, sustained change
    rather than a one-off glitch, since a real sensor glitch is
    typically a transient outlier that doesn't reliably repeat.  After
    STREAK_CONFIRM_COUNT such consistent readings in a row (roughly
    45-90s at the normal loop interval), the new value is accepted as
    the new baseline, breaking the lockout.
    """
    if new_val is None:
        _pending_streaks.pop(label, None)
        return None

    if last_val is None or abs(new_val - last_val) <= max_delta:
        _pending_streaks.pop(label, None)
        return new_val

    # Outside the plausible single-cycle range - track whether this
    # keeps happening consistently before giving up on it as real.
    pending_val, streak = _pending_streaks.get(label, (None, 0))
    if pending_val is not None and abs(new_val - pending_val) <= STREAK_CONSISTENCY_TOLERANCE:
        streak += 1
    else:
        streak = 1
    _pending_streaks[label] = (new_val, streak)

    if streak >= STREAK_CONFIRM_COUNT:
        state.log_event("warning",
                         f"{label} confirmed a sustained new reading ({new_val:.1f}, "
                         f"was stuck comparing against {last_val:.1f}) after {streak} "
                         "consistent cycles - accepting it as the new baseline")
        _pending_streaks.pop(label, None)
        return new_val

    state.log_event("warning", f"{label} reading rejected: jumped from "
                     f"{last_val:.1f} to {new_val:.1f} in one cycle "
                     f"({streak}/{STREAK_CONFIRM_COUNT} consistent readings so far)",
                     category="sensor_reading_rejected")
    return None


def activate_cooling():
    global fan_on, servo_open, fan_on_since
    if not servo_open:
        logging.info("Opening servo for cooling/ventilation...")
        set_servo_angle(SERVO_OPEN_ANGLE)
        servo_open = True
        time.sleep(SERVO_DELAY)
    if not fan_on:
        state.log_event("info", "Fan ON")
        turn_on(PIN_FAN)
        fan_on = True
        fan_on_since = time.time()


def deactivate_cooling():
    global fan_on, servo_open, fan_on_since
    if fan_on or servo_open:
        state.log_event("info", "Fan OFF, closing servo")
        turn_off(PIN_FAN)
        fan_on = False
        fan_on_since = None
        set_servo_angle(SERVO_CLOSED_ANGLE)
        servo_open = False


def emergency_shutdown_outputs(reason):
    global heater_on, heater_on_since, fan_on, servo_open, fan_on_since, humidity_on
    state.log_event("critical", f"EMERGENCY SHUTDOWN: {reason}", category="emergency_shutdown")
    turn_off(PIN_HEATER)
    heater_on = False
    heater_on_since = None
    turn_off(PIN_FAN)
    fan_on = False
    fan_on_since = None
    set_servo_angle(SERVO_CLOSED_ANGLE)
    servo_open = False
    turn_off(PIN_HUMIDITY)
    humidity_on = False


LIGHT_OVERRIDE_POLL_SECONDS = 1  # how often this loop re-reads config.json
# for a dashboard-triggered light override - the 50ms sleep below is for
# door-switch responsiveness, not for how fast an override needs to react;
# checking config.json 20x/sec instead of 1x/sec would be pure waste.


def light_loop():
    """Drives the door/lid light off the same reed switch the whole time -
    the switch always wins, opening the door always turns the light on
    regardless of anything below. Also now tracks open/closed transitions
    for the dashboard's door indicator: written to the DB only on an
    actual transition (not on every 50ms poll), so it stays cheap but
    still reflects within one web dashboard refresh.

    On top of the switch, also honors a dashboard-driven manual override
    (config.json's light_override_until, set by the Home page's live-view
    icon via /api/light-override) so the light can be checked on without
    actually opening the lid. Purely additive - it can only turn the light
    ON when the switch alone wouldn't, never prevent the switch turning it
    on or off. Self-expiring (see LIGHT_OVERRIDE_DURATION_SECONDS): once
    time.time() passes the stored timestamp, override_active just goes
    false on the next poll - nothing needs to actively clear it.

    Also watches how long the door has been continuously open and, past
    notifications.door_open_alert_minutes (0 = disabled - same "never"
    sentinel as snapshot_interval_minutes elsewhere in this project),
    logs ONE warning/door_open_timeout event for that open episode - not
    a new one every poll for as long as it stays open, and not a fixed
    safety behavior like the ones in the README (no relay/output is
    touched here at all) - purely so a genuinely forgotten-open door
    surfaces as a push notification instead of only showing up as a
    "Door opened" info-level line someone has to notice on the Logs page.

    Runs as a background thread for the life of the process, so any
    exception here needs to be caught and logged rather than allowed to
    kill the thread - an uncaught exception would silently disable the
    door light AND door tracking for the rest of the run, with the main
    control loop carrying on none the wiser."""
    last_open = None
    last_override_check = 0
    override_until = 0
    door_open_alert_minutes = 15
    open_since = None
    door_alert_sent = False
    while True:
        try:
            is_open = GPIO.input(PIN_SWITCH) == GPIO.LOW

            now = time.time()
            if now - last_override_check >= LIGHT_OVERRIDE_POLL_SECONDS:
                config = state.load_config()
                override_until = config.get("light_override_until", 0) or 0
                door_open_alert_minutes = config.get("notifications", {}).get("door_open_alert_minutes", 15)
                last_override_check = now
            override_active = now < override_until

            if is_open or override_active:
                turn_on(PIN_LIGHT)
            else:
                turn_off(PIN_LIGHT)

            if is_open != last_open:
                state.save_door_state(is_open)
                state.log_event("info", f"Door {'opened' if is_open else 'closed'}")
                last_open = is_open
                # New episode either way - a close always resets the
                # timer, and a fresh open always starts a fresh one, so
                # the alert (if it fires at all) is always about the
                # CURRENT stretch of open time, not one carried over
                # from a previous open/close cycle.
                open_since = now if is_open else None
                door_alert_sent = False

            if (is_open and open_since is not None and not door_alert_sent
                    and door_open_alert_minutes > 0
                    and (now - open_since) >= door_open_alert_minutes * 60):
                state.log_event("warning", f"Door has been open for over {door_open_alert_minutes} min",
                                 category="door_open_timeout")
                door_alert_sent = True
        except Exception:
            logging.exception("Unexpected error in light_loop - will retry next poll")
        time.sleep(0.05)


def run_cycle():
    """One full control-loop iteration. Any exception raised here
    (other than GracefulExit/KeyboardInterrupt) is caught by the
    caller, logged, and the loop continues on the next cycle rather
    than taking the whole service down."""
    global thermal_cool_request, heat_request, heater_on, heater_on_since, heater_lockout_until
    global fan_on, fan_on_since, fan_lockout_until, humidity_on
    global vent_active, vent_start, last_vent_start
    global last_good_internal_temp, last_good_internal_humidity, last_good_external_temp
    global last_good_external_humidity
    global sensor_fail_since, alarm_active
    global last_external_sensor_ok
    global humidity_saturated_since, last_heater_recovery
    global temp_range_since, temp_range_alert_sent, humidity_range_since, humidity_range_alert_sent

    loop_start = time.time()
    config = state.load_config()
    mode = config["current_mode"]
    setpoints = config["modes"][mode]
    HIGH_TEMP_F = setpoints["high_temp_f"]
    LOW_TEMP_F = setpoints["low_temp_f"]
    HUMIDITY_SETPOINT = setpoints["humidity_setpoint"]

    calib = config.get("calibration", {})
    raw_internal_temp, raw_internal_humidity = read_internal_sht31_f()
    raw_internal_temp = apply_offset(raw_internal_temp, calib.get("internal_temp_offset", 0.0))
    raw_internal_humidity = apply_offset(raw_internal_humidity, calib.get("internal_humidity_offset", 0.0))

    # Both external sources are read every cycle. Which one is "active"
    # (drives control decisions) is decided automatically here, not by a
    # manual config toggle: the BLE sensor is used whenever it's reported
    # within SENSOR_FAIL_TIMEOUT (the same freshness window the failsafe
    # already uses elsewhere), and the wired probe is used automatically
    # otherwise - no manual switch, no missed data while nobody's
    # watching the dashboard.
    #
    # Each physical sensor's reading is validated and stored under its
    # own fixed name (ble_temp/wired_temp) regardless of which one is
    # currently active - "active" is a decision about which value drives
    # control, not a relabeling of the data itself. Earlier versions
    # stored whichever one wasn't active as a generic "fallback" value,
    # which meant the same tile on the dashboard could silently show
    # data from a different physical sensor depending on system state.
    # Neither ble_temp/humidity nor wired_temp/humidity ever touches any
    # control-critical state (last_good_*, the delta-glitch filter, the
    # failsafe) on their own - only basic plausibility bounds apply,
    # since a single bad reading from either isn't dangerous the way a
    # bad ACTIVE reading would be, just cosmetically wrong for one cycle.
    raw_wired_temp, raw_wired_humidity = read_external_sht31_f()
    raw_wired_temp = apply_offset(raw_wired_temp, calib.get("wired_temp_offset", 0.0))
    raw_wired_humidity = apply_offset(raw_wired_humidity, calib.get("wired_humidity_offset", 0.0))

    raw_ble_temp = raw_ble_humidity = None
    ble_fresh = False
    if config.get("ble_mac"):
        ble_reading = state.get_ble_reading(config["ble_mac"])
        if ble_reading and (loop_start - ble_reading["ts"]) <= SENSOR_FAIL_TIMEOUT:
            raw_ble_temp = apply_offset(ble_reading["temp_f"], calib.get("ble_temp_offset", 0.0))
            raw_ble_humidity = apply_offset(ble_reading["humidity"], calib.get("ble_humidity_offset", 0.0))
            ble_fresh = True

    if ble_fresh:
        raw_external_temp, raw_external_humidity = raw_ble_temp, raw_ble_humidity
        active_external_source = "ble"
    else:
        raw_external_temp, raw_external_humidity = raw_wired_temp, raw_wired_humidity
        active_external_source = "local_gpio"

    # Log the transition itself (once, not every cycle) - automatic
    # failover should still be genuinely visible on the Logs page, not
    # silent just because nobody has to click a switch for it anymore.
    global last_active_external_source
    if last_active_external_source is not None and active_external_source != last_active_external_source:
        if active_external_source == "local_gpio":
            state.log_event("warning", "Automatically failed over to wired probe (BLE sensor stale)",
                             category="external_sensor_failover")
        else:
            state.log_event("info", "BLE sensor recovered, resuming as primary external sensor")
    last_active_external_source = active_external_source

    if not _plausible(raw_ble_temp, -40, 140):
        raw_ble_temp = None
    if not _plausible(raw_ble_humidity, 0, 100):
        raw_ble_humidity = None
    if not _plausible(raw_wired_temp, -40, 140):
        raw_wired_temp = None
    if not _plausible(raw_wired_humidity, 0, 100):
        raw_wired_humidity = None
    ble_temp, ble_humidity = raw_ble_temp, raw_ble_humidity
    wired_temp, wired_humidity = raw_wired_temp, raw_wired_humidity

    # Reject physically-impossible readings before delta-checking against history
    if not _plausible(raw_internal_temp, -40, 140):
        raw_internal_temp = None
    if not _plausible(raw_internal_humidity, 0, 100):
        raw_internal_humidity = None
    if not _plausible(raw_external_temp, -40, 140):
        raw_external_temp = None
    if not _plausible(raw_external_humidity, 0, 100):
        raw_external_humidity = None

    # External humidity is purely informational - nothing in this script
    # acts on it - so it's validated (same glitch filter as everything
    # else, so the chart doesn't show ugly one-sample spikes) but
    # deliberately kept OUT of the sensor-fail/failsafe gate below: a bad
    # external humidity reading should never trigger an emergency shutdown
    # of heat/cool/dehumidify.
    external_humidity = validate_reading(raw_external_humidity, last_good_external_humidity,
                                          MAX_DELTA_HUMIDITY, "External humidity")
    if external_humidity is not None:
        last_good_external_humidity = external_humidity



    # SHT31 condensation recovery: checks the RAW (physically-plausible
    # but not yet delta-validated) humidity, deliberately upstream of the
    # delta check below. A real condensation event legitimately produces a
    # fast jump to ~100% that the delta check would otherwise reject as an
    # implausible glitch every single cycle, forever - since the reading
    # never gets a chance to become "last good" to compare against, this
    # would look identical to a dead sensor and eventually trip the
    # emergency-shutdown failsafe instead of ever getting a chance to dry
    # itself out. Reacting to the raw value here is what actually lets the
    # heater pulse happen; the delta-validated value below still is - and
    # should remain - the only thing the heat/cool/dehumidify logic acts on.
    if (raw_internal_humidity is not None
            and raw_internal_humidity >= HUMIDITY_SATURATION_THRESHOLD):
        if humidity_saturated_since is None:
            humidity_saturated_since = loop_start
        elif (loop_start - humidity_saturated_since > SATURATION_RECOVERY_DELAY
              and loop_start - last_heater_recovery > HEATER_RECOVERY_COOLDOWN):
            state.log_event("warning",
                             "Internal humidity pegged near 100% for over a minute - "
                             "likely condensation on the SHT31; pulsing its heater to dry it off")
            try:
                sensor = _get_sht31_sensor()
                sensor.heater = True
                time.sleep(HEATER_PULSE_SECONDS)
                sensor.heater = False
            except (OSError, RuntimeError, ValueError, ImportError):
                logging.exception("Failed to pulse SHT31 heater")
            last_heater_recovery = loop_start
            humidity_saturated_since = None
    else:
        humidity_saturated_since = None

    internal_temp = validate_reading(raw_internal_temp, last_good_internal_temp,
                                      MAX_DELTA_TEMP, "Internal temp")
    internal_humidity = validate_reading(raw_internal_humidity, last_good_internal_humidity,
                                          MAX_DELTA_HUMIDITY, "Internal humidity")
    external_temp = validate_reading(raw_external_temp, last_good_external_temp,
                                      MAX_DELTA_TEMP, "External temp")

    # Log the BLE+wired-both-down transition itself (once, not every
    # cycle) - this is the one external-sensor failure mode that was
    # previously completely invisible on the dashboard. A lost wired/
    # fallback probe with BLE still fresh never affects external_temp at
    # all (BLE is simply used instead, silently - that's the whole point
    # of automatic failover), so this only fires when BOTH sources are
    # down simultaneously, which is also the one case that actually
    # matters: the dashboard's external tile has nothing at all to show.
    # Previously the only logging for this was a thermal-cooling-specific
    # logging.warning() below that (a) only wrote to climate.log/
    # journalctl, never the SQLite events table the dashboard Logs page
    # and events.txt download read from, and (b) only fired if thermal
    # cooling happened to be actively requested at that exact moment -
    # otherwise a real sensor outage produced zero log output anywhere.
    external_sensor_ok = external_temp is not None
    if last_external_sensor_ok is not None and external_sensor_ok != last_external_sensor_ok:
        if not external_sensor_ok:
            state.log_event("warning",
                             "External sensor unavailable (BLE sensor stale and wired/fallback "
                             "probe not reporting) - external reading will show as missing "
                             "until one of them recovers",
                             category="external_sensor_unavailable")
        else:
            state.log_event("info", "External sensor reporting again")
    last_external_sensor_ok = external_sensor_ok

    # Only INTERNAL sensor loss is treated as critical enough to shut
    # everything down - heating, dehumidifying, and the scheduled
    # cleaning-mode ventilation are all driven purely by internal
    # readings or a fixed timer, none of them need external_temp at all.
    # Losing external_temp specifically is handled separately below: it
    # only pauses the one decision that genuinely depends on it (whether
    # venting to outside air would actually help cool things down),
    # without stopping anything else.
    if internal_temp is None or internal_humidity is None:
        if sensor_fail_since is None:
            sensor_fail_since = loop_start
            # Only logged once per outage (here, on the first cycle) so
            # it's actually visible on the dashboard without spamming
            # it every 15s for a long outage - this line was previously
            # logging.warning() only (file/journald), which is why a
            # real hour-long outage never showed up on the dashboard at
            # all until the eventual "critical" shutdown, itself easy
            # to miss if it scrolled out of view.
            state.log_event("warning", "Internal sensor reading unavailable - starting failsafe countdown",
                             category="internal_sensor_failsafe")
        elapsed = loop_start - sensor_fail_since
        logging.warning(f"Critical (internal) sensor read failed/rejected ({elapsed:.0f}s since last good reading)")
        if elapsed > SENSOR_FAIL_TIMEOUT and not alarm_active:
            alarm_active = True
            emergency_shutdown_outputs(f"No valid internal sensor readings for over {SENSOR_FAIL_TIMEOUT}s")

        # Still write a row for this cycle, with internal_temp/humidity
        # NULL, instead of skipping log_reading() entirely (which is
        # what this branch used to do, by returning before ever reaching
        # the log_reading() call at the bottom of this function). An
        # internal-sensor outage used to leave a GAP in the readings
        # table - no row at all for as long as it lasted - which made it
        # indistinguishable, on the Data page, from the control loop
        # itself being down, and meant spotting a real outage required
        # noticing a jump in timestamps rather than just seeing a blank
        # cell the way a lost external/fallback reading already shows
        # (that path was never gated like this - see the external-sensor
        # comment above). Everything passed here is already known at
        # this point in the cycle - external/ble/wired readings were all
        # read and validated above before the internal check runs, and
        # fan_on/heater_on/humidity_on/vent_active are last cycle's
        # actual relay states, still accurate since nothing below this
        # branch has run yet to change them (emergency_shutdown_outputs()
        # above, if it fired, already updated those globals to reflect
        # the forced-off state before this call).
        pi_health = state.get_pi_health()
        state.log_reading(mode, None, None, external_temp, external_humidity,
                           fan_on, heater_on, humidity_on, vent_active,
                           ble_temp=ble_temp, ble_humidity=ble_humidity,
                           wired_temp=wired_temp, wired_humidity=wired_humidity,
                           active_external_source=active_external_source,
                           cpu_temp_f=pi_health["cpu_temp_f"], cpu_load_1m=pi_health["cpu_load_1m"])

        time.sleep(LOOP_INTERVAL)
        return

    # Good CRITICAL reading this cycle - clear failure/alarm state
    sensor_fail_since = None
    if alarm_active:
        state.log_event("info", "Sensor readings recovered, resuming normal control")
        alarm_active = False
    last_good_internal_temp = internal_temp
    last_good_internal_humidity = internal_humidity
    if external_temp is not None:
        last_good_external_temp = external_temp

    ext_temp_str = f"{external_temp:.1f}F" if external_temp is not None else "--F"
    ext_hum_str = f"{external_humidity:.1f}%" if external_humidity is not None else "--%"
    logging.info(f"External: {ext_temp_str}/{ext_hum_str} | "
                 f"Internal: {internal_temp:.1f}F/{internal_humidity:.1f}% | Mode: {mode}")

    # === Out-of-range alerting (internal reading only) ===
    # Deliberately separate from the heat/cool/humidify control decisions
    # above: those react to low_temp_f/high_temp_f/humidity_setpoint
    # immediately, every cycle, which is exactly what they're supposed to
    # do. Alerting on that same crossing would fire constantly during
    # perfectly normal hysteresis-driven swings the control loop is
    # already correcting. Instead this watches a wider band - each
    # mode's setpoints plus/minus a configurable margin - and only fires
    # after staying outside that wider band for notifications.range_alert_minutes
    # (same 0-disables-it / dwell-before-alerting pattern as the door-open
    # alert in light_loop()), so it's meant to catch a genuine problem
    # (equipment failure, a door that silently failed to close, a heat
    # wave overwhelming the AC) rather than ordinary operation.
    range_alert_minutes = config.get("notifications", {}).get("range_alert_minutes", 15)
    temp_alert_margin = setpoints.get("temp_alert_margin_f", 5.0)
    humidity_alert_margin = setpoints.get("humidity_alert_margin", 15.0)
    alert_low_temp = LOW_TEMP_F - temp_alert_margin
    alert_high_temp = HIGH_TEMP_F + temp_alert_margin
    alert_low_humidity = HUMIDITY_SETPOINT - humidity_alert_margin
    alert_high_humidity = HUMIDITY_SETPOINT + humidity_alert_margin

    if internal_temp < alert_low_temp or internal_temp > alert_high_temp:
        if temp_range_since is None:
            temp_range_since = loop_start
            temp_range_alert_sent = False
        if (not temp_range_alert_sent and range_alert_minutes > 0
                and (loop_start - temp_range_since) >= range_alert_minutes * 60):
            direction = "below" if internal_temp < alert_low_temp else "above"
            bound = alert_low_temp if direction == "below" else alert_high_temp
            state.log_event("warning",
                             f"Internal temp {internal_temp:.1f}F has been {direction} the "
                             f"acceptable range ({bound:.1f}F) for over {range_alert_minutes} min",
                             category="temp_out_of_range")
            temp_range_alert_sent = True
    else:
        temp_range_since = None
        temp_range_alert_sent = False

    if internal_humidity < alert_low_humidity or internal_humidity > alert_high_humidity:
        if humidity_range_since is None:
            humidity_range_since = loop_start
            humidity_range_alert_sent = False
        if (not humidity_range_alert_sent and range_alert_minutes > 0
                and (loop_start - humidity_range_since) >= range_alert_minutes * 60):
            direction = "below" if internal_humidity < alert_low_humidity else "above"
            bound = alert_low_humidity if direction == "below" else alert_high_humidity
            state.log_event("warning",
                             f"Internal humidity {internal_humidity:.1f}% has been {direction} the "
                             f"acceptable range ({bound:.1f}%) for over {range_alert_minutes} min",
                             category="humidity_out_of_range")
            humidity_range_alert_sent = True
    else:
        humidity_range_since = None
        humidity_range_alert_sent = False

    # === Scheduled ventilation cycle (cleaning mode only) ===
    if mode == "cleaning":
        vent_interval = setpoints.get("vent_interval_minutes", 30) * 60
        vent_duration = setpoints.get("vent_duration_minutes", 5) * 60
        if not vent_active and (loop_start - last_vent_start) >= vent_interval:
            vent_active = True
            vent_start = loop_start
            last_vent_start = loop_start
            state.log_event("info", "Cleaning mode: starting scheduled ventilation cycle")
        if vent_active and (loop_start - vent_start) >= vent_duration:
            vent_active = False
            state.log_event("info", "Cleaning mode: ventilation cycle complete")
    else:
        vent_active = False

    # === Thermal cooling request (sticky, with hysteresis) ===
    # This is the one decision that genuinely needs external_temp - can't
    # safely tell whether venting to outside air would help without
    # knowing how it compares to inside. Missing it pauses thermal
    # cooling specifically; it does not affect heating, dehumidifying,
    # or the scheduled ventilation cycle above, none of which depend on it.
    if external_temp is None:
        if thermal_cool_request:
            logging.warning("External temp unavailable - pausing thermal cooling until it returns")
        thermal_cool_request = False
    elif internal_temp > HIGH_TEMP_F:
        if external_temp < internal_temp - COOL_HYSTERESIS:
            thermal_cool_request = True
        else:
            if thermal_cool_request:
                logging.warning("Cooling wanted but external temp too high to help")
            thermal_cool_request = False
    elif internal_temp < HIGH_TEMP_F - COOL_HYSTERESIS:
        thermal_cool_request = False

    cooling_needed = thermal_cool_request or vent_active

    # === Heating request (sticky, with hysteresis) ===
    if internal_temp < LOW_TEMP_F:
        heat_request = True
    elif internal_temp > LOW_TEMP_F + HEAT_HYSTERESIS:
        heat_request = False

    # Mutual exclusion applies to genuine THERMAL cooling only - running
    # the heater while actively venting hot air out would be directly
    # self-defeating (heating air that's about to be pushed back
    # outside). The scheduled cleaning-mode ventilation cycle is
    # different: it's not temperature-driven at all, it fires on a fixed
    # schedule purely to flush air quality regardless of what the
    # temperature is doing. Blocking heat for that too would let a
    # routine air-quality flush cause a real temperature dip that has
    # nothing to do with why the fan turned on - so it's intentionally
    # excluded here even though vent_active still drives the fan/servo
    # itself (via cooling_needed below), unchanged.
    if thermal_cool_request and heat_request:
        logging.warning("Heat and cool both requested this cycle - prioritizing cooling")
        heat_request = False

    # --- Apply cooling / ventilation, with a runtime safety cutoff ---
    if cooling_needed:
        if fan_on_since and (loop_start - fan_on_since) > FAN_MAX_ON_SECONDS:
            state.log_event("warning", "Fan safety cutoff: exceeded max continuous runtime",
                             category="fan_safety_cutoff")
            deactivate_cooling()
            fan_lockout_until = loop_start + FAN_LOCKOUT_SECONDS
        elif loop_start > fan_lockout_until:
            activate_cooling()
    elif fan_on:
        deactivate_cooling()

    # --- Apply heating, with a runtime safety cutoff ---
    if heat_request and loop_start > heater_lockout_until:
        if not heater_on:
            state.log_event("info", "Heating ON")
            turn_on(PIN_HEATER)
            heater_on = True
            heater_on_since = loop_start
        elif heater_on_since and (loop_start - heater_on_since) > HEATER_MAX_ON_SECONDS:
            state.log_event("warning",
                             f"Heater safety cutoff: on continuously for over "
                             f"{HEATER_MAX_ON_SECONDS // 60} min without reaching setpoint",
                             category="heater_safety_cutoff")
            turn_off(PIN_HEATER)
            heater_on = False
            heater_on_since = None
            heater_lockout_until = loop_start + HEATER_LOCKOUT_SECONDS
    elif heater_on:
        state.log_event("info", "Heating complete - heater OFF")
        turn_off(PIN_HEATER)
        heater_on = False
        heater_on_since = None

    # === Humidity control ===
    if internal_humidity > HUMIDITY_SETPOINT + HUMIDITY_HYSTERESIS:
        if not humidity_on:
            state.log_event("info", f"Humidity {internal_humidity:.1f}% high - dehumidifier ON")
            turn_on(PIN_HUMIDITY)
            humidity_on = True
    elif humidity_on and internal_humidity < HUMIDITY_SETPOINT - HUMIDITY_HYSTERESIS:
        state.log_event("info", f"Humidity {internal_humidity:.1f}% low - dehumidifier OFF")
        turn_off(PIN_HUMIDITY)
        humidity_on = False

    # === Emergency checks (informational - outputs already governed above) ===
    if internal_temp < EXTREME_TEMP_LOW_F or internal_temp > EXTREME_TEMP_HIGH_F:
        logging.warning(f"Extreme temperature: {internal_temp:.1f}F")

    logging.info(
        f"Servo: {'OPEN' if servo_open else 'CLOSED'} | Fan: {'ON' if fan_on else 'OFF'} | "
        f"Heater: {'ON' if heater_on else 'OFF'} | Dehumidifier: {'ON' if humidity_on else 'OFF'} | "
        f"Vent cycle: {'ACTIVE' if vent_active else 'idle'} | "
        f"Door/Light: {'ON' if GPIO.input(PIN_LIGHT) == False else 'OFF'}"
    )

    # Pi health (CPU temp/load) - entirely separate from the enclosure's
    # own climate, sampled once per cycle purely for visibility into
    # whether the Pi itself is under strain (e.g. while tuning the
    # camera's capture/relay rate). Never fed into any control decision
    # above - get_pi_health() already never raises, so no try/except
    # needed here.
    pi_health = state.get_pi_health()

    # [2026-09-14] state.get_camera_stats() (a camera_service.py-measured
    # real-vs-target capture fps) is gone - it measured that process's
    # own continuous capture loop, which no longer exists now that live
    # view is served by camera-streamer instead (see
    # docs/camera-streamer-setup.md and PROJECT_STATUS.md). The matching
    # camera_actual_fps/camera_target_fps readings-table columns were
    # dropped on 2026-09-17 (see shared_state.py's
    # _migrate_readings_columns()) - log_reading() no longer takes them
    # at all.
    state.log_reading(mode, internal_temp, internal_humidity, external_temp, external_humidity,
                       fan_on, heater_on, humidity_on, vent_active,
                       ble_temp=ble_temp, ble_humidity=ble_humidity,
                       wired_temp=wired_temp, wired_humidity=wired_humidity,
                       active_external_source=active_external_source,
                       cpu_temp_f=pi_health["cpu_temp_f"], cpu_load_1m=pi_health["cpu_load_1m"])

    time.sleep(LOOP_INTERVAL)


def main():
    # init_db() must complete before the light_loop thread starts - that
    # thread hits the database (via save_door_state) on its very first
    # iteration, and racing it against table creation is exactly what
    # produced a startup "database is locked" crash before this fix.
    state.init_db()
    threading.Thread(target=light_loop, daemon=True).start()

    set_servo_angle(SERVO_CLOSED_ANGLE)
    logging.info("System started: servo closed, ready for climate control and door/light.")

    try:
        while True:
            try:
                run_cycle()
            except (KeyboardInterrupt, GracefulExit):
                raise
            except Exception:
                logging.exception("Unexpected error in control loop - will retry next cycle")
                state.log_event("error", "Unexpected error in control loop, see climate.log",
                                 category="control_loop_error")
                time.sleep(LOOP_INTERVAL)

    except (KeyboardInterrupt, GracefulExit):
        logging.info("Exiting safely...")

    finally:
        logging.info("Shutting down safely...")
        set_servo_angle(SERVO_CLOSED_ANGLE)
        servo.stop()
        for pin in [PIN_FAN, PIN_HEATER, PIN_HUMIDITY, PIN_LIGHT]:
            turn_off(pin)
        GPIO.cleanup()


if __name__ == "__main__":
    main()
