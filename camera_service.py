#!/usr/bin/env python3
"""
Timelapse capture for the dermestid enclosure - entirely optional, like
ble_listener.py. If you're not using a webcam, don't install/enable
systemd/dermestid-camera.service (or systemd/camera-streamer.service);
nothing else in this project depends on either.

[2026-09-14] This used to also own live view - it was the only process
that opened the USB device, capturing continuously via OpenCV and
writing camera/latest.jpg for webapp.py to relay as an MJPEG stream.
Live view now goes through camera-streamer instead (a purpose-built
V4L2 streaming daemon using the Pi's hardware JPEG encoder - see
docs/camera-streamer-setup.md and PROJECT_STATUS.md's 2026-09-14
entries for why), which is the only process that opens the USB device
now. This file no longer touches the device at all: it just wakes up
on the ACTIVE TIMELAPSE RECORDING's own interval (see below) and pulls one still frame from
camera-streamer's own /snapshot HTTP endpoint - the same way
webapp.py's live view now gets its frames too, just a single request
instead of an open stream. That single change is also why this loop is
so much lighter than it used to be: no continuous capture, no per-frame
JPEG encode, nothing to watchdog for a wedged device handle - the
worst that can happen here now is an HTTP request timing out, which
just gets logged and retried next cycle.

Saved frames land in camera/timelapse/ plus a DB row via
shared_state.save_camera_snapshot(), tagged with the recording they belong
to.

[2026-10-16] Timelapses are RECORDINGS now, started and stopped from the
Timelapse page (or offered when switching into Cleaning), NOT per-mode
sessions. This loop captures only while a recording is running, at that
recording's interval; with none running it captures nothing. A mode switch
no longer ends anything or compiles anything - compiling is a manual action
(the Timelapse page's Compile button, or compile_timelapse.py), done in
timelapse_encode.compile_recording(). See shared_state.py's "Timelapse
RECORDINGS" section for why. The capture timer is seeded from the active
recording's newest frame on startup, so a service restart mid-recording
keeps the same cadence instead of firing an extra snapshot.

A hardcoded, non-configurable disk-space safety net (see
CAMERA_LOW_DISK_THRESHOLD_MB below) prunes the OLDEST timelapse frames
if free space gets dangerously low - the whole point of a timelapse is
to keep frames, so this is a last resort to keep the SD card from
filling up and taking down the Pi (which would also kill climate
control - the actually safety-critical part of this project), not a
day-to-day retention policy. If you're hitting it regularly, lower the
snapshot interval or camera-streamer's own resolution/quality flags
(see docs/camera-streamer-setup.md) instead of relying on it.

Two notification categories cover this (see maybe_forecast_low_disk and
maybe_prune_for_disk_space below, and EVENT_CATEGORIES in
shared_state.py): "disk_space_low" fires at least DISK_FORECAST_WARNING_HOURS
BEFORE the threshold above is projected to be hit, based on the current
snapshot capture rate - an early heads-up to go fix the real problem;
"disk_space_pruned" fires when frames actually get auto-deleted, i.e.
the forecast's lead time ran out (or was never accurate to begin with -
e.g. a mode switch after the forecast raised the rate).

Run under systemd (see systemd/dermestid-camera.service).
"""
import collections
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import time
import urllib.error
import urllib.request

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

# How long to wait for camera-streamer's /snapshot endpoint before giving
# up on a single timelapse capture attempt. Generous relative to how
# fast a still-frame JPEG request "should" be (well under a second on
# localhost) because a slow response is far more likely than a fast
# failure - camera-streamer momentarily busy serving a live /stream
# viewer, or briefly reinitializing the device after a USB hiccup - and
# this only runs once per snapshot_interval_minutes, not in a tight
# loop, so there's no real cost to waiting a bit before treating it as a
# real failure.
SNAPSHOT_HTTP_TIMEOUT_SECONDS = 5

