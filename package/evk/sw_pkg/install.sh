#!/bin/bash

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# NEWRACOM HaLow - one-shot installer.
#
# Run from the extracted package root:
#   sudo ./install.sh
#
# This installs everything; the radio configuration (operating mode, country,
# channel, security, bridge, ...) is done afterwards in the Web GUI at
# https://<board-ip>:8443 - no install-time options for it.
#
# What it does, in order:
#   1. Installs build/runtime dependencies (apt-based systems)
#   2. Copies nrc_pkg/ to the target user's home (~/nrc_pkg, prior copy backed up)
#   3. Builds the driver(s) the package carries, plus cli_app, for the
#      running kernel
#   4. Raspberry Pi: compiles the device-tree overlay and installs it
#   5. Installs the systemd service stack (halow.target, Web GUI)
#   6. Enables at boot and starts (unless a reboot is required first)
#
# Options:
#   --user NAME              target user for ~/nrc_pkg (default: sudo caller)
#   --disable-onboard-wifi   RPi: disable onboard WiFi/BT (overlay + blacklist)
#   --kdir PATH              kernel build tree to compile against
#                            (default: /lib/modules/$(uname -r)/build, else
#                             /usr/src/linux-headers-$(uname -r))
#   --no-deps                skip the apt dependency step (offline boards, or a
#                            board already provisioned - nothing gets upgraded)
#   --no-dts                 skip device-tree overlay installation
#   --no-build               use the nrc.ko and cli_app the package ships
#                            instead of building them. They are built for one
#                            kernel, so uname -r has to match, and the overlay
#                            has to be in place already. Lets you install from
#                            evk/sw_pkg alone, without the package's src/.
#   --no-enable              do not enable services at boot
#   --no-start               install only, do not start the services now
#   -h | --help
set -e

PKG_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# The release package keeps its sources at its own root, and this script
# sits two levels down in evk/sw_pkg. A staged tree keeps them alongside.
SRC_ROOT="$PKG_ROOT/src"
[ -d "$SRC_ROOT" ] || [ ! -d "$PKG_ROOT/../../src" ] || \
    SRC_ROOT=$(CDPATH= cd -- "$PKG_ROOT/../../src" && pwd)

TARGET_USER=${SUDO_USER:-$(id -un)}
DISABLE_ONBOARD_WIFI=0
DO_DEPS=1
DO_DTS=1
DO_BUILD=1
DO_ENABLE=1
DO_START=1
KDIR=""
REBOOT_NEEDED=0
DTBO_CHANGED=0

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# print the whole comment header (stop at the first non-comment line), so
# the help text cannot silently truncate when the option list grows
usage() { sed -n '2,/^set -e/{/^set -e/d;p}' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --user)     TARGET_USER=$2; shift 2 ;;
        --kdir)     KDIR=$2; shift 2 ;;
        --disable-onboard-wifi) DISABLE_ONBOARD_WIFI=1; shift ;;
        --no-deps)  DO_DEPS=0; shift ;;
        --no-dts)   DO_DTS=0; shift ;;
        --no-build) DO_BUILD=0; shift ;;
        --no-enable) DO_ENABLE=0; shift ;;
        --no-start) DO_START=0; shift ;;
        -h|--help)  usage; exit 0 ;;
        *) usage; die "unknown option: $1" ;;
    esac
done

[ "$(id -u)" = 0 ] || die "run as root: sudo ./install.sh"
[ -d "$PKG_ROOT/nrc_pkg" ] || die "nrc_pkg/ not found next to install.sh - run from the package root"

USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[ -n "$USER_HOME" ] && [ -d "$USER_HOME" ] || die "cannot resolve home of user '$TARGET_USER'"

# ---- platform detection -----------------------------------------------------
PLATFORM=generic
BOARD_MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)
case "$BOARD_MODEL" in
    "Raspberry Pi"*) PLATFORM=raspi ;;
    *Rockchip*|*RK35*|*rk35*) PLATFORM=rockchip ;;
esac
log "platform: ${BOARD_MODEL:-unknown} (${PLATFORM}), kernel $(uname -r), user ${TARGET_USER}"

