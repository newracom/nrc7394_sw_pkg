#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# halow-hostapd.service ExecStart: run hostapd in the foreground on the conf
# rendered by start_modular.py (hostapd=external mode).
set -e
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ -n "$NRC_SCRIPT_DIR" ] || { echo "NRC_SCRIPT_DIR not set"; exit 1; }

# The external hostapd split only exists for the modular AP mode. In other
# modes (STA/SNIFFER, RELAY/MESH which run hostapd inside start_modular, or
# the plain AP whose start.py owns hostapd) this unit has nothing to do -
# exit cleanly so the target stays healthy.
if [ "${STA_TYPE:-1}" != "1" ] || [ "$HOSTAPD_MODE" != "external" ] \
        || [ "${DRIVER_STACK:-modular}" != "modular" ]; then
    echo "not in external-hostapd unified-AP mode (STA_TYPE=${STA_TYPE:-1}," \
         "DRIVER_STACK=${DRIVER_STACK:-modular}); nothing to run"
    exit 0
fi

CONF="$NRC_SCRIPT_DIR/conf/temp_hostapd_config.conf"
[ -f "$CONF" ] || { echo "no rendered conf ($CONF) - halow-wlan.service must run first"; exit 1; }

# re-apply the saved settings so a plain unit restart picks up ssid/psk changes
python3 "$NRC_SCRIPT_DIR/halow_config.py" apply-live "$CONF"

# The radio joins br0 only when hostapd enslaves it (bridge= in the conf),
# and the bridge port flags reset on every (re-)enslavement - so the flood
# guard must be re-armed here, not at bridge bring-up. The helper waits in
# the background for the port to appear in the bridge (halow_flood_off.sh).
if [ -x /usr/lib/halow/halow_flood_off.sh ] && grep -q '^bridge=' "$CONF"; then
    IFACE=$(sed -n 's/^interface=//p' "$CONF" | head -1)
    /usr/lib/halow/halow_flood_off.sh port "${IFACE:-wlan0}" >/dev/null 2>&1 &
fi

if [ "$HOSTAPD_DEBUG" = "1" ]; then
    exec hostapd -dd "$CONF"
fi
exec hostapd "$CONF"
