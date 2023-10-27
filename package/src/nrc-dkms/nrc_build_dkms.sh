#!/bin/bash

# Copyright (c) 2023, Teledatics Incorporated
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Contact: admin@teledatics.com

# Script to download Newrcom NRC7394 driver and build a kernel 
# module from source code and install it in the running system.

DKMS_REPO_URL="https://github.com/newracom/nrc7394_sw_pkg"
MODULE_NAME="nrc"
TEMP_DIR=$(mktemp -d)

# Clone the repo
git clone --depth 1 $DKMS_REPO_URL $TEMP_DIR

# Move into the cloned directory
cd $TEMP_DIR

# Assign version
MODULE_MAJOR=$(grep VERSION_MAJOR $TEMP_DIR/package/VERSION-SDK | sed s/VERSION_MAJOR=*.//)
MODULE_MINOR=$(grep VERSION_MINOR $TEMP_DIR/package/VERSION-SDK | sed s/VERSION_MINOR=*.//)
MODULE_VERSION="$MODULE_MAJOR-$MODULE_MINOR"

# Create a dkms.conf file
cat << EOF > dkms.conf
MAKE[0]="make -C package/src/nrc/ KERNEL_DIR=/usr/src/linux-headers-$(uname -r) modules"
CLEAN="make -C package/src/nrc/ KERNEL_DIR=/usr/src/linux-headers-$(uname -r) clean"
PACKAGE_NAME="$MODULE_NAME"
PACKAGE_VERSION="$MODULE_VERSION"
BUILT_MODULE_NAME[0]="$MODULE_NAME"
BUILT_MODULE_LOCATION[0]=package/src/nrc/
DEST_MODULE_LOCATION[0]="/updates/dkms"
REMAKE_INITRD="yes"
AUTOINSTALL="yes"
EOF

# Create the Debian packages (both source and binary)
dkms remove $MODULE_NAME/$MODULE_VERSION --all 2>/dev/null
dkms add .
dkms build -m $MODULE_NAME -v $MODULE_VERSION
dkms mkdsc -m $MODULE_NAME -v $MODULE_VERSION --source-only
dkms mkdsc -m $MODULE_NAME -v $MODULE_VERSION --binaries-only

# Clean up
rm -rf $TEMP_DIR
