#!/bin/bash

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

#
# Move the upstream ethernet IP between the ethernet interface and br0
# so that an established SSH session over that interface survives
# AP bridge setup/teardown (use_bridge_setup > 0, bridge_ip_mode = 1).
#
#   takeover : enslave <eth> into br0 and move its IP/GW/MAC onto br0
#   release  : detach <eth> from br0 and move br0's IP/GW back to <eth>
#              (does NOT restart dhcpcd: at this point in the stop sequence
#              the wlan interfaces are still being torn down and a fresh
#              dhcpcd can wedge on them, leaving <eth> unconfigured)
#   resync   : hand <eth> back to its network manager; call at the END of
#              the stop sequence, after the wlan modules are unloaded.
#              Verifies <eth> keeps an IPv4 address and restores it if the
#              manager fails.
#   bootclean: undo stale takeover config at boot (cron failsafe)
#
# Works with any of the network stacks found in the field:
#   dhcpcd (Raspberry Pi OS legacy), NetworkManager (RPi bookworm, most
#   Rockchip/Armbian images), systemd-networkd, or none of them.
# The interface names default to what start_modular.py recorded in
# /tmp/nrc_net.state, so Rockchip-style names (end0, enp1s0, ...) work.
#
# The bridge keeps the ethernet MAC, so DHCP renewals on br0 return the
# same lease the ethernet interface had.
#
# Usage: bridge_ip_move.sh {takeover|release|resync|bootclean} [eth_iface] [wlan_iface]

export PATH=$PATH:/sbin:/usr/sbin	# brctl/ip live in sbin (cron has a bare PATH)

NET_STATE=/tmp/nrc_net.state
# extract values instead of sourcing: /tmp is world-writable, so a root
# script must never execute that file's content; values are whitelisted
state_get()
{
	sed -n "s/^$1=\([A-Za-z0-9_.:-]\{1,15\}\)\$/\1/p" $NET_STATE 2>/dev/null | head -n1
}
if [ -f $NET_STATE ]; then
	NRC_ETH_IFACE=$(state_get NRC_ETH_IFACE)
	NRC_HALOW_IFACE=$(state_get NRC_HALOW_IFACE)
	NRC_BR_IFACE=$(state_get NRC_BR_IFACE)
fi

CMD=$1
ETH=${2:-${NRC_ETH_IFACE:-eth0}}
WLAN=${3:-${NRC_HALOW_IFACE:-wlan0}}
# "nodhcp": the caller addresses $BR itself (bridge_ip_mode 0/2), so a DHCP
# client on $BR would race the DHCP server the same run puts there. The
# address moved off $ETH stays, which is what keeps the box reachable.
BR_DHCP=${4:-dhcp}
BR=${NRC_BR_IFACE:-br0}
DHCPCD_CONF=/etc/dhcpcd.conf
FAILSAFE_CRON=/etc/cron.d/nrc_bridge_failsafe
RESYNC_MARKER=/tmp/nrc_bridge_resync_needed
# root-only tmpfs: the state file is written and later read (and the pid file
# killed) by root, so neither may live where any user can plant content. A
# takeover done by an older version is still honoured via the legacy path.
STATE_FILE=/run/nrc_bridge_takeover.state
[ -f $STATE_FILE ] || [ ! -f /tmp/nrc_bridge_takeover.state ] || STATE_FILE=/tmp/nrc_bridge_takeover.state
NETWORKD_DROPIN=/run/systemd/network/00-nrc-unmanaged-$ETH.network
BR_DHCP_PID=/run/nrc_dhcp_$BR.pid
[ -f $BR_DHCP_PID ] || [ ! -f /tmp/nrc_dhcp_$BR.pid ] || BR_DHCP_PID=/tmp/nrc_dhcp_$BR.pid
# takeover-state reader - same rule as state_get above: extract, never source
tk_get()
{
	sed -n "s|^$1=\([A-Za-z0-9_./:-]\{1,40\}\)\$|\1|p" $STATE_FILE 2>/dev/null | head -n1
}
BRIDGE_DENY_NOTE=/etc/dhcpcd.conf.nrc-bridge-deny	# written by ip_config_bridge.sh

if [[ $EUID -ne 0 ]]; then
	echo "This script must be run as root"
	exit 1
fi

nm_active()
{
	systemctl is-active --quiet NetworkManager 2>/dev/null
}

