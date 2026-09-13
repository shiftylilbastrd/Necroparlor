"""
Shared configuration and data-logging helpers used by both climate.py
(the control loop) and webapp.py (the local dashboard).

Both processes import this module rather than talking to each other
directly:
  - config.json holds the active mode + per-mode setpoints. Writes are
    atomic (write-tmp-then-rename) so a reader never sees a half-written
    file.
  - dermestid.db (SQLite, WAL mode) holds historical readings and an
    event log, so both processes can read/write concurrently without
    stepping on each other.

climate.py and webapp.py must live in the same directory as this file.
"""
import json
import os
import re
import sqlite3
import subprocess
import time
import fcntl
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "dermestid.db")

# Camera output - written by camera_service.py, read by webapp.py. Not
# git-tracked (runtime data, like the DB) - see .gitignore.
CAMERA_DIR = os.path.join(BASE_DIR, "camera")
CAMERA_LIVE_PATH = os.path.join(CAMERA_DIR, "latest.jpg")
CAMERA_TIMELAPSE_DIR = os.path.join(CAMERA_DIR, "timelapse")
CAMERA_TIMELAPSE_VIDEOS_DIR = os.path.join(CAMERA_DIR, "timelapse_videos")

# Used to anchor chart bucket boundaries to local midnight rather than
# UTC midnight (see _local_utc_offset_seconds in get_history) - hardcoded
# rather than reading the Pi's own system timezone, since that's one
# fewer thing that has to be correctly configured for this to work right.
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

VALID_MODES = ("dormant", "ready", "cleaning")

# Starting-point setpoints. These are reasonable defaults for a
# dermestid colony but are not a substitute for your own care sheet -
# verify against species-specific guidance and adjust from here.
DEFAULT_CONFIG = {
    "current_mode": "ready",
    # How often webapp.py checks GitHub for a new commit (read-only -
    # just updates the "update available" status, doesn't pull or
    # restart anything by itself). Minutes.
    "update_check_interval_minutes": 15,
    # Which git branch auto_update.sh and the background checker track.
    # Defaults to main - switching this to anything else means running
    # code that hasn't gone through the same scrutiny as what actually
    # ships to main, so it's meant for deliberate testing, not a normal
    # setting to leave changed long-term.
    "update_branch": "main",
    # Where climate.py gets the *internal* reading from:
    #   "dht22"  - wired DHT22/AM2302 probe on PIN_INTERNAL_TEMP (default -
    #              what's actually on hand)
    #   "sht31"  - I2C sensor with condensation-recovery heater support
    "internal_source": "dht22",
    # External (outside-air) reading: automatic failover, not a manual
    # choice. climate.py always reads both the wired probe and a BLE
    # sensor every cycle and uses whichever is fresher - the BLE sensor
    # if it's reported within SENSOR_FAIL_TIMEOUT, otherwise the wired
    # probe. ble_mac identifies *which* physical BLE unit to listen for
    # (useful if more than one is nearby); ble_sensor_type identifies
    # *which brand's* decoder to use, since different brands broadcast
    # different, incompatible advertisement formats - see
    # BLE_SENSOR_LIBRARIES below. Neither is a mode toggle.
    "ble_mac": None,
    "ble_sensor_type": "sensorpush",
    # The door/lid light (PIN_LIGHT) is normally slaved entirely to the
    # physical reed switch (see climate.py's light_loop()) - this is a
    # dashboard-driven manual override on top of that, for checking in on
    # the enclosure via the live view without having to actually open the
    # lid. 0 means no override active. A non-zero value is the epoch
    # timestamp it expires at, not a plain on/off flag - self-expiring
    # (see LIGHT_OVERRIDE_DURATION_SECONDS below) so a forgotten toggle or
    # a closed browser tab can't leave the light on indefinitely. The
    # physical switch still always wins when the door is actually open -
    # this only ever ADDS light-on time, never blocks the switch from
    # turning it on or off on its own.
    "light_override_until": 0,
    # Per-sensor calibration offsets, added to the raw reading before any
    # validation/control logic sees it - tied to the physical sensor
    # (internal/ble/wired), NOT to "active"/"fallback", since which
    # physical sensor plays which role can swap automatically during a
    # BLE sensor outage. An offset has to follow the actual hardware it
    # corrects for, not whatever label it's currently wearing on the
    # dashboard.
    "calibration": {
        "internal_temp_offset": 0.0,
        "internal_humidity_offset": 0.0,
        "ble_temp_offset": 0.0,
        "ble_humidity_offset": 0.0,
        "wired_temp_offset": 0.0,
        "wired_humidity_offset": 0.0,
    },
    # USB webcam settings for camera_service.py (optional feature - only
    # relevant if that service is installed/enabled, same as the BLE
    # listener). "device" is either a plain integer index ("0", "1", ...
    # matching /dev/videoN) or a full /dev path - a by-id path
    # (/dev/v4l/by-id/...) is more robust than a bare index if you ever
    # have more than one USB device, since indices can shuffle across a
    # reboot depending on enumeration order, a by-id symlink won't.
    # Resolution/quality are configurable (unlike most of climate.py's
    # tuning knobs) because they directly trade off against SD card
    # space for the timelapse - see snapshot_interval_minutes below.
    "camera": {
        "device": "0",
        "width": 1280,
        "height": 720,
        "jpeg_quality": 80,
        # Both of these are expressed as frames-per-second, not seconds-
        # between-frames - the two got confused with each other more than
        # once (by the person configuring this AND by the assistant
        # helping tune it) back when they were "intervals," since a
        # SMALLER interval means a FASTER/smoother feed, the opposite of
        # what "smaller = less" intuitively suggests. fps doesn't have
        # that problem: bigger number, more frames, plainly faster.
        # camera_service.py/webapp.py each convert their own fps value
        # to a sleep-interval internally (1.0 / fps) - config.json and
        # the Config page only ever deal in fps.
        "live_capture_fps": 5,       # camera_service.py's own capture rate
        "stream_relay_fps": 7,       # webapp.py's per-viewer MJPEG relay rate
    },
    # Cache of discover_camera.py's own probed results, keyed by device
    # index as a string (e.g. "0") - NOT a user-facing setting, just
    # memory of what Discover already found, so switching back to a
    # previously-discovered device (or just reloading the Config page)
    # doesn't fall back to the generic, unverified COMMON_RESOLUTIONS
    # list until Discover gets clicked again. Populated by
    # api_camera_discover() in webapp.py every time Discover actually
    # runs; only keyed by plain index, since that's the only form
    # selectCameraIndex() ever sets the device field to - a hand-typed
    # /dev/v4l/by-id/... path won't have a cached entry, which is fine,
    # it just falls back to COMMON_RESOLUTIONS like before this existed.
    "camera_known_resolutions": {},
    "modes": {
        "dormant": {
            # Cold enough to slow metabolism way down (less feeding,
            # less breeding) without risking cold-killing the colony.
            "low_temp_f": 55.0,
            "high_temp_f": 60.0,
            "humidity_setpoint": 40.0,
            # Timelapse snapshot cadence for this mode - 0 means never
            # (no snapshots captured while in this mode). Per-mode
            # rather than a single global setting, since a mode you
            # barely visit (dormant) plausibly warrants a different
            # cadence than one you're actively watching (ready).
            "snapshot_interval_minutes": 0
        },
        "ready": {
            # Warm/humid enough for active feeding and breeding.
            "low_temp_f": 78.0,
            "high_temp_f": 85.0,
            "humidity_setpoint": 50.0,
            "snapshot_interval_minutes": 0
        },
        "cleaning": {
            # Same thermal target as "ready" (colony stays active and
            # keeps working through a cleaning job) plus scheduled
            # forced-air cycles to keep odor from building up in the
            # ducted intake/exhaust.
            "low_temp_f": 78.0,
            "high_temp_f": 85.0,
            "humidity_setpoint": 50.0,
            "vent_interval_minutes": 30,
            "vent_duration_minutes": 5,
            "snapshot_interval_minutes": 0
        }
    }
}

