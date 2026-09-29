#!/usr/bin/env python

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# -*- coding: utf-8 -*-

import sys, os, time, subprocess, re
import threading
import mesh
from mesh import *
import nrc_net
import halow_config

# ============================================================================
# Default Configuration - edit the values below. The Web GUI writes the same
# variables when it saves, and a run records what it used back into its
# configuration, so both always show the same settings (halow_config.py).
# ============================================================================
##################################################################################
# Raspbery Pi Conf.
max_cpuclock      = 1        # Set Max CPU Clock : 0(off) or 1(on)
##################################################################################
# Firmware Conf.
model             = 7394     # 7292/7394
fw_download       = 1        # 0(FW Download off) or 1(FW Download on)
fw_name           = 'uni_s1g.bin'
auto_fw_update    = 0
fw_update_name    = 'uni_s1g_update.bin'
##################################################################################
# DEBUG Conf.
# NRC Driver log
driver_debug      = 0        # NRC Driver debug option : 0(off) or 1(on)
dbg_flow_control  = 0        # Print TRX slot and credit status in real-time
#--------------------------------------------------------------------------------#
# WPA Supplicant Log (STA Only)
supplicant_debug  = 0        # WPA Supplicant debug option : 0(off) or 1(on)
#--------------------------------------------------------------------------------#
# HOSTAPD Log (AP Only)
hostapd_debug     = 0        # Hostapd debug option    : 0(off) or 1(on)
#################################################################################
# CSPI Conf. (Default)
spi_clock    = 20000000      # SPI Master Clock Frequency
spi_bus_num  = 0             # SPI Master Bus Number
spi_cs_num   = 0             # SPI Master Chipselect Number
spi_gpio_irq = 5             # NRC-CSPI EIRQ GPIO Number
                             # BBB is 60 recommanded.
spi_polling_interval = 0     # NRC-CSPI Polling Interval (msec)

#
# NOTE:
#  - NRC-CSPI EIRQ Input Interrupt: spi_gpio_irq >= 0 and spi_polling_interval <= 0
#  - NRC-CSPI EIRQ Input Polling  : spi_gpio_irq >= 0 and spi_polling_interval > 0
#  - NRC-CSPI Registers Polling   : spi_gpio_irq < 0 and spi_polling_interval > 0
#
#--------------------------------------------------------------------------------#
# ft232h usb-spi conf. (ft232h cspi conf)
ft232h_usb_spi = 0           # ftdi ft232h usb-spi bridge
                             # 0 : unused
                             # 1 : nrc-cspi_eirq input polling
                             # 2 : nrc-cspi registers polling
# ch347 usb-spi conf. (ch347 cspi conf)
ch347_usb_spi = 0            # wch ch347 usb-spi bridge
                             # 0 : unused
                             # 1 : nrc-cspi registers polling
#################################################################################
# RF Conf.
max_txpwr         = -1       # -1 (board data decides) or dBm to cap it
#--------------------------------------------------------------------------------#
# TX Power of the RELAY's STA side
max_txpwr_sta     = -1       # RELAY only: -1 (board data decides) or dBm
bd_name           = ''       # board data name (bd defines max TX Power per CH/MCS/CC)
                             # specify your bd name here. If not, follow naming rules in strBDName()
#--------------------------------------------------------------------------------#
# TX Power Control (AP:Feedback only   STA:Control TX POWR according to Feedback from AP)
tpc = 0                      # 0 (disable) or 1 (enable)
#--------------------------------------------------------------------------------#
# EU EN 300 220 mode (EU country only)
#  0 (EN 304 220: duty cycle based, default) 1 (EN 300 220: 100ms pause before every TX)
eu_en_300_220     = 0        # 0 (disable) or 1 (enable)
#--------------------------------------------------------------------------------#
# Duty cycle configuration
duty_cycle_enable = 0        # 0 (disable) or 1 (enable)
duty_cycle_window = 0
duty_cycle_duration = 0
#--------------------------------------------------------------------------------#
# Set sub-XTAL bypass
sub_xtal_bypass = 0          # 0 (External XTAL is used ) or 1 (Enable sub-XTAL bypass)
##################################################################################
# PHY Conf.
guard_int         = 'auto'   # Guard Interval ('auto' (adaptive) or 'long'(LGI) or 'short'(SGI))
#--------------------------------------------------------------------------------#
# Supported CH Width (STA Only)
support_ch_width  = 1        # 0 (1/2MHz Support) or 1 (1/2/4MHz Support)
#--------------------------------------------------------------------------------#
# Set Location of 1MHz Primary Channel (BW2M or BW4M AP/SNIFFER Only)
loc_1m_primary_ch = -1       # -1        (default    : BW_2M(0),   BW_4M(1))
                             #  0|1|2|3  (choose one : BW_2M(0|1), BW_4M(0|1|2|3))
#--------------------------------------------------------------------------------#
# 4M op as 2M primary, forbid 1M frames (BW4M AP + its STA, set same on both)
bw_4m_2m_prim_loc = -1       # -1 (off) | 0 (lower 2M) | 1 (upper 2M)
#--------------------------------------------------------------------------------#
# 1M NDP (Block) ACK
#  Enable 1M NDP ACK on 2/4MHz BW instead of 2M NDP ACK
#  Note: if enabled, throughput can be decreased on high MCS
ndp_ack_1m        = 0        # 0 (disable) or 1 (enable)
#--------------------------------------------------------------------------------#
# CCA Threshold
cca_threshold = -75
#--------------------------------------------------------------------------------#
# Use bitmap encoding for NDP (block) ack operation (NRC7292 only)
bitmap_encoding   = 1        # 0 (disable) or 1 (enable)
#--------------------------------------------------------------------------------#
# User scrambler reversely (NRC7292 only)
reverse_scrambler = 1        # 0 (disable) or 1 (enable)
##################################################################################
# Channel Manager Conf. (AP Only) - boot ACS + runtime DCS
#  0 (manual) : use channel= in hostapd conf as-is (engine off)
#  1 (auto)   : FW picks the start channel (boot ACS) and keeps switching
#               it by channel quality in service (DCS)
#  2 (monitor): keep the manual channel; engine measures and logs only
channel_manager           = 0
channel_manager_profile   = 1   # 0: conservative, 1: normal, 2: aggressive
channel_manager_prefer_bw = 1   # candidate bw (explicit): 1: 1M, 2: 2M, 4: 4M
channel_manager_dwell     = 300 # boot-ACS scan dwell per channel (ms, 100..1000; 300 recommended)
channel_manager_exclude   = ''  # comma-separated S1G freq x10 to avoid (e.g. '9025,9035')
##################################################################################
# MAC Conf.
macaddr           = ''       # MAC Address (e.g. '00:11:22:33:44:55')
# S1G Short Beacon (AP & MESH Only)
#  If disabled, AP sends only S1G long beacon every BI
#  Recommend using S1G short beacon for network efficiency (Default: enabled)
short_bcn_enable  = 0        # 0 (disable) or 1 (enable)
#--------------------------------------------------------------------------------#
# Legacy ACK enable (AP & STA)
#  If disabled, AP/STA sends only NDP ack frame
#  Recommend using NDP ack mode  (Default: disable)
legacy_ack_enable  = 0       # 0 (NDP ack mode) or 1 (legacy ack mode)
#--------------------------------------------------------------------------------#
# DAC (Distributed Authentication Control) (AP only)
#  If enabled, AP sends beaon/probe response frame including authentication control IE
auth_control_enable   = 0    # 0 (disable) or 1 (enable)
auth_control_slot     = 100  # slot duration (in TU) (1~127)
auth_control_scale    = 10   # scale (1 or 10)
auth_control_ti_min   = 8    # mininum time interval (in scale * BI) (1~127)
auth_control_ti_max   = 64   # maximum time interval (in scale * BI) (1~255)
#--------------------------------------------------------------------------------#
# Beacon Bypass enable (STA only)
#  If enabled, STA receives beacon frame from other APs even connected
#  Recommend that STA only receives beacon frame from AP connected while connecting  (Default: disable)
beacon_bypass_enable  = 0    # 0 (Receive beacon frame from only AP connected while connecting)
                             # 1 (Receive beacon frame from all APs even while connecting)
#--------------------------------------------------------------------------------#
# Enable sched scan
#  If enabled, STA use scheduled scan
sched_scan_enable = 0            # 0 (disable) 1 (enable)
#--------------------------------------------------------------------------------#
# AMPDU (Aggregated MPDU)
#  Enable AMPDU for full channel utilization and throughput enhancement (Default: auto)
#  disable(0): AMPDU is deactivated
#   manual(1): AMPDU is activated and BA(Block ACK) session is made by manual
#     auto(2): AMPDU is activated and BA(Block ACK) session is made automatically
ampdu_enable      = 2        # 0 (disable) 1 (manual) 2 (auto)
#--------------------------------------------------------------------------------#
# NDP Probe Request
#  For STA, "scan_ssid=1" in wpa_supplicant's conf should be set to use
ndp_preq          = 0        # 0 (Legacy Probe Req) 1 (NDP Probe Req)
#--------------------------------------------------------------------------------#
# CQM (Channel Quality Manager) (STA Only)
#  STA can disconnect according to Channel Quality (Beacon Loss or Poor Signal)
#  Note: if disabled, STA keeps connection regardless of Channel Quality
cqm_enable        = 1        # 0 (disable) or 1 (enable)
#--------------------------------------------------------------------------------#
# RELAY (Do NOT use! it will be deprecated)
relay_type        = 1        # 0 (wlan0: STA, wlan1: AP) 1 (wlan0: AP, wlan1: STA)
relay_nat         = 1        # 0 (not use NAT) 1 (use NAT - no need to add routing table)
#--------------------------------------------------------------------------------#
# Power Save (STA Only)
#  3-types PS: (0)Always on (2)Deep_Sleep(TIM) (3)Deep_Sleep(nonTIM)
#     TIM Mode : check beacons during PS to receive BU from AP
#  nonTIM Mode : Not check beacons during PS (just wake up by TX or EXT INT)
power_save        = 0        # STA (power save type 0~3)
ps_timeout        = '3s'     # STA (timeout before going to sleep) (min:1000ms)
sleep_duration    = '3s'     # STA (sleep duration only for nonTIM deepsleep) (min:1000ms)
listen_interval   = 1000     # STA (listen interval in BI unit) (max:65530)
                             # Listen Interval time should be less than bss_max_idle time to avoid association reject