networkd_active()
{
	systemctl is-active --quiet systemd-networkd 2>/dev/null
}

dhcpcd_service_active()
{
	systemctl is-active --quiet dhcpcd 2>/dev/null
}

remove_dhcpcd_block()
{
	[ -f $DHCPCD_CONF ] || return
	sed -i '/^# NRC bridge config begin/,/^# NRC bridge config end/d' $DHCPCD_CONF
	# only remove denyinterfaces lines we added (they always contain wlan*);
	# a user's own unrelated denyinterfaces line is left alone
	sed -i '/^denyinterfaces .*wlan\*/d' $DHCPCD_CONF
	# also consume the deny line ip_config_bridge.sh recorded (e.g.
	# "denyinterfaces end0 wlan1" - no wlan* literal), otherwise a dhcpcd
	# host keeps its ethernet denied after stop and is unreachable after
	# the next reboot
	if [ -f $BRIDGE_DENY_NOTE ]; then
		OLD_DENY=$(grep -E '^denyinterfaces [A-Za-z0-9_. -]+$' $BRIDGE_DENY_NOTE | head -n1)
		[ -n "$OLD_DENY" ] && sed -i "\|^$OLD_DENY\$|d" $DHCPCD_CONF
		rm -f $BRIDGE_DENY_NOTE
	fi
}

# Detach the ethernet from whatever manages the network on this host, so
# nothing fights the manual enslavement/IP move below.
#  - NetworkManager: 'nmcli dev set managed no' is runtime-only, so a reboot
#    automatically returns the device to NM (no failsafe needed)
#  - systemd-networkd: a /run drop-in marks the link unmanaged; /run is
#    volatile so a reboot clears it as well
#  - dhcpcd: config-file deny block + failsafe cron (persistent config,
#    handled by bootclean)
unmanage_eth()
{
	if nm_active; then
		echo "[bridge_ip_move] NetworkManager active: unmanaging $ETH"
		nmcli dev set $ETH managed no 2>/dev/null
	fi

	if networkd_active; then
		echo "[bridge_ip_move] systemd-networkd active: unmanaging $ETH"
		mkdir -p /run/systemd/network
		printf '[Match]\nName=%s\n\n[Link]\nUnmanaged=yes\n' "$ETH" > $NETWORKD_DROPIN
		networkctl reload 2>/dev/null
	fi

	if [ -f $DHCPCD_CONF ]; then
		# dhcpcd must stop managing the slave interfaces (including hostapd's
		# per-STA WDS interfaces, e.g. wlan0.sta1) and manage br0 instead.
		# denyinterfaces MUST be at the top of the file: dhcpcd ignores it when
		# it appears after an "interface" stanza (options become per-interface).
		remove_dhcpcd_block
		sed -i "1i denyinterfaces $ETH wlan*" $DHCPCD_CONF
		echo "" >> $DHCPCD_CONF
		echo "# NRC bridge config begin" >> $DHCPCD_CONF
		echo "interface $BR" >> $DHCPCD_CONF
		echo "# NRC bridge config end" >> $DHCPCD_CONF
	fi
}

remanage_eth()
{
	if [ -f $NETWORKD_DROPIN ]; then
		rm -f $NETWORKD_DROPIN
		networkctl reload 2>/dev/null
	fi
	if nm_active; then
		nmcli dev set $ETH managed yes 2>/dev/null
	fi
	remove_dhcpcd_block
}

