# Project status (read this first in a new session)

This file exists because long troubleshooting sessions eventually get
auto-summarized, and auto-summaries flatten *why* a decision was made
even when they keep *what* changed. Everything below is the kind of
thing that's easy to silently reverse if you don't know it was
deliberate. The README documents how the system works; this documents
why it works that way and what's still in motion.

**Maintenance convention**: append dated entries under "Open threads /
known issues" as things come up or get resolved. Don't rewrite the
"Load-bearing decisions" section casually - only touch an entry there
if the actual underlying decision changed, and say so explicitly
rather than just editing it silently.

## Current state (as of this writing)

**[STANDING CONTEXT, stated by Ryan 2026-09-14 - re-read this every session,
it keeps getting lost across auto-summaries]**
1. **This is still benchtop development.** Nothing here is installed in
   the actual freezer enclosure yet. Don't treat "confirmed on real
   hardware" entries elsewhere in this file as "confirmed in the final
   deployment" - they mean "confirmed on the benchtop rig."
2. **All three sensors (internal DHT22, external DHT22, and the BLE
   sensor if attached) are sitting within inches of each other** on the
   bench right now. Internal/external readings tracking each other
   closely is expected right now and is NOT evidence the fixed-identity/
   fallback logic is broken - there's no real environmental separation
   yet to tell them apart.
3. **The relays are not switching any actual devices** - no real heater,
   fan, dehumidifier, or light is connected downstream of them right
   now. A relay "safety cutoff" (e.g. the 20-minute heater max-runtime)
   firing is expected/mechanical in this state, not evidence of an
   actual heating/cooling problem, since there's no real thermal load
   for the internal temperature to ever respond to.
4. **No separate power supply is available yet.** Everything currently
   has to be powered off the Pi's own GPIO 5V/3.3V pins for testing -
   don't suggest an external PSU as a near-term fix without checking
   whether one has become available.

- **`main` is current and complete.** The `ble-genericization` branch
  (full SensorPush→generic-BLE rebrand, the fixed-sensor-identity
  dashboard redesign, branch-selection in the update system, and
  several smaller fixes) has been merged into `main`, and the Pi has
  been switched back to tracking `main`.
- **`add-webcam` (started 2026-09-12) has been merged into `main`
  (2026-09-14).** Everything from the webcam work - live view + per-mode
  timelapse, the camera MJPG/fps fixes, the header/nav UI batch, the
  README rewrite and GPIO pinout diagram, the jumper-color recolor and
  3.3V/5V DHT22 wiring correction, and the sensorpush cleanup - is now
  on `main`. Verified directly by comparing both branches in a fresh
  clone: `main` fully contains every `add-webcam` commit, 0 remaining
  divergence. **Merged via GitHub Desktop's local merge+push, not a
  GitHub PR** - worth remembering because that path does NOT delete the
  source branch automatically the way a PR merge (with "Delete branch"
  clicked, or auto-delete-head-branches enabled) does; `add-webcam`
  stayed on the remote until manually deleted afterward. Still not run
  against real deployed hardware - see Open threads' camera/timelapse
  entries above for what remains unconfirmed on that front regardless
  of which branch it lives on now.