#--------------------------------------------------------------------------------#
# Idle Mode (STA Only)
#  When The device is running, but idle, The device enters Deep_Sleep(nonTIM)
idle_mode = 0                # 0 (disable) or 1 (enable)
#--------------------------------------------------------------------------------#
# BSS MAX IDLE PERIOD (aka. keep alive) (AP Only)
#  STA should follow (i.e STA should send any frame before period),if enabled on AP
#  Period is in unit of 1000TU(1024ms, 1TU=1024us)
#  Note: if disabled, AP removes STAs' info only with explicit disconnection like deauth
bss_max_idle_enable = 1      # 0 (disable) or 1 (enable)
bss_max_idle        = 1800   # time interval (e.g. 1800: 1843.2 sec) (1 ~ 163,830,000)
#--------------------------------------------------------------------------------#
#  SW encryption/decryption (default HW offload)
sw_enc              = 0      # 0 (HW), 1 (SW), 2 (HYBRID: SW GTK HW PTK)
#--------------------------------------------------------------------------------#
# Mesh Options (Mesh Only)
#  Manual Peering & Static IP
peer                = 0      # 0 (disable) or Peer MAC Address
static_ip           = 0      # 0 (disable) or Static IP Address
batman              = 0      # 0 (disable) or 'bat0' (B.A.T.M.A.N routing protocol)
#--------------------------------------------------------------------------------#
# Filter tx deauth frame for Multi Connection Test (STA Only) (Test only)
discard_deauth    = 0        # 1: discard TX deauth frame on STA
#--------------------------------------------------------------------------------#
# Use Power save pretend operation for no response STA
power_save_pretend  = 0      # 0 (disable) or 1 (enable)
#--------------------------------------------------------------------------------#
# TWT Wake Interval, Service Number, Service Period
# Two out of three values should be set, and the remaining value will be automatically calculated.
twt_int = 0                  # 0 (disable) or Wake Interval (usec)
twt_num = 0                  # 0 (disable) or Service Number
twt_sp = 0                   # 0 (disable) or Service Period (usec)
twt_force_sleep = 0          # force sleep at the end of service
twt_num_in_group = 1         # Max STA Number in one Slot (default 1)
twt_algo = 0                 # 0 (Balanced) or 1 (FCFS)
#--------------------------------------------------------------------------------#
# Use EEPROM for sysconfig & RFCAL
use_eeprom_config  = 0       # 0 (Flash Memory) or 1 (EEPROM)
##################################################################################
# IP Conf.
# HaLow-side addressing (AP, relay AP leg, mesh portal, bridge DHCP server)
#  '' = mode default: 192.168.200.1 (AP, mesh), 192.168.150.1 (relay AP leg)
ap_ip             = ''       # e.g. '192.168.210.1' or '192.168.210.1/24'
dhcp_start        = ''       # '' = .10 of the ap_ip subnet
dhcp_end          = ''       # '' = .50 of the ap_ip subnet
#--------------------------------------------------------------------------------#
# Client-side addressing (STA, relay STA leg, br0 in bridge static mode)
sta_static_ip     = ''       # '' = DHCP client, or e.g. '192.168.200.11/24'
sta_gateway       = ''       # default gateway for sta_static_ip
#--------------------------------------------------------------------------------#
# Wired uplink (eth). 'dhcp' leaves the port to the OS.
eth_iface         = 'eth0'   # which wired port the bridge/NAT use (e.g. 'eth1')
eth_ip_mode       = 'dhcp'   # 'dhcp' | 'static' | 'dhcp-server' (pool .10-.50 of eth_static_ip)
eth_static_ip     = ''       # CIDR, e.g. '192.168.100.1/24'
eth_gateway       = ''
eth_dns           = ''       # e.g. '8.8.8.8 1.1.1.1'
#--------------------------------------------------------------------------------#
# Use bridge setup in br0 interface
use_bridge_setup  = 0        # AP & STA : 0 (not use bridge setup) or n (use bridge setup with eth(n-1))
                             # RELAY : 0 (not use bridge setup) or 1 (use bridge setup with wlan0,wlan1)
bridge_ip_mode = 1           # 0 (static ip) 1 (dhcp client) 2 (dhcp server)
#--------------------------------------------------------------------------------#
# How long the STA bring-up waits for an IP address before returning.
# 0 = wait forever, which is what a person running this script by hand wants
# to see. Under systemd it is the wrong contract: association is a runtime
# state that wpa_supplicant keeps retrying on its own, so a station started
# out of range would sit in "activating" until the unit's start timeout
# killed it - taking halow.target and the Web GUI's apply lock with it.
ip_wait = 0                  # seconds to wait for an IP (0 = forever)
##################################################################################


s1g_ch_support_country_list = ["US", "JP", "TW", "AU", "NZ", "K1", "K2", "SG", "T2"]
eu_ch_support_country_list = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "GB", "SA"]

# Auto-detect nrc_pkg path based on script location
# This script should be located in nrc_pkg/script/ or nrc_pkg/scripts/
SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
NRC_PKG_PATH = os.path.dirname(SCRIPT_DIR)  # Parent directory of script dir

def validate_nrc_pkg_path():
    """Validate nrc_pkg path, trying the usual locations"""
    global NRC_PKG_PATH

    # First, check if auto-detected path is valid
    if os.path.isdir(NRC_PKG_PATH):
        required_dirs = ["script", "sw/driver", "sw/firmware"]
        missing_dirs = [d for d in required_dirs if not os.path.isdir(os.path.join(NRC_PKG_PATH, d))]

        if not missing_dirs:
            print("[*] nrc_pkg path auto-detected: %s" % NRC_PKG_PATH)
            return NRC_PKG_PATH

    # If auto-detection failed, try fallback paths
    fallback_paths = [
        "/home/pi/nrc_pkg",
        "/home/ubuntu/nrc_pkg",
        os.path.expanduser("~/nrc_pkg")
    ]

    for fallback in fallback_paths:
        if os.path.isdir(fallback):
            required_dirs = ["script", "sw/driver", "sw/firmware"]
            missing_dirs = [d for d in required_dirs if not os.path.isdir(os.path.join(fallback, d))]
            if not missing_dirs:
                NRC_PKG_PATH = fallback
                print("[*] nrc_pkg path found: %s" % NRC_PKG_PATH)
                return NRC_PKG_PATH

    # Nothing usable: a prompt here would hang a systemd or scripted run,
    # so say what was checked and stop.
    print("[!] ERROR: nrc_pkg not found. Checked %s and %s" % (NRC_PKG_PATH, ", ".join(fallback_paths)))
    print("[!] Each must contain: script, sw/driver, sw/firmware")
    sys.exit(1)

# Validate path at startup
validate_nrc_pkg_path()

def check_and_fix_permissions(script_path):
    """Check if script has execute permission, if not, add it"""
    if not os.path.exists(script_path):
        return False

    if not os.access(script_path, os.X_OK):
        print("[*] Adding execute permission to %s" % script_path)
        try:
            os.chmod(script_path, 0o755)
            return True
        except PermissionError:
            # Try with sudo
            print("[*] Trying with sudo...")
            ret = os.system("sudo chmod +x %s" % script_path)
            return ret == 0
    return True

def check_and_install_tool(tool_name, package_name=None):
    """Check if a command-line tool exists, if not, try to install it"""
    if package_name is None:
        package_name = tool_name

    # sbin is not on a plain user's PATH, so `which dnsmasq` misses an
    # installed one and every run would try to reinstall it
    for d in ('/usr/sbin', '/sbin', '/usr/bin', '/bin', '/usr/local/sbin', '/usr/local/bin'):
        if os.path.exists(os.path.join(d, tool_name)):
            return True

    # Check if tool exists (Python 2/3 compatible)
    try:
        # Try using which command
        subprocess.check_output(['which', tool_name], stderr=subprocess.STDOUT)
        return True
    except subprocess.CalledProcessError:
        # Tool not found, try to install
        print("[!] %s not found, attempting to install..." % tool_name)
        install_cmd = "sudo apt-get update -qq && sudo apt-get install -y %s" % package_name
        ret = os.system(install_cmd)
        if ret == 0:
            print("[*] %s installed successfully" % tool_name)
            return True
        else:
            print("[!] Failed to install %s" % tool_name)
            return False
    except OSError:
        # which command doesn't exist, assume tool is not available
        print("[!] %s not found, attempting to install..." % tool_name)
        install_cmd = "sudo apt-get update -qq && sudo apt-get install -y %s" % package_name
        ret = os.system(install_cmd)
        if ret == 0:
            print("[*] %s installed successfully" % tool_name)
            return True
        else:
            print("[!] Failed to install %s" % tool_name)
            return False

def ensure_script_executable(script_path):
    """Ensure script exists and is executable"""
    if not os.path.exists(script_path):
        print("[!] WARNING: Script not found: %s" % script_path)
        return False

    if not check_and_fix_permissions(script_path):
        print("[!] WARNING: Could not set execute permission for: %s" % script_path)
        return False

    return True

def check_system_dependencies():
    """Check and install required system tools"""
    print("[*] Checking system dependencies...")
    required_tools = {
        'iw': 'iw',
        'rfkill': 'rfkill',
        'ifconfig': 'net-tools',
        'brctl': 'bridge-utils',
        # the DHCP server every AP / relay / mesh portal run needs
        'dnsmasq': 'dnsmasq'
    }

    missing_tools = []
    for tool, package in required_tools.items():
        if not check_and_install_tool(tool, package):
            missing_tools.append(tool)

    if missing_tools:
        print("[!] WARNING: Some tools could not be installed: %s" % ", ".join(missing_tools))
        print("[!] You may need to install them manually")
    else:
        print("[*] All system dependencies are available")

script_path = NRC_PKG_PATH + "/script/"

def check(interface):
    """The interface's IPv4 address as an 'inet <addr>' line, '' when none."""
    addr = nrc_net.iface_ipv4(interface)
    return "inet " + addr if addr else ''



def usage_print():
    print("Usage: \n\tstart.py [sta_type] [security_mode] [country] [channel] [sniffer_mode] \
            \n\tstart.py [sta_type] [security_mode] [country] [mesh_mode] [mesh_peering] [mesh_ip]")
    print("Argument:    \n\tsta_type      [0:STA   |  1:AP  |  2:SNIFFER  | 3:RELAY |  4:MESH] \
            \n\tsecurity_mode [0:Open  |  1:WPA2-PSK  |  2:WPA3-OWE  |  3:WPA3-SAE | 4:WPA2-PSK(WPS-PBC)] \
                         \n\tcountry       [US:USA  |  JP:Japan  |  TW:Taiwan  |  AU:Australia  |  NZ:New Zealand  | \
                         \n\t               K1:Korea-USN  |  K2:Korea-MIC  |  SG:Singapore | T2: Taiwan-nCC \
                         \n\t               and EU channel support countries(EU countries, GB and SA)] \
                         \n\t----------------------------------------------------------- \
                         \n\tchannel       [S1G Channel Number]   * Only for Sniffer & AP \
                         \n\t               (AP: giving a channel = manual; channel_manager auto is ignored) \
                         \n\tsniffer_mode  [0:Local | 1:Remote]   * Only for Sniffer \
                         \n\tmesh_mode     [0:MPP | 1:MP | 2:MAP] * Only for Mesh \
                         \n\tmesh_peering  [Peer MAC address]     * Only for Mesh \
                         \n\tmesh_ip       [Static IP address]    * Only for Mesh")
    print("Example:  \n\tOPEN mode STA for US                : ./start.py 0 0 US \
                      \n\tSecurity mode AP for US                : ./start.py 1 1 US \
                      \n\tLocal Sniffer mode on CH 40 for Japan  : ./start.py 2 0 JP 40 0 \
                      \n\tSAE mode Mesh AP for US                : ./start.py 4 3 US 2 \
                      \n\tMesh Point with static ip              : ./start.py 4 3 US 1 192.168.222.1 \
                      \n\tMesh Point with manual peering         : ./start.py 4 3 US 1 8c:0f:fa:00:29:46 \
                      \n\tMesh Point with manual peering & ip    : ./start.py 4 3 US 1 8c:0f:fa:00:29:46 192.168.222.1")
    print("Note: \n\tsniffer_mode should be set as '1' when running sniffer on remote terminal \
                  \n\tMPP, MP mode support only Open, WPA3-SAE security mode")
    exit()

