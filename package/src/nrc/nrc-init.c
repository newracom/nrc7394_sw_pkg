/*
 * Copyright (c) 2016-2019 Newracom, Inc.
 *
 * Permission to use, copy, modify, and/or distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <linux/kernel.h>
#include <linux/slab.h>
#include <linux/module.h>
#include <linux/device.h>
#include <linux/platform_device.h>
#include <linux/gpio.h>
#include "nrc.h"
#include "nrc-init.h"
#include "nrc-hif.h"
#include "nrc-debug.h"
#include "nrc-mac80211.h"
#include "nrc-fw.h"
#include "nrc-netlink.h"
#include "nrc-stats.h"
#include "wim.h"
#include "nrc-vendor.h"
#if defined(CONFIG_SUPPORT_BD)
#include <linux/kernel.h>
#include <linux/fs.h>
#include <asm/uaccess.h>
#include "nrc-bd.h"
#endif /* defined(CONFIG_SUPPORT_BD) */
#include "nrc-twt-sched.h"

char *fw_name;
module_param(fw_name, charp, 0444);
MODULE_PARM_DESC(fw_name, "Firmware file name");

#if defined(CONFIG_SUPPORT_BD)
char *bd_name ="bd.dat";
module_param(bd_name, charp, 0600);
MODULE_PARM_DESC(bd_name, "Board Data file name");
#endif /* defined(CONFIG_SUPPORT_BD) */

/**
 * default port name
 */
#if defined(CONFIG_ARM)
#if defined(CONFIG_NRC_HIF_UART)
char *hifport = "/dev/ttyAMA0";
#else
char *hifport = "/dev/ttyUSB0";
#endif
#else
char *hifport = "/dev/ttyUSB0";
#endif
module_param(hifport, charp, 0600);
MODULE_PARM_DESC(hifport, "HIF port device name");

/**
 * default port speed
 */
#if defined(CONFIG_NRC_HIF_CSPI)
int hifspeed = (20*1000*1000);
#elif defined(CONFIG_NRC_HIF_SSP)
int hifspeed = (1300*1000);
#elif defined(CONFIG_NRC_HIF_UART)
int hifspeed = 115200;
#else
int hifspeed = 115200;
#endif
module_param(hifspeed, int, 0600);
MODULE_PARM_DESC(hifspeed, "HIF port speed");

int spi_bus_num;
module_param(spi_bus_num, int, 0600);
MODULE_PARM_DESC(spi_bus_num, "SPI controller bus number");

int spi_cs_num;
module_param(spi_cs_num, int, 0600);
MODULE_PARM_DESC(spi_cs_num, "SPI chip select number");

int spi_gpio_irq = -1;
module_param(spi_gpio_irq, int, 0600);
MODULE_PARM_DESC(spi_gpio_irq, "SPI gpio irq");

int spi_polling_interval = 0;
module_param(spi_polling_interval, int, 0600);
MODULE_PARM_DESC(spi_polling_interval, "SPI polling interval (msec)");

int spi_gdma_irq = 6;
module_param(spi_gdma_irq, int, 0600);
MODULE_PARM_DESC(spi_gdma_irq, "SPI gdma irq");

bool loopback;
module_param(loopback, bool, 0600);
MODULE_PARM_DESC(loopback, "HIF loopback");

int lb_count = 1;
module_param(lb_count, int, 0600);
MODULE_PARM_DESC(lb_count, "HIF loopback Buffer count");

int disable_cqm = 0;
module_param(disable_cqm, int, 0600);
MODULE_PARM_DESC(disable_cqm, "Disable CQM (0: enable, 1: disable)");

int disable_cqm_on_scan = 0;
module_param(disable_cqm_on_scan, int, 0600);
MODULE_PARM_DESC(disable_cqm_on_scan, "Disable CQM on scan (0: enable, 1: disable)");

int listen_interval = 100;
module_param(listen_interval, int, 0600);
MODULE_PARM_DESC(listen_interval, "Listen Interval");

/**
 * bss_max_idle (in unit of 1000TUs(1024ms)
 */
int bss_max_idle;
module_param(bss_max_idle, int, 0600);
MODULE_PARM_DESC(bss_max_idle, "BSS Max Idle");

/**
 * enable/disable the s1g short beacon
 */
bool enable_short_bi = 1;
module_param(enable_short_bi, bool, 0600);
MODULE_PARM_DESC(enable_short_bi, "Enable Short Beacon Interval");

#if defined(CONFIG_SUPPORT_LEGACY_ACK)
/* enable/disable the legacy ack mode */
bool enable_legacy_ack = false;
module_param(enable_legacy_ack, bool, 0600);
MODULE_PARM_DESC(enable_legacy_ack, "Enable Legacy ACK mode");
#endif /* CONFIG_SUPPORT_LEGACY_ACK */

