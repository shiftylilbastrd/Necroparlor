#!/usr/bin/env python3
"""
Passive BLE listener for SensorPush HT1 / HT.w / HTP.xw sensors.

SensorPush sensors continuously broadcast their temperature and
humidity in an unencrypted BLE advertisement - no pairing, no
connection, no app or gateway required. This script just listens
(using the Pi's onboard Bluetooth radio) and writes the latest
decoded reading for every SensorPush device it hears into the shared
SQLite DB, keyed by BLE address. climate.py then reads whichever
address is configured as the "external" sensor from there instead of
a wired GPIO probe.

Note: battery level is NOT included - SensorPush only exposes that
over an active GATT connection, which this script deliberately avoids
(passive listening only, so it never touches the sensors' battery
budget beyond what they already spend broadcasting).

Run under systemd (see systemd/dermestid-sensorpush.service).
"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time

from bleak import BleakScanner
from bluetooth_sensor_state_data import BluetoothServiceInfo
from sensorpush_ble import SensorPushBluetoothDeviceData

import shared_state as state

LOG_DIR = os.path.join(state.BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(os.path.join(LOG_DIR, "sensorpush.log"),
                             maxBytes=1_000_000, backupCount=3),
        logging.StreamHandler()
    ]
)

# If no SensorPush reading has actually been decoded in this long, assume
# the BLE scan has silently stalled - a known real-world issue with
# long-running BlueZ/bleak scans: the process stays alive and systemd
# has no way to know anything's wrong (Restart=on-failure only triggers
# on an actual exit), but the underlying scan just stops delivering
# advertisements after enough hours. The watchdog below exits the
# process deliberately when this happens, so systemd's existing
# Restart=on-failure brings it back up fresh.
WATCHDOG_TIMEOUT = 600  # 10 minutes - readings normally arrive every
                         # few seconds, so this is a generous margin that
                         # won't false-trigger on ordinary signal dips

_last_reading_time = None

# One decoder per BLE address - SensorPush's decoder needs to see
# consecutive advertisements from the *same* device to track state
# correctly, so these must not be shared across addresses.
_parsers = {}


def on_advertisement(device, advertisement_data):
    global _last_reading_time
    address = device.address
    if address not in _parsers:
        _parsers[address] = SensorPushBluetoothDeviceData()

    service_info = BluetoothServiceInfo.from_advertisement(
        device, advertisement_data, "dermestid_sensorpush")

    try:
        update = _parsers[address].update(service_info)
    except Exception:
        logging.exception(f"Failed to decode advertisement from {address}")
        return

    temp_c = humidity = None
    for key, value in update.entity_values.items():
        if key.key == "temperature":
            temp_c = value.native_value
        elif key.key == "humidity":
            humidity = value.native_value

    if temp_c is None or humidity is None:
        return  # not a SensorPush device, or this advertisement didn't carry a full reading

    temp_f = temp_c * 9.0 / 5.0 + 32.0
    state.save_ble_reading(address, temp_f, humidity, advertisement_data.rssi)
    logging.info(f"{address}: {temp_f:.1f}F / {humidity:.1f}%RH (rssi {advertisement_data.rssi})")
    _last_reading_time = time.time()


async def main():
    global _last_reading_time
    state.init_db()
    logging.info("Starting passive BLE scan for SensorPush sensors...")
    scanner = BleakScanner(detection_callback=on_advertisement)
    await scanner.start()
    _last_reading_time = time.time()  # grace period starts now, before any real reading exists yet
    try:
        while True:
            await asyncio.sleep(60)
            if time.time() - _last_reading_time > WATCHDOG_TIMEOUT:
                logging.error(
                    f"No SensorPush reading decoded in over {WATCHDOG_TIMEOUT // 60} "
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
