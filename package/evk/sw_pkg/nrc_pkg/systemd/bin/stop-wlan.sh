#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# halow-wlan.service ExecStop
set -e
[ -n "$NRC_SCRIPT_DIR" ] || { echo "NRC_SCRIPT_DIR not set"; exit 1; }
cd "$NRC_SCRIPT_DIR"
# The cached firmware-log reading describes a firmware that is about to be
# gone; the Web GUI would otherwise keep offering its controls as live.
rm -f /run/halow/fwlog-state.txt
# stop_modular.py only ships with the modular stack; stop.py covers the rest.
[ -f stop_modular.py ] && exec python3 stop_modular.py
exec python3 stop.py