# Sanity bounds used to validate anything coming in from the web UI.
TEMP_BOUNDS_F = (40.0, 100.0)
HUMIDITY_BOUNDS = (10.0, 90.0)
VENT_INTERVAL_BOUNDS = (5, 240)   # minutes
VENT_DURATION_BOUNDS = (1, 60)    # minutes
# 0 is the sentinel for "never" (no timelapse capture in that mode) and
# is validated separately from this range, same pattern as the update-
# branch/interval validators below.
SNAPSHOT_INTERVAL_BOUNDS = (1, 1440)  # minutes, when not 0/never

CAMERA_WIDTH_BOUNDS = (160, 1920)
CAMERA_HEIGHT_BOUNDS = (120, 1080)
CAMERA_QUALITY_BOUNDS = (30, 95)   # JPEG quality - below 30 is visibly
                                    # useless, above 95 has negligible
                                    # visual benefit for a large size cost
CAMERA_FPS_BOUNDS = (0.1, 20)  # frames/sec - camera_service.py's own
# capture rate. A floor of 0.1fps (one frame per 10s) rather than the
# old interval-based floor's equivalent of one frame per 30s: going
# slower than that isn't really a "live view" anymore, it overlaps with
# what the separate timelapse/snapshot_interval_minutes feature is
# already for - tightening this bound removes that redundant range
# rather than preserving it. See camera_service.py's docstring for the
# Pi-CPU-vs-smoothness trade-off at the fast end. Ceiling raised from an
# original 10 to match STREAM_RELAY_FPS_BOUNDS below - the original 10
# cap blocked testing higher rates before there was any real evidence
# it would help, and Pi-health data (see PROJECT_STATUS.md) shows this
# Pi has CPU/thermal headroom well beyond 10fps. Note: 5->10fps testing
# didn't noticeably reduce perceived "jitter," which points more at the
# capture/relay pipeline's frame-timing consistency than at the raw fps
# number - raising this ceiling is about not blocking further testing,
# not an expectation that a bigger number alone fixes the complaint.
STREAM_RELAY_FPS_BOUNDS = (0.2, 20)  # frames/sec - the per-viewer MJPEG
# relay rate (webapp.py's api_camera_stream), independent of the capture
# rate above - see the "camera" DEFAULT_CONFIG comment for why this is
# separately tunable. Ceiling is higher than CAMERA_FPS_BOUNDS's since
# relaying an already-captured frame is far cheaper than actually
# capturing/encoding a new one, so a faster relay is safe even when the
# capture rate itself can't go that fast.

# How long a dashboard-triggered light override lasts before it expires on
# its own (climate.py's light_loop() just compares against this timestamp,
# no separate "turn if off" action ever needs to run). 5 minutes is enough
# to look the enclosure over via the live view without babysitting a
# toggle, short enough that a forgotten/stuck browser tab doesn't leave
# the light on for hours.
LIGHT_OVERRIDE_DURATION_SECONDS = 300

MAC_ADDRESS_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
# Accepts a bare device index ("0", "1") or a /dev path, including a
# by-id symlink (/dev/v4l/by-id/usb-...). Not used in a shell command
# (cv2.VideoCapture takes it directly), so this is a sanity check
# against garbage input, not a shell-injection guard - still kept
# reasonably strict since there's no legitimate reason for a device
# string to contain anything outside this set.
CAMERA_DEVICE_RE = re.compile(r"^[A-Za-z0-9_/.-]{1,255}$")

# Registry of supported BLE sensor brands for the passive listener
# (ble_listener.py). Each entry names a PyPI package and the class
# within it that decodes that brand's advertisement format - all of
# these share the same interface (a BluetoothData subclass with a
# .update(service_info) method returning .entity_values), since
# they're siblings in the same open-source ecosystem Home Assistant
# uses for its native Bluetooth integrations. Adding a new brand is
# usually just: pip install <package>, then one line here.
#
# Confidence varies by entry - "sensorpush", "inkbird", and "govee"
# were directly confirmed against real source/usage examples; others
# follow the same well-established naming convention across this
# ecosystem but weren't individually verified. If a brand's class name
# has changed or is different than listed, ble_listener.py will fail
# with a clear ImportError naming the exact package/class it tried,
# not a silent failure - check that brand's PyPI page if so.
BLE_SENSOR_LIBRARIES = {
    "sensorpush": {"package": "sensorpush_ble", "class": "SensorPushBluetoothDeviceData", "label": "SensorPush"},
    "govee": {"package": "govee_ble", "class": "GoveeBluetoothDeviceData", "label": "Govee"},
    "inkbird": {"package": "inkbird_ble", "class": "INKBIRDBluetoothDeviceData", "label": "INKBIRD"},
    "xiaomi": {"package": "xiaomi_ble", "class": "XiaomiBluetoothDeviceData", "label": "Xiaomi"},
    "ruuvitag": {"package": "ruuvitag_ble", "class": "RuuviTagBluetoothDeviceData", "label": "RuuviTag"},
}


def validate_ble_mac(ble_mac):
    """Validates a BLE sensor address for saving - identifies which
    physical unit to listen for, not a mode toggle (external source
    selection is automatic failover, not manual). Returns (mac, None)
    on success or (None, error) on failure. An empty value is allowed -
    it just means no BLE sensor is configured, so climate.py always
    uses the wired probe until one is set."""
    if not ble_mac:
        return None, None
    if not MAC_ADDRESS_RE.match(ble_mac):
        return None, "ble_mac must look like AA:BB:CC:DD:EE:FF"
    return ble_mac.upper(), None


def validate_ble_sensor_type(sensor_type):
    """Validates a BLE sensor brand selection against the supported
    registry above. Returns (sensor_type, None) on success or
    (None, error) on failure."""
    if sensor_type not in BLE_SENSOR_LIBRARIES:
        return None, f"ble_sensor_type must be one of {sorted(BLE_SENSOR_LIBRARIES)}"
    return sensor_type, None



