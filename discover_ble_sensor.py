#!/usr/bin/env python3
"""
One-time helper: scans for nearby BLE sensors (brand selected by
config.json's "ble_sensor_type") and prints each one's BLE address
plus its live reading, so you can figure out which physical unit (if
you have more than one) is which.

Many BLE sensor brands advertise under the same generic name for every
unit of that model, so there's often no way to tell them apart by name
- only by BLE address. Tip: warm one sensor in your hand while this
runs and watch which address's temperature climbs, then note that
address down.

Run: python3 discover_ble_sensor.py
Stop with Ctrl+C. Put the address you want as your external/duct
sensor into config.json's "ble_mac" field (or set it from the
dashboard's Config page).
"""
import asyncio

from bleak import BleakScanner
from bluetooth_sensor_state_data import BluetoothServiceInfo

import shared_state as state
from ble_listener import load_decoder_class

_parsers = {}


def on_advertisement(device, advertisement_data, decoder_class):
    address = device.address
    if address not in _parsers:
        _parsers[address] = decoder_class()

    service_info = BluetoothServiceInfo.from_advertisement(device, advertisement_data, "discover")
    try:
        update = _parsers[address].update(service_info)
    except Exception:
        return

    temp_c = humidity = None
    for key, value in update.entity_values.items():
        if key.key == "temperature":
            temp_c = value.native_value
        elif key.key == "humidity":
            humidity = value.native_value

    if temp_c is None:
        return

    temp_f = temp_c * 9.0 / 5.0 + 32.0
    humidity_str = f"{humidity:5.1f}%RH" if humidity is not None else "  n/a  "
    print(f"{address}   {temp_f:5.1f}F   {humidity_str}   rssi={advertisement_data.rssi}")


async def main():
    config = state.load_config()
    sensor_type = config.get("ble_sensor_type", "sensorpush")
    decoder_class = load_decoder_class(sensor_type)
    label = state.BLE_SENSOR_LIBRARIES[sensor_type]["label"]

    print(f"Scanning for {label} sensors (Ctrl+C to stop)...")
    print("Tip: warm one sensor in your hand to see which address reacts.\n")
    scanner = BleakScanner(
        detection_callback=lambda device, adv: on_advertisement(device, adv, decoder_class)
    )
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await scanner.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
