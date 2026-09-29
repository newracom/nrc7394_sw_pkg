#!/bin/bash

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# NEWRACOM HaLow Unified AP - uninstaller (counterpart of install.sh).
#
#   sudo ./uninstall.sh              # remove the software, KEEP the configuration
#   sudo ./uninstall.sh --purge      # also remove the configuration and the package
#   sudo ./uninstall.sh --purge --dts --disable-onboard-wifi-revert   # leave no trace
#
# Default (no options) removes:
#   - the systemd units (stopped and disabled first): halow.target,
#     halow-wlan, halow-hostapd, halow-webd, halow-gateway
#   - the helper scripts and the Web GUI under /usr/lib/halow
#   - the loaded kernel modules (nrc_wlan, nrc-mcp, nrc_core, nrc_spi, nrc)
# and KEEPS:
#   - /etc/halow (halow.conf, config.json with the admin password, schema.json,
#     TLS material) so a later install picks the settings back up
#   - ~/nrc_pkg, any ~/nrc_pkg_backup_* and the device-tree overlay
#
# Options:
#   --purge                  also delete /etc/halow, ~/nrc_pkg, the firmware
#                            copies in /lib/firmware (uni_s1g.bin, nrc*_bd.dat)
#                            and the firmware core dumps.
#                            ~/nrc_pkg_backup_* directories are never touched.
#   --dts                    also remove the dtoverlay=nrc-rpi[-mono] lines from
#                            config.txt and delete both .dtbo files (needs a reboot)
#   --disable-onboard-wifi-revert
#                            undo --disable-onboard-wifi: drop the disable-wifi /
#                            disable-bt overlay lines and the brcmfmac blacklist
#   --keep-modules           do not unload the kernel modules
#   --user NAME              user whose ~/nrc_pkg is removed (default: sudo caller)
#   -n | --dry-run           print what would be done, change nothing
#   -h | --help
set -e

PURGE=0
DO_DTS=0
REVERT_WIFI=0
KEEP_MODULES=0
DRY=0
TARGET_USER=${SUDO_USER:-$(id -un)}
REBOOT_NEEDED=0

log()  { printf '\033[1;36m[uninstall]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -e/{/^set -e/d;p}' "$0" | sed 's/^# \{0,1\}//'; }

# every state change goes through this so --dry-run is honest
RUN() {
    if [ "$DRY" = 1 ]; then
        printf '  would run: %s\n' "$*"
    else
        "$@" || return $?
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --purge)        PURGE=1; shift ;;
        --dts)          DO_DTS=1; shift ;;
        --disable-onboard-wifi-revert) REVERT_WIFI=1; shift ;;
        --keep-modules) KEEP_MODULES=1; shift ;;
        --user)         TARGET_USER=$2; shift 2 ;;
        -n|--dry-run)   DRY=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) usage; die "unknown option: $1" ;;
    esac
done

[ "$(id -u)" = 0 ] || die "run as root: sudo ./uninstall.sh"
[ "$DRY" = 1 ] && log "DRY RUN - nothing will be changed"

USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
UNITS="halow.target halow-wlan.service halow-hostapd.service halow-webd.service halow-gateway.service halow-health-mon.service halow-loghub.service halow-fwlog.service"

# ---- 1. stop the stack ------------------------------------------------------
# halow.target's ExecStop chain (stop-wlan.sh) tears down the bridge, DHCP and
# NAT rules, so stop it before anything is removed.
log "stopping services"
for u in halow.target halow-health-mon.service halow-gateway.service halow-hostapd.service \
         halow-wlan.service halow-webd.service halow-loghub.service halow-fwlog.service; do
    if systemctl list-unit-files "$u" >/dev/null 2>&1 && \
       [ -f "/etc/systemd/system/$u" ]; then
        RUN systemctl stop "$u" || warn "stop $u failed (continuing)"
    fi
done