def validate_camera_settings(values):
    """Validates the USB webcam device/resolution/quality settings for
    saving. Returns (cleaned_dict, None) on success or (None, error) on
    failure. Deliberately doesn't try to open the device to confirm it
    actually exists/works - this just sanity-checks the shape of the
    input; camera_service.py logs a clear error on its own if the
    configured device can't actually be opened."""
    device = values.get("device", "0")
    if not isinstance(device, str) or not CAMERA_DEVICE_RE.match(device):
        return None, "device must be a device index (e.g. '0') or a /dev path"
    try:
        width = int(values.get("width", 1280))
        height = int(values.get("height", 720))
        quality = int(values.get("jpeg_quality", 80))
    except (TypeError, ValueError):
        return None, "width, height, and jpeg_quality must be whole numbers"
    try:
        # Frames/sec, not seconds-between-frames - see the "camera"
        # DEFAULT_CONFIG comment for why. Rounded so tiny float noise
        # from the Config page's number input doesn't accumulate into
        # config.json.
        live_fps = round(float(values.get("live_capture_fps", 5)), 2)
    except (TypeError, ValueError):
        return None, "live_capture_fps must be a number"
    try:
        relay_fps = round(float(values.get("stream_relay_fps", 7)), 2)
    except (TypeError, ValueError):
        return None, "stream_relay_fps must be a number"
    if not (CAMERA_WIDTH_BOUNDS[0] <= width <= CAMERA_WIDTH_BOUNDS[1]):
        return None, f"width must be between {CAMERA_WIDTH_BOUNDS[0]} and {CAMERA_WIDTH_BOUNDS[1]}"
    if not (CAMERA_HEIGHT_BOUNDS[0] <= height <= CAMERA_HEIGHT_BOUNDS[1]):
        return None, f"height must be between {CAMERA_HEIGHT_BOUNDS[0]} and {CAMERA_HEIGHT_BOUNDS[1]}"
    if not (CAMERA_QUALITY_BOUNDS[0] <= quality <= CAMERA_QUALITY_BOUNDS[1]):
        return None, f"jpeg_quality must be between {CAMERA_QUALITY_BOUNDS[0]} and {CAMERA_QUALITY_BOUNDS[1]}"
    if not (CAMERA_FPS_BOUNDS[0] <= live_fps <= CAMERA_FPS_BOUNDS[1]):
        return None, (f"live_capture_fps must be between "
                       f"{CAMERA_FPS_BOUNDS[0]} and {CAMERA_FPS_BOUNDS[1]}")
    if not (STREAM_RELAY_FPS_BOUNDS[0] <= relay_fps <= STREAM_RELAY_FPS_BOUNDS[1]):
        return None, (f"stream_relay_fps must be between "
                       f"{STREAM_RELAY_FPS_BOUNDS[0]} and {STREAM_RELAY_FPS_BOUNDS[1]}")
    return {
        "device": device,
        "width": width,
        "height": height,
        "jpeg_quality": quality,
        "live_capture_fps": live_fps,
        "stream_relay_fps": relay_fps,
    }, None


CALIBRATION_KEYS = (
    "internal_temp_offset", "internal_humidity_offset",
    "ble_temp_offset", "ble_humidity_offset",
    "wired_temp_offset", "wired_humidity_offset",
)


def validate_calibration(values):
    """Validates a full set of calibration offsets for saving. `values`
    should have all six CALIBRATION_KEYS. Returns (cleaned_dict, None) on
    success or (None, error) on failure. Offsets are bounded to +/-20 -
    generous for genuine sensor calibration (a cheap sensor reading more
    than 20 degrees or 20%RH off is broken, not just uncalibrated) while
    still catching an obviously wrong entry (a stray extra digit, a
    misplaced decimal point) before it quietly corrupts every reading
    from that sensor."""
    cleaned = {}
    for key in CALIBRATION_KEYS:
        try:
            v = float(values.get(key, 0.0))
        except (TypeError, ValueError):
            return None, f"{key} must be a number"
        if not (-20 <= v <= 20):
            return None, f"{key} must be between -20 and 20"
        cleaned[key] = v
    return cleaned, None


def _atomic_write(path, data_str):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(data_str)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp_path, path)