#if defined(CONFIG_SUPPORT_BEACON_BYPASS)
/* enable/disable beacon bypass */
bool enable_beacon_bypass = false;
module_param(enable_beacon_bypass, bool, 0600);
MODULE_PARM_DESC(enable_beacon_bypass, "Enable Beacon Bypass");
#endif /* CONFIG_SUPPORT_BEACON_BYPASS */

#if defined(CONFIG_SUPPORT_AUTH_CONTROL)
/* set enable/disable and authentication control parameters*/
int set_auth_control[5] = {0, 0, 0, 0,0};
module_param_array(set_auth_control, int, NULL, 0600);
MODULE_PARM_DESC(set_auth_control, "Set auth control (0|1(off,on), slot, ti_min, ti_max, scale");
#endif /* CONFIG_SUPPORT_AUTH_CONTROL */

bool enable_monitor;
module_param(enable_monitor, bool, 0600);
MODULE_PARM_DESC(enable_monitor, "Enable Monitor");

/**
 * milisecond unit
 * to resolve QoS null frame variable-interval issue
 */
int bss_max_idle_offset;
module_param(bss_max_idle_offset, int, 0600);
MODULE_PARM_DESC(bss_max_idle_offset, "BSS Max Idle Offset");

/**
 * override  mac-address on nrc driver, follow below format
 * xx:xx:xx:xx:xx:xx
 */
static char *macaddr = ":";
module_param(macaddr, charp, 0);
MODULE_PARM_DESC(macaddr, "MAC Address");

/**
 * enable/disable the power save mode by default
 */
int power_save = NRC_PS_NONE;;
module_param(power_save, int, 0600);
MODULE_PARM_DESC(power_save, "power save");

/**
 * enable/disable the idle mode by default
 */
bool idle_mode = false;;
module_param(idle_mode, bool, 0600);
MODULE_PARM_DESC(idle_mode, "idle mode");

/**
 * deepsleep duration of non-TIM mode power save
 */
int sleep_duration[2] = {0,};
module_param_array(sleep_duration, int, NULL, 0600);
MODULE_PARM_DESC(sleep_duration, "deepsleep duration of non-TIM mode power save");

/**
 * wlantest mode
 */
bool wlantest = false;
module_param(wlantest, bool, 0600);
MODULE_PARM_DESC(wlantest, "wlantest");

/**
 * Set NDP Probe Request mode
 */
bool ndp_preq = false;
module_param(ndp_preq, bool, 0600);
MODULE_PARM_DESC(ndp_preq, "Enable NDP Probe Request");

/**
 * Set 1M NDP ACK
 */
bool ndp_ack_1m = false;
module_param(ndp_ack_1m, bool, 0600);
MODULE_PARM_DESC(ndp_ack_1m, "Enable 1M NDP ACK");

/**
 * Enable HSPI init
 */
bool enable_hspi_init = false;
module_param(enable_hspi_init, bool, S_IRUSR | S_IWUSR);
MODULE_PARM_DESC(enable_hspi_init, "Enable HSPI Initialization");

/**
 * Enable handling null func (ps-poll) on mac80211
 */
bool nullfunc_enable = false;
module_param(nullfunc_enable, bool, S_IRUSR | S_IWUSR);
MODULE_PARM_DESC(nullfunc_enable, "Enable null func on mac80211");

/**
 * Set up AMPDU mode (0: AMPDU disabled 1: AMPDU enabled (manual) 2: AMPDU enabled (auto))
  manual: manually enable aggregation via CLI
  auto : automatically enable aggregation via ba session when first QoS data is transmitted after connection
 */
int ampdu_mode = NRC_AMPDU_AUTO;
module_param(ampdu_mode, int, 0600);
MODULE_PARM_DESC(ampdu_mode, "Set AMPDU mode");

/**
 * Encryption/Decryption type for PTK/GTK (0: HW, 1: SW, 2: HW PTK, SW GTK)
 */
int sw_enc = 0;
module_param(sw_enc, int, S_IRUSR | S_IWUSR);
MODULE_PARM_DESC(sw_enc, "Use SW Encryption instead of HW Encryption");

/**
 * Set Singal Monitor  mode
 */
bool signal_monitor = false;
module_param(signal_monitor, bool, 0600);
MODULE_PARM_DESC(signal_monitor, "Enable SIGNAL(RSSI/SNR) Monitor");

/**
 * Set configuration of KR Band
 */
int kr_band = -1;
module_param(kr_band, int, 0600);
MODULE_PARM_DESC(kr_band, "Specify KR band (KR USN1(1) or KR USN5(2))");

/**
 * Set configuration of SG Band
 */
int sg_band = -1;
module_param(sg_band, int, 0600);
MODULE_PARM_DESC(sg_band, "Specify SG band (866-869 MHz(8) or 920-925 MHz(9))");

