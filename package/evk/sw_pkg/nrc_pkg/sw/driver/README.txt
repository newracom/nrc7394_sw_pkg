Host driver binaries for the HaLow stack.

Layout (the start scripts look here):

  nrc_modular/   modular driver set used by start_modular.py - the
                 Unified AP (MCP capable) and every non-AP mode:
                   nrc_spi.ko   SPI backend
                   nrc_core.ko  HAL core
                   nrc_wlan.ko  WLAN frontend
                   nrc-mcp.ko   MCP frontend (ESL)

  nrc/           monolithic driver used by start.py - the plain AP mode:
                   nrc.ko

Both subdirectories are optional: this directory itself is still accepted
as a fallback location for the modular set (older package layouts).

Modules are kernel specific - a .ko only loads on the kernel release it
was built against (see `modinfo <file> | grep vermagic`).

NOTE: the two flavours request opposite SPI IRQ polarity (monolithic
IRQF_TRIGGER_LOW, modular IRQF_TRIGGER_HIGH). On boards whose irqchip
latches the trigger type - the RP1 on a Raspberry Pi 5 does - the second
flavour cannot map the interrupt after the first one has used it
("type mismatch, failed to map hwirq-N", request_irq() fails). Reboot the
board when switching between the plain AP and the Unified AP mode.

SDK-v1.3.0
built on Linux kernel 4.14.70
