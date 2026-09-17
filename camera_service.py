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
on the CURRENTLY ACTIVE MODE's own `snapshot_interval_minutes` (per-mode
in config.json, 0 = never) and pulls one still frame from
camera-streamer's own /snapshot HTTP endpoint - the same way
webapp.py's live view now gets its frames too, just a single request
instead of an open stream. That single change is also why this loop is
so much lighter than it used to be: no continuous capture, no per-frame
JPEG encode, nothing to watchdog for a wedged device handle - the
worst that can happen here now is an HTTP request timing out, which
just gets logged and retried next cycle.

Saved frames land in camera/timelapse/ plus a DB row via
shared_state.save_camera_snapshot(). Tracked with a single
last-saved timestamp regardless of which mode was active when it was
set - switching from a long-interval mode to a short-interval one
doesn't itself fire an immediate snapshot just because the mode
changed; the new interval simply starts being measured against
whenever the last snapshot actually happened.

When the CURRENT MODE CHANGES, whatever frames were just accumulated for
the mode being left get compiled into an actual .mp4 via ffmpeg (a
"session" video - e.g. one video per visit to Cleaning mode), then the
raw frames that went into it are deleted - the video is the lasting
record from then on, not a growing pile of loose JPEGs. See
maybe_compile_session()/compile_session_video() below for the full
reasoning (session-boundary tracking, why compiling runs in a background
thread, the concat-demuxer approach, and the too-few-frames case). None
of this changed in the camera-streamer switch - it never touched the
capture device directly, only the DB/filesystem.

A hardcoded, non-configurable disk-space safety net (see
CAMERA_LOW_DISK_THRESHOLD_MB below) prunes the OLDEST timelapse frames
if free space gets dangerously low - the whole point of a timelapse is
to keep frames, so this is a last resort to keep the SD card from
filling up and taking down the Pi (which would also kill climate
control - the actually safety-critical part of this project), not a
day-to-day retention policy. If you're hitting it regularly, lower the
snapshot interval or camera-streamer's own resolution/quality flags
(see docs/camera-streamer-setup.md) instead of relying on it.

