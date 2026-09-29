Wireshark 4.0.5 for Raspberry Pi (armhf)
========================================

These packages are provided as a convenience for capturing 802.11ah (HaLow)
traffic on the evaluation platform.

Build information
-----------------

Source:    wireshark-4.0.5.tar.xz
           https://2.na.dl.wireshark.org/src/wireshark-4.0.5.tar.xz
Platform:  Raspberry Pi 4
OS:        Raspbian 10 Buster (Linux kernel 5.10.17)
Built by:  Newracom, Inc.


License and corresponding source
--------------------------------

Wireshark is licensed under the GNU General Public License, version 2 or
later (GPL-2.0-or-later). The full licence text is in the LICENSES/ directory
at the root of this package.

Newracom made no changes to the Wireshark source. These packages were built
from the unmodified upstream release tarball on the platform described above.

The build configuration used to produce these packages is in BUILD.txt beside
this file. The unmodified upstream source tarball is published on the same
release page as this software package:

    https://github.com/newracom/nrc7394_sw_pkg/releases

    wireshark-4.0.5.tar.xz

Together, BUILD.txt and that tarball are the complete corresponding source.

If you redistribute a product that includes these binaries, the GPL obligations
apply to your distribution as well. In that case you must make the
corresponding source available to your recipients, for example by passing on
the link above.


Scope
-----

These packages are an evaluation convenience for the NRC7394 EVK. They are not
a component of the NRC7394 product, are not loaded on the device, and are not
security-maintained by Newracom. The version bundled here is fixed for this
release.

Known vulnerabilities in this version, and our assessment of them, are
published in the Software Bill of Materials at sbom/ in this package. If your
deployment policy does not permit them, install Wireshark from your
distribution's package repository and remove these packages; the capture
workflow below is unchanged.


Installation
------------

1. Install the prerequisite libraries from the distribution

   The distribution's own wireshark package is installed first: it pulls in
   the shared libraries that the 4.0.5 packages below depend on. The 4.0.5
   packages are then installed over it with dpkg.

   sudo apt update
   sudo apt install wireshark lxqt-sudo omniidl libpcap0.8-dev python3-ply \
                    libsnacc0c2 snacc libminizip1 -y

2. If you will use the GUI over VNC, raise the display resolution

   sudo raspi-config
   # Display Options -> Resolution

3. Install the packages

   cd deb
   sudo dpkg -i lib*.deb
   sudo dpkg -i wireshark*.deb
   sudo dpkg -i tshark*.deb


Starting the sniffer
--------------------

Local, via VNC viewer:

    cd ~/nrc_pkg/script
    ./start.py 2 0 US 165 0

Remote, via SSH with X forwarding:

    DISPLAY="<PC IP address>:10.0"
    ./start.py 2 0 US 165 1
