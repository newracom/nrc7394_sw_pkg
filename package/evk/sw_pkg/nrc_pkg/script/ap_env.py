#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

"""ap_env.py - Publish live Unified AP info to /run/halow/ap.env.

Unified AP P0: single source of truth.
  - MAC comes from the chip serial flash (driver loaded without macaddr=),
    read back from /sys/class/net/<iface>/address.
  - Channel comes from the hostapd conf, read back via `hostapd_cli status`
    and mapped to the S1G frequency with the driver's channel table
    (generated from nrc-s1g.c). cli_app is used only as a fallback when
    the table lookup fails.

The vendor gateway container bind-mounts /run/halow read-only and its
entrypoint (gateway/run.sh) sources ap.env instead of hardcoding
mac/chBeacon.

Usage:
    sudo python3 ap_env.py --country US [--iface wlan0] [--output PATH]

Called automatically at the end of start_modular.py AP bring-up.
"""

import argparse
import os
import re
import subprocess
import sys

NET_STATE_FILE = "/tmp/nrc_net.state"
DEFAULT_OUTPUT = "/run/halow/ap.env"
HOSTAPD_CTRL = "/var/run/hostapd"
HOSTAPD_CLI_CANDIDATES = ["/sbin/hostapd_cli", "/usr/sbin/hostapd_cli", "hostapd_cli"]
CLI_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli_app")

# Countries that share the EU S1G channel plan. start_modular.py accepts these
# individual codes (there is no "EU" country code) and resolves them to the EU
# conf directory; the channel table below is keyed the same way.
EU_COUNTRIES = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
                "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
                "NL", "PL", "PT", "RO", "SE", "SI", "SK", "GB", "SA"]


def table_key(country):
    """Map a configured country code to its S1G_CH_TABLE / conf-file key."""
    c = (country or "").upper()
    return "EU" if c in EU_COUNTRIES else c


