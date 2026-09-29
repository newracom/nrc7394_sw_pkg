#!/bin/sh
#

SPI_BUS_NUM=3

while getopts "s:g:h" option; do
case $option in
	s) 
		SPI_BUS_NUM=$OPTARG
		;;

	g) 
		GPIO_BASE_NUM=$OPTARG
		;;

	h)
		echo "Usage: $0 [options]"
		echo "Options:"
		echo "  -s     set the SPI bus number (default : $SPI_BUS_NUM)"
		echo "  -h     print this help"
		exit
		;;

	\?) 
		echo "Invalid Option"
		exit
esac
done

MODULE_PARAMS="spi_bus_num=$SPI_BUS_NUM"

echo "sudo insmod ./drivers/spi/spi-ch347/spi-ch347.ko $MODULE_PARAMS"
sudo insmod ./drivers/spi/spi-ch347/spi-ch347.ko $MODULE_PARAMS

ls /sys/class/gpio
ls /sys/class/spi_master
