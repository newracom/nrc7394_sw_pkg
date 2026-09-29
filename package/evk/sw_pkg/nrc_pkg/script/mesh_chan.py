#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

"""Point the mesh configuration files at a channel.

Mesh is the one mode start_modular.py takes no channel for: run_mp/run_mpp/
run_map hand wpa_supplicant a shipped country file, and the frequency is
written inside it (conf/<plan>/mp_halow_{open,sae}.conf). Mesh AP adds a
hostapd leg on the same radio, whose own file (map_halow_*.conf) carries the
fake channel number instead. So a configured channel only reaches the radio
if it is written into those files first - which is what this does, from the
same S1G table the rest of the stack uses.

usage: mesh_chan.py <country> <channel>
"""
import os
import re
import sys

import ap_env

CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conf")


def patch(path, subs):
    """Apply (pattern, replacement) pairs line-wise. True when it changed."""
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return False
    out = text
    for pat, rep in subs:
        out = re.sub(pat, rep, out, flags=re.M)
    if out == text:
        return False
    with open(path, "w") as fh:
        fh.write(out)
    return True


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    country, channel = argv[1], argv[2]
    try:
        channel = int(channel)
    except ValueError:
        print("[mesh_chan] '%s' is not a channel number" % argv[2],
              file=sys.stderr)
        return 2

    plan = ap_env.table_key(country)
    row = ap_env.S1G_CH_TABLE.get(plan, {}).get(channel)
    if not row:
        print("[mesh_chan] channel %d is not in the %s channel table - "
              "leaving the mesh configuration alone" % (channel, plan),
              file=sys.stderr)
        return 1
    # the mesh confs speak the non-S1G ("fake") frequency, the same one
    # `iw dev mesh0 info` reports; the channel number is its index
    freq = row[2]
    base = os.path.join(CONF_DIR, plan)

    for name in ("mp_halow_open.conf", "mp_halow_sae.conf"):
        if patch(os.path.join(base, name),
                 [(r"^(\s*)frequency=.*$", r"\g<1>frequency=%d" % freq),
                  (r"^(\s*)freq_list=.*$", r"\g<1>freq_list=%d" % freq),
                  (r"^(\s*)scan_freq=.*$", r"\g<1>scan_freq=%d" % freq)]):
            print("[mesh_chan] %s/%s: channel %d (%d MHz)"
                  % (plan, name, channel, freq))

    # Mesh AP's hostapd leg shares the radio, so it has to name the same
    # channel - and hw_mode follows the fake band the way it does for AP mode
    # (1..14 are the 2.4 GHz entries, everything above lives in 5 GHz).
    hw_mode = "g" if channel <= 14 else "a"
    for name in sorted(os.listdir(base)):
        if name.startswith("map_halow_") and name.endswith(".conf"):
            if patch(os.path.join(base, name),
                     [(r"^channel=.*$", "channel=%d" % channel),
                      (r"^hw_mode=.*$", "hw_mode=%s" % hw_mode)]):
                print("[mesh_chan] %s/%s: channel %d (hw_mode=%s)"
                      % (plan, name, channel, hw_mode))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
