#!/usr/bin/env python3
"""
Local dashboard for the dermestid enclosure.

Run with: python3 webapp.py
Then visit http://<pi-ip-address>:8080 from any device on your LAN.

There is NO authentication on this - it's meant for your home network
only. Don't port-forward it to the internet.
"""
import time
import datetime
import os
import json
import shutil
import subprocess
import threading
import logging
import fcntl
import urllib.error
import urllib.request

from flask import Flask, jsonify, request, render_template, send_file, abort, Response

import shared_state as state

app = Flask(__name__)

# [2026-09-14] Live view switched to camera-streamer (see
# docs/camera-streamer-setup.md and PROJECT_STATUS.md) - webapp.py no
# longer relays live frames itself (the dashboard's <img> points
# straight at camera-streamer's own /stream now), so
# CAMERA_STALE_MULTIPLIER/STREAM_RELAY_FPS_DEFAULT, which sized that
# relay, are gone. api_camera_status() below now checks reachability
# with a quick direct request to camera-streamer instead.
CAMERA_STREAMER_STATUS_TIMEOUT_SECONDS = 2

# How recent a BLE reading has to be to show up in the Config page's
# "Discover" list - generous relative to how often sensors actually
# broadcast (a few seconds to a minute depending on brand), so a device
# that's still physically present doesn't drop off the list between
# clicks, while a sensor that was removed or had its battery pulled
# eventually stops cluttering it.
BLE_DISCOVER_FRESHNESS_SECONDS = 600


@app.route("/")
def home():
    return render_template("home.html", active_page="home")


@app.route("/logs")
def logs_page():
    return render_template("logs.html", active_page="logs")


@app.route("/data")
def data_page():
    return render_template("data.html", active_page="data")


@app.route("/config")
def config_page():
    return render_template("config.html", active_page="config")


@app.route("/timelapse")
def timelapse_page():
    return render_template("timelapse.html", active_page="timelapse")


@app.route("/api/status")
def api_status():
    config = state.load_config()
    latest = state.get_latest_reading()
    stale = bool(latest) and (time.time() - latest["ts"]) > 90

    ble_status = None
    if config.get("ble_mac"):
        ble = state.get_ble_reading(config["ble_mac"])
        if ble:
            ble_status = {
                "temp_f": ble["temp_f"],
                "humidity": ble["humidity"],
                "rssi": ble["rssi"],
                "age_seconds": time.time() - ble["ts"],
                "battery_pct": ble["battery_pct"],
                "battery_voltage": ble["battery_voltage"],
                "battery_age_seconds": (time.time() - ble["battery_ts"]) if ble["battery_ts"] else None,
            }

    door = state.get_door_state()
    door_status = None
    if door:
        door_status = {"is_open": door["is_open"], "age_seconds": time.time() - door["ts"]}

    last_valid = state.get_last_valid_timestamps()
    disk_free_gb = state.get_disk_free_gb()

    return jsonify({"config": config, "latest": latest, "stale": stale,
                     "ble_status": ble_status, "door_status": door_status,
                     "last_valid_timestamps": last_valid,
                     "disk_free_gb": disk_free_gb})


@app.route("/api/light-override", methods=["POST"])
def api_light_override():
    """Manually turns the door/lid light on (or clears an existing
    override to turn it back off early), for the Home page's live-view
    icon overlay - lets you check in on the enclosure without physically
    opening the lid. Purely a config.json write: climate.py's light_loop()
    is what actually drives the GPIO pin (already systemd-controlled, no
    sudo/subprocess needed here, unlike the camera/update routes), reading
    this value back at most once a second. The physical door switch
    always wins over this either way - see light_loop()'s docstring.

    `on: true` sets the override LIGHT_OVERRIDE_DURATION_SECONDS into the
    future (self-expiring, not something that needs a separate "turn off"
    call to ever happen); `on: false` clears it immediately, letting the
    light drop back to just following the door switch on light_loop()'s
    next poll (up to LIGHT_OVERRIDE_POLL_SECONDS later)."""
    body = request.get_json(force=True, silent=True) or {}
    on = bool(body.get("on"))
    config = state.load_config()
    config["light_override_until"] = (time.time() + state.LIGHT_OVERRIDE_DURATION_SECONDS) if on else 0
    state.save_config(config)
    if on:
        state.log_event("info", "Light manually turned on from the dashboard "
                                 f"(auto-expires in {state.LIGHT_OVERRIDE_DURATION_SECONDS // 60} min)")
    else:
        state.log_event("info", "Light manual override cleared from the dashboard")
    return jsonify({"light_override_until": config["light_override_until"]})