# Generated from NRC_MACSW umac/umac_s1g_channel.c (CHANNEL_MAPPING_TABLE)
# {country: {hostapd_channel: (s1g_freq_01mhz, bw_code, nons1g_freq_mhz)}}
# hostapd_channel = nons1g_freq_index (the number in the hostapd conf);
# bw_code: 0=1M,1=2M,2=4M; nons1g_freq_mhz = chan_switch target freq
S1G_CH_TABLE = {
    'US': {1: (9025, 0, 2412), 2: (9030, 1, 2417), 3: (9035, 0, 2422), 5: (9045, 0, 2432), 6: (9050, 1, 2437), 7: (9055, 0, 2442), 8: (9060, 2, 2447), 9: (9065, 0, 2452), 10: (9070, 1, 2457), 11: (9075, 0, 2462), 36: (9085, 0, 5180), 37: (9095, 0, 5185), 38: (9105, 0, 5190), 39: (9115, 0, 5195), 40: (9125, 0, 5200), 41: (9135, 0, 5205), 42: (9145, 0, 5210), 43: (9155, 0, 5215), 44: (9165, 0, 5220), 45: (9175, 0, 5225), 46: (9185, 0, 5230), 47: (9195, 0, 5235), 48: (9205, 0, 5240), 50: (9040, 1, 5250), 52: (9060, 1, 5260), 54: (9080, 1, 5270), 56: (9100, 1, 5280), 58: (9120, 1, 5290), 60: (9140, 1, 5300), 62: (9160, 1, 5310), 64: (9180, 1, 5320), 66: (9200, 1, 5330), 68: (9220, 1, 5340), 70: (9240, 1, 5350), 72: (9260, 1, 5360), 76: (9050, 2, 5380), 80: (9090, 2, 5400), 84: (9130, 2, 5420), 88: (9170, 2, 5440), 92: (9210, 2, 5460), 96: (9250, 2, 5480), 100: (9255, 0, 5500), 104: (9265, 0, 5520), 108: (9275, 0, 5540), 112: (9270, 1, 5560), 116: (9260, 2, 5580), 149: (9215, 0, 5745), 150: (9225, 0, 5750), 151: (9235, 0, 5755), 152: (9245, 0, 5760), 153: (9090, 1, 5765), 154: (9110, 1, 5770), 155: (9130, 1, 5775), 156: (9150, 1, 5780), 157: (9170, 1, 5785), 158: (9190, 1, 5790), 159: (9210, 1, 5795), 160: (9230, 1, 5800), 161: (9250, 1, 5805), 162: (9100, 2, 5810), 163: (9140, 2, 5815), 164: (9180, 2, 5820), 165: (9220, 2, 5825)},
    'JP': {1: (8470, 0, 2412), 2: (8480, 0, 2417), 3: (8490, 0, 2422), 4: (8500, 0, 2427), 5: (8510, 0, 2432), 6: (8520, 0, 2437), 7: (8530, 0, 2442), 8: (8540, 0, 2447), 9: (8475, 1, 2452), 10: (8495, 1, 2457), 11: (8515, 1, 2462), 12: (8535, 1, 2467), 13: (8485, 2, 2472), 36: (9235, 1, 5180), 37: (9245, 1, 5185), 38: (9255, 1, 5190), 39: (9265, 1, 5195), 40: (9210, 0, 5200), 41: (8525, 2, 5205), 42: (9230, 0, 5210), 43: (9240, 0, 5215), 44: (9250, 0, 5220), 45: (9260, 0, 5225), 46: (9270, 0, 5230), 47: (9245, 2, 5235), 48: (9255, 2, 5240)},
    'K1': {36: (9215, 0, 5180), 37: (9225, 0, 5185)},
    'K2': {36: (9255, 0, 5180), 37: (9265, 0, 5185), 38: (9275, 0, 5190), 39: (9285, 0, 5195), 40: (9295, 0, 5200), 41: (9305, 0, 5205), 42: (9270, 1, 5210), 43: (9290, 1, 5215), 48: (9315, 0, 5240)},
    'TW': {36: (8390, 0, 5180), 37: (8400, 0, 5185), 38: (8410, 0, 5190), 39: (8420, 0, 5195), 40: (8430, 0, 5200), 41: (8440, 0, 5205), 42: (8450, 0, 5210), 43: (8460, 0, 5215), 44: (8470, 0, 5220), 45: (8480, 0, 5225), 46: (8490, 0, 5230), 47: (8500, 0, 5235), 48: (8510, 0, 5240), 149: (8395, 1, 5745), 150: (8415, 1, 5750), 151: (8435, 1, 5755), 152: (8455, 1, 5760), 153: (8475, 1, 5765), 154: (8495, 1, 5770), 155: (8405, 2, 5775), 156: (8445, 2, 5780), 157: (8485, 2, 5785), 158: (9210, 0, 5790), 159: (9220, 0, 5795), 160: (9230, 0, 5800), 161: (9240, 0, 5805), 162: (9215, 1, 5810), 163: (9235, 1, 5815), 164: (9225, 2, 5820)},
    'EU': {36: (8635, 0, 5180), 37: (8645, 0, 5185), 38: (8655, 0, 5190), 39: (8665, 0, 5195), 40: (8675, 0, 5200), 41: (8685, 0, 5205), 42: (8695, 0, 5210), 43: (8640, 1, 5215), 44: (8660, 1, 5220), 45: (8680, 1, 5225), 46: (8650, 2, 5230), 47: (8670, 2, 5235), 149: (9169, 0, 5745), 150: (9179, 0, 5750), 151: (9189, 0, 5755)},
    # CN: table missing
    'NZ': {36: (9155, 0, 5180), 37: (9165, 0, 5185), 38: (9175, 0, 5190), 39: (9185, 0, 5195), 40: (9195, 0, 5200), 41: (9205, 0, 5205), 42: (9215, 0, 5210), 43: (9225, 0, 5215), 44: (9235, 0, 5220), 45: (9245, 0, 5225), 46: (9255, 0, 5230), 47: (9265, 0, 5235), 48: (9275, 0, 5240), 153: (9170, 1, 5765), 154: (9190, 1, 5770), 155: (9210, 1, 5775), 156: (9230, 1, 5780), 157: (9250, 1, 5785), 158: (9270, 1, 5790), 162: (9180, 2, 5810), 163: (9220, 2, 5815), 164: (9260, 2, 5820)},
    'AU': {36: (9155, 0, 5180), 37: (9165, 0, 5185), 38: (9175, 0, 5190), 39: (9185, 0, 5195), 40: (9195, 0, 5200), 41: (9205, 0, 5205), 42: (9215, 0, 5210), 43: (9225, 0, 5215), 44: (9235, 0, 5220), 45: (9245, 0, 5225), 46: (9255, 0, 5230), 47: (9265, 0, 5235), 48: (9275, 0, 5240), 153: (9170, 1, 5765), 154: (9190, 1, 5770), 155: (9210, 1, 5775), 156: (9230, 1, 5780), 157: (9250, 1, 5785), 158: (9270, 1, 5790), 162: (9180, 2, 5810), 163: (9220, 2, 5815), 164: (9260, 2, 5820)},
    'SG': {36: (8665, 0, 5180), 37: (8675, 0, 5185), 38: (8685, 0, 5190), 39: (8680, 1, 5195), 40: (9175, 0, 5200), 41: (9185, 0, 5205), 42: (9195, 0, 5210), 43: (9205, 0, 5215), 44: (9215, 0, 5220), 45: (9225, 0, 5225), 46: (9235, 0, 5230), 47: (9245, 0, 5235), 149: (9180, 1, 5745), 150: (9200, 1, 5750), 151: (9220, 1, 5755), 152: (9240, 1, 5760), 153: (9190, 2, 5765), 154: (9230, 2, 5770)},
    'T2': {36: (9205, 0, 5180), 37: (9215, 0, 5185), 38: (9225, 0, 5190), 39: (9235, 0, 5195), 40: (9245, 0, 5200), 41: (9210, 1, 5205), 42: (9230, 1, 5210), 43: (9220, 2, 5215)},
}


