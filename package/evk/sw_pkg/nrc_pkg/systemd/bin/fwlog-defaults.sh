#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# Read the firmware's log settings once, after the stack has come up, and on a
# debug package apply this package's defaults first.
#
# The reading is cached for the Web GUI. That panel drives firmware-owned
# settings, so it cannot show them without asking the chip - and asking is a
# shell command the firmware echoes into the log being read, so the GUI does
# not ask on its own. Taking the reading here, at the one moment the settings
# are known to have just been established, is what lets the panel open with
# its controls live instead of greyed out, which reads as "not supported".
#
# A release package leaves the firmware at its own power-on defaults --
# flight recorder, errors only, console output excluded -- which cost no
# bandwidth and still capture an assert. A debug package is for somebody
# sitting in front of the box wanting to watch it work, so it opts into the
# live stream and includes console output; the level stays at `none` because
# on a debug image the module traces are voluminous and are better turned up
# deliberately, from the Web GUI, for the one subsystem being investigated.
#
# Only the defaults are applied, and only at start: anything the operator
# changes afterwards stands until the next restart. The cached reading is
# refreshed by those changes too - the Web GUI rewrites it whenever it does
# ask the firmware - so it never contradicts the chip for long.
set -u

# The unit passes NRC_SCRIPT_DIR in from halow.conf. Without it there
# is no package to find - guessing an install path would only turn a
# missing variable into a silent no-op somewhere else.
[ -n "${NRC_SCRIPT_DIR:-}" ] || exit 0
CLI="$NRC_SCRIPT_DIR/cli_app"
[ -x "$CLI" ] || exit 0

CACHE=/run/halow/fwlog-state.txt
mkdir -p /run/halow 2>/dev/null

# Whatever is cached describes the firmware that was running before this
# start, not the one coming up now. Better no reading than that one.
rm -f "$CACHE" "$CACHE.tmp"

# The firmware answers only once it has been downloaded and started, which
# happens in a different unit; wait for it rather than racing it.
#
# cli_app exits 0 whether or not the firmware answered (its one-shot path
# prints OK/FAIL and returns 0 regardless), so the exit status says nothing.
# The answer does: a real reading contains "mode(".
i=0
while [ "$i" -lt 15 ]; do
    "$CLI" hlog show >"$CACHE.tmp" 2>/dev/null
    grep -q 'mode(' "$CACHE.tmp" 2>/dev/null && break
    i=$((i + 1))
    sleep 2
done
if [ "$i" -ge 15 ]; then
    # No firmware, so no reading. Leave no cache rather than a stale one: the
    # GUI says "not read" and offers Refresh, which is true.
    rm -f "$CACHE.tmp"
    exit 0
fi

# A release package leaves the firmware at its power-on defaults. Only a debug
# package changes them, and then the reading has to be retaken - the one above
# describes the settings we just replaced. If the retake fails, the earlier
# reading stays: wrong about three settings is better than empty.
if [ "${BUILD_TYPE:-}" = "debug" ]; then
    "$CLI" hlog mode stream >/dev/null 2>&1
    "$CLI" hlog level none  >/dev/null 2>&1
    "$CLI" hlog cli on      >/dev/null 2>&1
    if "$CLI" hlog show >"$CACHE.tmp2" 2>/dev/null && grep -q 'mode(' "$CACHE.tmp2"; then
        mv -f "$CACHE.tmp2" "$CACHE.tmp"
    fi
    rm -f "$CACHE.tmp2"
    echo "[fwlog-defaults] debug package: live stream, level none, console output on"
fi

# Rename so a reader never sees a half-written file.
mv -f "$CACHE.tmp" "$CACHE" 2>/dev/null
