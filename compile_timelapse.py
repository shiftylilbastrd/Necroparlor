#!/usr/bin/env python3
"""Compile a stopped timelapse recording into a video from the command line -
the same compile the Timelapse page's Compile button runs (same encoder
settings, same gallery entry, same delete-frames-after-success behaviour),
but with NO time limit and ffmpeg's progress line shown in the terminal,
since a person is deliberately waiting on it. Use it when a dashboard
compile timed out ("frames kept").

[2026-10-16] Works on recordings now (started/stopped from the Timelapse
page), not per-mode sessions. Shares the dashboard's single encode lock, so
it refuses to start while a preview or another compile is running, and
vice versa.

Usage (on the Pi, as the pi user, from the project directory):

    python3 compile_timelapse.py          # list recordings with frames
    python3 compile_timelapse.py 7        # compile recording #7

Only STOPPED recordings can be compiled - stop a running one on the
Timelapse page first.

Don't run it with sudo: files it creates would be owned by root, and the
dashboard (running as pi) couldn't clean them up later.
"""
import argparse
import os
import sys
from datetime import datetime

import shared_state as state
import timelapse_encode


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "--"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("recording_id", nargs="?", type=int, help="id of the stopped recording to compile")
    args = parser.parse_args()

    if os.geteuid() == 0:
        print("Refusing to run as root - run it as the pi user (no sudo). "
              "See the note at the top of this file.", file=sys.stderr)
        return 2

    state.init_db()
    recs = state.get_recordings_with_frames()

    if args.recording_id is None:
        if not recs:
            print("No recordings with frames - nothing to compile.")
            return 0
        print("Timelapse recordings with frames:")
        for r in recs:
            est = r["frame_count"] / state.timelapse_fps()
            status = "RECORDING - stop it first" if r["status"] == "recording" else "stopped"
            print(f"  #{r['id']:<4} {state.recording_label(r)[:40]:<40} {r['frame_count']:>6} frames  "
                  f"{fmt_ts(r['first_ts'])} -> {fmt_ts(r['last_ts'])}  ~{est:.0f}s  ({status})")
        print("\nCompile one with: python3 compile_timelapse.py <id>")
        return 0

    rec = next((r for r in recs if r["id"] == args.recording_id), None)
    if not rec:
        print(f"No recording #{args.recording_id} with frames.")
        return 1
    if rec["status"] == "recording":
        print("That recording is still running - stop it on the Timelapse page first.")
        return 1
    if rec["frame_count"] < timelapse_encode.MINIMUM_FRAMES_FOR_VIDEO:
        print(f"Only {rec['frame_count']} frame(s) - at least {timelapse_encode.MINIMUM_FRAMES_FOR_VIDEO} "
              "are needed for a video.")
        return 1

    lock = state.try_encode_lock()
    if lock is None:
        print("Another timelapse encode (a preview or compile) is running - try again once it finishes.")
        return 1
    try:
        print(f"Compiling '{state.recording_label(rec)}' ({rec['frame_count']} frames), no time limit. "
              "This can take a while on a Pi; ffmpeg's progress is shown below.")
        result = timelapse_encode.compile_recording(rec["id"], timeout_seconds=None, show_progress=True)
    finally:
        state.release_encode_lock(lock)
    print()
    if result["ok"]:
        print("Done - the video is on the Timelapse page and the raw frames were removed.")
        return 0
    print(f"Compile did not finish - frames kept. {result['error']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
