"""The one place a timelapse's frames get turned into an .mp4 - shared by
the Timelapse page's Compile button and "Build preview" (webapp.py) and
compile_timelapse.py (SSH). [2026-10-16] Compiling is manual now (see
compile_recording() at the bottom) - nothing compiles on a mode change any
more. Keeping the ffmpeg command in one module means a preview is
always encoded exactly the way the final video will be.

Overlay: each frame can have its capture time and the time elapsed since
the session's first frame burned in, e.g.
    Sep 29, 2:05 PM · +2d 04h 12m
Done without touching the JPEGs: every entry in ffmpeg's concat list gets
a `file_packet_metadata 'overlay=<label>'` line, and a single drawtext
filter prints `%{metadata:overlay}` - i.e. whatever label was attached to
the frame currently being drawn. Measured on 400 1280x720 frames: no
measurable encode-time cost over the plain encode.
"""
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

import shared_state as state

# x264 preset for every timelapse encode. ffmpeg's implicit default is
# "medium"; "veryfast" measured ~2.5x faster on identical 1280x720 frames
# with an essentially identical (slightly smaller, even) output file - a
# static timelapse scene gives the slower presets' extra motion search
# nothing to find. The encode also runs under `nice -n 19` so it can
# never compete with climate.py (the actually safety-critical process)
# for CPU.
X264_PRESET = "veryfast"

# [2026-10-18] Fix attempt for the compile-makes-the-Pi-unresponsive issue
# (PROJECT_STATUS.md, 2026-10-14). nice only lowers CPU *priority* - x264
# still spreads across all four cores and ffmpeg reads every frame off the
# SD card with nothing holding it back, so sshd/Flask/sqlite can starve.
#   - ENCODE_THREADS caps x264 (and the filter graph) at 2 of the Pi 4's 4
#     cores: the encode takes roughly twice as long, but half the CPU is
#     always free and the SoC runs cooler (no thermal throttling).
#   - `ionice -c3` (idle class) lets ffmpeg read the SD card only when
#     nothing else wants it. Only honoured by the BFQ I/O scheduler - with
#     mq-deadline/none it's a harmless no-op, so check
#     /sys/block/mmcblk0/queue/scheduler on the Pi before counting on it.
ENCODE_THREADS = 2


def _low_priority_prefix():
    """nice (+ ionice when installed) - prepended to every ffmpeg encode."""
    prefix = ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c3"]
    return prefix

# Safety cap on the ffmpeg process. [2026-09-29] This used to be a flat
# 300s regardless of session size, which made any long session impossible
# to compile (an 8048-frame cleaning session timed out). It now scales
# with the frame count - ~10x what the Pi 4 actually measured (0.072s/
# frame, 8048 frames in 583s) - with a floor for tiny sessions, so it
# still catches a genuine hang, just not a legitimately big job.
TIMEOUT_FLOOR_SECONDS = 300
TIMEOUT_PER_FRAME_SECONDS = 1.0

MODE_LABELS = {"dormant": "Dormant", "breeding": "Breeding", "ready": "Ready", "cleaning": "Cleaning"}

# DejaVu ships with Raspberry Pi OS (fontconfig depends on it, and ffmpeg
# pulls in fontconfig). If it's somehow missing, drawtext falls back to
# fontconfig's default font instead - and if even that fails, encode()
# retries without the overlay rather than losing the video.
OVERLAY_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def timeout_for(frame_count):
    return max(TIMEOUT_FLOOR_SECONDS, frame_count * TIMEOUT_PER_FRAME_SECONDS)


def overlay_enabled():
    return bool(state.load_config().get("timelapse_overlay", True))


def frame_label(mode, ts, session_start_ts):
    """The burned-in text for one frame. Local time from state.LOCAL_TZ
    (the same zone the rest of the dashboard uses), and elapsed time
    counted from the first frame being encoded - for a preview that's the
    session start, so the numbers carry straight over to the final video.
    [2026-09-30] No mode name (Ryan's call) - the gallery card and the
    preview row already say which mode a video is from. `mode` is still
    passed in so it could come back without touching the callers."""
    when = datetime.fromtimestamp(ts, state.LOCAL_TZ)
    clock = when.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    elapsed = max(0, int(ts - session_start_ts))
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{clock} · +{days}d {hours:02d}h {minutes:02d}m"