# Note whether this is an upgrade over a running stack - it decides between
# start and restart at the end (see step 7).
STACK_WAS_ACTIVE=0
systemctl is-active --quiet halow.target 2>/dev/null && STACK_WAS_ACTIVE=1
[ "$STACK_WAS_ACTIVE" = 1 ] && log "an active HaLow stack is running - the Web GUI moves to the new package, the radio stays off until you start it"

# ---- 1. dependencies --------------------------------------------------------
install_deps() {
    command -v apt-get >/dev/null 2>&1 || {
        warn "apt-get not found - install build tools, dtc, python3, iw, hostapd manually"
        return 0
    }
    log "installing dependencies (apt)"
    export DEBIAN_FRONTEND=noninteractive
    if ! apt-get update -qq; then
        # EOL Raspbian releases (e.g. Buster) are removed from the live
        # mirrors and every fetch 404s; the same packages still live on the
        # archive host. Swap the mirror and retry once.
        if grep -qsE '^deb .*(raspbian\.raspberrypi\.org|mirrordirector\.raspbian\.org)' /etc/apt/sources.list; then
            warn "apt mirror failed - this Raspbian release looks EOL, switching /etc/apt/sources.list to legacy.raspbian.org"
            cp /etc/apt/sources.list /etc/apt/sources.list.bak-halow-install
            sed -i -E 's#https?://(raspbian\.raspberrypi\.org|mirrordirector\.raspbian\.org)/raspbian#http://legacy.raspbian.org/raspbian#g' \
                /etc/apt/sources.list
            apt-get update -qq || warn "apt-get update still failing (offline?) - trying with the local cache"
        else
            warn "apt-get update failed (offline?) - trying with the local cache"
        fi
    fi
    apt-get install -y --no-install-recommends \
        libnl-genl-3-dev cmake libcjson-dev libreadline-dev \
        build-essential device-tree-compiler python3 iw rfkill net-tools \
        bridge-utils hostapd wpasupplicant rsync \
        || warn "some dependency packages failed to install"

    # STA/relay/mesh need a per-interface DHCP client; NetworkManager images
    # (Debian 12+) ship none of the classic three the start scripts can use.
    if ! command -v dhcpcd >/dev/null 2>&1 && \
       ! command -v dhclient >/dev/null 2>&1 && \
       ! command -v udhcpc >/dev/null 2>&1; then
        apt-get install -y --no-install-recommends dhcpcd-base 2>/dev/null || \
        apt-get install -y --no-install-recommends dhcpcd5 2>/dev/null || \
        apt-get install -y --no-install-recommends isc-dhcp-client 2>/dev/null || \
            warn "no DHCP client installed - STA modes will not get an IP"
    fi

    # our stack runs hostapd itself - keep the distro service out of the way
    systemctl disable --now hostapd >/dev/null 2>&1 || true

    # kernel headers for the out-of-tree driver build
    if [ ! -d "/lib/modules/$(uname -r)/build" ]; then
        if [ "$PLATFORM" = raspi ] && apt-cache show raspberrypi-kernel-headers >/dev/null 2>&1; then
            apt-get install -y raspberrypi-kernel-headers || true
        fi
        [ -d "/lib/modules/$(uname -r)/build" ] || \
            apt-get install -y "linux-headers-$(uname -r)" || true
    fi
}
if [ "$DO_DEPS" = 1 ]; then
    install_deps
else
    log "--no-deps: skipping the apt dependency step"
fi

# Kernel build tree for the out-of-tree modules. Distro kernels expose it as
# /lib/modules/<rel>/build; several vendor BSPs (Rockchip, some RPi images)
# ship the headers under /usr/src without that symlink, so look there too.
detect_kdir() {
    [ -n "$KDIR" ] && return 0
    if [ -d "/lib/modules/$(uname -r)/build" ]; then
        KDIR="/lib/modules/$(uname -r)/build"
        return 0
    fi
    for d in "/usr/src/linux-headers-$(uname -r)" /usr/src/linux-headers-*; do
        if [ -f "$d/Makefile" ]; then
            KDIR="$d"
            return 0
        fi
    done
    return 1
}
# Without --no-build the driver is built here, because a .ko only loads on the
# kernel it was built against. A package copied out of the release tree without
# its src/ cannot do that: say so now, naming the real cause - the old path
# died later complaining about kernel headers, which was not what was missing.
if [ "$DO_BUILD" = 1 ] && [ ! -d "$SRC_ROOT/nrc" ] && \
        [ ! -d "$SRC_ROOT/nrc_modular" ]; then
    die "no driver sources under $SRC_ROOT.