def strSTA():
    if int(sys.argv[1]) == 0:
        return 'STA'
    elif int(sys.argv[1]) == 1:
        return 'AP'
    elif int(sys.argv[1]) == 2:
        return 'SNIFFER'
    elif int(sys.argv[1]) == 3:
        return 'RELAY'
    elif int(sys.argv[1]) == 4:
        return 'MESH'
    else:
        usage_print()

def checkEUCountry():
    if str(sys.argv[3]) in eu_ch_support_country_list:
        return True

def checkCountry():
    if checkEUCountry() or str(sys.argv[3]) in s1g_ch_support_country_list:
        return
    else:
        usage_print()

def checkMeshUsage():
    global sw_enc,relay_type, peer, static_ip
    if len(sys.argv) < 5:
        usage_print()
    # Use sw_enc=2 with Hybrid Security
    # sw_enc=2
    relay_type = int(sys.argv[4])
    if len(sys.argv) == 6:
        if isMacAddress(sys.argv[5]):
            peer = sys.argv[5]
        elif isIP(sys.argv[5]):
            static_ip = sys.argv[5]
        elif sys.argv[5] == 'nodhcp':
            static_ip = 'nodhcp'
        else:
            usage_print()
    elif len(sys.argv) == 7:
        if isMacAddress(sys.argv[5]):
            peer = sys.argv[5]
        else:
            usage_print()
        if isIP(sys.argv[6]):
            static_ip = sys.argv[6]
        elif sys.argv[6] == 'nodhcp':
            static_ip = 'nodhcp'
        else:
            usage_print()
    argv_print()

def checkParamValidity():
    if strSTA() == 'STA' and int(power_save) > 0 and int(listen_interval) > 65535:
        print("Max listen_interval is 65535!")
        exit()

def strSecurity():
    if int(sys.argv[2]) == -1:
        return 'CUSTOM'
    elif int(sys.argv[2]) == 0:
        return 'OPEN'
    elif int(sys.argv[2]) == 1:
        return 'WPA2-PSK'
    elif int(sys.argv[2]) == 2:
        return 'WPA3-OWE'
    elif int(sys.argv[2]) == 3:
        return 'WPA3-SAE'
    elif int(sys.argv[2]) == 4:
        return 'WPA2-PSK(WPS-PBC)'
    else:
        usage_print()

def strPSType():
    if int(power_save) == 0:
        return 'Always On'
    elif int(power_save) == 2:
        return 'Deep Sleep (TIM)'
    elif int(power_save) == 3:
        return 'Deep Sleep (nonTIM)'
    else:
        return 'Invalid Type'

def strSnifferMode():
    if int(sys.argv[5]) == 0:
        return 'LOCAL'
    elif int(sys.argv[5]) == 1:
        return 'REMOTE'
    else:
        usage_print()

def strOnOff(param):
    if int(param) == 1:
        return 'ON'
    else:
        return 'OFF'

def strAMPDUMode(param):
    if int(param) == 0:
        return 'OFF'
    elif int(param) == 1:
        return 'MANUAL'
    else:
        return 'AUTO'

def strMeshMode():
    if int(sys.argv[4]) == 0:
        return 'Mesh Portal'
    elif int(sys.argv[4]) == 1:
        return 'Mesh Point'
    elif int(sys.argv[4]) == 2:
        return 'Mesh AP'
    else:
        usage_print()

def strOriCountry():
    if str(sys.argv[3]) == 'K1' or str(sys.argv[3]) == 'K2':
        return 'KR'
    elif str(sys.argv[3]) == 'T2':
        return 'TW'
    else:
        return str(sys.argv[3])

def isNumber(s):
    try:
        float(s)
        return True
    except ValueError:
        return False

def isMacAddress(s):
    if len(s) == 17 and ':' in s:
        return True
    else:
        return False

def isIP(s):
    if len(s) > 6 and len(s) < 16 and '.' in s:
        return True
    else:
        return False

def strBDName():
    if str(bd_name):
        return str(bd_name)
    else:
        return 'nrc' + str(model) + '_bd.dat'

def configure_power_save_gpio(use_eeprom_config):
    # Check kernel version
    import platform
    kernel_version = platform.release()
    # Parse major and minor version as integers for proper comparison
    version_parts = kernel_version.split('.')
    major = int(version_parts[0])
    minor = int(version_parts[1])

    # Compare as tuple (6, 12) >= (6, 6) = True
    is_kernel_6_6_or_later = (major, minor) >= (6, 6)

    if int(use_eeprom_config) == 1:
        if is_kernel_6_6_or_later:
            #7394 STA kernel 6.6+ (host_gpio_out(527) --> target_gpio_in(14))
            ps_gpio_arg = " power_save_gpio=527,14,1"
        else:
            #7394 STA using EEPROM B/D (host_gpio_out(15) --> target_gpio_in(12))
            ps_gpio_arg = " power_save_gpio=15,12,1"
    else:
        if is_kernel_6_6_or_later:
            #7394 STA kernel 6.6+ (host_gpio_out(529) --> target_gpio_in(14))
            ps_gpio_arg = " power_save_gpio=529,14,1"
        else:
            #7394 STA (host_gpio_out(17) --> target_gpio_in(14))
            ps_gpio_arg = " power_save_gpio=17,14,1"

    return ps_gpio_arg

def argv_print():
    print("------------------------------")
    print("Model            : " + str(model))
    print("STA Type         : " + strSTA())
    print("Country          : " + str(sys.argv[3]))
    print("Security Mode    : " + strSecurity())
    print("BD Name          : " + strBDName())
    print("AMPDU            : " + strAMPDUMode(ampdu_enable))
    if strSTA() == 'STA':
        print("CQM              : " + strOnOff(cqm_enable))
    if strSTA() == 'SNIFFER':
        print("Channel Selected : " + str(sys.argv[4]))
        print("Sniffer Mode     : " + strSnifferMode())
    if int(fw_download) == 1:
        print("Download FW      : " + fw_name)
    if int(auto_fw_update) == 1:
        print("Update FW        : " + fw_update_name)
    print ("MAX TX Power     : " + ("auto (board data)"
           if str(max_txpwr).strip() in ('-1', '') else str(max_txpwr) + " dBm"))
    if int(bss_max_idle_enable) == 1 :
        if strSTA() == 'AP' or strSTA() == 'RELAY' or strSTA() == 'STA':
            print("BSS MAX IDLE     : " + str(bss_max_idle))
    if strSTA() == 'STA':
        print("Power Save Type  : " + strPSType())
        if int(power_save) > 0:
            print("PS Timeout       : " + ps_timeout)
        if int(power_save) == 3:
            print("Sleep Duration   : " + sleep_duration)
        if int(listen_interval) > 0:
            print("Listen Interval  : " + str(listen_interval))
    if strSTA() == 'MESH':
        print("Mesh Mode        : " + strMeshMode())
    # Bridged runs address br0, not the radio, and only ip mode 2 serves DHCP
    # there - printing the AP pool in the other modes describes a server that
    # never starts.
    if strSTA() in ('AP', 'RELAY', 'MESH') and int(use_bridge_setup) == 0:
        ip, pfx, start, end = ap_pool()
        print("HaLow AP address : %s/%d  DHCP %s - %s" % (ip, pfx, start, end))
    if strSTA() in ('STA', 'RELAY') and sta_static_ip:
        print("STA static IP    : " + sta_static_ip + (" gw " + sta_gateway if sta_gateway else ""))
    if int(use_bridge_setup) > 0:
        mode = int(bridge_ip_mode)
        line = "Bridge           : br0, %s" % {0: 'static ip', 1: 'dhcp client',
                                               2: 'dhcp server'}.get(mode, str(mode))
        if strSTA() == 'RELAY':
            line += " (addressed on the STA side)"
        elif mode == 2:
            ip, pfx, start, end = ap_pool()
            line += "  %s/%d  DHCP %s - %s" % (ip, pfx, start, end)
        elif mode == 0:
            cidr, gw = bridge_static()
            line += "  %s%s" % (cidr, (" gw " + gw) if gw else "")
        print(line)
    if eth_ip_mode != 'dhcp':
        print("Ethernet         : %s %s" % (eth_ip_mode, eth_static_ip))
    if int(eu_en_300_220) == 1:
        print("EU EN 300 220    : Enabled (100ms TX pause)")
    if int(use_eeprom_config) == 1:
        print("CONFIG_LOCATION  : EEPROM")
    else:
        print("CONFIG_LOCATION  : FLASH")
    print("------------------------------")

def copyConf():
    copy_script = NRC_PKG_PATH + "/sw/firmware/copy.sh"
    if ensure_script_executable(copy_script):
        os.system("sudo " + copy_script + " " + str(model) + " " + strBDName() + " " + str(use_eeprom_config))
    else:
        print("[!] Skipping firmware copy (script not available)")


def iface_ipv4(interface):
    return nrc_net.iface_ipv4(interface)


relay_routed = False   # set by setup_ap_network for a routed (no NAT) relay

def relay_route_hint():
    """Print the route the upstream side needs when the relay does not NAT.

    Only this box can say what the address is, and nobody else will say it:
    without that route the AP-leg clients reach the upstream network and get
    no replies, which looks exactly like a broken radio link. Said at the end
    of bring-up rather than while installing the rules, because the station
    leg is still getting its lease then and the line would have had a
    placeholder where the address belongs.
    """
    up = relay_sta_leg()
    subnet = ap_pool()[0].rsplit('.', 1)[0] + '.0'
    via = ''
    for _ in range(15):                       # the lease usually lands first
        via = iface_ipv4(up)
        if via:
            break
        time.sleep(1)
    print("[*] NAT is off: the upstream side needs a route back to %s/24" % subnet)
    print("      ip route add %s/24 via %s"
          % (subnet, via or "<this relay's %s address>" % up))

DNSMASQ_EXTRA_DIR = script_path + "conf/etc/dnsmasq.d"


def relay_sta_leg():
    """The vif a relay joins upstream on, and so the MAC the AP knows it by.

    relay_type says which half is which (0: wlan0 STA, 1: wlan1 STA) and the
    dispatch at the bottom starts run_sta on exactly this one. Read it here
    rather than spelling the names out again wherever the uplink is meant.
    """
    return 'wlan1' if int(relay_type) == 1 else 'wlan0'


def upstream_eth():
    """Wired port the bridge, NAT and the eth settings use.

    A board with two wired ports has no right answer to guess: eth_iface
    names it, 'auto' takes the default-route port and the legacy
    use_bridge_setup index is the last fallback.
    """
    return nrc_net.detect_eth_iface(eth_iface, max(int(use_bridge_setup) - 1, 0))


def ap_pool():
    """(ip, prefix, pool_start, pool_end) of the HaLow-side DHCP server."""
    default = "192.168.150.1" if strSTA() == 'RELAY' else "192.168.200.1"
    return nrc_net.resolve_pool(default, ap_ip, dhcp_start, dhcp_end)


def bridge_static():
    """br0 address and gateway in bridge static mode (bridge_ip_mode=0).

    A station or relay is addressed on its uplink, which is what sta_static_ip
    names. An AP serves the bridge instead and has no such item on its screen -
    its address is ap_ip, the same one dhcp-server mode puts on br0. Falling
    back to the constant there ignored what the operator set.
    """
    if strSTA() == 'AP' and not sta_static_ip:
        ip, pfx, _start, _end = ap_pool()
        return "%s/%d" % (ip, pfx), sta_gateway
    return (sta_static_ip or "192.168.200.100/24"), (sta_gateway or "192.168.200.1")


