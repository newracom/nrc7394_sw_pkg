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

#ifndef _NRC_HIF_CSPI_H_
#define _NRC_HIF_CSPI_H_

#include <linux/spi/spi.h>
#include "nrc-hif.h"

struct spi_sys_reg {
	u8 wakeup;	/* 0x0 */
	u8 status;	/* 0x1 */
	u16 chip_id;	/* 0x2-0x3 */
	u32 modem_id;	/* 0x4-0x7 */
	u32 sw_id;	/* 0x8-0xb */
	u32 board_id;	/* 0xc-0xf */
} __packed;

struct spi_status_reg {
	struct {
		u8 mode;
		u8 enable;
		u8 latched_status;
		u8 status;
	} eirq;
	u8 txq_status[6];
	u8 rxq_status[6];
	u32 msg[4];

#define EIRQ_IO_ENABLE	(1<<2)
#define EIRQ_EDGE	(1<<1)
#define EIRQ_ACTIVE_LO	(1<<0)

#define EIRQ_DEV_SLEEP	(1<<3)
#define EIRQ_DEV_READY	(1<<2)
#define EIRQ_RXQ	(1<<1)
#define EIRQ_TXQ	(1<<0)

#define TXQ_ERROR	(1<<7)
#define TXQ_SLOT_COUNT	(0x7F)
#define RXQ_SLOT_COUNT	(0x7F)

} __packed;

#define SPI_BUFFER_SIZE (496-20)

/* C-SPI command
 *
 * [31:24]: start byte (0x50)
 * [23:23]: burst (0: single, 1: burst)
 * [22:22]: direction (0: read, 1: write)
 * [21:21]: fixed (0: incremental, 1: fixed)
 * [20:13]: address
 * [12:0]: length (for multi-word transfer)
 * [7:0]: wdata (for single write)
 */
#define C_SPI_READ	0x50000000
#define C_SPI_WRITE	0x50400000
#define C_SPI_BURST	0x00800000
#define C_SPI_FIXED	0x00200000
#define C_SPI_ADDR(x)	(((x) & 0xff) << 13)
#define C_SPI_LEN(x)	((x) & 0x1fff)
#define C_SPI_WDATA(x)	((x) & 0xff)
#define C_SPI_ACK	0x47

#define TX_SLOT 0
#define RX_SLOT 1

#define CREDIT_QUEUE_MAX (12)

struct nrc_cspi_ops {
	int (*read_regs)(struct spi_device *spi,
		u8 addr, u8 *buf, ssize_t size);
	int (*write_reg)(struct spi_device *spi, u8 addr, u8 data);
	ssize_t (*read)(struct spi_device *spi, u8 *buf, ssize_t size);
	ssize_t (*write)(struct spi_device *spi, u8 *buf, ssize_t size);
};

/* Object prepended to strut nrc_hif_device */
struct nrc_spi_priv {
	struct spi_device *spi;
	struct nrc_hif_device *hdev;

	/* work, kthread, ... */
	struct delayed_work work;
	struct task_struct *kthread;
	wait_queue_head_t wait; /* wait queue */

#if !defined(CONFIG_SUPPORT_THREADED_IRQ)
	struct workqueue_struct *irq_wq;
	struct work_struct irq_work;
#endif

	struct {
		struct spi_sys_reg sys;
		struct spi_status_reg status;
	} hw;

	spinlock_t lock;
	struct {
		u16 head;
		u16 tail;
		u16 size;
		u16 count;
	} slot[2];
	int max_slot_num;

	/* VIF0(AC0~AC3), BCN, CONC, VIF1(AC0~AC3), padding*/
	int hw_queues;
	u8 front[CREDIT_QUEUE_MAX];
	u8 rear[CREDIT_QUEUE_MAX];
	u8 credit_max[CREDIT_QUEUE_MAX];
#ifdef CONFIG_TRX_BACKOFF
	atomic_t trx_backoff;
#endif
	unsigned long loopback_prev_cnt;
	unsigned long loopback_total_cnt;
	unsigned long loopback_last_jiffies;
	unsigned long loopback_read_usec;
	unsigned long loopback_write_usec;
	unsigned long loopback_measure_cnt;
	struct mutex bus_lock_mutex;
	struct nrc_cspi_ops *ops;

	int polling_interval;
	struct task_struct *polling_kthread;
};

struct nrc_hif_device *nrc_hif_cspi_init(void);
int nrc_hif_cspi_exit(struct nrc_hif_device *hdev);

#endif