/**
 * Set configuration of TW Band
 */
int tw_band = -1;
module_param(tw_band, int, 0600);
MODULE_PARM_DESC(tw_band, "Specify TW band (8xxMHz(8) or 9xxMHz(9))");

/**
 * Debug Level All
 */
bool debug_level_all = false;
module_param(debug_level_all, bool, 0600);
MODULE_PARM_DESC(debug_level_all, "Driver debug level all");

/**
 * discard TX deauth frame for Mult-STA test
 */
bool discard_deauth = false;
module_param(discard_deauth , bool, 0600);
MODULE_PARM_DESC(discard_deauth , "(Test only) discard TX deauth for Multi-STA test");

/**
 * Debug flow control (print TRX slot and credit status)
 */
bool dbg_flow_control = false;
module_param(dbg_flow_control, bool, 0600);
MODULE_PARM_DESC(dbg_flow_control, "Print slot and credit status");

/**
 * Use bitmap encoding for NDP (block) ack
 */
bool bitmap_encoding = true;
module_param(bitmap_encoding , bool, 0600);
MODULE_PARM_DESC(bitmap_encoding , "(NRC7292 only) Use bitmap encoding for block ack");

/**
 * Apply scrambler reversely
 */
bool reverse_scrambler = true;
module_param(reverse_scrambler , bool, 0600);
MODULE_PARM_DESC(reverse_scrambler , "(NRC7292 only) Apply scrambler reversely");

#if defined(CONFIG_S1G_CHANNEL)
char * nrc_country_code="!";
module_param(nrc_country_code, charp, 0444);
MODULE_PARM_DESC(nrc_country_code, "Two letter fw country code");
#endif /* defined(CONFIG_S1G_CHANNEL) */

/**
 * gpio for power save (default Host_output(GP20) --> Target_input(GP11))
 */
int power_save_gpio[3] = {HOST_GPIO_FOR_TARGET_WAKEUP, TARGET_GPIO_FOR_WAKEUP, TARGET_WAKEUP_ACTIVE_HIGH};
module_param_array(power_save_gpio, int, NULL, 0600);
MODULE_PARM_DESC(power_save_gpio, "gpio for power save");

/**
 * Maximum beacon loss count
 */
int beacon_loss_count = 20;
module_param(beacon_loss_count, int, 0600);
MODULE_PARM_DESC(beacon_loss_count, "Number of beacon intervals before we decide beacon was lost");

/**
 * Ignore listen interval value while comparing it with bss max idle on AP
 */
bool ignore_listen_interval = false;
module_param(ignore_listen_interval, bool, 0600);
MODULE_PARM_DESC(ignore_listen_interval, "Ignore listen interval value while comparing it with bss max idle on AP");

/**
 * Supported CH width (0 (1/2Mhz)  or 1 (1/2/4MHz))
 */
int support_ch_width = 1;
module_param(support_ch_width, int, 0600);
MODULE_PARM_DESC(support_ch_width, "Supported CH width (0:1/2MHz Support, 1:1/2/4Mhz Support");

/**
 * Set rate control mode
 */
uint8_t ap_rc_mode = 0xff;
//module_param(ap_rc_mode, byte, 0600);
//MODULE_PARM_DESC(ap_rc_mode, "AP Rate control mode (1:Disable,Use default_mcs, 2:Individual RC for each STA. 3:RC incorporating RX MCS");

uint8_t sta_rc_mode = 0xff;
//module_param(sta_rc_mode, byte, 0600);
//MODULE_PARM_DESC(sta_rc_mode, "STA Rate control mode (1:Disable,Use default_mcs, 2:Individual RC for each STA. 3:RC incorporating RX MCS");

/**
 * Set default mcs
 */
uint8_t ap_rc_default_mcs = 0xff;
//module_param(ap_rc_default_mcs, byte, 0600);
//MODULE_PARM_DESC(ap_rc_default_mcs, "AP Default MCS");

uint8_t sta_rc_default_mcs = 0xff;
//module_param(sta_rc_default_mcs, byte, 0600);
//MODULE_PARM_DESC(sta_rc_default_mcs, "STA Default MCS");

/**
 * Power save pretend value for no response STA
 */
bool ps_pretend = false;
module_param(ps_pretend, bool, 0600);
MODULE_PARM_DESC(ps_pretend, "Power save pretend for no response STA");

/**
 * set duty cycle (default: off)
 */
int set_duty_cycle[3] = {0, 0, 0};
module_param_array(set_duty_cycle, int, NULL, 0600);
MODULE_PARM_DESC(set_duty_cycle, "Set duty cycle (0|1(off,on), window, duration");

/**
 * set cca threshold (default: -75)
 */
