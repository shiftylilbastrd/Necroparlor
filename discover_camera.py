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
"""
import os

import cv2

MAX_INDEX_TO_TRY = 10


def main():
    found_any = False
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
            print(f"/dev/video{index}: opened but no frame could be read - probably not a real camera")
            continue
        found_any = True
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"discover_camera_{index}.jpg")
        cv2.imwrite(out_path, frame)
        print(f"/dev/video{index}: OK, {width}x{height} - saved a sample frame to {out_path}")

    if not found_any:
        print(
            "No working camera found on indices 0-9. Check `lsusb` shows the webcam, "
            "and that /dev/video* nodes exist at all (`ls /dev/video*`)."
        )
    else:
        print("\nCopy whichever index's sample image actually looks like your enclosure "
              "into config.json's camera.device (as a plain number, e.g. \"0\").")


if __name__ == "__main__":
    main()
