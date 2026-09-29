# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

import os
import sys
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nrc_net
from mesh_add_peer import addPeer, checkPeer, checkMeshJoin

# Auto-detect script path based on this module's location
MESH_SCRIPT_PATH = os.path.abspath(__file__)
MESH_SCRIPT_DIR = os.path.dirname(MESH_SCRIPT_PATH)
NRC_PKG_BASE = MESH_SCRIPT_DIR  # This points to nrc_pkg/script

#--------------------------------------------------------------------------------#
# Addressing comes from the caller as a dict (start.py mesh_net()): ip, prefix,
# start, end (portal address and DHCP pool), eth (uplink), dnsmasq_d (operator
# dnsmasq options). The defaults below apply when a flow is called without one.
#--------------------------------------------------------------------------------#

MESH_NET_DEFAULT = {'ip': '192.168.200.1', 'prefix': 24,
                    'start': '192.168.200.10', 'end': '192.168.200.50',
                    'eth': 'eth0', 'dnsmasq_d': None}


def _net(net):
    merged = dict(MESH_NET_DEFAULT)
    merged.update(net or {})
    return merged





def mesh_conf(country, name, interface=None):
    """Copy a packaged mesh conf aside and launch from the copy.

    The edits below (no_auto_peer, the portal's interface= line) rewrite the
    file, and doing that to the packaged conf leaves the next run reading
    someone else's leftovers. The mesh ID and credentials are already in the
    shipped file, however they were configured (halow_config).
    """
    src = "%s/conf/%s/%s" % (NRC_PKG_BASE, country, name)
    dst = "%s/conf/temp_%s" % (NRC_PKG_BASE, name)
    os.system("sudo cp %s %s" % (src, dst))
    if interface:
        os.system('sudo sed -i "s/^interface=.*/interface=%s/" %s'
                  % (interface, dst))
    return dst


def set_no_auto_peer(conf, enabled):
    """Manual peering means the supplicant must not peer with everyone.

    The line this writes was never reached before: the command was built as
    "... \" + NRC_PKG_BASE + \"/conf/..." with the concatenation inside the
    string literal, so sed was handed a path that does not exist and said so
    to a log nobody reads. A node configured with a specific peer MAC still
    auto-peered with every node in earshot.
    """
    if enabled:
        os.system('sudo sed -i "s/^\\( *\\)#*no_auto_peer=1/\\1no_auto_peer=1/" '
                  + conf)
    else:
        os.system('sudo sed -i "s/^\\( *\\)#*no_auto_peer=1/\\1#no_auto_peer=1/" '
                  + conf)


def check(interface):
    """The interface's IPv4 address as an 'inet <addr>' line, '' when none."""
    addr = nrc_net.iface_ipv4(interface)
    return "inet " + addr if addr else ''


def isInterface(interface):
    if os.path.isdir("/sys/class/net/" + interface):
        return True
    else:
        return False

def getIpAddr(interface):
    return nrc_net.iface_ipv4(interface)


def getNetAddr(interface):
    addr = nrc_net.iface_ipv4(interface)
    return addr.rsplit('.', 1)[0] if addr else ''


def addBridgeMeshAP(wlan, mesh):
    print("bridge " + wlan + " with " + mesh)
    os.system("sudo brctl addbr br0")
    os.system("sudo brctl addif br0 " + wlan)
    os.system("sudo brctl addif br0 " + mesh)
    os.system("sudo ifconfig br0 up")
    print("[11] Connect and DHCP for Mesh Network")
    # Bound the wait: the lease comes over the mesh from an MPP, so with no
    # peers in range an endless loop rides into the systemd start timeout and
    # tears the whole mesh down. The client stays resident and picks the lease
    # up whenever a peer appears.
    nrc_net.start_dhcp_client_recorded("br0")
    ret = check("br0")
    waited = 0
    while ret == '' and waited < 30:
        print("Waiting for IP")
        time.sleep(1)
        waited = waited + 1
        ret = check("br0")
    if ret == '':
        print("no DHCP lease on br0 yet - the DHCP client keeps trying")
        return
    os.system("sudo ip route add default via " + getNetAddr("br0") + ".1 dev br0")