# Make sure SOME DHCP client keeps renewing br0's lease. On RPi OS this is
# the dhcpcd service; on NM/networkd images (Rockchip etc.) there is no
# dhcpcd, so fall back to dhclient/udhcpc. Without this the copied address
# silently expires hours later - the classic "works, then randomly stops".
start_br_dhcp_client()
{
	stop_br_dhcp_client
	# a new pid file always goes to the root-only path (the /tmp fallback
	# above is only for stopping a client an older version started)
	BR_DHCP_PID=/run/nrc_dhcp_$BR.pid
	rm -f /tmp/nrc_dhcp_$BR.pid

	if dhcpcd_service_active || systemctl is-enabled --quiet dhcpcd 2>/dev/null; then
		# dhcpcd is this host's manager: hand the lease over to it
		# (same MAC -> same address on renewal). Note: merely having the
		# dhcpcd package installed does NOT take this branch - starting a
		# disabled dhcpcd on an NM/networkd host would create a second,
		# competing network manager.
		systemctl restart dhcpcd 2>/dev/null || dhcpcd -b $BR 2>/dev/null
		echo "[bridge_ip_move] $BR DHCP client: dhcpcd"
	elif command -v dhclient >/dev/null; then
		dhclient -nw -pf $BR_DHCP_PID $BR 2>/dev/null
		echo "[bridge_ip_move] $BR DHCP client: dhclient"
	elif command -v udhcpc >/dev/null; then
		udhcpc -b -R -i $BR -p $BR_DHCP_PID >/dev/null 2>&1
		echo "[bridge_ip_move] $BR DHCP client: udhcpc"
	else
		echo "[bridge_ip_move] WARNING: no DHCP client found (dhcpcd/dhclient/udhcpc)"
		echo "[bridge_ip_move] WARNING: $BR keeps the copied address but the lease will"
		echo "[bridge_ip_move] WARNING: NEVER be renewed - expect an outage when it expires"
		return 1
	fi

	# verify: br0 must hold an IPv4 address once the client has settled
	for i in $(seq 1 15); do
		sleep 1
		CUR=$(ip -4 -o addr show dev $BR scope global 2>/dev/null | awk '{print $4}' | head -n1)
		[ -n "$CUR" ] && { echo "[bridge_ip_move] $BR has $CUR"; return 0; }
	done
	echo "[bridge_ip_move] WARNING: $BR has no IPv4 address after DHCP handover;"
	echo "[bridge_ip_move] WARNING: restoring the copied address as a fallback"
	[ -n "$IP" ] && ip addr replace $IP dev $BR
	[ -n "$GW" ] && ip route replace default via $GW dev $BR
	return 1
}

stop_br_dhcp_client()
{
	if [ -f $BR_DHCP_PID ]; then
		PID=$(cat $BR_DHCP_PID 2>/dev/null)
		# pid files live in world-writable /tmp: only accept a number
		[[ "$PID" =~ ^[0-9]+$ ]] && kill "$PID" 2>/dev/null
		rm -f $BR_DHCP_PID
	fi
	pkill -f "dhclient.*[[:space:]]$BR\$" 2>/dev/null
	pkill -f "udhcpc.*-i $BR" 2>/dev/null
}

