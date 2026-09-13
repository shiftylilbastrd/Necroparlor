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

- **`main` is current and complete.** The `ble-genericization` branch
  (full SensorPush→generic-BLE rebrand, the fixed-sensor-identity
  dashboard redesign, branch-selection in the update system, and
  several smaller fixes) has been merged into `main`, and the Pi has
  been switched back to tracking `main`.
- **`add-webcam` is now an active feature branch** (started 2026-09-12):
  adding an optional USB webcam - live view + per-mode timelapse. See
  the "Camera / timelapse" load-bearing-decisions section below. Not
  yet run against real hardware - see Open threads.
- **Pi 4 8GB migration decided but not yet executed** (2026-09-13): the
  project is moving from the Pi 3B+ to a spare Pi 4 8GB as the sole
  processor - user's own suggestion, given the project is still in
  development (rewiring/reflashing isn't costly right now), the Pi 4
  has far more headroom for the camera work (including `camera-streamer`
  if that gets integrated later), GPIO pinout is identical across the
  3/4 family so sensor/relay wiring doesn't change, and the Pi 4's more
  robust power delivery may also resolve the Tier-3 BLE/under-voltage
  issue below (shared power rail theory). **Plan is to move the existing
  SD card into the Pi 4, not re-flash/re-clone** - Raspberry Pi OS
  images auto-detect the board via device-tree at boot and support the
  whole 3/4/Zero2 family from one image, so this should just work.
  Pending user confirmation after doing this "in the morning": check
  `cat /proc/device-tree/model` (should now say Pi 4), `vcgencmd
  get_throttled` (watching specifically for the under-voltage bits
  clearing now that it's off the 3B+'s power budget), and that all
  three services (`dermestid-climate`, `dermestid-ble`,
  `dermestid-web`) came up clean. **`camera-streamer` (ayufan's, a
  native binary alternative to the hand-rolled MJPEG relay - see the
  superseded live-view entry below for why it wasn't used initially) is
  deliberately not yet integrated into the dashboard** - the plan is to
  verify it runs standalone on the Pi 4 first, then write dashboard
  integration code against its actual observed API behavior, not
  against documentation alone.
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
  the Pi 4 too. **Separately noticed the same session**:
  `dermestid-battery.service`'s log showed `"No SensorPush address
  configured - nothing to check"` - `config.json`'s `ble_mac` was empty
  at check time, which is why the dashboard's battery reading had gone
  stale (56hr) independent of the listener's stuck state - not a timer/
  service bug, just needs the address re-set (Config page's External
  card, or the new Discover button once the listener is healthy again).
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
