#!/usr/bin/python

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

import sys
import os
import time
import commands
 
def change_channel():
	cmdstr = "sudo iw dev wlan0 set channel " + str(sys.argv[1])
	os.system(cmdstr)

total = len(sys.argv)
cmdargs = str(sys.argv)

if total < 2:
	print ("Usage: change_channel.py channelnumber")
else:
	print ("channel number: %s " % str(sys.argv[1]))
	change_channel()