Unpack the whole release package - install.sh builds from the src/ beside it.
To install without building, pass --no-build; the nrc.ko this package ships
must then match kernel $(uname -r) or the radio will not come up."
fi

# Building both driver flavours needs a few hundred MB of scratch; a full
# rootfs fails halfway through the build instead of up front.
if [ "$DO_BUILD" = 1 ]; then
    FREE_MB=$(df -Pm / | awk 'NR==2 {print $4}')
    if [ "${FREE_MB:-0}" -lt 400 ]; then
        die "only ${FREE_MB}MB free on / - the driver builds need ~400MB.
Free some space first, e.g.:
  sudo apt-get clean && sudo journalctl --vacuum-size=50M
  sudo docker builder prune -f     # if docker is installed
or re-run with --no-build if the modules are already in place."
    elif [ "$FREE_MB" -lt 800 ]; then
        warn "only ${FREE_MB}MB free on / - tight but continuing"
    fi
fi

if [ "$DO_BUILD" = 1 ]; then
    detect_kdir || die "kernel headers for $(uname -r) not found.
Install them for your board's kernel (or pass --kdir /path/to/kernel/headers),
or re-run with --no-build after placing prebuilt .ko files in
nrc_pkg/sw/driver/nrc/."
    log "kernel build tree: $KDIR"
fi

# ---- 2. copy nrc_pkg to the user's home -------------------------------------
if [ -d "$USER_HOME/nrc_pkg" ]; then
    BACKUP="$USER_HOME/nrc_pkg_backup_$(date +%Y%m%d_%H%M%S)"
    log "backing up existing $USER_HOME/nrc_pkg -> $BACKUP"
    mv "$USER_HOME/nrc_pkg" "$BACKUP"
fi
log "installing package to $USER_HOME/nrc_pkg"
cp -r "$PKG_ROOT/nrc_pkg" "$USER_HOME/nrc_pkg"
[ -f "$PKG_ROOT/VERSION-SDK" ] && cp "$PKG_ROOT/VERSION-SDK" "$USER_HOME/nrc_pkg/"
chown -R "$TARGET_USER:" "$USER_HOME/nrc_pkg"
chmod -R u+rwX,go+rX "$USER_HOME/nrc_pkg"
find "$USER_HOME/nrc_pkg" \( -name '*.sh' -o -name '*.py' \) -exec chmod +x {} +
# the host binaries carry no extension, so the sweep above walks past them. A
# chmgrd without +x costs every channel switch, and start.py only checks that
# the file is there - the package came up looking healthy with no daemon.
for b in cli_app chmgrd; do
    [ -f "$USER_HOME/nrc_pkg/script/$b" ] && chmod 755 "$USER_HOME/nrc_pkg/script/$b"
done

# ---- 3. build the driver(s) + cli_app ---------------------------------------
DRV_DST="$USER_HOME/nrc_pkg/sw/driver"
MODULAR_OK=0
MONO_OK=0

