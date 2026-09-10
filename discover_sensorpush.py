#!/usr/bin/env python3
"""
One-time helper: scans for nearby SensorPush sensors and prints each
one's BLE address plus its live reading, so you can figure out which
physical unit (of your three) is which.

All SensorPush HT1 units advertise under the same generic name ("s"),
so there's no way to tell them apart by name - only by BLE address.
Tip: warm one sensor in your hand while this runs and watch which
address's temperature climbs, then note that address down.

Run: python3 discover_sensorpush.py
Stop with Ctrl+C. Put the address you want as your external/duct
sensor into config.json's "sensorpush_mac" field (or set it from the
dashboard once that's wired up).
"""
import asyncio

from bleak import BleakScanner
from bluetooth_sensor_state_data import BluetoothServiceInfo
from sensorpush_ble import SensorPushBluetoothDeviceData

_parsers = {}


def on_advertisement(device, advertisement_data):
    address = device.address
    if address not in _parsers:
        _parsers[address] = SensorPushBluetoothDeviceData()

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
    print("Scanning for SensorPush sensors (Ctrl+C to stop)...")
    print("Tip: warm one sensor in your hand to see which address reacts.\n")
    scanner = BleakScanner(detection_callback=on_advertisement)
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
