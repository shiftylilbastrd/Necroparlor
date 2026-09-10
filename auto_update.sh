#!/bin/bash
# Checks GitHub for new commits and, if found, pulls and restarts the
# affected services automatically. Run periodically via
# dermestid-autoupdate.timer - once this is set up, dragging updated
# files into GitHub is the ONLY step left; no more manual "git pull" or
# "systemctl restart" on the Pi.
#
# Requires passwordless sudo for the specific systemctl restart commands
# below - see README.md for the one-time setup (a sudoers.d entry).
set -e
cd "$(dirname "$0")"

BEFORE=$(git rev-parse HEAD)
git fetch origin main --quiet
AFTER=$(git rev-parse origin/main)

if [ "$BEFORE" = "$AFTER" ]; then
    echo "No update available (still at ${BEFORE:0:7})"
    exit 0
fi

echo "Update found: ${BEFORE:0:7} -> ${AFTER:0:7}"

# Fail BEFORE touching any files if passwordless sudo isn't set up for
# the restart commands below - better to bail out cleanly here than to
# pull new code and then discover we can't restart the services to
# actually run it (leaving files updated but the old code still live).
if ! sudo -n true 2>/dev/null; then
    echo "ERROR: passwordless sudo isn't configured for the required systemctl commands."
    echo "See README.md 'Optional: automatic updates from GitHub' for the one-time visudo setup."
    exit 1
fi

# config.json is tracked in git but also gets rewritten by the dashboard
# (setpoints, sensor source, etc.) - stash any such local changes before
# pulling so they can't conflict, then restore them afterward.
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Local changes detected (likely config.json from the dashboard) - stashing"
    git stash push --quiet -m "auto-update: preserving local changes"
    STASHED=1
fi

git pull origin main --quiet

if [ "$STASHED" = "1" ]; then
    if git stash pop --quiet; then
        echo "Local changes restored after update"
    else
        echo "WARNING: local changes could not be automatically restored (conflict)."
        echo "Run 'git stash list' and 'git stash show -p' on the Pi to recover them by hand."
    fi
fi

echo "Restarting climate control..."
sudo systemctl restart dermestid-climate.service

if systemctl is-enabled --quiet dermestid-sensorpush.service 2>/dev/null; then
    echo "Restarting SensorPush listener (currently enabled)..."
    sudo systemctl restart dermestid-sensorpush.service
fi

# Web dashboard restarts LAST, deliberately. When this script is
# triggered from the dashboard's "Update now" button, it runs as a
# child process of dermestid-web.service itself - and systemd's default
# behavior when restarting a service is to kill its entire process
# group (KillMode=control-group), not just the tracked main process.
# The instant the line below tells systemd to restart the web service,
# systemd kills THIS SCRIPT too, as collateral, before it can run
# anything after it. Detaching this script from its parent Python
# process (see webapp.py's /api/apply-update) protects against the
# Flask process dying - it does NOT protect against systemd killing the
# whole group on a service restart. So: everything else must happen
# BEFORE this line, or it silently never runs.
echo "Restarting web dashboard..."
sudo systemctl restart dermestid-web.service

echo "Update complete - now running $(git rev-parse --short HEAD)"