build_all() {
    local jobs; jobs=$(nproc 2>/dev/null || echo 2)

    # Build from inside the source directory (subshell), never `make -C`: the
    # driver Makefiles derive include and Module.symvers paths from $(PWD),
    # which with -C still points at the caller's directory.
    build_in() {   # <dir> <make args...>
        local d=$1; shift
        ( cd "$d" && make "$@" )
    }

    if [ -d "$SRC_ROOT/cli_app" ]; then
        log "building cli_app"
        build_in "$SRC_ROOT/cli_app" clean >/dev/null 2>&1 || true
        build_in "$SRC_ROOT/cli_app" -j"$jobs"
        install -m 0755 "$SRC_ROOT/cli_app/cli_app" "$USER_HOME/nrc_pkg/script/cli_app"
    else
        warn "src/cli_app missing - skipping cli_app build"
    fi

    # Channel manager daemon. It lives beside cli_app in the script directory
    # because that is where start.py looks for it, and start.py only launches
    # it when the file is there - so a failed build costs the channel manager
    # and nothing else.
    if [ -d "$SRC_ROOT/chmgrd" ]; then
        log "building chmgrd (channel manager)"
        build_in "$SRC_ROOT/chmgrd" clean >/dev/null 2>&1 || true
        if build_in "$SRC_ROOT/chmgrd" -j"$jobs"; then
            install -m 0755 "$SRC_ROOT/chmgrd/chmgrd" \
                "$USER_HOME/nrc_pkg/script/chmgrd"
        else
            warn "chmgrd build FAILED - automatic channel selection unavailable"
        fi
    fi

    # The modular stack serves unified-ap only; every other mode runs on the
    # monolithic driver (see systemd/bin/start-wlan.sh). Build it where the
    # package carries it - a package without --mcp does not.
    if [ -d "$SRC_ROOT/nrc_modular" ]; then
        log "building modular driver stack (nrc_spi/nrc_core/nrc_wlan/nrc-mcp)"
        build_in "$SRC_ROOT/nrc_modular" KDIR="$KDIR" clean >/dev/null 2>&1 || true
        if build_in "$SRC_ROOT/nrc_modular" KDIR="$KDIR" -j"$jobs"; then
            install -d "$DRV_DST/nrc_modular"
            install -m 0644 \
                "$SRC_ROOT/nrc_modular/backend/nrc_spi/nrc_spi.ko" \
                "$SRC_ROOT/nrc_modular/hal/nrc_core/nrc_core.ko" \
                "$SRC_ROOT/nrc_modular/frontend/nrc_wlan/nrc_wlan.ko" \
                "$SRC_ROOT/nrc_modular/frontend/nrc_mcp/nrc-mcp.ko" \
                "$DRV_DST/nrc_modular/"
            MODULAR_OK=1
        else
            warn "modular driver build FAILED - unified-ap mode unavailable"
        fi
    fi

    if [ -d "$SRC_ROOT/nrc" ]; then
        log "building monolithic driver (nrc.ko)"
        build_in "$SRC_ROOT/nrc" KDIR="$KDIR" clean >/dev/null 2>&1 || true
        if build_in "$SRC_ROOT/nrc" KDIR="$KDIR" -j"$jobs"; then
            install -d "$DRV_DST/nrc"
            install -m 0644 "$SRC_ROOT/nrc/nrc.ko" "$DRV_DST/nrc/"
            MONO_OK=1
        else
            warn "monolithic driver build FAILED - plain 'ap' mode unavailable."
            warn "This driver flavour trails the mac80211 API; on recent kernels"
            warn "only the modular stack (unified-ap and the other modes) builds."
        fi
    fi

    [ "$MODULAR_OK" = 1 ] || [ "$MONO_OK" = 1 ] || \
        die "no driver could be built for kernel $(uname -r) - see the build output above"
    chown -R "$TARGET_USER:" "$DRV_DST" "$USER_HOME/nrc_pkg/script"

}
if [ "$DO_BUILD" = 1 ]; then
    build_all
else
    log "--no-build: using the nrc.ko and cli_app the package ships"
    # they were built elsewhere, so say so before the radio fails to come up
    ko="$USER_HOME/nrc_pkg/sw/driver/nrc.ko"
    if [ -f "$ko" ] && command -v modinfo >/dev/null 2>&1; then
        vm=$(modinfo -F vermagic "$ko" 2>/dev/null | cut -d" " -f1)
        [ -n "$vm" ] && [ "$vm" != "$(uname -r)" ] && \
            warn "that nrc.ko is for kernel $vm, this board runs $(uname -r) - it will not load"
    fi
fi

