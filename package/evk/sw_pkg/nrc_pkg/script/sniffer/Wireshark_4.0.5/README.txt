Date: 4/25/2023
Author: Aaron J. Lee (jehun.lee@newracom.com)

Wireshark code: wireshark-4.0.5.tar.xz (https://2.na.dl.wireshark.org/src/wireshark-4.0.5.tar.xz)

Platform: Raspberry Pi 4
OS: Raspbian 10 Buster (Linux Kernel Version: 5.10.17)

1. Install libraries by using the wireshark 2.6.20 package
sudo apt update
sudo apt install wireshark lxqt-sudo omniidl libpcap0.8-dev python3-ply libsnacc0c2 snacc libminizip1 -y

2. Display Options -> Resolution -> Choose higher resolution for local start option 4.1 below
sudo raspi-config

3. Install Wireshark-4.0.5
cd deb
sudo dpkg -i lib*.deb
sudo dpkg -i wireshark*.deb
sudo dpkg -i tshark*.deb

4. Start sniffer

4.1 Local start (via VNC viewer)
cd ~/nrc_pkg/script
./start.py 2 0 US 165 0

4.2 Remote start (via MobaXterm SSH session)
DISPLAY="<PC IP address>:10.0"
./start.py 2 0 US 165 1