log "disabling services"
for u in $UNITS; do
    [ -f "/etc/systemd/system/$u" ] && { RUN systemctl disable "$u" || true; }
done

# dump_dir is a module parameter: read it before the module goes away.
CD_DIR=$(cat /sys/module/nrc_core/parameters/dump_dir 2>/dev/null \
         || cat /sys/module/nrc/parameters/dump_dir 2>/dev/null || true)

# ---- 2. kernel modules -----------------------------------------------------
if [ "$KEEP_MODULES" = 0 ]; then
    log "unloading kernel modules"
    for m in nrc_wlan nrc_mcp nrc_core nrc_spi nrc; do
        if lsmod 2>/dev/null | grep -q "^$m "; then
            RUN rmmod "$m" || warn "rmmod $m failed (in use?)"
        fi
    done
fi

# ---- 3. units + helpers ----------------------------------------------------
log "removing systemd units"
for u in $UNITS; do
    [ -f "/etc/systemd/system/$u" ] && RUN rm -f "/etc/systemd/system/$u"
done
RUN systemctl daemon-reload
RUN systemctl reset-failed || true

if [ -d /usr/lib/halow ]; then
    log "removing /usr/lib/halow (helpers + Web GUI)"
    RUN rm -rf /usr/lib/halow
fi

# the flood-guard udev rule points at the helper removed above; an orphaned
# rule would re-run a nonexistent script on every WDS port hotplug
if [ -f /etc/udev/rules.d/90-halow-flood-off.rules ]; then
    log "removing the flood-guard udev rule"
    RUN rm -f /etc/udev/rules.d/90-halow-flood-off.rules
    RUN udevadm control --reload || true
fi

# health monitor binary + hooks (conf is left unless --purge)
[ -f /usr/local/bin/nrc_health_mon ] && RUN rm -f /usr/local/bin/nrc_health_mon
[ -f /usr/local/bin/mcp_test ] && RUN rm -f /usr/local/bin/mcp_test
RUN rm -f /run/halow/mcp-test-config.json /run/halow/testapp-in \
          /run/nrc-health/status.json
[ -d /usr/local/lib/nrc-health ] && RUN rm -rf /usr/local/lib/nrc-health
# runtime dir lives on tmpfs, but drop it when empty so nothing lingers
[ -d /run/nrc-health ] && RUN rmdir --ignore-fail-on-non-empty /run/nrc-health

# ---- 4. purge: configuration, package, firmware ----------------------------
if [ "$PURGE" = 1 ]; then
    if [ -d /etc/halow ]; then
        log "purging /etc/halow (configuration, admin password, TLS)"
        RUN rm -rf /etc/halow
    fi
    [ -f /etc/nrc-health-mon.conf ] && { log "purging /etc/nrc-health-mon.conf"; RUN rm -f /etc/nrc-health-mon.conf; }
    # note: /etc/halow/mcp-test.json (the test-app overlay) is under /etc/halow,
    # already removed by the rm -rf above.
    if [ -n "$USER_HOME" ] && [ -d "$USER_HOME/nrc_pkg" ]; then
        log "purging $USER_HOME/nrc_pkg"
        RUN rm -rf "$USER_HOME/nrc_pkg"
    fi
    for f in /lib/firmware/uni_s1g.bin /lib/firmware/uni_s1g_update.bin \
             /lib/firmware/nrc7394_bd.dat /lib/firmware/nrc7292_bd.dat; do
        [ -f "$f" ] && { log "removing $f"; RUN rm -f "$f"; }
    done
    if [ -n "$USER_HOME" ] && ls -d "$USER_HOME"/nrc_pkg_backup_* >/dev/null 2>&1; then
        log "kept: $(ls -d "$USER_HOME"/nrc_pkg_backup_* | tr '\n' ' ')"
    fi
    # a session outlives the password that made it: purging the config
    # and leaving these behind let an old cookie walk back in
    RUN rm -f /run/halow/ap.env /run/halow/fwlog-store.pid \
              /run/halow/webd-sessions.json
    # captured evidence goes only with --purge: the merged gateway logs, the
    # firmware console archive and the health monitor's event history
    [ -d /var/log/halow-loghub ] && { log "purging /var/log/halow-loghub"; RUN rm -rf /var/log/halow-loghub; }
    [ -d /var/log/halow ] && { log "purging /var/log/halow"; RUN rm -rf /var/log/halow; }
    # dumps outlive every install: the old driver wrote host_core_dump_<n>.bin
    # to the kernel's cwd, "/", so boards carry them for years.
    for f in "${CD_DIR:-/var/log}"/nrc_core_dump_*.bin /host_core_dump_*.bin; do
        [ -f "$f" ] && { log "removing $f"; RUN rm -f "$f"; }
    done
    [ -d /var/lib/nrc-health ] && { log "purging /var/lib/nrc-health"; RUN rm -rf /var/lib/nrc-health; }