# How often this loop wakes up to check whether a snapshot is due -
# independent of snapshot_interval_minutes itself (which can be as long
# as 24h). Same fixed-wake-cadence pattern climate.py uses for its own
# main loop, rather than computing an exact sleep-until-due duration -
# simpler, and cheap enough to check this often given there's no device
# to hold open anymore.
POLL_INTERVAL_SECONDS = 15

# See the module docstring - a safety net, deliberately not exposed as
# a config.json setting (same "guardrail, not a day-to-day knob"
# treatment climate.py gives its own runtime cutoffs).
CAMERA_LOW_DISK_THRESHOLD_MB = 200
CAMERA_LOW_DISK_PRUNE_COUNT = 20

_low_disk_warned = False

# How far ahead maybe_forecast_low_disk() below has to warn - see its
# docstring. Also not a config.json knob, same reasoning as the disk
# threshold above - this is a heads-up on the way to that hardcoded
# safety net, not a separately-tunable schedule.
DISK_FORECAST_WARNING_HOURS = 24

# Rolling window of the most recently captured snapshots' actual JPEG
# sizes (bytes) - recorded for free every time fetch_snapshot() succeeds
# (see the main loop below), and averaged by maybe_forecast_low_disk() to
# estimate a real bytes-per-snapshot rate. Reading real capture sizes
# instead of guessing from resolution*quality means the forecast tracks
# whatever camera-streamer's actually producing right now, including a
# resolution/quality change made on the Settings page mid-session.
# Starts empty on every service (re)start - meaning no forecast until at
# least one snapshot has actually been captured since startup, which is
# fine for a background best-effort estimate, not a safety-critical one.
DISK_FORECAST_SAMPLE_SIZE = 10
_recent_snapshot_sizes = collections.deque(maxlen=DISK_FORECAST_SAMPLE_SIZE)
_disk_forecast_warned = False

# Compiling moved to timelapse_encode.compile_recording() (manual, from the
# dashboard or compile_timelapse.py) - see the module docstring.

def fetch_snapshot(streamer_port):
    """Pulls one still-frame JPEG from camera-streamer's own /snapshot
    endpoint (see docs/camera-streamer-setup.md) - localhost only, since
    this always runs on the same Pi as camera-streamer itself, never
    over the LAN. Returns the raw JPEG bytes, or None (logging why) on
    any failure - a connection refused (camera-streamer not running/not
    installed yet), a timeout, or a non-200 response all just mean "no
    snapshot this cycle," not a crash; the next POLL_INTERVAL_SECONDS
    cycle tries again on its own.

    Plain urllib rather than adding a `requests` dependency - this is a
    single GET with a timeout, which urllib already does natively, and
    nothing else in this project has needed `requests` so far."""
    url = f"http://127.0.0.1:{streamer_port}/snapshot"
    try:
        with urllib.request.urlopen(url, timeout=SNAPSHOT_HTTP_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                logging.warning(f"camera-streamer returned HTTP {resp.status} for {url}")
                return None
            return resp.read()
    except urllib.error.URLError as e:
        # Covers connection-refused (camera-streamer not running) and a
        # genuine timeout alike - both are the same "no frame this
        # cycle" case from here, just logged with whichever reason
        # urllib actually gives.
        logging.warning(f"Could not reach camera-streamer at {url}: {e}")
        return None


def maybe_prune_for_disk_space():
    """Safety-net pruning only - see the module docstring. Logs the
    transition once, not every check, so a sustained low-disk situation
    doesn't spam the event log every cycle - same pattern climate.py
    uses for sensor-failover logging. Its own category
    (disk_space_pruned) rather than the generic camera_issue - this is a
    real "data just got deleted" event, worth being individually
    toggle-able on the Notifications card separately from routine
    camera-streamer-unreachable noise."""
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
                "happening, lower the snapshot interval or camera-streamer's "
                "resolution/quality (see docs/camera-streamer-setup.md).",
                category="disk_space_pruned"
            )
            _low_disk_warned = True
    elif _low_disk_warned:
        # Recovered above the threshold (more space freed up, or the
        # pruning above just did its job) - reset so a FUTURE dip below
        # the threshold warns again instead of staying silently
        # "already warned" forever.
        state.log_event("info", f"Camera: free disk space recovered ({free_mb:.0f}MB) - pruning stopped")
        _low_disk_warned = False


