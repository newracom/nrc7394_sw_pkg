#!/bin/bash
#

#
# 0 : use static ip address
# 1 : use dhcp client
# 2 : use dhcp server
#
BRIDGE_IP_MODE=1

#
# BRIDGE_IP_MODE=0
#
BRIDGE_STATIC_IPADDR="192.168.200.100/24"
BRIDGE_STATIC_GATEWAY="192.168.200.1"

#
# BRIDGE_IP_MODE=2
#
BRIDGE_DHCPS_IPADDR="192.168.200.1/24"
BRIDGE_DHCPS_RANGE="192.168.200.10,192.168.200.50,255.255.255.0,24h"

#########################################################################################################

DHCPCD_CONF=/etc/dhcpcd.conf
DNSMASQ_CONF=/etc/dnsmasq.conf

usage ()
{
	echo "Usage: ./ip_config_bridge.sh {AP|STA|RELAY} <eth_id> [<ip_mode>]    # <ip_mode> : 0=static, 1=dhcpc, 2=dhcps"
	exit 1
}

if [[ $EUID -ne 0 ]]; then
	echo "This script must be run as root" 
	exit 1
fi

if [ $# -eq 3 ]; then
	case $3 in
		0|1|2)
			BRIDGE_IP_MODE=$3
			;;

		*)
			usage	
	esac
fi

case $1 in
	AP|STA)
		if [ $2 = 0 ]; then
			# delete eth0/wlan0/wlan1
			sed -i '58,$d' $DHCPCD_CONF
			sed -i '1,$d' $DNSMASQ_CONF
		else
			# delete wlan0/wlan1
			sed -i '58,68d' $DHCPCD_CONF
			sed -i '3,$d' $DNSMASQ_CONF
		fi
	
		DENY_IFACES="eth$2 wlan0"
		;;

	RELAY)
		# delete wlan0/wlan1
		sed -i '58,68d'  $DHCPCD_CONF

		if [ "$(sed -n '1p' $DNSMASQ_CONF)" = "interface=eth0" ]; then
			sed -i '3,$d' $DNSMASQ_CONF
		else
			sed -i '1,$d' $DNSMASQ_CONF
		fi
		
		DENY_IFACES="wlan0 wlan1"
		;;

	*)
		usage
esac

sed -i'' -r -e "/# Example static IP configuration:/i\# Prevent dhcp assignment to the following interfaces." $DHCPCD_CONF
sed -i'' -r -e "/# Example static IP configuration:/i\denyinterfaces $DENY_IFACES" $DHCPCD_CONF
sed -i'' -r -e "/# Example static IP configuration:/i\ " $DHCPCD_CONF

echo "" >> $DHCPCD_CONF 
echo "# br0" >> $DHCPCD_CONF 
#echo "denyinterfaces $DENY_IFACES" >> $DHCPCD_CONF
echo "interface br0" >> $DHCPCD_CONF
echo "#metric 100" >> $DHCPCD_CONF

case $BRIDGE_IP_MODE in
	0) # static
		echo "static ip_address=$BRIDGE_STATIC_IPADDR" >> $DHCPCD_CONF
		echo "static routers=$BRIDGE_STATIC_GATEWAY" >> $DHCPCD_CONF
		;;
	
	1) # dhcpc
		;;

	2) # dhcps
		echo "static ip_address=$BRIDGE_DHCPS_IPADDR" >> $DHCPCD_CONF
		
		echo "interface=br0" >> $DNSMASQ_CONF
		echo "dhcp-range=$BRIDGE_DHCPS_RANGE" >> $DNSMASQ_CONF
		;;
esac

ifconfig br0 0.0.0.0
