#!/usr/bin/env python3
"""
USB webcam service for the dermestid enclosure - entirely optional, like
ble_listener.py. If you're not using a webcam, don't install/enable
systemd/dermestid-camera.service; nothing else in this project depends
on it.

Two independent things happen on the same loop, both driven by
config.json so a dashboard change takes effect within one cycle without
a restart (same reasoning as climate.py re-reading its own config):

  - Live view: `camera.live_capture_fps` times a second (default 5fps -
    a real live feed, not a slideshow), grab a frame and atomically
    overwrite camera/latest.jpg. webapp.py relays that
    same file to browsers as an MJPEG stream (/api/camera/stream.mjpg,
    multipart/x-mixed-replace) - still just one process, this one,
    ever opening the actual USB device, so any number of simultaneous
    dashboard viewers never each try to grab it themselves (most UVC
    webcams only support one client at a time in the first place, so
    that would just break the second viewer). This is also why the
    frame rate is a config setting rather than hardcoded fast: a Pi 3B+
    shares this CPU with climate.py, the actually safety-critical part
    of this project, so push it faster than the default only if you've
    confirmed there's headroom (check `top`/CPU temp under load), and
    back off resolution/quality first if not.
  - Timelapse: on the CURRENTLY ACTIVE MODE's own
    `snapshot_interval_minutes` (per-mode in config.json, 0 = never),
    save a permanent frame into camera/timelapse/ plus a DB row via
    shared_state.save_camera_snapshot(). Tracked with a single
    last-saved timestamp regardless of which mode was active when it
    was set - switching from a long-interval mode to a short-interval
    one doesn't itself fire an immediate snapshot just because the mode
    changed; the new interval simply starts being measured against
    whenever the last snapshot actually happened.

When the CURRENT MODE CHANGES, whatever frames were just accumulated for
the mode being left get compiled into an actual .mp4 via ffmpeg (a
"session" video - e.g. one video per visit to Cleaning mode), then the
raw frames that went into it are deleted - the video is the lasting
record from then on, not a growing pile of loose JPEGs. See
maybe_compile_session()/compile_session_video() below for the full
reasoning (session-boundary tracking, why compiling runs in a background
thread, the concat-demuxer approach, and the too-few-frames case).

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
import subprocess
import threading
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
# maybe_compile_session), so a hang here can't block live-view capture,
# but it should still never be allowed to run forever.
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
    never stall the live-view capture loop above (an ffmpeg encode of a
    long session is a real multi-second CPU task - fine as a rare,
    one-off event per mode change, not fine if it froze live view for
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


def open_capture(device, width, height):
    """Opens the configured device and requests a resolution - cv2
    clamps to the nearest mode the hardware actually supports rather
    than erroring if the exact number isn't available, so there's
    nothing here to validate against the device's real capabilities.
    `device` is either a plain integer index as a string (most USB
    webcams show up as /dev/video0, i.e. index 0) or a path (e.g. a
    /dev/v4l/by-id/... symlink - more stable across reboots than a bare
    index if more than one USB video device is ever connected).

    Forces MJPG as the capture format, requested BEFORE the resolution
    (some V4L2 drivers only honor a format change if it's set first).
    Without this, OpenCV/V4L2 is free to negotiate an uncompressed
    format (commonly YUYV) once a high resolution is requested - a raw
    1920x1080 YUYV frame is ~4MB, and over USB2 that alone caps real
    throughput to a handful of fps no matter what camera.live_capture_fps
    is set to in config.json, since cap.read() just blocks until the
    hardware/bus can deliver the next one. This was diagnosed
    2026-09-13 from an actual screen-recorded live feed (see
    PROJECT_STATUS.md): real content only updated ~4.5x/sec with
    noticeable jitter even at live_capture_fps=20, meaning the config
    knob was never the bottleneck. MJPG frames at the same resolution
    are roughly 10-20x smaller, which should let the actual hardware
    ceiling be much higher if the camera supports it at all - if a
    connected camera doesn't support MJPG, this is a harmless no-op and
    behavior is unchanged from before."""
    cam_id = int(device) if device.isdigit() else device
    cap = cv2.VideoCapture(cam_id)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
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

    # Real, measured capture rate - see save_camera_stats()'s docstring
    # in shared_state.py for why this exists (config.json's
    # live_capture_fps is what's ASKED for, this is what's actually
    # happening). An EMA rather than a plain per-cycle 1/interval so a
    # single unusually fast or slow cycle doesn't make the dashboard
    # number jump around - ALPHA is a "how quickly should this react to
    # a real change" tradeoff, not a tuned constant worth exposing.
    _FPS_EMA_ALPHA = 0.3
    actual_fps_ema = None
    last_capture_time = None
    last_stats_write_time = 0.0

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

    try:
        while True:
            # Re-read config every cycle - a mode switch or a settings
            # change from the dashboard should take effect within one
            # cycle, not require restarting this service.
            config = state.load_config()
            cam_cfg = config.get("camera", {})
            # config.json stores this as frames/sec now (see shared_state's
            # DEFAULT_CONFIG comment for why) - this loop still just needs
            # a sleep duration, so convert once per cycle rather than
            # threading fps through the rest of this function.
            live_fps = cam_cfg.get("live_capture_fps", 5)
            interval_seconds = 1.0 / live_fps if live_fps > 0 else 0.2
            quality = cam_cfg.get("jpeg_quality", 80)
            current_mode = config.get("current_mode", "ready")
            mode_settings = config.get("modes", {}).get(current_mode, {})
            snapshot_interval_minutes = mode_settings.get("snapshot_interval_minutes", 0)

            if current_mode != last_mode:
                transition_time = time.time()
                maybe_compile_session(last_mode, session_start_ts, transition_time)
                last_mode = current_mode
                session_start_ts = transition_time

            ok, frame = cap.read() if cap.isOpened() else (False, None)

            if ok:
                consecutive_failures = 0
                now = time.time()
                last_success_time = now
                if not camera_was_available:
                    state.log_event("info", "Camera reconnected - frames are being captured again")
                    camera_was_available = True

                # Measure the ACTUAL interval between successful reads,
                # not the requested one - this is what exposes a gap
                # between live_capture_fps and what the hardware/USB
                # bus can really deliver (see open_capture()'s docstring
                # and PROJECT_STATUS.md's 2026-09-13 entry). Skips the
                # very first frame (nothing to measure an interval
                # against yet) and any interval that's absurdly small/
                # zero (clock weirdness, not a real 1000fps camera).
                if last_capture_time is not None:
                    dt = now - last_capture_time
                    if dt > 0.001:
                        instant_fps = 1.0 / dt
                        actual_fps_ema = (instant_fps if actual_fps_ema is None
                                           else (_FPS_EMA_ALPHA * instant_fps
                                                 + (1 - _FPS_EMA_ALPHA) * actual_fps_ema))
                last_capture_time = now

                # Written about once/sec regardless of how fast frames
                # are actually coming in - this is a status readout for
                # the dashboard, not something that needs to be as fresh
                # as the live JPEG itself.
                if actual_fps_ema is not None and (now - last_stats_write_time) >= 1.0:
                    state.save_camera_stats(actual_fps_ema, live_fps)
                    last_stats_write_time = now

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

            # 0.05s floor (20fps hard ceiling), not interval_seconds' own
            # 0.1s config-validation floor - protects against a corrupt or
            # hand-edited config.json with an even smaller/zero/negative
            # value pegging this loop (and a CPU core) at 100%.
            time.sleep(max(0.05, interval_seconds))
    finally:
        cap.release()


if __name__ == "__main__":
    main()