def server_ranges(iface):
    """dnsmasq ranges: the HaLow side on iface (None = none) + eth in dhcp-server mode."""
    ranges = []
    if iface:
        ip, pfx, start, end = ap_pool()
        ranges.append({'iface': iface, 'start': start, 'end': end,
                       'netmask': nrc_net.prefix_to_netmask(pfx), 'lease': '24h'})
    if eth_ip_mode == 'dhcp-server' and eth_static_ip:
        eip, epfx, es, ee = nrc_net.resolve_pool(eth_static_ip)
        ranges.append({'iface': upstream_eth(), 'start': es, 'end': ee,
                       'netmask': nrc_net.prefix_to_netmask(epfx), 'lease': '24h'})
    return ranges


def mesh_net():
    """Addressing the mesh flows hand to nrc_net (portal address, pool, uplink)."""
    ip, pfx, start, end = ap_pool()
    return {'ip': ip, 'prefix': pfx, 'start': start, 'end': end,
            'eth': upstream_eth(), 'dnsmasq_d': DNSMASQ_EXTRA_DIR}


def setup_sta_network(iface):
    """Client-side addressing once wpa_supplicant runs. Returns the interface to wait on."""
    if int(use_bridge_setup) > 0:
        mode = int(bridge_ip_mode)
        nrc_net.dhcpcd_off_bridge_ports()
        if mode == 1:
            nrc_net.start_dhcp_client_recorded('br0')
            nrc_net.arp_flux_guard()
        elif mode == 0:
            cidr, gw = bridge_static()
            nrc_net.set_static_ip('br0', cidr, gw, metric=100)
        else:
            ip, pfx, _, _ = ap_pool()
            nrc_net.set_static_ip('br0', "%s/%d" % (ip, pfx))
            nrc_net.start_dhcp_server(server_ranges('br0'), DNSMASQ_EXTRA_DIR)
            nrc_net.dhcp_bridge_isolate(iface)
        return 'br0'
    if sta_static_ip:
        nrc_net.set_static_ip(iface, sta_static_ip, sta_gateway, metric=100)
    else:
        nrc_net.start_dhcp_client_recorded(iface)
    return iface


def setup_ap_network(iface):
    """AP-side NAT, address and DHCP server once hostapd runs."""
    global relay_routed
    if int(use_bridge_setup) > 0:
        if strSTA() == 'RELAY':
            return                            # br0 is addressed on the STA leg
        mode = int(bridge_ip_mode)
        if mode == 1:
            print("[7] Skip NAT (bridge in dhcp client mode)")
            print("[8] Skip AP DHCP server (br0 uses upstream DHCP)")
        elif mode == 0:
            print("[8] br0 static address (bridge)")
            cidr, gw = bridge_static()
            # keep what br0 already has: the management address moved off eth
            nrc_net.set_static_ip('br0', cidr, gw, flush=False)
        else:
            print("[8] br0 static address + DHCP server (bridge)")
            ip, pfx, _, _ = ap_pool()
            nrc_net.set_static_ip('br0', "%s/%d" % (ip, pfx), flush=False)
            nrc_net.start_dhcp_server(server_ranges('br0'), DNSMASQ_EXTRA_DIR)
            nrc_net.dhcp_bridge_isolate(upstream_eth())
        return
    print("[7] Start NAT")
    if strSTA() == 'AP':
        nrc_net.start_nat(upstream_eth(), iface)
    else:
        up = relay_sta_leg()
        if int(relay_nat) == 1:
            nrc_net.start_nat(up, iface)
        else:
            # Routed relay: the return route must live on the upstream AP (relay_route_hint).
            nrc_net.start_nat(up, iface, masquerade=False)
            relay_routed = True
    print("[8] Configure AP Network (Static IP + DHCP Server)")
    ip, pfx, start, end = ap_pool()
    nrc_net.set_static_ip(iface, "%s/%d" % (ip, pfx))
    nrc_net.start_dhcp_server(server_ranges(iface), DNSMASQ_EXTRA_DIR)









def addWLANInterface(interface):
    if strSTA() == 'RELAY' and interface == "wlan1":
        print("[*] Create wlan1 for concurrent mode")
        os.system("sudo iw dev wlan0 interface add wlan1 type managed")
        os.system("sudo ifconfig wlan1 up")
        time.sleep(3)


def chmgr_mode_str():
    return 'auto' if int(channel_manager) == 1 else 'monitor'

def chmgr_arm():
    # Arm the FW engine: sweep + pick + score retention (dcs_seed).
    # DCS starts by itself when the FW detects the BSS start.
    country = str(sys.argv[3])
    if checkEUCountry():
        country = "EU"
    if int(channel_manager_prefer_bw) not in (1, 2, 4):
        sys.exit("[chmgr] channel_manager_prefer_bw must be 1, 2 or 4 (0/all removed)")
    cmd = script_path + 'cli_app set chmgr ' + chmgr_mode_str() + \
          ' profile=' + str(channel_manager_profile) + ' cc=' + country
    cmd += ' bw=' + str(channel_manager_prefer_bw)
    dwell = min(max(int(channel_manager_dwell), 100), 1000)
    if dwell != 300:
        cmd += ' dwell=' + str(dwell)
    if str(channel_manager_exclude) not in ('', '0'):
        cmd += ' exclude=' + str(channel_manager_exclude)
    try:
        result = subprocess.check_output('timeout 30 ' + cmd, shell=True).decode()
    except:
        result = ''
    print("[chmgr] arm: " + result.strip())
    return 'ok' in result

def chmgr_check():
    # channel_manager=auto: get the boot-ACS pick and write a temp hostapd conf.
    # Pick source: FW 'show chmgr'
    # so auto also works on a chmgr-less FW.
    country = str(sys.argv[3])
    if checkEUCountry():
        country = "EU"
    conf_path = script_path + "conf/" + country
    conf_temp = script_path + "conf/temp_chmgr.conf"
    conf_file = ""

    if strSecurity() == "CUSTOM":
        conf_path = "."
        conf_file = "/wizard_ap.conf"
    elif strSecurity() == "OPEN":
        conf_file += "/ap_halow_open.conf"
    elif strSecurity() == 'WPA2-PSK':
        conf_file += "/ap_halow_wpa2.conf"
    elif strSecurity() == 'WPA3-OWE':
        conf_file += "/ap_halow_owe.conf"
    elif strSecurity() == 'WPA3-SAE':
        conf_file += "/ap_halow_sae.conf"
    elif strSecurity() == 'WPA2-PSK(WPS-PBC)':
        conf_file += "/ap_halow_pbc.conf"

    best_channel = ''
    try:
        result = subprocess.check_output('timeout 30 ' + script_path +
                                         'cli_app show chmgr', shell=True).decode()
        m = re.search(r'pick=(\d+)', result)
        if m:
            best_channel = m.group(1)
            print(result)
    except:
        pass


    if best_channel in ('', '0'):   # disabled status prints pick=0
        return 'Fail'

    os.system("sudo cp " + conf_path + conf_file + " " + conf_temp)
    os.system("sed -i '/channel=/d' " + conf_temp)
    os.system("sed -i '/hw_mode=/d' " + conf_temp)
    os.system("sed -i '/#ssid=/d' " + conf_temp)
    if int(best_channel) < 36:
        os.system('sed -i "/ssid=.*/ahw_mode=g" ' + conf_temp)
    else:
        os.system('sed -i "/ssid=.*/ahw_mode=a" ' + conf_temp)
    os.system('sed -i "/hw_mode=.*/achannel=' + best_channel + '" ' + conf_temp)
    os.system("sed -i \"s/^country_code=.*/country_code=%s/g\" %s" %
              (strOriCountry(), conf_temp))
    print("[chmgr] start with channel: " + best_channel)
    return 'Done'

def ft232h_usb():
    # Re-define SPI parameters for ft232h_usb_spi
    # ft232h_usb_spi
    global spi_clock, spi_bus_num, spi_gpio_irq, spi_cs_num, spi_polling_interval
    print("[*] use ft232h_usb_spi")
    spi_bus_num = 3
    spi_gpio_irq = 600
    if int(spi_clock) > 15000000:
        spi_clock = 15000000
    if int(spi_cs_num) != 0:
        spi_cs_num = 0
    if int(spi_polling_interval) <= 0:
        spi_polling_interval = 50
    if int(ft232h_usb_spi) != 1:
        spi_gpio_irq = -1

def ch347_usb():
    # Re-define SPI parameters for ch347_usb_spi
    # ch347_usb_spi
    global spi_clock, spi_bus_num, spi_gpio_irq, spi_cs_num, spi_polling_interval
    print("[*] use ch347_usb_spi")
    spi_bus_num = 3
    spi_gpio_irq = -1
    if int(spi_clock) > 20000000:
        spi_clock = 20000000
    if int(spi_cs_num) != 0:
        spi_cs_num = 0
    if int(spi_polling_interval) <= 0:
        spi_polling_interval = 50

def setAPParam():
    # Re-define parameters for AP mode
    global ndp_preq
    ndp_preq=1

def setRelayParam():
    # Re-define parameters for RELAY mode
    global sw_enc, power_save, ndp_ack_1m, ndp_preq
    power_save=0; ndp_ack_1m=0; ndp_preq=0;
    # Use sw_enc=2 with Hybrid Security
    # sw_enc=2

def setSnifferParam():
    # Re-define parameters for Sniffer mode
    global sw_enc, ampdu_enable, bss_max_idle_enable, power_save, ndp_ack_1m, listen_interval
    sw_enc=0; ampdu_enable=0; bss_max_idle_enable=0; power_save=0; ndp_ack_1m=0; listen_interval=0;

def setMeshParam():
    # Re-define parameters for MESH mode
    global short_bcn_enable
    short_bcn_enable=0

