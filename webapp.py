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
import subprocess
import threading
import logging
import fcntl

from flask import Flask, jsonify, request, render_template

import shared_state as state

app = Flask(__name__)


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
        "check_interval_minutes": config.get("update_check_interval_minutes", 15)
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


def _git_check_for_update():
    """Read-only: fetches from origin and compares local HEAD to
    origin/main. Never pulls or restarts anything by itself - applying
    an update is a separate, explicit action (the timer or the
    dashboard button), both of which reuse the same tested
    auto_update.sh rather than duplicating this logic.

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
        subprocess.run(["git", "fetch", "origin", "main", "--quiet"],
                        cwd=state.BASE_DIR, check=True, timeout=30, capture_output=True)
        local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=state.BASE_DIR,
                                check=True, capture_output=True, text=True).stdout.strip()
        remote = subprocess.run(["git", "rev-parse", "origin/main"], cwd=state.BASE_DIR,
                                 check=True, capture_output=True, text=True).stdout.strip()
        is_available = local != remote
        remote_message = None
        if is_available:
            msg = subprocess.run(["git", "log", "origin/main", "-1", "--pretty=%s"],
                                  cwd=state.BASE_DIR, check=True, capture_output=True, text=True)
            remote_message = msg.stdout.strip()
        state.save_update_status(is_available, local[:7], remote[:7], remote_message)
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
    app.run(host="0.0.0.0", port=8080, debug=False)