def atomic_write_bytes(path, data_bytes):
    """Same write-tmp-then-rename pattern as _atomic_write, for binary
    data - used by camera_service.py so webapp.py (or a browser
    fetching /api/camera/latest.jpg directly) never reads a half-written
    JPEG. Public (no leading underscore) since it's used from a
    different module, unlike the JSON-specific version above."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(data_bytes)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp_path, path)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            data = json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    # One-time migration from the old SensorPush-specific key names to
    # the new brand-agnostic ones (sensorpush_mac -> ble_mac,
    # sensorpush_*_offset -> ble_*_offset). Without this, upgrading past
    # that rename would silently reset an already-configured MAC address
    # and calibration offsets back to their defaults on the very next
    # load - the data wouldn't actually be gone, just sitting under a
    # key nothing reads anymore. Naturally completes itself on the next
    # save (of anything), since it rewrites `data` in place before the
    # merge below.
    if "sensorpush_mac" in data and "ble_mac" not in data:
        data["ble_mac"] = data.pop("sensorpush_mac")
    if "calibration" in data:
        cal = data["calibration"]
        if "sensorpush_temp_offset" in cal and "ble_temp_offset" not in cal:
            cal["ble_temp_offset"] = cal.pop("sensorpush_temp_offset")
        if "sensorpush_humidity_offset" in cal and "ble_humidity_offset" not in cal:
            cal["ble_humidity_offset"] = cal.pop("sensorpush_humidity_offset")
    # Same idea, for the camera interval settings' seconds -> fps unit
    # change: a VALUE conversion, not just a key rename, so a plain
    # backfill (below) can't do this on its own - an old config.json
    # missing live_capture_fps would otherwise silently fall back to the
    # DEFAULT_CONFIG fps value instead of preserving whatever rate was
    # actually configured under the old key.
    if "camera" in data:
        cam = data["camera"]
        if "live_capture_interval_seconds" in cam and "live_capture_fps" not in cam:
            old_interval = cam.pop("live_capture_interval_seconds")
            try:
                cam["live_capture_fps"] = round(1.0 / float(old_interval), 2)
            except (TypeError, ValueError, ZeroDivisionError):
                pass  # leave it out - the backfill below fills in the default
        if "stream_relay_interval_seconds" in cam and "stream_relay_fps" not in cam:
            old_interval = cam.pop("stream_relay_interval_seconds")
            try:
                cam["stream_relay_fps"] = round(1.0 / float(old_interval), 2)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    # Backfill any keys/modes added in later versions of this script so
    # an old config.json on disk doesn't crash a newer climate.py.
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update({k: v for k, v in data.items() if k not in ("modes", "calibration", "camera")})
    for mode_name, defaults in DEFAULT_CONFIG["modes"].items():
        merged["modes"][mode_name] = {**defaults, **data.get("modes", {}).get(mode_name, {})}
    merged["calibration"] = {**DEFAULT_CONFIG["calibration"], **data.get("calibration", {})}
    # Shallow-merged like calibration above (not replaced outright like
    # most top-level keys) so a config.json from before this field
    # existed - or one that only ever saved a subset of camera keys -
    # still backfills whichever camera settings it's missing from
    # DEFAULT_CONFIG, rather than silently losing them.
    merged["camera"] = {**DEFAULT_CONFIG["camera"], **data.get("camera", {})}
    return merged


def save_config(config):
    _atomic_write(CONFIG_PATH, json.dumps(config, indent=2))


def validate_setpoints(mode, values):
    """Validate setpoint values coming from the web UI.
    Returns (cleaned_dict, None) on success or (None, error_message) on failure.
    """
    if mode not in VALID_MODES:
        return None, f"Unknown mode '{mode}'"
    try:
        low = float(values["low_temp_f"])
        high = float(values["high_temp_f"])
        humidity = float(values["humidity_setpoint"])
    except (KeyError, TypeError, ValueError):
        return None, "low_temp_f, high_temp_f and humidity_setpoint are required numbers"

    if not (TEMP_BOUNDS_F[0] <= low <= TEMP_BOUNDS_F[1]):
        return None, f"low_temp_f must be between {TEMP_BOUNDS_F[0]} and {TEMP_BOUNDS_F[1]}"
    if not (TEMP_BOUNDS_F[0] <= high <= TEMP_BOUNDS_F[1]):
        return None, f"high_temp_f must be between {TEMP_BOUNDS_F[0]} and {TEMP_BOUNDS_F[1]}"
    if low >= high:
        return None, "low_temp_f must be less than high_temp_f"
    if not (HUMIDITY_BOUNDS[0] <= humidity <= HUMIDITY_BOUNDS[1]):
        return None, f"humidity_setpoint must be between {HUMIDITY_BOUNDS[0]} and {HUMIDITY_BOUNDS[1]}"

    cleaned = {"low_temp_f": low, "high_temp_f": high, "humidity_setpoint": humidity}

    if mode == "cleaning":
        try:
            interval = int(values.get("vent_interval_minutes", 30))
            duration = int(values.get("vent_duration_minutes", 5))
        except (TypeError, ValueError):
            return None, "vent_interval_minutes and vent_duration_minutes must be integers"
        if not (VENT_INTERVAL_BOUNDS[0] <= interval <= VENT_INTERVAL_BOUNDS[1]):
            return None, (f"vent_interval_minutes must be between "
                           f"{VENT_INTERVAL_BOUNDS[0]} and {VENT_INTERVAL_BOUNDS[1]}")
        if not (VENT_DURATION_BOUNDS[0] <= duration <= VENT_DURATION_BOUNDS[1]):
            return None, (f"vent_duration_minutes must be between "
                           f"{VENT_DURATION_BOUNDS[0]} and {VENT_DURATION_BOUNDS[1]}")
        if duration >= interval:
            return None, "vent_duration_minutes should be shorter than vent_interval_minutes"
        cleaned["vent_interval_minutes"] = interval
        cleaned["vent_duration_minutes"] = duration

    # Timelapse cadence applies to every mode, not just cleaning - 0 is
    # the "never" sentinel and is deliberately exempt from
    # SNAPSHOT_INTERVAL_BOUNDS (which only constrains the non-zero case).
    try:
        snapshot_interval = int(values.get("snapshot_interval_minutes", 0))
    except (TypeError, ValueError):
        return None, "snapshot_interval_minutes must be a whole number"
    if snapshot_interval != 0 and not (SNAPSHOT_INTERVAL_BOUNDS[0] <= snapshot_interval <= SNAPSHOT_INTERVAL_BOUNDS[1]):
        return None, (f"snapshot_interval_minutes must be 0 (never) or between "
                       f"{SNAPSHOT_INTERVAL_BOUNDS[0]} and {SNAPSHOT_INTERVAL_BOUNDS[1]}")
    cleaned["snapshot_interval_minutes"] = snapshot_interval

    return cleaned, None


# --------------------------------------------------------------------------
# SQLite storage for readings + events (powers the dashboard graph/log)
# --------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            ts REAL PRIMARY KEY,
            mode TEXT,
            internal_temp REAL,
            internal_humidity REAL,
            external_temp REAL,
            external_humidity REAL,
            fan INTEGER,
            heater INTEGER,
            dehumidifier INTEGER,
            vent INTEGER
        )
    """)
    _migrate_readings_columns(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            ts REAL,
            level TEXT,
            message TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ble_readings (
            address TEXT PRIMARY KEY,
            ts REAL,
            temp_f REAL,
            humidity REAL,
            rssi INTEGER,
            battery_pct REAL,
            battery_voltage REAL,
            battery_ts REAL
        )
    """)
    _migrate_ble_readings_columns(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS door_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            is_open INTEGER,
            ts REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS update_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            is_available INTEGER,
            local_commit TEXT,
            remote_commit TEXT,
            remote_message TEXT,
            checked_at REAL
        )
    """)
    _migrate_update_state_columns(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS camera_snapshots (
            ts REAL PRIMARY KEY,
            mode TEXT,
            filename TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timelapse_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            mode TEXT,
            start_ts REAL,
            end_ts REAL,
            frame_count INTEGER,
            filename TEXT,
            poster_filename TEXT,
            duration_seconds REAL,
            file_size_bytes INTEGER
        )
    """)
    conn.commit()
    conn.close()


def _migrate_readings_columns(conn):
    """Adds columns to readings for DBs that predate them."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(readings)").fetchall()}
    if "external_humidity" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN external_humidity REAL")
    if "fallback_external_temp" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN fallback_external_temp REAL")
    if "fallback_external_humidity" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN fallback_external_humidity REAL")
    if "active_external_source" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN active_external_source TEXT")
    # ble_temp/wired_temp replace fallback_external_temp/humidity above -
    # each physical sensor's reading now lives under its own fixed name
    # regardless of which one is currently active, instead of whichever
    # one ISN'T active being stored as a generic, role-based "fallback"
    # value. The old fallback_external_* columns are kept (unused going
    # forward) purely so historical rows already written under the old
    # scheme don't lose data.
    if "ble_temp" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN ble_temp REAL")
    if "ble_humidity" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN ble_humidity REAL")
    if "wired_temp" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN wired_temp REAL")
    if "wired_humidity" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN wired_humidity REAL")
    # Pi health (see get_pi_health()) - sampled once per control cycle by
    # climate.py alongside the sensor readings, purely for visibility into
    # whether the Pi itself (not the enclosure) is under strain - e.g.
    # while tuning camera.live_capture_fps/stream_relay_fps for a
    # smoother live feed. Never touches any
    # control-critical logic (failsafe, delta-glitch filter, etc.) -
    # informational only, same treatment as ble_temp/wired_temp above.
    if "cpu_temp_f" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN cpu_temp_f REAL")
    if "cpu_load_1m" not in existing:
        conn.execute("ALTER TABLE readings ADD COLUMN cpu_load_1m REAL")


