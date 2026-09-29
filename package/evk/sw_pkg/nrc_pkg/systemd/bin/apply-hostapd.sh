#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# Apply hostapd-level config changes (ssid/passphrase/...) with the minimal
# restart: only hostapd is bounced; the driver and network stay up.
# Preferred path: restart halow-hostapd.service (hostapd=external mode) -
# its ExecStart re-applies the saved settings and its
# ExecStartPost regenerates ap.env. Legacy fallback: in-place restart for
# installs where start_modular still runs hostapd itself.
set -e
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

. /etc/halow/halow.conf
[ "${STA_TYPE:-1}" = "1" ] || [ "${STA_TYPE:-1}" = "3" ] || {
    echo "hostapd apply only makes sense in AP/RELAY mode (STA_TYPE=${STA_TYPE:-1})"; exit 1; }
# The live conf is whatever hostapd was started with: temp_chmgr.conf when
# the channel manager picked the channel, temp_hostapd_config.conf otherwise.
CONF=""
for c in $(ps -o args= -C hostapd 2>/dev/null | tr ' ' '\n' | grep '\.conf$'); do
    if [ -f "$c" ]; then CONF="$c"; break; fi
done
if [ -z "$CONF" ]; then
    for c in "$NRC_SCRIPT_DIR/conf/temp_hostapd_config.conf" \
             "$NRC_SCRIPT_DIR/conf/temp_chmgr.conf"; do
        if [ -f "$c" ]; then CONF="$c"; break; fi
    done
fi
[ -n "$CONF" ] || { echo "no live hostapd conf - is the AP running?"; exit 1; }

# A bridge hostapd had to create is a bridge hostapd deletes on exit, and in
# bridge mode that br0 carries this board's management address (eth is
# enslaved with no address of its own). Snapshot it and put it back before
# the new hostapd starts - which also makes that hostapd a guest on br0.
BR=$(sed -n 's/^bridge=//p' "$CONF" | head -1)
BR_MAC=""; BR_ADDRS=""; BR_PORTS=""; BR_ROUTES=""
if [ -n "$BR" ] && [ -d "/sys/class/net/$BR" ]; then
    BR_MAC=$(cat "/sys/class/net/$BR/address" 2>/dev/null)
    BR_ADDRS=$(ip -o -4 addr show dev "$BR" 2>/dev/null | awk '{print $4}')
    BR_PORTS=$(ls "/sys/class/net/$BR/brif" 2>/dev/null)
    BR_ROUTES=$(ip -4 route show default dev "$BR" 2>/dev/null)
fi

restore_bridge() {
    [ -n "$BR" ] || return 0
    [ -n "$BR_ADDRS$BR_PORTS" ] || return 0
    if [ ! -d "/sys/class/net/$BR" ]; then
        ip link add name "$BR" type bridge 2>/dev/null || return 0
        [ -n "$BR_MAC" ] && ip link set "$BR" address "$BR_MAC" 2>/dev/null
        echo "[apply-hostapd] $BR went down with hostapd - restored"
    fi
    for p in $BR_PORTS; do
        [ -d "/sys/class/net/$p" ] || continue
        [ -d "/sys/class/net/$BR/brif/$p" ] && continue
        ip link set "$p" master "$BR" 2>/dev/null || true
    done
    ip link set "$BR" up 2>/dev/null
    for a in $BR_ADDRS; do
        ip -o -4 addr show dev "$BR" 2>/dev/null | awk '{print $4}' \
            | grep -qx "$a" && continue
        ip addr add "$a" dev "$BR" 2>/dev/null || true
    done
    [ -n "$BR_ROUTES" ] && \
        ip route replace $BR_ROUTES dev "$BR" 2>/dev/null
    return 0
}

if [ "$HOSTAPD_MODE" = "external" ] && \
   [ "${DRIVER_STACK:-modular}" = "modular" ] && \
   [ -f /etc/systemd/system/halow-hostapd.service ]; then
    systemctl stop halow-hostapd.service
    restore_bridge
    systemctl start halow-hostapd.service
    echo "hostapd restarted"
    exit 0
fi

# ---- legacy path (hostapd owned by start_modular) --------------------------
python3 "$NRC_SCRIPT_DIR/halow_config.py" apply-live "$CONF"
# past this point hostapd is down: an abort here would leave the AP off the
# air, so nothing below may end the script on its own
set +e
killall hostapd 2>/dev/null
sleep 1
restore_bridge
# transient unit so hostapd does not live in the caller's cgroup
systemd-run -q --collect --unit halow-hostapd-r$$ hostapd "$CONF" || \
    hostapd -B "$CONF"

# re-enslaving the radio into br0 resets the bridge port flood flags (see
# hostapd-run.sh) - re-arm the guard, or the restart re-opens the LAN
# broadcast floodgate the guard exists to close
if [ -x /usr/lib/halow/halow_flood_off.sh ] && grep -q '^bridge=' "$CONF"; then
    FGIFACE=$(sed -n 's/^interface=//p' "$CONF" | head -1)
    /usr/lib/halow/halow_flood_off.sh port "${FGIFACE:-wlan0}" >/dev/null 2>&1 &
fi

IFACE=$(grep '^interface=' "$CONF" | cut -d= -f2)
for i in $(seq 1 20); do
    if hostapd_cli -p /var/run/hostapd -i "$IFACE" status 2>/dev/null \
        | grep -q '^state=ENABLED'; then
        echo "hostapd restarted on $IFACE"
        exit 0
    fi
    sleep 1
done
echo "hostapd did not reach ENABLED within 20s"
exit 1