def _quote(value):
    # ffmpeg's concat-list quoting: single quotes, with a literal single
    # quote written as '\''. Our paths/labels never contain one in
    # practice, but it's cheap insurance.
    return "'" + value.replace("'", "'\\''") + "'"


def _write_list(path, mode, frames, overlay, fps):
    duration = 1.0 / fps
    start_ts = frames[0]["ts"]
    with open(path, "w", encoding="utf-8") as f:
        for frame in frames:
            f.write(f"file {_quote(os.path.join(state.CAMERA_TIMELAPSE_DIR, frame['filename']))}\n")
            if overlay:
                f.write(f"file_packet_metadata {_quote('overlay=' + frame_label(mode, frame['ts'], start_ts))}\n")
            f.write(f"duration {duration}\n")
        # concat demuxer quirk: `duration` on the LAST entry is ignored
        # unless that same file is listed once more after it.
        f.write(f"file {_quote(os.path.join(state.CAMERA_TIMELAPSE_DIR, frames[-1]['filename']))}\n")


def _drawtext_filter():
    font = f"fontfile={OVERLAY_FONT}:" if os.path.exists(OVERLAY_FONT) else ""
    # TOP-left, font scaled to the video height so it reads the same at
    # 720p and 1080p, on a translucent box so it stays legible over both a
    # bright lid-open frame and a grey infrared one. [2026-09-30] Was
    # bottom-left, which is exactly where a browser's native video
    # controls sit - and they stay up once a clip ends (a short preview
    # ends almost immediately), so the label was in the file but hidden
    # behind the play button/timeline on the real Pi. Don't move it back
    # to the bottom.
    return (f"drawtext={font}text='%{{metadata\\:overlay}}':x=16:y=16:"
            "fontsize=h/28:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=8")


def _run(cmd, timeout_seconds, show_progress, progress_cb):
    """Runs ffmpeg, returning (returncode, stderr_tail, timed_out).
    A watchdog timer kills it at timeout_seconds (None = no limit). With
    progress_cb, ffmpeg's machine-readable `-progress` output is read
    line by line and progress_cb(frames_done) called as it goes."""
    stderr_file = None if show_progress else tempfile.TemporaryFile(mode="w+")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE if progress_cb else subprocess.DEVNULL,
        stderr=stderr_file, text=True)
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout_seconds, kill) if timeout_seconds else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        if progress_cb:
            for line in proc.stdout:
                if line.startswith("frame="):
                    try:
                        progress_cb(int(line.split("=", 1)[1]))
                    except ValueError:
                        pass
        proc.wait()
    finally:
        if timer:
            timer.cancel()
    tail = ""
    if stderr_file is not None:
        stderr_file.seek(0)
        tail = stderr_file.read()[-500:]
        stderr_file.close()
    return proc.returncode, tail, timed_out.is_set()


