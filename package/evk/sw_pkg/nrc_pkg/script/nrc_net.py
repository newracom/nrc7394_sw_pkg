# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# -*- coding: utf-8 -*-
#
# nrc_net.py - host network environment helpers shared by
# start_modular.py / stop_modular.py (and usable from start.py).
#
# Solves two field problems:
#  1. Interface names differ across hosts (eth0 vs end0/enp1s0, and the
#     NRC HaLow interface is not always wlan0 when the board has onboard
#     Wi-Fi). Names are resolved once here and recorded in a state file
#     so the stop/recovery path tears down exactly what start set up.
#  2. The DHCP client stack differs across hosts (dhcpcd on Raspberry Pi
#     OS legacy, NetworkManager/systemd-networkd on Rockchip/Armbian).
#     start_dhcp_client() picks whatever exists instead of assuming dhcpcd.
#
# Python 2 and 3 both run this file: a manual ./start.py takes the
# "#!/usr/bin/env python" shebang, which is python2 on Raspberry Pi OS
# buster, while the Web GUI path runs python3. No subprocess.run(), no
# f-strings, no DEVNULL.

import os
import re
import subprocess
import time

# Interface names resolved by start and consumed by stop/recovery.
# /tmp is deliberate: the state describes THIS boot only (a reboot
# dismantles the bridge anyway), matching nrc_bridge_takeover.state.
NET_STATE_FILE = "/tmp/nrc_net.state"

BR_IFACE = "br0"

# Interfaces that can never be the upstream ethernet ('wl' covers the
# predictable names wlp*/wlx* as well as wlan*)
_NON_ETH_PREFIX = re.compile(r'^(wl|br|lo$|p2p|mesh|bat|mon|docker|veth|tun|tap)')

# state-file values are interpolated into root shell commands by the stop
# scripts; accept plausible interface names only
_SAFE_NAME = re.compile(r'^[A-Za-z0-9_.:-]{1,15}$')


def _is_wireless(dev):
    return os.path.isdir('/sys/class/net/%s/phy80211' % dev) or \
           os.path.isdir('/sys/class/net/%s/wireless' % dev)