def run(cmd, **kw):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                       universal_newlines=True, **kw)
    except (subprocess.CalledProcessError, OSError):
        return None


def read_net_state():
    state = {}
    try:
        with open(NET_STATE_FILE) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    if re.match(r"^[A-Za-z0-9_.:-]{1,15}$", v):
                        state[k] = v
    except OSError:
        pass
    return state


def read_mac(iface):
    try:
        with open("/sys/class/net/%s/address" % iface) as f:
            return f.read().strip().lower()
    except OSError:
        return None


def make_rfmac(mac):
    b = mac.replace(":", "").upper()
    return b[0:6] + "FFFE" + b[6:12]


def hostapd_status(iface):
    for cli in HOSTAPD_CLI_CANDIDATES:
        out = run([cli, "-p", HOSTAPD_CTRL, "-i", iface, "status"])
        if out:
            return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {}


def cli_app_show_config():
    """Fallback: parse S1G Frequency/Bandwidth from `cli_app show config`."""
    out = run(["sudo", CLI_APP, "show", "config"])
    if not out:
        return None, None
    freq = bw = None
    for line in out.splitlines():
        m = re.match(r"\s*Frequency\s*[: ]\s*([0-9.]+)", line)
        if m:
            f = float(m.group(1))
            freq = int(round(f * 10)) if f < 2000 else int(round(f))
        m = re.match(r"\s*Bandwidth\s*[: ]\s*(\d)", line)
        if m:
            bw = {1: 0, 2: 1, 4: 2}.get(int(m.group(1)), int(m.group(1)))
    return freq, bw


def iface_ipv4(iface):
    out = run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    if out:
        m = re.search(r"inet ([0-9.]+)/", out)
        if m:
            return m.group(1)
    return ""


def mgmt_iface_ip():
    """Management (default-route) interface and its IPv4 address."""
    out = run(["ip", "-4", "route", "show", "default"])
    if out:
        m = re.search(r"\bdev\s+(\S+)", out)
        if m:
            dev = m.group(1)
            return dev, iface_ipv4(dev)
    return "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", required=True, help="country as passed to start_modular.py (US/EU/JP/K1/K2/TW/NZ/AU/CN)")
    ap.add_argument("--iface", default=None, help="HaLow interface (default: from %s)" % NET_STATE_FILE)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    state = read_net_state()
    iface = args.iface or state.get("NRC_HALOW_IFACE", "wlan0")
    country = args.country.upper()

    mac = read_mac(iface)
    if not mac:
        print("[ap_env] ERROR: cannot read MAC of %s" % iface)
        return 1
    if mac.startswith("02:"):
        print("[ap_env] WARNING: %s has a host-generated MAC (%s) - "
              "no MAC in chip serial flash?" % (iface, mac))

    status = hostapd_status(iface)
    channel = int(status["channel"]) if status.get("channel", "").isdigit() else None

    chbeacon = bandwidth = None
    source = "none"
    ckey = table_key(country)
    if channel is not None and ckey in S1G_CH_TABLE and channel in S1G_CH_TABLE[ckey]:
        chbeacon, bandwidth, _nons1g = S1G_CH_TABLE[ckey][channel]
        source = "hostapd+table"
    else:
        chbeacon, bandwidth = cli_app_show_config()
        source = "cli_app"
        if chbeacon is None:
            print("[ap_env] ERROR: could not resolve S1G channel "
                  "(hostapd channel=%s country=%s, cli_app fallback failed)"
                  % (channel, country))
            return 1

    mgmt_dev, mgmt_ip = mgmt_iface_ip()
    ap_ip = iface_ipv4(state.get("NRC_BR_IFACE", "")) or iface_ipv4(iface)

    lines = [
        "# Generated by ap_env.py - do not edit (source: %s)" % source,
        "HALOW_IFACE=%s" % iface,
        "HALOW_MAC=%s" % mac,
        "HALOW_MAC_FLAT=%s" % mac.replace(":", "").upper(),
        "HALOW_RFMAC=%s" % make_rfmac(mac),
        "HALOW_CHANNEL=%s" % (channel if channel is not None else ""),
        "HALOW_CHBEACON=%d" % chbeacon,
        "HALOW_BANDWIDTH=%d" % bandwidth,
        "HALOW_COUNTRY=%s" % country,
        "HALOW_MGMT_IFACE=%s" % mgmt_dev,
        "HALOW_IP=%s" % mgmt_ip,
        "HALOW_AP_IP=%s" % ap_ip,
        "HALOW_READY=1",
    ]

    outdir = os.path.dirname(args.output)
    os.makedirs(outdir, exist_ok=True)
    tmp = args.output + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.rename(tmp, args.output)
    print("[ap_env] wrote %s (mac=%s chBeacon=%d bw=%d via %s)"
          % (args.output, mac, chbeacon, bandwidth, source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
