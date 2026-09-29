# Licensing

This document explains the licensing of the NRC7394 Software Package, component
by component. It is intended for open source compliance review.

The short version is in [LICENSE](LICENSE). Full license texts are in
[LICENSES/](LICENSES/). Files that cannot carry a comment header are declared in
[REUSE.toml](REUSE.toml).

This package follows the [REUSE Specification 3.3](https://reuse.software).
Running `reuse lint` verifies that every file in the repository carries
licensing information.

---

## 1. Component map

| Component | Path | License | Source available |
| --- | --- | --- | --- |
| Host driver | `package/src/nrc/` | ISC | Yes, in this repository |
| HSPI helper | `package/src/nrc_hspi_simple/` | ISC | Yes, in this repository |
| CLI application | `package/src/cli_app/` | MIT | Yes, in this repository |
| Channel manager daemon | `package/src/chmgrd/` | ISC | Yes, in this repository |
| FT232H USB-SPI driver | `package/src/ft232h-usb-spi/` | GPL-2.0-only | Yes, in this repository |
| CH347 USB-SPI driver | `package/src/ch347-usb-spi/` | GPL-2.0-or-later | Yes, in this repository |
| Firmware images | `**/*.bin` | LicenseRef-NewracomFirmware, plus the third-party licenses listed in 3.1 | No — binary only |
| Board data | `**/*.dat` | LicenseRef-NewracomFirmware | No — binary only |
| Prebuilt `nrc.ko` | `package/evk/**/nrc.ko` | ISC | Yes — built from `package/src/nrc/` |
| Prebuilt `cli_app` | `package/evk/**/cli_app` | MIT | Yes — built from `package/src/cli_app/` |
| Prebuilt `chmgrd` | `package/evk/**/chmgrd` | ISC | Yes — built from `package/src/chmgrd/` |
| Scripts and configuration | `**/*.py`, `**/*.sh`, `**/*.conf` | ISC | Yes, in this repository |
| Device tree sources | `**/*.dts` | ISC | Yes, in this repository |
| Documentation | `**/*.pdf` | LicenseRef-NewracomDoc | Not applicable |
| Bundled Wireshark packages | `.../sniffer/**/*.deb` | GPL-2.0-or-later | Yes — see section 4 |

### Not included in this package

`hostapd` and `wpa_supplicant` are **not distributed** in this repository. The
setup scripts install them from the host distribution's package repository.
Their licensing is therefore governed by that distribution, not by this
package.

---

## 2. Third-party components

### FT232H USB-SPI driver — `package/src/ft232h-usb-spi/`

Original work by Anatolij Gustschin `<agust@denx.de>`, DENX Software
Engineering, 2017-2018. The file headers record that the original file name was
`spi_ftdi_mpsse.c`. Upstream: the "spi: add FTDI MPSSE SPI controller driver"
patch series on the Linux kernel mailing list, v4, February 2019
(<https://lkml.iu.edu/1902.2/05398.html>). Licensed under GPL-2.0-only.

Modified by Newracom, Inc. to support the NRC7394 device. Modifications are
provided under the same license. The complete modified source is in this
repository.

### CH347 USB-SPI driver — `package/src/ch347-usb-spi/`

Original work by Nanjing Qinheng Microelectronics Co., Ltd. (WCH),
`<tech@wch.cn>`, 2023, <http://wch.cn>. Upstream: the vendor's public repository
<https://github.com/WCHSoftGroup/ch34x_mphsi_master_linux> (this copy corresponds
to the v1.0, 2023.03 revision). Licensed under GPL-2.0-or-later.

Modified by Newracom, Inc. to support the NRC7394 device. Modifications are
provided under the same license. The complete modified source is in this
repository.

---

## 3. Firmware and board data

The firmware images (`nrc7394_cspi.bin`, `nrc7394_cspi_eeprom.bin`) execute on a
processor inside the NRC7394 device. They are loaded by the host driver but are
not linked into, and are not a derivative work of, the host kernel. They are
distributed in binary form under
[LicenseRef-NewracomFirmware](LICENSES/LicenseRef-NewracomFirmware.txt).

In plain terms, and without limiting that license:

- You may ship the firmware inside a product that contains a Newracom device,
  and your customers may ship it onward.
- You may not modify it, use it with non-Newracom silicon, or reverse engineer
  it.
- Module partners who have received the Board Data Tool under a separate
  written agreement may generate board data for their own module designs. Board
  data determines transmit power and regulatory behaviour; responsibility for
  the regulatory compliance of a product built with modified board data rests
  with the party that generated it.
- No firmware source code is provided.

Any third-party components compiled into the firmware retain their own licenses.
Where such components are present they are listed in the Software Bill of
Materials published in [sbom/](sbom/).

### 3.1 Third-party components inside the firmware

The firmware images are statically linked. The following third-party components
are compiled into them. Their license texts are in [LICENSES/](LICENSES/).

| Component | Version | License | Notes |
| --- | --- | --- | --- |
| FreeRTOS Kernel | 10.0.0 | MIT | Modified by Newracom |
| newlib / libgloss | ARM GNU Toolchain 10.3-2021.10 | BSD-style collection (`LicenseRef-Newlib`) | C runtime |
| libgcc | 10.3.1 | GPL-3.0-or-later WITH GCC-exception-3.1 | Compiler runtime support |
| Arm CMSIS Core | 5.x | Apache-2.0 | Cortex-M core access headers, inlined |
| mpaland printf | vendored | MIT | Embedded printf implementation |
| `dl_list` (from hostapd) | vendored header | BSD-3-Clause | Doubly-linked list macros |

No cryptographic library is linked into the firmware. Ciphering and integrity
checking are performed by the hardware security engine of the MAC; the firmware
only manages hardware key slots.

The firmware performs no outbound network communication of its own. It has no
TCP/IP stack, contains no server address, URL or domain string, and transmits
only what the host driver instructs it to transmit. It collects no telemetry and
sends no crash reports.

---

## 4. Bundled Wireshark packages

The `.deb` packages under
`package/evk/sw_pkg/nrc_pkg/script/sniffer/` are builds of Wireshark, licensed
under **GPL-2.0-or-later**. They are provided as a convenience for HaLow packet
capture on the evaluation platform.

The complete corresponding source code for these binaries is published on the
same release page as this package. See the README in the sniffer directory for
the exact location.

If you redistribute a product that includes these binaries, the GPL obligations
apply to your distribution as well.

---

## 5. History of this document

Releases up to and including **v1.3.2** were published with a single GPL-2.0
license file at the repository root, and no per-directory or per-file license
declarations other than the notices inside the source files themselves.

Those in-file notices have been unchanged since **v1.0 (2023-04-05)**: the host
driver sources have carried the ISC notice, the CLI application sources have
carried the MIT notice, and the kernel module has declared `Dual BSD/GPL`
throughout. The root license file did not reflect that composition.

This document and the accompanying `LICENSES/` directory and `REUSE.toml`
correct that. They state the licensing that the per-file notices have always
expressed, and extend it to the files that previously carried no notice at all,
such as firmware images and documentation.

**Previous releases are not affected.** Software received under an earlier
release remains governed by the terms under which it was received at that time.
This document applies from **v1.3.3** onward.

---

## 6. Verifying compliance

```sh
pipx install reuse
reuse lint
```

A compliant result reports that every file has both copyright and licensing
information. A Software Bill of Materials in CycloneDX and SPDX formats is
published in [sbom/](sbom/) with each release.

---

## 7. Contact

Questions about the licensing of this package may be raised through the issue
tracker of this repository.