def _read_first_line(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except (IOError, OSError):
        return ''


def detect_eth_iface(configured='auto', legacy_index=0):
    """Resolve the upstream ethernet interface name.

    Order: explicit configured name -> default-route device ->
    legacy eth<index> -> first eth*/en* device present.
    """
    if configured and str(configured) != 'auto':
        name = str(configured)
        if os.path.exists('/sys/class/net/' + name):
            return name
        # a typo here would bridge nothing and cut the wired session
        print("[!] wired port %s is not on this board - using auto" % name)

    # The device carrying the default route is the upstream by definition.
    try:
        out = subprocess.check_output(
            "ip -4 route show default 2>/dev/null", shell=True).decode()
        for line in out.splitlines():
            m = re.search(r'\bdev\s+(\S+)', line)
            if m and not _NON_ETH_PREFIX.match(m.group(1)) \
                    and not _is_wireless(m.group(1)):
                return m.group(1)
    except (subprocess.CalledProcessError, OSError):
        pass

    # No usable default route (e.g. cable unplugged): legacy encoding first
    legacy = 'eth' + str(max(int(legacy_index), 0))
    if os.path.exists('/sys/class/net/' + legacy):
        return legacy

    try:
        for dev in sorted(os.listdir('/sys/class/net')):
            if re.match(r'^(eth|en)', dev):
                return dev
    except OSError:
        pass
    return legacy


def detect_halow_iface(configured='auto', default='wlan0'):
    """Discover the NRC HaLow interface of the loaded driver.

    The NRC driver registers its mac80211 phy as 'nrc80211' (see
    'iw phy nrc80211 ...' usage in the start scripts), which makes
    /sys/class/net/*/phy80211/name a reliable anchor even when an
    onboard Wi-Fi chip already owns wlan0.
    """
    if configured and str(configured) != 'auto':
        return str(configured)

    candidates = []
    try:
        for dev in sorted(os.listdir('/sys/class/net')):
            phy = _read_first_line('/sys/class/net/%s/phy80211/name' % dev)
            if phy == 'nrc80211':
                candidates.append(dev)
    except OSError:
        pass

    if not candidates:
        # Fallback: any net device bound to an nrc* driver (covers a
        # renamed phy or a legacy driver build)
        try:
            for dev in sorted(os.listdir('/sys/class/net')):
                drv = '/sys/class/net/%s/device/driver' % dev
                if os.path.islink(drv) and 'nrc' in os.path.basename(os.readlink(drv)):
                    candidates.append(dev)
        except OSError:
            pass

    if candidates:
        # Android-compat kernels (Rockchip BSP) auto-create an extra
        # 'p2p0' netdev on every P2P-capable phy at registration time.
        # It sorts before 'wlan0' and must never win the election:
        # hostapd would run on it while the STA-typed wlan0 drags the
        # firmware into STA mode (see remove_p2p_ifaces()).
        preferred = [d for d in candidates if not d.startswith('p2p')]
        return (preferred or candidates)[0]
    return default


def remove_p2p_ifaces(phy='nrc80211'):
    """Delete p2p* netdevs the kernel auto-created on the NRC phy.

    Rockchip/Android BSP kernels add a 'p2p0' interface to any wiphy
    advertising P2P support. On the HaLow radio it is pure noise: it
    occupies the second VIF slot, gets picked up by NetworkManager as
    a Wi-Fi device, and the S1G firmware asserts if a channel is ever
    programmed through it. Returns the list of removed interfaces.
    """
    removed = []
    try:
        for dev in sorted(os.listdir('/sys/class/net')):
            if not dev.startswith('p2p'):
                continue
            if _read_first_line('/sys/class/net/%s/phy80211/name' % dev) == phy:
                os.system('sudo iw dev %s del 2>/dev/null' % dev)
                removed.append(dev)
    except OSError:
        pass
    return removed


def derive_second_iface(primary):
    """Name for the second (concurrent/relay) HaLow interface.

    wlan0 -> wlan1 (legacy behavior). If that name already belongs to a
    foreign device (onboard Wi-Fi), fall back to <primary>_1.
    """
    m = re.match(r'^(.*?)(\d+)$', primary)
    if m:
        cand = m.group(1) + str(int(m.group(2)) + 1)
    else:
        cand = primary + '1'
    if os.path.exists('/sys/class/net/' + cand):
        # already on the NRC phy: a leftover from a previous run - reuse it
        if _read_first_line('/sys/class/net/%s/phy80211/name' % cand) == 'nrc80211':
            return cand
        # taken by another radio -> pick a name that cannot collide
        return primary + '_1'
    return cand


def write_net_state(eth, wlan, wlan2):
    """Record resolved names for the stop/recovery path (root-owned)."""
    content = ("# generated by start_modular.py - consumed by stop_modular.py,\n"
               "# bridge_ip_move.sh and recovery. Do not edit.\n"
               "NRC_ETH_IFACE=%s\n"
               "NRC_HALOW_IFACE=%s\n"
               "NRC_HALOW_IFACE2=%s\n"
               "NRC_BR_IFACE=%s\n" % (eth, wlan, wlan2, BR_IFACE))
    # rm first: a pre-created file in world-writable /tmp would otherwise
    # keep its foreign owner after tee truncates it
    os.system("sudo rm -f %s; printf '%s' | sudo tee %s > /dev/null"
              % (NET_STATE_FILE, content, NET_STATE_FILE))


def read_net_state():
    """Names recorded by the last start; legacy defaults when absent."""
    state = {
        'eth': 'eth0',
        'wlan': 'wlan0',
        'wlan2': 'wlan1',
        'br': BR_IFACE,
    }
    keymap = {
        'NRC_ETH_IFACE': 'eth',
        'NRC_HALOW_IFACE': 'wlan',
        'NRC_HALOW_IFACE2': 'wlan2',
        'NRC_BR_IFACE': 'br',
    }
    try:
        with open(NET_STATE_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    # values end up inside root shell commands: accept
                    # plausible interface names only (/tmp is world-writable)
                    if k in keymap and _SAFE_NAME.match(v):
                        state[keymap[k]] = v
    except (IOError, OSError):
        pass
    return state


def remove_net_state():
    os.system("sudo rm -f %s 2>/dev/null" % NET_STATE_FILE)


def _cmd_exists(cmd):
    # sbin is not on a plain user's PATH and a manual start.py runs as one,
    # so `command -v dhcpcd` says no on a host that has it
    for d in ('/usr/sbin', '/sbin', '/usr/bin', '/bin', '/usr/local/sbin', '/usr/local/bin'):
        if os.path.exists(os.path.join(d, cmd)):
            return True
    return os.system("command -v %s > /dev/null 2>&1" % cmd) == 0


def iface_ipv4(iface):
    """First IPv4 address of iface, '' when it has none.

    Uses `ip`, not ifconfig: net-tools lives in /sbin, which a manual run
    does not have on its PATH (python2 Popen then dies with ENOENT).
    """
    out = os.popen("ip -4 -o addr show dev %s 2>/dev/null" % iface).read()
    m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)(/\d+)?', out)
    return m.group(1) if m else ''


def dhcp_pid_file(iface):
    return "/tmp/nrc_dhcp_%s.pid" % iface


def start_dhcp_client(iface):
    """Start a DHCP client on iface with whatever this host provides.

    Preference: dhcpcd (Raspberry Pi OS legacy path, config already tuned
    for it) -> dhclient -> udhcpc. Returns the client name or None.
    """
    if _cmd_exists('dhcpcd'):
        # -b: background immediately so the daemon survives no-carrier
        # windows (wpa_supplicant/hostapd may start later)
        os.system("sudo dhcpcd -b %s" % iface)
        return 'dhcpcd'
    if _cmd_exists('dhclient'):
        os.system("sudo dhclient -nw -pf %s %s 2>/dev/null" % (dhcp_pid_file(iface), iface))
        return 'dhclient'
    if _cmd_exists('udhcpc'):
        os.system("sudo udhcpc -b -R -i %s -p %s > /dev/null 2>&1" % (iface, dhcp_pid_file(iface)))
        return 'udhcpc'
    print("[!] WARNING: no DHCP client available (dhcpcd/dhclient/udhcpc)")
    print("[!]          %s will not get a dynamic IP address" % iface)
    return None


def stop_dhcp_client(iface):
    """Stop whatever start_dhcp_client() launched for iface."""
    os.system("sudo dhcpcd -k %s 2>/dev/null" % iface)
    pidfile = dhcp_pid_file(iface)
    if os.path.exists(pidfile):
        pid = _read_first_line(pidfile)
        if pid.isdigit():  # pid files live in world-writable /tmp
            os.system("sudo kill %s 2>/dev/null" % pid)
        os.system("sudo rm -f %s" % pidfile)
    os.system("sudo pkill -f 'dhclient.*[[:space:]]%s$' 2>/dev/null" % iface)
    os.system("sudo pkill -f 'udhcpc.*-i %s' 2>/dev/null" % iface)


def dhcpcd_off_bridge_ports(br='br0'):
    """Take the system DHCP client off the radio's bridge ports.

    A bridge port must carry no address - only br0 does. dhcpcd, started by
    the OS with no interface list, manages every interface it finds, so it
    runs a DHCP client on the HaLow vifs as well: a bridged relay was seen
    holding an address of the AP's wired LAN on its enslaved leg, and
    IPv4LL addresses on the ports that got no answer. Only wireless ports
    are touched; the wired port keeps its own client and its address is
    moved to br0 by bridge_ip_move.sh.
    """
    try:
        ports = os.listdir('/sys/class/net/%s/brif' % br)
    except OSError:
        return []
    done = []
    for port in sorted(ports):
        if not os.path.isdir('/sys/class/net/%s/phy80211' % port):
            continue
        os.system("sudo dhcpcd -k %s >/dev/null 2>&1" % port)
        os.system("sudo ip addr flush dev %s 2>/dev/null" % port)
        done.append(port)
    if done:
        print("[net] %s: dhcpcd kept off the bridge ports (%s)"
              % (br, ", ".join(done)))
    return done


def wait_for_bridge(br, port, timeout=15):
    """Wait until <port> is enslaved into <br> (hostapd creates the bridge
    from its 'bridge=' config line; the old fixed sleep(3) was not enough
    on slower boards)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists('/sys/class/net/%s/brif/%s' % (br, port)):
            return True
        time.sleep(0.5)
    print("[!] WARNING: %s not enslaved into %s within %ds" % (port, br, timeout))
    return False


def wait_for_iface(iface, timeout=15):
    """Wait until a network interface exists."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists('/sys/class/net/%s' % iface):
            return True
        time.sleep(0.5)
    print("[!] WARNING: interface %s did not appear within %ds" % (iface, timeout))
    return False


# ---------------------------------------------------------------------------
# Network window. Every address, DHCP client, DHCP server and NAT
# change of the HaLow stack goes through these functions and is recorded in
# a state file, so teardown() undoes exactly what the last start set up. The
# system dhcpcd/dnsmasq services and /etc/dhcpcd.conf, /etc/dnsmasq.conf are
# never rewritten; the DHCP server is one dnsmasq on a generated conf.
# ---------------------------------------------------------------------------

RUN_DIR = os.environ.get("NRC_NET_RUN_DIR", "/run/halow")
NET_PLAN_FILE = RUN_DIR + "/net.json"
DNSMASQ_CONF = RUN_DIR + "/dnsmasq.conf"
DNSMASQ_PID = RUN_DIR + "/dnsmasq.pid"
DNSMASQ_UNIT = "halow-dnsmasq"
LEASE_FILE = "/var/lib/misc/dnsmasq.leases"
DNS_FORWARD = "8.8.8.8"
DRYRUN = os.environ.get("NRC_NET_DRYRUN") == "1"

_SAFE_CIDR = re.compile(r'^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$')


def _sh(cmd):
    if DRYRUN:
        print("[dry] " + cmd)
        return 0
    return os.system(cmd)


def _sudo(cmd):
    return _sh("sudo " + cmd)


def _sudo_out(cmd):
    if DRYRUN:
        print("[dry] " + cmd)
        return ''
    try:
        with open(os.devnull, 'w') as devnull:
            out = subprocess.check_output("sudo " + cmd, shell=True, stderr=devnull)
        return out.decode() if hasattr(out, 'decode') else out
    except (subprocess.CalledProcessError, OSError):
        return ''


def _write_root_file(path, content):
    """Write a root-owned file under RUN_DIR (tmpfs) via sudo."""
    if os.access(os.path.dirname(path), os.W_OK):
        with open(path, "w") as f:          # test/dry runs point RUN_DIR at a scratch dir
            f.write(content)
        return
    if DRYRUN:
        print("[dry] write %s (%d bytes)" % (path, len(content)))
        return
    p = subprocess.Popen(["sudo", "sh", "-c",
                          "mkdir -p '%s' && cat > '%s'"
                          % (os.path.dirname(path), path)],
                         stdin=subprocess.PIPE)
    p.communicate(content.encode())


def is_ipv4(addr):
    parts = str(addr).split('.')
    return len(parts) == 4 and all(
        p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def split_cidr(cidr, default_prefix=24):
    """'a.b.c.d/nn' -> ('a.b.c.d', nn); a bare address gets default_prefix."""
    s = str(cidr).strip()
    if '/' in s:
        ip, pfx = s.split('/', 1)
        try:
            pfx = int(pfx)
        except ValueError:
            pfx = default_prefix
    else:
        ip, pfx = s, default_prefix
    return ip, max(0, min(32, pfx))


def prefix_to_netmask(pfx):
    bits = (0xffffffff << (32 - pfx)) & 0xffffffff if pfx else 0
    return "%d.%d.%d.%d" % (bits >> 24, (bits >> 16) & 255, (bits >> 8) & 255, bits & 255)


def resolve_pool(default_ip, ap_ip='', dhcp_start='', dhcp_end=''):
    """Fold the configured address and DHCP pool over a mode's defaults.

    Returns (ip, prefix, pool_start, pool_end). Each option is independent
    and optional. Setting only ap_ip moves the pool to .10-.50 of its
    subnet. A bad or out-of-subnet bound is reported and replaced by the
    default so dnsmasq never refuses to start on a silent typo.
    """
    ip, pfx = split_cidr(ap_ip) if str(ap_ip).strip() else (default_ip, 24)
    if not is_ipv4(ip):
        print("[!] ap_ip '%s' is not an IPv4 address - using %s" % (ap_ip, default_ip))
        ip, pfx = default_ip, 24
    net = ip.rsplit('.', 1)[0] + '.'
    bounds = []
    for name, value, tail in (('dhcp_start', dhcp_start, '10'),
                              ('dhcp_end', dhcp_end, '50')):
        v = str(value).strip()
        if v and not is_ipv4(v):
            print("[!] %s '%s' is not an IPv4 address - using %s%s" % (name, v, net, tail))
            v = ''
        elif v and not v.startswith(net):
            print("[!] %s '%s' is outside %s0/%d - using %s%s" % (name, v, net, pfx, net, tail))
            v = ''
        bounds.append(v or net + tail)
    return ip, pfx, bounds[0], bounds[1]


def _load_plan():
    state = {'static': [], 'dhcp_client': [], 'dhcp_server': None,
             'nat': [], 'ip_forward_prev': None, 'sysctl_prev': {},
             'dnsmasq_service_stopped': False}
    try:
        import json
        with open(NET_PLAN_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            state.update(saved)
    except (IOError, OSError, ValueError):
        pass
    return state


def _save_plan(state):
    import json
    _write_root_file(NET_PLAN_FILE, json.dumps(state, indent=1) + "\n")


def read_plan():
    """The addressing the last start applied (for dashboards / stop)."""
    return _load_plan()


def _dhcpcd_denies(iface):
    """True when /etc/dhcpcd.conf has a denyinterfaces line covering iface."""
    import fnmatch
    try:
        with open("/etc/dhcpcd.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("denyinterfaces"):
                    for pat in line.split()[1:]:
                        if fnmatch.fnmatch(iface, pat):
                            return True
    except (IOError, OSError):
        pass
    return False


DHCPCD_CONF = "/etc/dhcpcd.conf"
_LEGACY_NOTE = "# disabled by nrc_pkg: this interface is configured by start.py"
# Lines that belong to an "interface X" block in dhcpcd.conf.
_IFACE_BLOCK_KEYS = ("static", "metric", "nohook", "fallback", "inform",
                     "noipv6", "noipv4", "profile", "env", "require",
                     "option", "ipv4ll", "noipv4ll")


def neutralise_legacy_dhcpcd(iface):
    """Comment out a stanza that fights the stack for this interface.

    The old package wrote "interface wlan0 / static ip_address=..." into
    /etc/dhcpcd.conf by line number, and a board upgraded from it keeps that
    block. dhcpcd then pins the old address instead of asking for a lease
    (station) or races the address the stack sets (AP). Only the block for
    this interface is touched, and only by commenting: the file's global
    settings - persistent, the option lines, slaac - stay as they are.
    Returns True when something was changed.
    """
    try:
        with open(DHCPCD_CONF) as f:
            lines = f.read().splitlines()
    except (IOError, OSError):
        return False
    out, changed, here = [], False, False
    for ln in lines:
        t = ln.strip()
        if t.startswith("#") or not t:
            if not t:
                here = False
            out.append(ln)
            continue
        if t.startswith("interface "):
            here = t.split()[1] == iface
            if here:
                out.append(_LEGACY_NOTE)
                out.append("#" + ln)
                changed = True
                continue
            out.append(ln)
            continue
        if t.startswith("denyinterfaces"):
            keep = [w for w in t.split()[1:] if w != iface]
            if len(keep) != len(t.split()) - 1:
                changed = True
                if keep:
                    out.append("denyinterfaces " + " ".join(keep))
                else:
                    out.append(_LEGACY_NOTE)
                    out.append("#" + ln)
                continue
            out.append(ln)
            continue
        if here and t.split()[0] in _IFACE_BLOCK_KEYS:
            out.append("#" + ln)
            changed = True
            continue
        here = False
        out.append(ln)
    if not changed:
        return False
    if not os.path.exists(DHCPCD_CONF + ".nrc-bak"):
        _sudo("cp -p %s %s.nrc-bak" % (DHCPCD_CONF, DHCPCD_CONF))
    _write_root_file(DHCPCD_CONF, "\n".join(out) + "\n")
    print("[net] %s: commented out a leftover dhcpcd.conf stanza "
          "(backup: %s.nrc-bak)" % (iface, DHCPCD_CONF))
    return True


def _dhcpcd_pins_static(iface):
    """True when /etc/dhcpcd.conf pins a static address on iface.

    A board that once ran as an AP under the old package keeps a stanza like
    "interface wlan0 / static ip_address=192.168.200.1/24". dhcpcd honours it
    instead of asking for a lease, so a station started later comes up with
    the old AP address - or, once something flushes it, with none at all.
    """
    try:
        with open("/etc/dhcpcd.conf") as f:
            here = False
            for line in f:
                t = line.strip()
                if t.startswith("#") or not t:
                    continue
                if t.startswith("interface "):
                    here = t.split()[1] == iface
                    continue
                if here and t.startswith("static ip_address"):
                    return True
    except (IOError, OSError):
        pass
    return False


def _dhcpcd_master_running():
    return os.system("pgrep -x dhcpcd > /dev/null 2>&1") == 0


def set_static_ip(iface, cidr, gateway='', metric=None, flush=True):
    """Give iface a fixed address, taking it away from any DHCP client.

    The system dhcpcd (Raspberry Pi OS legacy manages every interface) is
    told to release and stop on this interface only; the service and the
    ethernet uplink are untouched. IPv4 addresses on the interface are
    flushed first so a stale static stanza in /etc/dhcpcd.conf from an
    older package cannot leave a second address behind.

    flush=False keeps the addresses already on the interface. br0 needs it:
    it carries the management address the bridge took over from eth, and
    flushing that away leaves the board with no way in.
    """
    if not _SAFE_CIDR.match(str(cidr)):
        print("[!] set_static_ip: bad address '%s' for %s - skipped" % (cidr, iface))
        return False
    ip, pfx = split_cidr(cidr)
    cidr = "%s/%d" % (ip, pfx)
    if iface.startswith("wlan"):
        neutralise_legacy_dhcpcd(iface)
    _sudo("nmcli dev set %s managed no 2>/dev/null" % iface)
    if _dhcpcd_master_running():
        _sudo("dhcpcd -k %s > /dev/null 2>&1" % iface)
    if flush:
        _sudo("ip -4 addr flush dev %s 2>/dev/null" % iface)
    _sudo("ip addr replace %s dev %s" % (cidr, iface))
    _sudo("ip link set %s up" % iface)
    if gateway and is_ipv4(gateway):
        m = " metric %d" % int(metric) if metric else ""
        _sudo("ip route replace default via %s dev %s%s" % (gateway, iface, m))
    st = _load_plan()
    st['static'] = [e for e in st['static'] if e.get('iface') != iface]
    st['static'].append({'iface': iface, 'cidr': cidr, 'gateway': gateway or ''})
    _save_plan(st)
    print("[net] %s: static %s%s" % (iface, cidr, (" gw " + gateway) if gateway else ""))
    return True


def start_dhcp_client_recorded(iface):
    """start_dhcp_client() plus the state entry teardown() needs."""
    # dhcpcd reads /etc/dhcpcd.conf: an older package pinned a static address
    # there (or denied the interface), and it then never sends a request at
    # all. The other clients do not read that file, so they are used instead.
    if iface.startswith("wlan"):
        neutralise_legacy_dhcpcd(iface)
    pinned = _dhcpcd_pins_static(iface)
    blocked = _dhcpcd_denies(iface) or pinned
    client = None
    if blocked:
        why = ("pins a static address on" if pinned else "denies")
        for cmd, line in (
                ('dhclient', "dhclient -nw -pf %s %s 2>/dev/null"
                             % (dhcp_pid_file(iface), iface)),
                ('udhcpc', "udhcpc -b -R -i %s -p %s > /dev/null 2>&1"
                           % (iface, dhcp_pid_file(iface)))):
            if _cmd_exists(cmd):
                print("[net] %s: /etc/dhcpcd.conf %s this interface - "
                      "using %s for the lease" % (iface, why, cmd))
                _sudo(line)
                client = cmd
                break
        if not client:
            print("[!] %s: /etc/dhcpcd.conf %s this interface and no other "
                  "DHCP client is installed - the lease will not arrive; "
                  "comment that block out or install dhclient" % (iface, why))
    if not client:
        client = start_dhcp_client(iface)
    if client:
        st = _load_plan()
        if iface not in st['dhcp_client']:
            st['dhcp_client'].append(iface)
        _save_plan(st)
        print("[net] %s: DHCP client (%s)" % (iface, client))
    return client


def prefer_default_route(iface, metric=100):
    """Put this interface's default route ahead of a wired one.

    dhcpcd metrics a wireless lease above a wired port, so a board with a
    control port sends HaLow traffic out the wire. The old package got this
    from "metric 100" in /etc/dhcpcd.conf, which is commented out now.
    """
    out = os.popen("ip -4 route show default dev %s 2>/dev/null" % iface).read()
    for ln in out.splitlines():
        f = ln.split()
        if 'via' not in f:
            continue
        gw = f[f.index('via') + 1]
        cur = int(f[f.index('metric') + 1]) if 'metric' in f else 0
        if cur == int(metric):
            return True
        # metric is part of the route key, so add the new one before dropping the old
        _sudo("ip route replace default via %s dev %s metric %d"
              % (gw, iface, int(metric)))
        _sudo("ip route del default via %s dev %s metric %d 2>/dev/null"
              % (gw, iface, cur))
        print("[net] %s: default route metric %d -> %d" % (iface, cur, metric))
        return True
    return False


def arp_flux_guard():
    """Answer ARP only from the interface that owns the address.

    Needed when br0 shares a subnet with a second NIC (lab setups): without
    it the other NIC answers ARP for br0's address and replies land on the
    wrong port. Previous values are recorded so teardown() restores them.
    """
    st = _load_plan()
    for key, val in (("net.ipv4.conf.all.arp_ignore", "1"),
                     ("net.ipv4.conf.all.arp_announce", "2")):
        if key not in st['sysctl_prev']:
            st['sysctl_prev'][key] = _read_first_line(
                "/proc/sys/" + key.replace('.', '/')) or "0"
        _sudo("sysctl -qw %s=%s" % (key, val))
    _save_plan(st)


def _unit_active(unit):
    return os.system("systemctl is-active --quiet %s 2>/dev/null" % unit) == 0


def start_dhcp_server(ranges, extra_conf_dir=None):
    """Run the one HaLow DHCP server: dnsmasq on a generated conf.

    ranges: [{'iface', 'start', 'end', 'netmask', 'lease'}], one entry per
    served interface (AP, relay AP leg, mesh portal, br0, eth0 in
    dhcp-server mode). The system dnsmasq service is stopped when active
    because both would bind UDP 67. Operator options go in extra_conf_dir
    (*.conf), which the generated conf includes. The process runs as the
    transient unit halow-dnsmasq where systemd-run exists, so it shows in
    systemctl and the journal; otherwise as a plain daemon with a pid file.
    """
    ranges = [r for r in ranges if r.get('iface')]
    if not ranges:
        return False
    lines = ["# generated by nrc_net.py (%s) - do not edit; operator options: %s"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), extra_conf_dir or "n/a"),
             "bind-interfaces", "domain-needed", "bogus-priv",
             "server=" + DNS_FORWARD, "dhcp-leasefile=" + LEASE_FILE]
    for r in ranges:
        lines.append("interface=%s" % r['iface'])
        lines.append("dhcp-range=%s,%s,%s,%s" % (
            r['start'], r['end'], r.get('netmask') or "255.255.255.0",
            r.get('lease') or "24h"))
    if extra_conf_dir and os.path.isdir(extra_conf_dir):
        lines.append("conf-dir=%s,*.conf" % extra_conf_dir)
    _write_root_file(DNSMASQ_CONF, "\n".join(lines) + "\n")

    st = _load_plan()
    if _unit_active("dnsmasq"):
        _sudo("systemctl stop dnsmasq")
        st['dnsmasq_service_stopped'] = True
    _stop_our_dnsmasq()
    started = ''
    dnsmasq = dnsmasq_bin()
    if not dnsmasq:
        print("[!] dnsmasq is not installed - no DHCP server for %s"
              % ", ".join(r['iface'] for r in ranges))
        st['dhcp_server'] = None
        _save_plan(st)
        return False
    if _cmd_exists('systemd-run'):
        rc = _sudo("systemd-run -q --unit %s --collect -p Restart=no "
                   "%s -k -C %s > /dev/null 2>&1" % (DNSMASQ_UNIT, dnsmasq, DNSMASQ_CONF))
        if rc == 0:
            started = 'unit'
    if not started:
        rc = _sudo("%s -C %s --pid-file=%s" % (dnsmasq, DNSMASQ_CONF, DNSMASQ_PID))
        started = 'daemon' if rc == 0 else ''
    if not DRYRUN:
        time.sleep(1)
        alive = _unit_active(DNSMASQ_UNIT) or \
            os.system("pgrep -f 'dnsmasq.*-C %s' > /dev/null 2>&1" % DNSMASQ_CONF) == 0
        if not alive:
            print("[!] dnsmasq did not start - check: journalctl -u %s / %s --test -C %s"
                  % (DNSMASQ_UNIT, dnsmasq, DNSMASQ_CONF))
            started = ''
    st['dhcp_server'] = {'ranges': ranges, 'how': started}
    _save_plan(st)
    for r in ranges:
        print("[net] %s: DHCP server %s - %s (%s)" % (r['iface'], r['start'], r['end'],
                                                       r.get('lease') or "24h"))
    return bool(started)


def dnsmasq_bin():
    """Absolute path of dnsmasq: it lives in /usr/sbin, which is not on a
    plain user's PATH, and systemd-run resolves the name in its own PATH."""
    for p in ("/usr/sbin/dnsmasq", "/sbin/dnsmasq", "/usr/bin/dnsmasq"):
        if os.path.exists(p):
            return p
    return "dnsmasq" if _cmd_exists("dnsmasq") else ""


def _stop_our_dnsmasq():
    _sudo("systemctl stop %s 2>/dev/null" % DNSMASQ_UNIT)
    _sudo("systemctl reset-failed %s 2>/dev/null" % DNSMASQ_UNIT)
    pid = _read_first_line(DNSMASQ_PID)
    if pid.isdigit():
        _sudo("kill %s 2>/dev/null" % pid)
    _sudo("pkill -f 'dnsmasq.*-C %s' 2>/dev/null" % DNSMASQ_CONF)
    # instances older packages started with /tmp configs
    _sudo("pkill -f 'dnsmasq -C /tmp/dnsmasq_' 2>/dev/null")
    _sudo("rm -f %s /tmp/dnsmasq_*.conf /tmp/dnsmasq_*.pid 2>/dev/null" % DNSMASQ_PID)
    # the lease file outlives the server: a run that later takes its address
    # from the upstream LAN would still read yesterday's 192.168.200.x here
    _sudo("rm -f %s 2>/dev/null" % LEASE_FILE)


def start_nat(up, down, masquerade=True, ipv6=False):
    """Forward between down (HaLow side) and up (uplink).

    masquerade=True hides the down side behind up's address (AP -> eth,
    relay with NAT, mesh portal -> eth). masquerade=False forwards both
    ways unmasked (routed relay, mesh AP leg). Rules carry the comment
    'halow' and are recorded, so stop_nat() deletes exactly these.
    """
    st = _load_plan()
    if st['ip_forward_prev'] is None:
        st['ip_forward_prev'] = _read_first_line("/proc/sys/net/ipv4/ip_forward") or "0"
    _sudo("sysctl -qw net.ipv4.ip_forward=1")
    if ipv6:
        _sudo("sysctl -qw net.ipv6.conf.all.forwarding=1")
    if masquerade:
        rules = [('nat', 'POSTROUTING -o %s -j MASQUERADE' % up),
                 ('filter', 'FORWARD -i %s -o %s -m state --state RELATED,ESTABLISHED -j ACCEPT' % (up, down)),
                 ('filter', 'FORWARD -i %s -o %s -j ACCEPT' % (down, up))]
    else:
        rules = [('filter', 'FORWARD -i %s -o %s -j ACCEPT' % (down, up)),
                 ('filter', 'FORWARD -i %s -o %s -j ACCEPT' % (up, down))]
    for table, spec in rules:
        spec = spec + ' -m comment --comment halow'
        t = '-t nat ' if table == 'nat' else ''
        if _sh("sudo iptables %s-C %s > /dev/null 2>&1" % (t, spec)) != 0:
            _sudo("iptables %s-A %s" % (t, spec))
        if [table, spec] not in st['nat']:
            st['nat'].append([table, spec])
    _save_plan(st)
    print("[net] forwarding %s <-> %s%s" % (down, up, " (NAT)" if masquerade else ""))


# rule shapes older packages installed without the comment tag
def _legacy_nat_cleanup():
    for e, w0, w1 in {('eth0', 'wlan0', 'wlan1')}:
        for spec in ("-t nat -D POSTROUTING -o %s -j MASQUERADE" % e,
                     "-D FORWARD -i %s -o %s -m state --state RELATED,ESTABLISHED -j ACCEPT" % (e, w0),
                     "-D FORWARD -i %s -o %s -j ACCEPT" % (w0, e),
                     "-t nat -D POSTROUTING -o %s -j MASQUERADE" % w1,
                     "-D FORWARD -i %s -o %s -m state --state RELATED,ESTABLISHED -j ACCEPT" % (w0, w1),
                     "-D FORWARD -i %s -o %s -j ACCEPT" % (w1, w0),
                     "-t nat -D POSTROUTING -o %s -j MASQUERADE" % w0,
                     "-D FORWARD -i %s -o %s -m state --state RELATED,ESTABLISHED -j ACCEPT" % (w1, w0),
                     "-D FORWARD -i %s -o %s -j ACCEPT" % (w0, w1)):
            _sudo("iptables %s 2>/dev/null" % spec)
    for i0, i1 in {('eth0', 'wlan0'), ('eth0', 'mesh0'), ('eth0', 'bat0'), ('eth0', 'br0'), ('wlan0', 'mesh0')}:
        _sudo("iptables -D FORWARD --in-interface %s -j ACCEPT 2>/dev/null" % i0)
        _sudo("iptables -t nat -D POSTROUTING --out-interface %s -j MASQUERADE 2>/dev/null" % i1)
        _sudo("iptables -D FORWARD --in-interface %s -j ACCEPT 2>/dev/null" % i1)
        _sudo("iptables -t nat -D POSTROUTING --out-interface %s -j MASQUERADE 2>/dev/null" % i0)


def stop_nat(st=None):
    st = st or _load_plan()
    for table, spec in st.get('nat', []):
        t = '-t nat ' if table == 'nat' else ''
        _sudo("iptables %s-D %s 2>/dev/null" % (t, spec))
    _legacy_nat_cleanup()
    st['nat'] = []
    if _unit_active("docker"):
        print("[*] docker is active, leaving ip_forward enabled")
    else:
        prev = st.get('ip_forward_prev')
        _sudo("sysctl -qw net.ipv4.ip_forward=%s" % (prev if prev in ("0", "1") else "0"))
    st['ip_forward_prev'] = None
    return st


# Both spellings exist in the field: this file wrote "NRC eth config", the
# Web GUI writes "NRC eth_config". Removal has to match either.
_ETH_MARK = re.compile(r"^#\s*NRC eth[_ ]config (begin|end)\s*$", re.I)


def clear_eth_static(iface=''):
    """Drop the marked static block a previous eth_ip_mode=static left behind.

    Without this the wired port keeps the fixed address after the setting goes
    back to dhcp, and the two start paths disagree - the Web GUI removes it.
    """
    try:
        with open(DHCPCD_CONF) as f:
            cur = f.read().splitlines()
    except (IOError, OSError):
        return True
    out, skip, found = [], False, False
    blk_iface, blk_cidr = '', ''
    for ln in cur:
        m = _ETH_MARK.match(ln.strip())
        if m:
            skip = m.group(1).lower() == 'begin'
            found = True
            continue
        if skip:
            t = ln.strip()
            if t.startswith('interface '):
                blk_iface = t.split()[1]
            elif t.startswith('static ip_address'):
                blk_cidr = t.split('=', 1)[-1].strip()
            continue
        out.append(ln)
    if not found:
        return True
    _write_root_file(DHCPCD_CONF, "\n".join(out) + "\n")
    # dhcpcd does not reclaim an address it no longer manages, so take it off
    if blk_cidr:
        _sudo("ip addr del %s dev %s 2>/dev/null" % (blk_cidr, blk_iface or iface))
    if _unit_active("dhcpcd"):
        _sudo("systemctl restart dhcpcd")
    print("[net] %s: removed the static wired block from %s" % (iface or "eth", DHCPCD_CONF))
    return True


def configure_eth(iface, mode, static_cidr='', gateway='', dns=''):
    """Wired uplink address: 'dhcp' leaves the OS alone; 'static' and
    'dhcp-server' pin static_cidr through whatever manages the port -
    NetworkManager (nmcli), dhcpcd (/etc/dhcpcd.conf marker block, restarted
    only when the block changed) or plain ip(8). Never recorded for teardown:
    the uplink must survive stop, or the SSH session goes with it.
    """
    if mode not in ('static', 'dhcp-server'):
        return clear_eth_static(iface)
    if not _SAFE_CIDR.match(str(static_cidr)):
        print("[!] eth_ip_mode=%s needs eth_static_ip (CIDR) - %s left as is" % (mode, iface))
        return False
    ip, pfx = split_cidr(static_cidr)
    cidr = "%s/%d" % (ip, pfx)
    dns_list = " ".join(re.split(r'[,\s]+', dns.strip())) if dns.strip() else ""
    con = ''
    if _unit_active("NetworkManager"):
        con = _sudo_out("nmcli -g GENERAL.CONNECTION dev show %s" % iface).strip()
    if con and con != '--':
        _sudo("nmcli con mod '%s' ipv4.method manual ipv4.addresses %s ipv4.gateway '%s' ipv4.dns '%s'"
              % (con, cidr, gateway, dns_list))
        _sudo("nmcli con up '%s' ifname %s > /dev/null 2>&1" % (con, iface))
        print("[net] %s: static %s via NetworkManager" % (iface, cidr))
        return True
    if os.path.exists("/etc/dhcpcd.conf") and _unit_active("dhcpcd"):
        block = ["# NRC eth config begin", "interface " + iface, "static ip_address=" + cidr]
        if gateway:
            block.append("static routers=" + gateway)
        if dns_list:
            block.append("static domain_name_servers=" + dns_list)
        block.append("# NRC eth config end")
        try:
            with open("/etc/dhcpcd.conf") as f:
                cur = f.read().splitlines()
        except (IOError, OSError):
            cur = []
        out, skip, old = [], False, []
        for ln in cur:
            if ln.strip() == block[0]:
                skip = True
            if skip:
                old.append(ln)
            else:
                out.append(ln)
            if ln.strip() == block[-1]:
                skip = False
        if old == block:
            return True                       # already in place - no restart
        _write_root_file("/etc/dhcpcd.conf", "\n".join(out + block) + "\n")
        _sudo("systemctl restart dhcpcd")
        print("[net] %s: static %s via dhcpcd" % (iface, cidr))
        return True
    _sudo("ip addr replace %s dev %s" % (cidr, iface))
    _sudo("ip link set %s up" % iface)
    if gateway and is_ipv4(gateway):
        _sudo("ip route replace default via %s dev %s" % (gateway, iface))
    print("[net] %s: static %s" % (iface, cidr))
    return True


# ebtables: protocol names need /etc/ethertypes, which some images do not
# ship, so the ethertype and the IP protocol go in as numbers (0x0800 = IPv4,
# 17 = UDP). Ports 67:68 are the two DHCP ends.
_EBT_DHCP = "-p 0x0800 --ip-protocol 17 --ip-destination-port 67:68 -j DROP"


def _ebt_rule(chain, rule, on):
    """Add or remove one ebtables rule. Re-arming cannot stack duplicates."""
    while _sudo("ebtables -D %s %s >/dev/null 2>&1" % (chain, rule)) == 0:
        pass
    if not on:
        return True
    return _sudo("ebtables -A %s %s >/dev/null 2>&1" % (chain, rule)) == 0


def dhcp_bridge_isolate(uplink, on=True):
    """Keep DHCP from crossing the bridge while this board serves it.

    A bridge makes the wired LAN and the air one segment, so a DHCP server
    already on that LAN answers our stations first: the operator asked this
    board to address the HaLow side and the stations came up on the LAN's
    addresses instead. One ebtables rule stops DHCP being forwarded between
    bridge ports - a station's request no longer reaches the other server,
    and that server's reply no longer reaches the station. Our own dnsmasq
    is untouched: requests reach it as local delivery, not as forwarding,
    so a device on the wired side can still be served from here.
    """
    if not _cmd_exists('ebtables'):
        if on:
            print("[!] ebtables not available - the wired LAN's DHCP server "
                  "will also answer HaLow stations")
        return False
    ok = _ebt_rule('FORWARD', _EBT_DHCP, on)
    st = _load_plan()
    st['dhcp_isolate'] = (uplink or 'br0') if (on and ok) else None
    _save_plan(st)
    if on and ok:
        print("[net] DHCP does not cross the bridge: stations are addressed "
              "from here, not by the server on %s" % (uplink or "the uplink"))
    elif on:
        print("[!] could not install the ebtables DHCP rule - the wired "
              "LAN's DHCP server will also answer HaLow stations")
    return ok


def teardown():
    """Undo what the last start recorded: DHCP server, DHCP clients, NAT,
    sysctl changes. Static addresses vanish with their interfaces (rmmod /
    brctl delbr), so they are not removed here. Also clears leftovers of
    older packages (/tmp dnsmasq instances, untagged NAT rules)."""
    st = _load_plan()
    _stop_our_dnsmasq()
    for iface in st.get('dhcp_client', []):
        stop_dhcp_client(iface)
    st['dhcp_client'] = []
    st['dhcp_server'] = None
    st = stop_nat(st)
    if st.get('dhcp_isolate') is not None:
        dhcp_bridge_isolate(st.get('dhcp_isolate'), on=False)
        st['dhcp_isolate'] = None
    for key, val in st.get('sysctl_prev', {}).items():
        _sudo("sysctl -qw %s=%s" % (key, val))
    st['sysctl_prev'] = {}
    st['static'] = []
    if st.get('dnsmasq_service_stopped'):
        # ours is down by now, so the board's own server can have UDP 67 back
        _sudo("systemctl start dnsmasq")
    st['dnsmasq_service_stopped'] = False
    _sudo("rm -f %s 2>/dev/null" % NET_PLAN_FILE)


def summary_lines():
    """Human-readable view of the recorded plan (for ifconfig-style output)."""
    st = _load_plan()
    out = []
    for e in st.get('static', []):
        out.append("  %-6s static %s%s" % (e['iface'], e['cidr'],
                                           (" gw " + e['gateway']) if e.get('gateway') else ""))
    for i in st.get('dhcp_client', []):
        out.append("  %-6s DHCP client" % i)
    srv = st.get('dhcp_server') or {}
    for r in srv.get('ranges', []):
        out.append("  %-6s DHCP server %s - %s" % (r['iface'], r['start'], r['end']))
    for table, spec in st.get('nat', []):
        out.append("  iptables %s%s" % ("-t nat " if table == 'nat' else "", spec.split(' -m comment')[0]))
    return out