- **Pi 4 8GB migration completed (2026-09-13)** - moved the existing SD
  card into the Pi 4 as planned (no re-flash/re-clone needed; Raspberry
  Pi OS auto-detected the board via device-tree). Original motivation:
  more headroom for the camera work, identical GPIO pinout across the
  3/4 family so no rewiring needed, and the theory that the Pi 4's
  more robust power delivery would resolve the Tier-3 BLE/under-voltage
  issue below (shared power rail theory). **That last part did NOT
  pan out**: BLE sensor data is still missing after the migration and
  a dashboard reboot - see the dated Tier-3 entry below. This is a
  significant data point - it rules out "just a 3B+ power budget
  problem" as the sole cause, since the Pi 4 has meaningfully better
  power delivery and the symptom persisted anyway. Still to check:
  what power supply is actually being used on the Pi 4 (it needs its
  own proper 5V/3A USB-C supply - reusing the 3B+'s old micro-USB
  supply via an adapter would reintroduce a *new* under-voltage
  problem, not fix the old one) - not yet confirmed either way.
  **`camera-streamer` (ayufan's, a
  native binary alternative to the hand-rolled MJPEG relay - see the
  superseded live-view entry below for why it wasn't used initially) is
  deliberately not yet integrated into the dashboard** - the plan is to
  verify it runs standalone on the Pi 4 first, then write dashboard
  integration code against its actual observed API behavior, not
  against documentation alone.
- **SD card upgraded (2026-09-14)**: 16GB SanDisk Class 4 -> 64GB PNY
  Elite-X (UHS-I U3, A1, V30), cloned over via `rpi-clone` rather than a
  from-scratch reinstall. See the dated Open-threads entry below for the
  full story, including a real `rpi-clone` gotcha (it doesn't reliably
  fix up `/boot/firmware/cmdline.txt`'s PARTUUID the way it does
  `/etc/fstab`) worth knowing before doing this again. Confirmed booting
  and running with 48.1GB free. Old 16GB card kept as an untouched spare.
- The Pi should have `dermestid-ble.service` installed and enabled
  (replacing the old `dermestid-sensorpush.service`, which should be
  stopped/disabled/removed). Sudoers should authorize
  `dermestid-ble.service`, not the old name.
- **[correction, 2026-09-12]** `config.json` is *supposed to be*, and
  as of this entry actually IS again, not git-tracked - but it had
  silently drifted back to being tracked (last touched by a normal
  "Add files via upload" commit, `f25443d`) with no corresponding
  `.gitignore` entry, directly contradicting this file's own older
  claim below that it wasn't. Almost certainly an accidental
  re-upload via the GitHub web UI drag-and-drop workflow this project
  uses for deploys (see "Established workflows" below) - dragging in a
  local `config.json` re-adds it to the commit if it's included in the
  drop. Re-fixed here: `git rm --cached config.json` + re-added to
  `.gitignore`. **If you ever see `config.json` show up as a pending
  change in `git status` on the Pi again, that's this same drift
  happening again, not a new bug** - check `.gitignore` and re-remove
  it from tracking rather than assuming the working file itself is
  wrong.
- `config.json` is deliberately **not git-tracked** (see above) - a
  fresh clone won't have one, and that's correct, not a bug.

## Load-bearing decisions

### Sensor identity vs. role - the recurring theme
Several real bugs this project has hit trace back to the same root
cause: confusing a physical sensor's *identity* with the *role* it
happens to be playing (active/fallback) at a given moment. The fix
pattern has been consistent: tie things to identity, never to role.
- Calibration offsets are keyed `internal_temp_offset`,
  `ble_temp_offset`, `wired_temp_offset` - never `external_*` or
  `fallback_*` - because which physical sensor is "active" changes
  automatically, and an offset has to follow the hardware, not the
  label. Verified directly: forced a failover, confirmed the wired
  probe's own offset stayed correct rather than picking up the BLE
  sensor's.
- The `readings` table stores `ble_temp`/`ble_humidity` and
  `wired_temp`/`wired_humidity` as separate, always-populated columns
  - each always holds that specific sensor's own reading, whether it's
  currently active or not. `active_external_source` is a label on top,
  never a reason data moves to a different column. (Earlier design had
  a single `fallback_external_temp` column holding "whichever isn't
  active" - meant the same field could silently show a different
  physical sensor's data depending on system state. This was a real
  reported bug, fixed by the redesign above.)
- **UI labels are intentionally different from the code's internal
  names.** The dashboard and Data page show "External" (= the BLE
  sensor) and "Fallback" (= the wired probe), matching what the Config
  page already established - even though the underlying fields and
  variables are named `ble_*`/`wired_*` in code. This is deliberate,
  not inconsistency to "clean up" - it was an explicit request to match
  existing UI terminology, made *after* the fixed-identity redesign
  was built. Don't rename the code fields to match the UI text, and
  don't rename the UI text back to `ble`/`wired`.

### External sensor failover
- Fully automatic, not a manual toggle: the BLE sensor is used
  whenever it's reported within `SENSOR_FAIL_TIMEOUT` (90s), the wired
  probe automatically otherwise. There is no `external_source` config
  key anymore (removed along with the manual toggle it used to
  control).
- Only **internal** sensor loss triggers the full critical failsafe
  (forces heat/fan/dehum off after 90s) - every control decision
  depends on internal readings, so there's no safe degraded mode
  there. Losing the **external** sensor (both BLE and wired down)
  only pauses thermal cooling specifically, since that's the one
  decision that genuinely needs to know if outside air would help.
  Heating and dehumidifying keep running on internal data alone
  either way.
- Every failover transition (both directions) is logged once, not
  spammed every cycle.

### Heat vs. fan/vent interaction
- `cooling_needed = thermal_cool_request or vent_active` drives the
  fan either way, but the heat-blocking mutual-exclusion check only
  looks at `thermal_cool_request` specifically, not `cooling_needed`.
  Reasoning: genuine thermal cooling and heat really would fight each
  other (heating air that's about to be vented out), but the
  scheduled cleaning-mode ventilation cycle is timer-driven for air
  quality, not temperature - blocking heat during it could cause a
  real temperature dip unrelated to why the fan turned on. Heat is
  allowed to run alongside a scheduled vent cycle; it's still blocked
  during real thermal cooling.

### Sensor reading validation
- The anti-glitch filter (`validate_reading()`) rejects an
  implausible single-cycle jump, but has a streak-recovery mechanism:
  3 consecutive readings that are mutually consistent with each other
  (even though they differ from the old accepted baseline) get
  accepted as a genuine sustained change. Without this, a real gradual
  drift gets rejected forever against an ever-more-stale frozen
  reference - this happened for real once (internal humidity), causing
  an emergency shutdown that never recovered until the service was
  manually restarted.
- DHT22 reads get up to 3 retries within a single cycle
  (`DHT_READ_RETRIES`) before giving up - ordinary single-attempt DHT
  flakiness is well-documented, and this measurably cut the false
  "sensor unavailable" rate (roughly 37% → under 5% of cycles, under a
  simulated 35% single-attempt failure rate matching real observed log
  frequency). There's also a `DHT_READ_GAP_SECONDS` pause between the
  internal and wired DHT22 reads each cycle - built as a hypothesis
  about back-to-back single-wire interference, but the retry logic
  above turned out to be the more impactful fix. The gap is harmless
  either way; just don't assume it was the thing that actually
  resolved the false-alarm pattern.

### BLE sensor support
- Brand is pluggable via `shared_state.BLE_SENSOR_LIBRARIES` (a
  registry of package/class per brand: SensorPush, Govee, INKBIRD,
  Xiaomi, RuuviTag). SensorPush, Govee, and INKBIRD's exact class names
  were directly verified against real source/usage examples; Xiaomi
  and RuuviTag follow the same well-established naming convention but
  weren't individually confirmed - `ble_listener.py` fails with a
  clear, actionable error (not a silent crash) if a class name turns
  out to be wrong.
- `ble_battery.py` is genuinely SensorPush-HT1-specific and **cannot**
  be generalized the same way as the listener - its GATT characteristic
  and voltage formula are reverse-engineered for that one device's
  firmware specifically, with no equivalent cross-brand library
  ecosystem the way passive advertisement decoding has. It skips
  itself cleanly (logging why) if a different brand is configured.
  Many other brands include battery directly in the passive
  advertisement instead, which `ble_listener.py` saves for free when
  present - check there before assuming a brand needs its own battery
  script.
- Watchdog timeout is 2 minutes (`WATCHDOG_TIMEOUT`), not the
  originally-designed 10 - shortened based on a real observed pattern
  (a clean ~10-minute stretch, then a stall that a simple restart
  reliably clears). There's also a retry loop for BlueZ's "already in
  progress" error on `scanner.start()`, which handles a *different,
  shorter-lived* stuck state than the watchdog does.
- **Three tiers of BLE failure exist, and only two currently
  self-heal**: a short stall (watchdog catches it, ~2min), a
  short-lived BlueZ "already in progress" state (retry logic catches
  it, ~25s) - and a *deeper* stuck state that has, at least once,
  required a full Pi reboot when `systemctl restart bluetooth` alone
  didn't clear it. An automatic reboot-escalation for that third tier
  was deliberately **not** built yet, pending more data on how often
  it actually recurs - see open threads below.

### Update system
- `config.json` is **not git-tracked** (removed deliberately after
  repeated real merge conflicts between the dashboard's live rewrites
  and whatever the committed default happened to be). A fresh clone
  has none; `load_config()` auto-creates it from `DEFAULT_CONFIG`.
  **Any future rename of a config key needs an explicit migration
  inside `load_config()`** (see the `sensorpush_mac` → `ble_mac`
  migration for the pattern) - otherwise upgrading silently resets
  that setting to its default, which happened for real once before
  the migration was added.
- `auto_update.sh` restarts `dermestid-web.service` **last**,
  deliberately. When triggered via the dashboard button, the script
  runs as a child of that same service - systemd's default
  `KillMode=control-group` kills the *entire* cgroup (including the
  detached script) the instant the service is told to restart, not
  just the tracked main process. Everything else has to happen first,
  or it silently never runs.
- A shared lockfile (`.git_update.lock` + `flock`) keeps the
  background update-checker and a manual `auto_update.sh` run from
  colliding on the same git ref at the same time - hit this for real
  once ("cannot lock ref ... is at X but expected Y").
- The sudo preflight check tests the **actual** restart commands
  directly (`sudo -n systemctl restart ...`, checking the real exit
  code) rather than a separate probe command. Two earlier attempts
  (`sudo -n true`, then `sudo -n -l | grep`) both turned out to test
  something subtly different from what actually mattered and gave
  false negatives in practice - this was the version that finally
  matched reality.
- Branch selection (`update_branch` config key) lets the dashboard
  switch which branch `auto_update.sh` tracks - it correctly does a
  `git checkout -B`, not a `git pull`, when the target differs from
  what's checked out, with the same stash-safety either way. **This
  only handles git-level file changes.** It cannot automatically
  apply a structural migration a branch might need (a renamed systemd
  service, a new required config key) - those still need to be done by
  hand, same as the BLE rename itself needed.

### Camera / timelapse (added on `add-webcam`, 2026-09-12)
- **Separate systemd service (`camera_service.py`), not code inside
  `webapp.py`** - deliberately mirrors the BLE listener's architecture
  rather than having Flask open the device itself. Two concrete reasons,
  not just "for consistency": (1) most USB UVC webcams only accept one
  open client at a time - if each dashboard viewer's request tried to
  open the device itself, a second simultaneous viewer would just break;
  a single background process owning the device and writing a shared
  `camera/latest.jpg` file sidesteps that entirely. (2) the timelapse
  has to keep capturing on schedule independent of the web process's own
  lifecycle, which restarts far more often (every applied update, per
  `auto_update.sh`) than a capture loop should be interrupted.
- **[superseded, 2026-09-13]** Live view started as a periodically-
  overwritten still (`camera/latest.jpg`) that the dashboard just polled
  on a timer, deliberately not real video, to sidestep the multi-viewer
  device-contention problem below. Once actually seen on real hardware,
  a stop-motion "live" view wasn't good enough - real video was
  explicitly requested. Solved without touching the device-contention
  reasoning at all: `camera_service.py` still writes the same single
  `camera/latest.jpg`, now just much faster
  (`live_capture_interval_seconds` default dropped from 2s to 0.2s,
  bounds loosened from whole seconds to as low as 0.1s), and
  `webapp.py` added `/api/camera/stream.mjpg`, which re-reads that same
  file on its own short timer (`STREAM_RELAY_INTERVAL`, decoupled from
  the capture rate) and relays it as a `multipart/x-mixed-replace`
  stream - a plain `<img>` tag renders that as continuous video natively,
  no player/codec/JS polling loop needed. Still exactly one process ever
  opens the USB device; any number of browser tabs just get their own
  relay of the same file. The one non-obvious catch this required:
  `app.run()` needed `threaded=True` added, since a stream holds its
  HTTP connection open indefinitely and Werkzeug's dev-server default is
  one request at a time - without it, one open camera tab would have
  silently frozen every other page on the dashboard. Real tradeoff that
  remains: frame rate is a shared-CPU-with-`climate.py` budget on a Pi
  3B+, so it's a config setting to tune per-hardware, not a bigger
  default baked in - see README's Camera section.
- **Timelapse interval is per-mode** (`snapshot_interval_minutes` inside
  each mode's block in `config.json`, alongside its setpoints), not a
  single global setting - explicit request, since a mode you barely
  visit (Dormant) plausibly warrants a different cadence than one you're
  actively watching. `0` is the sentinel for "never" and is exempt from
  the normal minutes bounds check.
- **One global `last_snapshot_time`, not one per mode** - deliberately
  NOT reset on a mode switch. Switching from a long-interval mode to a
  short-interval one doesn't itself fire an immediate snapshot just
  because the mode changed; the newly-active interval simply starts
  being measured against whenever the last snapshot actually happened,
  regardless of which mode was active then. Avoids a snapshot burst
  every time someone flips modes on the dashboard.
- **Timelapse frames are meant to be kept, not rotated on a schedule** -
  there's deliberately no "keep N days" setting. The only thing that
  deletes old snapshots is a hardcoded disk-space safety net
  (`CAMERA_LOW_DISK_THRESHOLD_MB` = 200MB free, in `camera_service.py`):
  below that threshold it prunes the oldest snapshots and logs a warning
  once (not every cycle, same as the sensor-failover logging pattern
  above). This exists because a full SD card would take down the whole
  Pi - including climate control, the actually safety-critical part of
  this project - not because timelapses are meant to be short-lived.
  Same "hardcoded guardrail, not a UI setting" treatment as climate.py's
  own runtime cutoffs - see "Tuning knobs that stay hardcoded" in
  README.md.
- Snapshot metadata lives in a new `camera_snapshots` SQLite table (ts
  PRIMARY KEY, mode, filename), matching the whole-second integer `ts`
  used as the actual JPEG's filename too - so a snapshot's DB row and
  its file on disk can never disagree about which one it is, and the
  `/api/camera/snapshot/<int:ts>.jpg` route can look a row up directly
  from the integer in the URL with no separate mapping.
- **Hardware verification status** - see the dated webcam entry under
  "Open threads" below for current state; this has moved past "no real
  hardware available" and into real-Pi testing as of 2026-09-13.
- **[added, 2026-09-13] Timelapse now compiles real `.mp4` video files,
  not just a renamed still-frame gallery** - explicit request, and
  explicitly the "compile real video files" option over a
  frame-gallery-with-a-new-name alternative that was also offered.
  `camera_service.py` tracks the current mode-session's start time in
  memory (`session_start_ts`); on every mode change it hands the just-
  ended session's frame range to `maybe_compile_session()`, which
  runs `ffmpeg` (concat demuxer, fixed `TIMELAPSE_VIDEO_FPS`=12, CFR via
  `-r` only - **do not add `-vsync vfr` alongside `-r`, ffmpeg 6.1.1+
  rejects that combination as contradictory, caught by a real smoke
  test**) in a background thread so a multi-second encode never blocks
  the live-capture loop, guarded by a `_compiling_modes` set +
  `threading.Lock()` against two overlapping compiles for the same
  mode. Requires at least `MINIMUM_FRAMES_FOR_VIDEO` (3) frames or the
  session is skipped entirely (too short to bother). If `ffmpeg` isn't
  installed, compiling is skipped with a logged warning, not a crash -
  see README's Camera section for the `apt install ffmpeg` step.
- **Zero persisted session-boundary state, by design.** Nothing records
  "session started at time T" to disk - `camera_service.py` recovers it
  on every startup (including after a restart mid-session) via
  `get_earliest_camera_snapshot_ts(mode)`, which relies on a database
  invariant: `camera_snapshots` always holds exactly the CURRENT,
  not-yet-compiled session's frames for a given mode, because a
  successful compile deletes the frames it just consumed
  (`delete_camera_snapshots`). This is why frame deletion-after-compile
  isn't just disk-space hygiene - it's load-bearing for session-boundary
  recovery across a restart. Don't add code that leaves compiled frames
  in `camera_snapshots` "just in case" without also updating this
  recovery logic.
- **`timelapse_videos` uses an autoincrement integer `id` primary key,
  not `ts REAL PRIMARY KEY`** - the first version used `ts` (with
  `INSERT OR REPLACE`) to match `camera_snapshots`' convention, but a
  real smoke test caught a same-second collision: two different modes'
  background compile threads finishing within the same wall-clock
  second silently overwrote one row with the other. Every route/helper
  keys off `id`, not `ts`, as a result (`/api/timelapse/video/<id>`,
  etc.) - don't "simplify" this back to `ts` without re-introducing that
  bug.
- **Page reorg (2026-09-13, explicit request):** the live MJPEG stream
  moved from its own page onto the Home page (between the sensor tiles
  and the History chart) - it's the actual point of the camera feature,
  wanted at a glance without a page navigation. What used to be the
  Camera page is renamed **Timelapse** and now shows only the compiled-
  video gallery (poster thumbnails, per-video download/delete, checkbox
  multi-select for bulk download/delete) - it no longer shows a live
  feed or raw uncompiled frames at all. The Config page's "Camera" and
  "Software updates" sections were merged into one side-by-side
  `Camera & software updates` section (explicit request: "place the
  software update tile next to the camera time[sic - tile]").
- **Discovery buttons on the Config page (2026-09-13, explicit
  request)** - "a clean display of the discover_*.py output without
  having to use the cli," for both the external BLE sensor and the
  camera:
  - **BLE**: `/api/ble-discover` is deliberately read-only against
    `ble_listener.py`'s own already-saved readings
    (`get_all_ble_readings()`), NOT a second `BleakScanner` scan. Two
    independent scanners hitting the same BlueZ adapter concurrently is
    exactly the kind of start/stop churn that caused the Tier-3
    stuck-`bluetoothd` incident documented below - this was a
    deliberate design constraint, not an oversight, and any future
    change to this endpoint needs to preserve "never starts its own
    scan."
  - **Camera**: `/api/camera-discover` shells out to
    `discover_camera.py --json` (factored into a shared `probe_devices()`
    function so the CLI and the web route can't drift apart) rather than
    importing `cv2` into `webapp.py` directly - `webapp.py` has
    deliberately stayed `cv2`-free, so a broken/missing opencv install
    can only break the camera-specific feature, never the whole
    dashboard (this property already mattered once this session: the
    missing-Camera-nav-link bug was initially suspected to be a `cv2`
    problem before the real cause - stale duplicate templates - was
    found). It also stops `dermestid-camera.service` before probing and
    restarts it after (in a `finally`, so a failed or timed-out probe
    still restarts the service) - most UVC webcams only allow one open
    client, so without this the service's own in-use index would never
    show up in the scan. Needs two new `sudoers` lines
    (`stop`/`start dermestid-camera.service`, alongside the existing
    `restart` line) - see README's sudoers section; without them,
    discovery still runs, it just won't see whichever index the service
    already has open (a warning `hint` field in the response says so).
- **Discovery/config UX polish (2026-09-13, real-hardware follow-up
  after BLE discovery was confirmed working on the Pi 4)**:
  - **Config-page saves now auto-restart the affected service when the
    change actually needs it, instead of just telling the user to do it
    by hand.** A shared `_restart_service()` helper (`sudo -n systemctl
    restart <unit>`, best-effort) is called from `/api/ble-mac` only
    when `ble_sensor_type` (the brand) changed, and from
    `/api/camera-settings` only when `device`/`width`/`height` changed
    - deliberately NOT for every save. `ble_mac` itself needs no
    restart at all (confirmed by reading the actual code:
    `ble_listener.py` never references it - only `climate.py` and the
    dashboard do, both reading `config.json` fresh every
    cycle/request), and `jpeg_quality`/`live_capture_interval_seconds`
    are already re-read every camera capture cycle - restarting for
    either would just be a pointless live-view interruption for a
    change that was going to apply itself within a second or two
    anyway. Both routes report `restart_attempted`/`restart_ok` in
    their JSON response so the UI can tell "saved, restarted
    automatically" apart from "saved, but the restart failed - do it by
    hand" (e.g. sudoers not set up yet) rather than silently claiming
    success either way.
  - **`discover_camera.py` now explicitly requests 1920x1080
    (`cap.set(CAP_PROP_FRAME_WIDTH/HEIGHT, ...)`) before reading a
    frame back**, instead of reading whatever OpenCV/V4L2's default
    negotiated resolution happens to be. Real bug this fixes: a camera
    capable of 1920x1080 was being reported as 640x480 by Discover,
    because that's V4L2's common UVC default when nothing explicitly
    requests otherwise - not a capability limit, just an unrequested
    default. What comes back after the explicit request is the
    camera's actual negotiated capability (V4L2 clamps to the nearest
    it supports if 1920x1080 itself isn't available).
  - **Camera Discover's resolution reading now has an actual purpose
    beyond display**: clicking a discovered thumbnail pre-fills the
    Width/Height config fields from it, not just the device index
    (`lastDiscoveredCameras` keyed by index, read in
    `selectCameraIndex()`). This was a direct response to the question
    "if it knows the resolution what purpose is there for the
    resolution fields" - the fields themselves still matter
    independently of discovery (they're the live-capture performance
    knob, see the Pi 3B+ CPU-sharing tradeoff elsewhere in this doc),
    but there was no reason to make the user look up and retype a
    number the probe had already found. Same treatment given to BLE
    Discover's rows for UI parity (`.selected` highlight via
    `selectBleAddress()`) - previously only the Camera grid highlighted
    a selection.
- **Manual light override (2026-09-13, explicit request)**: a 💡 icon
  overlaid on the Home page's live view now turns on the door/lid light
  (`PIN_LIGHT`) on demand, described by the user as something that was
  "supposed to" already exist. Design: `climate.py`'s `light_loop()`
  thread (already polling the reed switch, `PIN_SWITCH`, every 50ms) was
  extended to also honor `config.json`'s new `light_override_until` -
  an epoch timestamp, not a plain bool, so the override **self-expires**
  (`LIGHT_OVERRIDE_DURATION_SECONDS` = 300) rather than needing anything
  to actively turn it back off; a forgotten click or closed tab can't
  leave the enclosure lit indefinitely. `light_loop()` only re-reads
  config once a second (`LIGHT_OVERRIDE_POLL_SECONDS`), not on every
  50ms door-switch poll - the switch itself needs that responsiveness,
  the override doesn't. **The switch always wins**: the light-on
  condition is `is_open OR override_active` - this button can only ever
  ADD light-on time on top of the switch, never suppress it, so there's
  no way for a stuck override to mask the switch actually opening (or
  vice versa). New route `/api/light-override` (POST, `{on: true/false}`)
  just writes the config value; unlike the camera/BLE-service routes,
  this needed no new `sudoers` entry, since `climate.py` (the process
  that owns `PIN_LIGHT`) reads `config.json` directly rather than this
  route shelling out to `systemctl`. Verified via Flask-test-client
  smoke tests (override timing math, persistence, clearing, `/api/status`
  surfacing it) and a standalone replica of `light_loop()`'s on/off
  decision logic (climate.py itself can't be imported/run in a sandbox
  without real `RPi.GPIO` hardware) - **not yet run on the actual Pi**.

### Notification system (added on `notification-system`, 2026-09-17)

Outbound alerting - Pushover / email (SMTP) / generic webhook - for warning-or-above `log_event()` calls, plus
one genuinely new alert type (door left open too long) that didn't exist as a logged event before this.
Config-page card, README section, and full design notes are covered there - this entry is about the choices
that aren't obvious from reading the code cold.

- **Lives in `shared_state.py`, not a new module.** `notifications.py` was the initial plan (mirroring how
  `camera_service.py`/`ble_listener.py`/`ble_battery.py` are separate concern-specific scripts), but the
  dispatch logic is tightly bound to `log_event()` and `config.json` - both already owned by `shared_state.py`
  - and a separate module would need `shared_state.py` to import it back (for the config/log_event hooks),
  creating a circular import for no real benefit. Centralizing it here instead matches this file's existing
  "shared config + logging helpers used by every other script" role.
- **`log_event()` is the single choke point**, not something threaded through every caller's own logic. Every
  script already calls `log_event(level, message)` for anything alarm-worthy; adding an optional `category`
  kwarg (only needed for the specific call sites that get a per-category mute toggle - see `EVENT_CATEGORIES`)
  and firing the notification check from inside `log_event()` itself meant zero new call sites anywhere, just
  one new optional argument at existing ones.
- **Dispatch never runs on the caller's thread.** `climate.py`'s control loop has a fixed `LOOP_INTERVAL`
  cadence that sensor timing and relay safety cutoffs both depend on - a slow/unreachable Pushover API or SMTP
  handshake stalling that loop would turn a notification problem into a climate-control problem. A lazily-
  started background thread + `queue.Queue` (`_ensure_notify_thread`/`_notify_worker` in `shared_state.py`)
  absorbs this; `log_event()` only ever enqueues. Lazy start (not at import time) so one-off helper scripts
  that import `shared_state.py` but rarely/never call `log_event()` (`discover_ble_sensor.py`,
  `discover_camera.py`) don't gain a permanent background thread just for importing the module.
- **Per-category cooldown state is in-memory only, not persisted.** A `climate.py` restart clears it, which
  means a restart right after a real alert can re-notify immediately even within the configured cooldown
  window. **This is intentional, not a gap** - a process that just restarted (possibly *because of* whatever
  tripped the alert) is exactly when you most want to know if the condition is still true, not when a stale
  cooldown from before the restart should stay silent about it.
- **A notification failure must never call `log_event()`.** Delivery failures (a bad Pushover token, SMTP auth
  failure, unreachable webhook URL) are logged via the plain `logging` module only. Routing them through
  `log_event()` would make a failed notification itself notification-worthy by the same rule that produced it
  - an infinite-recursion trap disguised as "just log everything consistently."
- **The email password is never round-tripped to the browser.** `/api/status` returns the saved config
  wholesale (same as everything else on the Config page - see the "no login" note in the README, which now
  explicitly covers this), but `api_set_notification_settings()` in `webapp.py` strips `smtp_password` out of
  its own save-response, and the Config page's password field is always left blank on load rather than
  pre-filled - typing nothing and saving keeps whatever's already stored, on the same "empty means unchanged,
  not cleared" convention used for the email password specifically (every OTHER notification field, including
  the Pushover token/user key, round-trips normally the same as BLE/camera settings already do - only the SMTP
  password gets this treatment, since it's the one credential this project stores that isn't already
  effectively public on the LAN via some other already-visible field).
- **Categories cover the call sites that were already warning/error/critical before this existed**, plus
  `door_open_timeout` (new). `EVENT_CATEGORIES` in `shared_state.py` is the single source of truth the Config
  page's checklist renders from (`/api/notification-categories`) - adding a new categorized alert later means
  adding one entry there and passing `category=` at that one `log_event()` call site, nothing else.
- **Not yet tested against a real Pushover account, real SMTP server, or real webhook receiver** - no
  credentials were available while building this. The "Send test" button exists specifically so this gets a
  real end-to-end check the first time real credentials are entered, rather than only finding out a channel is
  broken the next time something actually goes wrong. Verify all three (or whichever you configure) with Send
  test before relying on any of it.

## Established workflows / things that look like bugs but aren't

- Deployment is via dragging files into GitHub's web UI, not git CLI
  pushes from a dev machine. **Always check the branch selector shows
  the intended branch before dragging files in** - it's easy to
  accidentally commit to whatever branch GitHub happened to have
  selected.
- `auto_update.sh`'s own executable permission bit (`chmod +x`) shows
  up as a "local change" on essentially every pull or branch switch,
  since GitHub's web uploader doesn't preserve the exec bit. This is
  expected and harmless - the stash/restore cycle handles it silently
  most of the time; don't chase it as a real issue if it appears in a
  "local changes detected" message.
- The `update-dermestid` shell alias just does `cd ~/dermestid &&
  ./auto_update.sh` - if it's "not found," it's almost always either a
  fresh shell that hasn't sourced `.bashrc` yet, or the word order
  typo (`dermestid-update` instead of `update-dermestid`).
- Testing a branch: either use the dashboard's branch dropdown now
  that it exists, or manually `git fetch origin && git checkout
  <branch>` and restart services by hand - **don't** use
  `update-dermestid` while manually testing a branch that isn't yet
  what the dashboard's `update_branch` config points to, since the two
  can disagree about which branch is "current."

## Open threads / known issues

- **[2026-09-17]** `readings.camera_actual_fps`/`camera_target_fps` (unused since the 2026-09-14
  camera-streamer switch, previously left in the schema unwritten - see the two dated `PROJECT_STATUS.md`
  entries further down from when they were added) are now actually DROPPED, not just unpopulated: gone from
  `log_reading()`'s signature, `get_readings_table()`'s SELECT, and the Data page's "Cam FPS"/"Cam target"
  columns. `_migrate_readings_columns()` in shared_state.py drops them from any existing DB on the next
  `init_db()` (the Pi's real DB has them - old rows' OTHER columns are untouched, only the two dead ones go
  away) - needs SQLite 3.35+, which any current Raspberry Pi OS has; wrapped in try/except so an unexpectedly
  old sqlite3 just leaves the (already-dead) columns in place and logs why instead of crashing the service on
  startup. Verified against a simulated pre-migration DB (existing row survived, columns gone, idempotent on
  a second `init_db()` call) - not yet run against the actual Pi's real `dermestid.db`, so the DROP COLUMN
  step there specifically is still worth confirming once this branch updates it (`dermestid-web.service`'s
  restart runs `init_db()` on the very first request after startup).
- **[resolved, 2026-09-17]** Notification system (`notification-system` branch, pushed to GitHub and running
  on the Pi) - **all three channels confirmed working via their per-channel "Send test" buttons**: webhook
  (Discord, after fixing two real bugs the first live test surfaced - Discord's Cloudflare front door blocks
  Python's default `User-Agent` outright (HTTP 403, error 1010) before the request reaches Discord's own
  webhook handler, and the original generic JSON payload had no `content` field, which Discord requires to
  actually display anything - both fixed in `_send_webhook()`, see shared_state.py); Pushover; and email (the
  only snag was Google itself - a plain Gmail password is rejected by SMTP with "Application-specific password
  required" (534, 5.7.9) once 2-Step Verification is on, which it is for most accounts now - fixed by
  generating a Gmail App Password at myaccount.google.com/apppasswords and using that instead of the real
  account password in the SMTP password field; not a code bug, no code change needed). Still worth confirming
  a real triggering event (not just the test button) produces a push, e.g. leaving the door open past
  `door_open_alert_minutes`.
- **[2026-09-17]** Config page split into **Config** (per-mode setpoints + the Notifications card) and a new
  **Settings** page (sensor sources/calibration, USB webcam, software-update branch/interval/apply) - same
  underlying `/api/*` endpoints, just regrouped across two `<nav>` tabs (see base.html) so the page people
  tune often (setpoints, notification thresholds) isn't buried under the stuff set up once and rarely touched.
  The global update-available banner in base.html now links to `/settings` (where "Update now" now lives)
  instead of `/config`. Not yet confirmed on the Pi as of this entry - purely a template/route reorg, no
  backend or schema change, so low risk, but worth a quick look that both tabs render and every button on
  each still works after the update.
- **[resolved, 2026-09-18]** Real-world Pushover test after merging `notification-system` into `main`
  surfaced an actual bug: Pushover's own API rejected the saved token ("application token is invalid") -
  root cause was user error (Pushover's User Key and API Token are two separate values from two different
  places on pushover.net, easy to swap or leave stale), not a code bug, but it exposed a real UX gap - the
  Pushover/Email/Webhook cards only ever had "Send test" buttons; the only way to actually SAVE credentials
  was the General card's Save button further up the Notifications section, easy to miss after just editing
  one channel's card (confirmed to have actually bitten Ryan - re-typed credentials after "losing" the first
  set, though the deeper cause there was Pushover rejecting an invalid token rather than a straight save
  failure). Fixed by giving each channel card its own "Save" button (`saveNotificationSettings(msgId)` now
  takes an optional message-element id so each card can show its own result) - it still saves the SAME full
  notifications block as before (the backend only ever accepts the whole shape, see
  api_set_notification_settings in webapp.py), just reachable and confirmable from whichever card you were
  actually editing.
- **[2026-09-18]** Auto-save on the Settings page's branch-to-track select (`onBranchSelectManualChange()`),
  matching the Internal source select's existing "pick it, it's saved" pattern - Ryan's idea, from the same
  Pushover-credentials conversation, that a plain dropdown (no risk of half-typed data) shouldn't need a
  separate Save click. Deliberately NOT applied to the notification credential fields (Pushover token, email
  password, etc.) or plain number inputs - those benefit from an explicit, deliberate Save. The "Save branch"
  button is kept as a manual fallback (retry after a failed auto-save, or re-check without changing the
  selection), not removed.
- **[2026-09-18]** Extended the same auto-save treatment to the Config page's Notifications General card -
  Ryan asked for it specifically for "enabling/disabling specific alerts." Now auto-save on change: the
  master "Enable notifications" checkbox, the "Minimum severity" select, every per-category checkbox (the
  actual "specific alerts" he meant), "Quiet hours" enabled, and "Still notify for critical events during
  quiet hours." Still requires the explicit Save button: cooldown minutes, door-open-alert minutes, and the
  quiet-hours start/end times - all free-typed values where a save-on-every-keystroke would commit a
  half-typed number (e.g. saving "9" on the way to typing "90"). Implementation note: since the backend only
  ever accepts the WHOLE notifications block in one request (same as the per-channel Save buttons above),
  every auto-save from a checkbox/select also re-saves whatever's currently sitting in those manual number/time
  fields at that moment - harmless if they're untouched (they already match what's saved), but worth knowing:
  toggling a category checkbox while you have a half-typed cooldown value WOULD commit that half-typed value
  too. Not worth solving with per-field save endpoints for what's a rare edge case on a single-user LAN
  dashboard - just documenting the actual behavior here.
- **[deferred, 2026-09-18]** iOS home-screen badge (a number on the dashboard's icon, like a native app) for
  notifications - discussed, not started. Technically possible via the Badging API (Safari 16.4+ supports it
  for a site Added to Home Screen), but a badge that updates while the app is closed needs real Web Push - a
  service worker, VAPID keys, a push subscription stored server-side, and the push handler calling
  `setAppBadge()`. Service workers refuse to register over plain HTTP except on `localhost`, and this dashboard
  is plain Flask over HTTP on the Pi's LAN IP (no login, no TLS - see README), so this is blocked on the Pi
  getting real HTTPS first. Ryan's plan is to revisit once Traefik + Authelia are set up (would give it a
  proper hostname + cert + auth in front). Pushover already covers "notice it on my phone" in the meantime -
  this would just be a nicer-feeling badge on top of that, not a new capability.
- **[2026-09-17]** Internal-sensor-outage rows now written to the readings table (see the internal-sensor
  Load-bearing decision above for the "why") - worth flagging one side effect on the Home page: the page-level
  "stale" banner (`data.stale`, >90s since the latest row) used to also catch an internal-sensor outage,
  since no row got written at all while one was ongoing. Now that a row IS written every cycle during an
  outage (with `internal_temp`/`internal_humidity` null), `latest.ts` stays fresh and that banner no longer
  fires for this specific case - the Internal tile going to "--°F"/"--%RH" immediately (next cycle, not after
  90s) is the replacement signal, which is arguably more specific/useful, but it's a real behavior change from
  before, not just a Data-page cosmetic fix. The banner still fires correctly if the whole control loop is
  actually dead (climate.py crashed/hung) - that path never wrote a row before either and still doesn't.
- **[resolved]** `ble-genericization` merged into `main`; Pi confirmed
  switched back to tracking `main`.
- **[open]** A Data-page report of "Fallback missing for a while" came
  in showing old "External"/"Fallback" column headers from *before*
  the fixed-identity rename - most likely just a stale
  `dermestid-web.service` that hadn't been restarted to pick up the
  new template (Flask caches templates in production mode), not a new
  bug. No further reports since, but never explicitly re-confirmed
  fixed - worth a fresh look if it recurs.
- **[open]** Tier-3 BLE failure (deep bluetoothd stuck state,
  `systemctl restart bluetooth` insufficient, needs a full reboot) -
  happened twice this session, then a third time (2026-09-13) on real
  hardware right after the USB webcam was first plugged in and opened.
  `vcgencmd get_throttled` showed `0x50000` (under-voltage + ARM freq
  capping *have occurred* since boot, sticky bits - not necessarily
  active at the moment checked), and `dmesg` showed only the usual
  stuck-scanning BLE errors, no camera/USB errors. Circumstantial but
  plausible: the webcam's current draw caused a brief under-voltage
  event that also knocked the BT/Wi-Fi combo chip into this same stuck
  state (shared power rail). A reboot fixed it again. No automatic
  recovery built yet; if this keeps coinciding with the webcam
  specifically, a powered USB hub for the webcam is the likely real
  fix rather than another BLE-side workaround.
- **[open, 2026-09-13, worse recurrence]** A fourth occurrence, and the
  first that didn't self-clear: `dermestid-ble.service` was reported
  missing external BLE data for ~12 hours straight, confirmed via
  `journalctl` as a continuous restart loop (`restart counter` in the
  hundreds - 267, 268... - by the time it was checked) - the
  `WATCHDOG_TIMEOUT` (2min) "no reading decoded" trip firing and
  restarting the service every single cycle with **zero** successful
  reads the entire time, not the occasional stall-then-recover pattern
  the watchdog was designed around. `vcgencmd get_throttled` again
  showed `0x50000` (under-voltage/throttling occurred since boot,
  sticky). Same conditions as before: still the Pi 3B+, webcam plugged
  in and actively being tested (see the live-view performance entry
  below). This is the strongest data point yet that this is a real,
  recurring hardware limitation of running the webcam off the 3B+'s
  power budget, not a one-off - the "wait for more data before building
  auto-recovery" threshold from the original entry above arguably has
  been met now. **Decision (2026-09-13): explicitly holding off on
  Tier-3 auto-reboot logic until after the Pi 4 migration** - if the
  migration's better power delivery makes this failure mode disappear
  on its own (the leading theory), building auto-reboot logic now would
  be wasted, and possibly risky, work. Revisit only if this recurs on
  the Pi 4 too. **`sudo systemctl restart bluetooth` alone confirmed
  insufficient for this occurrence** (2026-09-13) - tried first as the
  lighter-weight step; BLE still failed afterward, now with
  `RuntimeError: Could not start BLE scan after 5 attempts` and
  `[org.bluez.Error.InProgress] Operation already in progress` on every
  attempt. Matches this project's very first Tier-3 note exactly
  (`systemctl restart bluetooth` insufficient, needs a full reboot).
  **A full `sudo reboot` was then tried and, for the first time ever on
  this project, did NOT clear it either** - immediately after boot
  (low PIDs, ~1000s, confirming a genuinely fresh boot), `ble_listener`
  still hit the identical `[org.bluez.Error.InProgress] Operation
  already in progress` on its very first scan attempt. Every prior
  Tier-3 incident was cleared by a reboot; this is a new, worse tier.
  **Leading new hypothesis**: `dermestid-sensorpush.service` - the old,
  pre-genericization service that "Current state" above already notes
  *should* have been stopped/disabled/removed on the Pi, but was never
  actually confirmed done - is still enabled and starting at boot
  alongside `dermestid-ble.service`. Two independent processes both
  trying to open a BLE scan on the same adapter at boot would produce
  exactly this symptom (immediate, permanent "already in progress" on
  every attempt by either one, surviving a reboot because *both* come
  back up every time). This would fit the pattern already established
  twice this session (the battery service's stale `ExecStart`, the
  stale root-level unit-file duplicates) of pre-rename artifacts never
  being fully cleaned up on the Pi. Not yet confirmed - check with
  `systemctl list-units --all --type=service | grep dermestid` and
  `systemctl status dermestid-sensorpush.service`; if it's
  loaded/enabled, `sudo systemctl stop dermestid-sensorpush.service &&
  sudo systemctl disable dermestid-sensorpush.service` and reboot again
  to test.
  **Both leading theories now ruled out post-migration (2026-09-13):**
  `dermestid-sensorpush.service` genuinely doesn't exist on this Pi
  ("could not be found") - not a duplicate-service conflict. And
  `vcgencmd get_throttled` reads a clean `0x0` on the Pi 4 with its own
  proper PSU - not a power problem either, current or historical. **The
  error changed completely after the migration**: no longer
  `[org.bluez.Error.InProgress]` (the old stuck-scan symptom) - now
  `BleakBluetoothNotAvailableReason.POWERED_OFF: No powered Bluetooth
  adapters found`, meaning BlueZ itself thinks the radio is off. This
  reads as a straightforward Bluetooth-adapter-availability problem
  specific to the new hardware (an `rfkill` soft-block, or
  `bluetooth.service` not actually coming up cleanly after the SD-card
  swap), not a continuation of the old Tier-3 stuck-scan issue at all -
  treat this as a new, unrelated symptom rather than "the same bug
  persisting." `dermestid-ble.service` already has
  `After=bluetooth.target`/`Wants=bluetooth.target`, so this isn't a
  simple boot-ordering race either (11+ restarts in, well past any
  first-boot timing issue). Next diagnostic step given to the user:
  `rfkill list`, `systemctl status bluetooth --no-pager`, `hciconfig
  -a` - not yet confirmed which.
  **Separately, and initially misdiagnosed, in the same
  session**: `dermestid-battery.service`'s log showed `"No SensorPush
  address configured - nothing to check"`, first assumed to mean
  `config.json`'s `ble_mac` was genuinely empty - wrong. **The real
  cause: that exact log string only exists in `sensorpush_battery.py`
  (the pre-genericization script, still present in the repo but fully
  superseded), which checks the legacy `sensorpush_mac` config key -
  not `ble_battery.py`'s current `"No BLE sensor address configured"`
  wording checking `ble_mac`.** Confirmed by the user that the Config
  page *does* show an address (`ble_mac` is populated) - so the battery
  check that actually ran on the Pi was the old script, not the new
  one. Since installing a systemd unit (`sudo cp .../*.service
  /etc/systemd/system/` + `daemon-reload`) is a one-time manual step,
  not something `git pull`/`auto_update.sh` ever touches, the most
  likely explanation is the Pi's installed
  `/etc/systemd/system/dermestid-battery.service` still has its
  `ExecStart` pointing at the old `sensorpush_battery.py` from before
  the BLE-genericization rename, and was simply never re-installed
  after `ble_battery.py` replaced it. **Confirmed on the actual Pi
  (2026-09-13)**: `ExecStart=/usr/bin/python3
  /home/pi/dermestid/sensorpush_battery.py` - exactly as guessed. Fix
  given to the user (re-copy `systemd/dermestid-battery.service` to
  `/etc/systemd/system/`, `daemon-reload`, restart the timer) - not yet
  confirmed applied. This is the same category of gap the Update-system
  section above already documents ("structural changes... need manual
  steps"), just newly observed for a script rename rather than a
  service rename. **General takeaway worth remembering**: any future
  rename of a script a systemd unit's `ExecStart` points at needs a
  note in that feature's own docs to re-run the install step - the same
  way `load_config()` migrations get a comment pointing at the
  `sensorpush_mac` → `ble_mac` pattern for config keys.
  **Also found and fixed while investigating**: a second, separate
  instance of the stale-root-level-duplicate-file bug (see the
  `[resolved, 2026-09-13]` template-duplicates entry above) -
  `dermestid-battery.service`, `dermestid-battery.timer`,
  `dermestid-autoupdate.service`, `dermestid-autoupdate.timer`,
  `dermestid-ble.service`, `dermestid-climate.service`, and
  `dermestid-web.service` all had byte-identical stale copies sitting
  at the repo root (same drag-and-drop-to-GitHub's-web-UI cause as the
  template incident), separate from the real ones in `systemd/` that
  the README's install commands actually reference. Confirmed
  byte-identical before deleting, so this wasn't the cause of the
  battery bug above - just the same latent clutter/confusion risk,
  removed proactively. Deleted from the repo; **still needs manual
  deletion from the user's local folder and, once pulled, from the
  Pi's working copy** (the device bridge that pushes files to the
  user's folder can't delete remotely, same limitation as the earlier
  template cleanup).
- **[open, 2026-09-13]** Live view performance on the Pi 3B+ confirmed
  poor with real hardware + real network (Chrome on desktop, same LAN):
  "very slow and choppy," not the smooth video the MJPEG relay was
  designed to deliver. Not yet root-caused to a specific bottleneck
  (webcam capture rate, JPEG encode time, or Flask/Werkzeug relay
  overhead specifically) - CPU contention with `climate.py` on a Pi 3B+
  while also feeding the same webcam's power draw into the Tier-3 BLE
  issue above is the leading theory, consistent with the tradeoff
  already flagged in README's Camera section. Real fix is expected to
  be the pending Pi 4 migration (more CPU/power headroom); not worth
  chasing a 3B+-specific optimization (lower resolution/quality/fps as
  a stopgap) given the migration is already imminent.
- **[open]** SHT31 upgrade for the internal sensor is under
  consideration, motivated by real DHT22 reliability issues even after
  the retry-logic fix. Probe form factor (PTFE vs. ceramic vs. metal
  mesh filter cap) was researched but no purchase decision made yet.
- **[open]** `xiaomi`/`ruuvitag` entries in `BLE_SENSOR_LIBRARIES`
  have unverified exact class names (follow the established
  convention but weren't checked against real source) - confirm before
  actually switching to either.
- **[resolved, 2026-09-12]** `config.json` had drifted back into git
  tracking with no `.gitignore` entry, contradicting this file's own
  claim that it wasn't tracked - see the dated correction note under
  "Current state" above. Re-fixed; watch for it recurring via the
  drag-and-drop upload workflow.
- **[open, 2026-09-13]** New USB webcam feature (`add-webcam` branch)
  now partially verified on real hardware (Logitech C922, Pi 3B+):
  `discover_camera.py` found it at `/dev/video0` fine, `pip install
  opencv-python-headless --break-system-packages` was needed (not
  preinstalled), and `camera_service.py` ran and wrote `latest.jpg`
  successfully in a first foreground test. The missing Camera nav link
  (separate issue, now resolved - see the stale-duplicate-template entry
  above) is no longer blocking. Still open: the service died silently
  once with no error/shutdown logged, suspected SIGHUP from the SSH
  session dropping (unlike `climate.py`, it has no signal handler) -
  needs to run as the actual `dermestid-camera.service` systemd unit
  instead of foreground testing, not yet done. Still
  unverified: real resolution/quality behavior and whether the
  watchdog/reopen timing constants (60s stall timeout, reopen after 10
  consecutive failures) feel right against a real webcam's actual
  failure patterns. Also now includes real MJPEG video streaming (see
  the superseded live-view decision above, and the still-image-only
  entry in `dermestid-camera.service`'s open questions) - pushed but
  NOT yet verified on the Pi: whether ~5fps at 1280x720 is actually
  smooth in a browser over the LAN, and whether it costs `climate.py`
  any real CPU/temperature headroom on a Pi 3B+. Check both once
  deployed; back off `live_capture_interval_seconds`, resolution, or
  quality on the Config page if the Pi runs hot or climate control's
  own timing gets sloppy.
- **[resolved, 2026-09-13]** The Camera nav link never appeared in the UI
  after the webcam feature shipped, even after service restarts and a
  full reboot - root cause was a stale, unused **duplicate set of
  template files sitting at the repo root** (`base.html`, `config.html`,
  `data.html`, `home.html`, `logs.html`), separate from the real ones in
  `templates/` that Flask actually renders (`Flask(__name__)` uses the
  default `templates/` folder; nothing in the code ever references the
  root copies). Their git history is "Add files via upload" commits -
  the same drag-and-drop-to-GitHub's-web-UI workflow that caused the
  `config.json` tracking drift above, most likely dropping a batch of
  html edits into the repo root instead of into `templates/` at some
  point in the past. The webcam feature's nav-link edit landed on the
  dead root copy instead of the live `templates/base.html`, so it took
  effect nowhere. Fixed by moving the edit to `templates/base.html` and
  deleting the five stale root-level duplicates outright, so this can't
  recur. If any dashboard page ever renders old/missing content after a
  confirmed successful deploy again, check for a duplicate template at
  the repo root before assuming a service restart or caching issue.
- **[resolved, 2026-09-13]** `auto_update.sh` could wedge the repo into
  a broken, permanently-failing state when switching branches across
  the point where `config.json` went from git-tracked to untracked
  (see the `config.json` entry above) - git sees the incoming branch
  delete the file while the safety-net stash simultaneously modifies
  it, a real conflict it can't auto-resolve; a failed `stash pop` then
  left the index in a half-merged state that made every subsequent run
  fail at the same step (`error: could not write index`), even for a
  plain pull that shouldn't have touched `config.json` at all. Fixed by
  taking `config.json` out of git's hands entirely: it's now backed up
  out-of-band (`$HOME/.dermestid_config_backup.json`) and force-
  untracked (`git rm --cached -f`) before the stash step ever runs, and
  restored + re-untracked after checkout/pull completes, regardless of
  whether the branch landed on tracks it. Recovering the Pi's already-
  wedged index required a one-time manual fix: `git restore --staged
  auto_update.sh` (harmless queued mode-bit change), `git rm --cached
  config.json`, `git stash drop` (the unrecoverable conflicted stash
  entry), plus removing a couple of stray files
  (`.git_update.lock`-adjacent junk from mangled terminal pastes).
- **[open, 2026-09-13]** Full feature batch implemented and
  unit/smoke-tested (in the dev sandbox, not on real hardware yet):
  Home-page live feed, Camera page renamed to Timelapse with real
  ffmpeg-compiled `.mp4` videos (download/delete/multi-select), merged
  Config-page Camera+updates section, and Discover buttons for both the
  BLE sensor and the camera device. See the dated Camera/timelapse
  load-bearing entries above for the design decisions. Two real bugs
  were caught and fixed by smoke tests before this shipped (the
  `-vsync vfr` + `-r` ffmpeg conflict, and the `timelapse_videos`
  same-second primary-key collision) - both described above. **Still
  needed before this is done:** deploy to the Pi (after the Pi 4
  migration above) and verify for real - the MJPEG live feed rendering
  smoothly on the Home page, a full mode-change actually triggering a
  video compile, the Timelapse page's download/delete/bulk actions
  against a real video file, and both Discover buttons against real
  hardware (the camera one specifically needs the two new `sudoers`
  lines added on the Pi, or it'll silently only find indices the camera
  service isn't already holding open). `config.json`'s camera block
  needs no migration for this batch - no key was renamed.
- **[open, 2026-09-13]** BLE battery check retry cadence fixed:
  `ble_battery.py` failing (e.g. the SensorPush phone app happened to
  be connected) used to mean waiting a full 24h+ for the next
  `OnCalendar=daily` timer fire before retrying - user reported this
  directly ("a mechanism needs to be in place so it doesn't just
  repeatedly fail"). Fixed with a freshness check rather than just a
  blunter timer: `dermestid-battery.timer` now fires every 4h
  (`OnCalendar=*-*-* 0/4:00:00`), but `ble_battery.py`'s `main()` skips
  the actual BLE connection attempt whenever `battery_ts` (already
  existed in `ble_readings`, previously only used for display) shows
  the last *successful* check is under `BATTERY_CHECK_FRESHNESS_HOURS`
  (20h) old. Net effect: normal operation still only genuinely connects
  to the sensor ~once/day (same HT1 single-connection-at-a-time
  consideration as before), but a failed day now gets retried within a
  few hours instead of up to ~24-48h later. Verified via a standalone
  logic-replica test (6 cases: never-checked, fresh, just-under-
  threshold, just-over-threshold, stale/failed-yesterday, falsy
  `battery_ts`) - real BLE connection logic itself untestable in this
  sandbox (no `bleak`/hardware). **Still needs**: re-copying
  `systemd/dermestid-battery.timer` to `/etc/systemd/system/` and
  `daemon-reload` + timer restart on the Pi (same one-time-install
  caveat as any `.timer`/`.service` change - `git pull`/`auto_update.sh`
  alone won't pick this up).
- **[open, 2026-09-13]** `climate.py`'s `read_temp_and_humidity_f()`
  (the wired DHT22 probe reader, used for both the internal sensor in
  `dht22` mode and the external sensor's `local_gpio` fallback) never
  logged anything on failure - it silently returned `(None, None)`
  after exhausting `DHT_READ_RETRIES`, and only caught `RuntimeError`
  specifically (any other exception type would have propagated
  uncaught and potentially crashed the control loop). Found while
  investigating the user's report of missing wired/Fallback sensor
  data persisting after the Pi 4 migration - there was genuinely no way
  to see in the logs whether that specific probe was failing at all.
  Fixed: now catches any exception type (never crashes the loop) and
  logs a `journalctl -u dermestid-climate.service`-visible warning,
  naming the GPIO pin and the specific error, only once all retries are
  exhausted (not per-attempt, since a single flaky attempt is routine
  and expected - see the existing docstring reasoning above this fix).
  Verified via a standalone logic-replica test (5 cases: clean success,
  transient `RuntimeError` then recovery, transient non-`RuntimeError`
  then recovery, all-attempts-raise, all-attempts-return-`None`) -
  `climate.py` itself can only be `ast.parse`d in this sandbox, not
  imported/run (needs real `RPi.GPIO`/`adafruit_dht`/hardware). **Still
  open**: the user's physical wiring (signal=pin7/BCM4, ground=pin9,
  power=pin17) was independently confirmed correct against
  `PIN_EXTERNAL_TEMP=4`, ruling out mis-wiring - once this logging fix
  is deployed and the probe fails again, `journalctl -u
  dermestid-climate.service -n 40` should finally say *why*
  (connection/sensor/Pi4-timing) instead of just that the field is
  empty.
- **[resolved, 2026-09-13]** Light-override icon not appearing on the
  Home page live view, despite the code genuinely being present in
  `templates/home.html` - user confirmed the given instructions (check
  for a stale root-level duplicate template first, then restart
  `dermestid-web.service` to clear Flask's production-mode template
  caching - see the two precedents this was based on) resolved it.
  Which of the two was the actual cause wasn't specified back, but
  either way this confirms the deploy-visibility-gap diagnosis was
  right, not a code bug.
- **[open, 2026-09-13]** Video "still choppy... nothing like a true
  live feed" reported again after the Discover/auto-restart UX batch
  shipped. Root cause is almost certainly still the same one already
  identified two rounds ago, not a new problem: the user had
  `live_capture_interval_seconds` set to `2` (2s between frames =
  0.5fps; the tuned default is `0.2`, ~5fps) and, separately, the new
  Discover-and-prefill feature had set the operational live-capture
  resolution to the camera's max 1920x1080 (via Save) rather than a
  performance-appropriate value - both compound the same symptom.
  Recommended fix given: set `live_capture_interval_seconds` back to
  `~0.2` and resolution back down to `1280x720` (or lower) on the
  Config page. **Confirmed applied (2026-09-13)** - user set the
  interval back to `0.2` - **and video is still choppy**, so this is
  now confirmed to be a real pipeline problem, not just a leftover bad
  config value. User asked directly whether switching to
  `camera-streamer` (a purpose-built V4L2/libcamera MJPEG/HLS streaming
  daemon, commonly used in the OctoPrint/3D-printing community, that
  uses the Pi's hardware JPEG encoder) would fix it instead of
  continuing to tune this project's own OpenCV-based pipeline. Given
  serious consideration rather than dismissed - see the dated entry
  below for the full assessment (this pipeline currently does 100%
  software JPEG encode via OpenCV with no hardware acceleration at all,
  a genuinely real bottleneck camera-streamer would remove) - not yet
  decided/started, pending the user's go-ahead given it's a real
  architectural change (external binary dependency, and it would need
  to take over the "only one process holds the USB device" role that
  `camera_service.py` currently owns for both live view AND timelapse
  capture - see below). Root cause still not narrowed further than
  that (capture rate, encode time, or relay overhead specifically -
  see the Pi
  3B+-era live-view-performance entry above, which may or may not
  still be relevant post-Pi-4-migration).
- **[open, 2026-09-13]** `journalctl -u dermestid-climate.service`
  provided for the missing wired/Fallback sensor investigation - showed
  `External: --F/--%` on literally every single logged cycle across
  ~18 minutes and two service restarts, with **no** `DHT22 on GPIOx
  failed...` warning line anywhere, even though the new logging fix
  (see the dated entry above) should log one every cycle if the wired
  probe genuinely failed that consistently. This means the log
  predates that fix being deployed (it was only just pushed to the
  user's local folder this same round) - the diagnosis can't move
  forward until it's actually deployed (`git pull` + `sudo systemctl
  restart dermestid-climate.service` on the Pi) and a fresh log is
  pulled afterward. Separately worth noting for whoever reads this
  next: `climate.py` reads BLE and wired every cycle regardless of
  which is active (see the External-source-selection comment in
  `climate.py`), and only falls back to wired at all once BLE goes
  stale past `SENSOR_FAIL_TIMEOUT` - so `External: --` on every single
  cycle with no exception could ALSO mean BLE has gone stale again
  (forcing the wired fallback) at the same time the wired probe itself
  is failing, not necessarily proof the wired probe alone is broken.
  Worth checking `dermestid-ble.service`'s own recent freshness
  alongside the redeployed climate log, not just climate.py in
  isolation.
- **[open, 2026-09-13]** User asked directly why not just switch the
  live-view pipeline to `camera-streamer` instead of continuing to tune
  `camera_service.py`'s own OpenCV-based capture. Honest assessment:
  this wasn't a "considered and rejected" decision before now - it's a
  genuinely strong candidate that hadn't been evaluated as an
  alternative to incremental tuning. What this project's pipeline
  currently does, confirmed by reading both files: `camera_service.py`
  captures via `cv2.VideoCapture.read()`, JPEG-encodes every single
  frame in pure software via `cv2.imencode()` (zero hardware
  acceleration - neither the Pi 3B+ nor Pi 4's GPU-based JPEG/H264
  encoder is touched at all), writes it to `camera/latest.jpg`, and
  `webapp.py` (a separate process) re-reads that same file from disk
  every `STREAM_RELAY_INTERVAL` (0.15s) per connected browser tab and
  relays it as a `multipart/x-mixed-replace` MJPEG stream over Flask's
  built-in Werkzeug dev server (not a production WSGI server) -
  genuinely a lot of avoidable overhead stacked up (software encode,
  disk-file handoff between two processes, a dev server doing the
  actual video relay) for what's meant to be a smooth live feed.
  `camera-streamer` is a purpose-built V4L2/libcamera MJPEG/HLS/WebRTC
  daemon (originally for OctoPrint, widely used for exactly this kind
  of Pi + USB webcam setup) that uses the hardware JPEG encoder and
  would very plausibly fix the choppiness outright, likely more
  effectively than any further tuning of this pipeline could. **Why
  this needs a real decision, not just a swap**: `camera_service.py`
  currently does double duty - it's the ONLY process that opens the
  USB device at all, specifically so simultaneous dashboard viewers and
  the timelapse-snapshot logic never fight over it (most UVC webcams
  only support one client). Handing live view to `camera-streamer`
  means `camera_service.py` can no longer be the one holding the device
  open for live capture - either it stops capturing for live view
  entirely and only wakes up per-mode to grab timelapse snapshots via
  `camera-streamer`'s own snapshot endpoint (avoiding the two-clients
  problem, but a real rewrite of `camera_service.py`'s main loop and
  probably `discover_camera.py`'s stop/start-the-service dance too), or
  the two run side by side against genuinely separate devices/paths
  (not applicable here, single webcam). It also adds a real external
  binary dependency (build/install `camera-streamer` on the Pi itself,
  a new systemd unit, its own config surface) rather than staying a
  pure-Python/OpenCV/ffmpeg stack, which is a different maintenance
  profile than everything else in this project. **Not started** -
  this is a legitimate redesign, not a drop-in swap; needs the user's
  go-ahead on the scope before starting given the architectural
  tradeoffs above, especially since it would touch the same file the
  timelapse feature (a whole separate, already-shipped feature) also
  depends on.
  **User pushed back asking for the actual why-or-why-not against the
  intended outcome rather than picking from options** - real point
  worth recording: this pipeline's conservative defaults (the ~5-6.7fps
  ceiling, the STREAM_RELAY_INTERVAL/live_capture_interval_seconds
  split) were explicitly tuned around Pi 3B+ CPU scarcity (see
  `camera_service.py`'s own docstring and the Pi 3B+-era live-view-
  performance entry above) - all written and tuned BEFORE the Pi 4
  migration. It's genuinely possible current choppiness is just those
  stale conservative limits rather than a hard architectural ceiling -
  a cheap, lower-risk thing to actually try and measure on the Pi 4
  first (push STREAM_RELAY_INTERVAL/live_capture_interval_seconds
  higher, watch CPU/temp) before committing to the camera-streamer
  rewrite. camera-streamer's real structural advantage is removing the
  100%-software JPEG encode (the most likely dominant bottleneck) via
  the Pi's hardware encoder - genuinely the more "correct" fix for a
  truly smooth feed at low CPU cost - but that advantage is worth less
  if a Pi-4-appropriate speed bump on the EXISTING pipeline already
  gets acceptably smooth video. Recommended to the user: try the cheap
  speed-bump test first; if still choppy with headroom to spare, that's
  real evidence for camera-streamer being worth its cost. Not yet
  tried.
- **[open, 2026-09-13]** Correction to my own prior guidance: I told the
  user to "bump `live_capture_interval_seconds` and
  `STREAM_RELAY_INTERVAL` up" to test for smoother video on the Pi 4 -
  backwards. Lower = faster/smoother for both (fewer seconds *between*
  frames = more frames per second), the exact non-intuitive-direction
  trap this file's own earlier `live_capture_interval_seconds=2`
  choppy-video entry already flagged once before - I repeated the same
  mistake in my own phrasing this time. Also `STREAM_RELAY_INTERVAL`
  wasn't actually a Config-page setting at all - it was a hardcoded
  constant in `webapp.py`, so "adjust it and watch CPU/temp" wasn't
  even actionable as stated. Both fixed together as a real feature
  batch, not just a wording correction:
  - `stream_relay_interval_seconds` is now a genuine `camera` config
    key (default `0.15`, `STREAM_RELAY_INTERVAL_BOUNDS = (0.05, 5)`),
    Config-page-editable, read fresh every frame by
    `api_camera_stream()`'s generator (no service restart needed - the
    old module constant stays only as a fallback default if the key is
    somehow missing).
  - Added `shared_state.get_pi_health()`: reads CPU temp via `vcgencmd
    measure_temp`, load average via `os.getloadavg()`, and the
    under-/over-voltage `get_throttled` bits (same bitmask decoding
    already used by hand throughout the BLE Tier-3 troubleshooting
    above - bit0/bit1 current, bit16/bit18 sticky-since-boot) - all
    wrapped so a missing `vcgencmd` (not a real Pi) returns all-`None`
    rather than raising. Verified via a standalone test with a fake
    `vcgencmd` script on `$PATH` covering the parse of real
    `temp=53.8'C`/`throttled=0x50000` and `0x50003` output (the actual
    hex value from this project's own earlier BLE troubleshooting), and
    the no-`vcgencmd`-present case (this dev sandbox).
  - `climate.py` samples it once per control cycle (cheap - a couple of
    fast subprocess calls) and logs `cpu_temp_f`/`cpu_load_1m` into the
    SAME `readings` row as the sensor data (two new nullable columns,
    migrated the same way `ble_temp`/`wired_temp` were) - one sampling
    loop, one table, not a parallel system.
  - Home page: a small readout under the live view (`Pi: 128.8°F
    (53.8°C) · load 0.42`), turning amber past 158°F/70°C and red past
    176°F/80°C - not yet-throttling thresholds, just "keep an eye on
    it" vs. "getting close." Data page: two new raw-table columns
    (`Pi °F`, `Pi load`). Both come from the same `/api/status` and
    `/api/readings-table` endpoints already polled/fetched - no new
    endpoint needed for either.
  - Verified via Flask test client (home/config/data pages render the
    new elements; `/api/camera-settings` accepts
    `stream_relay_interval_seconds` without triggering a service
    restart; `/api/status` and `/api/readings-table` surface
    `cpu_temp_f`/`cpu_load_1m` once `log_reading()` is called with
    them) and a standalone DB test covering the column migration
    against a simulated pre-existing `readings` table.
  - The `get_throttled` under-/over-voltage flags themselves
    (`throttled_now`/`throttled_since_boot` in `get_pi_health()`'s
    return value) are NOT yet surfaced anywhere on the dashboard, only
    logged/displayed temp and load - deliberately scoped down to avoid
    growing this further; still worth adding to the Home page readout
    later if a power issue needs watching live again, same category as
    the BLE Tier-3 investigation above.
  - **Not yet deployed/tested on real hardware.** Needs `git pull` on
    the Pi, PLUS explicit restarts this time (unlike a Config-page save,
    which is what the auto-restart-on-save logic elsewhere in this
    project actually covers, and doesn't apply to a code deploy at
    all) - `dermestid-climate.service` needs a restart to pick up the
    new `get_pi_health()` call and DB columns, and `dermestid-web.
    service` needs one too, both for the new `/api/status`/`/api/
    readings-table` fields AND because `home.html`/`config.html`/
    `data.html` all changed - exactly the template-caching gotcha the
    light-icon entry above already hit once this same session.
    `camera_service.py` itself is untouched by this batch (the relay
    interval is read by `webapp.py`, not the camera service), so
    `dermestid-camera.service` doesn't need restarting for this.
- **[open, 2026-09-13]** User-requested simplification, before the
  batch above even got deployed: (1) re-express the camera's two
  frame-rate settings as frames/sec instead of seconds-between-frames,
  and (2) replace the separate width/height number inputs with a single
  resolution dropdown populated from what the camera actually supports.
  Both implemented:
  - **fps rename**: `camera.live_capture_interval_seconds` ->
    `live_capture_fps` (default `5`, `CAMERA_FPS_BOUNDS = (0.1, 10)` -
    tightened the floor vs. the old interval bound's effective 0.033fps
    equivalent, since anything slower than "1 frame per 10s" isn't
    really a live view anymore, it overlaps with the separate
    `snapshot_interval_minutes` timelapse feature) and `camera.
    stream_relay_interval_seconds` -> `stream_relay_fps` (default `7`,
    `STREAM_RELAY_FPS_BOUNDS = (0.2, 20)`). `camera_service.py` and
    `webapp.py`'s `api_camera_stream()` each convert their own fps
    value to a sleep interval internally (`1.0 / fps`) once per cycle -
    config.json and the Config page only ever deal in fps now. A
    one-time migration in `load_config()` converts an old config.json's
    seconds-based values to their fps equivalent on next load (same
    pattern as the earlier `sensorpush_mac` -> `ble_mac` rename, but a
    VALUE conversion this time, not just a key rename) - verified with
    the user's own actual stale value from earlier this session
    (`live_capture_interval_seconds: 2` -> `live_capture_fps: 0.5`,
    confirming just how slow that setting really was), plus stability
    (no double-conversion on repeated load/save) and the
    already-migrated and brand-new-config cases.
  - **This also directly corrects my own "bump the interval up" mistake
    from the entry above** - fps genuinely can't be misread in the
    wrong direction the way the interval naming could, so this isn't
    just a wording fix, it removes the whole class of mistake.
  - **Resolution dropdown**: `discover_camera.py` now actually tests
    each device against a curated `COMMON_RESOLUTIONS` list (3840x2160
    down to 320x240, mixed 16:9/4:3) via `probe_supported_resolutions()`
    - sets each candidate, reads a real frame back (not just set()+get()
    without reading, which some V4L2 drivers can report success for
    without truly committing to), and keeps only what the driver
    actually delivers within a 2px tolerance. The camera's own
    negotiated max (from the existing 1920x1080 probe request) is
    always included even if it's not an exact `COMMON_RESOLUTIONS`
    match. Verified via a fake `cv2.VideoCapture` simulating a webcam
    that only truly supports 3 of the 11 candidates - confirmed exactly
    those 3 come back, real max first. The Config page's Camera card
    now has one `<select>` instead of two number inputs; clicking a
    Discover result rebuilds its options from THAT device's own
    `supported_resolutions` (falling back to the generic
    `COMMON_RESOLUTIONS` list, duplicated as a JS constant - "keep in
    sync" comment pointing at the Python source - if Discover hasn't
    been run yet, or a particular probe came back empty). The
    currently-configured width/height is always included as an option
    even if hand-edited into config.json outside this list, so opening
    the Config page and saving without touching this field can never
    silently change it.
  - Verified via Flask test client: Config page renders the new
    `<select>`/fps inputs with no stale element IDs; `/api/camera-
    settings` accepts `live_capture_fps`/`stream_relay_fps`, rejects
    out-of-bounds values, and defaults correctly when omitted.
  - **Not yet deployed** - same restart requirements as the Pi-health
    batch directly above (this shipped in the same push, before that
    batch had been deployed/tested on real hardware yet either).
- **[open, 2026-09-13]** Wired/fallback external DHT22 (GPIO4,
  `PIN_EXTERNAL_TEMP`) investigation - user provided fresh
  `journalctl -u dermestid-climate.service` output after the DHT-
  failure-logging fix went in. Findings:
  - GPIO4 failed on **every single cycle** in the ~5 minute window shown
    (100% failure rate), always with "device returned None for
    temperature/humidity" - not a raised exception. GPIO27 (internal)
    also failed for the first ~3 cycles right after the service
    restart, then recovered - a previously-unknown data point, since
    only the external probe had been suspected before.
  - **Found and fixed a real bug while investigating**: pulled
    `adafruit_dht`'s actual source (v4.0.12) and confirmed
    `DHTBase.measure()` enforces its own ~2s minimum interval between
    physical reads *per device instance* - if called again sooner, it
    does NOT re-trigger a real read, it silently re-returns whatever
    `self._temperature`/`self._humidity` already held (`None` on a
    device that's never had a successful read) with no exception at
    all. `DHT_READ_RETRY_DELAY_SECONDS` is only 0.5s, so attempts 2 and
    3 inside `read_temp_and_humidity_f()` were never doing a real
    bitbang read - they were instantly echoing attempt 1's already-
    failed result back, faster than the sensor's own minimum sample
    interval allows. This exactly matches the "returned None" pattern
    in the logs, and means `DHT_READ_RETRIES=3` was effectively 1 real
    attempt per cycle, not 3. **Fixed** in `climate.py`:
    `_get_dht_device(pin, force_new=...)` now recreates the device
    object on every retry (`attempt > 0`), not just once per pin - a
    fresh instance has `_last_called` reset to 0, so `measure()` always
    performs a genuine new physical read. This affects both sensors
    equally and is a real robustness improvement regardless of the
    GPIO4 mystery below, but does NOT by itself explain a 100%,
    cycle-over-cycle failure rate sustained across ~17 independent
    cycles - each of those was already a genuine fresh physical attempt
    15s apart (well over the library's 2s minimum), so this bug was
    only ever wasting the *within-call* retries, not masking 17
    real successes.
  - **Leading hardware hypothesis, not yet confirmed**: GPIO4 (physical
    pin 7) is the Raspberry Pi's **default 1-Wire bus pin** - enabling
    1-Wire (via `raspi-config` or a `dtoverlay=w1-gpio` line in
    `/boot/firmware/config.txt`, e.g. for a DS18B20) claims GPIO4 for
    the kernel's `w1-gpio` driver by default unless a `gpiopin=`
    override is set. A DHT22 uses a completely different single-wire
    timing protocol, so if that overlay is active, every single bitbang
    attempt on GPIO4 would fail consistently - not flaky, not
    intermittent, matching the observed 100% failure rate exactly -
    regardless of the wiring itself being correct (already confirmed
    earlier this session: physical pin 7 = GPIO4, pin 9 = GND, pin 17 =
    3.3V). This would also explain the "External: 84.4F/47.6%" reading
    logged moments before the restart: `climate.py`'s failover logic
    uses BLE as the active external source whenever it's fresh, falling
    back to wired automatically - so that reading was very likely
    sourced from BLE the whole time, with GPIO4 silently failing
    underneath even then. **Not yet checked** - needs the user to run,
    on the Pi itself: `cat /boot/firmware/config.txt | grep -i w1`,
    `ls /sys/bus/w1/devices/ 2>/dev/null`, and `lsmod | grep w1` (or
    check Interface Options -> 1-Wire in `raspi-config`). If 1-Wire is
    enabled, disabling it (or moving whatever uses it to a different
    `gpiopin=`) is the fix.
  - **Fallback hypothesis if 1-Wire isn't it**: a bare (4-pin, no
    onboard PCB) DHT22 needs its own external ~10k ohm pull-up resistor
    between VCC and the data line per the datasheet; many 3-pin
    breakout modules include this on-board, a bare sensor does not. If
    the internal sensor (GPIO27) is a breakout module and the external/
    wired one is a bare sensor without an added pull-up, that alone
    would produce exactly this kind of persistent, one-sided failure.
    Worth confirming which type of DHT22 is on the wired external run,
    and whether a pull-up resistor was added.
  - **[RESOLVED, 2026-09-13]** Both hypotheses above turned out to be
    wrong, ruled out one at a time with real evidence rather than
    assumption:
    - 1-Wire: confirmed off (`grep -i w1 config.txt`, `/sys/bus/w1/
      devices/`, `lsmod | grep w1` all empty).
    - `pigpiod`/Remote GPIO: confirmed not installed at all (`systemctl
      status pigpiod` - unit not found; `dpkg -l | grep pigpio` - empty).
    - Dual GPIO-backend race (RPi.GPIO direct import vs. Blinka's own
      backend): ruled out - `pip3 show RPi.GPIO` shows `Required-by:
      Adafruit-Blinka`, meaning Blinka uses the very same `RPi.GPIO`
      install for the DHT22 bitbang reads, not a separate/competing
      backend. One unified access path, no cross-library conflict.
    - Supply voltage (3.3V vs. the internal sensor's 5V): ruled out by
      direct test - moved the external probe's VCC from pin 17 (3.3V)
      to 5V, failure persisted identically, reverted.
    - The physical DHT22 sensor unit itself: ruled out by swapping which
      sensor head sat on which wiring run (internal <-> external) while
      leaving the wires themselves in place - the failure stayed with
      the GPIO4 wiring regardless of which sensor was attached to it.
    - **Root cause, confirmed directly**: `pinctrl get 4` with the DATA
      wire physically unplugged from the Pi's header entirely (nothing
      connected to the pin at all) still reported the pin reading `lo`
      despite being configured as an input with its internal pull-up
      enabled. A pin in that exact configuration can only read high if
      it's functioning correctly - there is no wiring, sensor, or config
      state that can make a genuinely floating, pulled-up input read
      low. This is a hardware fault in GPIO4 on this specific Pi 4
      board (comparison: `pinctrl get 27` read `hi`, the expected/
      correct idle state, the whole time).
    - **Fix applied**: `PIN_EXTERNAL_TEMP` moved from GPIO4 to GPIO5
      (physical pin 29, previously unused by this project) in
      `climate.py`, with a comment explaining why. README's external-
      probe wiring instructions and both other GPIO4 mentions updated
      to match. **User still needs to physically move the DATA wire**
      from physical pin 7 to physical pin 29 (GND/VCC unchanged) and
      restart `dermestid-climate.service` after pulling this update -
      not yet confirmed working on real hardware.
    - Worth noting for the historical record: this whole investigation
      also turned up and fixed a real, independent bug in
      `read_temp_and_humidity_f()`'s retry logic (see the fps/dropdown
      entry above this one for the Pi-health batch context, and the
      code comment in `climate.py` itself for the fix) - `adafruit_dht`
      silently no-ops retries called within ~2s of a prior attempt on
      the same device instance, so the original code's 3 "retries" were
      actually 1 real attempt plus 2 instant echoes of its result. Fixed
      by recreating the device object on every retry. This is a genuine
      improvement to both sensors' resilience to ordinary single-attempt
      flakiness, independent of the GPIO4 hardware fault above.
    - **[UPDATE, 2026-09-13]** Turned out the "GPIO4 is dead on this Pi's
      silicon" conclusion above was likely wrong, or at least incomplete
      - the Pi is currently mounted in an Argon ONE V2 case (temporary,
      not used in final deployment) whose internal riser board the Pi's
      GPIO header plugs through, which sits in the electrical path
      even after unplugging a wire at the case's own breakout. After
      properly rewiring the fallback probe's DATA line to GPIO5/pin 29
      (confirmed done correctly this time), it started producing data -
      intermittently, which is the documented-normal DHT22 flakiness
      this codebase already expects (see `read_temp_and_humidity_f`'s
      own docstring), not a remaining problem. Root cause is most likely
      the Argon case's pass-through connector rather than genuine Pi
      board damage, but this hasn't been definitively confirmed by
      testing outside the case yet - worth doing before deciding whether
      GPIO4 is safe to use again once the case is out of the picture for
      final deployment.
    - **UI fix, same investigation**: the Data page's raw readings table
      showed "BLE"/"Wired" in the Active source column while every
      other column and the Home page tiles already say "External"/
      "Fallback" for these same two sensors - inconsistent naming
      (technology vs. tile identity) noticed while watching this table
      during the fix above. `templates/data.html`'s `srcAbbrev()` now
      returns "Ext"/"FB" to match.
- **[open, 2026-09-13]** Three header/nav UI requests, done together:
  - **Door badge replaced with a light badge.** The header used to show
    a door open/closed badge (initially just recolored to grayscale-when-
    closed per an earlier request, then replaced outright here rather
    than kept). It's now a `Light` badge using the same `.output-badge`
    on/off styling as Fan/Heater/Dehum, reflecting the light's actual
    physical on/off state - `is_open || lightOverrideActive`, mirroring
    the exact same OR climate.py's `light_loop()` uses for `PIN_LIGHT`
    - not just whether the manual override (the existing 💡 button on
    the live-view card, unchanged) happens to be set.
  - **Door-open is now a large global banner**, styled like the existing
    update-available banner (reusing the `.banner` danger/red style
    already used for the stale-sensor and camera-unavailable warnings on
    Home) and living in `base.html` so it shows on every page, not just
    Home - matches the update banner's "regardless of which page you're
    on" behavior, since an open enclosure matters no matter which tab
    you're looking at. Polled every 15s (`/api/status`) - faster than the
    update banner's 60s, since this is time-sensitive in a way an
    available software update isn't.
  - **Nav bar is now sticky** (`position: sticky; top: 0` on
    `nav.topnav`) so switching pages (Home/Logs/Data/Timelapse/Config)
    doesn't require scrolling back to the top first.
  - Not yet confirmed working on real hardware/deployed.
  - **Follow-up fix, same day**: user asked whether it'd be lighter-weight
    to have the door/update banners "part of the header for paging" -
    they already are (`base.html` is the one shared layout every page
    extends, not duplicated per-page), but the question surfaced a real
    redundant-fetch bug: Home already polls `/api/status` every 5s for
    its own tiles, and the new door-banner code in `base.html` was
    *also* independently polling the same endpoint every 15s - doubling
    that page's request rate for no benefit, since Home's own faster
    poll already has everything the banner needs. Fixed by exposing
    `updateDoorBanner()` as a standalone global function in `base.html`
    and having Home set `window.__doorStatusPolledElsewhere = true`
    (checked before `base.html`'s trailing script sets up its own
    fetch+interval) so Home drives the banner from its existing 5s poll
    instead of a second, separate one. Every other page (Logs/Data/
    Timelapse/Config) still gets the base.html-driven 15s poll, since
    they don't otherwise fetch `/api/status` at all.
  - **Bug from this same edit, broke every page**: a code comment in
    `base.html` literally contained the text `{% block` ... `%}` (talking
    about the Jinja block-tag mechanism in prose) - Jinja2 scans the
    entire raw template file for `{%`/`%}` before anything about HTML or
    JS comments is understood, so it tried to parse that comment as a
    real template tag and threw `TemplateSyntaxError: expected token
    'name', got '//'` on every single page (all of them extend
    `base.html`), a full Internal Server Error site-wide. Fixed by
    rewording the comment to avoid literal `{%`/`%}` characters. Verified
    this time with an actual Jinja2 parse check (`env.get_template()`)
    against every template file, not just eyeballing the diff - worth
    doing that check by default before pushing any template change from
    now on, not only after something breaks.
  - **[CONFIRMED, 2026-09-13]** User confirmed the site loads again after
    the fix above. Unrelated to the Jinja bug itself but hit in the same
    session: their `update-dermestid` shell alias (`~/.bashrc`, personal,
    not part of this repo) was calling `./auto_update.sh` directly, which
    needs the executable bit - lost at some point, most likely because
    their commit path goes through GitHub Desktop on Windows, which has
    no concept of a Unix executable bit and can write the file back as
    non-executable. Changed the alias to `bash auto_update.sh` instead
    (matching what `webapp.py`'s own "Update now" button already does),
    which no longer depends on that bit at all - should stop recurring.
  - **Still open**: the Light badge/door-banner/sticky-nav UI batch and
    the door-banner redundant-poll fix (both earlier entries above) are
    not yet confirmed working on real hardware - worth a check next time
    the dashboard's up.

- **[2026-09-13] Camera resolution memory (Discover no longer needed on
  every device switch)**
  - User asked for the device to remember which resolutions
    `discover_camera.py` already found for the currently-selected camera,
    so switching devices (or just reloading Config) doesn't fall back to
    the generic, unverified `COMMON_RESOLUTIONS` list until Discover gets
    clicked again.
  - New `config.json` key `camera_known_resolutions`: a plain dict keyed
    by device index as a *string* (e.g. `"0"`), value = that device's
    list of confirmed-supported resolutions from the last time Discover
    actually ran. Added to `DEFAULT_CONFIG` in `shared_state.py`; existing
    config.json files on disk backfill this key automatically the next
    time `load_config()` merges against defaults, no migration needed.
  - `webapp.py`'s `api_camera_discover()` now writes into this dict for
    every device Discover finds, right before returning its response -
    so the cache is always as fresh as the last real probe.
  - `templates/config.html`: added a shared `applyResolutionsForDevice()`
    helper used by both `loadConfig()` (on page load) and the device
    input field's new `oninput` handler (`onCameraDeviceInput()`) - it
    prefers a *live* Discover result from this page session if one
    exists, falls back to the persisted `camera_known_resolutions` cache
    otherwise, and only falls back to the generic `COMMON_RESOLUTIONS`
    list if neither exists yet for that device. `selectCameraIndex()`
    (clicking a device in the Discover results list) was refactored to
    reuse the same helper instead of duplicating the logic. The
    resolution-dropdown hint text now says "remembered from a previous
    Discover" when serving from either the live or cached result, versus
    "click Discover above" when it's still showing the generic list.
  - Verified clean before pushing: `python3 -m py_compile shared_state.py
    webapp.py` and a real Jinja2 parse check
    (`env.get_template(name)`) against all six templates - both clean.
    This check is now standard practice after the earlier
    `{% block %}`-in-a-comment incident above broke every page; doing it
    by default is cheap insurance against a repeat.
  - **Not yet confirmed working on real hardware.** To test: run Discover
    once for a camera, then either reload the Config page or switch to a
    different device and back - the resolution dropdown should repopulate
    immediately from memory without Discover needing to run again.
    `dermestid-web.service` needs a restart to pick up the `webapp.py` /
    `config.html` changes (config.json itself needs no manual edit - the
    new key just starts empty and fills in the next time Discover runs).

- **[2026-09-13] Live-feed jitter root-caused to camera capture format,
  not fps settings - likely fix applied, not yet verified on hardware**
  - Context: user had already tried raising `live_capture_fps` 5→10 and
    it didn't reduce perceived jitter (a real, useful negative result -
    see the fps-bounds entry above). Config was later pushed to 20/20
    for both `live_capture_fps` and `stream_relay_fps` for a real test.
  - User uploaded a short screen recording of the live feed at those
    settings. Rather than eyeballing it, analyzed it quantitatively:
    downsampled every decoded frame and measured actual pixel-content
    changes (not the screen recording's own 30fps container rate, which
    is irrelevant - a recording can hit 30fps while replaying the exact
    same still image most of the time). Real content only updated ~4.5x/
    sec on average (~0.224s between changes), with real jitter on top
    (~0.13-0.23s typical, occasional stalls to 0.6-0.77s) - i.e.
    genuinely unmoved from the old ~5fps behavior despite both fps
    settings being raised to 20. This confirms neither fps setting was
    ever the actual bottleneck.
  - **Likely root cause**: `camera_service.py`'s `open_capture()` never
    told OpenCV/V4L2 which pixel format to capture in. Left unset, many
    UVC webcams get negotiated into an uncompressed format (commonly
    YUYV) once a high resolution is requested - a raw 1920x1080 YUYV
    frame is ~4MB/frame, and over USB2 bandwidth that alone caps real
    frame delivery to roughly the range observed, independent of
    `live_capture_fps`/`stream_relay_fps`, since `cap.read()` simply
    blocks until the hardware/bus can deliver the next frame. This
    matches every observed symptom: pinned near 5fps, real jitter, and
    completely unmoved by the earlier 5→10 and 10→20 config changes.
  - **Fix applied** (not yet confirmed on real hardware): force MJPG as
    the capture format via `cap.set(cv2.CAP_PROP_FOURCC, ...)`, set
    BEFORE the resolution request (some V4L2 drivers only honor a format
    change if it comes first) - in both `camera_service.py`'s
    `open_capture()` (the actual capture loop) and `discover_camera.py`'s
    `probe_devices()` (so Discover's "supported resolutions" reflect the
    same format the real capture loop will use, not a possibly-different
    negotiated format). MJPG frames are roughly 10-20x smaller than raw
    at the same resolution, which should let the real hardware ceiling
    be much higher if the connected camera supports MJPG at all - if it
    doesn't, `.set()` on an unsupported fourcc is a harmless no-op
    (confirmed: tested against a non-existent device index, raised no
    exception), so this can't make things worse than before.
  - **Also added real fps telemetry**, since eyeballing screen recordings
    isn't a sustainable way to verify this: `camera_service.py` now
    measures the actual wall-clock interval between successful
    `cap.read()`s (EMA-smoothed, `_FPS_EMA_ALPHA = 0.3`) and writes it to
    a small `camera/stats.json` (not git-tracked, like `camera/latest.jpg`)
    roughly once/sec via `shared_state.save_camera_stats()`/
    `get_camera_stats()` (returns `None` if the file is missing or more
    than `CAMERA_STATS_MAX_AGE_SECONDS` (30s) old, so a dead
    `dermestid-camera.service` reads as "unknown," never a misleading
    "0 fps"). `webapp.py`'s `/api/status` now includes this as
    `camera_stats`, and the Home page's existing Pi-health line
    (`updatePiHealth()`) now appends `camera: X.X fps actual (target Y)`
    when available - so tuning `live_capture_fps` going forward can be
    judged against what's REALLY happening, not just what was configured
    (this is exactly the gap that made the earlier 5→10→20 config
    changes look like they weren't working - they may have been correct
    changes, just invisible against a hardware ceiling with no way to
    measure it before now).
  - Verified before pushing: `python3 -m py_compile` on all four touched
    `.py` files, a Jinja2 parse check on all six templates, a smoke test
    confirming `cap.set(CAP_PROP_FOURCC, ...)` on an unopened/nonexistent
    device raises no exception, and a round-trip test of
    `save_camera_stats()`/`get_camera_stats()` including the staleness
    path.
  - **Not yet confirmed working on real hardware.** To test: pull,
    restart `dermestid-camera.service` (and `dermestid-web.service` for
    the `/api/status`/Home changes), open the live view, and watch the
    new "camera: X.X fps actual" line on Home - if it climbs meaningfully
    closer to the configured `live_capture_fps`, the MJPG fix worked; if
    it's still pinned near ~5fps, the bottleneck is something else
    (worth checking `v4l2-ctl --device=/dev/video0 --list-formats-ext`
    on the Pi at that point to see what resolutions/fps the camera
    actually advertises per format) and JPEG quality/resolution may need
    to come down instead of fps going up.
  - **[CONFIRMED, 2026-09-13]** User re-tested with a fresh screen
    recording after deploying the MJPG fix and restarting the camera
    service. Same quantitative video analysis as before: previously only
    ~8% of the recording's own 30fps frames were near-duplicates of the
    one before them, and every duplicate run was a single frame (never
    back-to-back) - a dramatic change from the earlier clip, where
    content held static for ~0.2-0.77s stretches at a time. The fix
    worked - real capture rate is now tracking close to the recording's
    own frame rate instead of being pinned near ~5fps.

- **[2026-09-14] Camera fps added to long-term tracked data**
  - Following the above, user asked to add the camera's real fps to the
    tracked data for long-term history - `camera/stats.json` (see
    save_camera_stats()/get_camera_stats() above) only ever holds the
    CURRENT value, overwritten ~once/sec, so there was no way to look
    back at how it trended over a day/week the way the climate readings
    already can.
  - Two new nullable columns on the existing `readings` table:
    `camera_actual_fps`, `camera_target_fps` - deliberately reusing this
    table rather than creating a new one, since `climate.py` already
    samples one other cross-process, purely-informational metric this
    same way every control cycle (`cpu_temp_f`/`cpu_load_1m` via
    `get_pi_health()`) - camera fps gets the exact same treatment:
    `climate.py`'s main loop now also calls `state.get_camera_stats()`
    once per cycle (every `LOOP_INTERVAL` = 15s) and passes
    `camera_actual_fps`/`camera_target_fps` into `log_reading()`. Both
    are NULL on any cycle where `dermestid-camera.service` isn't
    installed/running or its stats.json is stale - same "absence is
    unknown, not zero" handling as every other optional sensor in this
    table.
  - Surfaced on the Data page's raw table (`get_readings_table()`) as two
    new columns, "Cam FPS" and "Cam target", right after the existing
    "Pi load" column - same pattern as the Pi-health columns, showing
    every control cycle's exact stored value rather than an average.
  - Deliberately NOT added to the bucketed/averaged history chart
    (`get_history()`) that powers the Home page graph - `cpu_temp_f`/
    `cpu_load_1m` were never added there either when they were
    introduced; that chart is scoped to the enclosure's own climate
    readings, and Pi/camera health metrics have so far only ever been
    surfaced live (Home's status line) and in the raw Data page table.
    Worth revisiting if long-term trend-spotting by eye (vs. scanning
    the raw table) becomes something the user actually wants for this
    metric.
  - Verified before pushing: `python3 -m py_compile` on all touched
    `.py` files, a Jinja2 parse check on all six templates, and a real
    SQLite round-trip test against a temp DB covering (a) a `log_reading()`
    call with the new camera args omitted (existing callers/old code
    path - stores NULL, as before), (b) a call with real camera values
    (stores and reads back correctly via `get_readings_table()`), and
    (c) migrating a genuinely pre-existing readings table (created
    without these two columns) through `init_db()` and confirming the
    old row's data survives untouched and the new columns come back NULL.
  - **Not yet confirmed on real hardware/deployed** - next restart of
    `dermestid-climate.service` will pick this up; no config.json change
    or manual DB migration needed (schema migration is automatic via
    `init_db()`, same as every other column added to `readings` before
    this one).
  - **[CONFIRMED, 2026-09-14]** User pulled and restarted all services -
    camera fps history, the resolution-memory feature, and the Light/
    door-banner/sticky-nav UI batch are all confirmed working.

- **[2026-09-14] Header/nav tweaks: Pi stats moved to nav bar, live-feed
  light icon no longer changes background color, 🪲 favicon added**
  - Pi CPU temp/load (previously folded into a line under Home's live
    view, alongside camera fps) moved to the nav bar itself
    (`base.html`), right-aligned via `margin-left:auto` on a new
    `.nav-pi-stats` div next to the page tabs - visible on every page now
    instead of Home only, same reasoning as the door banner already
    living in the shared nav/layout. Populated by the same `/api/status`
    poll that already drives the door banner (`checkGlobalDoorBanner()`,
    now also calling the new `updateNavPiStats()`) - Home still uses its
    own faster 5s poll and calls `updateNavPiStats()` directly, same
    "don't poll twice" pattern as before. Hidden on phone widths (no room
    next to the full-width tab bar there). The camera fps line stays
    exactly where it was on Home, just renamed (`updatePiHealth()` ->
    `updateCameraFpsLine()`, `#piHealthLine` -> `#cameraFpsLine`) since
    it's fps-only now.
  - Live-feed light toggle button (the 💡 overlay on the camera view, NOT
    the header Light badge) no longer turns its background yellow when
    on - instead the emoji itself is `filter: grayscale(1)` while off and
    `grayscale(0)` (full color) while on, with a quick CSS transition
    between the two.
  - Added a 🪲 favicon (the same beetle emoji already in the page's own
    `<h1>`) site-wide via an inline SVG data URI `<link rel="icon">` in
    `base.html` - no separate favicon.ico file to generate or host.
  - Verified before pushing: Jinja2 parse check on all six templates.
  - **Not yet confirmed on real hardware/deployed** - needs
    `dermestid-web.service` restarted to pick up all three template
    changes.
  - **[CONFIRMED, 2026-09-14]** User pulled/restarted and confirmed this
    batch, plus wanted the site's name changed from "Dermestid Enclosure"
    to "Necroparlor" everywhere it appears (browser tab title on all six
    pages, and the `<h1>` on Home) - plain find/replace, no other
    "Dermestid Enclosure" strings existed anywhere else in the codebase
    (confirmed by grep across all `.py`/`.html` files).

- **[2026-09-14] Free disk space added to the header, next to Pi temp/load**
  - New `shared_state.get_disk_free_gb()` - free space on this project's
    own directory (the SD card, on a real Pi), via `shutil.disk_usage()`.
    Deliberately its own small function rather than folded into
    `get_pi_health()`: it's pure Python (no `vcgencmd`), and is called
    fresh on every `/api/status` request from `webapp.py` rather than
    only once per `climate.py` control cycle - cheaper and simpler than
    threading it through `log_reading()`/the `readings` table the way
    `cpu_temp_f`/camera fps are, and this was only asked for as a live
    header readout, not long-term tracked history (unlike the camera fps
    ask from earlier today).
  - `webapp.py`'s `/api/status` now includes a top-level `disk_free_gb`.
  - `base.html`'s `updateNavPiStats()` (added earlier today for Pi temp/
    load in the nav bar) now also takes `diskFreeGb` and appends
    "X.XGB free" to the same line - e.g. "Pi: 95.5°F (35.3°C) · load
    0.72 · 12.3GB free". Two new warning thresholds, `DISK_FREE_WARN_GB`
    (2GB) and `DISK_FREE_DANGER_GB` (0.5GB), color the whole line the
    same way the existing Pi-temp thresholds already do - chosen to give
    a heads-up well before `camera_service.py`'s own
    `CAMERA_LOW_DISK_THRESHOLD_MB` (200MB) safety-net pruning would ever
    need to fire, since camera timelapse frames are the most likely
    thing to actually fill the SD card.
  - Verified before pushing: `python3 -m py_compile` on all touched `.py`
    files, a Jinja2 parse check on all six templates, and confirmed
    `get_disk_free_gb()` returns a sane real value in this dev
    environment.
  - **Not yet confirmed on real hardware/deployed** - needs
    `dermestid-web.service` restarted to pick up the `webapp.py`/
    `base.html` changes.

- **[2026-09-14] README rewritten for concision, GPIO pinout diagram added**
  - User asked for `README.md` to be made "as clear, concise, and
    streamlined as possible." It had grown to 361 lines/36KB, much of it
    narrative justification and troubleshooting-anecdote prose that
    belongs in this file, not there (this project already draws that
    line - the README's own opening line says as much). Rewrote it down
    to ~11.9KB: every actionable command, wiring table, config key, and
    safety-relevant constant kept (cross-checked several against the
    actual code while rewriting - e.g. caught that the README claimed a
    20-minute fan runtime cutoff, but `climate.py`'s real
    `FAN_MAX_ON_SECONDS` is 60 minutes, only the heater is 20 - fixed to
    match code, not the old README text). Dropped entirely: the "What
    changed from your original script" changelog section (redundant with
    this file), verified-this-specifically QA anecdotes, and multi-
    paragraph design-rationale digressions. Also fixed a stale "Pi 3B+"
    CPU-sharing reference in the camera fps paragraph that predated this
    session's Pi 4 migration.
  - Added `docs/gpio-pinout.svg` - a full 40-pin header diagram,
    color-coded (used-by-this-project / I2C-optional / power / ground /
    reserved / unused / dead-on-this-board), generated from
    `climate.py`'s actual `PIN_*` constants rather than a generic Pi
    pinout graphic. Built via a small generator script,
    `docs/gen_gpio_pinout.py` (not part of the running app - rerun it by
    hand and commit the regenerated SVG if `PIN_*` ever changes again,
    same as the GPIO4->5 move earlier this session would have needed).
    Verified by rendering to PNG with `cairosvg` and visually inspecting
    - caught and fixed a legend-text-clipping issue and a low-contrast
    swatch-color issue on the first pass before finalizing.
  - **Not yet confirmed by the user** - both the trimmed README and the
    new diagram are pushed but not yet reviewed for accuracy/readability
    on their end.
  - **[2026-09-14, follow-up]** User reported the new `docs/` folder
    wasn't showing up in their repo after committing. Both
    `docs/gen_gpio_pinout.py` and `docs/gpio-pinout.svg` were confirmed
    present on their machine at the correct path with the correct file
    sizes, so the push itself was fine - most likely the new folder
    just hadn't been picked up/staged in a GitHub Desktop commit yet
    (a brand-new folder needs to be explicitly included, unlike an edit
    to an already-tracked file). **[RESOLVED, 2026-09-14]** - the actual
    cause was different: the user's local checkout is on the
    `add-webcam` branch (confirmed by reading `.git/HEAD` directly on
    their machine), and GitHub's file browser defaults to showing
    `main` unless a different branch is picked from the dropdown - the
    commit itself (titled "matching jumper colors") was fine, `docs/`
    was just on a branch GitHub wasn't displaying. User confirmed
    switching the branch dropdown shows everything correctly. Worth
    remembering for any future "I don't see X on GitHub" report from
    this user - check which branch is selected before assuming a push
    problem.
  - **[2026-09-14, correction]** While recoloring the diagram (see below),
    learned the DHT22 wiring documented above was wrong: **both the
    internal and external DHT22 are actually powered from 3.3V (pin 1 /
    pin 17), not 5V** - both 5V pins (2, 4) are already committed to
    relay board power. DHT22 tolerates 3.3-5.5V so this changes nothing
    functionally, but the README's wiring tables and this diagram were
    both stating 5V. Fixed in both places.

- **[2026-09-14] GPIO pinout diagram recolored to match actual jumper
  wire colors, cleanup list identified for dead tracked files**
  - `docs/gen_gpio_pinout.py` no longer uses a generic category scheme
    (used/power/ground/reserved/unused) - the color of every pin in the
    diagram now matches the real jumper wire color Ryan is using on the
    physical build: red=5V, orange=3.3V, black=5V ground (not currently
    pinned to a specific spare GND pin in the diagram - no confirmed
    single pin services it), brown=3.3V ground, gray=door switch AND
    heater relay (deliberately the same color in real life), yellow=
    sensor data AND door servo signal (also deliberately the same),
    green=light relay, blue=fan relay, purple=dehumidifier relay. I2C
    (optional SHT31, not currently installed) kept its own muted blue,
    distinct from the fan relay's brighter blue, specifically so it
    reads as "inactive/optional" rather than "in use." Legend expanded
    from 7 to 13 entries to cover all of these; canvas widened
    (1040->1200) and the legend laid out as a 3-column grid to fit
    without clipping - verified again via `cairosvg` render + visual
    inspection.
  - This is also what surfaced the 3.3V/5V wiring correction above - the
    two DHT22 VCC pins had to be recategorized by voltage to pick their
    color, which is what prompted asking the user which voltage they're
    actually on.
  - Went through `git ls-files` cross-referenced with `grep -rln` across
    the whole codebase and confirmed five *tracked* files are dead -
    fully superseded by the generic BLE rebrand and unreferenced
    anywhere: `discover_sensorpush.py`, `sensorpush_battery.py`,
    `sensorpush_listener.py`, `systemd/dermestid-sensorpush.service`,
    and `download` (a stray file byte-for-byte identical to
    `.gitignore`'s contents - `git log` shows it came in via an
    "Add files via upload" commit, almost certainly an accidental
    GitHub-web drag-and-drop of `.gitignore` under the browser's default
    "download" filename). Handed this list to the user to delete
    themselves - no tool in this environment can delete a file on their
    machine directly. **[CONFIRMED DELETED, 2026-09-14]** - user deleted
    all five locally; verified via `device_list_dir` that none of them
    remain in the folder.
  - **[CORRECTION, 2026-09-14]** The "still needs a commit/push" note
    above was stale by the time it was written - checking `.git/logs/
    HEAD` directly shows the deletion was already committed same-day as
    the local delete, in the "removing unneeded files" commit (right
    before "matching jumper colors"), and pushed along with everything
    else since. Nothing left to do here; this was a documentation lag,
    not a real pending action - worth remembering that a "not yet
    committed" claim in this file can go stale if the user commits
    through Desktop without it being confirmed back in the same
    conversation.

- **[open, 2026-09-14] Frequent "Internal sensor reading unavailable"
  warnings - investigation started**
  - User reported dashboard logs full of "Internal sensor reading
    unavailable - starting failsafe countdown" (internal DHT22, GPIO27)
    - roughly 33 separate outages across ~5 hours on 2026-09-13 (this
    message only logs once per outage, on the first failed cycle, until
    a good read clears it - see the code comment at the `log_event` call
    site in `climate.py`), i.e. a full 3-retry read failure about every
    8-9 minutes on average. That's well beyond the "occasional
    single-attempt DHT flakiness" an earlier session's fix assumed was
    normal. Confirmed these are genuine raw read failures, not the
    anti-glitch delta filter (`validate_reading()` logs a distinctly
    different message, "...reading rejected: jumped from X to Y...",
    which never appeared in the reported logs).
  - Same log dump also showed 7 "Heater safety cutoff: on continuously
    for over 20 min" events that evening. **Initial theory (probably
    wrong, see correction below)**: proposed that frequent sensor
    outages were blinding the control loop during heating runs (a failed
    read cycle skips the whole heat/cool decision block entirely, so the
    heater can't be turned off even if setpoint was actually reached
    during an outage), contributing to hitting the hard 20-minute cutoff
    instead of a normal setpoint-reached shutoff.
  - **Correction, same day**: per the standing benchtop-context note
    added above, the relay outputs aren't wired to any actual heater/
    fan/dehumidifier right now. With no real thermal load, the internal
    temperature was never going to move toward setpoint no matter what
    the sensor was doing - so hitting the 20-minute heater cutoff every
    time is expected mechanical behavior in this test rig, not evidence
    tied to the sensor-outage frequency. The sensor-outage investigation
    below still stands on its own; the heater-cutoff correlation
    proposed initially should be treated as unconfirmed/likely
    coincidental, not a real causal link, unless a real heater load is
    added later and the cutoffs keep happening just as often.
  - **Leading hypothesis for the outage frequency, given this session's
    parallel finding that both DHT22s are wired to 3.3V (see the jumper-
    color/GPIO-pinout entries above)**: 3.3V is the bottom of the DHT22's
    spec range, and the sensor draws more current during an actual
    sample than at idle - more vulnerable to a failed read from wiring
    resistance/connection quality at 3.3V than at 5V.
  - **Live experiment, in progress (2026-09-14)**: user temporarily
    unhooked the relay/servo power from the Pi's 5V pins and moved both
    DHT22s onto 5V instead, specifically to test the voltage theory -
    only possible because the relays aren't driving real loads yet (see
    standing context above) and there's no separate PSU available. This
    is explicitly a **temporary benchtop test wiring**, not a documented
    final state - **README.md and docs/gpio-pinout.svg still say 3.3V
    for both DHT22s and have NOT been changed to match**, pending the
    result of this test and Ryan confirming whether 5V is the wiring
    he's keeping. Update both docs once that's settled either way.
  - Also suggested, not yet done: cross-reference the Data page's Pi
    load / camera fps columns (added earlier this session) against the
    outage timestamps to check for CPU-contention correlation as an
    alternate/additional cause.
  - **[UPDATE, 2026-09-14]** Both DHT22s have now been on 5V since
    2026-09-13, and Ryan reports the missing-fallback-sensor-data
    frequency has dropped substantially since the switch - real
    evidence in favor of the 3.3V-marginal-supply theory above, though
    not yet a controlled before/after comparison (see the dated
    "Frequent... warnings" entry's own outage-count method above if
    that's worth doing precisely). **Still not fully resolved/closed
    out**: (1) this was explicitly wired as a TEMPORARY test - the 5V
    pins it's using are the same ones earmarked for relay/servo power,
    which isn't connected yet (see this file's own standing benchtop-
    context note) - so before this can be called the permanent wiring,
    need to confirm there's enough 5V current budget for both DHT22s
    AND the relays/servo once those are actually wired in, not just
    for two low-draw sensors alone. (2) Pending that, README.md and
    docs/gpio-pinout.svg both still document 3.3V for both DHT22s and
    have not been updated - don't change them until (1) is actually
    settled, since flipping them now and then having to revert if the
    power budget doesn't work out would just recreate the exact
    doc-drift this file exists to prevent.
  - **Ryan's expectation, 2026-09-14**: likely going to need a separate
    power supply once the relays/servo are actually wired in - the
    Pi's own 5V rail probably can't carry both DHT22s and real
    relay/servo loads at once. Not confirmed by testing yet (nothing's
    wired to real loads currently - see this file's own standing
    benchtop-context note), just the working assumption to test once
    real hardware goes on the relays. **Deferred to later testing**,
    listed here so it isn't lost: once a PSU is in hand and the relays
    have a real load behind them, check whether the Pi's 5V rail alone
    is still enough with a separate PSU carrying the relay/servo side,
    or whether the DHT22s specifically need to go back to 3.3V (or their
    own dedicated supply) at that point.

- **[RESOLVED, 2026-09-14] SD card migrated: 16GB SanDisk Class 4 ->
  64GB PNY Elite-X (UHS-I U3, A1, V30)**
  - Motivation: the old card's small capacity and old Class-4 controller
    were both a real concern for a 24/7 project writing to SQLite every
    ~15s (see the card-comparison discussion earlier the same day) -
    64GB with an A1 rating gives much more headroom for the database,
    logs, and timelapse frames, plus a controller better suited to
    sustained small random writes.
  - Method: `rpi-clone` (github.com/billw2/rpi-clone), run on the Pi
    itself against the new card attached via a USB reader while the old
    card stayed the boot device - avoided a full from-scratch OS/deps/
    systemd/sudoers setup entirely, just a straight clone. rpi-clone
    auto-extended the destination partition to fill the card (57.4G) as
    part of the clone itself - no separate `raspi-config` expand step
    needed.
  - **First clone attempt was interrupted, not a real failure**: the
    user's PC restarted mid-clone, killing the PuTTY/SSH session (and
    with it the foreground `rpi-clone` process via SIGHUP) partway
    through. This left `/dev/sda2` mounted at `/mnt/clone` with nothing
    to clean it up, which then blocked the next attempt with "target is
    busy" until the leftover mount (`/mnt/clone/boot/firmware` nested
    under `/mnt/clone`) was manually unmounted. **Lesson for next time**:
    always run `rpi-clone` inside `tmux` (`sudo apt install -y tmux` if
    not already present) so a dropped connection can't strand it -
    `tmux attach` picks the exact same session back up instead of
    losing the run.
  - **Real bug, found the hard way over two failed boots**: `rpi-clone`
    reliably updates `/etc/fstab`'s PARTUUID for the new card (confirmed
    correct both times, root AND `/boot/firmware` lines) but does **NOT**
    reliably update `/boot/firmware/cmdline.txt`'s `root=PARTUUID=...` -
    the log only ever printed one generic "Editing .../etc/fstab
    PARTUUID..." line, never a matching one for `cmdline.txt`, across
    two separate full clone runs. Left uncorrected, the cloned card
    boots the kernel but then hangs forever looking for a root partition
    by the OLD card's PARTUUID, which doesn't exist once you're booted
    standalone on the new card - no HDMI needed to see it, it just never
    comes up on the network/SSH. Diagnosed and fixed **without a
    monitor**, using the old (still-intact) 16GB card as a known-good
    fallback to boot into: swap old card back in, boot, SSH in, mount
    the new card's partitions via the USB reader, and directly compare
    `cat cmdline.txt`'s PARTUUID against `lsblk -o NAME,PARTUUID
    /dev/sda`'s real value. Took two rounds to get exactly right - the
    first manual edit dropped the `-02` partition-number suffix entirely
    (`root=PARTUUID=e8626dc3` instead of `root=PARTUUID=e8626dc3-02`),
    which matches neither partition and produces the identical hang.
    **Lesson for next time, and for any future rpi-clone use on this
    project**: always manually check (and if needed, fix)
    `/boot/firmware/cmdline.txt`'s `root=PARTUUID=` against the new
    card's actual partition PARTUUID (`lsblk -o NAME,PARTUUID`) before
    trusting a clone to boot standalone - don't rely on the fstab edit
    alone as proof cmdline.txt was also handled.
  - **Confirmed working, 2026-09-14**: booted clean on the 64GB card,
    dashboard reports 48.1GB free (consistent with the auto-extended
    57.4G partition minus ~5.3G used by the OS/project - no data loss,
    nothing left to redo).
  - Old 16GB SanDisk card is now a spare/backup, untouched throughout
    this whole process.

- **[2026-09-14, end of session] Next planned work: a new branch to
  explore switching the live-view pipeline to `camera-streamer`.** See
  the existing camera-streamer analysis in the "Camera / timelapse"
  load-bearing-decisions section above (around the MJPEG-relay-vs-
  camera-streamer trade-off discussion) for the reasoning already
  worked out before stopping tonight - the plan going in is still to
  verify it runs standalone on the Pi first, then write dashboard
  integration against its actual observed behavior rather than
  documentation alone. Nothing done on this yet as of this entry - a
  fresh session tomorrow starts the new branch from here.

- **[2026-09-14] Dashboard UI session: mobile dropdown fix, iOS
  home-screen icon, badge icon/label redesign (Heat/Dry), Logs page
  severity filtering + download.** Six commits shipped directly to
  `main` (the `camera-streamer` branch existed but sat untouched at
  the commit it was created from throughout - see the branch-sync
  entry below).
  - **Mobile mode-select dropdown fix**: `.mode-options` was anchored
    with `right: 0`, which only positioned it correctly when the
    dropdown trigger sat at the right edge of a wide row - on narrow
    viewports where flex-wrap collapsed it to the screen's left edge,
    the panel rendered mostly off-screen to the left. Fixed with a
    `@media (max-width: 640px)` block forcing
    `.mode-dropdown { width: 100%; }` and
    `.mode-options { left: 0; right: 0; width: auto; }`. Verified via
    a real Flask instance + Playwright screenshot at mobile viewport
    width.
  - **iOS "Add to Home Screen" icon**: was showing a letter instead of
    the beetle icon. Root cause: iOS ignores `rel="icon"` (which the
    dashboard already had - a data-URI 🪲 SVG) for home-screen
    bookmarks and specifically looks for `rel="apple-touch-icon"`.
    Added `static/apple-touch-icon.png` (180x180, 🪲 rendered via
    headless Chromium + Noto Color Emoji, autocropped and centered on
    the app's `--bg` color rather than left transparent - iOS
    composites its own rounded-corner mask over a flat background,
    and transparent apple-touch-icons render inconsistently across
    iOS versions) and linked it from `base.html`. **Not yet confirmed
    on a real iOS device** - standard approach, but not yet visually
    checked on an actual iPhone home screen.
  - **Fan/Heat/Dry badge redesign** (the fan badge was the only
    animated one of the four, and didn't match the others):
    - Removed the spin animation entirely - `.badge-icon.spin` and its
      `@keyframes spin` deleted from `home.html`'s CSS, `outputBadge()`
      simplified to just toggle the badge's own `on`/`off` class
      (dropped the `iconId`/`spins` args it used to take; dimming the
      icon in the off state is handled purely by CSS off that class
      now).
    - Fan icon replaced with a custom-traced 4-blade fan PNG
      (`static/fan-icon.png`, blue blades / cyan hub), not an emoji.
      Took two rounds: first swapped the swirl (🌀) for a hand-fan
      emoji (🪭) by mistake before Ryan clarified he wanted an actual
      mechanical 4-blade fan shape and wanted to see options before
      anything was written; corrected by tracing a reference image he
      supplied via PIL alpha-masking, then splitting it into the
      two-tone blue/cyan version using a radius-based hub/blade
      boundary confirmed by actually measuring per-radius opacity in
      the source image (it's a solid disc hub, not the hollow ring the
      small preview looked like).
    - "Heater" badge label shortened to "Heat" (the `title` tooltip
      attribute left as "Heater").
    - "Dehum" badge relabeled "Dry" and its icon replaced with a
      hand-authored inline SVG (amber up-arrows over blue wavy water
      lines) instead of the swirl. Originally going to use a specific
      Flaticon icon Ryan linked, but Flaticon's free tier requires
      attribution (or a paid Premium account) - flagged that rather
      than embedding it, and Ryan chose an original icon in the same
      simple style instead. The badge's `id`/`title` attributes
      (`dehumBadge`/`dehumIcon`/"Dehumidifier") were deliberately left
      unchanged since nothing downstream keys off the visible label
      text.
  - **Logs page**: severity filter changed from exact-match to "at or
    above" (selecting Warning now also shows Error/Critical rows, the
    convention most log viewers use, instead of hiding them) -
    `get_recent_events()` in `shared_state.py` rewritten to filter on
    an `EVENT_LEVELS`-derived slice instead of a single value, and
    `limit` made optional (`None` = no `LIMIT` clause) to support a
    full export. Added `GET /api/events/download`, returning a
    plain-text log file (`Content-Disposition: attachment`) honoring
    whatever level filter is currently selected, plus a "Download log"
    button next to Refresh. Dropdown labels simplified from "Warning &
    above"/"Critical only" wording down to plain "Warning"/"Critical"
    per Ryan's request - behavior unchanged, just no longer spelled
    out in the label text. Verified with a real temp-SQLite-DB test
    harness plus a live Playwright-driven browser test against the
    actual `webapp.py` (caught and fixed a leftover duplicate
    `params.append(limit)` line from the old always-LIMIT code during
    that testing, which would have crashed the new `limit=None` export
    path with a param-count mismatch).

- **[2026-09-14] `camera-streamer` branch brought up to date with
  `main` for testing.** Confirmed directly from `.git/logs/HEAD` and
  both branches' own ref files (not just assumed from GitHub Desktop's
  UI) that this is a pure fast-forward, not a real merge:
  `camera-streamer` was created off `main` at commit `25bbffe...` and
  received zero commits of its own since - every checkout onto it in
  the reflog is `25bbffe`→`25bbffe`. All of today's six dashboard
  commits above happened directly on `main` after switching straight
  back to `main` post-creation, ending at `main`'s current tip,
  `ebc53f5...`. Since `camera-streamer`'s tip is a direct ancestor of
  `main`'s current tip with no divergent commits on either side,
  bringing it up to date is just moving the branch pointer forward -
  no merge conflicts are possible here. **To do it in GitHub
  Desktop**: switch to the `camera-streamer` branch, then Branch menu
  -> "Update from main" (GitHub Desktop will show it as a
  fast-forward, no merge commit created). Not yet done as of this
  entry - instructions given to Ryan, pending him running it locally.

- **[2026-09-14] Researched whether `camera-streamer` can serve both
  the live view AND the existing timelapse feature despite needing
  sole access to the camera device** - directly answers the open
  question raised in the "Camera / timelapse" load-bearing-decisions
  entry above (the "two-clients problem" paragraph). Fetched
  camera-streamer's actual docs (`docs/streaming.md`) rather than
  reasoning from the README alone: it exposes several HTTP endpoints
  off the one running daemon, not just the live stream -
  `/stream` (MJPEG), `/video`/`/video.mp4`/`/video.mkv` (H264),
  `/webrtc`, **and `/snapshot` - a single JPEG still-frame, documented
  as "works well everywhere."** This is real evidence for the
  direction already sketched in the load-bearing entry above:
  `camera-streamer` would be the only process that opens the actual
  V4L2 device, but that doesn't mean only one *feature* can use it -
  `camera_service.py`'s timelapse logic could stop opening the device
  itself entirely and instead HTTP GET `camera-streamer`'s `/snapshot`
  endpoint once per mode-driven capture interval, the same way
  `webapp.py` already treats the live view as an HTTP concern rather
  than a device concern. Still means rewriting `camera_service.py`'s
  capture loop and probably `discover_camera.py`'s start/stop dance
  too (nothing rewritten yet - this is confirmation the redesign is
  viable, not the redesign itself), but the core worry - "will we have
  to choose between live view and timelapse" - has a real answer now:
  no, both can be served off the one daemon over HTTP. **Not yet
  verified against a real running instance** - `docs/streaming.md` is
  documentation, not an observed response from an actual Pi; per the
  plan already set for this work (verify it runs standalone on the Pi
  first, then integrate against its actual observed behavior rather
  than documentation alone - see the entry above), still worth
  confirming `/snapshot` behaves as documented before committing to
  the rewrite.

- **[2026-09-14] Live feed reported "still isn't smooth"** despite the
  MJPG-capture-format fix's **[CONFIRMED, 2026-09-13]** entry above
  (in the camera fps discussion) showing real capture rate improved
  dramatically. Not necessarily a contradiction - worth flagging
  clearly rather than quietly explaining away: that confirmation was
  about *capture rate specifically* (frame-duplicate analysis on a
  screen recording of the raw feed), not the full delivery pipeline a
  browser viewer actually experiences. The load-bearing-decisions
  entry above already identifies two other stacked bottlenecks the
  MJPG fix never touched: `camera_service.py` still does 100% software
  JPEG encode (`cv2.imencode()`, no hardware acceleration at all), and
  `webapp.py` still relays it through Flask's built-in Werkzeug dev
  server (not a production WSGI server) via a disk-file handoff
  between two separate processes, once per connected browser tab every
  `STREAM_RELAY_INTERVAL`. Either of those could still produce visible
  choppiness even with a healthy capture rate underneath it. This is
  the concrete motivation for actually testing `camera-streamer` now
  rather than continuing to tune the existing pipeline further - it
  replaces the software encode with the Pi's hardware JPEG encoder and
  serves the stream itself instead of through a dev-server relay,
  addressing both remaining bottlenecks at once rather than one at a
  time.

- **[2026-09-17] `camera-streamer` switch actually implemented on the
  `camera-streamer` branch.** Ryan said explicitly not to worry about
  preserving `camera_service.py`/`webapp.py`'s old camera code as long
  as live view + timelapse both still work - this is a real rewrite of
  the camera pipeline, not an incremental patch. **Nothing in this
  entry has been run against real hardware yet** - see "What's actually
  verified" at the end before assuming any of this just works.

  **`camera_service.py`** no longer touches the USB device at all - the
  whole OpenCV capture loop (`open_capture()`, the watchdog/reopen
  logic, the per-frame JPEG encode) is gone. It now only wakes every
  `POLL_INTERVAL_SECONDS` (15s) to check whether the CURRENT mode's
  `snapshot_interval_minutes` is due, and if so pulls one still frame
  from `camera-streamer`'s own `http://127.0.0.1:<port>/snapshot`
  endpoint (plain `urllib`, no new dependency) instead of grabbing one
  itself. Session-boundary tracking, `maybe_compile_session()`/
  `compile_session_video()` (the ffmpeg concat-demuxer compile-on-
  mode-change logic), and the low-disk-space pruning safety net are
  all **unchanged** - none of that ever touched the capture device
  directly, only the DB/filesystem, so the camera-streamer switch
  doesn't touch it either.

  **`webapp.py`**: `/api/camera/stream.mjpg` (the old Flask-relayed
  MJPEG endpoint) and `/api/camera/latest.jpg` are both **removed** -
  the dashboard's live-view `<img>` now points straight at
  camera-streamer's own `/stream` endpoint instead, eliminating the
  disk-file-handoff-through-a-dev-server relay identified above as a
  real bottleneck. `/api/camera/status`'s `available` check changed from
  a local-file mtime check to an actual short-timeout (2s) HTTP request
  to camera-streamer's `/snapshot` - a real reachability check, not
  just "is the systemd unit active." It also now returns `stream_url`/
  `snapshot_url`, built from the INCOMING REQUEST's own `Host` header
  (`request.host`) rather than a hardcoded IP - since camera-streamer
  runs on the same Pi as webapp.py, just a different port, whatever
  hostname/IP the browser used to reach the dashboard is also how it
  can reach camera-streamer directly, no matter how the Pi's actually
  reached on the LAN (bare IP, mDNS name, etc.).

  **Port conflict, handled**: camera-streamer's documented default HTTP
  port is 8080 - the exact same port `webapp.py` already uses
  (`app.run(..., port=8080, ...)`). `config.json`'s new
  `camera.streamer_port` (default **8090**) moves it off that collision,
  and `validate_camera_settings()` now rejects 8080 outright rather than
  just documenting the conflict, so a typo here fails loudly at save
  time instead of quietly breaking whichever service starts second.

  **Config page (Camera card)**: JPEG-quality/live-capture-fps/
  stream-relay-fps fields are gone - those were `camera_service.py`'s
  own OpenCV settings, which no longer exist; quality/capture-rate are
  camera-streamer's own CLI flags now (see
  `docs/camera-streamer-setup.md`), not something this dashboard
  controls. Device/Resolution/Discover are unchanged in shape but now
  feed camera-streamer's config instead of `cv2.VideoCapture` - saving
  writes `camera-streamer.env` (new, gitignored, read by
  `systemd/camera-streamer.service` via `EnvironmentFile=`) and
  restarts `camera-streamer.service`, not `dermestid-camera.service`.
  Discover's stop/restart-around-the-probe dance now targets
  `camera-streamer.service` too, since that's the process holding the
  device open now - `dermestid-camera.service` (timelapse-only) never
  opens the device itself anymore, so it doesn't need stopping for
  discovery at all, one less moving part than before.

  **Real bug caught before shipping**: `climate.py` still called
  `state.get_camera_stats()` once per control cycle (to log
  `camera_actual_fps`/`camera_target_fps` into the readings table) after
  `get_camera_stats()`/`save_camera_stats()` were removed from
  `shared_state.py` - would have thrown `AttributeError` and crashed
  `climate.py`, the actually safety-critical process, on its very next
  cycle after deploy. Caught during a deliberate re-read of every file
  that referenced the removed functions before considering this done,
  not by running the code (can't, no real Pi here) - fixed by dropping
  the dead camera-fps sampling from `climate.py` entirely; those two
  readings-table columns are left in the schema (nullable, same
  "absence is unknown, not zero" treatment every other optional sensor
  here already gets) so old history stays visible on the Data page, they
  just stop getting new values from here on.

  **New: `systemd/camera-streamer.service`, `docs/camera-streamer-
  setup.md`.** The setup doc has real, sourced build steps (apt
  packages, `git clone --recursive`, `make && sudo make install`) and is
  explicit about the two things that could NOT be confirmed from
  camera-streamer's own docs/source as of this entry and need checking
  against real `camera-streamer --help` output on the Pi before trusting
  the unit file as-is: (1) the exact flag for changing the HTTP port off
  its 8080 default (guessed as `--http-port` - not found documented
  anywhere, only `--http-listen` for the bind address was confirmed);
  (2) whether `sudo make install` actually puts the binary at
  `/usr/local/bin/camera-streamer` (a reasonable guess for a `make
  install` C project, not something directly confirmed). Both are called
  out inline in the systemd unit's own comments, not just in the doc, so
  they're not easy to miss while actually setting this up.

  **What's actually verified**: `python3 -m py_compile` on every touched
  `.py` file (`shared_state.py`, `camera_service.py`, `webapp.py`,
  `discover_camera.py`, `climate.py`), a Jinja2 parse check on
  `home.html`/`config.html` (the two templates touched), and a careful
  read-through for every caller of everything removed (the
  `climate.py` bug above is exactly what that read-through was for).
  **What's NOT verified**: nothing has actually run against
  camera-streamer or a real camera - not `/snapshot`'s actual response
  shape, not whether `--camera-type=libcamera --camera-format=MJPEG` is
  really right for this project's specific USB webcam, not the guessed
  port flag, not a live Playwright/browser test of the new `<img>`
  pointing cross-port at camera-streamer. This is exactly the kind of
  thing this project's own stated plan for camera-streamer already
  called for verifying against real observed behavior before trusting
  documentation alone - next step is following `docs/camera-streamer-
  setup.md` on the actual Pi, start to finish, and correcting whatever
  in this entry turns out wrong once it's real.