takeover()
{
	# a 169.254 address is dhcpcd's fallback when no lease arrived, not an
	# address this port owns - carrying it onto $BR would put the bridge on
	# the link-local subnet and probe IPs would ask there
	IP=$(ip -4 -o addr show dev $ETH scope global | awk '{print $4}' \
		| grep -v '^169\.254\.' | head -n1)
	GW=$(ip -4 route show default | awk -v e="$ETH" '$0 ~ ("dev " e) {print $3}' | head -n1)
	MAC=$(cat /sys/class/net/$ETH/address 2>/dev/null)

	if [ -z "$MAC" ]; then
		echo "[bridge_ip_move] ERROR: interface '$ETH' does not exist - aborting takeover"
		echo "[bridge_ip_move] ERROR: set bridge_eth_iface in start_modular.py to the real"
		echo "[bridge_ip_move] ERROR: upstream interface name (e.g. end0) and retry"
		exit 1
	fi

	# take the device away from NM/networkd/dhcpcd BEFORE moving its address,
	# otherwise the manager re-adds addresses to the enslaved port and fights
	# the bridge (intermittent uplink loss on NM-based boards)
	unmanage_eth

	# Failsafe: if the host reboots while the dhcpcd deny config is in place,
	# br0 no longer exists and a denied $ETH would leave it unreachable. Undo
	# the config at boot when no bridge is present. (NM/networkd handling is
	# runtime-only and clears itself on reboot.)
	SELF=$(readlink -f "$0")
	echo "@reboot root sleep 15; /bin/bash $SELF bootclean" > $FAILSAFE_CRON
	rm -f $RESYNC_MARKER	# stale marker from an interrupted stop

	# remember what we moved so release can restore it even if the bridge
	# is dismantled behind our back (hostapd deletes the bridge it created,
	# ejecting $ETH and destroying br0's IP, when it is stopped).
	# Always write the root-only /run path - the /tmp legacy fallback above
	# is for READING an older takeover's state, never a write target.
	STATE_FILE=/run/nrc_bridge_takeover.state
	rm -f /tmp/nrc_bridge_takeover.state
	echo "SAVED_IP=$IP" > $STATE_FILE
	echo "SAVED_GW=$GW" >> $STATE_FILE
	echo "SAVED_ETH=$ETH" >> $STATE_FILE

	echo "[bridge_ip_move] $ETH -> $BR (ip=$IP gw=$GW mac=$MAC)"

	# Docker (or anything loading br_netfilter) makes bridged traffic
	# traverse iptables FORWARD, whose policy docker sets to DROP -- that
	# silently kills upstream<->HaLow bridged IP traffic while ARP still
	# passes. Two layers, so AP/docker start order does not matter:
	# 1) load br_netfilter NOW and disable its iptables hooks -- if it were
	#    loaded later (e.g. by docker) it would default them back on
	# 2) an explicit ACCEPT for bridged frames at the top of FORWARD, in
	#    case something re-enables the hooks while the AP is running
	modprobe br_netfilter 2>/dev/null
	if [ -e /proc/sys/net/bridge/bridge-nf-call-iptables ]; then
		sysctl -q -w net.bridge.bridge-nf-call-iptables=0 \
			net.bridge.bridge-nf-call-ip6tables=0 \
			net.bridge.bridge-nf-call-arptables=0 2>/dev/null
	fi
	iptables -C FORWARD -m physdev --physdev-is-bridged -j ACCEPT 2>/dev/null \
		|| iptables -I FORWARD 1 -m physdev --physdev-is-bridged -j ACCEPT 2>/dev/null

	brctl addbr $BR 2>/dev/null	# no-op when hostapd already created it
	# nodhcp has no lease identity to preserve, so keep the MAC the caller
	# pinned at creation (the AP vif) instead of forcing the ethernet's on
	[ "$BR_DHCP" = "nodhcp" ] || ip link set dev $BR address $MAC
	ip link set dev $ETH up
	ip addr flush dev $WLAN 2>/dev/null
	brctl addif $BR $ETH 2>/dev/null
	ip link set dev $BR up

	# The flush below is the point of no return: if the enslave silently
	# failed (brctl missing, interface state), flushing $ETH and planting
	# the IP on a port-less bridge strands the box - the exact outcome
	# this script exists to prevent. Verify before cutting over.
	if ! brctl show $BR 2>/dev/null | grep -qw $ETH; then
		echo "[bridge_ip_move] ERROR: $ETH did not enslave into $BR - aborting takeover"
		rm -f $FAILSAFE_CRON $STATE_FILE
		remanage_eth
		exit 1
	fi

	# connectivity gap starts here -- keep it minimal
	ip addr flush dev $ETH
	[ -n "$IP" ] && ip addr replace $IP dev $BR
	[ -n "$GW" ] && ip route replace default via $GW dev $BR
	# gap over: the same IP now lives on br0, established TCP sessions resume

	# keep the lease renewed by whatever DHCP client this system has
	if [ "$BR_DHCP" = "nodhcp" ]; then
		# The deny block only reaches a running dhcpcd on a restart, and a
		# restart would hand $BR the lease this mode does not want. Take
		# the slave interfaces off dhcpcd instead, or it re-adds the
		# address to $ETH and the box ends up with two of them.
		if dhcpcd_service_active; then
			dhcpcd -k $ETH >/dev/null 2>&1
			for d in /sys/class/net/wlan*; do
				[ -e "$d" ] && dhcpcd -k "$(basename $d)" >/dev/null 2>&1
			done
			ip -4 addr flush dev $ETH 2>/dev/null
			# dhcpcd re-adopts $BR when its carrier comes back and puts a
			# 169.254 address there; probe IPs would then ask on that subnet
			dhcpcd -k $BR >/dev/null 2>&1
			ip -4 addr show dev $BR 2>/dev/null | grep -o '169\.254\.[0-9.]*/[0-9]*' \
				| while read a; do ip addr del $a dev $BR 2>/dev/null; done
		fi
		echo "[bridge_ip_move] caller addresses $BR; no DHCP client here"
	else
		start_br_dhcp_client
	fi

	# drop anything a stale dhcpcd/config left on the slave interfaces
	ip addr flush dev $ETH 2>/dev/null
	ip -4 addr flush dev $WLAN 2>/dev/null
}