Run under systemd (see systemd/dermestid-camera.service).
"""
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import subprocess
import threading
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

# A session shorter than this many frames doesn't get compiled - a couple
# of frames from an accidental few-second mode toggle would just produce
# a flickering fraction-of-a-second clip, not something worth keeping as
# its own video. Its frames are simply left in camera_snapshots (not
# deleted) - they'll naturally be included in whatever session next
# actually finishes for that mode, per get_earliest_camera_snapshot_ts's
# invariant (see shared_state.py). No data is lost either way.
MINIMUM_FRAMES_FOR_VIDEO = 3

# Playback speed of a compiled session video, in frames per second of
# OUTPUT video - unrelated to the capture cadence (snapshot_interval_
# minutes), which is minutes between frames, not fps. 12fps keeps even a
# several-day session down to a short, actually-watchable clip (e.g. a
# 3-day Cleaning session on a 5-minute interval is ~864 frames -> 72s of
# video) without needing to be a dashboard setting; edit this constant
# directly if you want a different pace.
TIMELAPSE_VIDEO_FPS = 12

# Safety cap on the ffmpeg subprocess itself, same watchdog philosophy as
# the rest of this file - compiling runs in a background thread (see
# maybe_compile_session), so a hang here can't block the (much lighter,
# now HTTP-only) capture loop, but it should still never be allowed to
# run forever.
COMPILE_TIMEOUT_SECONDS = 300

# Which modes currently have a compile running in a background thread -
# guards against a rapidly-flapping mode triggering two overlapping
# compiles (and therefore two competing deletes) for the same frames.
# The rare case where this actually skips a compile just leaves that
# mode's frames in place for the next successful compile to pick up -
# same "no data lost" reasoning as the too-few-frames case above.
_compiling_modes = set()
_compiling_lock = threading.Lock()


def maybe_compile_session(mode, start_ts, end_ts):
    """Called once, right when the current mode changes away from
    `mode` - decides whether there's enough freshly-accumulated timelapse
    footage from that just-ended session to bother compiling, and if so
    kicks the actual ffmpeg work off in a background thread so it can
    never stall this loop (an ffmpeg encode of a long session is a real
    multi-second CPU task - fine as a rare, one-off event per mode
    change, not fine if it delayed the next snapshot's due-check for
    that whole time)."""
    frames = state.get_camera_snapshots_in_range(mode, start_ts, end_ts)
    if len(frames) < MINIMUM_FRAMES_FOR_VIDEO:
        return
    with _compiling_lock:
        if mode in _compiling_modes:
            logging.warning(f"Timelapse: a {mode} compile is already running - "
                             "leaving this session's frames for the next one")
            return
        _compiling_modes.add(mode)

    def run():
        try:
            compile_session_video(mode, start_ts, end_ts, frames)
        finally:
            with _compiling_lock:
                _compiling_modes.discard(mode)

    threading.Thread(target=run, daemon=True).start()


def compile_session_video(mode, start_ts, end_ts, frames):
    """Stitches one mode-session's already-captured JPEG frames into a
    single .mp4 via ffmpeg's concat demuxer (the standard way to turn an
    arbitrary sequence of same-size still images into a video - the
    image2 sequence demuxer needs consecutively-numbered filenames, which
    these epoch-timestamp names aren't), keeps a copy of one frame as a
    poster thumbnail for the gallery, records it all via
    shared_state.save_timelapse_video(), and then deletes the raw frames
    that went into it - the video is the lasting record from here on,
    not a second, redundant copy of every frame sitting alongside it on
    a Pi's small SD card. If anything goes wrong, the raw frames are left
    untouched so nothing is ever lost to a failed compile."""
    if not shutil.which("ffmpeg"):
        state.log_event(
            "warning",
            f"Timelapse: ffmpeg not found - can't compile the {mode} session that just "
            f"ended ({len(frames)} frames). Install it with: sudo apt install ffmpeg. "
            "The raw frames are kept, and this session's video can't be recovered "
            "retroactively once a future compile succeeds and deletes them, so install "
            "ffmpeg before too many more sessions pass if you want this feature."
        )
        return

    os.makedirs(state.CAMERA_TIMELAPSE_VIDEOS_DIR, exist_ok=True)
    video_filename = f"{mode}_{int(start_ts)}_{int(end_ts)}.mp4"
    poster_filename = f"{mode}_{int(start_ts)}_{int(end_ts)}.jpg"
    video_path = os.path.join(state.CAMERA_TIMELAPSE_VIDEOS_DIR, video_filename)
    poster_path = os.path.join(state.CAMERA_TIMELAPSE_VIDEOS_DIR, poster_filename)
    list_path = os.path.join(state.CAMERA_TIMELAPSE_VIDEOS_DIR, f".compile_{int(time.time())}.txt")

    frame_duration = 1.0 / TIMELAPSE_VIDEO_FPS
    with open(list_path, "w") as f:
        for frame in frames:
            frame_path = os.path.join(state.CAMERA_TIMELAPSE_DIR, frame["filename"])
            # ffmpeg's concat demuxer has its own tiny quoting format for
            # this list file - single quotes around the path, with a
            # literal single quote escaped as '\''. Irrelevant for our
            # own epoch-integer.jpg filenames in practice, but cheap
            # insurance against ever choking on a stray character.
            escaped = frame_path.replace("'", "'\\''")
            f.write(f"file '{escaped}'\nduration {frame_duration}\n")
        # concat demuxer quirk: `duration` on the LAST entry is ignored
        # unless that same file is also listed once more after it, with
        # no duration of its own.
        last_escaped = os.path.join(state.CAMERA_TIMELAPSE_DIR, frames[-1]["filename"]).replace("'", "'\\''")
        f.write(f"file '{last_escaped}'\n")

    try:
        # -r here is an OUTPUT constant frame rate, not a passthrough of
        # the concat list's per-frame `duration` timing - deliberately
        # NOT combined with -vsync/-fps_mode vfr, which newer ffmpeg
        # rejects outright as contradictory with an explicit -r
        # ("One of -r/-fpsmax was specified together a non-CFR -vsync").
        # Letting ffmpeg resample each held-frame's duration to a fixed
        # CFR output is exactly what's wanted here anyway, since
        # TIMELAPSE_VIDEO_FPS is the real knob for playback speed.
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-pix_fmt", "yuv420p", "-r", str(TIMELAPSE_VIDEO_FPS), video_path],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        state.log_event("error", f"Timelapse: ffmpeg timed out compiling {mode} session "
                                  f"({len(frames)} frames) after {COMPILE_TIMEOUT_SECONDS}s - raw frames kept.")
        os.remove(list_path)
        return
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)

    if result.returncode != 0 or not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        state.log_event(
            "error",
            f"Timelapse: ffmpeg failed compiling {mode} session ({len(frames)} frames) - "
            f"raw frames kept. {result.stderr[-500:] if result.stderr else ''}"
        )
        if os.path.exists(video_path):
            os.remove(video_path)
        return

    # Reuse an already-captured frame as the poster thumbnail (the middle
    # one reads as more representative of the session than the first)
    # rather than asking ffmpeg to re-decode the video just to grab one
    # frame back out of it.
    middle_frame_path = os.path.join(state.CAMERA_TIMELAPSE_DIR, frames[len(frames) // 2]["filename"])
    try:
        shutil.copyfile(middle_frame_path, poster_path)
    except FileNotFoundError:
        poster_filename = None

    duration_seconds = len(frames) / TIMELAPSE_VIDEO_FPS
    file_size_bytes = os.path.getsize(video_path)
    state.save_timelapse_video(mode, start_ts, end_ts, len(frames), video_filename,
                                poster_filename, duration_seconds, file_size_bytes)
    # The video now IS the record of this session - the raw frames that
    # went into it would just be redundant disk usage from here on.
    state.delete_camera_snapshots([f["ts"] for f in frames])
    state.log_event(
        "info",
        f"Timelapse: compiled a {duration_seconds:.0f}s video from {len(frames)} {mode} "
        f"frames ({file_size_bytes / (1024 * 1024):.1f}MB)"
    )


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
                "happening, lower the snapshot interval or camera-streamer's "
                "resolution/quality (see docs/camera-streamer-setup.md)."
            )
            _low_disk_warned = True
    elif _low_disk_warned:
        state.log_event("info", f"Camera: free disk space recovered ({free_mb:.0f}MB) - pruning stopped")
        _low_disk_warned = False


def main():
    state.init_db()
    os.makedirs(state.CAMERA_DIR, exist_ok=True)

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

    # Session-boundary tracking for timelapse video compiling (see
    # maybe_compile_session above). last_mode/session_start_ts start from
    # whatever's ALREADY sitting in camera_snapshots for the mode we're
    # coming up in, not from "now" - that's what makes this correct
    # across a service restart mid-session: nothing is asked to survive
    # in memory, it's reconstructed from the DB's own invariant that
    # camera_snapshots only ever holds a mode's not-yet-compiled frames.
    last_mode = config.get("current_mode", "ready")
    session_start_ts = state.get_earliest_camera_snapshot_ts(last_mode)
    if session_start_ts is None:
        session_start_ts = time.time()

    logging.info("Timelapse capture loop starting - pulling snapshots from "
                  "camera-streamer's /snapshot endpoint, not opening the "
                  "camera device directly (see docs/camera-streamer-setup.md).")

    while True:
        # Re-read config every cycle - a mode switch or a settings
        # change from the dashboard should take effect within one
        # cycle, not require restarting this service.
        config = state.load_config()
        cam_cfg = config.get("camera", {})
        streamer_port = cam_cfg.get("streamer_port", 8090)
        current_mode = config.get("current_mode", "ready")
        mode_settings = config.get("modes", {}).get(current_mode, {})
        snapshot_interval_minutes = mode_settings.get("snapshot_interval_minutes", 0)

        if current_mode != last_mode:
            transition_time = time.time()
            maybe_compile_session(last_mode, session_start_ts, transition_time)
            last_mode = current_mode
            session_start_ts = transition_time

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
                    maybe_prune_for_disk_space()
                    state.save_camera_snapshot(current_mode, jpeg_bytes)
                    last_snapshot_time = time.time()
                else:
                    consecutive_failures += 1
                    if streamer_was_reachable:
                        state.log_event(
                            "warning",
                            "Could not reach camera-streamer for a timelapse snapshot - "
                            "check it's installed and running (see "
                            "docs/camera-streamer-setup.md). Will keep retrying."
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
