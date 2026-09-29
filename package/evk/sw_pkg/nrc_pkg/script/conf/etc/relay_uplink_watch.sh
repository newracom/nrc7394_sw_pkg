#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# Watch a relay's (or a bridging station's) uplink leg and keep it usable.
#
# Two things nothing else does:
#   - while the leg is not associated, clear the ignore list and reassociate.
#     On a chain start the node above is not serving yet, the association
#     times out and wpa_supplicant backs off to 90 s per try; a relay two
#     hops down was measured out for 21 minutes.
#   - after it reconnects, ask for the address again. The DHCP client sits on
#     br0, whose carrier never drops (our own AP leg holds it up), so the
#     kernel never tells it the uplink went away - a station without a bridge
#     re-requests by itself, this one would keep the old lease for its full
#     24 h. Asking is unconditional: the network behind the link may have
#     changed its server without becoming unreachable, which is exactly what
#     a gateway ping cannot tell apart.
#
# Usage: relay_uplink_watch.sh <sta_leg> [bridge]    (exits with the stack)

export PATH=$PATH:/sbin:/usr/sbin

STA=${1:-wlan1}
BR=${2:-br0}
NET_PLAN=/run/halow/net.json

# stdout is /dev/null when start.py launches this, so say it where it keeps:
# journalctl -t relay_watch
say() { logger -t relay_watch "$*" 2>/dev/null || echo "[relay_watch] $*"; }
POLL=10			# s between checks
NUDGE=30		# s unassociated before a reassociate
SETTLE=5		# s to let the link settle before asking again

wpa() { wpa_cli -i "$STA" "$@" 2>/dev/null; }
state() { wpa status | sed -n 's/^wpa_state=//p'; }

# Only a bridge this run put a DHCP client on: a static address must not be
# handed to dhcpcd, and a board serving its own pool has nothing to ask for.
br_takes_dhcp()
{
	[ -r "$NET_PLAN" ] || return 1
	python3 - "$NET_PLAN" "$BR" <<-'PY' 2>/dev/null
	import json, sys
	p, br = sys.argv[1], sys.argv[2]
	try:
	    d = json.load(open(p))
	except Exception:
	    sys.exit(1)
	sys.exit(0 if br in (d.get("dhcp_client") or []) else 1)
	PY
}

renew()
{
	say "$BR: uplink came back - asking for the address again"
	# rebind, not release: nobody may answer (the node above is still coming
	# up), and dropping the address first would leave this board with none
	dhcpcd -n "$BR" >/dev/null 2>&1
}

prev=""
down=0
while [ -e /sys/class/net/"$STA" ]; do
	sleep "$POLL"
	st=$(state)
	[ -n "$st" ] || continue	# supplicant gone: the stack is being torn down
	if [ "$st" = "COMPLETED" ]; then
		if [ "$prev" != "COMPLETED" ]; then
			sleep "$SETTLE"
			if [ -d /sys/class/net/"$BR" ] && br_takes_dhcp; then
				renew
			fi
		fi
		down=0
	else
		down=$((down + POLL))
		if [ "$down" -ge "$NUDGE" ]; then
			down=0
			say "$STA: $st for ${NUDGE}s - reassociating"
			wpa bssid_ignore clear >/dev/null 2>&1 || wpa blacklist clear >/dev/null 2>&1
			wpa reassociate >/dev/null 2>&1
		fi
	fi
	prev=$st
done