int set_cca_threshold = -75;
module_param(set_cca_threshold, int, 0600);
MODULE_PARM_DESC(set_cca_threshold, "Set cca threshold (default: -75)");

/**
 * TWT 
 */

uint twt_num;
module_param(twt_num, uint, 0600);
MODULE_PARM_DESC(twt_num, "Total TWT service number (default: 0, disabled)");

u64 twt_sp;
module_param(twt_sp, ullong, 0600);
MODULE_PARM_DESC(twt_sp, "TWT service period (default: 0, usec)");

u64 twt_int;
module_param(twt_int, ullong, 0600);
MODULE_PARM_DESC(twt_int, "TWT Wake Interval (default: 0, usec)");

bool twt_service;
module_param(twt_service, bool, S_IRUSR);
MODULE_PARM_DESC(twt_service, "Indicate TWT service on (only STA)");

bool twt_force_sleep;
module_param(twt_force_sleep, bool, S_IRUSR | S_IWUSR);
MODULE_PARM_DESC(twt_force_sleep, "Force sleep at the end of service (only STA)");

uint twt_num_in_group;
module_param(twt_num_in_group, uint, S_IRUSR | S_IWUSR);
MODULE_PARM_DESC(twt_num_in_group, "TWT Max STA Number in a Group (only AP)");

uint8_t twt_algo;
module_param(twt_algo, byte, S_IRUSR | S_IWUSR);
MODULE_PARM_DESC(twt_algo, "TWT scheduling algorithm (only AP, 0:Balanced, 1:FCFS)");

static bool has_macaddr_param(uint8_t *dev_mac)
{
	int res;

	if (macaddr[0] == ':')
		return false;

	res = sscanf(macaddr, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx",
				&dev_mac[0], &dev_mac[1], &dev_mac[2],
				&dev_mac[3], &dev_mac[4], &dev_mac[5]);

	return (res == 6);
}

/****************************************************************************
 * FunctionName : nrc_set_macaddr_from_fw
 * Description : This function set MAC Addresses from Serial Flash.
 *	Case1: macaddr0, macaddr1 are both written in Serial Flash
 *	VIF0 = macaddr0 / VIF1 = macaddr1
 *
 *	Case2: only macaddr0 is written in Serial Flash
 *	VIF0 = macaddr0 / VIF1 = macaddr0 with private bit
 *
 *	Case3: only macaddr1 is written in Serial Flash
 *	VIF0 = macaddr1 with private bit / VIF1 = macaddr1
 *
 *	Case4: macaddr0,macaddr1 are both not written in Serial Flash
 *	VIF0/VIF1 = generated macaddr by Host(RP)
 *
 * Returns : NONE
 *****************************************************************************/
static void nrc_set_macaddr_from_fw(struct nrc *nw, struct wim_ready *ready)
{
	int i;

	for (i = 0; i < NR_NRC_VIF; i++)
		nw->has_macaddr[i] = true;

	if (ready->v.has_macaddr[0] && ready->v.has_macaddr[1]) {
		for (i = 0; i < NR_NRC_VIF; i++) {
			memcpy(&nw->mac_addr[i].addr[0],
					&ready->v.macaddr[i][0], ETH_ALEN);
		}
	} else if (ready->v.has_macaddr[0] && !ready->v.has_macaddr[1]) {
		memcpy(&nw->mac_addr[0].addr[0],
				&ready->v.macaddr[0][0], ETH_ALEN);
		if (!(nw->mac_addr[0].addr[0]&0x2)) {
			memcpy(&nw->mac_addr[1].addr[0],
				&ready->v.macaddr[0][0], ETH_ALEN);
			nw->mac_addr[1].addr[0] |= 0x2;
		} else {
			nw->has_macaddr[1] = false;
		}
	} else if (!ready->v.has_macaddr[0] && ready->v.has_macaddr[1]) {
		memcpy(&nw->mac_addr[1].addr[0],
				&ready->v.macaddr[1][0], ETH_ALEN);
		if (!(nw->mac_addr[1].addr[0]&0x2)) {
			memcpy(&nw->mac_addr[0].addr[0],
					&ready->v.macaddr[1][0], ETH_ALEN);
			nw->mac_addr[0].addr[0] |= 0x2;
		} else {
			nw->has_macaddr[0] = false;
		}
	} else {
		for (i = 0; i < NR_NRC_VIF; i++)
			nw->has_macaddr[i] = false;
	}
}

