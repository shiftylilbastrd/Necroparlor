#!/usr/bin/env python3
"""
USB webcam service for the dermestid enclosure - entirely optional, like
ble_listener.py. If you're not using a webcam, don't install/enable
systemd/dermestid-camera.service; nothing else in this project depends
on it.

Two independent things happen on the same loop, both driven by
config.json so a dashboard change takes effect within one cycle without
a restart (same reasoning as climate.py re-reading its own config):

  - Live view: every `camera.live_capture_interval_seconds` (default
    2s), grab a frame and atomically overwrite camera/latest.jpg.
    webapp.py's /api/camera/latest.jpg just serves whatever's currently
    there - deliberately NOT a per-viewer video stream, so multiple
    simultaneous dashboard viewers never each try to open the USB
    device themselves (most UVC webcams only support one client at a
    time in the first place, so that would just break the second
    viewer).
  - Timelapse: on the CURRENTLY ACTIVE MODE's own
    `snapshot_interval_minutes` (per-mode in config.json, 0 = never),
    save a permanent frame into camera/timelapse/ plus a DB row via
    shared_state.save_camera_snapshot(). Tracked with a single
    last-saved timestamp regardless of which mode was active when it
    was set - switching from a long-interval mode to a short-interval
    one doesn't itself fire an immediate snapshot just because the mode
    changed; the new interval simply starts being measured against
    whenever the last snapshot actually happened.

A hardcoded, non-configurable disk-space safety net (see
CAMERA_LOW_DISK_THRESHOLD_MB below) prunes the OLDEST timelapse frames
if free space gets dangerously low - the whole point of a timelapse is
to keep frames, so this is a last resort to keep the SD card from
filling up and taking down the Pi (which would also kill climate
control - the actually safety-critical part of this project), not a
day-to-day retention policy. If you're hitting it regularly, lower the
snapshot interval, resolution, or JPEG quality on the Config page
instead of relying on it.

Run under systemd (see systemd/dermestid-camera.service).
"""
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import time

import cv2

import shared_state as state

LOG_DIR = os.path.join(state.BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(os.path.join(LOG_DIR, "camera.log"),
                             maxBytes=1_000_000, backupCount=3),
        logging.StreamHandler()
    ]
)

# If no frame has been successfully read in this long, assume the
# capture device itself is wedged - a known real-world failure mode for
# USB UVC webcams on Linux (the device node can hang, or vanish and
# reappear, after a transient USB fault) rather than failing cleanly on
# every call. Same watchdog philosophy as ble_listener.py's BLE-scan-
# stall watchdog: exit hard so systemd's Restart=on-failure brings the
# process back up with a fresh cv2.VideoCapture, since a wedged capture
# handle inside THIS process usually can't be recovered any other way.
WATCHDOG_TIMEOUT = 60

# Well before the full watchdog timeout, proactively release() and
# reopen the device on a run of consecutive failures - recovers from
# some USB transients without a full process restart. Reset after each
# attempt so it can fire again roughly every ~10 cycles if the device
# stays unavailable, rather than only once per process lifetime.
CONSECUTIVE_FAILURES_BEFORE_REOPEN = 10

# See the module docstring - a safety net, deliberately not exposed as
# a config.json setting (same "guardrail, not a day-to-day knob"
# treatment climate.py gives its own runtime cutoffs).
CAMERA_LOW_DISK_THRESHOLD_MB = 200
CAMERA_LOW_DISK_PRUNE_COUNT = 20

_low_disk_warned = False


def open_capture(device, width, height):
    """Opens the configured device and requests a resolution - cv2
    clamps to the nearest mode the hardware actually supports rather
    than erroring if the exact number isn't available, so there's
    nothing here to validate against the device's real capabilities.
    `device` is either a plain integer index as a string (most USB
    webcams show up as /dev/video0, i.e. index 0) or a path (e.g. a
    /dev/v4l/by-id/... symlink - more stable across reboots than a bare
    index if more than one USB video device is ever connected)."""
    cam_id = int(device) if device.isdigit() else device
    cap = cv2.VideoCapture(cam_id)
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