bootclean()
{
	# Runs at boot via $FAILSAFE_CRON: if no bridge with $ETH exists, the
	# takeover config is stale and would leave the host unreachable
	if brctl show $BR 2>/dev/null | grep -qw $ETH; then
		return
	fi
	echo "[bridge_ip_move] bootclean: removing stale bridge config"
	remove_dhcpcd_block
	rm -f $FAILSAFE_CRON $STATE_FILE $RESYNC_MARKER $NETWORKD_DROPIN
	if dhcpcd_service_active || systemctl is-enabled --quiet dhcpcd 2>/dev/null; then
		systemctl restart dhcpcd 2>/dev/null
	fi
}

# The address takeover moved off $ETH, when $BR still carries it. Empty when
# there is no takeover state or the address is gone.
br_saved_addr()
{
	s=$(tk_get SAVED_IP)
	[ -n "$s" ] || return 0
	ip -4 -o addr show dev $BR 2>/dev/null | grep -qw "${s%%/*}" && echo "$s"
}


# The port a caller names can be wrong: stop.py reads it from a state file
# that only the modular start wrote, so it falls back to eth0 while the run
# bridged eth1. The takeover receipt says which port was actually moved, and
# planting the bridge address on any other one blackholes the LAN.
use_saved_eth()
{
	saved=$(tk_get SAVED_ETH)
	[ -n "$saved" ] || return 0
	[ "$saved" = "$ETH" ] && return 0
	[ -e /sys/class/net/$saved ] || return 0
	echo "[bridge_ip_move] takeover was on $saved, not $ETH - using $saved"
	ETH=$saved
}

release()
{
	use_saved_eth
	rm -f $FAILSAFE_CRON
	stop_br_dhcp_client

	# Restore an IP to $ETH only when takeover moved one off it (its state
	# file is the receipt). In STA-bridge mode br0's address is a DHCP lease
	# that arrived over the HaLow side and $ETH never owned it - on a board
	# whose $ETH is a stub (e.g. a camera wired point-to-point) planting the
	# bridge IP plus a default route there outranks the real uplink NIC and
	# blackholes every LAN reply while ARP still answers.
	if [ ! -f $STATE_FILE ]; then
		echo "[bridge_ip_move] no takeover state - flushing $BR only"
		if brctl show $BR 2>/dev/null | grep -qw $ETH; then
			brctl delif $BR $ETH
		fi
		ip addr flush dev $BR 2>/dev/null
		remove_dhcpcd_block
		iptables -D FORWARD -m physdev --physdev-is-bridged -j ACCEPT 2>/dev/null
		ip link set dev $ETH up
		echo "$ETH" > $RESYNC_MARKER
		return
	fi

	if brctl show $BR 2>/dev/null | grep -qw $ETH; then
		# normal path: $ETH still enslaved, take the IP off the bridge.
		# $BR carries two addresses when the run also gave it a static one
		# (bridge_ip_mode 0/2); the one to hand back is the one takeover
		# moved off $ETH, not whichever the kernel lists first.
		IP=$(br_saved_addr)
		# Only an address takeover moved off $ETH belongs back on it. With
		# an empty receipt the port never had one, and whatever $BR holds is
		# the caller's own service address (bridge_ip_mode 0/2) - adopting
		# that would plant the AP address on the port and carry it into the
		# next run, leaving $BR on two bands.
		if [ -z "$IP" ] && [ -n "$(tk_get SAVED_IP)" ]; then
			IP=$(ip -4 -o addr show dev $BR scope global | awk '{print $4}' | head -n1)
		fi
		GW=$(ip -4 route show default | awk -v b="$BR" '$0 ~ ("dev " b) {print $3}' | head -n1)
		if [ -z "$IP" ]; then
			# br0 lost its address (failed DHCP handover at takeover, or
			# an expired lease with no client) - without this fallback
			# $ETH would come back with no IP at all
			IP=$(tk_get SAVED_IP)
			GW=$(tk_get SAVED_GW)
			[ -n "$IP" ] && echo "[bridge_ip_move] $BR has no address - restoring the takeover state"
		fi
		echo "[bridge_ip_move] $BR -> $ETH (ip=$IP gw=$GW)"
		brctl delif $BR $ETH
		ip addr flush dev $BR 2>/dev/null
	else
		# hostapd deletes the bridge it created when it is stopped,
		# ejecting $ETH and destroying br0's IP with it; recover the
		# address from what is left of br0 or from the takeover state
		IP=$(br_saved_addr)
		[ -n "$IP" ] || IP=$(ip -4 -o addr show dev $BR scope global 2>/dev/null | awk '{print $4}' | head -n1)
		GW=$(ip -4 route show default 2>/dev/null | awk -v b="$BR" '$0 ~ ("dev " b) {print $3}' | head -n1)
		if [ -z "$IP" ] && [ -f $STATE_FILE ]; then
			# whitelist-extracted, never sourced (see tk_get)
			IP=$(tk_get SAVED_IP)
			GW=$(tk_get SAVED_GW)
		fi
		if [ -z "$IP" ]; then
			echo "[bridge_ip_move] $ETH not in $BR and nothing to restore"
			remanage_eth
			rm -f $STATE_FILE
			return
		fi
		echo "[bridge_ip_move] $BR already dismantled, restoring $ETH (ip=$IP gw=$GW)"
		ip addr flush dev $BR 2>/dev/null
	fi

	# remove the persistent dhcpcd deny block right away: if the stop
	# sequence dies before "resync", a reboot must not find $ETH denied
	# (the running dhcpcd still denies it in-memory until resync restarts it)
	remove_dhcpcd_block
	rm -f $STATE_FILE

	# bridged-frame ACCEPT is only needed while the bridge exists
	iptables -D FORWARD -m physdev --physdev-is-bridged -j ACCEPT 2>/dev/null

	ip link set dev $ETH up
	[ -n "$IP" ] && ip addr replace $IP dev $ETH
	[ -n "$GW" ] && ip route replace default via $GW dev $ETH

	# The manager is NOT given the device back here: on dhcpcd systems the
	# wlan interfaces are still being torn down and a fresh dhcpcd instance
	# can wedge on them; on NM systems re-managing now would deactivate/
	# reactivate $ETH while stop is still running. The manual address above
	# keeps SSH alive until "resync" runs at the end of the stop sequence.
	echo "$ETH" > $RESYNC_MARKER
}

