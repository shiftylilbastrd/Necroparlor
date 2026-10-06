#!/usr/bin/env python3
"""Manually compile a mode's kept (uncompiled) timelapse frames into a
video - the recovery path for when the camera service's automatic
compile fails or times out and logs "raw frames kept".

Uses the exact same compile_session_video() the camera service does
(same encoder settings, same gallery entry, same delete-frames-after-
success behaviour), just with NO time limit and ffmpeg's progress line
shown in the terminal, since a person is deliberately waiting on it.
Safe to run while dermestid-camera.service is running: a per-mode lock
file means the service skips its own compile of that mode (leaving the
frames alone) if one is already in progress from here, and vice versa.

Usage (on the Pi, as the pi user, from the project directory):

    python3 compile_timelapse.py              # list modes with kept frames
    python3 compile_timelapse.py cleaning     # compile them

If the mode you name is the one currently ACTIVE, its frames include the
session still being recorded - compiling cuts that session at this
point (the next video picks up from here). Pass --include-current to
confirm that's what you want.

Don't run it with sudo: files it creates would be owned by root, and the
camera service (running as pi) couldn't clean them up later.
"""
import argparse
import os
import sys
import time
from datetime import datetime

import shared_state as state
import camera_service


def pending_by_mode():
    conn = state.get_db()
    rows = conn.execute(
        "SELECT mode, COUNT(*), MIN(ts), MAX(ts) FROM camera_snapshots GROUP BY mode ORDER BY mode"
    ).fetchall()
    conn.close()
    return rows


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", nargs="?", help="mode whose kept frames to compile")
    parser.add_argument("--include-current", action="store_true",
                        help="allow compiling the currently active mode's frames")
    args = parser.parse_args()

    if os.geteuid() == 0:
        print("Refusing to run as root - run it as the pi user (no sudo). "
              "See the note at the top of this file.", file=sys.stderr)
        return 2

    state.init_db()
    rows = pending_by_mode()
    current_mode = state.load_config().get("current_mode", "ready")

    if not args.mode:
        if not rows:
            print("No kept timelapse frames - nothing to compile.")
            return 0
        print(f"Kept (uncompiled) timelapse frames - current mode is '{current_mode}':")
        for mode, count, first, last in rows:
            est = count / state.timelapse_fps()
            active = "  (ACTIVE - still recording)" if mode == current_mode else ""
            print(f"  {mode:<12} {count:>6} frames  {fmt_ts(first)} -> {fmt_ts(last)}  "
                  f"~{est:.0f}s of video{active}")
        print("\nCompile one with: python3 compile_timelapse.py <mode>")
        return 0

    mode = args.mode
    match = [r for r in rows if r[0] == mode]
    if not match:
        known = ", ".join(r[0] for r in rows) or "none"
        print(f"No kept frames for mode '{mode}'. Modes with kept frames: {known}")
        return 1
    if mode == current_mode and not args.include_current:
        print(f"'{mode}' is the active mode, so its frames include the session still "
              "being recorded. Re-run with --include-current to compile them anyway "
              "(the rest of the current session will become a separate video).")
        return 1

    _, count, first, _ = match[0]
    end_ts = time.time()
    frames = state.get_camera_snapshots_in_range(mode, first, end_ts)
    if len(frames) < camera_service.MINIMUM_FRAMES_FOR_VIDEO:
        print(f"Only {len(frames)} frame(s) - fewer than the "
              f"{camera_service.MINIMUM_FRAMES_FOR_VIDEO} needed for a video.")
        return 1

    print(f"Compiling {len(frames)} {mode} frames ({fmt_ts(first)} -> now), no time limit. "
          "This can take a while on a Pi; ffmpeg's progress is shown below.")
    ok = camera_service.compile_session_video(mode, first, end_ts, frames,
                                              timeout_seconds=None, show_progress=True)
    print()
    if ok:
        print("Done - the video is on the Timelapse page and the raw frames were removed.")
        return 0
    print("Compile did not finish - raw frames kept. Check the dashboard log / "
          "logs/camera.log for the reason.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
