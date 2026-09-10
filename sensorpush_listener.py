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

# One decoder per BLE address - SensorPush's decoder needs to see
# consecutive advertisements from the *same* device to track state
# correctly, so these must not be shared across addresses.
_parsers = {}


def on_advertisement(device, advertisement_data):
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


async def main():
    state.init_db()
    logging.info("Starting passive BLE scan for SensorPush sensors...")
    scanner = BleakScanner(detection_callback=on_advertisement)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await scanner.stop()


if __name__ == "__main__":
    asyncio.run(main())