resync()
{
	if [ ! -f $RESYNC_MARKER ]; then
		echo "[bridge_ip_move] resync not needed"
		return
	fi
	# release resolved the port and left the name here; the receipt itself is
	# gone by now. /tmp is world-writable, so accept only a plain name that
	# exists (same rule as state_get).
	marker=$(sed -n '1s/^\([A-Za-z0-9_.-]\{1,15\}\)$/\1/p' $RESYNC_MARKER 2>/dev/null)
	if [ -n "$marker" ] && [ -e /sys/class/net/$marker ]; then
		ETH=$marker
	fi
	rm -f $RESYNC_MARKER

	# a 169.254 address is dhcpcd's fallback when no lease arrived, not an
	# address this port owns - carrying it onto $BR would put the bridge on
	# the link-local subnet and probe IPs would ask there
	IP=$(ip -4 -o addr show dev $ETH scope global | awk '{print $4}' \
		| grep -v '^169\.254\.' | head -n1)
	GW=$(ip -4 route show default | awk -v e="$ETH" '$0 ~ ("dev " e) {print $3}' | head -n1)

	# hand $ETH back to whatever manages the network on this host
	remanage_eth
	if nm_active; then
		nmcli dev connect $ETH 2>/dev/null &
	fi
	if dhcpcd_service_active || systemctl is-enabled --quiet dhcpcd 2>/dev/null; then
		systemctl restart dhcpcd 2>/dev/null
	fi

	for i in $(seq 1 15); do
		sleep 1
		CUR=$(ip -4 -o addr show dev $ETH scope global | awk '{print $4}' | head -n1)
		if [ -n "$CUR" ]; then
			echo "[bridge_ip_move] resync ok: $ETH has $CUR"
			return
		fi
	done

	echo "[bridge_ip_move] resync: network manager left $ETH unconfigured, restoring $IP"
	[ -n "$IP" ] && ip addr replace $IP dev $ETH
	[ -n "$GW" ] && ip route replace default via $GW dev $ETH
}

case $CMD in
	takeover)
		takeover
		;;
	release)
		release
		;;
	resync)
		resync
		;;
	bootclean)
		bootclean
		;;
	*)
		echo "Usage: $0 {takeover|release|resync|bootclean} [eth_iface] [wlan_iface] [nodhcp]"
		exit 1
		;;
esac