def removeBridgeMeshAP(wlan, mesh):
    if isInterface("br0"):
        os.system("sudo ifconfig br0 down > /dev/null 2>&1")
        os.system("sudo brctl delif br0 " + wlan + " > /dev/null 2>&1")
        os.system("sudo brctl delif br0 " + mesh + " > /dev/null 2>&1")
        os.system("sudo brctl delbr br0 > /dev/null 2>&1")


def startMeshDHCPServer(interface, net=None):
    """Give the mesh portal its address and serve leases to the points.

    A mesh point with no static address of its own runs a DHCP client and
    waits; without this nothing answers it, so neither end has an address
    and the mesh carries no traffic.
    """
    n = _net(net)
    print("[*] Mesh portal: address %s/%d and DHCP on %s" % (n['ip'], n['prefix'], interface))
    nrc_net.set_static_ip(interface, "%s/%d" % (n['ip'], n['prefix']))
    nrc_net.start_dhcp_server([{'iface': interface, 'start': n['start'],
                                'end': n['end'],
                                'netmask': nrc_net.prefix_to_netmask(n['prefix'])}],
                              n['dnsmasq_d'])



def startMeshNAT(intf0, intf1):
    """Forward between the mesh side (intf1) and the uplink (intf0)."""
    nrc_net.start_nat(intf0, intf1, ipv6=True)


def stopMeshNAT():
    nrc_net.stop_nat()


def addMeshInterface(interface):
    print("[*] Create " + interface + " for concurrent mode")
    os.system("sudo iw dev wlan0 interface add " + interface + " type managed")
    os.system("sudo ifconfig " + interface + " up")

def run_mp(interface, country, security, debug, peermac, ip, batman, net=None):
    removeBridgeMeshAP(interface, interface)
    os.system("sudo killall -9 wpa_supplicant")
    os.system("sudo batctl if del wlan0 > /dev/null 2>&1")
    os.system("sudo batctl if del mesh0 > /dev/null 2>&1")
    os.system("sudo ifconfig bat0 down > /dev/null 2>&1")

    if int(debug) == 1:
        debug = '-dddd'
    else:
        debug = ''

    print("[7] Start wpa_supplicant on " + interface)
    if security == 'OPEN':
        conf = mesh_conf(country, "mp_halow_open.conf")
        set_no_auto_peer(conf, peermac != 0)
        os.system("sudo wpa_supplicant -i" + interface + " -c " + conf + " "
                  + debug + " &")
    elif security == 'WPA3-SAE':
        conf = mesh_conf(country, "mp_halow_sae.conf")
        set_no_auto_peer(conf, peermac != 0)
        os.system("sudo wpa_supplicant -i" + interface + " -c " + conf + " "
                  + debug + " &")
    checkMeshJoin(interface)

    if batman != 0:
        os.system("sudo iw dev " + interface + " set mesh_param mesh_fwding 0")
        os.system("sudo batctl -m " + batman + " if add " + interface)
        os.system("sudo ifconfig " + batman + " up")
        os.system("sudo ifconfig " + interface + " mtu 1532")
        intf = batman
    else:
        os.system("sudo iw dev " + interface + " set mesh_param mesh_hwmp_rootmode 2")
        os.system("sudo iw dev " + interface + " set mesh_param mesh_hwmp_root_interval 1000")
        intf = interface

    os.system("sudo iw dev " + interface + " set mesh_param mesh_plink_timeout 0")

    if ip != 0:
        nrc_net.set_static_ip(intf, ip)

    if peermac != 0 and peermac != '00:00:00:00:00:00':
        print("[*] Manual Peering with " + peermac)
        if batman != 0:
            addPeer(interface, peermac, 'batman')
        else:
            addPeer(interface, peermac, '')

    print("[8] Connect and DHCP")
    if ip == 0:
        nrc_net.start_dhcp_client_recorded(intf)
    ret = check(intf)
    waited = 0
    # Bounded: with no mesh peer in range there is no DHCP path at all, and
    # under systemd an endless wait rides into the unit's start timeout and
    # tears the mesh down. The client stays resident and picks the lease up
    # whenever a peer appears.
    while ret == '' and waited < 30:
        print("Waiting for IP")
        time.sleep(1)
        waited = waited + 1
        ret = check(intf)

    print(ret)
    if ret == '':
        print("no DHCP lease yet - the DHCP client keeps trying. HaLow Mesh Point ready")
    else:
        print("IP assigned. HaLow Mesh Point ready")
    print("--------------------------------------------------------------------")
    if peermac != 0 and peermac != '00:00:00:00:00:00':
        if batman != 0:
            checkPeer(interface, peermac, 'batman')
        else:
            checkPeer(interface, peermac, '')