def _migrate_ble_readings_columns(conn):
    """Adds battery columns to ble_readings if this DB was created by an
    earlier version of this project that didn't have them yet."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ble_readings)").fetchall()}
    for column, coltype in [("battery_pct", "REAL"), ("battery_voltage", "REAL"), ("battery_ts", "REAL")]:
        if column not in existing:
            conn.execute(f"ALTER TABLE ble_readings ADD COLUMN {column} {coltype}")


def save_ble_reading(address, temp_f, humidity, rssi):
    """Called by ble_listener.py for every decoded advertisement it
    sees, keyed by BLE address so multiple sensors can be tracked at once
    even though only one is currently wired into the control loop. Only
    touches the passive-advertisement columns - leaves battery fields
    (which come from a separate active-connection check) untouched."""
    conn = get_db()
    conn.execute(
        "INSERT INTO ble_readings (address, ts, temp_f, humidity, rssi) VALUES (?,?,?,?,?) "
        "ON CONFLICT(address) DO UPDATE SET ts=excluded.ts, temp_f=excluded.temp_f, "
        "humidity=excluded.humidity, rssi=excluded.rssi",
        (address.upper(), time.time(), temp_f, humidity, rssi)
    )
    conn.commit()
    conn.close()


def save_ble_battery(address, battery_pct, battery_voltage):
    """Called by ble_battery.py after its periodic active-connection
    battery check (currently only implemented for SensorPush's HT1 -
    see that file). Only touches the battery columns - leaves whatever
    temp/humidity/rssi the passive listener last wrote alone."""
    conn = get_db()
    conn.execute(
        "INSERT INTO ble_readings (address, battery_pct, battery_voltage, battery_ts) VALUES (?,?,?,?) "
        "ON CONFLICT(address) DO UPDATE SET battery_pct=excluded.battery_pct, "
        "battery_voltage=excluded.battery_voltage, battery_ts=excluded.battery_ts",
        (address.upper(), battery_pct, battery_voltage, time.time())
    )
    conn.commit()
    conn.close()


def get_ble_reading(address):
    conn = get_db()
    row = conn.execute(
        "SELECT ts, temp_f, humidity, rssi, battery_pct, battery_voltage, battery_ts "
        "FROM ble_readings WHERE address = ?",
        (address.upper(),)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"ts": row[0], "temp_f": row[1], "humidity": row[2], "rssi": row[3],
            "battery_pct": row[4], "battery_voltage": row[5], "battery_ts": row[6]}


def get_all_ble_readings():
    """Every BLE sensor currently being heard, regardless of which one
    (if any) is wired into the control loop - useful for the discovery
    helper and for a future multi-sensor dashboard."""
    conn = get_db()
    rows = conn.execute(
        "SELECT address, ts, temp_f, humidity, rssi, battery_pct, battery_voltage, battery_ts "
        "FROM ble_readings"
    ).fetchall()
    conn.close()
    return [{"address": r[0], "ts": r[1], "temp_f": r[2], "humidity": r[3], "rssi": r[4],
             "battery_pct": r[5], "battery_voltage": r[6], "battery_ts": r[7]} for r in rows]


def log_reading(mode, internal_temp, internal_humidity, external_temp, external_humidity,
                 fan, heater, dehumidifier, vent,
                 ble_temp=None, ble_humidity=None, wired_temp=None, wired_humidity=None,
                 active_external_source=None, cpu_temp_f=None, cpu_load_1m=None):
    """external_temp/humidity is whichever physical sensor is currently
    ACTIVE (drives control decisions) - it's the value the delta-glitch
    filter and failsafe machinery track continuously across cycles,
    regardless of which physical sensor is behind it at any moment.
    ble_temp/wired_temp are each physical sensor's OWN reading under its
    own fixed name, always, regardless of which one is active - so a
    dashboard tile for "the BLE sensor" always shows the BLE sensor,
    never silently relabeled to show the wired probe's data just
    because the wired probe happens to be the one currently active.
    cpu_temp_f/cpu_load_1m are the Pi's OWN health (see get_pi_health()),
    unrelated to the enclosure's climate - purely informational."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO readings "
        "(ts, mode, internal_temp, internal_humidity, external_temp, external_humidity, "
        "fan, heater, dehumidifier, vent, ble_temp, ble_humidity, wired_temp, wired_humidity, "
        "active_external_source, cpu_temp_f, cpu_load_1m) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), mode, internal_temp, internal_humidity, external_temp, external_humidity,
         int(fan), int(heater), int(dehumidifier), int(vent), ble_temp, ble_humidity, wired_temp, wired_humidity,
         active_external_source, cpu_temp_f, cpu_load_1m)
    )
    conn.commit()
    conn.close()


def get_pi_health():
    """Reads the Pi's own CPU temperature, load average, and
    under-voltage/throttling flags - entirely separate from the
    enclosure's own climate sensors, purely for visibility into whether
    the PI ITSELF is under strain (e.g. while tuning the camera's
    capture/relay rate for a smoother live feed, or chasing a BLE
    stability issue possibly tied to under-voltage - see
    PROJECT_STATUS.md's BLE Tier-3 entries, where `vcgencmd
    get_throttled` was already being checked by hand for exactly this
    reason before this function existed).

    `vcgencmd` is Raspberry Pi firmware-specific and only meaningful on
    real Pi hardware - returns a dict of all-None values (never raises)
    if it's missing entirely, so this is always safe to call from
    anywhere, including in this project's own test/dev environment.

    get_throttled's hex value is a bitmask: bit0/bit1 are CURRENT
    under-voltage/freq-capping, bit16/bit18 are STICKY (have occurred at
    some point since boot, not necessarily right now) - both are
    surfaced separately since "currently throttled" and "was throttled
    at some point" call for different reactions.
    """
    result = {
        "cpu_temp_f": None,
        "cpu_load_1m": None,
        "cpu_load_5m": None,
        "cpu_load_15m": None,
        "throttled_now": None,
        "throttled_since_boot": None,
    }

    try:
        load_1m, load_5m, load_15m = os.getloadavg()
        result["cpu_load_1m"] = round(load_1m, 2)
        result["cpu_load_5m"] = round(load_5m, 2)
        result["cpu_load_15m"] = round(load_15m, 2)
    except OSError:
        # getloadavg() is POSIX-only, and can theoretically raise if the
        # kernel doesn't support it - not expected on a Pi, but this is
        # informational data, never worth crashing over.
        pass

    try:
        temp_out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                                   text=True, timeout=5)
        # Expected output: "temp=53.8'C\n"
        if temp_out.returncode == 0 and "temp=" in temp_out.stdout:
            temp_c = float(temp_out.stdout.split("temp=")[1].split("'")[0])
            result["cpu_temp_f"] = round(temp_c * 9.0 / 5.0 + 32.0, 1)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError, OSError):
        # FileNotFoundError specifically means vcgencmd isn't installed at
        # all (e.g. this code running somewhere other than a real Pi) -
        # every other exception here is some other form of "couldn't get
        # a reading this cycle," same non-fatal treatment.
        pass

    try:
        throttled_out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                        text=True, timeout=5)
        # Expected output: "throttled=0x50000\n"
        if throttled_out.returncode == 0 and "throttled=" in throttled_out.stdout:
            bits = int(throttled_out.stdout.split("throttled=")[1].strip(), 16)
            result["throttled_now"] = bool(bits & 0x1) or bool(bits & 0x2)
            result["throttled_since_boot"] = bool(bits & 0x10000) or bool(bits & 0x40000)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError, OSError):
        pass

    return result