@app.route("/api/ble-sensors")
def api_ble_sensors():
    """All BLE addresses currently being heard, for picking which one
    to wire in as the external sensor."""
    return jsonify(state.get_all_ble_readings())


def _restart_service(unit_name):
    """Best-effort restart of a dermestid systemd unit (sudo -n,
    non-interactive - same sudoers pattern as everywhere else this
    dashboard shells out to systemctl), used right after saving a
    setting that a service only reads once at its own startup, so the
    change takes effect immediately instead of leaving the user to
    restart it by hand. Deliberately swallows any failure into a plain
    False rather than raising: the setting itself is always saved
    either way (this runs after state.save_config(), never blocking
    it), a missing sudoers entry on a fresh install just means the
    caller reports "saved, but the service needs a restart" instead of
    silently claiming success. Returns True only on a confirmed
    zero-exit restart."""
    try:
        result = subprocess.run(["sudo", "-n", "systemctl", "restart", unit_name],
                                 capture_output=True, timeout=15)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@app.route("/api/ble-sensor-types")
def api_ble_sensor_types():
    """The supported BLE sensor brand registry, for populating the
    Config page's brand dropdown."""
    return jsonify({key: info["label"] for key, info in state.BLE_SENSOR_LIBRARIES.items()})


@app.route("/api/ble-mac", methods=["POST"])
def api_set_ble_mac():
    """Sets which physical BLE unit to listen for, and which brand's
    decoder to use. Not a source toggle - external sensor failover is
    automatic (see climate.py) - this just identifies which BLE address
    is the right one (useful if more than one matching unit is nearby)
    and which library decodes its advertisements.

    The address itself (`ble_mac`) needs no restart - only climate.py
    and this dashboard ever read it, and both re-load config.json fresh
    every cycle/request; ble_listener.py doesn't even look at it (it
    listens for every device matching the selected brand's decoder,
    not just one address - see get_all_ble_readings()/the Discover
    button). The brand (`ble_sensor_type`) is different: ble_listener.py
    reads it once at startup to pick which decoder to import, so a
    brand change is restarted automatically here rather than leaving
    the user to do it by hand."""
    body = request.get_json(force=True, silent=True) or {}
    mac, error = state.validate_ble_mac(body.get("ble_mac"))
    if error:
        return jsonify({"error": error}), 400
    sensor_type, error = state.validate_ble_sensor_type(body.get("ble_sensor_type", "sensorpush"))
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    brand_changed = config.get("ble_sensor_type") != sensor_type
    config["ble_mac"] = mac
    config["ble_sensor_type"] = sensor_type
    state.save_config(config)
    restarted = _restart_service("dermestid-ble.service") if brand_changed else False
    state.log_event("info", f"BLE sensor address {'set to ' + mac if mac else 'cleared'} "
                             f"(brand: {state.BLE_SENSOR_LIBRARIES[sensor_type]['label']})" +
                             (" - listener restarted automatically" if brand_changed and restarted else
                              " - automatic restart failed, restart dermestid-ble.service by hand"
                              if brand_changed else ""))
    response = dict(config)
    response["restart_attempted"] = brand_changed
    response["restart_ok"] = restarted
    return jsonify(response)


