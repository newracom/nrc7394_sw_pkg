There are two ways to run this package. Pick one - they both own the driver
and hostapd, so they must not run at the same time.


1. Scripts (default)

   Copy nrc_pkg/ to the board's home directory, then:

     cd ~/nrc_pkg
     chmod -R 755 *
     cd script
     sudo ./start.py 1 3 US 36        # AP, WPA3-SAE, US, channel 36
     sudo ./stop.py

   Run start.py with no argument for the full usage. See the README under
   ~/nrc_pkg for the aliases and the sniffer commands.

   To refresh an installed package with a newer nrc_pkg/:

     ./update.sh


2. install.sh with the Web UI (experimental)

   Installs the driver, the systemd services and the Web UI, and starts them
   at boot:

     sudo ./install.sh              # a board with internet access
     sudo ./install.sh --no-deps    # skip the apt step

   It builds the driver and cli_app for the board's own kernel, so the package
   has to reach the board whole: the sources live in its top-level src/, two
   levels above this directory.

   To install from this sw_pkg directory alone, add --no-build:

     sudo ./install.sh --no-deps --no-build

   It then uses the nrc.ko and cli_app already under nrc_pkg/, which are
   built for one kernel - uname -r has to match, and the device-tree overlay
   has to be in place from an earlier install.

   Then open https://<board-ip>:8443 - the first login password is
   newracom (there is no user name) and it asks you to set your own. Set
   the operating mode, country, channel and security there.

   The Web UI runs from /usr/lib/halow/web/, not from ~/nrc_pkg/web/.

   Service state:

     systemctl is-active halow.target halow-wlan halow-hostapd halow-webd
     journalctl -u halow-webd -n 50

   Remove it:

     sudo /usr/lib/halow/uninstall.sh            # keeps the configuration
     sudo /usr/lib/halow/uninstall.sh --purge    # removes it too

   The settings live in /etc/halow - halow.conf, config.json (it holds the
   admin password), schema.json and the TLS material. Installing keeps what
   is already there, so start from the defaults by removing that directory
   first:

     sudo rm -rf /etc/halow

   --purge does the same and also removes ~/nrc_pkg, the firmware copies in
   /lib/firmware (uni_s1g*.bin, nrc*_bd.dat) and the logs under /var/log/halow
   and /var/log/halow-loghub. It never touches ~/nrc_pkg_backup_*.

   The dtoverlay line in config.txt survives either way; add --dts to remove
   it, which needs a reboot.

   start.py and stop.py detect this stack and stop halow.target first. Run
   'sudo systemctl start halow.target' to hand control back to the Web UI.