def _probe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def encode(mode, frames, video_path, timeout_seconds="auto", overlay=None,
           show_progress=False, progress_cb=None):
    """Encodes `frames` (dicts with ts/filename, oldest first) into
    video_path. Returns a dict: ok, timed_out, error (text), elapsed
    (seconds), overlay (whether the overlay actually made it in).
    Never deletes frames - that's the caller's decision.

    [2026-09-30] "ok" is verified, not taken from ffmpeg's exit code: when
    a frame's JPEG is missing, ffmpeg's concat demuxer logs "Impossible to
    open", STOPS READING THE LIST, and still exits 0 - leaving a video
    truncated at that frame. camera_service then deleted every frame of
    the session, so everything after the gap was silently lost. Now
    frames whose file is already missing are skipped up front (result
    "skipped"), and the finished video's duration is checked against the
    frames that went in; a short video is a failure (result "error" says
    so), so the caller keeps the frames.

    timeout_seconds: "auto" scales with frame count (timeout_for), None
    means no limit. overlay: None = use the config setting.
    If an overlay encode fails for a reason other than a timeout, it's
    retried once without the overlay, so a font/filter problem on the Pi
    costs the timestamps, never the video.

    fps is resolved ONCE here (state.timelapse_fps()) and used for every
    attempt within this single encode call, rather than re-read per
    attempt - so a settings change that lands mid-encode (overlay retry)
    can't produce a video with two different frame rates spliced
    together. The resolved value is also returned in the result dict
    (result["fps"]) so callers that compute a duration from frame count
    afterwards (camera_service.py) use the exact rate this encode
    actually ran at, immune to a config change between the encode
    finishing and the caller reading it back."""
    if timeout_seconds == "auto":
        timeout_seconds = timeout_for(len(frames))
    if overlay is None:
        overlay = overlay_enabled()
    fps = state.timelapse_fps()
    if not shutil.which("ffmpeg"):
        return {"ok": False, "timed_out": False, "elapsed": 0, "overlay": False, "fps": fps,
                "error": "ffmpeg not installed (sudo apt install ffmpeg)"}

    present = [f for f in frames
               if os.path.exists(os.path.join(state.CAMERA_TIMELAPSE_DIR, f["filename"]))]
    skipped = len(frames) - len(present)
    if len(present) < 2:
        return {"ok": False, "timed_out": False, "elapsed": 0, "overlay": False, "skipped": skipped,
                "frames_encoded": 0, "fps": fps, "error": f"only {len(present)} of {len(frames)} frame files exist"}
    frames = present
    expected_seconds = len(frames) / fps

    list_dir = os.path.dirname(video_path)
    os.makedirs(list_dir, exist_ok=True)
    started = time.time()
    attempts = [True, False] if overlay else [False]
    result = None
    for use_overlay in attempts:
        list_path = os.path.join(list_dir, f".list_{mode}_{os.getpid()}_{int(time.time() * 1000)}.txt")
        _write_list(list_path, mode, frames, use_overlay, fps)
        cmd = _low_priority_prefix() + ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                                        "-filter_threads", "1",
                                        "-f", "concat", "-safe", "0", "-i", list_path]
        if use_overlay:
            cmd += ["-vf", _drawtext_filter()]
        # -r is an OUTPUT constant frame rate - deliberately NOT combined
        # with -vsync/-fps_mode vfr, which newer ffmpeg rejects as
        # contradictory with an explicit -r. fps (resolved above from
        # state.timelapse_fps()) is the real playback-speed knob.
        cmd += ["-c:v", "libx264", "-preset", X264_PRESET, "-threads", str(ENCODE_THREADS),
                "-pix_fmt", "yuv420p", "-r", str(fps),
                # moov atom up front so the dashboard can start playing a
                # big video before the whole file has downloaded
                "-movflags", "+faststart"]
        if progress_cb:
            cmd += ["-progress", "pipe:1", "-nostats"]
        elif show_progress:
            cmd.append("-stats")
        cmd.append(video_path)
        try:
            code, tail, timed_out = _run(cmd, timeout_seconds, show_progress, progress_cb)
        finally:
            if os.path.exists(list_path):
                os.remove(list_path)
        ok = (code == 0 and not timed_out and os.path.exists(video_path)
              and os.path.getsize(video_path) > 0)
        error = "" if ok else (tail.strip() or f"ffmpeg exited with code {code}")
        if ok:
            # Allow two frames of slack for ffmpeg's CFR rounding.
            actual = _probe_duration(video_path)
            if actual is not None and actual < expected_seconds - 2.0 / fps:
                ok = False
                got = int(round(actual * fps))
                error = (f"video came out truncated ({got} of {len(frames)} frames) - a frame file "
                         "went missing during the encode")
        result = {"ok": ok, "timed_out": timed_out, "overlay": use_overlay and ok, "error": error,
                  "skipped": skipped, "frames_encoded": len(frames) if ok else 0, "fps": fps}
        if not ok and os.path.exists(video_path):
            # A killed or failed ffmpeg leaves a truncated, unplayable file.
            os.remove(video_path)
        if ok or timed_out or error.startswith("video came out truncated"):
            break  # a missing frame isn't the overlay's fault - don't retry
        if use_overlay:
            state.log_event("warning", f"Timelapse: encoding with the timestamp overlay failed for {mode} "
                                        f"({result['error'][:200]}) - retrying without it",
                             category="camera_issue")
    result["elapsed"] = time.time() - started
    return result


# --------------------------------------------------------------------------
# [2026-10-16] Compiling a timelapse RECORDING into a gallery video. Moved
# here from camera_service.py (compile_session_video) when timelapses became
# start/stop recordings instead of per-mode sessions: compiling is now
# MANUAL (the Timelapse page's Compile button, or compile_timelapse.py), so
# it runs in webapp.py's process or a terminal, never as a side effect of a
# mode switch. Every caller holds state.try_encode_lock() - one encode on
# the Pi at a time.
# --------------------------------------------------------------------------
MINIMUM_FRAMES_FOR_VIDEO = 3