@app.route("/api/ble-discover")
def api_ble_discover():
    """Clean, read-only view of the BLE sensors ble_listener.py's own
    already-running scan has heard recently - for the Config page's
    Discover button, so finding a sensor's address doesn't require SSHing
    in and running discover_ble_sensor.py from the CLI.

    Deliberately NOT a new scan of its own: ble_listener.py already saves
    a DB row for every matching-brand device it decodes an advertisement
    from (state.get_all_ble_readings(), "useful for the discovery helper"
    per its own docstring - this route is that helper). Spinning up a
    second, independent BleakScanner from a web request would instead
    have this process and the persistent listener both opening/closing
    BLE discovery sessions against the same BlueZ adapter - exactly the
    kind of scan start/stop churn that's already caused a real stuck-
    bluetoothd incident on this Pi once (see PROJECT_STATUS.md's Tier-3
    BLE entry). Reading the existing listener's own data avoids that
    risk entirely, at the cost of only showing devices the brand's
    decoder can actually parse (same limitation discover_ble_sensor.py
    already has - it uses the identical decoder)."""
    cutoff = time.time() - BLE_DISCOVER_FRESHNESS_SECONDS
    readings = [r for r in state.get_all_ble_readings() if r["ts"] and r["ts"] >= cutoff]
    for r in readings:
        r["age_seconds"] = time.time() - r["ts"]
    readings.sort(key=lambda r: r["age_seconds"])
    return jsonify(readings)


@app.route("/api/calibration", methods=["POST"])
def api_set_calibration():
    """Saves per-sensor calibration offsets. Tied to physical sensor
    identity (internal/ble/wired), not the dynamic active/
    fallback role, since which physical sensor plays which role can
    swap automatically during a BLE sensor outage."""
    body = request.get_json(force=True, silent=True) or {}
    cleaned, error = state.validate_calibration(body)
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    config["calibration"] = cleaned
    state.save_config(config)
    state.log_event("info", "Sensor calibration offsets updated")
    return jsonify(config)


@app.route("/api/internal-source", methods=["POST"])
def api_set_internal_source():
    body = request.get_json(force=True, silent=True) or {}
    source = body.get("source")
    if source not in ("dht22", "sht31"):
        return jsonify({"error": "source must be 'dht22' or 'sht31'"}), 400
    config = state.load_config()
    config["internal_source"] = source
    state.save_config(config)
    state.log_event("info", f"Internal sensor source changed to '{source}'")
    return jsonify(config)


@app.route("/api/camera/status")
def api_camera_status():
    """[2026-09-14] `available` used to be a staleness check on
    camera/latest.jpg's mtime (the file camera_service.py wrote every
    capture). Now that camera-streamer serves live view directly (see
    docs/camera-streamer-setup.md), there's no local file to check the
    age of - this makes a real, short-timeout request to
    camera-streamer's own /snapshot endpoint instead. A quick real check
    rather than just trusting "the process is running": camera-streamer
    can be running but still not delivering frames (device unplugged,
    wedged after a USB fault), the same failure mode the old file-based
    check was built to catch.

    Also returns stream_url/snapshot_url, built from THIS REQUEST's own
    Host header rather than a hardcoded IP - camera-streamer runs on the
    same Pi as webapp.py, just a different port (config.json's
    camera.streamer_port), so whatever hostname/IP the browser used to
    reach the dashboard is also how it can reach camera-streamer
    directly. This is what templates/home.html points its live-view
    <img> at.
    """
    config = state.load_config()
    cam_cfg = config.get("camera", {})
    port = cam_cfg.get("streamer_port", 8090)
    host = request.host.split(":")[0]
    snapshot_url = f"http://{host}:{port}/snapshot"
    stream_url = f"http://{host}:{port}/stream"

    available = False
    try:
        req = urllib.request.Request(snapshot_url, method="GET")
        with urllib.request.urlopen(req, timeout=CAMERA_STREAMER_STATUS_TIMEOUT_SECONDS) as resp:
            available = resp.status == 200
    except (urllib.error.URLError, OSError):
        available = False

    disk = shutil.disk_usage(state.BASE_DIR)
    return jsonify({
        "available": available,
        "stream_url": stream_url,
        "snapshot_url": snapshot_url,
        "camera": cam_cfg,
        "current_mode": config.get("current_mode"),
        "snapshot_interval_minutes": config.get("modes", {}).get(config.get("current_mode"), {}).get("snapshot_interval_minutes", 0),
        "snapshot_count": state.get_camera_snapshot_count(),
        "disk_free_mb": disk.free / (1024 * 1024),
    })


