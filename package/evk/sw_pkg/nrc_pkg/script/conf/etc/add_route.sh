#!/bin/bash

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

sudo route add -net 192.168.150.0 netmask 255.255.255.0 gw 192.168.200.$1
route

echo "Done"