def log_event(level, message):
    """Records a state-change/alarm event to SQLite AND to the Python
    logging module, so it shows up in both the dashboard event log and
    the on-disk log file."""
    conn = get_db()
    conn.execute("INSERT INTO events VALUES (?,?,?)", (time.time(), level, message))
    conn.commit()
    conn.close()
    getattr(logging, level.lower(), logging.info)(message)


def get_last_valid_timestamps():
    """For each tile's metric, when did it last actually have a valid
    (non-null) reading - not just when the most recent row was written.
    A tile's value can go null for a cycle or several (a failed read, a
    stale external source) while the row itself keeps getting written -
    this is what lets the dashboard show a genuine per-tile 'last
    updated' time instead of implying every tile refreshed just because
    /api/status was polled again.

    Limited to the last 24h so this stays a cheap, fast query regardless
    of how large the readings table eventually grows - a metric with no
    valid reading in the last 24h is already extremely stale, so there's
    no need to scan further back to say so.
    """
    conn = get_db()
    since = time.time() - 86400
    row = conn.execute(
        """
        SELECT MAX(CASE WHEN internal_temp IS NOT NULL THEN ts END),
               MAX(CASE WHEN ble_temp IS NOT NULL THEN ts END),
               MAX(CASE WHEN wired_temp IS NOT NULL THEN ts END)
        FROM readings
        WHERE ts >= ?
        """,
        (since,)
    ).fetchone()
    conn.close()
    return {"internal": row[0], "ble": row[1], "wired": row[2]}


def get_latest_reading():
    conn = get_db()
    row = conn.execute(
        "SELECT ts, mode, internal_temp, internal_humidity, external_temp, external_humidity, "
        "fan, heater, dehumidifier, vent, ble_temp, ble_humidity, wired_temp, wired_humidity, "
        "active_external_source, cpu_temp_f, cpu_load_1m FROM readings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    keys = ["ts", "mode", "internal_temp", "internal_humidity", "external_temp", "external_humidity",
            "fan", "heater", "dehumidifier", "vent", "ble_temp", "ble_humidity", "wired_temp", "wired_humidity",
            "active_external_source", "cpu_temp_f", "cpu_load_1m"]
    return dict(zip(keys, row))


def _local_utc_offset_seconds():
    """Current UTC offset for LOCAL_TZ, in seconds (negative for zones
    west of UTC, e.g. -25200 for Pacific Daylight). Recomputed on every
    call rather than cached, so it's automatically correct across DST
    transitions without needing a service restart."""
    return int(datetime.now(LOCAL_TZ).utcoffset().total_seconds())


def get_history(hours):
    """Returns downsampled time-series points covering the last `hours`
    hours, bucketed/averaged so long ranges don't ship huge numbers of
    rows to the browser. Each metric also carries its min/max within the
    bucket, for a mean-with-band chart (like Home Assistant's Statistics
    Graph card) - bands are naturally near-zero width when a bucket only
    holds one raw reading and widen when it holds several, exactly
    reflecting real variance, not a fabricated effect.

    Bucket size is a fixed, human-meaningful interval per range tier
    (matching the dashboard's four range buttons) rather than a sliding
    point-count target - so the chart's grid lines land on predictable
    boundaries (every 15 min, every hour, etc.) at every zoom level
    instead of some arbitrary in-between spacing.

    Boundaries are anchored to LOCAL midnight, not UTC midnight: readings
    are stored as raw UTC epoch seconds, and naively flooring those to a
    bucket size gives boundaries that land on UTC-clean-but-locally-odd
    clock times (e.g. a 6h bucket edge at UTC midnight is 5pm the
    previous day in Pacific time - not a clean local hour at all). Each
    timestamp is shifted into local-equivalent seconds before bucketing,
    then shifted back so the returned value is still a valid UTC epoch
    the frontend can display correctly - the net effect is that an 8h
    bucket's edges land on local 12am/6am/12pm/6pm rather than an arbitrary
    UTC-derived local time.
    """
    conn = get_db()
    since = time.time() - hours * 3600
    if hours <= 1:
        bucket_seconds = 2 * 60            # 1h view  -> 2-minute buckets
    elif hours <= 6:
        bucket_seconds = 15 * 60          # 6h view  -> 15-minute buckets
    elif hours <= 24:
        bucket_seconds = 60 * 60          # 24h view -> 1-hour buckets
    elif hours <= 168:
        bucket_seconds = 6 * 60 * 60       # 7d view  -> 6-hour buckets
    else:
        bucket_seconds = 24 * 60 * 60     # 30d view -> 1-day buckets
    offset = _local_utc_offset_seconds()
    rows = conn.execute(
        """
        SELECT CAST((ts + ?) / ? AS INTEGER) * ? - ? AS bucket,
               AVG(internal_temp), MIN(internal_temp), MAX(internal_temp),
               AVG(internal_humidity), MIN(internal_humidity), MAX(internal_humidity),
               AVG(ble_temp), MIN(ble_temp), MAX(ble_temp),
               AVG(ble_humidity), MIN(ble_humidity), MAX(ble_humidity),
               AVG(wired_temp), MIN(wired_temp), MAX(wired_temp),
               AVG(wired_humidity), MIN(wired_humidity), MAX(wired_humidity)
        FROM readings
        WHERE ts >= ?
        GROUP BY bucket
        ORDER BY bucket ASC
        """,
        (offset, bucket_seconds, bucket_seconds, offset, since)
    ).fetchall()
    conn.close()
    return [
        {
            "ts": r[0],
            "internal_temp": r[1], "internal_temp_min": r[2], "internal_temp_max": r[3],
            "internal_humidity": r[4], "internal_humidity_min": r[5], "internal_humidity_max": r[6],
            "ble_temp": r[7], "ble_temp_min": r[8], "ble_temp_max": r[9],
            "ble_humidity": r[10], "ble_humidity_min": r[11], "ble_humidity_max": r[12],
            "wired_temp": r[13], "wired_temp_min": r[14], "wired_temp_max": r[15],
            "wired_humidity": r[16], "wired_humidity_min": r[17], "wired_humidity_max": r[18],
        }
        for r in rows
    ]


def save_door_state(is_open):
    """Called by climate.py's light_loop() only when the door actually
    transitions open/closed - not polled continuously - so this stays
    cheap even though the switch itself is checked every 50ms."""
    conn = get_db()
    conn.execute(
        "INSERT INTO door_state (id, is_open, ts) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET is_open=excluded.is_open, ts=excluded.ts",
        (int(is_open), time.time())
    )
    conn.commit()
    conn.close()


