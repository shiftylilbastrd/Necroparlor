# Setting up camera-streamer (live view)

[2026-09-14] Live view switched from this project's own OpenCV capture
loop to [camera-streamer](https://github.com/ayufan/camera-streamer), a
purpose-built V4L2 streaming daemon that uses the Pi's hardware JPEG
encoder. See `PROJECT_STATUS.md`'s 2026-09-14 entries for the full
reasoning (the old pipeline did 100% software JPEG encoding and relayed
frames through Flask's dev server - both real, identified bottlenecks
camera-streamer removes).

**[2026-09-17 UPDATE] Confirmed working against a real Pi + USB webcam**
(camera-streamer v0.4.2) - standalone `/stream` and `/snapshot` both
tested and produced "100% live view with no jitter," a real fix for the
choppiness this whole switch was meant to solve. See
`PROJECT_STATUS.md`'s 2026-09-17 entry for the full record, including
one thing this doc got wrong at first (flagged inline below, in step 2)
and corrected once real `--help` output was in hand. The steps below are
now written from that confirmed run, not just documentation - though
wiring it up as a permanent systemd service (vs. the standalone
foreground test that was actually run) is still the next thing to
verify.

## 1. Build camera-streamer on the Pi

Build it in its own directory, NOT inside this repo (`~/dermestid`) -
keeps its build artifacts out of this project's git history entirely.

```bash
sudo apt-get -y install libavformat-dev libavutil-dev libavcodec-dev \
    libcamera-dev liblivemedia-dev v4l-utils pkg-config xxd \
    build-essential cmake libssl-dev
cd ~
git clone https://github.com/ayufan-research/camera-streamer.git --recursive
cd camera-streamer
make
sudo make install
```

Requires a 5.15+ or 6.1+ (LTS) kernel and Debian Bookworm - check
`uname -r` first; camera-streamer's own docs call out older setups
(Bullseye) needing their `stable-0.3` branch instead.

**Confirmed** - `sudo make install` does put the binary at
`/usr/local/bin/camera-streamer` (verified with `which camera-streamer`
on 2026-09-17). `systemd/camera-streamer.service`'s `ExecStart=` already
points there; no fix needed. Worth double-checking on your own install
anyway with `which camera-streamer`, just in case a future
camera-streamer release changes its install layout.

## 2. Find the real CLI flags

```bash
camera-streamer --help
```

**Both things this doc originally asked you to confirm here are now
settled**, from a real 2026-09-17 run (camera-streamer v0.4.2) - kept
here for the record:

- **The HTTP port flag is `--http-port`** - confirmed directly in
  `--help`'s own output, exactly as `systemd/camera-streamer.service`
  already uses it. (Needed regardless of version: camera-streamer's
  default port is **8080**, the same port `webapp.py` uses - see
  `config.json`'s `camera.streamer_port`, defaulted to **8090** here to
  avoid the collision, and `validate_camera_settings()` in
  `shared_state.py`, which rejects 8080 outright.)
- **USB camera type is `--camera-type=v4l2`, NOT `libcamera`.** This
  doc originally said `libcamera` based on an indirect summary of
  camera-streamer's own docs - wrong. `--help`'s own example command
  line defaults to `v4l2`, and that's what was actually run and
  confirmed working below. libcamera's pipeline is built around the
  Pi's CSI camera + ISP, not a generic USB UVC device - `v4l2` is the
  right type for exactly this project's hardware.
  `--camera-format=MJPEG` was correct as originally written (forces the
  same compressed capture format that fixed the old OpenCV pipeline's
  choppiness too - see the MJPG capture-format entries in
  `PROJECT_STATUS.md`).

## 3. Try it standalone first - before wiring up systemd

Confirms the device/flags actually work before trusting a background
service to it - this is the exact command that was run and confirmed
working on 2026-09-17:

```bash
camera-streamer --camera-path=/dev/video0 --camera-type=v4l2 \
    --camera-format=MJPEG --camera-width=1280 --camera-height=720 \
    --http-listen=0.0.0.0 --http-port=8090
```

(swap in whichever `/dev/videoN` `discover_camera.py` - or the Config
page's Discover button - finds for your webcam). Then from another
device on the same LAN:

- `http://<pi-ip>:8090/` - the index page
- `http://<pi-ip>:8090/stream` - should show a live MJPEG feed in a
  browser. **Confirmed 2026-09-17: real live view, no jitter** - the
  actual fix this whole camera-streamer switch was for.
- `http://<pi-ip>:8090/snapshot` - should return a single JPEG.
  **Confirmed 2026-09-17**: real requests logged and handled correctly
  in camera-streamer's own console output.

Ctrl-C it once confirmed working, before moving on to step 4.

## 4. Wire it up

```bash
cd ~/dermestid
git pull   # picks up camera_service.py, webapp.py, and this doc
```

Set the device/resolution/port either via the Config page's Camera card
(writes `camera-streamer.env` and restarts the service for you), or by
hand:

```bash
cat > ~/dermestid/camera-streamer.env <<EOF
CAMERA_PATH=/dev/video0
CAMERA_WIDTH=1280
CAMERA_HEIGHT=720
CAMERA_STREAMER_PORT=8090
EOF
```

```bash
sudo cp ~/dermestid/systemd/camera-streamer.service /etc/systemd/system/
sudo cp ~/dermestid/systemd/dermestid-camera.service /etc/systemd/system/   # updated - timelapse only now
sudo systemctl daemon-reload
sudo systemctl enable --now camera-streamer.service
sudo systemctl enable --now dermestid-camera.service
```

```bash
systemctl status camera-streamer.service
journalctl -u camera-streamer.service -f
```

Then open the dashboard's Home page - the live view `<img>` should point
at `http://<pi-ip>:8090/stream` (visible in your browser's dev tools, or
via the "available"/`stream_url` fields `/api/camera/status` returns).

## 5. Sudoers, if you want the Config page's Discover button and camera
   settings to work without SSHing in

Same pattern as the rest of this project's sudoers setup - see the main
README's "Automatic updates" section for the full file, and add these
three lines to it (`sudo visudo -f /etc/sudoers.d/dermestid`):

```
pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart camera-streamer.service
pi ALL=(root) NOPASSWD: /usr/bin/systemctl stop camera-streamer.service
pi ALL=(root) NOPASSWD: /usr/bin/systemctl start camera-streamer.service
```

## What changed vs. what's still open

**Confirmed against real hardware (2026-09-17):** the standalone
`camera-streamer --camera-path=/dev/video0 --camera-type=v4l2 ...`
command in step 3 above, run manually in the foreground - real
`/stream` and `/snapshot` requests, genuinely smooth live view.

**Not yet confirmed:** running it as the actual `camera-streamer.service`
systemd unit (step 4) - EnvironmentFile substitution, `Restart=on-failure`
recovery, and the dashboard's own reachability check
(`/api/camera/status`) all still need a real test once it's enabled as a
background service rather than a foreground command. The sudoers lines
(step 5) haven't been added yet either, so the Config page's Discover
button and camera-settings Save will keep failing to auto-restart the
service until they are.

**Done, written against camera-streamer's documented API:**
`camera_service.py` no longer opens the USB device at all - it only
pulls a timelapse snapshot from camera-streamer's own `/snapshot`
endpoint on each mode's `snapshot_interval_minutes`. `webapp.py`'s live
view now points the dashboard straight at camera-streamer's `/stream`
instead of relaying frames itself through Flask's dev server. The
Config page's Camera card now configures camera-streamer's own
device/resolution/port (written to `camera-streamer.env`) instead of a
`cv2.VideoCapture` this project no longer has.

**Not yet done / explicitly deferred:** JPEG quality and capture-rate
tuning are camera-streamer's own CLI flags now (see `docs/configure.md`
in its repo - `--camera-snapshot.options=compression_quality=NN` and
similar), not wired into the Config page - edit
`systemd/camera-streamer.service`'s `ExecStart=` by hand if you want to
tune those for now. Whether camera-streamer can ALSO be told to serve
`/snapshot` and `/stream` at genuinely independent quality/resolution
settings (rather than one shared capture config) wasn't confirmed
either - worth checking once it's running for real.