def setModuleParam():
    # Set module parameters based on configuration

    # Initialize arguments for module params
    spi_arg = fw_arg = fw_update_arg = power_save_arg = sleep_duration_arg = idle_mode_arg = \
    bss_max_idle_arg = ndp_preq_arg = ndp_ack_1m_arg = auto_ba_arg = \
    sw_enc_arg =  cqm_arg = listen_int_arg = drv_dbg_arg = sched_scan_arg = tw_band_arg = \
    sbi_arg = discard_deauth_arg = dbg_fc_arg = kr_band_arg = legacy_ack_arg = \
    be_arg = rs_arg = beacon_bypass_arg = ps_gpio_arg = bd_name_arg = \
    support_ch_width_arg = ps_pretend_arg = duty_cycle_arg = cca_thresh_arg = \
    twt_arg = auth_control_arg = sub_xtal_bypass_arg = macaddr_arg = \
    loc_1m_primary_ch_arg = tpc_arg = bw_4m_2m_prim_loc_arg = en300_arg = ""

    # Check ft232h_usb_spi
    if int(ft232h_usb_spi) > 0:
        ft232h_usb()

    # Check ch347_usb_spi
    if int(ch347_usb_spi) > 0:
        ch347_usb()

    # Set parameters for AP (support NDP probing)
    if strSTA() == 'AP':
        setAPParam()

    # Set parameters for RELAY
    if strSTA() == 'RELAY':
        setRelayParam()

    # Set parameters for SNIFFER
    if strSTA() == 'SNIFFER':
        setSnifferParam()

    # Set parameters for MESH
    if strSTA() == 'MESH':
        setMeshParam()

    # module param for spi setting
    # default:
    #  hifspeed(20000000) spi_bus_num(0) spi_cs_num(0) spi_gpio_irq(5) spi_polling_interval(0)
    spi_arg = " hifspeed=" + str(spi_clock) + \
              " spi_bus_num=" + str(spi_bus_num) + \
              " spi_cs_num=" + str(spi_cs_num) + \
              " spi_gpio_irq=" + str(spi_gpio_irq) + \
              " spi_polling_interval=" + str(spi_polling_interval)

    # module param for FW download from host
    # default: fw_name (NULL: no download)
    if int(fw_download) == 1:
        fw_arg = " fw_name=" + fw_name

    # module param for FW update from host to flash
    # default: fw_update_name (NULL: no update)
    if int(auto_fw_update) == 1:
        fw_update_arg = " auto_fw_update=1 fw_update_name=" + fw_update_name

    # module param for power_save
    # default: power_save(0: active mode) sleep_duration(0,0)
    if strSTA() == 'STA' and int(power_save) > 0:
        if str(model) == "7394":
            ps_gpio_arg = configure_power_save_gpio(use_eeprom_config)
        power_save_arg = " power_save=" + str(power_save)
        if int(power_save) == 3:
            sleep_duration_arg = " sleep_duration=" + re.sub(r'[^0-9]','',sleep_duration)
            unit = sleep_duration[-1]
            if unit == 'm':
                sleep_duration_arg += ",0"
            else:
                sleep_duration_arg += ",1"

    # module param for IDLE mode
    # default: idle_mode(0: disabled)
    if strSTA() == 'STA' and int(idle_mode) == 1:
        idle_mode_arg = " idle_mode=1"
        if str(model) == "7394":
            ps_gpio_arg = configure_power_save_gpio(use_eeprom_config)

    if int(duty_cycle_enable) == 1:
        duty_cycle_arg = " set_duty_cycle="+str(duty_cycle_enable)+","+str(duty_cycle_window)+","+str(duty_cycle_duration)

    cca_thresh_arg = " set_cca_threshold="+str(cca_threshold)

    # module param for bss_max_idle (keep alive)
    # default: bss_max_idle(0: disabled)
    if int(bss_max_idle_enable) == 1:
        if strSTA() == 'AP' or strSTA() == 'RELAY' or strSTA() == 'STA':
            bss_max_idle_arg = " bss_max_idle=" + str(bss_max_idle)

    # module param for NDP Prboe Request (NDP scan)
    # default: ndp_preq(0: disabled)
    if int(ndp_preq) == 1:
        ndp_preq_arg = " ndp_preq=1"

    # module param for EU EN 300 220 mode
    # default: eu_en_300_220(0: EN 304 220)
    if int(eu_en_300_220) == 1:
        en300_arg = " eu_en_300_220=1"

    # module param for 1MBW NDP ACK
    # default: ndp_ack_1m(0: disabled)
    if int(ndp_ack_1m) == 1:
        ndp_ack_1m_arg = " ndp_ack_1m=1"

    # module param for AMPDU
    # default: auto (0: disable 1: manual 2: auto)
    if int(ampdu_enable) != 2:
        auto_ba_arg = " ampdu_mode=" + str(ampdu_enable)

    # module param for SW-based ENC/DEC
    # default: sw_enc(0: HW-based ENC/DEC)
    if int(sw_enc) > 0:
        sw_enc_arg = " sw_enc=" + str(sw_enc)

    # module param for CQM
    # default: disable_cqm(0: CQM enabled)
    if int(cqm_enable) == 0:
        cqm_arg = " disable_cqm=1"

    # module param for short beacon
    # default: enable_short_bi(1: Short Beacon enabled)
    if int(short_bcn_enable) == 1:
        sbi_arg = " enable_short_bi=1"
    elif int(short_bcn_enable) == 0:
        sbi_arg = " enable_short_bi=0"

    # module param for legacy ack mode
    # default: 0(Legacy ACK disabled)
    if int(legacy_ack_enable) == 1:
        legacy_ack_arg = " enable_legacy_ack=1"

    # module param for authentication control
    # default: 0(disabled)
    if int(auth_control_enable) == 1 and strSTA() == 'AP':
        auth_control_arg = " set_auth_control="+str(auth_control_enable)+","+str(auth_control_slot)+","+\
                         str(auth_control_ti_min)+","+str(auth_control_ti_max)+ ","+str(auth_control_scale)

    # module param for beacon bypass
    # default: 0(beacon bypass disabled)
    if int(beacon_bypass_enable) == 1:
        beacon_bypass_arg = " enable_beacon_bypass=1"

    # module param for sched scan
    # default: 0(sched scan disabled)
    if int(sched_scan_enable) == 1:
        sched_scan_arg = " enable_sched_scan=1"

    # module param for listen interval
    # default: listen_interval(100)
    if int(listen_interval) > 0:
        listen_int_arg = " listen_interval=" + str(listen_interval)

    # module param for KR Band (KR only)
    # default: not defined(-1) (1:K1(KR USN1), 2:K2(KR USN5))
    if str(sys.argv[3]) == 'K1':
        kr_band_arg = " kr_band=1"
    elif str(sys.argv[3]) == 'K2':
        kr_band_arg = " kr_band=2"

    # module param for TW Band (TW only)
    # default: not defined(TW:Taiwan) (2:T2(TW NCC))
    if str(sys.argv[3]) == 'T2':
        tw_band_arg = " tw_band=2"

    # module param for deauth-discard on STA (test only)
    # default: discard_deauth(0: disabled)
    if int(discard_deauth) == 1:
        discard_deauth_arg = " discard_deauth=1"

    # module param for driver debug (debug only)
    # default: debug_level_all(0: disabled)
    if int(driver_debug) == 1:
        drv_dbg_arg = " debug_level_all=1"

    # module param for flow control debug (debug only)
    # default: dbg_flow_control(0: disabled)
    if int(dbg_flow_control) == 1:
        dbg_fc_arg = " debug_level_all=1 dbg_flow_control=1"

    # module param for bitmap encoding
    # default: use bitmap encoding (1: enabled)
    if int(bitmap_encoding) == 0:
        be_arg = " bitmap_encoding=0"

    # module param for reverse scrambler
    # default: use reverse scrambler (1: enabled)
    if int(reverse_scrambler) == 0:
        rs_arg = " reverse_scrambler=0"

    # module param for power save pretend
    # default: use power save pretend (0: disabled)
    if int(power_save_pretend) == 1:
        ps_pretend_arg = " ps_pretend=1"

    # module param for TWT
    # default: disabled
    if int(twt_num) > 0 or int(twt_sp) > 0 or int(twt_int) > 0:
        twt_arg = " twt_num=" + str(twt_num) + " twt_sp=" + str(twt_sp) + " twt_int=" + str(twt_int) + \
                  " twt_force_sleep=" + str(twt_force_sleep) + " twt_num_in_group=" + str(twt_num_in_group) + \
                  " twt_algo=" + str(twt_algo)

    # module param for Sub XTAL bypass
    # default: use sub_xtal_pass (0: false)
    if int(sub_xtal_bypass) == 1:
        sub_xtal_bypass_arg = " sub_xtal_bypass=1"

    # module param for board data file
    # default: bd.dat
    bd_name_arg = " bd_name=" + strBDName()

    # module param for supported channel width
    # default : support 1/2/4MHz (1: 1/2/4Mhz)
    if strSTA() == 'STA' and int(support_ch_width) == 0:
        support_ch_width_arg = " support_ch_width=0"

    # module param for MAC address
    # default : use MAC address from EEPROM/Flash
    if macaddr != '':
        macaddr_arg = " macaddr=" + macaddr

    # module param for location of 1MHz primary channel
    # default : BW2M(0) BW4M(1) (-1)
    if int(loc_1m_primary_ch) >= -1 and (strSTA() == 'AP' or strSTA() == 'SNIFFER'):
        loc_1m_primary_ch_arg = " loc_1m_primary_ch=" + str(loc_1m_primary_ch)

    # module param for 4M op as 2M primary (no 1M), 0:lower 1:upper, -1:off
    if int(bw_4m_2m_prim_loc) in (0, 1):
        bw_4m_2m_prim_loc_arg = " bw_4m_2m_prim_loc=" + str(bw_4m_2m_prim_loc)

    # module param for tx power control
    # default: not use tpc (0: disabled)
    if int(tpc) == 1:
        tpc_arg = " tx_power_control=1"

    # module parameter setting while loading NRC driver
    # Default value is used if arg is not defined
    module_param = spi_arg + fw_arg + fw_update_arg + \
                 power_save_arg + sleep_duration_arg + idle_mode_arg + bss_max_idle_arg + \
                 ndp_preq_arg + ndp_ack_1m_arg + auto_ba_arg + sw_enc_arg + \
                 cqm_arg + listen_int_arg + drv_dbg_arg + sched_scan_arg + tw_band_arg + \
                 sbi_arg + discard_deauth_arg + dbg_fc_arg + kr_band_arg + legacy_ack_arg + \
                 be_arg + rs_arg + beacon_bypass_arg + ps_gpio_arg + bd_name_arg + support_ch_width_arg + \
                 ps_pretend_arg + duty_cycle_arg + cca_thresh_arg + twt_arg + auth_control_arg + \
                 sub_xtal_bypass_arg + macaddr_arg + loc_1m_primary_ch_arg + \
                 bw_4m_2m_prim_loc_arg + tpc_arg + en300_arg

    return module_param