@app.route("/api/camera/snapshots")
def api_camera_snapshots():
    """Paginated timelapse gallery listing, same before_ts cursor
    pattern as /api/events and /api/readings-table."""
    limit = request.args.get("limit", default=50, type=int)
    before = request.args.get("before", default=None, type=float)
    snapshots = state.get_camera_snapshots(limit=limit, before_ts=before)
    for s in snapshots:
        s["url"] = f"/api/camera/snapshot/{int(s['ts'])}.jpg"
    return jsonify(snapshots)


@app.route("/api/camera/snapshot/<int:ts>.jpg")
def api_camera_snapshot(ts):
    row = state.get_camera_snapshot(ts)
    if not row or not os.path.exists(os.path.join(state.CAMERA_TIMELAPSE_DIR, row["filename"])):
        abort(404)
    return send_file(os.path.join(state.CAMERA_TIMELAPSE_DIR, row["filename"]), mimetype="image/jpeg")


@app.route("/api/timelapse/videos")
def api_timelapse_videos():
    """Paginated compiled-video gallery listing for the Timelapse page -
    same before_ts cursor pattern as /api/events and /api/camera/snapshots."""
    limit = request.args.get("limit", default=24, type=int)
    before = request.args.get("before", default=None, type=float)
    videos = state.get_timelapse_videos(limit=limit, before_ts=before)
    for v in videos:
        v["url"] = f"/api/timelapse/video/{v['id']}.mp4"
        v["poster_url"] = f"/api/timelapse/poster/{v['id']}.jpg" if v["poster_filename"] else None
    return jsonify(videos)


@app.route("/api/timelapse/pending")
def api_timelapse_pending():
    """Stats for the Timelapse page's header, about the CURRENT,
    not-yet-compiled session only (not a lifetime total) - how many
    frames camera_service.py has pulled from camera-streamer so far,
    how long a video would be if that session compiled right now, and
    how much disk space they're using. See
    shared_state.get_pending_timelapse_stats for why a plain directory
    scan is both correct and cheap here."""
    return jsonify(state.get_pending_timelapse_stats())


@app.route("/api/timelapse/video/<int:video_id>.mp4")
def api_timelapse_video_file(video_id):
    row = state.get_timelapse_video(video_id)
    if not row:
        abort(404)
    path = os.path.join(state.CAMERA_TIMELAPSE_VIDEOS_DIR, row["filename"])
    if not os.path.exists(path):
        abort(404)
    # as_attachment lets this same URL serve both inline <video> playback
    # (browsers ignore Content-Disposition for that) and the gallery's
    # "Download" link/button - no separate download-only route needed.
    return send_file(path, mimetype="video/mp4", as_attachment=False, download_name=row["filename"])


@app.route("/api/timelapse/poster/<int:video_id>.jpg")
def api_timelapse_poster(video_id):
    row = state.get_timelapse_video(video_id)
    if not row or not row["poster_filename"]:
        abort(404)
    path = os.path.join(state.CAMERA_TIMELAPSE_VIDEOS_DIR, row["poster_filename"])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/timelapse/video/<int:video_id>", methods=["DELETE"])
def api_timelapse_video_delete(video_id):
    if not state.delete_timelapse_video(video_id):
        abort(404)
    state.log_event("info", f"Timelapse video #{video_id} deleted via dashboard")
    return jsonify({"deleted": video_id})


@app.route("/api/timelapse/videos/delete-many", methods=["POST"])
def api_timelapse_videos_delete_many():
    """Bulk delete for the gallery's multi-select checkboxes - one call
    instead of the browser firing N separate DELETE requests."""
    body = request.get_json(force=True, silent=True) or {}
    id_list = body.get("id_list")
    if not isinstance(id_list, list) or not id_list:
        return jsonify({"error": "id_list must be a non-empty list"}), 400
    deleted = [vid for vid in id_list if state.delete_timelapse_video(vid)]
    if deleted:
        state.log_event("info", f"{len(deleted)} timelapse video(s) deleted via dashboard")
    return jsonify({"deleted": deleted, "not_found": [vid for vid in id_list if vid not in deleted]})


