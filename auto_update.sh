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

# config.json is meant to be local machine state, never git-tracked (see
# .gitignore), specifically so the dashboard's own live rewrites of it
# (setpoints, sensor sources, camera settings, etc.) can never conflict
# with an incoming update. That untracking only exists starting on the
# add-webcam branch, though - every older branch (main included, until
# this is merged) still has config.json in its committed tree. Switching
# between "tracks it" and "doesn't track it" is not a normal update from
# git's point of view: git sees the incoming branch delete the file while
# a stash simultaneously modifies it, a real conflict it can't
# auto-resolve, and a failed stash-pop here leaves the index in a broken
# half-merged state that then blocks every subsequent run at this same
# step (this happened in practice - see PROJECT_STATUS.md).
#
# So config.json is handled entirely outside of git now, every run,
# regardless of which branch is tracking it: back up whatever is
# currently on disk, let checkout/pull do whatever it wants to any
# tracked copy, then force the backup back into place and make sure it's
# untracked again. This can never conflict with a stash because
# config.json is removed from the index (if present) before the stash
# below ever looks at it.
CONFIG_BACKUP="$HOME/.dermestid_config_backup.json"
if [ -f config.json ]; then
    cp config.json "$CONFIG_BACKUP"
fi
if git ls-files --error-unmatch config.json >/dev/null 2>&1; then
    git rm --cached -f --quiet config.json
fi

# General safety net for anything ELSE that might have local uncommitted
# changes to a tracked file (config.json can no longer be one, per
# above), which a pull or branch switch could otherwise fail on.
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Local changes detected in a tracked file - stashing"
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

# Restore the live config regardless of what the branch we just landed on
# tracks - if it still has config.json in its tree (e.g. main, pre-merge),
# checkout/pull just wrote that stale committed copy over ours; put the
# real one back and strip it from the index again so it stays untracked
# from here on, this run and every future one.
if [ -f "$CONFIG_BACKUP" ]; then
    cp "$CONFIG_BACKUP" config.json
    if git ls-files --error-unmatch config.json >/dev/null 2>&1; then
        git rm --cached -f --quiet config.json
    fi
fi

echo "Restarting climate control..."
if ! sudo -n systemctl restart dermestid-climate.service; then
    echo "ERROR: passwordless sudo failed for dermestid-climate.service."
    echo "The code has already been updated (git pull succeeded) - just needs a manual restart:"
    echo "  sudo systemctl restart dermestid-climate.service dermestid-web.service dermestid-ble.service dermestid-camera.service"
    echo "See README.md 'Optional: automatic updates from GitHub' for the one-time visudo setup to avoid this going forward."
    exit 1
fi

if systemctl is-enabled --quiet dermestid-ble.service 2>/dev/null; then
    echo "Restarting BLE sensor listener (currently enabled)..."
    if ! sudo -n systemctl restart dermestid-ble.service; then
        echo "ERROR: passwordless sudo failed for dermestid-ble.service."
        echo "(dermestid-climate.service was already restarted successfully above)"
        echo "Run manually: sudo systemctl restart dermestid-ble.service dermestid-web.service dermestid-camera.service"
        exit 1
    fi
fi

if systemctl is-enabled --quiet dermestid-camera.service 2>/dev/null; then
    echo "Restarting camera service (currently enabled)..."
    if ! sudo -n systemctl restart dermestid-camera.service; then
        echo "ERROR: passwordless sudo failed for dermestid-camera.service."
        echo "(dermestid-climate.service was already restarted successfully above)"
        echo "Run manually: sudo systemctl restart dermestid-camera.service dermestid-web.service"
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