def run_common():
    # Check and install system dependencies
    check_system_dependencies()

    # Ensure required scripts are executable
    scripts_to_check = [
        NRC_PKG_PATH + "/script/conf/etc/clock_config.sh",
        NRC_PKG_PATH + "/sw/firmware/copy.sh"
    ]

    for script in scripts_to_check:
        ensure_script_executable(script)

    if int(max_cpuclock) == 1:
        print("[*] Set Max CPU Clock on RPi")
        clock_script = NRC_PKG_PATH + "/script/conf/etc/clock_config.sh"
        if ensure_script_executable(clock_script):
            os.system("sudo " + clock_script)
        else:
            print("[!] Skipping clock configuration (script not available)")

    # A previous bridge run left the management address on br0. Tearing that
    # stack down destroys br0 and the address with it, so hand it back to eth
    # before anything is stopped - the run takes it over again further down.
    if os.path.exists("/run/nrc_bridge_takeover.state"):
        move_script = NRC_PKG_PATH + "/script/conf/etc/bridge_ip_move.sh"
        if ensure_script_executable(move_script):
            print("[*] Releasing the bridge address from the previous run")
            os.system("sudo " + move_script + " release")

    # stop.py, bridge_ip_move.sh and the boot failsafe read these names.
    # Only start_modular.py used to write them, so the stock flow left the
    # eth0 default behind and a bridge on eth1 was handed back to eth0.
    nrc_net.write_net_state(upstream_eth(), 'wlan0', 'wlan1')

    print("[0] Clear")
    # The channel manager holds the netlink group and drives channel
    # switches; a leftover daemon from the previous session would keep
    # doing both. Match the executable name - a -f pattern also matches
    # this script's own invocation.
    os.system("sudo pkill -9 -x chmgrd 2>/dev/null")
    # NetworkManager: unmanage wlan/p2p/br0 instead of stopping it system-
    # wide (a full stop kills the ethernet DHCP lease / SSH on NM-based hosts)
    os.system("sudo mkdir -p /etc/NetworkManager/conf.d 2>/dev/null")
    os.system("printf '[keyfile]\\nunmanaged-devices=interface-name:wlan*;interface-name:p2p*;interface-name:br0\\n' | "
              "sudo tee /etc/NetworkManager/conf.d/nrc-unmanaged-wlan.conf > /dev/null 2>&1")
    os.system("sudo systemctl try-reload-or-restart NetworkManager 2>/dev/null")
    os.system("sudo systemctl stop wpa_supplicant 2>/dev/null")
    os.system("sudo hostapd_cli disable 2>/dev/null")
    os.system("sudo wpa_cli disable wlan0 2>/dev/null ")
    os.system("sudo wpa_cli disable wlan1 2>/dev/null")
    os.system("sudo killall -9 wpa_supplicant 2>/dev/null")
    os.system("sudo killall -9 hostapd 2>/dev/null")
    os.system("sudo killall -9 wireshark 2>/dev/null")
    os.system("sudo rmmod nrc 2>/dev/null")
    # The modular stack claims the same SPI device (both flavours export the
    # alias spi:nrc80211), so anything left of it - including a copy udev
    # auto-loaded from /lib/modules at boot - makes our insmod fail with
    # "Device or resource busy". start_modular.py clears this flavour the same
    # way. Dependency order: frontends, then core, then the SPI backend.
    for mod in ("nrc_wlan", "nrc_mcp", "nrc_core", "nrc_spi"):
        os.system("sudo rmmod %s 2>/dev/null" % mod)
    os.system("sudo rm "+script_path+"conf/temp_hostapd_config.conf 2>/dev/null")
    os.system("sudo rm "+script_path+"conf/temp_chmgr.conf 2>/dev/null")
    os.system("sudo rm "+script_path+"conf/temp_m[ap]*_halow_*.conf 2>/dev/null")
    os.system("sudo sh -c '[ -e /proc/sys/kernel/sysrq ] && echo 0 > /proc/sys/kernel/sysrq'")
    nrc_net.teardown()
    time.sleep(1)

    print("[1] Copy and Set Module Parameters")
    copyConf()
    insmod_arg = setModuleParam()

    print("[2] Set Initial Country")
    country_code = strOriCountry()
    ret = os.system("sudo iw reg set " + country_code)

    # Verify country code setting
    country_set_success = False
    time.sleep(1)
    try:
        iw_reg_result = subprocess.check_output("sudo iw reg get", shell=True).decode()
        if "country " + country_code in iw_reg_result:
            print("[*] Country code (%s) set successfully via iw reg" % country_code)
            country_set_success = True
        else:
            print("[!] WARNING: iw reg set failed to set country code")
    except:
        print("[!] ERROR: iw reg set failed, will retry after loading driver")

    print("[3] Loading module")
    # monolithic driver lives in sw/driver/nrc/ (the modular stack used by
    # start_modular.py lives in sw/driver/nrc_modular/); the bare sw/driver/
    # path is kept as a fallback for old package layouts
    nrc_ko = NRC_PKG_PATH + "/sw/driver/nrc/nrc.ko"
    if not os.path.exists(nrc_ko):
        nrc_ko = NRC_PKG_PATH + "/sw/driver/nrc.ko"
    print("sudo insmod %s " % nrc_ko + insmod_arg)
    os.system("sudo insmod %s " % nrc_ko + insmod_arg + "")

    if int(spi_polling_interval) <= 0:
        time.sleep(5)
    else:
        time.sleep(10)

    if strSTA() == 'RELAY' and int(relay_type) == 0:
        addWLANInterface('wlan1')
    elif strSTA() == 'MESH' and int(relay_type) == 2:
        addMeshInterface('mesh0')

    # Unblock RF-kill if blocked
    subprocess.call(["sudo", "rfkill", "unblock", "all"])
    ret = subprocess.call(["sudo", "ifconfig", "wlan0", "up"])
    if ret == 255:
        print("ERROR: Failed to bring up wlan0 interface")
        os.system('sudo rmmod nrc.ko')
        sys.exit()

    # Retry country code setting after interface is up if initial attempt failed
    if not country_set_success:
        print("[3.1] Retrying country code with iw reg after interface up")
        time.sleep(1)
        os.system("sudo iw reg set " + country_code)
        time.sleep(1)
        try:
            iw_reg_result = subprocess.check_output("sudo iw reg get", shell=True).decode()
            if "country " + country_code in iw_reg_result:
                print("[*] Country code (%s) successfully set after interface up" % country_code)
                country_set_success = True
            else:
                print("[!] WARNING: Country code setting still failed after interface up")
        except:
            print("[!] WARNING: Could not verify country code after interface up")

    print("[4] Set Maximum TX Power")
    set_txpwr()

    print("[5] Set guard interval: " + guard_int)
    os.system('sudo ' + NRC_PKG_PATH + '/script/cli_app set gi ' + guard_int)

    if eth_ip_mode != 'dhcp':
        print("[*] Ethernet %s: %s" % (upstream_eth(), eth_ip_mode))
    # called for 'dhcp' too: that is where a leftover static block is removed
    nrc_net.configure_eth(upstream_eth(), eth_ip_mode, eth_static_ip, eth_gateway, eth_dns)
    # modes without a HaLow-side server still need one for the eth pool
    if eth_ip_mode == 'dhcp-server' and strSTA() in ('STA', 'SNIFFER'):
        nrc_net.start_dhcp_server(server_ranges(None), DNSMASQ_EXTRA_DIR)

def set_txpwr():
    """TX power, per vif where the two halves need their own.

    'auto' sends nothing at all: the firmware comes up on the board data
    file's own maximum. A number is written as a ceiling against it. TPC,
    when it is on, writes a station's power itself on every decision, so
    that vif is left alone. A RELAY takes a value per side.
    """
    cli = 'sudo ' + NRC_PKG_PATH + '/script/cli_app set txpwr '

    def one(val, vif=None, tpc_owns=False):
        """auto = the board data file decides, a number caps it against it."""
        v = str(val).strip().strip("'\"").lower()
        if v in ('-1', ''):
            v = 'auto'
        pfx = ('%d ' % vif) if vif is not None else ''
        name = '' if vif is None else ' vif%d' % vif
        if tpc_owns:
            # TPC writes a station's power on every decision; a value set
            # here would only be overwritten
            print("[*] TX power%s: TPC decides" % name)
            return False
        if v == 'auto':
            # nothing is sent: the firmware comes up on the board data
            # file's own maximum, which is what "auto" asks for
            print("[*] TX power%s: auto (board data)" % name)
            return False
        os.system(cli + pfx + 'limit ' + str(val))
        print("[*] TX power%s: %s dBm" % (name, val))
        return True

    tpc_on = int(tpc) == 1
    if strSTA() == 'RELAY':
        sta_vif = int(relay_sta_leg()[-1])
        ap_vif = 0 if sta_vif == 1 else 1
        one(max_txpwr, ap_vif)
        one(max_txpwr_sta, sta_vif, tpc_owns=tpc_on)
        return
    capped = one(max_txpwr, tpc_owns=(tpc_on and strSTA() == 'STA'))
    # the mac80211 ceiling is a second limiter; a monitor interface does not
    # transmit, and TPC must not be capped from here
    if capped and strSTA() != 'SNIFFER':
        os.system('sudo iw phy nrc80211 set txpower limit '
                  + str(int(str(max_txpwr).strip().strip("'\"")) * 100))


def bridge_mac_cmd(src_iface):
    """Shell that gives br0 a real MAC of its own, taken from src_iface.

    A portless bridge gets a random MAC and keeps following its lowest port
    afterwards, so the kernel treats it as its own to change. dhcpcd takes
    the address while that random MAC is still on, and the server's lease
    then names a MAC that stops existing - which is why the AP could not
    pair a relay with its address. This has to run before the bridge is up.
    """
    try:
        with open('/sys/class/net/%s/address' % src_iface) as f:
            mac = f.read().strip()
    except OSError:
        return ''
    if not mac:
        return ''
    # arp_notify: the kernel announces the MAC when an address lands on
    # br0, so nobody keeps ARPing the one this bridge no longer wears
    return ('sudo ip link set br0 address %s 2>/dev/null; '
            'sudo sysctl -qw net.ipv4.conf.br0.arp_notify=1 2>/dev/null; '
            % mac)


def bridge_mac_src(ap_iface):
    """Which interface's MAC br0 wears on an AP.

    A relay wears its STA vif, the MAC the AP above leases to. A bridge this
    board addresses itself has no lease identity to inherit, so it wears the
    AP vif: our address then answers ARP as the BSSID, which is the one thing
    a station can check it against. A DHCP client keeps the wired MAC, or the
    server hands the board a new address on the next renewal.
    """
    if strSTA() == 'RELAY':
        return relay_sta_leg()
    if int(bridge_ip_mode) != 1:
        return ap_iface
    return upstream_eth()


def run_sta(interface):
    country = str(sys.argv[3])
    if checkEUCountry():
        country="EU"
    conf_dir = script_path + "conf/" + country
    conf_file = ""
    os.system("sudo killall -9 wpa_supplicant")

    if int(use_bridge_setup) > 0:
        bridge = '-b br0 '
        print('[*] STA bridge configuration')
        if strSTA() == 'RELAY' :
            os.system('sudo brctl addbr br0; {m}sudo ifconfig wlan1 up; sudo ifconfig wlan1 0.0.0.0; sudo ifconfig wlan0 0.0.0.0; sudo iw {w} set 4addr on; sudo brctl addif br0 {w}; sudo ifconfig br0 up'.format(w=interface, m=bridge_mac_cmd(relay_sta_leg())))
        else :
            eth = upstream_eth()
            os.system('sudo brctl addbr br0; {m}sudo ifconfig {e} up; sudo ifconfig {w} 0.0.0.0; sudo ifconfig {e} 0.0.0.0; sudo iw {w} set 4addr on; sudo brctl addif br0 {w}; sudo brctl addif br0 {e}; sudo ifconfig br0 up'.format(e=eth, w=interface, m=bridge_mac_cmd(eth)))
            os.system('sudo brctl show')
        # bridge air guard, only where it is switched on (default off)
        os.system('sudo sh ' + script_path + 'conf/etc/halow_flood_off.sh apply ' + interface)
    else:
        bridge = ''

    if int(supplicant_debug) == 1:
        debug = '-dddd'
    else:
        debug = ''

    if int(power_save) > 0:
        print("[*] Set default power save timeout for " + interface)
        os.system("sudo iwconfig " + interface + " power timeout " + ps_timeout)

    print("[6] Start wpa_supplicant on " + interface)
    if strSecurity() == "CUSTOM" :
        os.system("sudo wpa_supplicant -i" + interface + " -c " + NRC_PKG_PATH + "/script/wizard_sta.conf " + bridge + debug + " &")
    elif strSecurity() == 'OPEN':
        conf_file = "/sta_halow_open.conf "
    elif strSecurity() == 'WPA2-PSK':
        conf_file = "/sta_halow_wpa2.conf "
    elif strSecurity() == 'WPA3-OWE':
        conf_file = "/sta_halow_owe.conf "
    elif strSecurity() == 'WPA3-SAE':
        conf_file = "/sta_halow_sae.conf "
    elif strSecurity() == 'WPA2-PSK(WPS-PBC)':
        conf_file = "/sta_halow_pbc.conf "

    if conf_file != "":
        # The shipped conf stays pristine: run from a copy, because the
        # country substitution below rewrites it. The Web GUI's SSID and
        # credentials are already in the shipped file (halow_config).
        temp_conf = script_path + "conf/temp_supplicant_config.conf"
        os.system("sudo cp %s %s" % ((conf_dir + conf_file).strip(), temp_conf))
        if country == "EU":
            os.system("sed -i \"s/^country=.*/country=%s/g\" %s" % ( str(sys.argv[3]), temp_conf ) )
        os.system("sudo wpa_supplicant -i" + interface + " -c " + temp_conf + " " + bridge + debug + " &")
        if strSecurity() == 'WPA2-PSK(WPS-PBC)':
            time.sleep(1)
            os.system("sudo wpa_cli wps_pbc")
    time.sleep(3)

    sta_leg = interface
    interface = setup_sta_network(interface)
    # Keep the uplink usable after this script returns: nudge an association
    # that backed off (a relay under a node that was not serving yet has been
    # out for 20 min), and ask for a new address when the network behind the
    # link changed - the client sits on br0, whose carrier never drops.
    watch = script_path + 'conf/etc/relay_uplink_watch.sh'
    if ensure_script_executable(watch):
        os.system('sudo sh %s %s br0 > /dev/null 2>&1 &' % (watch, sta_leg))

    print("[7] Connect and DHCP")
    if not strSecurity() == "CUSTOM":
        ret = check(interface)
        waited = 0
        while ret == '':
            if int(ip_wait) and waited >= int(ip_wait):
                print("No IP after %ss - leaving wpa_supplicant to keep trying"
                      % ip_wait)
                print("HaLow STA started (not yet associated)")
                print("---------------------------------------------------------")
                return
            print("Waiting for IP")
            time.sleep(5)
            waited += 5
            ret = check(interface)

        print(ret)
        # the lease carries dhcpcd's own metric, which loses to a wired port
        nrc_net.prefer_default_route(interface)
    print("IP assigned. HaLow STA ready")
    print("--------------------------------------------------------------------")