@app.route("/api/camera-settings", methods=["POST"])
def api_set_camera_settings():
    """[2026-09-14] device/width/height are camera-streamer's OWN capture
    settings now, not camera_service.py's (see docs/camera-streamer-
    setup.md) - a change to any of them (or streamer_port) is written
    into camera-streamer.env via state.write_camera_streamer_env() and
    then needs camera-streamer.service restarted to pick it up, which
    this does automatically. Unlike the old jpeg_quality/live_capture_fps/
    stream_relay_fps settings (removed - see the "camera" DEFAULT_CONFIG
    comment in shared_state.py), there's no config here anymore that
    takes effect without a restart - camera-streamer only reads its
    launch args/env once, at its own startup."""
    body = request.get_json(force=True, silent=True) or {}
    cleaned, error = state.validate_camera_settings(body)
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    config["camera"] = cleaned
    state.save_config(config)
    state.write_camera_streamer_env(cleaned)
    restarted = _restart_service("camera-streamer.service")
    state.log_event("info", "Camera settings updated" +
                             (" - camera-streamer restarted automatically" if restarted else
                              " - automatic restart failed, restart camera-streamer.service by hand"))
    response = dict(config)
    response["restart_attempted"] = True
    response["restart_ok"] = restarted
    return jsonify(response)


CAMERA_DISCOVER_TIMEOUT_SECONDS = 30


