#!/usr/bin/env python3
"""
Local dashboard for the dermestid enclosure.

Run with: python3 webapp.py
Then visit http://<pi-ip-address>:8080 from any device on your LAN.

There is NO authentication on this - it's meant for your home network
only. Don't port-forward it to the internet.
"""
import time
import os
import shutil
import subprocess
import threading
import logging
import fcntl

from flask import Flask, jsonify, request, render_template, send_file, abort, Response

import shared_state as state

app = Flask(__name__)

# A live camera frame is considered stale once it's older than this many
# multiples of the CONFIGURED live-capture interval, rather than a fixed
# number of seconds - keeps the staleness threshold proportional to
# however fast camera_service.py is actually set to capture, instead of
# being wrong at either extreme (a fixed 10s threshold would falsely
# flag a deliberately slow 5s-interval setup, or too slowly notice a
# genuinely dead 1s-interval one).
CAMERA_STALE_MULTIPLIER = 5

# How often /api/camera/stream.mjpg re-reads camera/latest.jpg and pushes
# it to each connected browser tab, independent of how fast
# camera_service.py itself is actually capturing (camera.
# live_capture_interval_seconds). Decoupled on purpose: capture rate is a
# Pi-CPU-vs-smoothness tradeoff for camera_service.py, this is a
# per-viewer relay cost for webapp.py, and ~6-7fps is already smooth
# enough to read as "live video" rather than a slideshow - no reason to
# push more HTTP writes per viewer than that even if capture is faster.
STREAM_RELAY_INTERVAL = 0.15


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


@app.route("/camera")
def camera_page():
    return render_template("camera.html", active_page="camera")


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

    return jsonify({"config": config, "latest": latest, "stale": stale,
                     "ble_status": ble_status, "door_status": door_status,
                     "last_valid_timestamps": last_valid})


@app.route("/api/ble-sensors")
def api_ble_sensors():
    """All BLE addresses currently being heard, for picking which one
    to wire in as the external sensor."""
    return jsonify(state.get_all_ble_readings())


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
    and which library decodes its advertisements. Changing the brand
    requires restarting ble_listener.py to take effect (it's read once
    at startup, not re-checked every cycle)."""
    body = request.get_json(force=True, silent=True) or {}
    mac, error = state.validate_ble_mac(body.get("ble_mac"))
    if error:
        return jsonify({"error": error}), 400
    sensor_type, error = state.validate_ble_sensor_type(body.get("ble_sensor_type", "sensorpush"))
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    config["ble_mac"] = mac
    config["ble_sensor_type"] = sensor_type
    state.save_config(config)
    state.log_event("info", f"BLE sensor address {'set to ' + mac if mac else 'cleared'} "
                             f"(brand: {state.BLE_SENSOR_LIBRARIES[sensor_type]['label']})")
    return jsonify(config)


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
    config = state.load_config()
    cam_cfg = config.get("camera", {})
    interval = cam_cfg.get("live_capture_interval_seconds", 0.2)
    available = False
    age_seconds = None
    if os.path.exists(state.CAMERA_LIVE_PATH):
        age_seconds = time.time() - os.path.getmtime(state.CAMERA_LIVE_PATH)
        available = age_seconds < interval * CAMERA_STALE_MULTIPLIER
    disk = shutil.disk_usage(state.BASE_DIR)
    return jsonify({
        "available": available,
        "age_seconds": age_seconds,
        "camera": cam_cfg,
        "current_mode": config.get("current_mode"),
        "snapshot_interval_minutes": config.get("modes", {}).get(config.get("current_mode"), {}).get("snapshot_interval_minutes", 0),
        "snapshot_count": state.get_camera_snapshot_count(),
        "disk_free_mb": disk.free / (1024 * 1024),
    })


@app.route("/api/camera/latest.jpg")
def api_camera_latest():
    if not os.path.exists(state.CAMERA_LIVE_PATH):
        abort(404)
    response = send_file(state.CAMERA_LIVE_PATH, mimetype="image/jpeg")
    # Every poll should get whatever's freshest right now, never a
    # browser-cached copy from the last one - same reasoning as the
    # rest of this dashboard's live-updating tiles.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/camera/stream.mjpg")
def api_camera_stream():
    """A genuine live video feed, not the old still-image polling - a
    plain browser <img> tag renders a multipart/x-mixed-replace response
    natively as continuously-updating video, no player/codec/JS polling
    loop needed. This still doesn't touch the USB device itself: it just
    re-reads camera/latest.jpg (the file camera_service.py is the sole
    writer of) on a short timer and relays whatever's currently there
    into this one HTTP connection. That's what keeps "only one process
    ever opens the camera" true even with the dashboard open in several
    browser tabs at once - each tab just gets its own independent relay
    of the same file, same reasoning as the old polling endpoint, just
    pushed from the server instead of pulled by the client.

    Requires threaded=True on app.run() below - this request stays open
    indefinitely, and the single-threaded dev-server default would let
    one open camera tab freeze every other page on the dashboard for as
    long as it stayed open.
    """
    def generate():
        boundary = b"--frame"
        while True:
            try:
                with open(state.CAMERA_LIVE_PATH, "rb") as f:
                    frame = f.read()
                yield (boundary + b"\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" +
                       frame + b"\r\n")
            except (FileNotFoundError, OSError):
                # camera_service.py hasn't written a first frame yet, or
                # isn't running - just keep retrying on the same schedule
                # rather than ending the stream; the browser <img> will
                # start showing frames the moment one appears on disk.
                pass
            time.sleep(STREAM_RELAY_INTERVAL)
    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


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


@app.route("/api/camera-settings", methods=["POST"])
def api_set_camera_settings():
    body = request.get_json(force=True, silent=True) or {}
    cleaned, error = state.validate_camera_settings(body)
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    config["camera"] = cleaned
    state.save_config(config)
    state.log_event("info", "Camera settings updated - restart the camera service "
                             "for a device/resolution change to take effect")
    return jsonify(config)


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
    if level not in (None, "info", "warning", "error", "critical"):
        return jsonify({"error": "level must be one of info/warning/error/critical"}), 400
    return jsonify(state.get_recent_events(limit=limit, level=level, before_ts=before))


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
    track. Doesn't switch anything itself - that happens the next time
    an update is actually applied (button or timer), same as any other
    pending update."""
    body = request.get_json(force=True, silent=True) or {}
    branch, error = state.validate_update_branch(body.get("branch"))
    if error:
        return jsonify({"error": error}), 400
    config = state.load_config()
    config["update_branch"] = branch
    state.save_config(config)
    state.log_event("info", f"Update branch changed to '{branch}'")
    return jsonify(config)


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