def run_mpp(interface, country, security, debug, peermac, ip, batman, net=None):
    removeBridgeMeshAP(interface, interface)
    os.system("sudo killall -9 wpa_supplicant")
    os.system("sudo batctl if del wlan0 > /dev/null 2>&1")
    os.system("sudo batctl if del mesh0 > /dev/null 2>&1")
    os.system("sudo ifconfig bat0 down > /dev/null 2>&1")

    if int(debug) == 1:
        debug = '-dddd'
    else:
        debug = ''

    print("[7] Start wpa_supplicant on " + interface)
    if security == 'OPEN':
        conf = mesh_conf(country, "mp_halow_open.conf")
        set_no_auto_peer(conf, peermac != 0)
        os.system("sudo wpa_supplicant -i" + interface + " -c " + conf + " "
                  + debug + " &")
    elif security == 'WPA3-SAE':
        conf = mesh_conf(country, "mp_halow_sae.conf")
        set_no_auto_peer(conf, peermac != 0)
        os.system("sudo wpa_supplicant -i" + interface + " -c " + conf + " "
                  + debug + " &")
    checkMeshJoin(interface)

    if ip != 'nodhcp':
        print("[8] Start NAT")
        startMeshNAT(_net(net)['eth'], interface)

    if batman != 0:
        os.system("sudo iw dev " + interface + " set mesh_param mesh_fwding 0")
        os.system("sudo batctl -m " + batman + " if add " + interface)
        os.system("sudo batctl -m " + batman + " if add " + _net(net)['eth'])
        os.system("sudo ifconfig bat0 up")
        os.system("sudo ifconfig " + interface + " mtu 1532")
        intf = batman
    else:
        os.system("sudo iw dev " + interface + " set mesh_param mesh_hwmp_rootmode 2")
        os.system("sudo iw dev " + interface + " set mesh_param mesh_hwmp_root_interval 1000")
        os.system("sudo iw dev " + interface + " set mesh_param mesh_gate_announcements 1")
        if ip == 'nodhcp':
            os.system("sudo ifconfig " + interface + " 0.0.0.0")
            os.system("sudo brctl addbr br0")
            os.system("sudo brctl addif br0 " + _net(net)['eth'])
            os.system("sudo brctl addif br0 " + interface)
            os.system("sudo ifconfig br0 up")
            intf = "br0"
        else:
            # The portal is the address authority for the mesh: it takes the
            # static address and serves the leases the mesh points ask for.
            # Nothing else does - the legacy ip_config.sh path that used to
            # arm dnsmasq only runs when the package sits at /home/pi, and no
            # mesh code starts a DHCP server at all, so a point with no
            # static address of its own never got one and the mesh carried
            # no traffic. This is the same helper AP mode uses, which works
            # wherever the package is installed.
            startMeshDHCPServer(interface, net)
            intf = interface

    os.system("sudo iw dev " + interface + " set mesh_param mesh_plink_timeout 0")

    ret = check(intf)
    waited = 0
    # Bounded for the same reason as run_mp: never park the systemd unit in
    # "activating" forever when no address is coming.
    while ret == '' and waited < 30:
        print("Waiting for IP")
        time.sleep(1)
        waited = waited + 1
        ret = check(intf)
    if ret == '':
        print("no IP on " + intf + " yet - continuing without routes")
    else:
        print("[9] Add Route")
        if ip == 'nodhcp':
            os.system("sudo ip route add " + getNetAddr(intf) + ".0/24 via " + getIpAddr(intf) + " dev " + intf)
            os.system("sudo ip route add " + getNetAddr(intf) + ".0/24 via " + getNetAddr(intf) + ".1 dev " + intf)
        else:
            os.system("sudo ip route add " + getNetAddr(intf) + ".0/24 via " + getIpAddr(intf) + " dev " + intf)
        os.system("route")

        if ip != 0 and ip != 'nodhcp':
            nrc_net.set_static_ip(intf, ip)
            os.system("sudo ip route add " + getNetAddr(intf) + ".0/24 via " + getIpAddr(intf) + " dev " + intf)

    if peermac != 0 and peermac != '00:00:00:00:00:00':
        print("[*] Manual Peering with " + peermac)
        if batman != 0:
            addPeer(interface, peermac, 'batman')
        else:
            addPeer(interface, peermac, '')

    print("[10] ifconfig")
    os.system('sudo ifconfig')
    print("HaLow Mesh Portal ready")
    print("--------------------------------------------------------------------")
    if peermac != 0 and peermac != '00:00:00:00:00:00':
        if batman != 0:
            checkPeer(interface, peermac, 'batman')
        else:
            checkPeer(interface, peermac, '')

