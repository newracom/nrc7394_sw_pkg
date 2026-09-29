#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# halow-hostapd.service ExecStartPost: wait for hostapd ENABLED, then publish
# the live AP info (ap.env) - MAC, S1G channel/freq, IPs - for the gateway
# add-on and the Web GUI.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ -n "$NRC_SCRIPT_DIR" ] || { echo "NRC_SCRIPT_DIR not set"; exit 1; }

# ap.env is AP-mode data (consumed by the gateway add-on and the Web GUI
# dashboard); in other operating modes there is nothing to publish.
[ "${STA_TYPE:-1}" = "1" ] || exit 0

IFACE=$(grep '^interface=' "$NRC_SCRIPT_DIR/conf/temp_hostapd_config.conf" 2>/dev/null | cut -d= -f2)
IFACE=${IFACE:-wlan0}

for i in $(seq 1 30); do
    if hostapd_cli -p /var/run/hostapd -i "$IFACE" status 2>/dev/null \
        | grep -q '^state=ENABLED'; then
        break
    fi
    sleep 1
done

python3 "$NRC_SCRIPT_DIR/ap_env.py" --country "${COUNTRY:-US}" --iface "$IFACE"
RC=$?

exit $RC