static void nrc_on_fw_ready(struct sk_buff *skb, struct nrc *nw)
{
	struct ieee80211_hw *hw = nw->hw;
	struct wim_ready *ready;
	struct wim *wim = (struct wim *)skb->data;
	int i;

	nrc_dbg(NRC_DBG_HIF, "system ready");
	ready = (struct wim_ready *) (wim + 1);
	nrc_dbg(NRC_DBG_HIF, "ready");
	nrc_dbg(NRC_DBG_HIF, "  -- type: %d", ready->h.type);
	nrc_dbg(NRC_DBG_HIF, "  -- length: %d", ready->h.len);
	nrc_dbg(NRC_DBG_HIF, "  -- version: 0x%08X",
						ready->v.version);
	nrc_dbg(NRC_DBG_HIF, "  -- tx_head_size: %d",
						ready->v.tx_head_size);
	nrc_dbg(NRC_DBG_HIF, "  -- rx_head_size: %d",
						ready->v.rx_head_size);
	nrc_dbg(NRC_DBG_HIF, "  -- payload_align: %d",
						ready->v.payload_align);
	nw->fwinfo.ready = NRC_FW_ACTIVE;
	nw->fwinfo.version = ready->v.version;
	if (nw->chip_id == 0x7394) {
		if (ready->v.chip_rev_num > 0) {
			nw->fwinfo.chip_rev_num = ready->v.chip_rev_num;
		} else {
			nw->fwinfo.chip_rev_num = 0;
		}
	}

	nw->fwinfo.rx_head_size = ready->v.rx_head_size;
	nw->fwinfo.tx_head_size = ready->v.tx_head_size;
	nw->fwinfo.payload_align = ready->v.payload_align;
	nw->fwinfo.buffer_size = ready->v.buffer_size;
	nrc_dbg(NRC_DBG_HIF, "  -- hw_version: %d",
						ready->v.hw_version);
	nw->fwinfo.hw_version = ready->v.hw_version;
	nrc_dbg(NRC_DBG_HIF, "  -- cap_mask: 0x%x",
						ready->v.cap.cap);
	nrc_dbg(NRC_DBG_HIF, "  -- cap_li: %d, %d",
						ready->v.cap.listen_interval, listen_interval);
	nrc_dbg(NRC_DBG_HIF, "  -- cap_idle: %d, %d",
						ready->v.cap.bss_max_idle, bss_max_idle);

	nw->cap.cap_mask = ready->v.cap.cap;
	nw->cap.listen_interval = ready->v.cap.listen_interval;
	nw->cap.bss_max_idle = ready->v.cap.bss_max_idle;
	nw->cap.max_vif = ready->v.cap.max_vif;

	if (has_macaddr_param(nw->mac_addr[0].addr)) {
		nw->has_macaddr[0] = true;
		nw->has_macaddr[1] = true;
		memcpy(nw->mac_addr[1].addr, nw->mac_addr[0].addr, ETH_ALEN);
		nw->mac_addr[1].addr[1]++;
		nw->mac_addr[1].addr[5]++;
	} else {
		nrc_set_macaddr_from_fw(nw, ready);
	}

	for (i = 0; i < NR_NRC_VIF; i++) {
		if (nw->has_macaddr[i])
			nrc_dbg(NRC_DBG_HIF, "  -- mac_addr[%d]: %pM",
								i, nw->mac_addr[i].addr);
	}

	for (i = 0; i < nw->cap.max_vif; i++) {
		nw->cap.vif_caps[i].cap_mask = ready->v.cap.vif_caps[i].cap;
		if (sw_enc == WIM_ENCDEC_HYBRID) {
			nw->cap.vif_caps[i].cap_mask |= WIM_SYSTEM_CAP_HYBRIDSEC;
		} else if (sw_enc == WIM_ENCDEC_SW) {
			nw->cap.vif_caps[i].cap_mask &= ~WIM_SYSTEM_CAP_HWSEC;
		}
		nrc_dbg(NRC_DBG_HIF, "  -- cap_mask[%d]: 0x%llx", i,
							nw->cap.vif_caps[i].cap_mask);
	}

	if (nw->chip_id == 0x7394) {
		nw->use_ext_lna = (ready->v.lna_offset < -10) ? 1 : 0;
		nrc_dbg(NRC_DBG_HIF, "  -- use_ext_lna:%d (%d)", nw->use_ext_lna, ready->v.lna_offset);
	}

	/* Override with insmod parameters */
	if (listen_interval) {
		hw->max_listen_interval = listen_interval;
		nw->cap.listen_interval = listen_interval;
	}

	if (nrc_mac_is_s1g(hw->priv)) {
		/* bss_max_idle: in unit of 1000 TUs (1024ms = 1.024 seconds) */
		if (bss_max_idle > S1G_UNSCALED_INTERVAL_MAX * 10000 || bss_max_idle <= 0) {
			nw->cap.bss_max_idle = 0;
		} else {
			nw->cap.bss_max_idle = bss_max_idle;
		}
	} else {
		if (bss_max_idle > __UINT16_MAX__ || bss_max_idle <= 0) {
			nw->cap.bss_max_idle = 0;
		} else {
			nw->cap.bss_max_idle = bss_max_idle;
		}
	}

	dev_kfree_skb(skb);
}

