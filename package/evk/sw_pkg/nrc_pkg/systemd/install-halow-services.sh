#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# Install the Unified AP systemd services (Unified AP P1/P2).
#
# Base install = generic HaLow AP: halow-wlan.service + halow-webd.service (Web
# GUI).
#
# Usage: sudo ./install-halow-services.sh [NRC_SCRIPT_DIR]
#   NRC_SCRIPT_DIR  path to nrc_pkg/script (default: auto = ../script of this file)
#
# After install (enable the member services too - their WantedBy=halow.target
# link is what makes the target pull them in):
#   sudo systemctl enable halow.target halow-wlan halow-hostapd halow-webd
#   sudo systemctl start  halow.target halow-webd
set -e

SCHEMA_ONLY=0
if [ "$1" = "--schema-only" ]; then
    SCHEMA_ONLY=1
    shift
fi

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Unified AP needs the MCP firmware image and the modular driver set. Offering
# it on a package that carries neither only produces a mode that cannot start,
# so drop it from the choices - the same test that picks DEFAULT_DRIVER_STACK.
# install.sh refreshes /etc/halow/schema.json from the package after we run, to
# pick up options a new release added; that puts the mode back, so it calls us
# again with --schema-only afterwards to re-apply this.
trim_operation_mode() {
    [ -f /etc/halow/schema.json ] || return 0
    if [ -f "$SELF_DIR/../sw/firmware/nrc7394_cspi_mcp.bin" ]; then
        _mcp=1
    else
        _mcp=0
    fi
    python3 - "$_mcp" <<'PYEOF' || echo "warn: could not adjust operation_mode in /etc/halow/schema.json"
import json, sys, collections
mcp = sys.argv[1] == "1"
p = "/etc/halow/schema.json"
d = json.load(open(p), object_pairs_hook=collections.OrderedDict)
om = d.get("operation_mode")
if om:
    vals = list(om.get("values", []))
    if mcp:
        if "unified-ap" not in vals:
            vals.insert(0, "unified-ap")
    else:
        vals = [v for v in vals if v != "unified-ap"]
    om["values"] = vals
    if om.get("default") not in vals:
        om["default"] = "ap" if "ap" in vals else (vals[0] if vals else "ap")
    json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
    open(p, "a").write("\n")
    print("operation_mode: default=%s values=%s" % (om["default"], ",".join(vals)))
PYEOF
}

if [ "$SCHEMA_ONLY" = "1" ]; then
    trim_operation_mode
    exit 0
fi
NRC_SCRIPT_DIR=${1:-$(CDPATH= cd -- "$SELF_DIR/../script" && pwd)}
CONF=/etc/halow/halow.conf

# start.py runs every mode; start_modular.py serves unified-ap only and an
# EVK package does not carry it, so its absence is not an error.
[ -f "$NRC_SCRIPT_DIR/start.py" ] || {
    echo "ERROR: start.py not found in $NRC_SCRIPT_DIR"; exit 1; }

