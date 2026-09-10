# Dermestid Enclosure Climate Control

Files:

- `climate.py` — main control loop (heater, fan/vent servo, dehumidifier, door light). Run this on the Pi at all times.
- `webapp.py` — local Flask dashboard: view live readings/history, switch modes, edit setpoints.
- `shared_state.py` — shared config + SQLite helpers used by both of the above. Must live in the same folder as them.
- `templates/` — the dashboard's pages: `base.html` (shared nav/layout), `home.html` (live status + chart + mode), `logs.html` (event log), `config.html` (setpoints + sensor source).
- `config.json` — current mode + per-mode setpoints. Auto-created if missing; edit by hand or through the dashboard.
- `sensorpush_listener.py` — optional background service that listens for SensorPush BLE sensors and feeds one of them into `climate.py` as the external reading, instead of a wired probe. Has a self-watchdog: BLE scans can silently stall after many hours of continuous operation without crashing (a known real-world BlueZ/bleak issue) - if 10 minutes pass with no reading actually decoded, it exits deliberately so systemd's `Restart=on-failure` brings it back up fresh rather than sitting there doing nothing indefinitely.
- `sensorpush_battery.py` — optional one-shot script, run daily by a systemd timer, that briefly connects to the external SensorPush sensor to check its battery level (battery isn't in the passive broadcast).
- `discover_sensorpush.py` — one-time helper to find your SensorPush sensors' BLE addresses.
- `systemd/*.service`, `systemd/*.timer` — units so everything starts on boot, restarts if it crashes, and the battery check runs on schedule.

## Install

```bash
sudo apt update
sudo apt install python3-pip python3-rpi.gpio
pip3 install flask adafruit-circuitpython-dht adafruit-circuitpython-sht31d --break-system-packages
```

`adafruit-circuitpython-dht` is what actually reads the DHT22/AM2302 sensors now — the older `Adafruit_DHT` package this project used to depend on has been deprecated and archived by Adafruit, and its installer fails outright on newer Raspberry Pi OS releases like Trixie (its build-time Pi-detection code doesn't recognize them). `adafruit-circuitpython-dht` is the actively-maintained CircuitPython/Blinka-based replacement Adafruit points people to instead, and it's a drop-in swap in this codebase — no other files needed to change.

**Internal sensor: DHT22/AM2302 (default) or SHT31 (optional upgrade)**

The internal reading defaults to a wired DHT22/AM2302, same idea as the external probe. Wire it like this:

| DHT22/AM2302 pin | Pi pin |
|---|---|
| VCC | 5V (pin 2) |
| GND | GND (pin 6) |
| DATA | GPIO27 (pin 13) |

If your sensor is a bare DHT22 chip (not a breakout module), add a 4.7kΩ–10kΩ pull-up resistor between DATA and VCC — most AM2302 modules (the ones in a small plastic housing with a 3-pin connector) already have this built in, so check before adding a second one.

If you get an SHT31 later, it's a drop-in swap with no code changes: enable I2C, wire it up, and flip "Internal sensor source" to SHT31 on the Config page. The SHT31 also unlocks a condensation-recovery feature the DHT22 can't do (see below) since it has an onboard heater and the DHT22 doesn't.

```bash
sudo raspi-config
# Interface Options -> I2C -> Enable, then reboot
```

| SHT31 pin | Pi pin |
|---|---|
| VIN | 3.3V (pin 1) |
| GND | GND (pin 6) |
| SCL | GPIO3 / SCL (pin 5) |
| SDA | GPIO2 / SDA (pin 3) |

Copy this whole folder to the Pi, e.g. `/home/pi/dermestid/`.

## Run manually (for testing)

```bash
cd /home/pi/dermestid
python3 climate.py      # in one terminal
python3 webapp.py       # in another
```

Then visit `http://<pi-ip-address>:8080` from any phone/laptop on your LAN. **There's no login on this dashboard** — it's fine on your home network, but don't port-forward it to the internet.

## Optional: external reading over Bluetooth (SensorPush)

If your external probe is a SensorPush HT1/HT.w/HTP.xw sensor instead of a wired DHT/AM2302, the Pi's onboard Bluetooth can read it directly — no gateway, hub, or extra hardware needed. This is entirely passive listening (no pairing, no connection), so it doesn't touch the sensor's battery budget.

```bash
pip3 install sensorpush-ble bleak --break-system-packages
```

Requires Python 3.11+, which is the default on current Raspberry Pi OS (Bookworm).

1. **Find your sensor's BLE address.** All SensorPush HT1 units broadcast under the same generic name ("s"), so the only way to tell three of them apart is by address:
   ```bash
   python3 discover_sensorpush.py
   ```
   Warm the one you want in your hand and watch which address's temperature climbs, then note that address (looks like `AA:BB:CC:DD:EE:FF`). Ctrl+C to stop.

2. **Point `climate.py` at it**, either from the dashboard (new "External sensor source" panel — pick SensorPush, paste the address, Save) or by hand-editing `config.json`:
   ```json
   "external_source": "sensorpush",
   "sensorpush_mac": "AA:BB:CC:DD:EE:FF"
   ```

3. **Run the listener** alongside the other two processes:
   ```bash
   python3 sensorpush_listener.py
   ```
   `climate.py` re-reads `config.json` every cycle same as always, so it'll start pulling the external reading from the BLE listener within one 15s cycle of you saving the change — no restart needed. If you switch `external_source` back to `local_gpio`, it falls right back to the wired probe.

**Both external sources are read every cycle, regardless of which one is active.** Whichever one isn't currently driving control decisions is shown on the dashboard's External tile as "Fallback (..., not active)" - purely for visibility, so a dead fallback probe (e.g. a wired probe kept connected as backup while SensorPush is the primary) is noticed the moment it stops working, not discovered mid-outage on the day you actually need it. The fallback reading never affects any control decision or the sensor-failure failsafe - a bad or missing fallback reading just shows as blank on the dashboard for that cycle.

Things worth knowing:
- **Range through metal ductwork is the main risk.** Test placement with `discover_sensorpush.py` running before you seal the sensor into the vent — ductwork can attenuate the signal more than open air.
- **Advertisements can occasionally pause** until something (the SensorPush app, or another BLE connection) "wakes" the sensor — this is a known SensorPush quirk, not a bug in this integration. Losing the external reading alone no longer shuts anything down (see "Sensor-failure failsafe" below) - it just pauses thermal cooling specifically until a fresh reading comes back, while heating and dehumidifying keep running on internal data.
- The old wired external-probe wiring (`PIN_EXTERNAL_TEMP`, GPIO4) is left intact and unused in this mode, so you can switch back any time without touching hardware.

### Battery level (daily check)

Battery level isn't in the passive advertisement — SensorPush only exposes it over a brief active Bluetooth connection. `sensorpush_battery.py` does exactly that, once a day by default, then disconnects immediately:

```bash
sudo cp systemd/dermestid-battery.service systemd/dermestid-battery.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dermestid-battery.timer
```

Check it ran, or run it once by hand to test:
```bash
systemctl list-timers dermestid-battery.timer
python3 sensorpush_battery.py
```

The result (percentage, voltage, and how long ago it was checked) shows up on the dashboard next to the external sensor's temperature, and a warning event is logged if the battery drops to 15% or below. The check is a no-op if `external_source` isn't set to SensorPush, so it's safe to enable even before you've configured an address.

A couple of things worth knowing:
- The HT1 only accepts **one** BLE connection at a time. If the SensorPush phone app happens to be connected right when the daily check runs, that check simply fails and retries the next day — no crash, just a logged warning.
- The percentage is a rough estimate from a linear voltage curve (3.1V full, 2.1V empty for the CR2032 it takes), not a precise fuel gauge — treat it as a "getting low, plan a swap" signal rather than an exact number.
- Daily is a sensible default given how slowly coin cells drain, but you can change the schedule by editing `OnCalendar=` in `dermestid-battery.timer` (e.g. `OnCalendar=weekly`).

## Run permanently (recommended)

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dermestid-climate.service
sudo systemctl enable --now dermestid-web.service
# only if you're using a SensorPush external sensor:
sudo systemctl enable --now dermestid-sensorpush.service
# optional daily battery check, see below:
sudo systemctl enable --now dermestid-battery.timer
```

Check status/logs:
```bash
systemctl status dermestid-climate.service
journalctl -u dermestid-climate.service -f
tail -f /home/pi/dermestid/logs/climate.log
```

The unit files assume the folder is at `/home/pi/dermestid` and the user is `pi` — edit `WorkingDirectory`/`ExecStart`/`User` if yours differs.

## Optional: update notifications + one-click apply

By default, getting a code change onto the Pi means `git pull` + restarting the affected service by hand every time. This project can now tell you when an update is waiting and apply it with one click, instead.

**Checking for updates works out of the box, no setup needed.** `webapp.py` runs a background check every 15 minutes by default (configurable on the Config page, 1–1440 minutes) — read-only, it only compares your local commit to GitHub's, never pulls or restarts anything by itself. When it finds something new, a banner appears at the top of every page (Home, Logs, Config) linking to the Config page, which also shows the specific commit message and an **"Update now"** button.

**Actually applying an update — either via that button, or the fully-hands-off timer below — needs a one-time permission setup**, since both ultimately restart services without anyone there to type a password:

```bash
sudo visudo -f /etc/sudoers.d/dermestid
```

Paste this in, save, and exit:
```
pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart dermestid-climate.service
pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart dermestid-web.service
pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart dermestid-sensorpush.service
```

(Run `which systemctl` first and double-check it matches `/usr/bin/systemctl` — if your system has it somewhere else, use that exact path instead, since `sudoers` rules must match exactly.)

```bash
chmod +x auto_update.sh
```

That's it for the button — the Config page's "Update now" runs `auto_update.sh` for you (as a detached background process, so it survives the web service restarting itself partway through — genuinely tested, not just assumed to work).

**If you'd rather it apply automatically with no click at all**, there's still the fully-hands-off timer option:

```bash
sudo cp systemd/dermestid-autoupdate.service systemd/dermestid-autoupdate.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dermestid-autoupdate.timer
```

This runs the same `auto_update.sh` every 5 minutes on its own — no banner needed, no button to click, it just happens. Check it's working:
```bash
systemctl list-timers dermestid-autoupdate.timer
journalctl -u dermestid-autoupdate.service -n 20
```

Or trigger it once immediately:
```bash
sudo systemctl start dermestid-autoupdate.service
journalctl -u dermestid-autoupdate.service -n 20
```

A few things worth knowing, whichever way you apply an update:
- If you've customized settings through the dashboard (setpoints, sensor source), `config.json` has local changes that aren't committed to git. The script stashes those before pulling and restores them right after, so they survive an update — this is tested, not just assumed. If an incoming update ever touches the exact same part of `config.json` your local changes touched, the automatic restore can fail; the script logs a clear warning if that happens, and `git stash list` on the Pi will have your changes waiting to be sorted out by hand.
- Before touching any files, the script verifies passwordless sudo actually works and bails out cleanly with a clear error if it doesn't — rather than pulling new code and then discovering it can't restart the services to run it, leaving you in a half-updated state.
- Anything pushed to your GitHub repo can end up running on the Pi (within the check interval, or on your next button click). Since only your own GitHub account can push to it, that's the same level of trust as "I trust my own account," but worth being aware of.
- Restarting `dermestid-climate.service` briefly interrupts climate control for a couple of seconds each time an update actually lands - not meaningfully different from restarting it by hand, just automatic now.

## What changed from your original script

**Safety fixes:**
- **Sensor sanity checking** — readings outside a physically-possible range, or that jump more than `MAX_DELTA_TEMP`/`MAX_DELTA_HUMIDITY` from the last good reading in one cycle, are now rejected as DHT glitches instead of trusted.
- **Sensor-failure failsafe** — this only applies to the *internal* sensor (temp or humidity): if no valid internal reading comes in for `SENSOR_FAIL_TIMEOUT` (90s), everything (heater, fan, dehumidifier) is forced off and an alarm event is logged, instead of leaving outputs in whatever state they were last in. Every control decision fundamentally depends on internal readings, so there's no safe degraded mode there. The *external* sensor is different: losing it doesn't stop the internal-only decisions (heating, dehumidifying, the scheduled cleaning-mode vent) since none of them need it - it only pauses thermal cooling specifically, since that's the one decision that genuinely can't be made safely without knowing whether outside air would actually help (running the fan blind could just import hotter air). This matters in practice if you keep a wired probe connected as a manual fallback for SensorPush: a garage-placed wired sensor being less accurate than true outdoor air is a fine tradeoff for a fallback role, since the system only needs the rough direction ("meaningfully cooler out or not") to make safe cooling decisions.
- **Glitch-vs-real-change recovery** — the anti-glitch filter (rejects a reading that jumps too far from the last accepted one) has a self-recovery mechanism: if several consecutive rejected readings keep landing consistently close to *each other*, even though they all differ from the old accepted value, that's treated as a genuine sustained change (real drift) rather than sensor noise, and gets accepted as the new baseline after a few consistent readings in a row. Without this, a real gradual temperature change that happened to exceed the per-cycle jump limit would get compared forever against an ever-more-stale frozen reference point and never be accepted again - which is exactly what happened once in practice before this was added, triggering a real emergency shutdown that then never recovered on its own until the service was restarted.
- **Heater runtime cutoff** — the heater can no longer run continuously for more than `HEATER_MAX_ON_SECONDS` (20 min) without reaching setpoint; it cuts off, logs a warning, and locks out for 5 minutes before it's allowed to retry. Same pattern applied to the fan for motor protection.
- **Heat/cool mutual exclusion** — if genuine thermal cooling and a heat request land in the same cycle, cooling wins and heating is skipped that cycle, with a logged warning, so they can't fight each other. This does *not* apply to the cleaning-mode scheduled ventilation cycle, which is a separate, deliberate carve-out: that vent fires on a fixed schedule purely to flush air quality, not because it's hot, so heat is allowed to run right alongside it if it's genuinely cold - otherwise a routine air-quality flush could cause a real temperature dip that has nothing to do with why the fan turned on.
- **Crash resilience** — an unexpected exception inside the control loop is now caught, logged, and the loop continues on the next cycle instead of taking the whole service down. `systemd` with `Restart=on-failure` is a second layer of defense on top of that.
- **Clean shutdown on `systemctl stop`/restart** — added a `SIGTERM` handler so GPIO cleanup (closing the servo, turning outputs off) actually runs when systemd stops or restarts the service, not just on Ctrl+C.
- **`PIN_LIGHT` moved from GPIO15 to GPIO26** — GPIO15 doubles as UART0 RXD, which caused unpredictable behavior on Pis with the serial console enabled. Rewire the door/light circuit to GPIO26, or edit `PIN_LIGHT` in `climate.py` back if you'd rather disable the serial console instead (`sudo raspi-config` → Interface Options → Serial Port → login shell off, hardware enabled off).
- **Logging** — switched from bare `print()` to Python's `logging` module with a rotating file (`logs/climate.log`, 5 x 2MB), plus every meaningful state change/alarm is also written to the SQLite `events` table so it shows up in the dashboard.

**New: activity modes**

`config.json` now holds three modes, each with its own temperature/humidity setpoints. `climate.py` re-reads this file every 15-second cycle, so a mode change from the dashboard takes effect almost immediately.

| Mode | Behavior |
|---|---|
| **Dormant** | Lower temperature band (55–60°F default) to slow the colony's metabolism — less feeding, less breeding, useful when you don't want them actively working. |
| **Ready** | Warmer/humid band (78–85°F, 50% RH default) for active feeding and breeding. |
| **Cleaning** | Same thermal targets as Ready, plus a scheduled forced-ventilation cycle (defaults: 5 minutes of fan+servo-open every 30 minutes) that runs regardless of temperature, to keep odor from building up in the ducted intake/exhaust while a job is in progress. |

The default numbers are a reasonable starting point, not a substitute for your own care-sheet — dermestid tolerances vary a bit by species and colony size, so watch how yours responds over the first week or two and tune from the dashboard.

**Web dashboard — three pages**
- **Home** (`/`) — purely informational: live internal/external temp, humidity, relay states (refreshing every 5s), a mode dropdown (Dormant/Ready/Cleaning), and the history graph (6h/24h/7d/30d) of internal temp, external temp, and humidity, downsampled server-side so long ranges stay fast.
- **Logs** (`/logs`) — the full event log (mode changes, relay on/off, warnings, alarms) with a level filter (info/warning/error/critical) and a "Load more" button for paging further back. Auto-refreshes only while you're on the newest page, so paging back doesn't get yanked out from under you.
**Config** (`/config`) — per-mode setpoint editing (with server-side range/sanity validation — e.g. it won't let you set low temp ≥ high temp, or a vent duration longer than the interval), the internal sensor source switch (DHT22 vs SHT31), and the external sensor source switch (wired probe vs SensorPush).

All three share the same `/api/*` endpoints as before; `/api/events` now also accepts `level` and `before` query params for the logs page's filtering and pagination.

**Internal sensor: switchable between DHT22 and SHT31**

`internal_source` in `config.json` (or the Config page dropdown) picks which sensor `climate.py` reads for the internal temp/humidity — `dht22` (default, GPIO27) or `sht31` (I2C). Switching takes effect within one control cycle, no restart needed. Whichever one isn't selected is never touched — with `dht22` selected, `climate.py` doesn't import or call anything I2C-related at all, so there's no risk of it trying to talk to hardware that isn't there.

If you're running the SHT31: it's a genuine upgrade — tighter accuracy (±0.2°C/±2%RH vs the DHT22's ±0.5°C/±2-5%RH) and, more importantly for this enclosure, an onboard heater it can use to dry itself off after condensation. With humidity actively held around 50% and a dehumidifier cycling, condensation on the sensor element is a realistic failure mode; the DHT22 has no way to recover from that on its own.

`climate.py` watches for internal humidity pegged at or above 99% for more than a minute (SHT31 mode only — this check is skipped entirely on DHT22, which has no heater to pulse) and, when it sees that, pulses the SHT31's heater for 10 seconds (rate-limited to once per 10 minutes) rather than just reporting garbage until it dries out on its own. This check runs on the raw reading *before* the delta-glitch filter, deliberately — a real condensation event can jump straight to ~100% faster than the filter's normal tolerance, and if the recovery logic only looked at filtered readings, a real condensation event would look identical to a dead sensor and slide straight into the emergency-shutdown failsafe instead of ever getting a chance to dry out. The delta-filtered value is still the only thing the actual heat/cool/dehumidify decisions act on, so control quality isn't affected — only the recovery trigger sees the raw value.

The external probe fallback (`local_gpio` mode, wired DHT22/AM2302 on GPIO4) is untouched and still available if you ever switch off SensorPush.

## Tuning knobs that stay hardcoded (on purpose)

Things like hysteresis bands, the sensor-failure timeout, the heater/fan runtime cutoffs, and the SHT31 condensation-recovery thresholds live as constants at the top of `climate.py` rather than in the web UI — they're safety guardrails, not day-to-day settings. If you want to adjust them, edit the constants directly and restart the service.
