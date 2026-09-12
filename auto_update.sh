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

# webapp.py runs its OWN periodic "git fetch" in the background
# (update_checker_loop, on whatever interval the Config page is set to)
# - completely independent of this script. If that background check and
# a manual run of this script ever land at nearly the same moment, two
# processes updating the same git ref concurrently produces a real git
# error ("cannot lock ref ... is at X but expected Y"). This lock makes
# the two mutually exclusive - webapp.py acquires the same lockfile
# (non-blocking - it just skips that one check and tries again next
# tick if this script is currently running) before doing its own fetch.
LOCKFILE=".git_update.lock"
exec 200>"$LOCKFILE"
if ! flock -w 30 200; then
    echo "ERROR: could not acquire the git update lock within 30s - the background"
    echo "update checker may be stuck. Try again in a moment."
    exit 1
fi

BEFORE=$(git rev-parse HEAD)
TARGET_BRANCH=$(python3 -c "import shared_state as state; print(state.load_config().get('update_branch', 'main'))")
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git fetch origin "$TARGET_BRANCH" --quiet
AFTER=$(git rev-parse "origin/$TARGET_BRANCH")

if [ "$BEFORE" = "$AFTER" ] && [ "$CURRENT_BRANCH" = "$TARGET_BRANCH" ]; then
    echo "No update available (still at ${BEFORE:0:7} on $CURRENT_BRANCH)"
    exit 0
fi

if [ "$CURRENT_BRANCH" != "$TARGET_BRANCH" ]; then
    echo "Branch switch found: $CURRENT_BRANCH -> $TARGET_BRANCH (${AFTER:0:7})"
else
    echo "Update found: ${BEFORE:0:7} -> ${AFTER:0:7}"
fi

# config.json is tracked in git but also gets rewritten by the dashboard
# (setpoints, sensor source, etc.) - stash any such local changes before
# pulling OR switching branches so they can't conflict, then restore
# them afterward. A branch switch can fail on uncommitted changes just
# as easily as a pull can - same treatment for both.
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Local changes detected (likely config.json from the dashboard) - stashing"
    git stash push --quiet -m "auto-update: preserving local changes"
    STASHED=1
fi

if [ "$CURRENT_BRANCH" != "$TARGET_BRANCH" ]; then
    # -B creates the local branch if it doesn't exist yet, or resets it
    # to exactly match the remote if it does - the Pi should never carry
    # local commits on a branch that don't exist upstream, so resetting
    # to match origin exactly is the correct, expected behavior here,
    # not data loss (uncommitted changes are separately protected by
    # the stash above; this only affects committed history on the
    # branch itself, which the Pi's clone should never diverge on).
    git checkout -B "$TARGET_BRANCH" "origin/$TARGET_BRANCH" --quiet
else
    git pull origin "$TARGET_BRANCH" --quiet
fi

if [ "$STASHED" = "1" ]; then
    if git stash pop --quiet; then
        echo "Local changes restored after update"
    else
        echo "WARNING: local changes could not be automatically restored (conflict)."
        echo "Run 'git stash list' and 'git stash show -p' on the Pi to recover them by hand."
    fi
fi

echo "Restarting climate control..."
if ! sudo -n systemctl restart dermestid-climate.service; then
    echo "ERROR: passwordless sudo failed for dermestid-climate.service."
    echo "The code has already been updated (git pull succeeded) - just needs a manual restart:"
    echo "  sudo systemctl restart dermestid-climate.service dermestid-web.service dermestid-ble.service"
    echo "See README.md 'Optional: automatic updates from GitHub' for the one-time visudo setup to avoid this going forward."
    exit 1
fi

if systemctl is-enabled --quiet dermestid-ble.service 2>/dev/null; then
    echo "Restarting BLE sensor listener (currently enabled)..."
    if ! sudo -n systemctl restart dermestid-ble.service; then
        echo "ERROR: passwordless sudo failed for dermestid-ble.service."
        echo "(dermestid-climate.service was already restarted successfully above)"
        echo "Run manually: sudo systemctl restart dermestid-ble.service dermestid-web.service"
        exit 1
    fi
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
if ! sudo -n systemctl restart dermestid-web.service; then
    echo "ERROR: passwordless sudo failed for dermestid-web.service."
    echo "(everything else above was already restarted successfully)"
    echo "Run manually: sudo systemctl restart dermestid-web.service"
    exit 1
fi

echo "Update complete - now running $(git rev-parse --short HEAD) on branch $(git rev-parse --abbrev-ref HEAD)"
