#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

import os
import sys
import time
from mesh import *

# Auto-detect script path based on script location
SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
script_path = SCRIPT_DIR + "/"

sys.path.insert(0, SCRIPT_DIR)
import nrc_net

# Interface names the last start recorded (legacy defaults when absent)
NET = nrc_net.read_net_state()

def check_halow_stack():
    """Refuse to run under a live Web GUI stack.

    halow.target owns the driver and hostapd through halow-wlan.service, so
    a manual run would leave two owners of one interface. Stopping it from
    here is not an option: halow-wlan's ExecStop is stop-wlan.sh, which calls
    this same script, so the target would be tearing itself down through us.
    Say what to do and stop.
    """
    # systemd itself invoked us (start-wlan.sh runs start.py as part of
    # halow-wlan.service), so the target being up is expected, not a clash.
    if os.environ.get("INVOCATION_ID"):
        return
    if os.system("systemctl is-active --quiet halow.target 2>/dev/null") != 0:
        return
    print("[*] halow.target is active (Web GUI stack) - stopping it first")
    os.system("sudo systemctl stop halow.target")
    time.sleep(2)


move_script = script_path + "conf/etc/bridge_ip_move.sh"
if os.path.exists(move_script):
    os.system("sudo chmod +x " + move_script + " 2>/dev/null")
    os.system("sudo %s release %s %s" % (move_script, NET['eth'], NET['wlan']))

os.system("sudo hostapd_cli disable 2>/dev/null")
for w in {NET['wlan'], NET['wlan2'], 'wlan0'}:
    os.system("sudo wpa_cli disable %s 2>/dev/null" % w)
os.system("sudo pkill -f relay_uplink_watch.sh 2>/dev/null")
os.system("sudo killall -9 wpa_supplicant 2>/dev/null")
os.system("sudo killall -9 hostapd 2>/dev/null")
os.system("sudo killall -9 wireshark-gtk 2>/dev/null")
os.system("sudo ifconfig br0 down 2>/dev/null")
os.system("sudo brctl delbr br0 2>/dev/null")
# DHCP server, DHCP clients, NAT rules and sysctl changes, exactly as the
# last start recorded them in /run/halow/net.json
nrc_net.teardown()
time.sleep(1)

print("[1] Remove module")
os.system("sudo batctl if del %s > /dev/null 2>&1" % NET['wlan'])
os.system("sudo batctl if del mesh0 > /dev/null 2>&1")
os.system("sudo ifconfig bat0 down > /dev/null 2>&1")
os.system("sudo rmmod nrc 2>/dev/null")
os.system("sudo rm "+script_path+"conf/temp_hostapd_config.conf 2>/dev/null")
os.system("sudo rm "+script_path+"conf/temp_chmgr.conf 2>/dev/null")
time.sleep(2)

# Hand the ethernet back to its network manager now that the wlan
# interfaces are gone (no-op when no bridge takeover happened)
if os.path.exists(move_script):
    os.system("sudo %s resync %s" % (move_script, NET['eth']))

print("[2] Done")
