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
import time
import fcntl
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "dermestid.db")

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
    "modes": {
        "dormant": {
            # Cold enough to slow metabolism way down (less feeding,
            # less breeding) without risking cold-killing the colony.
            "low_temp_f": 55.0,
            "high_temp_f": 60.0,
            "humidity_setpoint": 40.0
        },
        "ready": {
            # Warm/humid enough for active feeding and breeding.
            "low_temp_f": 78.0,
            "high_temp_f": 85.0,
            "humidity_setpoint": 50.0
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
            "vent_duration_minutes": 5
        }
    }
}

# Sanity bounds used to validate anything coming in from the web UI.
TEMP_BOUNDS_F = (40.0, 100.0)
HUMIDITY_BOUNDS = (10.0, 90.0)
VENT_INTERVAL_BOUNDS = (5, 240)   # minutes
VENT_DURATION_BOUNDS = (1, 60)    # minutes

MAC_ADDRESS_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

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
    # Backfill any keys/modes added in later versions of this script so
    # an old config.json on disk doesn't crash a newer climate.py.
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update({k: v for k, v in data.items() if k not in ("modes", "calibration")})
    for mode_name, defaults in DEFAULT_CONFIG["modes"].items():
        merged["modes"][mode_name] = {**defaults, **data.get("modes", {}).get(mode_name, {})}
    merged["calibration"] = {**DEFAULT_CONFIG["calibration"], **data.get("calibration", {})}
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
                 active_external_source=None):
    """external_temp/humidity is whichever physical sensor is currently
    ACTIVE (drives control decisions) - it's the value the delta-glitch
    filter and failsafe machinery track continuously across cycles,
    regardless of which physical sensor is behind it at any moment.
    ble_temp/wired_temp are each physical sensor's OWN reading under its
    own fixed name, always, regardless of which one is active - so a
    dashboard tile for "the BLE sensor" always shows the BLE sensor,
    never silently relabeled to show the wired probe's data just
    because the wired probe happens to be the one currently active."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO readings "
        "(ts, mode, internal_temp, internal_humidity, external_temp, external_humidity, "
        "fan, heater, dehumidifier, vent, ble_temp, ble_humidity, wired_temp, wired_humidity, "
        "active_external_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), mode, internal_temp, internal_humidity, external_temp, external_humidity,
         int(fan), int(heater), int(dehumidifier), int(vent), ble_temp, ble_humidity, wired_temp, wired_humidity,
         active_external_source)
    )
    conn.commit()
    conn.close()


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
        "active_external_source FROM readings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    keys = ["ts", "mode", "internal_temp", "internal_humidity", "external_temp", "external_humidity",
            "fan", "heater", "dehumidifier", "vent", "ble_temp", "ble_humidity", "wired_temp", "wired_humidity",
            "active_external_source"]
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
        "fan, heater, dehumidifier, vent FROM readings WHERE 1=1"
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
            "fan", "heater", "dehumidifier", "vent"]
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