int nrc_fw_start(struct nrc *nw)
{
	struct sk_buff *skb_req, *skb_resp;
	struct wim_drv_info_param *p;

	skb_req = nrc_wim_alloc_skb(nw, WIM_CMD_START, tlv_len(sizeof(struct wim_drv_info_param)));
	if (!skb_req)
		return -ENOMEM;

	p = nrc_wim_skb_add_tlv(skb_req, WIM_TLV_DRV_INFO,
							sizeof(struct wim_drv_info_param), NULL);
	p->boot_mode = !!fw_name;
	p->cqm_off = disable_cqm;
	p->bitmap_encoding = bitmap_encoding;
	p->reverse_scrambler = reverse_scrambler;
	p->kern_ver = (NRC_TARGET_KERNEL_VERSION>>8)&0x0fff; // 12 bits for kernel version (4 for major, 8 for minor)
	p->ps_pretend_flag = ps_pretend;
	p->vendor_oui = VENDOR_OUI;
	if (nw->chip_id == 0x7292) {
		p->deepsleep_gpio_dir = TARGET_DEEP_SLEEP_GPIO_DIR_7292;
	} else {
		p->deepsleep_gpio_dir = TARGET_DEEP_SLEEP_GPIO_DIR_739X;
	}
	p->deepsleep_gpio_out = TARGET_DEEP_SLEEP_GPIO_OUT;
	p->deepsleep_gpio_pullup = TARGET_DEEP_SLEEP_GPIO_PULLUP;
	/* 0: HW PTK/GTK, 1: SW PTK/GTK, 2: HW PTK, SW GTK */
	if(sw_enc < 0)
		sw_enc = 0;
	p->sw_enc = sw_enc;
	p->supported_ch_width = support_ch_width;
	p->duty_cycle_enable = set_duty_cycle[0]?true:false;
	p->duty_cycle_window = set_duty_cycle[1];
	p->duty_cycle_duration = set_duty_cycle[2];
	p->cca_threshold = set_cca_threshold;
	if (nw->twt_sched) {
		p->twt_wake_interval = nw->twt_sched->interval;
	} else {
		p->twt_wake_interval = 0;
	}

	p->auth_control_enable = set_auth_control[0]?true:false;
	p->auth_control_slot = set_auth_control[1];
	// 1-bit scale (0:1, 1:10)
	p->auth_control_ti_min = set_auth_control[2] |(set_auth_control[4] == 10? 1<<7:0);
	p->auth_control_ti_max = set_auth_control[3];

	skb_resp = nrc_xmit_wim_request_wait(nw, skb_req, (WIM_RESP_TIMEOUT * 70));
	if (skb_resp)
		nrc_on_fw_ready(skb_resp, nw);
	else
		return -ETIMEDOUT;
	return 0;
}

void nrc_set_bss_max_idle_offset(int value)
{
	bss_max_idle_offset = value;
}

void nrc_set_auto_ba(bool toggle)
{
	if (toggle)
		ampdu_mode = NRC_AMPDU_AUTO;
	else
		ampdu_mode = NRC_AMPDU_MANUAL;

	nrc_dbg(NRC_DBG_MAC, "%s: Auto BA session feature %s", __func__,
		(ampdu_mode == NRC_AMPDU_AUTO)? "ON" : "OFF");
}

int nrc_nw_set_model_conf(struct nrc *nw, u16 chip_id)
{
	nw->chip_id = chip_id;
	dev_info(nw->dev, "Configuration of H/W Dependent Setting : %04x\n", nw->chip_id);

	/**
	 * Config List
	 * - hw_queues
	 * - wowlan_pattern_num
	 */
	switch (nw->chip_id) {
		case 0x7292:
			nw->hw_queues = 6;
			nw->wowlan_pattern_num = 1;
			break;
		case 0x7394:
			nw->hw_queues = 11;
			nw->wowlan_pattern_num = 2;
			break;
		default:
			dev_err(nw->dev, "Unknown Newracom IEEE80211 chipset %04x", nw->chip_id);
			BUG();
	}

	dev_info(nw->dev, "- HW_QUEUES: %d\n", nw->hw_queues);
	dev_info(nw->dev, "- WoWLAN Pattern num: %d\n", nw->wowlan_pattern_num);

	return 0;
}

/**
 * The 27 countries in EU are referenced from
 * https://european-union.europa.eu/principles-countries-history/country-profiles_en
 */
const char *const eu_countries_cc[] = {
	"AT", "BE", "BG", "CY", "CZ", "DE",	"DK", "EE", "ES", "FI",
	"FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
	"NL", "PL", "PT", "RO", "SE", "SI", "SK",
	// Below countries are using the same S1G channels of EU even though not EU countries.
	"GB", "SA",
	NULL
};

