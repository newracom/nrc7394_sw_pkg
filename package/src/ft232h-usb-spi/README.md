# FT232H USB-SPI bridge driver

Linux kernel driver that exposes an FTDI FT232H in MPSSE mode as an SPI master,
used to connect an NRC7394 device to a host over USB.

## Origin

Original work by Anatolij Gustschin `<agust@denx.de>`, DENX Software
Engineering, 2017-2018. The file headers record that the original file name was
`spi_ftdi_mpsse.c`.

Upstream: the "spi: add FTDI MPSSE SPI controller driver" patch series posted
to the Linux kernel mailing list by the author, v4, February 2019:
<https://lkml.iu.edu/1902.2/05398.html>. The copyright and licence headers are
preserved exactly as received, and the complete source as distributed by
Newracom - original and modifications - is in this directory.

## License

GPL-2.0-only. The full text is in [LICENSE](LICENSE) and in the repository's
`LICENSES/GPL-2.0-only.txt`.

## Modifications

Modified by Newracom, Inc. to support the NRC7394 device. Modifications are
provided under the same license. The complete modified source is in this
directory.
