# Setting up camera-streamer (live view)

Live view is served by [camera-streamer](https://github.com/ayufan/camera-streamer),
a purpose-built V4L2 streaming daemon that uses the Pi's hardware JPEG
encoder. It replaced this project's own OpenCV capture loop on 2026-09-17
(decided 2026-09-14) - see `PROJECT_STATUS.md` for the reasoning: the old
pipeline did 100% software JPEG encoding and relayed frames through
Flask's dev server, both real bottlenecks camera-streamer removes.

**Status:** running on the Pi 4 as `camera-streamer.service` (v0.4.2;
standalone test 2026-09-17, systemd unit confirmed installed and running
in the 2026-10-06 verification pass). Live view, `/snapshot`, the
dashboard's reachability check, and the Camera page's save-and-restart
all work.

camera-streamer is the only process that opens the USB webcam.
`camera_service.py` never touches the device - it pulls a JPEG from
camera-streamer's `/snapshot` endpoint at the running timelapse
recording's interval (nothing is captured when no recording is running).

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

camera-streamer's own docs ask for a 5.15+ or 6.1+ (LTS) kernel and
Debian Bookworm or newer (this Pi runs Raspberry Pi OS Trixie) - check
`uname -r` first; older Bullseye setups need their `stable-0.3` branch.

`sudo make install` puts the binary at `/usr/local/bin/camera-streamer`
(confirmed with `which camera-streamer`), which is what
`systemd/camera-streamer.service`'s `ExecStart=` points at.

## 2. The flags this project relies on

Confirmed against `camera-streamer --help` on the real Pi (v0.4.2):

- **`--http-port`** sets the HTTP port. Needed because camera-streamer
  defaults to **8080**, the same port `webapp.py` uses. This project
  uses **8090** (`config.json`'s `camera.streamer_port`), and
  `validate_camera_settings()` in `shared_state.py` rejects 8080 outright.
- **`--camera-type=v4l2`**, NOT `libcamera`. libcamera is built around
  the Pi's CSI camera + ISP; a plain USB UVC webcam is `v4l2`.
- **`--camera-format=MJPEG`** - the webcam's compressed capture format
  (raw YUYV at high resolution saturates USB2).
- **`--camera-video.disabled=1`** - turns the H264 encoder off. It
  defaults to *enabled*, so this is set explicitly rather than just
  omitted. See "H264 / `/video`" below.

## 3. Try it standalone first

Confirms the device and flags work before trusting a background service
to it - this is the command that was run and confirmed on 2026-09-17:

```bash
camera-streamer --camera-path=/dev/video0 --camera-type=v4l2 \
    --camera-format=MJPEG --camera-width=1280 --camera-height=720 \
    --http-listen=0.0.0.0 --http-port=8090 \
    --camera-video.disabled=1
```

(swap in whichever `/dev/videoN` `discover_camera.py` finds for your
webcam). Then from another device on the same LAN:

- `http://<pi-ip>:8090/` - the index page
- `http://<pi-ip>:8090/stream` - live MJPEG feed in a browser
- `http://<pi-ip>:8090/snapshot` - a single JPEG

Ctrl-C it once confirmed working.

## 4. Wire it up

Set the device/resolution/port on the dashboard (Settings → Camera, USB
webcam card - saving writes `camera-streamer.env` and restarts the
service for you), or by hand:

```bash
cat > ~/dermestid/camera-streamer.env <<EOF
CAMERA_PATH=/dev/video0
CAMERA_WIDTH=1280
CAMERA_HEIGHT=720
CAMERA_STREAMER_PORT=8090
EOF
```

`camera-streamer.env` is generated, host-specific, and not git-tracked
(same as `config.json`).

```bash
sudo cp ~/dermestid/systemd/camera-streamer.service /etc/systemd/system/
sudo cp ~/dermestid/systemd/dermestid-camera.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-streamer.service
sudo systemctl enable --now dermestid-camera.service
```

```bash
systemctl status camera-streamer.service
journalctl -u camera-streamer.service -f
```

The Home page's live view then points straight at camera-streamer's
`/stream` on port 8090, using whatever host name or IP you reached the
dashboard by (`/api/camera/status` returns it as `stream_url`).
iPhone/iPad browsers can't show an MJPEG stream in an `<img>`, so on
those the page detects the failure after ~3s and falls back to
reloading `/snapshot` (labelled "Live (reduced frame rate ...)").

**`auto_update.sh` never re-installs this unit or restarts
camera-streamer.** If a commit changes `systemd/camera-streamer.service`,
re-run the `cp` + `daemon-reload` above and
`sudo systemctl restart camera-streamer.service` - the 2026-10-06
verification found the Pi had run an outdated copy for ~14 hours for
exactly this reason.

## 5. Sudoers, for the Camera page's Discover button and settings save

Add these to `/etc/sudoers.d/dermestid` (`sudo visudo -f
/etc/sudoers.d/dermestid`) - the main README's "Automatic updates"
section has the full file:

```
pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart camera-streamer.service
pi ALL=(root) NOPASSWD: /usr/bin/systemctl stop camera-streamer.service
pi ALL=(root) NOPASSWD: /usr/bin/systemctl start camera-streamer.service
```

Without them, saving still works but the restart has to be done by
hand, and Discover can't see the device camera-streamer is holding open.

## H264 / `/video` (tried, reverted)

camera-streamer's adaptive `/video` endpoint (HLS/MP4, which iOS can
play natively) was tried on 2026-10-06 as a smoother alternative to the
iOS snapshot fallback. Adding the H264 tuning flags from `--help` fixed
`/video`'s frozen first frame, but the dashboard got noticeably slower
and the camera availability check started flapping, so it was reverted
(`--camera-video.disabled=1`).

It was **not** a CPU problem - the logged load stayed around 0.1 and the
Pi stayed cool. Best guess, unconfirmed: a slow HLS client tying up
camera-streamer's HTTP connection handling. If this is revisited, look
at camera-streamer's connection/threading behaviour first, not just
bitrate or resolution. `home.html` still contains the unused
`tryVideoFallback()` path and `/api/camera/status` still returns
`video_url`, ready for that.

## Not wired into the dashboard

JPEG quality and capture rate are camera-streamer CLI flags
(`--camera-snapshot.options=compression_quality=NN` and similar - see
`docs/configure.md` in its repo), not dashboard settings; edit
`systemd/camera-streamer.service`'s `ExecStart=` to tune them. Whether
`/snapshot` and `/stream` can run at independent quality/resolution
hasn't been checked.