int country_match(const char *const cc[], const char *const country)
{
	int i;

	if (country == NULL)
		return 0;
	for (i = 0; cc[i]; i++) {
		if (cc[i][0] == country[0] && cc[i][1] == country[1])
			return 1;
	}

	return 0;
}

#define MAX_FW_RETRY_CNT  30
int nrc_nw_start(struct nrc *nw, bool restart)
{
	int ret;
	int i;

	if (nw->drv_state != NRC_DRV_INIT) {
		dev_err(nw->dev, "Invalid NW state (%d)\n", nw->drv_state);
		return -EINVAL;
	}

	if (fw_name && !nrc_check_boot_ready(nw)) {
		dev_err(nw->dev, "Boot not ready\n");
		return -EINVAL;
	}

#if defined(CONFIG_SUPPORT_BD)
	ret = nrc_check_bd(nw);
	if (ret) {
		dev_err(nw->dev, "Failed to nrc_check_bd\n");
		return -EINVAL;
	}
#endif

	ret = nrc_check_fw_file(nw);
	if (ret == true) {
		nrc_download_fw(nw);
		nrc_release_fw(nw);
	}


	for (i = 0; i < MAX_FW_RETRY_CNT; i++) {
		if(nrc_check_fw_ready(nw)) {
			goto ready;
		}
		mdelay(100);
	}
	dev_err(nw->dev, "Failed to nrc_check_fw_ready\n");
	return -ETIMEDOUT;

ready:
	nw->drv_state = NRC_DRV_START;
	ret = nrc_hif_start(nw->hif);
	if (ret) {
		dev_err(nw->dev, "Failed to nrc_hif_start\n");
		goto err_return;
	}

	ret = nrc_fw_start(nw);
	if (ret) {
		dev_err(nw->dev, "Failed to nrc_fw_start\n");
		goto err_return;
	}

	ret = nrc_netlink_init(nw);
	if (ret) {
		dev_err(nw->dev, "Failed to nrc_netlink_init\n");
		goto err_return;
	}

	if (!restart) {
		ret = nrc_register_hw(nw);
		if (ret) {
			dev_err(nw->dev, "Failed to nrc_register_hw\n");
			goto err_netlink_deinit;
		}
		nrc_init_debugfs(nw); /* This function must be called after ieee80211_register_hw */
	}
	else {
		nrc_mac_restart(nw);
		ieee80211_restart_hw(nw->hw);
	}

	ieee80211_wake_queues(nw->hw);

	return 0;

err_netlink_deinit:
	nrc_netlink_exit();
err_return:
	nrc_hif_stop(nw->hif);
	nrc_hif_reset_device(nw->hif);
	nw->drv_state = NRC_DRV_INIT;
	return ret;
}

int nrc_nw_stop(struct nrc *nw, bool restart)
{
	int counter = 0;

	if (nw->drv_state == NRC_DRV_INIT) {
		return 0;
	}

	if (!loopback) {
		nrc_netlink_exit();
		nrc_hif_sleep_target_prepare(nw->hif, power_save);
		if (!restart)
			nrc_unregister_hw(nw);
	}

	while(atomic_read(&nw->d_deauth.delayed_deauth)) {
		msleep(100);
		if (counter++ > 10) {
			atomic_set(&nw->d_deauth.delayed_deauth, 0);
			nrc_hif_sleep_target_prepare(nw->hif, power_save);
			break;
		}
	}

	nw->drv_state = NRC_DRV_CLOSING;

	ieee80211_stop_queues(nw->hw);

#ifdef CONFIG_USE_TXQ
	nrc_cleanup_txq_all(nw);
#endif
	nrc_exit_debugfs();

	nrc_hif_stop(nw->hif);
	nrc_hif_cleanup(nw->hif);

	nrc_hif_reset_device(nw->hif);

	nw->drv_state = NRC_DRV_INIT;
	return 0;
}


static void restart_worker (struct work_struct *work)
{
	struct nrc *nw = container_of(work, struct nrc, restart_work);
	struct nrc_hif_device *hdev = nw->hif;

#if defined(CONFIG_SUPPORT_BD)
    struct regulatory_request request;

	request.alpha2[0] = nw->alpha2[0];
	request.alpha2[1] = nw->alpha2[1];
	request.initiator = NL80211_REGDOM_SET_BY_DRIVER;
#endif


	dev_info(nw->dev, "Restart NRC\n");
	nrc_nw_stop(nw, true);
	nrc_hif_probe(hdev);
	nrc_reg_notifier(nw->hw->wiphy, &request);
	nrc_nw_start(nw, true);
}

void nrc_nw_restart(struct nrc *nw)
{
	queue_work(nw->restart_workqueue, &nw->restart_work);
}

struct nrc *nrc_nw_alloc(struct device *dev, struct nrc_hif_device *hdev)
{
	struct ieee80211_hw *hw;
	struct nrc *nw;