def _slug(text):
    keep = [c if c.isalnum() else "-" for c in (text or "").strip()]
    slug = "-".join(part for part in "".join(keep).split("-") if part)
    return slug[:60] or "timelapse"


def video_download_name(video):
    """A client-friendly filename, e.g. "Smith-whitetail_2026-10-16.mp4"."""
    day = datetime.fromtimestamp(video.get("start_ts") or video.get("ts") or time.time(),
                                 state.LOCAL_TZ).strftime("%Y-%m-%d")
    label = video.get("name") or MODE_LABELS.get(video.get("mode"), video.get("mode") or "timelapse")
    return f"{_slug(label)}_{day}.mp4"


def compile_recording(recording_id, timeout_seconds="auto", show_progress=False, progress_cb=None):
    """Encodes a STOPPED recording's frames into a gallery video, then
    deletes the frames (the video is the record from then on). The caller
    must already hold the encode lock. Returns {"ok", "error", "video_id"}.
    On any failure the frames are left untouched - nothing is ever lost to
    a failed compile."""
    rec = state.get_recording(recording_id)
    if not rec:
        return {"ok": False, "error": "No such recording."}
    if rec["status"] != "stopped":
        return {"ok": False, "error": "Stop the recording before compiling it." if rec["status"] == "recording"
                else "This recording was already compiled."}
    label = state.recording_label(rec)
    frames = state.get_recording_frames(recording_id)
    if len(frames) < MINIMUM_FRAMES_FOR_VIDEO:
        return {"ok": False, "error": f"Only {len(frames)} frame(s) - at least {MINIMUM_FRAMES_FOR_VIDEO} "
                                      "are needed for a video."}
    os.makedirs(state.CAMERA_TIMELAPSE_VIDEOS_DIR, exist_ok=True)
    start_ts, end_ts = frames[0]["ts"], frames[-1]["ts"]
    base = f"rec{recording_id}_{int(start_ts)}_{int(end_ts)}"
    video_filename, poster_filename = base + ".mp4", base + ".jpg"
    video_path = os.path.join(state.CAMERA_TIMELAPSE_VIDEOS_DIR, video_filename)
    poster_path = os.path.join(state.CAMERA_TIMELAPSE_VIDEOS_DIR, poster_filename)

    result = encode(f"rec{recording_id}", frames, video_path, timeout_seconds=timeout_seconds,
                    show_progress=show_progress, progress_cb=progress_cb)
    if result["timed_out"]:
        limit = timeout_for(len(frames)) if timeout_seconds == "auto" else timeout_seconds
        msg = (f"ffmpeg timed out after {limit:.0f}s - frames kept. Run "
               f"compile_timelapse.py {recording_id} on the Pi to compile with no time limit.")
        state.log_event("error", f"Timelapse: compiling '{label}' ({len(frames)} frames): {msg}",
                        category="camera_issue")
        return {"ok": False, "error": msg}
    if not result["ok"]:
        state.log_event("error", f"Timelapse: compiling '{label}' ({len(frames)} frames) failed - frames kept. "
                                 f"{result['error']}", category="camera_issue")
        return {"ok": False, "error": result["error"]}

    # Middle frame as the gallery poster (more representative than the first).
    try:
        shutil.copyfile(os.path.join(state.CAMERA_TIMELAPSE_DIR, frames[len(frames) // 2]["filename"]),
                        poster_path)
    except FileNotFoundError:
        poster_filename = None
    if result["skipped"]:
        state.log_event("warning", f"Timelapse: {result['skipped']} frame(s) of '{label}' had no image file "
                                   "and were left out of the video", category="camera_issue")
    duration_seconds = result["frames_encoded"] / result["fps"]
    file_size_bytes = os.path.getsize(video_path)
    video_id = state.save_timelapse_video(rec.get("mode_at_start") or "", start_ts, end_ts,
                                          result["frames_encoded"], video_filename, poster_filename,
                                          duration_seconds, file_size_bytes,
                                          recording_id=recording_id, name=label)
    state.mark_recording_compiled(recording_id, video_id)
    state.delete_camera_snapshots([f["ts"] for f in frames])
    state.delete_recording_preview(recording_id)
    state.log_event("info", f"Timelapse: compiled '{label}' - a {duration_seconds:.0f}s video from "
                            f"{result['frames_encoded']} frames ({file_size_bytes / (1024 * 1024):.1f}MB) "
                            f"in {result['elapsed']:.0f}s")
    return {"ok": True, "error": "", "video_id": video_id}