def run_map(wlan, mesh, country, security, debug, peermac, ip, batman, net=None):
    removeBridgeMeshAP(wlan, mesh)
    os.system("sudo killall -9 wpa_supplicant")
    os.system("sudo batctl if del wlan0 > /dev/null 2>&1")
    os.system("sudo batctl if del mesh0 > /dev/null 2>&1")
    os.system("sudo ifconfig bat0 down > /dev/null 2>&1")

    if int(debug) == 1:
        debug = '-dddd'
    else:
        debug = ''

    print("[7] Start wpa_supplicant on " + mesh)
    if security == 'OPEN':
        conf = mesh_conf(country, "mp_halow_open.conf")
        set_no_auto_peer(conf, peermac != 0)
        os.system("sudo wpa_supplicant -i" + mesh + " -c " + conf + " "
                  + debug + " &")
    else:
        conf = mesh_conf(country, "mp_halow_sae.conf")
        set_no_auto_peer(conf, peermac != 0)
        os.system("sudo wpa_supplicant -i" + mesh + " -c " + conf + " "
                  + debug + " &")
    checkMeshJoin(mesh)

    if batman != 0:
        os.system("sudo iw dev " + mesh + " set mesh_param mesh_fwding 0")
        os.system("sudo batctl -m " + batman + " if add " + mesh)
        os.system("sudo ifconfig " + batman + " up")
        os.system("sudo ifconfig " + mesh + " mtu 1532")
        intf = batman
    else:
        os.system("sudo iw dev " + mesh + " set mesh_param mesh_hwmp_rootmode 2")
        os.system("sudo iw dev " + mesh + " set mesh_param mesh_hwmp_root_interval 1000")
        intf = mesh

    os.system("sudo iw dev " + mesh + " set mesh_param mesh_plink_timeout 0")

    if ip != 0:
        nrc_net.set_static_ip(intf, ip)

    if peermac != 0 and peermac != '00:00:00:00:00:00':
        print("[*] Manual Peering with " + peermac)
        if batman != 0:
            addPeer(mesh, peermac, 'batman')
        else:
            addPeer(mesh, peermac, '')

    print("[8] Start hostapd on " + wlan)
    if security == 'OPEN':
        conf = mesh_conf(country, "map_halow_open.conf", wlan)
        os.system("sudo hostapd " + conf + " " + debug + " &")
    elif security == 'WPA2-PSK':
        conf = mesh_conf(country, "map_halow_wpa2.conf", wlan)
        os.system("sudo hostapd " + conf + " " + debug + " &")
    elif security == 'WPA3-OWE':
        conf = mesh_conf(country, "map_halow_owe.conf", wlan)
        os.system("sudo hostapd " + conf + " " + debug + " &")
    elif security == 'WPA3-SAE':
        conf = mesh_conf(country, "map_halow_sae.conf", wlan)
        os.system("sudo hostapd " + conf + " " + debug + " &")
    elif security == 'WPA-PBC':
        conf = mesh_conf(country, "map_halow_pbc.conf", wlan)
        os.system("sudo hostapd " + conf + " " + debug + " &")
        time.sleep(1)
        os.system("sudo hostapd_cli wps_pbc")
    time.sleep(1)

    print("[9] Start NAT")
    startMeshNAT(wlan, intf)

    print("[10] Add Bridge")
    addBridgeMeshAP(wlan, intf)

    print("[12] ifconfig")
    os.system('sudo ifconfig')
    print("HaLow Mesh AP ready")
    print("--------------------------------------------------------------------")
    if peermac != 0 and peermac != '00:00:00:00:00:00':
        if batman != 0:
            checkPeer(mesh, peermac, 'batman')
        else:
            checkPeer(mesh, peermac, '')
