#!/usr/bin/env python3
"""
One-shot battery check for the external SensorPush sensor.

Battery level isn't in the passive advertisement (see
sensorpush_listener.py) - SensorPush only exposes it over an active
BLE connection. This script connects briefly, reads the battery
characteristic, computes voltage and an approximate percentage, then
disconnects immediately.

Meant to run periodically (see systemd/dermestid-battery.timer -
daily by default), NOT continuously: the HT1 allows only one BLE
connection at a time, so holding one open would block the SensorPush
phone app (if you also use it) from ever connecting.

If a check fails - e.g. the phone app happened to be connected at the
same moment - it's logged as a warning and simply retried at the next
scheduled run. No connection means no reading, not a crash.

GATT characteristic and voltage formula are from the community's
reverse-engineered HT1 protocol documentation (MIT licensed):
https://github.com/wxfield/ha-sensorpush-ht1
"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import struct

from bleak import BleakClient

import shared_state as state

LOG_DIR = os.path.join(state.BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(os.path.join(LOG_DIR, "battery.log"),
                             maxBytes=500_000, backupCount=2),
        logging.StreamHandler()
    ]
)

BATTERY_CHAR_UUID = "ef090007-11d6-42ba-93b8-9dd7ec090aa9"
CONNECT_TIMEOUT_SECONDS = 30
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10

# CR2 lithium coin cell discharge curve, per the HT1 protocol docs:
# roughly 3.1V fresh, 2.1V empty. This is a linear approximation good
# enough for a "getting low, plan a swap" signal - not a precise gauge.
BATTERY_FULL_V = 3.1
BATTERY_EMPTY_V = 2.1
LOW_BATTERY_PCT = 15


def voltage_to_percent(voltage):
    pct = (voltage - BATTERY_EMPTY_V) / (BATTERY_FULL_V - BATTERY_EMPTY_V) * 100.0
    return max(0.0, min(100.0, round(pct, 1)))


async def read_battery_voltage(address):
    async with BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS) as client:
        raw = await client.read_gatt_char(BATTERY_CHAR_UUID)
        adc_raw, _die_temp_raw = struct.unpack("<HH", raw)
        return (adc_raw & 0x7FFF) * 3.6 / 1024


async def check_with_retries(address):
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(read_battery_voltage(address), timeout=CONNECT_TIMEOUT_SECONDS)
        except Exception as exc:
            last_error = exc
            logging.warning(f"Attempt {attempt}/{RETRY_ATTEMPTS} failed for {address}: {exc}")
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
    raise last_error


async def main():
    config = state.load_config()
    if config.get("external_source") != "sensorpush" or not config.get("sensorpush_mac"):
        logging.info("External source is not set to SensorPush - nothing to check.")
        return

    address = config["sensorpush_mac"]
    logging.info(f"Checking battery for {address}...")
    try:
        voltage = await check_with_retries(address)
    except Exception:
        logging.exception(f"Could not read battery from {address} after {RETRY_ATTEMPTS} attempts")
        state.log_event("warning",
                         f"SensorPush battery check failed for {address} - will retry next scheduled run")
        return

    pct = voltage_to_percent(voltage)
    state.save_ble_battery(address, pct, voltage)

    if pct <= LOW_BATTERY_PCT:
        state.log_event("warning", f"SensorPush battery low: ~{pct:.0f}% ({voltage:.2f}V) - plan a swap soon")
    else:
        state.log_event("info", f"SensorPush battery check: ~{pct:.0f}% ({voltage:.2f}V)")


if __name__ == "__main__":
    asyncio.run(main())