def maybe_prune_for_disk_space():
    """Safety-net pruning only - see the module docstring. Logs the
    transition once, not every check, so a sustained low-disk situation
    doesn't spam the event log every cycle - same pattern climate.py
    uses for sensor-failover logging."""
    global _low_disk_warned
    free_mb = shutil.disk_usage(state.BASE_DIR).free / (1024 * 1024)
    if free_mb < CAMERA_LOW_DISK_THRESHOLD_MB:
        removed = state.prune_oldest_camera_snapshots(CAMERA_LOW_DISK_PRUNE_COUNT)
        if not _low_disk_warned:
            state.log_event(
                "warning",
                f"Camera: free disk space ({free_mb:.0f}MB) below "
                f"{CAMERA_LOW_DISK_THRESHOLD_MB}MB - pruned {removed} oldest "
                "timelapse snapshot(s) as a safety net. If this keeps "
                "happening, lower the snapshot interval, resolution, or "
                "JPEG quality on the Config page."
            )
            _low_disk_warned = True
    elif _low_disk_warned:
        state.log_event("info", f"Camera: free disk space recovered ({free_mb:.0f}MB) - pruning stopped")
        _low_disk_warned = False


def main():
    state.init_db()
    os.makedirs(state.CAMERA_DIR, exist_ok=True)

    config = state.load_config()
    cam_cfg = config.get("camera", {})
    device = cam_cfg.get("device", "0")
    width = cam_cfg.get("width", 1280)
    height = cam_cfg.get("height", 720)

    logging.info(f"Opening camera device '{device}' at {width}x{height}...")
    cap = open_capture(device, width, height)
    if not cap.isOpened():
        logging.error(
            f"Could not open camera device '{device}' - check it's plugged in and that "
            "the device index/path in config.json's camera.device matches. "
            "Run discover_camera.py to list what's actually available."
        )

    last_success_time = time.time()  # grace period, same idea as ble_listener's watchdog
    last_snapshot_time = 0.0         # 0 lets the first eligible cycle actually save one
    consecutive_failures = 0
    camera_was_available = cap.isOpened()

    try:
        while True:
            # Re-read config every cycle - a mode switch or a settings
            # change from the dashboard should take effect within one
            # cycle, not require restarting this service.
            config = state.load_config()
            cam_cfg = config.get("camera", {})
            interval_seconds = cam_cfg.get("live_capture_interval_seconds", 2)
            quality = cam_cfg.get("jpeg_quality", 80)
            current_mode = config.get("current_mode", "ready")
            mode_settings = config.get("modes", {}).get(current_mode, {})
            snapshot_interval_minutes = mode_settings.get("snapshot_interval_minutes", 0)

            ok, frame = cap.read() if cap.isOpened() else (False, None)

            if ok:
                consecutive_failures = 0
                last_success_time = time.time()
                if not camera_was_available:
                    state.log_event("info", "Camera reconnected - frames are being captured again")
                    camera_was_available = True

                encode_ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                if encode_ok:
                    jpeg_bytes = buf.tobytes()
                    state.atomic_write_bytes(state.CAMERA_LIVE_PATH, jpeg_bytes)

                    if snapshot_interval_minutes > 0:
                        due = (time.time() - last_snapshot_time) >= snapshot_interval_minutes * 60
                        if due:
                            maybe_prune_for_disk_space()
                            state.save_camera_snapshot(current_mode, jpeg_bytes)
                            last_snapshot_time = time.time()
                else:
                    logging.warning("JPEG encode failed for a captured frame - skipping this cycle")
            else:
                consecutive_failures += 1
                if camera_was_available:
                    state.log_event("warning", "Camera unavailable - no frame could be read")
                    camera_was_available = False

                if consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_REOPEN:
                    logging.warning(
                        f"{consecutive_failures} consecutive failed reads - "
                        "releasing and reopening the capture device"
                    )
                    cap.release()
                    cap = open_capture(device, width, height)
                    consecutive_failures = 0

                if time.time() - last_success_time > WATCHDOG_TIMEOUT:
                    logging.error(
                        f"No frame successfully captured in over {WATCHDOG_TIMEOUT}s - "
                        "the capture device is likely wedged. Exiting immediately "
                        "(not attempting a graceful release() first, in case that "
                        "itself is part of what's stuck) so systemd restarts this "
                        "service with a fresh device handle."
                    )
                    os._exit(1)

            time.sleep(max(1, interval_seconds))
    finally:
        cap.release()


if __name__ == "__main__":
    main()
