#!/usr/bin/env python3
"""
One-time helper: probes /dev/video0 through /dev/video9 for a USB
webcam that actually opens and delivers a frame, and saves a sample
JPEG for each one it finds - so if more than one video device shows up
(some USB webcams register two indices, one for video and one for
metadata), you can just look at the saved images to tell which index is
the real camera before putting that number into config.json's
camera.device (or the Config page's camera device field).

Run: python3 discover_camera.py
Saved images land next to this script as discover_camera_N.jpg.

If camera-streamer.service is running, it already has the real camera
device open, so it won't show up here (most UVC webcams only allow one
client at a time) - stop it first: `sudo systemctl stop
camera-streamer.service`, then restart it afterwards. [2026-09-14]
camera-streamer replaced camera_service.py as the process that holds the
device open for live view (see docs/camera-streamer-setup.md) -
camera_service.py itself now only pulls occasional timelapse snapshots
over HTTP and never opens the device, so it doesn't need stopping for
this anymore. The dashboard's own Config page "Discover" button (Camera
card) does this same probing without needing the CLI at all, and
handles that stop/restart for you - see --json below, which is what
powers it.
"""
import argparse
import base64
import json
import os

import cv2

MAX_INDEX_TO_TRY = 10

# Curated common webcam resolutions, descending by pixel count - not
# every camera supports every one of these, which is exactly what
# probe_supported_resolutions() below actually checks. This backs the
# Config page's resolution dropdown, so it's a superset of anything a
# real USB webcam is likely to advertise (a mix of 16:9 and 4:3 entries,
# since plenty of webcams are one or the other, not both).
COMMON_RESOLUTIONS = [
    (3840, 2160), (2560, 1440), (1920, 1080), (1600, 1200),
    (1280, 960), (1280, 720), (1024, 768), (800, 600),
    (640, 480), (640, 360), (320, 240),
]


def probe_supported_resolutions(cap):
    """For an already-open cv2.VideoCapture, tests each COMMON_
    RESOLUTIONS entry by actually requesting it and reading a frame
    back, keeping only the ones the driver actually delivers (within a
    couple pixels - some UVC drivers round an odd requested dimension
    to the nearest one they actually support). This is what backs the
    Config page's resolution dropdown - "supported by the camera" is
    genuinely checked here, not just a generic list shown regardless of
    what the connected webcam can actually do.

    Deliberately request-and-READ-back, not just set()+get() without
    ever capturing a frame: some V4L2 drivers only actually commit a
    format change once streaming genuinely starts, so set()+get() alone
    can report "success" for a resolution the driver silently falls
    back away from the moment a frame is actually grabbed at it.

    Doesn't touch the caller's already-open device handle beyond
    changing its current format several times in a row - the caller is
    expected to release() it when done, same as before this existed."""
    supported = []
    for width, height in COMMON_RESOLUTIONS:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        ok, _frame = cap.read()
        if not ok:
            continue
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if abs(actual_w - width) <= 2 and abs(actual_h - height) <= 2:
            supported.append([width, height])
    return supported


def probe_devices(save_files=True, out_dir=None):
    """The actual probing logic, factored out so both this script's CLI
    and webapp.py's /api/camera-discover route (used by the Config
    page's Discover button) go through the exact same code path rather
    than maintaining two copies. Returns a list of dicts, one per
    working device index, each with width/height, supported_resolutions
    (see probe_supported_resolutions above - the camera's actual max
    negotiated capability, from the same 1920x1080 request this already
    made, is always included even if it didn't happen to be an exact
    match in COMMON_RESOLUTIONS), and either a saved_path (save_files=
    True, the CLI's original behavior) or an image_b64 (save_files=
    False - nothing touches disk, used by the web route so repeated
    dashboard clicks don't leave old discover_camera_N.jpg files
    scattered around the project directory forever)."""
    results = []
    for index in range(MAX_INDEX_TO_TRY):
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            continue
        # Force MJPG before requesting a resolution - same reasoning as
        # camera_service.py's open_capture() (see its docstring): without
        # this, OpenCV/V4L2 can silently negotiate an uncompressed format
        # at high resolutions, which this probe would then report as
        # "supported" even though camera_service.py (which forces MJPG
        # too, as of 2026-09-13) might behave very differently at that
        # same resolution in practice. Keeping this probe's request
        # identical to what the real capture loop asks for is what makes
        # "supported by this camera" here actually mean "supported the
        # way this project will really use it."
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        # Without an explicit request, OpenCV/V4L2 hands back whatever
        # resolution the driver defaults to - commonly 640x480 for UVC
        # webcams - which isn't the camera's actual capability, just its
        # out-of-the-box default. Asking for something high (1920x1080)
        # first makes V4L2 negotiate up to the nearest resolution the
        # hardware actually supports, so what gets read back afterward is
        # a real "here's what this camera can do," not just its default -
        # this is what config.html's Discover button pre-fills the
        # resolution dropdown's own detected-max option from.
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        ok, frame = cap.read()
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not ok:
            cap.release()
            continue
        # Uses the same already-open handle - probing candidate
        # resolutions one after another on a device that's already
        # open is far cheaper than reopening it per candidate, and
        # this doesn't affect the sample frame/width/height captured
        # just above (that's already in hand before this runs).
        supported_resolutions = probe_supported_resolutions(cap)
        # The camera's actual negotiated max (from the 1920x1080 request
        # above) isn't necessarily one of COMMON_RESOLUTIONS' exact
        # entries - always include it so the dropdown's top option is
        # genuinely "this camera's real max," not just whichever common
        # resolution happened to be closest.
        if [width, height] not in supported_resolutions:
            supported_resolutions.insert(0, [width, height])
        cap.release()
        encode_ok, buf = cv2.imencode(".jpg", frame)
        if not encode_ok:
            continue
        entry = {"index": index, "width": width, "height": height,
                  "supported_resolutions": supported_resolutions}
        if save_files:
            target_dir = out_dir or os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(target_dir, f"discover_camera_{index}.jpg")
            with open(path, "wb") as f:
                f.write(buf.tobytes())
            entry["saved_path"] = path
        else:
            entry["image_b64"] = base64.b64encode(buf.tobytes()).decode("ascii")
        results.append(entry)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--json", action="store_true",
        help="Print machine-readable JSON (each device's sample frame inline as base64, "
             "nothing saved to disk) instead of the normal human-readable output. This is "
             "what the dashboard's own Discover button uses - not meant for interactive use."
    )
    args = parser.parse_args()

    if args.json:
        print(json.dumps(probe_devices(save_files=False)))
        return

    results = probe_devices(save_files=True)
    if not results:
        print(
            "No working camera found on indices 0-9. Check `lsusb` shows the webcam, "
            "that /dev/video* nodes exist at all (`ls /dev/video*`), and that "
            "camera-streamer.service isn't already holding it open (`sudo systemctl stop "
            "camera-streamer.service` first, if installed)."
        )
    else:
        for r in results:
            res_list = ", ".join(f"{w}x{h}" for w, h in r["supported_resolutions"])
            print(f"/dev/video{r['index']}: OK, max {r['width']}x{r['height']} - "
                  f"saved a sample frame to {r['saved_path']}")
            print(f"  Supported resolutions: {res_list}")
        print("\nCopy whichever index's sample image actually looks like your enclosure "
              "into config.json's camera.device (as a plain number, e.g. \"0\"), and pick "
              "one of its supported resolutions for camera.width/height.")


if __name__ == "__main__":
    main()
