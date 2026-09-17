# Setting up camera-streamer (live view)

[2026-09-14] Live view switched from this project's own OpenCV capture
loop to [camera-streamer](https://github.com/ayufan/camera-streamer), a
purpose-built V4L2 streaming daemon that uses the Pi's hardware JPEG
encoder. See `PROJECT_STATUS.md`'s 2026-09-14 entries for the full
reasoning (the old pipeline did 100% software JPEG encoding and relayed
frames through Flask's dev server - both real, identified bottlenecks
camera-streamer removes).

**This is new, untested-on-real-hardware integration work.** The code
changes in this repo (camera_service.py, webapp.py, the Config page) are
written and self-consistent, but nobody has actually run camera-streamer
against this project's real Pi + USB webcam yet. Follow this doc's steps
in order, and expect to correct one or two things (flagged below) once
you see camera-streamer's actual `--help` output and behavior - that's
expected, not a sign something's wrong. This matches how the MJPG
capture-format fix and the fan/timelapse work earlier in this project
were also verified against real hardware before being called done, not
just documentation.

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

**Confirm the install path** - this repo's `systemd/camera-streamer.service`
assumes `sudo make install` puts the binary at `/usr/local/bin/camera-streamer`.
That was this project's best guess from camera-streamer's own docs, not
something directly confirmed - check with:

```bash
which camera-streamer
```

and fix `ExecStart=` in `systemd/camera-streamer.service` if it's
somewhere else.

## 2. Find the real CLI flags - especially the HTTP port

Run this before touching the systemd unit:

```bash
camera-streamer --help
```

Two things this project's systemd unit currently guesses at, that you
need to confirm/correct against that real output:

- **The HTTP port flag.** camera-streamer's documented default port is
  **8080** - the same port `webapp.py` already uses (see `app.run()` in
  `webapp.py`). The two can't both bind 8080 at once, so
  camera-streamer MUST be moved to a different port (this project
  defaults to **8090** - see `config.json`'s `camera.streamer_port` and
  the Config page's Camera card). `systemd/camera-streamer.service`
  currently guesses the flag is `--http-port=${CAMERA_STREAMER_PORT}` -
  this was NOT found documented anywhere in camera-streamer's own docs
  as of 2026-09-14 (only `--http-listen` for the bind address was
  confirmed). Confirm the real flag name from `--help` and fix the
  `ExecStart=` line if it's different.
- **USB camera flags.** `--camera-type=libcamera --camera-format=MJPEG`
  is camera-streamer's own documented recommendation for a USB (not
  CSI) camera - confirm it's still accurate for your installed version,
  and check their `docs/v4l2-usb-mode.md` for anything USB-specific
  `--help`'s summary doesn't cover.

## 3. Try it standalone first - before wiring up systemd

Confirms the device/flags actually work before trusting a background
service to it:

```bash
camera-streamer --camera-path=/dev/video0 --camera-type=libcamera \
    --camera-format=MJPEG --camera-width=1280 --camera-height=720 \
    --http-listen=0.0.0.0 --http-port=8090
```

(swap in whichever `/dev/videoN` `discover_camera.py` - or the Config
page's Discover button - finds for your webcam). Then from another
device on the same LAN:

- `http://<pi-ip>:8090/` - the index page
- `http://<pi-ip>:8090/stream` - should show a live MJPEG feed in a
  browser
- `http://<pi-ip>:8090/snapshot` - should return a single JPEG

If `/snapshot` doesn't behave as this project's `camera_service.py`
expects (a plain 200 response with JPEG bytes, no auth), that's a real
finding worth fixing the code for before moving on - don't just assume
the docs were right. Ctrl-C it once confirmed working.

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