# ---- 4. device tree overlay ---------------------------------------------------
# One overlay serves both driver flavours. They describe the same SPI device on
# the same active-high level IRQ, and the monolithic driver ignores the extra
# reset-gpios the modular stack uses, so there is nothing to switch between.
# select-dtoverlay.sh (installed to /usr/lib/halow) keeps config.txt pointed at
# it and is also re-run by the Web GUI.
install_dts_raspi() {
    local overlay_dir config_txt out
    command -v dtc >/dev/null 2>&1 || { warn "dtc not installed - skipping overlays"; return 0; }

    if [ -d /boot/firmware/overlays ]; then
        overlay_dir=/boot/firmware/overlays
        config_txt=/boot/firmware/config.txt
    else
        overlay_dir=/boot/overlays
        config_txt=/boot/config.txt
    fi
    [ -f "$config_txt" ] || { warn "$config_txt not found - skipping overlays"; return 0; }

    # Prints "changed" on stdout when the installed .dtbo was replaced (all
    # human-readable output goes to stderr so the caller can capture it); the
    # caller turns that into the reboot decision, since a .dtbo is only read
    # at boot.
    compile_overlay() {   # <dts> <name>
        [ -f "$1" ] || return 0
        out=/tmp/$2.dtbo
        dtc -@ -I dts -O dtb -o "$out" "$1" 2>/dev/null || {
            warn "failed to compile $1"; return 0; }
        if ! cmp -s "$out" "$overlay_dir/$2.dtbo" 2>/dev/null; then
            cp "$out" "$overlay_dir/$2.dtbo"
            echo changed
        fi
    }
    # Either source tree can supply the overlay - it is the same SPI node - so
    # take the modular tree's copy where the package has one and the
    # monolithic driver's otherwise. A package built from a tree with no
    # modular stack has only the latter.
    local nrc_dts=$SRC_ROOT/nrc_modular/backend/dts/nrc-rpi.dts
    [ -f "$nrc_dts" ] || nrc_dts=$SRC_ROOT/nrc/nrc-rpi.dts
    if [ ! -f "$nrc_dts" ]; then
        warn "no nrc-rpi.dts in this package - skipping overlay installation"
        return 0
    fi
    if [ "$(compile_overlay "$nrc_dts" nrc-rpi)" = changed ]; then
        DTBO_CHANGED=1
        log "installed $overlay_dir/nrc-rpi.dtbo (from ${nrc_dts#"$PKG_ROOT/"})"
    fi
    # earlier packages shipped a second, monolithic-only overlay; remove it so
    # an upgrade cannot leave the board booting a stale one
    if [ -f "$overlay_dir/nrc-rpi-mono.dtbo" ]; then
        rm -f "$overlay_dir/nrc-rpi-mono.dtbo"
        sed -i '/^dtoverlay=nrc-rpi-mono$/d' "$config_txt"
        DTBO_CHANGED=1
        log "removed the obsolete nrc-rpi-mono overlay"
    fi

    # A board that previously ran the stock NRC EVK setup still loads its
    # overlay (dtoverlay=newracom / newracom_pd). Stacked on ours it pins
    # GPIO 5,7-11 to the SPI controller, so nrc-cspi@0 can no longer claim
    # its IRQ pin and the driver probe fails with
    #   "pin gpio5 already requested by fe204000.spi" / probe error -22.
    # Ours fully replaces it (same SPI device, spidev disabled), so drop the
    # stock lines. Their .dtbo files are not ours to delete - keep them.
    if grep -q '^dtoverlay=newracom' "$config_txt"; then
        sed -i '/^dtoverlay=newracom/d' "$config_txt"
        REBOOT_NEEDED=1
        log "removed the stock EVK overlay line(s) (dtoverlay=newracom*):"
        log "they conflict with nrc-rpi and break the driver probe"
    fi

    if [ "$DISABLE_ONBOARD_WIFI" = 1 ]; then
        add_config_line() {
            grep -q "^$1\$" "$config_txt" || { echo "$1" >> "$config_txt"; REBOOT_NEEDED=1; }
        }
        add_config_line "dtoverlay=disable-wifi"
        add_config_line "dtoverlay=disable-bt"
        if [ ! -f /etc/modprobe.d/raspi-blacklist.conf ] || \
           ! grep -q brcmfmac /etc/modprobe.d/raspi-blacklist.conf; then
            {
                echo "blacklist brcmfmac"
                echo "blacklist brcmutil"
            } >> /etc/modprobe.d/raspi-blacklist.conf
            REBOOT_NEEDED=1
        fi
    fi
    return 0
}
if [ "$DO_DTS" = 1 ]; then
    case "$PLATFORM" in
        raspi) install_dts_raspi ;;
        *)
            warn "no automatic device-tree handling for this platform."
            warn "make sure the board DTB has the SPI slave + IRQ GPIO for the NRC chip"
            warn "(reference node: src/nrc/nrc-rpi.dts)."
            ;;
    esac