install -d /usr/lib/halow /etc/halow
install -m 0755 "$SELF_DIR"/bin/*.sh /usr/lib/halow/
# flood guard: the udev rule needs the helper at a stable path, and the rule
# itself covers hostapd's dynamic WDS ports (wlanX.staN) as they appear
if [ -f "$SELF_DIR/../script/conf/etc/halow_flood_off.sh" ]; then
    install -m 0755 "$SELF_DIR/../script/conf/etc/halow_flood_off.sh" /usr/lib/halow/
    install -m 0644 "$SELF_DIR/90-halow-flood-off.rules" /etc/udev/rules.d/
    udevadm control --reload 2>/dev/null || true
fi
# helpers that are not shell (the test app's config renderer)
for f in "$SELF_DIR"/bin/*.py; do
    [ -e "$f" ] && install -m 0755 "$f" /usr/lib/halow/
done
install -m 0644 "$SELF_DIR"/units/halow-wlan.service \
                "$SELF_DIR"/units/halow-hostapd.service \
                "$SELF_DIR"/units/halow.target /etc/systemd/system/

# Firmware log follower. It is `cli_app -lf` piped into the store, under
# Restart=always, so on a build whose cli_app has no -lf the pipe collapses
# immediately and systemd respawns the pair every five seconds forever -
# a restart loop in the journal, and a Web GUI panel offering controls
# nothing answers. Install and enable it only where the tooling supports it,
# and record the verdict so halow-webd can grey the panel out honestly
# rather than showing a tail that will never fill.
#
# `hlog` and `-lf` arrived together, and `hlog` is the one that appears in
# the command list, so it is what we probe for.
FWLOG_SUPPORTED=0
if [ -x "$NRC_SCRIPT_DIR/cli_app" ] && \
   "$NRC_SCRIPT_DIR/cli_app" help 2>/dev/null | grep -qw hlog; then
    FWLOG_SUPPORTED=1
fi
if [ "$FWLOG_SUPPORTED" = 1 ]; then
    install -m 0644 "$SELF_DIR"/units/halow-fwlog.service /etc/systemd/system/
    # The follower writes here; the directory has to exist before it starts.
    # fwlog/ holds the current firmware log plus its archives -- fwlog-store.py
    # would create it, but making it here keeps the permissions in one place.
    install -d -m 0755 /var/log/halow /var/log/halow/fwlog
else
    echo "cli_app has no firmware-log support - skipping halow-fwlog.service"
    systemctl disable --now halow-fwlog.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/halow-fwlog.service
fi
install -d -m 0755 /var/log/halow

# Web GUI (halow-webd)
if [ -d "$SELF_DIR/../web" ]; then
    install -d /usr/lib/halow/web/static
    install -m 0644 "$SELF_DIR/../web/halow_webd.py" /usr/lib/halow/web/
    # support-bundle report viewer (filled with collected data at bundle time)
    install -m 0644 "$SELF_DIR/../web/report_template.html" /usr/lib/halow/web/
    # CCA scanner bridge (USB NRC5294R1 test board -> scanner tab)
    install -m 0644 "$SELF_DIR/../web/halow_cca.py" /usr/lib/halow/web/
    # an older package left a gateway-only extension module behind
    rm -f /usr/lib/halow/web/halow_ext.py
    rm -rf /usr/lib/halow/web/__pycache__
    # the log hub merged the gateway add-on's own logs - drop what an
    # older package installed
    systemctl disable --now halow-loghub.service 2>/dev/null || true
    rm -f /etc/systemd/system/halow-loghub.service
    rm -rf /usr/lib/halow/loghub
    install -m 0644 "$SELF_DIR/../web/static/index.html" /usr/lib/halow/web/static/
    install -m 0644 "$SELF_DIR"/units/halow-webd.service /etc/systemd/system/
    if [ ! -f /etc/halow/schema.json ]; then
        install -m 0644 "$SELF_DIR/../web/schema.json" /etc/halow/schema.json
        echo "created /etc/halow/schema.json"
    else
        echo "kept existing /etc/halow/schema.json"
    fi
    trim_operation_mode
fi

# The gateway add-on and its health monitor are gone; an upgraded board still
# has their units enabled, so halow.target would keep pulling in a unit whose
# ExecStart no longer exists.
for _u in halow-gateway.service halow-health-mon.service; do
    if [ -f /etc/systemd/system/$_u ]; then
        systemctl disable --now $_u 2>/dev/null || true
        rm -f /etc/systemd/system/$_u
        echo "removed $_u (gateway add-on dropped)"
    fi
done
rm -f /usr/lib/halow/gateway-run.sh /usr/lib/halow/gateway-mode-gate.sh \
      /usr/lib/halow/mcp-beacon-int.sh /usr/lib/halow/wait-ap-env.sh \
      /usr/lib/halow/render-testapp-config.py

# Which driver flavour AP mode defaults to. The modular stack exists for the
# Unified AP, and the Unified AP needs the MCP firmware image - a package
# without one cannot start that mode at all (copy.sh refuses rather than
# quietly bringing up an AP with no ESL engine). So the default follows the
# firmware the package actually carries: modular where the MCP image is
# present, the monolithic driver otherwise. An operator who later installs a
# package with MCP gets the Unified AP offered in the Web GUI; the existing
# halow.conf is left alone either way (see the append-only branch below).
DEFAULT_DRIVER_STACK=nrc
if [ -f "$SELF_DIR/../sw/firmware/nrc7394_cspi_mcp.bin" ]; then
    DEFAULT_DRIVER_STACK=modular
fi

if [ ! -f "$CONF" ]; then
    cat > "$CONF" <<EOF
# HaLow service configuration (EnvironmentFile of halow-wlan.service).
# Positional args of start_modular.py.
NRC_SCRIPT_DIR=$NRC_SCRIPT_DIR
STA_TYPE=1
SECURITY=0
COUNTRY=US
# Optional channel override (empty = channel from the country conf file)
CHANNEL=
# L2 bridge mode: 0 = NAT + local DHCP, 1 = bridge HaLow to the wired LAN
BRIDGE=0
# hostapd supervision: external = systemd (halow-hostapd.service) runs hostapd
HOSTAPD_MODE=external
# driver flavour for AP mode: modular (Unified AP, MCP) or nrc (plain AP)
DRIVER_STACK=$DEFAULT_DRIVER_STACK
# whether this build has the firmware log channel (firmware + driver +
# cli_app -lf). 0 = the Web GUI's firmware-log panel says so instead of
# offering controls nothing answers.
FWLOG_SUPPORTED=$FWLOG_SUPPORTED
# Extra key=value args for start_modular.py (position independent)
EXTRA_ARGS=
EOF
    echo "created $CONF"
else
    echo "kept existing $CONF"
    # append keys introduced by later versions
    grep -q '^BRIDGE=' "$CONF" || echo 'BRIDGE=0' >> "$CONF"
    grep -q '^HOSTAPD_MODE=' "$CONF" || echo 'HOSTAPD_MODE=external' >> "$CONF"
    # Re-stamped, not just appended: this one describes the package that is
    # being installed right now, so an upgrade onto a build that gained (or
    # lost) the firmware log has to move the marker with it.
    if grep -q '^FWLOG_SUPPORTED=' "$CONF"; then
        sed -i "s/^FWLOG_SUPPORTED=.*/FWLOG_SUPPORTED=$FWLOG_SUPPORTED/" "$CONF"
    else
        echo "FWLOG_SUPPORTED=$FWLOG_SUPPORTED" >> "$CONF"
    fi
    grep -q '^DRIVER_STACK=' "$CONF" || echo "DRIVER_STACK=$DEFAULT_DRIVER_STACK" >> "$CONF"
fi

systemctl daemon-reload
echo "installed. next:"
echo "  sudo systemctl enable halow.target halow-wlan halow-hostapd halow-webd"
echo "  sudo systemctl start  halow.target halow-webd"
exit 0
