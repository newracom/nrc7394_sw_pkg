#!/bin/bash

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# Paths are relative to the package this script lives in: it installs
# into whichever user's home ran install.sh, not always /home/pi.
NRC_PKG_DIR=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)
sudo killall -9 hostapd
sudo rmmod nrc
sleep 2
sudo cp $NRC_PKG_DIR/sw/firmware/nrc7292_cspi.bin /lib/firmware/uni_s1g.bin
sudo insmod $NRC_PKG_DIR/sw/driver/nrc.ko power_save=0 fw_name=uni_s1g.bin
sleep 5
sudo ifconfig wlan0 0.0.0.0
sudo ifconfig eth0 0.0.0.0
sleep 5
$NRC_PKG_DIR/script/cli_app set txpwr 17
sudo hostapd ap_sdk_map.conf -ddddd | tee hostap.log
