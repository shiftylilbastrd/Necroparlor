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

If dermestid-camera.service is running, it already has the real camera
device open, so it won't show up here (most UVC webcams only allow one
client at a time) - stop it first: `sudo systemctl stop
dermestid-camera.service`, then restart it afterwards. The dashboard's
own Config page "Discover" button (Camera card) does this same probing
without needing the CLI at all, and handles that stop/restart for you -
see --json below, which is what powers it.
"""
import argparse
import base64
import json
import os

import cv2

MAX_INDEX_TO_TRY = 10


def probe_devices(save_files=True, out_dir=None):
    """The actual probing logic, factored out so both this script's CLI
    and webapp.py's /api/camera-discover route (used by the Config
    page's Discover button) go through the exact same code path rather
    than maintaining two copies. Returns a list of dicts, one per
    working device index, each with width/height and either a
    saved_path (save_files=True, the CLI's original behavior) or an
    image_b64 (save_files=False - nothing touches disk, used by the web
    route so repeated dashboard clicks don't leave old discover_camera_
    N.jpg files scattered around the project directory forever)."""
    results = []
    for index in range(MAX_INDEX_TO_TRY):
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            continue
        ok, frame = cap.read()
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        if not ok:
            continue
        encode_ok, buf = cv2.imencode(".jpg", frame)
        if not encode_ok:
            continue
        entry = {"index": index, "width": width, "height": height}
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
            "dermestid-camera.service isn't already holding it open (`sudo systemctl stop "
            "dermestid-camera.service` first, if installed)."
        )
    else:
        for r in results:
            print(f"/dev/video{r['index']}: OK, {r['width']}x{r['height']} - saved a sample frame to {r['saved_path']}")
        print("\nCopy whichever index's sample image actually looks like your enclosure "
              "into config.json's camera.device (as a plain number, e.g. \"0\").")


if __name__ == "__main__":
    main()
