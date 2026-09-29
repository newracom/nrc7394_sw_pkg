#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# Make sure the Raspberry Pi boot config carries our device-tree overlay.
#
# One overlay (nrc-rpi.dtbo) serves both driver flavours: the modular stack
# (Unified AP) and the monolithic driver claim the same SPI device on the same
# active-high level IRQ, and the monolithic driver simply does not look at the
# reset-gpios the modular stack uses. Switching flavours therefore changes
# nothing about the boot configuration - it is a module reload.
#
# usage: select-dtoverlay.sh    (an argument is accepted and ignored, so older
#                                callers passing a flavour keep working)
#
# Prints "reboot-required" and exits 0 when it changed something, "unchanged"
# when it did not. Silently succeeds on non-Pi boards (their SPI/IRQ nodes
# come from the board DTB, not from an overlay).
set -e

WANT=nrc-rpi

if [ -d /boot/firmware/overlays ]; then
    OVERLAY_DIR=/boot/firmware/overlays
    CONFIG_TXT=/boot/firmware/config.txt
elif [ -d /boot/overlays ]; then
    OVERLAY_DIR=/boot/overlays
    CONFIG_TXT=/boot/config.txt
else
    echo "unchanged"   # not a Raspberry Pi boot layout
    exit 0
fi
[ -f "$CONFIG_TXT" ] || { echo "unchanged"; exit 0; }

if [ ! -f "$OVERLAY_DIR/$WANT.dtbo" ]; then
    echo "missing $OVERLAY_DIR/$WANT.dtbo - re-run install.sh" >&2
    exit 1
fi

# already correct, and no leftover from the days of a second overlay
if grep -q "^dtoverlay=$WANT\$" "$CONFIG_TXT" && \
   ! grep -q '^dtoverlay=nrc-rpi-mono$' "$CONFIG_TXT"; then
    echo "unchanged"
    exit 0
fi

# drop any of our overlay lines, then append the wanted one
sed -i '/^dtoverlay=nrc-rpi\(-mono\)\?$/d' "$CONFIG_TXT"
echo "dtoverlay=$WANT" >> "$CONFIG_TXT"
# marker for the Web GUI banner; /run means it clears itself on the reboot
mkdir -p /run/halow 2>/dev/null
echo "boot overlay set to $WANT" > /run/halow/reboot-required 2>/dev/null
echo "reboot-required"
exit 0
