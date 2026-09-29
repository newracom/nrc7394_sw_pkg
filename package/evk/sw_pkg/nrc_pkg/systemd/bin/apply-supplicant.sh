#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# Apply supplicant-level config changes (ssid/psk) with the minimal
# disruption in STA mode: re-render the temp conf from the GUI overrides and
# make the running wpa_supplicant re-read it (wpa_cli reconfigure) - the
# driver/FW/network stay up.
set -e
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

. /etc/halow/halow.conf
[ "${STA_TYPE:-1}" = "0" ] || {
    echo "supplicant apply only makes sense in STA mode (STA_TYPE=${STA_TYPE:-1})"; exit 1; }
CONF="$NRC_SCRIPT_DIR/conf/temp_supplicant_config.conf"
[ -f "$CONF" ] || { echo "no rendered conf ($CONF) - is the STA running?"; exit 1; }

python3 "$NRC_SCRIPT_DIR/halow_config.py" apply-live "$CONF"

IFACE=$(grep '^NRC_HALOW_IFACE=' /tmp/nrc_net.state 2>/dev/null | cut -d= -f2)
IFACE=${IFACE:-wlan0}
wpa_cli -i "$IFACE" reconfigure > /dev/null

ST=unknown
for i in $(seq 1 30); do
    ST=$(wpa_cli -i "$IFACE" status 2>/dev/null | grep '^wpa_state=' | cut -d= -f2)
    if [ "$ST" = "COMPLETED" ]; then
        echo "supplicant reconfigured: connected"
        exit 0
    fi
    sleep 1
done
echo "supplicant reconfigured: not connected yet (state=$ST)"
exit 0