def get_door_state():
    conn = get_db()
    row = conn.execute("SELECT is_open, ts FROM door_state WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return None
    return {"is_open": bool(row[0]), "ts": row[1]}


def _migrate_update_state_columns(conn):
    """Adds branch-tracking columns to update_state for DBs that predate
    branch selection support."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(update_state)").fetchall()}
    if "current_branch" not in existing:
        conn.execute("ALTER TABLE update_state ADD COLUMN current_branch TEXT")
    if "target_branch" not in existing:
        conn.execute("ALTER TABLE update_state ADD COLUMN target_branch TEXT")


def save_update_status(is_available, local_commit, remote_commit, remote_message,
                        current_branch=None, target_branch=None):
    """Called by webapp.py's background update-checker thread after every
    GitHub check (read-only - this never pulls or restarts anything by
    itself, just records what it found). current_branch/target_branch
    let the dashboard distinguish "new commits on the branch you're
    already on" from "a different branch is selected and hasn't been
    switched to yet" - two different situations that need different
    handling by auto_update.sh."""
    conn = get_db()
    conn.execute(
        "INSERT INTO update_state (id, is_available, local_commit, remote_commit, remote_message, "
        "checked_at, current_branch, target_branch) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET is_available=excluded.is_available, "
        "local_commit=excluded.local_commit, remote_commit=excluded.remote_commit, "
        "remote_message=excluded.remote_message, checked_at=excluded.checked_at, "
        "current_branch=excluded.current_branch, target_branch=excluded.target_branch",
        (int(is_available), local_commit, remote_commit, remote_message, time.time(),
         current_branch, target_branch)
    )
    conn.commit()
    conn.close()


def get_update_status():
    conn = get_db()
    row = conn.execute(
        "SELECT is_available, local_commit, remote_commit, remote_message, checked_at, "
        "current_branch, target_branch FROM update_state WHERE id = 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"is_available": bool(row[0]), "local_commit": row[1], "remote_commit": row[2],
            "remote_message": row[3], "checked_at": row[4],
            "current_branch": row[5], "target_branch": row[6]}


UPDATE_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def validate_update_branch(branch):
    """Returns (branch, None) on success or (None, error) on failure.
    Deliberately strict (letters/digits/dot/underscore/slash/dash only) -
    this value gets interpolated into a shell command in auto_update.sh,
    so beyond just being a plausible branch name, it must not be able to
    smuggle in shell metacharacters."""
    if not branch or not UPDATE_BRANCH_RE.match(branch):
        return None, "branch name must contain only letters, numbers, dots, underscores, dashes, and slashes"
    if branch.startswith(".") or branch.startswith("/") or ".." in branch:
        return None, "not a valid branch name"
    return branch, None


def validate_update_interval(minutes):
    """Returns (minutes, None) on success or (None, error) on failure."""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return None, "interval must be a whole number of minutes"
    if not (1 <= minutes <= 1440):
        return None, "interval must be between 1 and 1440 minutes"
    return minutes, None


def get_readings_table(limit=50, before_ts=None):
    """Raw, un-bucketed readings rows for the Data page - every column,
    newest first, same before_ts cursor-pagination pattern as
    get_recent_events(). Unlike get_history() (which averages into
    buckets for charting), this returns exactly what's in the database
    row by row - useful for the kind of close diagnosis a chart can
    smooth over, like spotting a specific cycle's raw value.

    Includes both external_temp/humidity (whichever physical sensor was
    ACTIVE that cycle - what actually drove the heat/cool decision) and
    ble_temp/wired_temp (each physical sensor's own reading, always,
    regardless of which was active) - useful together for diagnosing
    exactly what each sensor was reporting independent of which one was
    driving control at the time."""
    conn = get_db()
    query = (
        "SELECT ts, mode, internal_temp, internal_humidity, external_temp, external_humidity, "
        "ble_temp, ble_humidity, wired_temp, wired_humidity, active_external_source, "
        "fan, heater, dehumidifier, vent, cpu_temp_f, cpu_load_1m FROM readings WHERE 1=1"
    )
    params = []
    if before_ts:
        query += " AND ts < ?"
        params.append(before_ts)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    keys = ["ts", "mode", "internal_temp", "internal_humidity", "external_temp", "external_humidity",
            "ble_temp", "ble_humidity", "wired_temp", "wired_humidity", "active_external_source",
            "fan", "heater", "dehumidifier", "vent", "cpu_temp_f", "cpu_load_1m"]
    return [dict(zip(keys, r)) for r in rows]


def get_recent_events(limit=50, level=None, before_ts=None):
    """level: optional exact-match filter ('info'/'warning'/'error'/'critical').
    before_ts: optional epoch timestamp - only events strictly older than this,
    for "load more" pagination on the logs page."""
    conn = get_db()
    query = "SELECT ts, level, message FROM events WHERE 1=1"
    params = []
    if level:
        query += " AND level = ?"
        params.append(level)
    if before_ts:
        query += " AND ts < ?"
        params.append(before_ts)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{"ts": r[0], "level": r[1], "message": r[2]} for r in rows]


# --------------------------------------------------------------------------
# Camera / timelapse (written by camera_service.py, read by webapp.py)
# --------------------------------------------------------------------------

def save_camera_snapshot(mode, jpeg_bytes):
    """Writes a timelapse frame to disk under CAMERA_TIMELAPSE_DIR and
    records it in the DB in one call, keyed by the same WHOLE-SECOND
    timestamp used as both the DB primary key and the filename - so the
    two can never disagree about which row a given file belongs to, and
    webapp.py's /api/camera/snapshot/<int:ts>.jpg route can look a row
    up directly by the integer it was given in the URL with no separate
    lookup table or fractional-second rounding to worry about. (Whole
    seconds are more than enough resolution for a snapshot cadence
    measured in minutes.) INSERT OR REPLACE mirrors log_reading()'s
    handling of its own ts-keyed table, for the same reason: two calls
    landing in the same second should overwrite, not raise.

    Not wrapped in the atomic-write-then-rename pattern used for the
    live frame (CAMERA_LIVE_PATH) - each timelapse file has its own
    unique path, so there's no reader that could ever observe a half-
    written *different* file the way a reader of the single shared
    latest.jpg path could."""
    os.makedirs(CAMERA_TIMELAPSE_DIR, exist_ok=True)
    ts = int(time.time())
    filename = f"{ts}.jpg"
    with open(os.path.join(CAMERA_TIMELAPSE_DIR, filename), "wb") as f:
        f.write(jpeg_bytes)
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO camera_snapshots (ts, mode, filename) VALUES (?,?,?)", (ts, mode, filename))
    conn.commit()
    conn.close()
    return ts, filename


def get_camera_snapshots(limit=50, before_ts=None):
    """Same before_ts cursor-pagination pattern as get_recent_events/
    get_readings_table, for the Camera page's timelapse gallery."""
    conn = get_db()
    query = "SELECT ts, mode, filename FROM camera_snapshots WHERE 1=1"
    params = []
    if before_ts:
        query += " AND ts < ?"
        params.append(before_ts)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{"ts": r[0], "mode": r[1], "filename": r[2]} for r in rows]