def maybe_forecast_low_disk():
    """Warns AT LEAST DISK_FORECAST_WARNING_HOURS before
    maybe_prune_for_disk_space() above would actually start deleting
    frames - that function only fires once free space has ALREADY
    dropped below the threshold, which (depending on the snapshot
    interval) can be anywhere from days to mere minutes of advance
    notice, not really a "heads up, go fix this" warning. This projects
    a simple linear rate - the rolling average of actually-captured
    snapshot sizes (_recent_snapshot_sizes) divided by the CURRENTLY
    ACTIVE recording's interval - forward from the
    current free space to estimate when it'll cross the threshold.

    Deliberately an estimate, not a guarantee: it only knows about the
    recording that's running RIGHT NOW (a future recording at a
    faster/slower interval isn't predicted), and it only accounts for
    timelapse growth specifically, not other things that might also be
    filling the SD card (logs, a big auto_update.sh pull, etc). That's
    fine for what this is - an early nudge to go investigate (lower the
    snapshot interval, lower camera-streamer's resolution/quality, or
    free up space) before anything gets auto-deleted, not a second
    safety-critical mechanism - maybe_prune_for_disk_space() is still
    what actually protects the Pi if this estimate turns out wrong.

    No recording running means nothing's accumulating from this source
    right now, so there's nothing to forecast."""
    global _disk_forecast_warned
    rec = state.get_active_recording()
    interval_minutes = rec["interval_minutes"] if rec else 0
    if not interval_minutes:
        _disk_forecast_warned = False  # not accumulating right now - a later recording can warn again
        return

    free_mb = shutil.disk_usage(state.BASE_DIR).free / (1024 * 1024)
    if free_mb < CAMERA_LOW_DISK_THRESHOLD_MB:
        return  # already past the forecast stage - maybe_prune_for_disk_space owns this now

    if not _recent_snapshot_sizes:
        return  # nothing captured yet this run to estimate a rate from
    avg_bytes = sum(_recent_snapshot_sizes) / len(_recent_snapshot_sizes)

    bytes_per_hour = avg_bytes * (60.0 / interval_minutes)
    if bytes_per_hour <= 0:
        return
    headroom_mb = free_mb - CAMERA_LOW_DISK_THRESHOLD_MB
    hours_until_threshold = (headroom_mb * 1024 * 1024) / bytes_per_hour

    if hours_until_threshold <= DISK_FORECAST_WARNING_HOURS:
        if not _disk_forecast_warned:
            state.log_event(
                "warning",
                f"Camera: at the current snapshot rate (~{avg_bytes / 1024:.0f}KB every "
                f"{interval_minutes}min for '{state.recording_label(rec)}'), free disk space "
                f"({free_mb:.0f}MB) is projected to hit the {CAMERA_LOW_DISK_THRESHOLD_MB}MB "
                f"auto-delete threshold in about {hours_until_threshold:.1f}h. Lower the "
                "snapshot interval or camera-streamer's resolution/quality (see "
                "docs/camera-streamer-setup.md), or free up space on the SD card, before "
                "oldest timelapse frames start getting auto-deleted.",
                category="disk_space_low"
            )
            _disk_forecast_warned = True
    else:
        # Enough headroom again (freed-up space, a longer interval was
        # set, etc.) - reset so a future dip under 24h warns again.
        _disk_forecast_warned = False


