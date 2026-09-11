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
import adafruit_dht           # DHT22/AM2302 - actively maintained (see below)
import board
# busio / adafruit_sht31d are NOT imported here - they're optional, only
# needed if internal_source is "sht31", and are imported lazily inside
# _get_sht31_sensor() below. This lets the script run with only
# adafruit-circuitpython-dht installed when internal_source is "dht22"
# (the default) - no need to install SHT31-only packages you're not using.

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
DHT_READ_GAP_SECONDS = 1.0         # gap between back-to-back DHT22 reads
                                    # (internal + wired fallback) - a
                                    # mitigation for possible interference
                                    # between two timing-sensitive
                                    # single-wire reads with no gap at all
DHT_READ_RETRIES = 3               # attempts per DHT22 read before giving
                                    # up for this cycle - smooths over
                                    # ordinary single-attempt flakiness
DHT_READ_RETRY_DELAY_SECONDS = 0.5 # pause between retry attempts
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
PIN_INTERNAL_TEMP = 27    # wired DHT22/AM2302 (internal_source: "dht22", default)
PIN_EXTERNAL_TEMP = 4     # wired external probe fallback (local_gpio mode)
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

# === INTERNAL SENSOR (SHT31, I2C) - only touched if internal_source is "sht31" ===
# Needs I2C enabled via raspi-config - see README. Wiring: VIN->3.3V,
# GND->GND, SCL->GPIO3 (pin 5), SDA->GPIO2 (pin 3).
# Created lazily on first use, not here at import time - this script must
# still start cleanly on a system with no SHT31 wired up at all (e.g. while
# using a DHT22 instead), so nothing I2C-related runs unless requested.
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
sensor_fail_since = None
alarm_active = False

humidity_saturated_since = None
last_heater_recovery = 0


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


_dht_devices = {}  # GPIO pin number -> adafruit_dht.DHT22 instance, created lazily


def _get_dht_device(pin):
    if pin not in _dht_devices:
        board_pin = getattr(board, f"D{pin}")
        _dht_devices[pin] = adafruit_dht.DHT22(board_pin, use_pulseio=False)
    return _dht_devices[pin]


def read_temp_and_humidity_f(pin):
    """Wired DHT22/AM2302 probe - used for the internal sensor when
    internal_source is "dht22" (PIN_INTERNAL_TEMP), and for the external
    sensor's local_gpio fallback (PIN_EXTERNAL_TEMP).

    Uses adafruit-circuitpython-dht rather than the old Adafruit_DHT
    package: Adafruit has deprecated and archived Adafruit_DHT and
    directs everyone to the CircuitPython library instead. Practically,
    Adafruit_DHT's build-time Pi-detection code is also just broken on
    newer OS releases (it fails to install outright on Raspberry Pi OS
    Trixie), so there's no path back to it anyway.

    Retries a couple of times within this single call before giving up.
    DHT sensors fail an occasional individual read attempt as a matter
    of course - it's a timing-sensitive single-wire bit-banged protocol,
    not a robust checksummed bus like I2C, and even a healthy sensor can
    see the odd "no response"/"checksum did not validate" error. The old
    Adafruit_DHT library actually built retries in by default
    (read_retry()) for exactly this reason - the newer CircuitPython
    library this project uses does not, so it's worth doing here
    explicitly rather than treating every single failed attempt as a
    real sensor problem. Real-world evidence: dozens of "internal sensor
    unavailable" failsafe-countdown starts in one evening, each
    recovering within a cycle or two - consistent with ordinary
    single-attempt DHT flakiness, not a genuine sustained failure.
    """
    for attempt in range(DHT_READ_RETRIES):
        try:
            device = _get_dht_device(pin)
            temp_c = device.temperature
            humidity = device.humidity
        except RuntimeError:
            if attempt < DHT_READ_RETRIES - 1:
                time.sleep(DHT_READ_RETRY_DELAY_SECONDS)
                continue
            return None, None
        if temp_c is None or humidity is None:
            if attempt < DHT_READ_RETRIES - 1:
                time.sleep(DHT_READ_RETRY_DELAY_SECONDS)
                continue
            return None, None
        return temp_c * 9.0 / 5.0 + 32.0, humidity
    return None, None


