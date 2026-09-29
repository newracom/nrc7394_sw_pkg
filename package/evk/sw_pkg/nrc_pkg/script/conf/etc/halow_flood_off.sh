#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

#
# Keep bridge flooding off the HaLow air.
#
# A Linux bridge floods unknown-unicast and multicast out every port. On an
# AP whose br0 joins a busy upstream LAN to the (narrow) HaLow radio, that
# flooding - unicast ARP refreshes from routers, frames to fdb-aged MACs,
# mDNS/SSDP - saturates the air with retries even though no station wants
# any of it (observed: ~100 frames/s of LAN chatter on a 2M channel, gone
# when the same AP uplinks over quiet LTE). These per-port flags are
# bridge-core features present in every kernel we run on (an earlier
# ebtables rate-limit layer was dropped: silently unavailable on kernels
# without ebtables such as the Rockchip BSP, and redundant under this
# guard).
#
#   flood off        no unknown-unicast flooding out this port. Unicast to a
#                    MAC whose fdb entry AGED OUT is dropped rather than
#                    flooded, so the guard also raises the bridge ageing to
#                    a day - otherwise a device quiet for >5 min would drop
#                    off the LAN until it next transmits.
#   mcast_flood off  no multicast flooding (mDNS/SSDP/...). This also stops
#                    IPv6 ND and ONVIF WS-Discovery over the HaLow bridge;
#                    fdb-learned unicast (every real flow) is unaffected.
#   neigh_suppress   the bridge answers upstream ARP for HaLow-side devices
#                    itself, from neighbour entries the web daemon pins as it
#                    learns each device (see _neigh_sync in halow_webd.py) -
#                    so the request never burns airtime yet the device stays
#                    reachable by IP from the LAN.
#   bcast_flood off  with ARP handled by the proxy above, the remaining
#                    upstream broadcast (other hosts' ARP, NetBIOS, ...) has
#                    no business on the air and is dropped. Not on a port in
#                    station mode: that one is the uplink, and dropping
#                    broadcast there stopped a downstream client's DHCP
#                    request from ever reaching the server. Measured on a
#                    busy office LAN: ~5.5 broadcasts/s, replicated to every
#                    WDS port = ~33 junk frames/s on a 1-2M channel - enough
#                    to starve tag polls. Caveat: a broadcast-flagged DHCP
#                    OFFER/ACK from the LAN server is dropped too (nearly all
#                    clients take the usual unicast reply), and a silent
#                    static-IP device is only reachable after it has spoken
#                    once (DHCP or gratuitous ARP) so the daemon can pin it.
#
# Usage:
#   halow_flood_off.sh apply [prefix]   all current bridge ports named
#                                       <prefix>* (default wlan)
#   halow_flood_off.sh port <dev>       one port; waits up to 15 s for the
#                                       device to join a bridge - the udev
#                                       rule runs this for hostapd's dynamic
#                                       WDS ports (wlanX.staN), which are
#                                       created first and enslaved a moment
#                                       later

export PATH=$PATH:/sbin:/usr/sbin

# Opt-in: the guard arms only where "Bridge air guard" is explicitly on in
# config.json. Every arming path (stack start, hostapd re-enslave, udev
# WDS-port rule, GUI re-enable) funnels through this script, so the choice
# holds across restarts - and a board with no Web UI, hence no config.json,
# keeps plain bridge flooding, which is the v1.3.2 behaviour.
if ! grep -Eq '"bridge_flood_guard"[[:space:]]*:[[:space:]]*"on"' \
		/etc/halow/config.json 2>/dev/null; then
	echo "[halow_flood_off] not enabled (bridge_flood_guard is not on)"
	exit 0
fi

# A port in station mode is our uplink: the relay's wlan1, or the radio of a
# bridging STA. Broadcast has to keep leaving through it, or a device behind
# us never reaches a DHCP server on the other side.
uplink_port()
{
	iw dev "$1" info 2>/dev/null | grep -q "type managed"
}

set_port()
{
	dev=$1
	ok=1
	if bridge link set dev "$dev" flood off mcast_flood off 2>/dev/null; then
		ok=0
	else
		# iproute2 too old or absent: the same knobs through sysfs
		for d in /sys/class/net/*/brif/"$dev"; do
			[ -d "$d" ] || continue
			echo 0 > "$d/unicast_flood" 2>/dev/null || break
			echo 0 > "$d/multicast_flood" 2>/dev/null
			ok=0
			break
		done
	fi
	[ "$ok" -eq 0 ] || return 1
	# ARP proxy + broadcast hygiene. Always via sysfs: iproute2's keyword
	# support for these two varies, the sysfs names do not. Missing files
	# (older kernels) skip silently - the flood/mcast layer above still holds.
	if uplink_port "$dev"; then
		bcast=1
		what="flood/mcast off, bcast kept (uplink), neigh_suppress on"
	else
		bcast=0
		what="flood/mcast/bcast off, neigh_suppress on"
	fi
	for d in /sys/class/net/*/brif/"$dev"; do
		[ -d "$d" ] || continue
		echo 1 > "$d/neigh_suppress" 2>/dev/null
		echo "$bcast" > "$d/broadcast_flood" 2>/dev/null
		# With flooding off, unicast toward an fdb-AGED MAC is dropped, not
		# flooded - so a device that stays quiet longer than the default
		# 300 s ageing (sleeping HaLow devices do) would be unreachable
		# from the LAN until it next transmits. Keep learned entries for a
		# day instead; the value is in centiseconds.
		br=${d%/brif/*}
		echo 8640000 > "$br/bridge/ageing_time" 2>/dev/null
		break
	done
	echo "[halow_flood_off] $dev: $what"
	return 0
}

case $1 in
apply)
	PFX=${2:-wlan}
	n=0
	for d in /sys/class/net/*/brif/${PFX}*; do
		[ -d "$d" ] || continue
		set_port "$(basename "$d")" && n=$((n + 1))
	done
	[ "$n" -gt 0 ] || echo "[halow_flood_off] no ${PFX}* bridge port found"
	;;
port)
	dev=$2
	[ -n "$dev" ] || exit 1
	i=0
	while [ "$i" -lt 30 ]; do
		set_port "$dev" && exit 0
		i=$((i + 1))
		sleep 0.5
	done
	echo "[halow_flood_off] $dev never joined a bridge"
	exit 1
	;;
*)
	echo "Usage: $0 {apply [prefix]|port <dev>}"
	exit 1
	;;
esac