def get_camera_snapshot(ts):
    conn = get_db()
    row = conn.execute("SELECT ts, mode, filename FROM camera_snapshots WHERE ts = ?", (ts,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"ts": row[0], "mode": row[1], "filename": row[2]}


def get_camera_snapshot_count():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM camera_snapshots").fetchone()[0]
    conn.close()
    return count


def prune_oldest_camera_snapshots(n):
    """Deletes the n oldest timelapse snapshots (DB row + file).

    This exists purely as a disk-space SAFETY NET for camera_service.py,
    not a day-to-day retention policy - the whole point of a timelapse
    is to keep frames, so this only fires when free disk space actually
    drops below a hardcoded threshold (see CAMERA_LOW_DISK_* in
    camera_service.py), the same "safety guardrail, not a setting"
    philosophy as climate.py's heater/fan runtime cutoffs. Returns how
    many were actually removed (can be fewer than n if there aren't
    that many yet)."""
    conn = get_db()
    rows = conn.execute("SELECT ts, filename FROM camera_snapshots ORDER BY ts ASC LIMIT ?", (n,)).fetchall()
    removed = 0
    for ts, filename in rows:
        try:
            os.remove(os.path.join(CAMERA_TIMELAPSE_DIR, filename))
        except FileNotFoundError:
            pass
        conn.execute("DELETE FROM camera_snapshots WHERE ts = ?", (ts,))
        removed += 1
    conn.commit()
    conn.close()
    return removed


def get_earliest_camera_snapshot_ts(mode):
    """The oldest not-yet-compiled frame currently sitting in
    camera_snapshots for this mode, or None if there isn't one.

    camera_service.py deletes a mode's snapshot rows the moment they're
    successfully compiled into a timelapse video (see
    save_timelapse_video/delete raw frames below), so whatever's left in
    this table for a given mode is, by construction, exactly that mode's
    CURRENT in-progress session - nothing here has been compiled yet.
    That invariant is what lets camera_service.py recover the right
    session start time across a service restart (a deploy, a crash, a
    manual restart) without needing to persist any extra state of its
    own: it just asks "what's the oldest uncompiled frame for the mode
    we're in right now?" instead of trusting an in-memory variable that
    a restart would have reset."""
    conn = get_db()
    row = conn.execute(
        "SELECT MIN(ts) FROM camera_snapshots WHERE mode = ?", (mode,)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else None


def get_camera_snapshots_in_range(mode, start_ts, end_ts):
    """All of one mode's timelapse frames within [start_ts, end_ts],
    oldest first - the exact input a session's ffmpeg compile needs, in
    playback order."""
    conn = get_db()
    rows = conn.execute(
        "SELECT ts, mode, filename FROM camera_snapshots "
        "WHERE mode = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
        (mode, start_ts, end_ts)
    ).fetchall()
    conn.close()
    return [{"ts": r[0], "mode": r[1], "filename": r[2]} for r in rows]


def delete_camera_snapshots(ts_list):
    """Deletes specific timelapse frames (DB row + file) by timestamp -
    used after a successful video compile, when the raw frames that went
    into it are no longer needed (the video is now the lasting record;
    see camera_service.py's compile step for why keeping both
    indefinitely isn't worth the disk space on a Pi's SD card)."""
    if not ts_list:
        return 0
    conn = get_db()
    removed = 0
    for ts in ts_list:
        row = conn.execute("SELECT filename FROM camera_snapshots WHERE ts = ?", (ts,)).fetchone()
        if row:
            try:
                os.remove(os.path.join(CAMERA_TIMELAPSE_DIR, row[0]))
            except FileNotFoundError:
                pass
            conn.execute("DELETE FROM camera_snapshots WHERE ts = ?", (ts,))
            removed += 1
    conn.commit()
    conn.close()
    return removed


def save_timelapse_video(mode, start_ts, end_ts, frame_count, filename, poster_filename,
                          duration_seconds, file_size_bytes):
    """Records a compiled session video - one row per completed
    ffmpeg compile (see camera_service.py). Deliberately keyed by an
    ordinary autoincrement id, NOT by timestamp like camera_snapshots/
    ble_readings are - compiling now happens in its own background
    thread per mode (see maybe_compile_session), so two different modes'
    sessions can legitimately finish compiling within the same wall-clock
    second; a timestamp-keyed INSERT OR REPLACE would silently make the
    second one overwrite the first instead of both existing (caught by a
    smoke test before this ever shipped). `ts` is kept as a plain column
    for display/sorting/pagination, just no longer unique. Returns the
    new row's id, which is what the download/poster/delete routes
    actually address a video by."""
    os.makedirs(CAMERA_TIMELAPSE_VIDEOS_DIR, exist_ok=True)
    conn = get_db()
    ts = time.time()
    cursor = conn.execute(
        "INSERT INTO timelapse_videos "
        "(ts, mode, start_ts, end_ts, frame_count, filename, poster_filename, duration_seconds, file_size_bytes) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, mode, start_ts, end_ts, frame_count, filename, poster_filename, duration_seconds, file_size_bytes)
    )
    conn.commit()
    video_id = cursor.lastrowid
    conn.close()
    return video_id


def get_timelapse_videos(limit=50, before_ts=None):
    """Same before_ts cursor-pagination pattern as get_recent_events/
    get_camera_snapshots, for the Timelapse page's video gallery. Still
    paginates by `ts` (compile time), not `id` - either works since they
    sort identically, but ts is the one already meaningful to a caller
    building a "load older" cursor."""
    conn = get_db()
    query = ("SELECT id, ts, mode, start_ts, end_ts, frame_count, filename, poster_filename, "
              "duration_seconds, file_size_bytes FROM timelapse_videos WHERE 1=1")
    params = []
    if before_ts:
        query += " AND ts < ?"
        params.append(before_ts)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [
        {"id": r[0], "ts": r[1], "mode": r[2], "start_ts": r[3], "end_ts": r[4], "frame_count": r[5],
         "filename": r[6], "poster_filename": r[7], "duration_seconds": r[8], "file_size_bytes": r[9]}
        for r in rows
    ]


def get_timelapse_video(video_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, ts, mode, start_ts, end_ts, frame_count, filename, poster_filename, "
        "duration_seconds, file_size_bytes FROM timelapse_videos WHERE id = ?",
        (video_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "ts": row[1], "mode": row[2], "start_ts": row[3], "end_ts": row[4], "frame_count": row[5],
            "filename": row[6], "poster_filename": row[7], "duration_seconds": row[8], "file_size_bytes": row[9]}


def delete_timelapse_video(video_id):
    """Deletes one compiled video (DB row + the .mp4 and its poster .jpg).
    Returns True if a row was actually found and removed."""
    conn = get_db()
    row = conn.execute("SELECT filename, poster_filename FROM timelapse_videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        conn.close()
        return False
    filename, poster_filename = row
    for name in (filename, poster_filename):
        if name:
            try:
                os.remove(os.path.join(CAMERA_TIMELAPSE_VIDEOS_DIR, name))
            except FileNotFoundError:
                pass
    conn.execute("DELETE FROM timelapse_videos WHERE id = ?", (video_id,))
    conn.commit()
    conn.close()
    return True