else
    log "--no-dts: skipping device-tree overlays"
fi

# ---- 5. systemd service stack -------------------------------------------------
log "installing systemd services"
sh "$USER_HOME/nrc_pkg/systemd/install-halow-services.sh" \
    "$USER_HOME/nrc_pkg/script"

# keep the uninstaller reachable without the original package tarball
[ -f "$PKG_ROOT/uninstall.sh" ] && \
    install -m 0755 "$PKG_ROOT/uninstall.sh" /usr/lib/halow/uninstall.sh

CONF=/etc/halow/halow.conf
# The option catalog ships with the product (the operator's values live in
# config.json), so refresh it - install-halow-services.sh deliberately keeps
# an existing one, which would hide options added by this release.
SCHEMA_SRC="$USER_HOME/nrc_pkg/web/schema.json"
if [ -f "$SCHEMA_SRC" ] && [ -f /etc/halow/schema.json ] && \
        ! cmp -s "$SCHEMA_SRC" /etc/halow/schema.json; then
    cp /etc/halow/schema.json /etc/halow/schema.json.bak
    install -m 0644 "$SCHEMA_SRC" /etc/halow/schema.json
    log "refreshed /etc/halow/schema.json (previous kept as schema.json.bak)"
    # The refresh puts the shipped catalog back, including modes this package
    # cannot run, so re-apply the trim install-halow-services.sh did before it.
    "$USER_HOME/nrc_pkg/systemd/install-halow-services.sh" --schema-only \
        2>/dev/null || true
fi

set_conf() {
    if grep -q "^$1=" "$CONF"; then
        sed -i "s|^$1=.*|$1=$2|" "$CONF"
    else
        echo "$1=$2" >> "$CONF"
    fi
}
# only path-dependent keys: everything the operator configures lives in the GUI
set_conf NRC_SCRIPT_DIR "$USER_HOME/nrc_pkg/script"
# Build type gates the Web GUI's cli_app write commands. The marker ships only
# in a package built with make_pkg.sh --debug; anything else is release.
if [ -f "$USER_HOME/nrc_pkg/BUILD_TYPE" ]; then
    set_conf BUILD_TYPE "$(tr -d '[:space:]' < "$USER_HOME/nrc_pkg/BUILD_TYPE")"
    warn "DEBUG package: the Web GUI can run cli_app write commands"
else
    set_conf BUILD_TYPE release
fi

# make sure config.txt carries the overlay, then decide about the reboot: a new
# or changed .dtbo, like a new config.txt line, is only read at boot
if [ "$DO_DTS" = 1 ] && [ "$PLATFORM" = raspi ] && \
   [ -x /usr/lib/halow/select-dtoverlay.sh ]; then
    [ "$(/usr/lib/halow/select-dtoverlay.sh || true)" = "reboot-required" ] && REBOOT_NEEDED=1
    [ "$DTBO_CHANGED" = 1 ] && REBOOT_NEEDED=1
fi
[ "$REBOOT_NEEDED" = 1 ] && log "boot configuration changed - a reboot is required"