def main():
    state.init_db()
    os.makedirs(state.CAMERA_DIR, exist_ok=True)
    adopted = state.adopt_orphan_frames()
    if adopted:
        logging.info(f"Timelapse: attached {adopted} untagged frame(s) to the running recording")

    config = state.load_config()
    last_snapshot_time = 0.0  # 0 lets the first eligible cycle actually save one

    # Consecutive-failure tracking is purely informational now (a log
    # line, not a watchdog exit) - there's no device handle in THIS
    # process to recover by reopening, unlike the old capture loop.
    # camera-streamer owns whatever recovery its own device handle
    # needs; if IT is wedged, that's outside what this process can fix,
    # only notice and report.
    consecutive_failures = 0
    streamer_was_reachable = True

    # Seed the capture timer from the running recording's newest frame (if
    # any), so a restart mid-recording keeps its cadence. Recomputed
    # whenever the active recording changes (a new one captures its first
    # frame right away).
    active_id = None

    logging.info("Timelapse capture loop starting - pulling snapshots from "
                  "camera-streamer's /snapshot endpoint, not opening the "
                  "camera device directly (see docs/camera-streamer-setup.md).")

    while True:
        # Re-read every cycle - starting/stopping a recording or changing
        # its interval from the dashboard takes effect within one cycle.
        config = state.load_config()
        cam_cfg = config.get("camera", {})
        streamer_port = cam_cfg.get("streamer_port", 8090)
        current_mode = config.get("current_mode", "ready")
        rec = state.get_active_recording()
        rec_id = rec["id"] if rec else None
        if rec_id != active_id:
            active_id = rec_id
            last_snapshot_time = 0.0
            if rec:
                frames = state.get_recording_frames(rec_id)
                if frames:
                    last_snapshot_time = float(frames[-1]["ts"])
                logging.info(f"Timelapse: recording '{state.recording_label(rec)}' is active "
                             f"(every {rec['interval_minutes']} min)")
        snapshot_interval_minutes = rec["interval_minutes"] if rec else 0

        # Checked every poll cycle (not just when a snapshot's actually
        # due) - free space itself can drop at any time, and
        # snapshot_interval_minutes can be as long as 24h, which would
        # otherwise be far too infrequent a check for a 24h-ahead warning.
        maybe_forecast_low_disk()

        if snapshot_interval_minutes > 0:
            due = (time.time() - last_snapshot_time) >= snapshot_interval_minutes * 60
            if due:
                jpeg_bytes = fetch_snapshot(streamer_port)
                if jpeg_bytes:
                    consecutive_failures = 0
                    if not streamer_was_reachable:
                        state.log_event("info", "camera-streamer reachable again - "
                                                 "timelapse snapshots resuming")
                        streamer_was_reachable = True
                    # Applies the same persistent crop the live view shows
                    # (see "crop" in shared_state's "camera" DEFAULT_CONFIG,
                    # and crop_jpeg_bytes() - a no-op/cheap return if no
                    # crop is actually set) so timelapse frames match what
                    # the dashboard's live view has been cropped down to,
                    # rather than saving the uncropped full frame.
                    jpeg_bytes = state.crop_jpeg_bytes(jpeg_bytes, cam_cfg.get("crop", {}))
                    _recent_snapshot_sizes.append(len(jpeg_bytes))
                    maybe_prune_for_disk_space()
                    state.save_camera_snapshot(current_mode, jpeg_bytes, recording_id=rec_id)
                    last_snapshot_time = time.time()
                else:
                    consecutive_failures += 1
                    if streamer_was_reachable:
                        state.log_event(
                            "warning",
                            "Could not reach camera-streamer for a timelapse snapshot - "
                            "check it's installed and running (see "
                            "docs/camera-streamer-setup.md). Will keep retrying.",
                            category="camera_issue"
                        )
                        streamer_was_reachable = False
                    # Still advance last_snapshot_time so a camera-streamer
                    # outage doesn't cause a burst of retries every
                    # POLL_INTERVAL_SECONDS for the rest of a long
                    # snapshot_interval_minutes window - one attempt per
                    # interval is enough while it stays down; it'll be
                    # caught up to within one interval once it's back.
                    last_snapshot_time = time.time()

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