def launch_hostapd(interface, orig_hostapd_conf_file, country, debug, channel):
    print("[*] configure file copied from: %s" % (orig_hostapd_conf_file) )
    TEMP_HOSTAPD_CONF = script_path +  "conf/temp_hostapd_config.conf"
    os.system("sudo cp %s %s" % ( orig_hostapd_conf_file,  TEMP_HOSTAPD_CONF ) )
    os.system("sed -i \"4s/.*/interface=%s/g\" %s" % ( interface, TEMP_HOSTAPD_CONF ) )
    if country == "EU":
        os.system("sed -i \"s/^country_code=.*/country_code=%s/g\" %s" % ( str(sys.argv[3]), TEMP_HOSTAPD_CONF ) )
    if channel:
        os.system("sed -i \"s/^channel=.*/channel=%s/g\" %s" % ( channel, TEMP_HOSTAPD_CONF ) )

        # US and JP map ch 1-13 to 2.4 GHz, so hostapd needs hw_mode=g there.
        if country in ("US", "JP") and 1 <= int(channel) and int(channel) <= 13:
            os.system("sed -i \"s/^hw_mode=.*/hw_mode=g/g\" %s" % ( TEMP_HOSTAPD_CONF ) )


    # The shipped conf keeps every channel line commented out, and this build
    # has no ACS: with no channel hostapd exits at once and the AP is silently
    # dead. Say so instead.
    if os.system("grep -q '^channel=' %s" % TEMP_HOSTAPD_CONF) != 0:
        sys.exit("[!] no channel for the AP - set one, or turn the channel "
                 "manager on so it can pick")

    os.system("sudo hostapd %s %s &" % ( TEMP_HOSTAPD_CONF, debug ) )


def run_ap(interface):
    country = str(sys.argv[3])
    if checkEUCountry():
        country="EU"
    conf_path = script_path + "conf/" + country
    conf_file = ""
    channel = None

    if strSecurity() == "CUSTOM" :
        conf_path = script_path
        conf_file = "/wizard_ap.conf"
    elif strSecurity() == "OPEN" :
        conf_file+="/ap_halow_open.conf"
    elif strSecurity() == 'WPA2-PSK' :
        conf_file+="/ap_halow_wpa2.conf"
    elif strSecurity() == 'WPA3-OWE' :
        conf_file+="/ap_halow_owe.conf"
    elif strSecurity() == 'WPA3-SAE' :
        conf_file+="/ap_halow_sae.conf"
    elif strSecurity() == 'WPA2-PSK(WPS-PBC)' :
        conf_file+="/ap_halow_pbc.conf"

    if int(use_bridge_setup) > 0:
        # Remove '#' before wds_sta, bridge in conf file
        os.system("sed -i /wds_sta/,/bridge/s/##*// " + conf_path + conf_file)
    else:
        # Add '#' before wds_sta, bridge in conf file
        os.system("sed -i /wds_sta/,/bridge/'s/^/#/;/wds_sta/,/bridge/s/##*/#/' " + conf_path + conf_file)

    if len(sys.argv) > 4 :
        channel = str(sys.argv[4])
        # 'auto' means "let the channel manager pick", i.e. no fixed channel
        if channel.strip().lower() == 'auto':
            channel = None

    if int(hostapd_debug) == 1:
        debug = '-dddd'
    else:
        debug = ''

    global channel_manager
    if strSTA() == 'RELAY':
        channel_manager = 0
        print("[*] Channel manager is not used in RELAY mode.")

    # explicit channel on the command line = manual operation
    if channel is not None and int(channel_manager) == 1:
        channel_manager = 0
        print("[*] Manual channel " + str(channel) + ": channel manager (auto) ignored")

    if str(sys.argv[3]) == 'K2' and int(channel_manager) > 0:
        channel_manager = 0
        print("[*] Channel manager disabled for K2 (FW-internal regulatory channel control)")

    chmgr_result = ''
    if int(channel_manager) > 0:
        print("[*] Channel manager: " + chmgr_mode_str() +
              " (profile=" + str(channel_manager_profile) + ")")
        if not chmgr_arm():
            sys.exit("[chmgr] arm failed - no usable candidate (check country/prefer_bw)")
        if int(channel_manager) == 1:
            chmgr_result = chmgr_check()
            if chmgr_result != 'Done':
                sys.exit("[chmgr] auto channel selection failed")

    # hostapd deletes a bridge it had to create itself, and this board's
    # address lives on br0 - so br0 must exist before hostapd does.
    if int(use_bridge_setup) > 0:
        # Both halves of a relay reach here, so both name br0 the same way:
        # after the station leg, which is the MAC the AP above knows this
        # board by. Whichever thread creates the bridge, the MAC is on it
        # before any port is - a bridge that exists with no MAC of its own
        # is one dhcpcd can take a lease on under a random one.
        src = bridge_mac_src(interface)
        # not brought up here: existing is all hostapd needs to see, and an
        # up bridge is one dhcpcd may start asking on
        os.system('sudo brctl addbr br0 2>/dev/null; %s'
                  % bridge_mac_cmd(src))

    print("[6] Start hostapd on " + interface)

    if int(channel_manager) == 1 and chmgr_result == 'Done':
        chmgr_conf = script_path + "conf/temp_chmgr.conf"
        os.system("sed -i " + '"4s/.*/interface=' + interface + '/g" ' +
                  chmgr_conf + " ")
        os.system("sudo hostapd " + chmgr_conf + " " + debug + " &")
        if strSecurity() == 'WPA2-PSK(WPS-PBC)':
            time.sleep(1)
            os.system("sudo hostapd_cli wps_pbc")
    else:
        if strSecurity() == 'CUSTOM':
            launch_hostapd( interface, NRC_PKG_PATH + '/script/wizard_ap.conf', country, debug, channel )
        else:
            launch_hostapd( interface, NRC_PKG_PATH + '/script/conf/' + country + conf_file, country, debug, channel )
        if strSecurity() == 'WPA2-PSK(WPS-PBC)':
            time.sleep(1)
            os.system("sudo hostapd_cli wps_pbc")
    if int(use_bridge_setup) > 0:
        # enslavement resets the port flags, so re-arm in the background - the
        # helper waits for the port and does nothing while the guard is off
        os.system('sudo sh ' + script_path + 'conf/etc/halow_flood_off.sh port '
                  + interface + ' >/dev/null 2>&1 &')
    time.sleep(3)

    if int(use_bridge_setup) > 0:
        print('[*] AP bridge configuration')
        if strSTA() == 'RELAY' :
            os.system('sudo brctl addif br0 {w}'.format(w=interface))
        else :
            eth = upstream_eth()
            # Move eth's IP/GW/MAC onto br0 in one quick sequence so an SSH
            # session over eth survives the enslavement. Every bridge mode
            # needs it: deleting eth's address instead left the board with no
            # management address at all. Modes that address br0 themselves
            # take no DHCP client on br0 - they add their address to the one
            # moved here.
            move_script = NRC_PKG_PATH + "/script/conf/etc/bridge_ip_move.sh"
            keep = "" if int(bridge_ip_mode) == 1 else " nodhcp"
            if ensure_script_executable(move_script):
                os.system("sudo " + move_script + " takeover " + eth + " " + interface + keep)
            else:
                print("[!] bridge_ip_move.sh not available - enslaving without moving the address")
                os.system('sudo brctl addbr br0 2>/dev/null; sudo ifconfig {e} up; sudo brctl addif br0 {e}; sudo ifconfig br0 up '.format(e=eth))
        os.system('sudo sh ' + script_path + 'conf/etc/halow_flood_off.sh apply wlan')
        os.system('sudo brctl show')
        nrc_net.dhcpcd_off_bridge_ports()
        time.sleep(3)

    setup_ap_network(interface)

    time.sleep(3)
    if int(channel_manager) > 0 and os.path.exists(script_path + 'chmgrd'):
        chmgrd = script_path + 'chmgrd'
        # a package that shipped it without +x looked healthy: the file was
        # there, the launch failed into /dev/null, and this said "started"
        if not os.access(chmgrd, os.X_OK):
            os.system("sudo chmod 755 " + chmgrd)
        os.system("sudo pkill -9 -x chmgrd 2>/dev/null")
        os.system("sudo " + chmgrd + " -i " + interface + " > /dev/null 2>&1 &")
        time.sleep(0.5)
        if os.system("pgrep -x chmgrd > /dev/null 2>&1") == 0:
            print("[chmgr] chmgrd started")
        else:
            print("[chmgr] chmgrd did NOT start - no channel switching")
    print("[9] ifconfig")
    os.system('sudo ifconfig')
    print("HaLow AP ready")
    print("--------------------------------------------------------------------")

    # Publish live AP info for the Web GUI dashboard (/run/halow/ap.env)
    print("[10] Generating /run/halow/ap.env")
    os.system('sudo python3 %sap_env.py --country %s --iface %s'
              % (script_path, str(sys.argv[3]), interface))

    if relay_routed:
        relay_route_hint()

