#!/usr/bin/env python3
"""
Passive BLE listener for a temperature/humidity sensor - brand
selected by config.json's "ble_sensor_type" (see
shared_state.BLE_SENSOR_LIBRARIES for the supported list).

Most BLE sensors continuously broadcast their temperature and humidity
in an unencrypted advertisement - no pairing, no connection, no app or
gateway required. This script just listens (using the Pi's onboard
Bluetooth radio) and writes the latest decoded reading for every
matching device it hears into the shared SQLite DB, keyed by BLE
address. climate.py then reads whichever address is configured as the
external sensor from there instead of a wired GPIO probe.

Switching brands is a config change, not a code change, PROVIDED the
new brand has a decoder in BLE_SENSOR_LIBRARIES (most popular consumer
BLE temp/humidity sensors do - they're siblings in the same
open-source ecosystem Home Assistant uses for its native Bluetooth
integrations, sharing an identical decode interface). Changing
ble_sensor_type requires restarting this service - it's read once at
startup, not re-checked every cycle, since a brand swap is a
deliberate hardware change, not something that happens mid-session.

Note: battery level often ISN'T in the passive advertisement - some
brands include it (saved here for free when present), others (like
SensorPush) only expose it over a separate active GATT connection,
which this script deliberately avoids for the brands that don't
include it passively (see ble_battery.py, currently SensorPush-HT1-
specific, for that separate mechanism).

Run under systemd (see systemd/dermestid-ble.service).
"""
import asyncio
import importlib
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time

from bleak import BleakScanner
from bluetooth_sensor_state_data import BluetoothServiceInfo

import shared_state as state

LOG_DIR = os.path.join(state.BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(os.path.join(LOG_DIR, "ble.log"),
                             maxBytes=1_000_000, backupCount=3),
        logging.StreamHandler()
    ]
)

# If no reading has actually been decoded in this long, assume the BLE
# scan has silently stalled - a known real-world issue with
# long-running BlueZ/bleak scans: the process stays alive and systemd
# has no way to know anything's wrong (Restart=on-failure only triggers
# on an actual exit), but the underlying scan just stops delivering
# advertisements after enough hours. The watchdog below exits the
# process deliberately when this happens, so systemd's existing
# Restart=on-failure brings it back up fresh.
#
# Originally 600s (10 minutes), based on an assumption this would be a
# rare, multi-hour event. Real-world evidence on this specific Pi shows
# a much more frequent pattern instead: a clean ~10-minute stretch of
# good readings, then a stall that a simple process restart reliably
# clears - confirmed twice in the same session, each restart producing
# another ~10 clean minutes before the next stall. Under a 10-minute
# timeout, that meant roughly HALF the time was spent showing stale
# data while waiting for the watchdog to notice. Shortened to 2 minutes:
# still a huge multiple of the normal few-seconds-between-readings
# cadence (won't false-trigger on ordinary signal dips), but cuts the
# stale-data window dramatically for this observed stall pattern.
WATCHDOG_TIMEOUT = 120  # 2 minutes

_last_reading_time = None
_decoder_class = None  # set once in main() from the configured brand

# One decoder instance per BLE address - these decoders need to see
# consecutive advertisements from the *same* device to track state
# correctly, so instances must not be shared across addresses.
_parsers = {}


def load_decoder_class(sensor_type):
    """Dynamically imports the decoder class for the configured BLE
    sensor brand, from the registry in shared_state.py. Only the
    package for the brand actually in use needs to be installed - not
    every possible brand - which is why this is a dynamic import at
    startup rather than a static one at the top of the file."""
    if sensor_type not in state.BLE_SENSOR_LIBRARIES:
        raise ValueError(
            f"Unknown ble_sensor_type '{sensor_type}' - must be one of "
            f"{sorted(state.BLE_SENSOR_LIBRARIES)}"
        )
    info = state.BLE_SENSOR_LIBRARIES[sensor_type]
    try:
        module = importlib.import_module(info["package"])
    except ImportError as e:
        pip_name = info["package"].replace("_", "-")
        raise ImportError(
            f"Could not import '{info['package']}' for ble_sensor_type="
            f"'{sensor_type}' ({info['label']}). Install it with: "
            f"pip3 install {pip_name} --break-system-packages"
        ) from e
    try:
        return getattr(module, info["class"])
    except AttributeError as e:
        raise AttributeError(
            f"Package '{info['package']}' has no class '{info['class']}' - "
            f"its API may have changed since this was written. Check "
            f"https://pypi.org/project/{info['package'].replace('_', '-')}/ "
            f"for the current class name and update shared_state.BLE_SENSOR_LIBRARIES."
        ) from e