def read_internal_sht31_f():
    """Internal SHT31 over I2C. Returns (None, None) on any I2C/CRC failure,
    or if no SHT31 is actually wired up (internal_source: "sht31" selected
    without the hardware present) - either way it flows into the same
    validation/failsafe pipeline as a failed wired read, rather than
    crashing the script."""
    try:
        sensor = _get_sht31_sensor()
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
    """Reject physically-implausible DHT glitches: an implausible jump
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
                     f"({streak}/{STREAK_CONFIRM_COUNT} consistent readings so far)")
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
    state.log_event("critical", f"EMERGENCY SHUTDOWN: {reason}")
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


def light_loop():
    """Drives the door/lid light off the same reed switch the whole time -
    unchanged. Also now tracks open/closed transitions for the dashboard's
    door indicator: written to the DB only on an actual transition (not
    on every 50ms poll), so it stays cheap but still reflects within one
    web dashboard refresh.

    Runs as a background thread for the life of the process, so any
    exception here needs to be caught and logged rather than allowed to
    kill the thread - an uncaught exception would silently disable the
    door light AND door tracking for the rest of the run, with the main
    control loop carrying on none the wiser."""
    last_open = None
    while True:
        try:
            is_open = GPIO.input(PIN_SWITCH) == GPIO.LOW
            if is_open:
                turn_on(PIN_LIGHT)
            else:
                turn_off(PIN_LIGHT)
            if is_open != last_open:
                state.save_door_state(is_open)
                state.log_event("info", f"Door {'opened' if is_open else 'closed'}")
                last_open = is_open
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
    global humidity_saturated_since, last_heater_recovery

    loop_start = time.time()
    config = state.load_config()
    mode = config["current_mode"]
    setpoints = config["modes"][mode]
    HIGH_TEMP_F = setpoints["high_temp_f"]
    LOW_TEMP_F = setpoints["low_temp_f"]
    HUMIDITY_SETPOINT = setpoints["humidity_setpoint"]

    internal_source = config.get("internal_source", "dht22")
    calib = config.get("calibration", {})
    if internal_source == "sht31":
        raw_internal_temp, raw_internal_humidity = read_internal_sht31_f()
    else:
        raw_internal_temp, raw_internal_humidity = read_temp_and_humidity_f(PIN_INTERNAL_TEMP)
        # A short gap before reading the second DHT22 below (the wired
        # external/fallback probe) - two of these timing-sensitive
        # single-wire sensors read back-to-back, with no gap at all, is a
        # plausible source of interference between the two reads. Only
        # needed when the internal sensor is ALSO a DHT22 - the SHT31
        # path above uses I2C, a completely different bus, so there's
        # nothing to interfere with there.
        time.sleep(DHT_READ_GAP_SECONDS)
    raw_internal_temp = apply_offset(raw_internal_temp, calib.get("internal_temp_offset", 0.0))
    raw_internal_humidity = apply_offset(raw_internal_humidity, calib.get("internal_humidity_offset", 0.0))

    # Both external sources are read every cycle. Which one is "active"
    # (drives control decisions) is decided automatically here, not by a
    # manual config toggle: SensorPush is used whenever it's reported
    # within SENSOR_FAIL_TIMEOUT (the same freshness window the failsafe
    # already uses elsewhere), and the wired probe is used automatically
    # otherwise - no manual switch, no missed data while nobody's
    # watching the dashboard. Whichever one ISN'T currently active is
    # kept as the fallback for visibility, same as before. The fallback
    # reading never touches any control-critical state (last_good_*, the
    # delta-glitch filter, the failsafe) - only basic plausibility bounds
    # apply to it, since a single bad fallback reading isn't dangerous
    # the way a bad ACTIVE reading would be, just cosmetically wrong for
    # one cycle.
    raw_wired_temp, raw_wired_humidity = read_temp_and_humidity_f(PIN_EXTERNAL_TEMP)
    raw_wired_temp = apply_offset(raw_wired_temp, calib.get("wired_temp_offset", 0.0))
    raw_wired_humidity = apply_offset(raw_wired_humidity, calib.get("wired_humidity_offset", 0.0))

    raw_sensorpush_temp = raw_sensorpush_humidity = None
    sensorpush_fresh = False
    if config.get("sensorpush_mac"):
        ble_reading = state.get_ble_reading(config["sensorpush_mac"])
        if ble_reading and (loop_start - ble_reading["ts"]) <= SENSOR_FAIL_TIMEOUT:
            raw_sensorpush_temp = apply_offset(ble_reading["temp_f"], calib.get("sensorpush_temp_offset", 0.0))
            raw_sensorpush_humidity = apply_offset(ble_reading["humidity"], calib.get("sensorpush_humidity_offset", 0.0))
            sensorpush_fresh = True

    if sensorpush_fresh:
        raw_external_temp, raw_external_humidity = raw_sensorpush_temp, raw_sensorpush_humidity
        raw_fallback_temp, raw_fallback_humidity = raw_wired_temp, raw_wired_humidity
        active_external_source = "sensorpush"
    else:
        raw_external_temp, raw_external_humidity = raw_wired_temp, raw_wired_humidity
        raw_fallback_temp, raw_fallback_humidity = raw_sensorpush_temp, raw_sensorpush_humidity
        active_external_source = "local_gpio"

    # Log the transition itself (once, not every cycle) - automatic
    # failover should still be genuinely visible on the Logs page, not
    # silent just because nobody has to click a switch for it anymore.
    global last_active_external_source
    if last_active_external_source is not None and active_external_source != last_active_external_source:
        if active_external_source == "local_gpio":
            state.log_event("warning", "Automatically failed over to wired probe (SensorPush stale)")
        else:
            state.log_event("info", "SensorPush recovered, resuming as primary external sensor")
    last_active_external_source = active_external_source

    if not _plausible(raw_fallback_temp, -40, 140):
        raw_fallback_temp = None
    if not _plausible(raw_fallback_humidity, 0, 100):
        raw_fallback_humidity = None
    fallback_temp, fallback_humidity = raw_fallback_temp, raw_fallback_humidity

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



    # SHT31 condensation recovery: only applies when the SHT31 is actually
    # the internal sensor - a DHT22 has no onboard heater to pulse, so this
    # whole mechanism is meaningless (and internal_sensor wouldn't even be
    # initialized) when internal_source is "dht22". This checks the RAW
    # (physically-plausible but not yet delta-validated) humidity,
    # deliberately upstream of the delta check below. A real condensation
    # event legitimately produces a fast jump to ~100% that the delta check
    # would otherwise reject as an implausible glitch every single cycle,
    # forever - since the reading never gets a chance to become "last good"
    # to compare against, this would look identical to a dead sensor and
    # eventually trip the emergency-shutdown failsafe instead of ever
    # getting a chance to dry itself out. Reacting to the raw value here is
    # what actually lets the heater pulse happen; the delta-validated value
    # below still is - and should remain - the only thing the
    # heat/cool/dehumidify logic acts on.
    if (internal_source == "sht31" and raw_internal_humidity is not None
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
            state.log_event("warning", "Internal sensor reading unavailable - starting failsafe countdown")
        elapsed = loop_start - sensor_fail_since
        logging.warning(f"Critical (internal) sensor read failed/rejected ({elapsed:.0f}s since last good reading)")
        if elapsed > SENSOR_FAIL_TIMEOUT and not alarm_active:
            alarm_active = True
            emergency_shutdown_outputs(f"No valid internal sensor readings for over {SENSOR_FAIL_TIMEOUT}s")
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
            state.log_event("warning", "Fan safety cutoff: exceeded max continuous runtime")
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
                             f"{HEATER_MAX_ON_SECONDS // 60} min without reaching setpoint")
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

    state.log_reading(mode, internal_temp, internal_humidity, external_temp, external_humidity,
                       fan_on, heater_on, humidity_on, vent_active, fallback_temp, fallback_humidity,
                       active_external_source)

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
                state.log_event("error", "Unexpected error in control loop, see climate.log")
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