else
    log "kept /etc/halow and $USER_HOME/nrc_pkg (use --purge to remove them)"
fi

# ---- 5. device tree --------------------------------------------------------
if [ -d /boot/firmware/overlays ]; then
    OVERLAY_DIR=/boot/firmware/overlays; CONFIG_TXT=/boot/firmware/config.txt
else
    OVERLAY_DIR=/boot/overlays; CONFIG_TXT=/boot/config.txt
fi

drop_config_line() {   # <exact line regex>
    [ -f "$CONFIG_TXT" ] || return 0
    if grep -qE "$1" "$CONFIG_TXT"; then
        RUN sed -i -E "/$1/d" "$CONFIG_TXT"
        REBOOT_NEEDED=1
    fi
}

if [ "$DO_DTS" = 1 ]; then
    log "removing the device-tree overlay"
    drop_config_line '^dtoverlay=nrc-rpi(-mono)?$'
    # nrc-rpi-mono.dtbo only exists on boards installed from an older package
    for f in "$OVERLAY_DIR/nrc-rpi.dtbo" "$OVERLAY_DIR/nrc-rpi-mono.dtbo"; do
        [ -f "$f" ] && RUN rm -f "$f"
    done
else
    [ -f "$CONFIG_TXT" ] && grep -qE '^dtoverlay=nrc-rpi' "$CONFIG_TXT" && \
        log "kept the dtoverlay=nrc-rpi line in $CONFIG_TXT (use --dts to remove)"
fi

if [ "$REVERT_WIFI" = 1 ]; then
    log "re-enabling the onboard WiFi/BT"
    drop_config_line '^dtoverlay=disable-wifi$'
    drop_config_line '^dtoverlay=disable-bt$'
    if [ -f /etc/modprobe.d/raspi-blacklist.conf ]; then
        RUN sed -i '/^blacklist brcmfmac$/d;/^blacklist brcmutil$/d' \
            /etc/modprobe.d/raspi-blacklist.conf
        REBOOT_NEEDED=1
    fi
fi

# ---- summary ---------------------------------------------------------------
echo
if [ "$DRY" = 1 ]; then
    log "dry run finished - nothing was changed"
    exit 0
fi
log "uninstall complete."
if [ "$PURGE" = 1 ]; then
    echo "  removed: units, /usr/lib/halow, /etc/halow, $USER_HOME/nrc_pkg, /lib/firmware images, core dumps"
else
    echo "  removed: units, /usr/lib/halow, kernel modules"
    echo "  kept   : /etc/halow, $USER_HOME/nrc_pkg (reinstall reuses them)"
fi
echo "  note   : the distro hostapd service was disabled at install time and"
echo "           is left disabled - re-enable it if this host needs it."
if [ "$REBOOT_NEEDED" = 1 ]; then
    echo
    echo "  >>> boot configuration changed - reboot to release the SPI/IRQ pins <<<"
fi
exit 0