def on_advertisement(device, advertisement_data):
    global _last_reading_time
    address = device.address
    if address not in _parsers:
        _parsers[address] = _decoder_class()

    service_info = BluetoothServiceInfo.from_advertisement(
        device, advertisement_data, "dermestid_ble")

    try:
        update = _parsers[address].update(service_info)
    except Exception:
        logging.exception(f"Failed to decode advertisement from {address}")
        return

    temp_c = humidity = battery_pct = None
    for key, value in update.entity_values.items():
        if key.key == "temperature":
            temp_c = value.native_value
        elif key.key == "humidity":
            humidity = value.native_value
        elif key.key == "battery":
            battery_pct = value.native_value

    if temp_c is None or humidity is None:
        return  # not a matching device, or this advertisement didn't carry a full reading

    temp_f = temp_c * 9.0 / 5.0 + 32.0
    state.save_ble_reading(address, temp_f, humidity, advertisement_data.rssi)
    if battery_pct is not None:
        # Some brands include battery right in the passive advertisement
        # (unlike SensorPush, which needs the separate active-connection
        # check in ble_battery.py) - save it here for free when available,
        # no extra connection needed.
        state.save_ble_battery(address, battery_pct, None)
    batt_str = f" batt={battery_pct:.0f}%" if battery_pct is not None else ""
    logging.info(f"{address}: {temp_f:.1f}F / {humidity:.1f}%RH (rssi {advertisement_data.rssi}){batt_str}")
    _last_reading_time = time.time()


async def start_scanner_with_retry(scanner, max_attempts=5, retry_delay=5):
    """BlueZ can get stuck reporting a scan as 'already in progress'
    (org.bluez.Error.InProgress) if a previous process was killed
    without cleanly stopping its scan first - which is exactly what can
    happen after the watchdog below deliberately hard-exits rather than
    risk hanging on a graceful shutdown. This stuck state lives in
    bluetoothd itself (a separate system service), not in this process,
    so simply restarting this script alone doesn't fix it - but BlueZ's
    internal scan state typically does clear on its own within a few
    seconds of the old D-Bus client disappearing, so a few retries
    (with an explicit stop() attempt first, to help nudge it along)
    usually recovers without needing an external Bluetooth service
    restart at all."""
    for attempt in range(1, max_attempts + 1):
        try:
            await scanner.start()
            return
        except Exception as e:
            logging.warning(f"scanner.start() failed (attempt {attempt}/{max_attempts}): {e}")
            try:
                await scanner.stop()
            except Exception:
                pass
            if attempt < max_attempts:
                await asyncio.sleep(retry_delay)
    raise RuntimeError(f"Could not start BLE scan after {max_attempts} attempts")


async def main():
    global _last_reading_time, _decoder_class
    state.init_db()
    config = state.load_config()
    sensor_type = config.get("ble_sensor_type", "sensorpush")
    _decoder_class = load_decoder_class(sensor_type)
    label = state.BLE_SENSOR_LIBRARIES[sensor_type]["label"]
    logging.info(f"Starting passive BLE scan for {label} sensors...")
    scanner = BleakScanner(detection_callback=on_advertisement)
    await start_scanner_with_retry(scanner)
    _last_reading_time = time.time()  # grace period starts now, before any real reading exists yet
    try:
        while True:
            await asyncio.sleep(15)  # tightened from 60s to stay proportional
                                      # to the shorter WATCHDOG_TIMEOUT above -
                                      # detection lag is now at most ~15s past
                                      # the threshold instead of up to 60s
            if time.time() - _last_reading_time > WATCHDOG_TIMEOUT:
                logging.error(
                    f"No reading decoded in over {WATCHDOG_TIMEOUT // 60} "
                    "minutes - the BLE scan has likely silently stalled. Exiting "
                    "immediately (not attempting a graceful scanner.stop() first, "
                    "in case that itself is part of what's stuck) so systemd "
                    "restarts this service with a fresh scan."
                )
                os._exit(1)  # hard exit - bypasses try/finally entirely,
                              # guaranteed not to hang even if the
                              # underlying BLE session is itself wedged
    finally:
        await scanner.stop()


if __name__ == "__main__":
    asyncio.run(main())