# ---- 6. enable / start ----------------------------------------------------------
# Enable the members but NOT halow.target: a member's WantedBy=halow.target
# symlink is created when that member is enabled, so the target needs them
# enabled to have anything to pull in - but whether the target itself comes up
# at power-on is the operator's choice, and it ships off. halow-webd is
# WantedBy=multi-user.target, so the Web GUI is up either way and can turn the
# radio on (Settings -> Start radio at boot), which enables the target.
UNITS_ENABLE="halow-wlan.service halow-hostapd.service halow-webd.service"
# Start only the Web GUI, so the box matches the setting it ships with.
UNITS="halow-webd.service"
# The firmware log follower is cheap to leave running (the firmware defaults
# to flight-recorder mode and streams nothing until asked) and has to be up
# in advance to catch an assert dump, which arrives unsolicited. It exists
# only where the tooling supports it - install-halow-services.sh probes
# cli_app and installs the unit accordingly, so enable what it installed
# rather than asking for a unit that is deliberately absent.
mkdir -p /var/log/halow
if [ -f /etc/systemd/system/halow-fwlog.service ]; then
    UNITS_ENABLE="$UNITS_ENABLE halow-fwlog.service"
fi
if [ "$DO_ENABLE" = 1 ]; then
    log "enabling at boot: $UNITS_ENABLE"
    # shellcheck disable=SC2086
    systemctl enable $UNITS_ENABLE >/dev/null
fi
if [ "$DO_START" = 1 ]; then
    if [ "$REBOOT_NEEDED" = 1 ]; then
        warn "reboot required for the new boot configuration - not starting now"
        warn "the services start automatically after the reboot"
    else
        # restart, not start: on an upgrade the units are already active and
        # `start` would be a no-op, leaving the previously installed package
        # (old modules, old NRC_SCRIPT_DIR, old Web GUI) running. restart also
        # starts units that were inactive, so it covers the fresh case too.
        if [ "$STACK_WAS_ACTIVE" = 1 ]; then
            log "restarting: $UNITS (loading the newly installed package)"
        else
            log "starting: $UNITS"
        fi
        # halow.target is deliberately not among them: installing must not put
        # the radio on air by itself.
        if [ "$DO_ENABLE" = 0 ]; then
            warn "--no-enable was given: halow.target has no member links, so it"
            warn "starts nothing. Enable the units, or start them individually."
        fi
        log "radio is off: set 'Start radio at boot' in the Web GUI, or run"
        log "  sudo systemctl start halow.target"

        # shellcheck disable=SC2086
        systemctl restart $UNITS
    fi
fi

# ---- summary --------------------------------------------------------------------
# The address the operator can actually browse to is the one on the interface
# holding the default route - `hostname -I` merely lists addresses in
# enumeration order, which put a camera link (eth0) and docker0 (172.17.0.1)
# ahead of the management network on real boards. Ask the kernel which source
# address it would use to reach the outside instead; fall back to the first
# listed address only when the board has no default route at all.
IP_HINT=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
[ -n "$IP_HINT" ] || IP_HINT=$(hostname -I 2>/dev/null | awk '{print $1}')
DRV_NOTE="modular + monolithic"
[ "$MODULAR_OK" = 1 ] && [ "$MONO_OK" = 0 ] && DRV_NOTE="modular only (plain AP unavailable)"
[ "$MODULAR_OK" = 0 ] && [ "$MONO_OK" = 1 ] && DRV_NOTE="monolithic only (Unified AP unavailable)"
[ "$DO_BUILD" = 0 ] && DRV_NOTE="not rebuilt (--no-build)"
echo
log "installation complete."
echo "  package   : $USER_HOME/nrc_pkg"
echo "  drivers   : $DRV_NOTE"
echo "  services  : $UNITS"
echo "  web GUI   : https://${IP_HINT:-<board-ip>}:8443"
echo
echo "  Set the operating mode, country, channel and security in the Web GUI"
echo "  (first login password: 'newracom', no user name - set your own)."
if [ "$REBOOT_NEEDED" = 1 ]; then
    echo
    echo "  >>> REBOOT REQUIRED (boot configuration changed) - run: sudo reboot <<<"
elif [ "$DO_START" = 0 ]; then
    echo
    echo "  start now with: sudo systemctl start $UNITS"
fi
exit 0