	hw = nrc_mac_alloc_hw(sizeof(struct nrc), NRC_DRIVER_NAME);

	if (!hw) {
			return NULL;
	}

	nw = hw->priv;
	nw->hw = hw;
	nw->dev = dev;
	nw->hif = hdev;
	hdev->nw = nw;

	nw->loopback = loopback;
	nw->lb_count = lb_count;
	nw->drv_state = NRC_DRV_INIT;

	nw->vendor_skb_beacon = NULL;
	nw->vendor_skb_probe_req = NULL;
	nw->vendor_skb_probe_rsp = NULL;
	nw->vendor_skb_assoc_req = NULL;

	nrc_stats_init();
	nw->fw_priv = nrc_fw_init(nw);
	if (!nw->fw_priv) {
		dev_err(nw->dev, "Failed to initialize FW");
		goto err_hw_free;
	}

	mutex_init(&nw->target_mtx);
	mutex_init(&nw->state_mtx);

	spin_lock_init(&nw->vif_lock);

	init_completion(&nw->hif_tx_stopped);
	init_completion(&nw->hif_rx_stopped);
	init_completion(&nw->hif_irq_stopped);
	if (nrc_wim_response_init(nw) < 0) {
		goto err_hw_free;
	}

	nw->workqueue = create_singlethread_workqueue("nrc_wq");
	nw->ps_wq = create_singlethread_workqueue("nrc_ps_wq");
	nw->restart_workqueue = create_singlethread_workqueue("nrc_restart");

	INIT_DELAYED_WORK(&nw->roc_finish, nrc_mac_roc_finish);
	INIT_DELAYED_WORK(&nw->rm_vendor_ie_wowlan_pattern, nrc_rm_vendor_ie_wowlan_pattern);

	INIT_WORK(&nw->restart_work, restart_worker);

#ifdef CONFIG_USE_TXQ
#ifdef CONFIG_NEW_TASKLET_API
	tasklet_setup(&nw->tx_tasklet, nrc_tx_tasklet);
#else
	tasklet_init(&nw->tx_tasklet, nrc_tx_tasklet, (unsigned long) nw);
#endif
#endif

	if (!disable_cqm) {
		nrc_mac_dbg("CQM is enabled");
		nw->beacon_timeout = 0;
#if KERNEL_VERSION(4, 15, 0) > LINUX_VERSION_CODE
		setup_timer(&nw->bcn_mon_timer,
					nrc_bcn_mon_timer, (unsigned long)nw);
#else
		timer_setup(&nw->bcn_mon_timer, nrc_bcn_mon_timer, 0);
#endif
	}

	if (twt_sp + twt_num + twt_int != 0) {
		if (twt_num_in_group == 0) {
			twt_num_in_group = 1;
		}
		nw->twt_sched = nrc_twt_sched_init(nw, twt_sp, twt_num, twt_int,
			twt_num_in_group, twt_algo);
		//nw->twt_requester = false;
		//nw->twt_responder = true;
	} else {
		nw->twt_sched = NULL;
		//nw->twt_requester = false;
		//nw->twt_responder = false;
		dev_info(nw->dev, "TWT is disabled\n");
	}

	return nw;

err_hw_free:
	nrc_mac_free_hw(hw);

	return NULL;
}

void nrc_nw_free(struct nrc *nw)
{
	if (nw->fw_priv) {
		nrc_fw_exit(nw->fw_priv);
	}

	nrc_twt_sched_deinit(nw);

	tasklet_kill(&nw->tx_tasklet);

	if (!disable_cqm) {
		del_timer(&nw->bcn_mon_timer);
	}

	if (nw->workqueue != NULL) {
		flush_workqueue(nw->workqueue);
		destroy_workqueue(nw->workqueue);
	}

	if (nw->ps_wq != NULL) {
		flush_workqueue(nw->ps_wq);
		destroy_workqueue(nw->ps_wq);
	}

	if (nw->restart_workqueue != NULL) {
		flush_workqueue(nw->restart_workqueue);
		destroy_workqueue(nw->restart_workqueue);
	}

	if (nw->vendor_skb_beacon) {
		dev_kfree_skb_any(nw->vendor_skb_beacon);
	}

	if (nw->vendor_skb_probe_req) {
		dev_kfree_skb_any(nw->vendor_skb_probe_req);
	}

	if (nw->vendor_skb_probe_rsp) {
		dev_kfree_skb_any(nw->vendor_skb_probe_rsp);
	}

	if (nw->vendor_skb_assoc_req) {
		dev_kfree_skb_any(nw->vendor_skb_assoc_req);
	}

	nrc_wim_response_deinit(nw);
	nrc_stats_deinit();

	nrc_mac_free_hw (nw->hw);
}