def run_sniffer():
    print("[6] Setting Monitor Mode")
    time.sleep(3)
    os.system('sudo ifconfig wlan0 down; sudo iw dev wlan0 set type monitor; sudo ifconfig wlan0 up')
    print("[7] Setting Country: " + strOriCountry())
    os.system("sudo iw reg set " + strOriCountry())
    time.sleep(3)
    print("[8] Setting Channel: " + str(sys.argv[4]))
    os.system("sudo iw dev wlan0 set channel " + str(sys.argv[4]))
    time.sleep(3)
    print("[9] Start Sniffer")
    if strSnifferMode() == 'LOCAL':
        os.system('sudo wireshark -i wlan0 -k -S -l &')
    else:
        os.system('lxqt-sudo wireshark -i wlan0 -k -S -l &')
    print("HaLow SNIFFER ready")
    print("--------------------------------------------------------------------")

def apply_kv_args():
    # GUI parity hook: accept the start_modular-style key=value arguments for
    # the script-header options, so the Web GUI configures both start flows
    # the same way. Every key maps onto a global this script already defines;
    # unknown/malformed args stay positional and hit the usage checks below.
    kv_types = {
        'guard_int': str, 'short_bcn_enable': int, 'legacy_ack_enable': int,
        'ndp_ack_1m': int, 'ampdu_enable': int, 'bss_max_idle_enable': int,
        'bss_max_idle': int, 'cca_threshold': int, 'power_save_pretend': int,
        # Automatic channel selection is the channel manager here: it does the
        # boot-time sweep self_config used to do and keeps scoring afterwards,
        # so it replaces self_config/prefer_bw/dwell_time rather than sitting
        # beside them. Two boot-ACS mechanisms writing two different temporary
        # hostapd configs would race for the same channel.
        'channel_manager': int, 'channel_manager_profile': int,
        'channel_manager_prefer_bw': int, 'channel_manager_dwell': int,
        'channel_manager_exclude': str,
        # radio options this tree has and the modular flow does not
        'tpc': int, 'eu_en_300_220': int, 'bw_4m_2m_prim_loc': int,
        'hostapd_debug': int, 'use_eeprom_config': int, 'fw_name': str,
        'bd_name': str, 'bridge_ip_mode': int, 'relay_type': int,
        'relay_nat': int, 'max_cpuclock': int, 'power_save': int,
        'ap_ip': str, 'dhcp_start': str, 'dhcp_end': str,
        'sta_static_ip': str, 'sta_gateway': str, 'eth_iface': str,
        'eth_ip_mode': str,
        'eth_static_ip': str, 'eth_gateway': str, 'eth_dns': str,
        'ps_timeout': str, 'sleep_duration': str, 'listen_interval': int,
        'idle_mode': int, 'cqm_enable': int, 'beacon_bypass_enable': int,
        'sched_scan_enable': int, 'support_ch_width': int, 'ndp_preq': int,
        'peer': str, 'static_ip': str, 'batman': str, 'macaddr': str,
        'twt_int': int, 'twt_num': int, 'twt_sp': int, 'twt_force_sleep': int,
        'twt_num_in_group': int, 'twt_algo': int, 'ip_wait': int,
        # start_modular calls this one use_bridge_setup internally
        'bridge': int,
    }
    from_cli = set()
    positional = [sys.argv[0]]
    for arg in sys.argv[1:]:
        key = arg.split('=', 1)[0]
        if '=' in arg and key in kv_types:
            val_str = arg.split('=', 1)[1]
            name = 'use_bridge_setup' if key == 'bridge' else key
            if name not in globals():
                print("[!] %s not supported by start.py - ignored" % key)
                continue
            try:
                val = kv_types[key](val_str)
                # peer/static_ip/batman default to the integer 0 ("disabled")
                # and the mesh code tests them with "!= 0". A GUI-persisted
                # "0" arrives as the *string* '0', which passes that test and
                # e.g. turns batman mode on with an interface literally named
                # "0" - so map the disable sentinels back to the integer.
                if key in ('peer', 'static_ip', 'batman') and val in ('', '0'):
                    val = 0
                globals()[name] = val
                from_cli.add(name)
                print("[*] %s set to %s" % (name, globals()[name]))
            except ValueError:
                print("[!] Invalid %s value: %s" % (key, val_str))
        else:
            positional.append(arg)
    sys.argv = positional
    return from_cli


def settings_in(from_cli):
    """Take the saved configuration when it is newer than this script.

    The Web GUI writes the header on every save, so this only has work to do
    after something replaced the script - a package update, a restored
    backup. Without it a GUI-configured board would come back up on the
    package defaults. Command-line arguments always win over both.
    """
    if not halow_config.have_config():
        return
    applied = halow_config.sync_in(NRC_PKG_PATH, SCRIPT_PATH)
    if not applied:
        return
    schema = halow_config.load_schema(NRC_PKG_PATH)
    values = halow_config.config_to_vars(halow_config.load_config(), schema)
    for name in applied:
        if name in from_cli or name not in globals():
            continue
        cur, val = globals()[name], values.get(name)
        try:
            globals()[name] = int(val) if isinstance(cur, int) else type(cur)(val)
        except (TypeError, ValueError):
            globals()[name] = val
    print("[*] settings taken from %s: %s"
          % (halow_config.CONFIG_FILE, ", ".join(sorted(applied))))


_settings_snapshot = {}


def snapshot_settings():
    """Header values as the operator left them, before the mode overrides.

    setSnifferParam() and the other per-mode helpers force values for the
    run - a sniffer turns AMPDU, sw_enc and power save off. Recording those
    afterwards would write them into the saved configuration, and the next
    AP start would come up with AMPDU off.
    """
    global _settings_snapshot
    names = set(halow_config.CONF_KEY_TO_VAR.values())
    names.add('use_bridge_setup')
    _settings_snapshot = dict((n, globals()[n]) for n in names if n in globals())


def settings_out():
    """Record what this run used, so the Web GUI shows the same values."""
    if from_webui() or not halow_config.have_config():
        return                      # the GUI path already has these values
    values = dict(_settings_snapshot) if _settings_snapshot else \
        dict((n, globals()[n]) for n in set(halow_config.CONF_KEY_TO_VAR.values())
             if n in globals())
    values['use_bridge_setup'] = values.get('use_bridge_setup', use_bridge_setup)
    pos = {'sta_type': sys.argv[1], 'security': sys.argv[2], 'country': sys.argv[3]}
    if strSTA() in ('AP', 'RELAY') and len(sys.argv) > 4:
        pos['channel'] = sys.argv[4]
    elif strSTA() == 'SNIFFER' and len(sys.argv) > 5:
        pos['channel'], pos['sniffer_mode'] = sys.argv[4], sys.argv[5]
    elif strSTA() == 'MESH' and len(sys.argv) > 4:
        pos['mesh_mode'] = sys.argv[4]
    network = {}
    role = {'AP': 'ap', 'RELAY': 'ap', 'STA': 'sta', 'MESH': 'mesh'}.get(strSTA())
    if role and strSecurity() != 'CUSTOM':
        path = halow_config.conf_path(NRC_PKG_PATH, str(sys.argv[3]), role,
                                      int(sys.argv[2]))
        if path:
            ssid, pw, bi = halow_config.read_network(path)
            network = {'ssid': ssid, 'password': pw}
            # a station has no beacon of its own, so only the AP side reports it
            if bi and role == 'ap':
                network['beacon_int'] = bi
        # a relay reads its AP leg above; the uplink it joins lives in the
        # station conf, and the GUI keeps that under its own two keys
        if strSTA() == 'RELAY':
            up = halow_config.conf_path(NRC_PKG_PATH, str(sys.argv[3]), 'sta',
                                        int(sys.argv[2]))
            if up:
                u_ssid, u_pw, _ = halow_config.read_network(up)
                # same SSID on both legs is the default, and the GUI shows it
                # by leaving the uplink fields empty - do not fill them in
                if u_ssid and u_ssid != network.get('ssid'):
                    network['uplink_ssid'] = u_ssid
                if u_pw and u_pw != network.get('password'):
                    network['uplink_password'] = u_pw
    changed = halow_config.sync_out(NRC_PKG_PATH, values, pos, network)
    if changed:
        print("[*] %s updated: %s" % (halow_config.CONFIG_FILE, ", ".join(changed)))


def from_webui():
    """True when systemd started us, i.e. the Web GUI path.

    halow-wlan.service runs start-wlan.sh, which runs this script, and
    systemd sets INVOCATION_ID for a unit's processes. A manual run has no
    such variable. Two things depend on the difference: a manual run may
    have to stop a live GUI stack first, and only a manual run records what
    it used back into the saved configuration.
    """
    return bool(os.environ.get("INVOCATION_ID"))


def check_halow_stack():
    """Hand the radio over before a manual run touches it.

    halow.target owns the driver and hostapd through halow-wlan.service.
    Doing nothing would leave two owners of one interface, and the hostapd
    supervisor would respawn hostapd with the Web GUI's conf 3 s after we
    kill it. Only on a manual run - from_webui() keeps us from tearing down
    the very stack that started us.
    """
    # systemd itself invoked us (start-wlan.sh runs start.py as part of
    # halow-wlan.service), so the target being up is expected, not a clash.
    if from_webui():
        return
    if os.system("systemctl is-active --quiet halow.target 2>/dev/null") != 0:
        return
    # Manual run under a live stack: two owners of one interface, and
    # halow-hostapd would respawn hostapd 3 s after we kill it. Take the
    # stack down rather than fail - stopping the target also stops the
    # hostapd supervisor, which a plain 'killall hostapd' cannot do.
    print("[*] halow.target is active (Web GUI stack) - stopping it first")
    os.system("sudo systemctl stop halow.target")
    time.sleep(2)


if __name__ == '__main__':
    check_halow_stack()
    cli_keys = apply_kv_args()
    settings_in(cli_keys)
    snapshot_settings()     # before any per-mode override touches the globals
    if len(sys.argv) < 4 or not isNumber(sys.argv[1]) or not isNumber(sys.argv[2]):
        usage_print()
    elif strSTA() == 'SNIFFER' and len(sys.argv) < 6:
        usage_print()
    elif strSTA() == 'MESH':
        checkMeshUsage()
    else:
        argv_print()

    checkParamValidity()
    checkCountry()

    print("NRC " + strSTA() + " setting for HaLow...")

    run_common()

    if strSTA() == 'STA':
        run_sta('wlan0')
    elif strSTA() == 'AP':
        run_ap('wlan0')
    elif strSTA() == 'SNIFFER':
        run_sniffer()
    elif strSTA() == 'RELAY':
        #start STA and then AP for Relay
        if int(relay_type) == 0:
            t = threading.Thread(target=run_sta, args=('wlan0',))
            t.start()
            run_ap('wlan1')
        else:
            addWLANInterface('wlan1')
            t = threading.Thread(target=run_sta, args=('wlan1',))
            t.start()
            run_ap('wlan0')
    elif strSTA() == 'MESH':
        if strMeshMode() == 'Mesh Portal':
            run_mpp('wlan0', str(sys.argv[3]), strSecurity(), supplicant_debug, peer, static_ip, batman, mesh_net())
        elif strMeshMode() == 'Mesh Point':
            run_mp('wlan0', str(sys.argv[3]), strSecurity(), supplicant_debug, peer, static_ip, batman, mesh_net())
        elif strMeshMode() == 'Mesh AP':
            run_map('wlan0', 'mesh0', str(sys.argv[3]), strSecurity(), supplicant_debug, peer, static_ip, batman, mesh_net())
        else:
            usage_print()
    else:
        usage_print()

    nrc_net.dhcpcd_off_bridge_ports()   # vifs that appeared during bring-up
    settings_out()

print("Done.")