@app.route("/api/camera-discover", methods=["POST"])
def api_camera_discover():
    """Runs discover_camera.py --json for the Config page's Discover
    button, so finding the right /dev/videoN index doesn't require
    SSHing in and running it from the CLI.

    Deliberately shells out to the script rather than importing cv2
    directly into this process: webapp.py has stayed cv2-free on
    purpose so far (a missing/broken opencv install can't take down the
    whole dashboard, only the camera-specific features - this already
    mattered once this session, when a stale-template bug got
    misdiagnosed as a camera dependency problem before the real cause
    was found). A subprocess keeps that property; if cv2 is missing or
    broken, only this one request fails.

    [2026-09-14] Stops/restarts camera-streamer.service now, not
    dermestid-camera.service - camera-streamer is the process that holds
    the real camera device open continuously since the switch (see
    docs/camera-streamer-setup.md), and most UVC webcams only allow one
    client at a time, so without this the current in-use index just
    wouldn't show up in the scan at all - not a crash, just a silently
    confusing "no camera found" result. dermestid-camera.service (the
    timelapse-only process now) never opens the device itself, so it
    doesn't need stopping for this anymore. Both systemctl calls are
    best-effort (sudo -n, non-interactive) - if camera-streamer isn't
    installed, or the Pi's sudoers isn't set up for it yet, they simply
    fail quietly and discovery still runs, it just might not see
    whichever index camera-streamer was already holding."""
    subprocess.run(["sudo", "-n", "systemctl", "stop", "camera-streamer.service"],
                    capture_output=True, timeout=15)
    try:
        result = subprocess.run(
            ["python3", os.path.join(state.BASE_DIR, "discover_camera.py"), "--json"],
            capture_output=True, text=True, timeout=CAMERA_DISCOVER_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": f"Discovery timed out after {CAMERA_DISCOVER_TIMEOUT_SECONDS}s"}), 500
    finally:
        subprocess.run(["sudo", "-n", "systemctl", "start", "camera-streamer.service"],
                        capture_output=True, timeout=15)

    if result.returncode != 0:
        return jsonify({"error": f"discover_camera.py failed: {(result.stderr or '')[-500:]}"}), 500
    try:
        devices = json.loads(result.stdout)
    except json.JSONDecodeError:
        return jsonify({"error": "Could not parse discovery output - see logs/camera.log or run "
                                  "discover_camera.py manually on the Pi."}), 500

    # Remember each device's probed resolutions in config.json, keyed by
    # index - so switching back to (or reloading the page on) a device
    # already discovered once doesn't fall back to the generic, unverified
    # COMMON_RESOLUTIONS list until Discover gets clicked again. See
    # DEFAULT_CONFIG["camera_known_resolutions"]'s comment in
    # shared_state.py.
    if devices:
        config = state.load_config()
        known = config.setdefault("camera_known_resolutions", {})
        for d in devices:
            known[str(d["index"])] = d.get("supported_resolutions") or []
        state.save_config(config)

    response = {"devices": devices}
    if not devices:
        response["hint"] = ("No camera found on indices 0-9. If camera-streamer is "
                             "installed but the Pi's sudoers isn't set up to let the dashboard "
                             "stop/restart it (see README), the in-use index won't show up here - "
                             "check README's sudoers section, or stop the service by hand first.")
    return jsonify(response)


@app.route("/api/history")
def api_history():
    hours = request.args.get("hours", default=24, type=float)
    hours = max(0.5, min(hours, 24 * 60))  # clamp between 30 min and 60 days
    return jsonify(state.get_history(hours))


@app.route("/api/events")
def api_events():
    limit = request.args.get("limit", default=50, type=int)
    level = request.args.get("level", default=None, type=str)
    before = request.args.get("before", default=None, type=float)
    if level not in (None, *state.EVENT_LEVELS):
        return jsonify({"error": "level must be one of info/warning/error/critical"}), 400
    return jsonify(state.get_recent_events(limit=limit, level=level, before_ts=before))


@app.route("/api/events/download")
def api_events_download():
    """Plain-text export of the Logs page's "download log" button - ALL
    matching rows (limit=None), not just whatever page is currently
    loaded in the browser, honoring the same level filter (and its "at
    or above" severity semantics - see get_recent_events) selected
    there. This is the unified cross-service events table (climate.py/
    webapp.py/camera_service.py/ble_listener.py all write into it via
    log_event()), which is what the Logs page actually shows - NOT any
    one service's own raw logs/*.log file on disk (those are per-
    process and also catch lower-level logging() calls that never went
    through log_event() at all, e.g. individual DHT22 retry-attempt
    warnings), so this export and that page always agree on content."""
    level = request.args.get("level", default=None, type=str)
    if level not in (None, *state.EVENT_LEVELS):
        return jsonify({"error": "level must be one of info/warning/error/critical"}), 400
    events = state.get_recent_events(limit=None, level=level)
    events.reverse()  # oldest-first reads naturally in a downloaded file; the dashboard itself shows newest-first
    lines = [
        f"{datetime.datetime.fromtimestamp(e['ts']).strftime('%Y-%m-%d %H:%M:%S')}  {e['level'].upper():<8}  {e['message']}"
        for e in events
    ]
    body = "\n".join(lines) + ("\n" if lines else "")
    filename = (f"necroparlor-events"
                f"{'-' + level if level else ''}"
                f"-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.txt")
    return Response(body, mimetype="text/plain",
                     headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/readings-table")
def api_readings_table():
    limit = request.args.get("limit", default=50, type=int)
    before = request.args.get("before", default=None, type=float)
    return jsonify(state.get_readings_table(limit=limit, before_ts=before))


@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("mode")
    if mode not in state.VALID_MODES:
        return jsonify({"error": f"mode must be one of {state.VALID_MODES}"}), 400
    config = state.load_config()
    config["current_mode"] = mode
    state.save_config(config)
    state.log_event("info", f"Mode changed to '{mode}' via web dashboard")
    return jsonify(config)


@app.route("/api/setpoints", methods=["POST"])
def api_set_setpoints():
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("mode")
    cleaned, error = state.validate_setpoints(mode, body)
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    config["modes"][mode] = cleaned
    state.save_config(config)
    state.log_event("info", f"Setpoints for '{mode}' updated via web dashboard")
    return jsonify(config)


@app.route("/api/update-status")
def api_update_status():
    """Read-only - reflects whatever the background checker last found.
    Polled by the banner on every page and the Config page's tile."""
    status = state.get_update_status()
    config = state.load_config()
    return jsonify({
        "status": status,
        "check_interval_minutes": config.get("update_check_interval_minutes", 15),
        "update_branch": config.get("update_branch", "main"),
    })


@app.route("/api/update-branches")
def api_update_branches():
    """Lists branches available on the remote, for the Config page's
    dropdown. A lightweight `git ls-remote` (just queries refs, doesn't
    fetch any objects), so this is safe to call on every page load
    without meaningfully touching the repo or the network."""
    try:
        result = subprocess.run(["git", "ls-remote", "--heads", "origin"],
                                 cwd=state.BASE_DIR, check=True, timeout=15, capture_output=True, text=True)
    except Exception:
        logging.exception("Could not list remote branches")
        return jsonify({"error": "could not reach the remote to list branches"}), 502
    branches = []
    for line in result.stdout.splitlines():
        # Each line looks like: <sha>\trefs/heads/<branch-name>
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            branches.append(parts[1][len("refs/heads/"):])
    return jsonify(sorted(branches))


@app.route("/api/update-branch", methods=["POST"])
def api_set_update_branch():
    """Sets which branch auto_update.sh and the background checker
    track, and immediately runs the same read-only check
    check-for-update-now uses (git fetch + compare against the new
    target) before responding - so the Config page can show whether
    that branch actually differs from HEAD right away, instead of
    showing the PREVIOUS branch's stale status until the background
    checker's next tick (up to update_check_interval_minutes later).
    Doesn't switch anything itself - that happens the next time an
    update is actually applied (button or timer), same as any other
    pending update. Response shape matches /api/check-for-update-now's
    for the same reason: the frontend renders both the same way."""
    body = request.get_json(force=True, silent=True) or {}
    branch, error = state.validate_update_branch(body.get("branch"))
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    config["update_branch"] = branch
    state.save_config(config)
    state.log_event("info", f"Update branch changed to '{branch}'")
    _git_check_for_update()
    status = state.get_update_status()
    config = state.load_config()
    return jsonify({
        "status": status,
        "check_interval_minutes": config.get("update_check_interval_minutes", 15),
        "update_branch": config.get("update_branch", "main"),
    })


@app.route("/api/update-check-interval", methods=["POST"])
def api_set_update_interval():
    body = request.get_json(force=True, silent=True) or {}
    minutes, error = state.validate_update_interval(body.get("minutes"))
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    config["update_check_interval_minutes"] = minutes
    state.save_config(config)
    state.log_event("info", f"Update check interval changed to {minutes} minutes")
    return jsonify(config)


@app.route("/api/apply-update", methods=["POST"])
def api_apply_update():
    """Runs auto_update.sh as a fully DETACHED background process, not a
    normal subprocess call. This request handler is itself running
    inside dermestid-web.service, and the script restarts that exact
    service partway through - a non-detached child would be killed
    along with this process at that point, silently aborting the
    update. start_new_session=True (equivalent to setsid) gives the
    child its own process group so it survives the parent's restart;
    verified this actually works with a standalone test before wiring
    it in here, not just assumed.

    Returns immediately, before the script has necessarily finished -
    the browser is expected to show a "check back in a few seconds"
    message and reload rather than wait for a response that may never
    come if the restart happens first.
    """
    script_path = os.path.join(state.BASE_DIR, "auto_update.sh")
    log_dir = os.path.join(state.BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "apply_update.log")
    log_file = open(log_path, "a")
    subprocess.Popen(
        ["bash", script_path],
        cwd=state.BASE_DIR,
        stdout=log_file, stderr=subprocess.STDOUT,
        start_new_session=True
    )
    state.log_event("info", "Update triggered manually from the dashboard")
    return jsonify({"status": "started"})


@app.route("/api/check-for-update-now", methods=["POST"])
def api_check_for_update_now():
    """Runs the same read-only check the background thread does
    (git fetch + compare), synchronously, right now - for when the
    configured check interval is longer than you want to wait, without
    needing to change that interval or restart anything. Just a fetch
    and a comparison (no pull, no restart), so unlike apply-update this
    is safe to run inline and return the fresh result directly, no
    detached-process complexity needed."""
    _git_check_for_update()
    status = state.get_update_status()
    config = state.load_config()
    return jsonify({
        "status": status,
        "check_interval_minutes": config.get("update_check_interval_minutes", 15),
        "update_branch": config.get("update_branch", "main"),
    })


def _git_check_for_update():
    """Read-only: fetches the CONFIGURED target branch from origin and
    compares local HEAD to it. Never pulls, checks out, or restarts
    anything by itself - applying an update (including a branch switch)
    is a separate, explicit action (the timer or the dashboard button),
    both of which reuse the same tested auto_update.sh rather than
    duplicating this logic.

    Comparing HEAD against origin/<target branch> naturally covers two
    different situations with the same logic: if the currently checked-
    out branch IS the target branch, a difference means new commits are
    available; if a DIFFERENT branch is selected, HEAD and the target
    almost certainly differ entirely, meaning a branch switch is what's
    actually needed. current_branch/target_branch are both recorded so
    the dashboard can tell these apart and message them differently.

    Uses the same lockfile as auto_update.sh, non-blocking: if a manual
    update is currently running, this just skips this one check and
    tries again on the next tick, rather than risking two processes
    trying to update the same git ref at the same time (a real error -
    "cannot lock ref ... is at X but expected Y" - that happened in
    practice when this background check and a manual run overlapped).
    """
    lock_path = os.path.join(state.BASE_DIR, ".git_update.lock")
    lock_file = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logging.info("Skipping this update check - auto_update.sh appears to be running")
            return
        config = state.load_config()
        target_branch = config.get("update_branch", "main")
        current_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                         cwd=state.BASE_DIR, check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "fetch", "origin", target_branch, "--quiet"],
                        cwd=state.BASE_DIR, check=True, timeout=30, capture_output=True)
        local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=state.BASE_DIR,
                                check=True, capture_output=True, text=True).stdout.strip()
        remote = subprocess.run(["git", "rev-parse", f"origin/{target_branch}"], cwd=state.BASE_DIR,
                                 check=True, capture_output=True, text=True).stdout.strip()
        is_available = local != remote
        remote_message = None
        if is_available:
            msg = subprocess.run(["git", "log", f"origin/{target_branch}", "-1", "--pretty=%s"],
                                  cwd=state.BASE_DIR, check=True, capture_output=True, text=True)
            remote_message = msg.stdout.strip()
        state.save_update_status(is_available, local[:7], remote[:7], remote_message,
                                  current_branch=current_branch, target_branch=target_branch)
    except Exception:
        logging.exception("Background update check failed")
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def update_checker_loop():
    """Background thread, started once when webapp.py starts. Checks
    immediately on startup, explicitly (not relying on an implicit
    zero-sentinel trick) - this matters because applying an update
    restarts this very service, so an immediate check right on startup
    is what makes the "update available" banner clear itself promptly
    afterward, rather than waiting out however much of the old check
    interval happened to remain. After that first check, re-reads the
    configured interval every 30s (a short tick) rather than sleeping
    for the full interval at once, so shortening the interval via the
    Config page also takes effect promptly instead of waiting out
    whatever the old, longer interval happened to be.
    """
    last_check = None
    while True:
        try:
            config = state.load_config()
            interval_seconds = config.get("update_check_interval_minutes", 15) * 60
            if last_check is None or time.time() - last_check >= interval_seconds:
                _git_check_for_update()
                last_check = time.time()
        except Exception:
            logging.exception("Unexpected error in update_checker_loop - will retry next tick")
        time.sleep(30)


if __name__ == "__main__":
    state.init_db()
    threading.Thread(target=update_checker_loop, daemon=True).start()
    # threaded=True is required, not optional, now that /api/camera/
    # stream.mjpg holds its connection open indefinitely - Werkzeug's
    # dev server otherwise handles one request at a time, so a single
    # open camera tab would silently freeze every other page (status,
    # config, logs...) on the whole dashboard for as long as it stayed
    # open.
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
