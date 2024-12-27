/*
 * Copyright (c) 2016-2024 Newracom, Inc.
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

#undef TRACE_SYSTEM
#define TRACE_SYSTEM nrc

#if !defined(_NRC_TRACE_H_) || defined(TRACE_HEADER_MULTI_READ)

#include <linux/tracepoint.h>

#include "nrc.h"
#include "nrc-hif-cspi.h"

#if !defined(_NRC_TRACE_H_)

static char *mgmt_str[] = {
	"ASSOC REQ",
	"ASSOC RESP",
	"REASSOC REQ",
	"REASSOC RESP",
	"PROBE REQ",
	"PROBE RESP",
	"???",
	"???",
	"BEACON",
	"ATIM",
	"DISASSOC",
	"AUTH",
	"DEAUTH",
	"ACTION",
};

static inline char *get_mgmt_str (u8 subtype)
{
	return mgmt_str[subtype >> 4];
}

static inline u32 nrc_frm_hdr_len(const void *buf, size_t len)
{
    const struct ieee80211_hdr *hdr = buf;

    /* In some rare cases (e.g. fcs error) device reports frame buffer
     * shorter than what frame header implies (e.g. len = 0). The buffer
     * can still be accessed so do a simple min() to guarantee caller
     * doesn't get value greater than len.
     */
    return min_t(u32, len, ieee80211_hdrlen(hdr->frame_control));
}
#endif

#define _NRC_TRACE_H_

#if !defined(CONFIG_NRC_TRACING)
#undef TRACE_EVENT
#define TRACE_EVENT(name, proto, ...) \
static inline void trace_ ## name(proto) {}
#undef DECLARE_EVENT_CLASS
#define DECLARE_EVENT_CLASS(...)
#undef DEFINE_EVENT
#define DEFINE_EVENT(evt_class, name, proto, ...) \
static inline void trace_ ## name(proto) {}
#endif
        

TRACE_EVENT(nrc_test,
    TP_PROTO(u32 time),
    TP_ARGS(time),
    TP_STRUCT__entry(
        __field(u32, time)
    ),
    TP_fast_assign(
        __entry->time = time;
    ),
    TP_printk("EVT_LOGT:%010u",
           __entry->time)
);

DECLARE_EVENT_CLASS(nrc_hdr_event,
		    TP_PROTO(struct nrc *nw, const void *data, size_t len, int8_t data1, int8_t data2),

	TP_ARGS(nw, data, len, data1, data2),

	TP_STRUCT__entry(
		__string(device, dev_name(nw->dev))
		__string(driver, dev_driver_string(nw->dev))
		__field(u16, fc_etc)
		__field(u8, fc_type)
		__field(u8, fc_subtype)
		__array(char, addr1, ETH_ALEN)
		__array(char, addr2, ETH_ALEN)
		__array(char, addr3, ETH_ALEN)
		__field(u16, sn)
		__field(u8, tid)
		__field(size_t, len)
		__field(s8, data1)
		__field(s8, data2)
		__dynamic_array(u8, data, nrc_frm_hdr_len(data, len))
	),

	TP_fast_assign(
		struct ieee80211_hdr *hdr = (void *)data;
		__le16 fc = hdr->frame_control;

		__assign_str(device, dev_name(nw->dev));
		__assign_str(driver, dev_driver_string(nw->dev));
		__entry->fc_type = (fc & cpu_to_le16(IEEE80211_FCTL_FTYPE));
		__entry->fc_subtype = (fc & cpu_to_le16(IEEE80211_FCTL_STYPE));
		__entry->fc_etc = fc & cpu_to_le16(0xFF00);
		memcpy(__entry->addr1, hdr->addr1, ETH_ALEN);
		memcpy(__entry->addr2, hdr->addr2, ETH_ALEN);
		memcpy(__entry->addr3, hdr->addr3, ETH_ALEN);
		__entry->sn = IEEE80211_SEQ_TO_SN(hdr->seq_ctrl);
#if KERNEL_VERSION(4, 17, 0) <= NRC_TARGET_KERNEL_VERSION
		__entry->tid = ieee80211_get_tid(hdr);
#else
		__entry->tid = *ieee80211_get_qos_ctl(hdr) & IEEE80211_QOS_CTL_TID_MASK;
#endif
		__entry->len = nrc_frm_hdr_len(data, len);
		__entry->data1 = data1;
		__entry->data2 = data2;
		memcpy(__get_dynamic_array(data), data, __entry->len);
	),

#if 0
	TP_printk(
		"%s %s len %zu \n%s\n",
		__get_str(driver),
		__get_str(device),
		__entry->len,
		__print_hex(__get_dynamic_array(data), __entry->len)
	)
#endif
	TP_printk(
		"len %zu, %s %s \n\n\t\t\t[RA:%pM] [TA:%pM] SN:%4d TID:%u (%d,%d)\n\n",
		//__get_str(driver),
		//__get_str(device),
		__entry->len,
		(__entry->fc_type == cpu_to_le16(IEEE80211_FTYPE_DATA))?__print_flags(__entry->fc_subtype, " ",
			{ IEEE80211_STYPE_DATA, "DATA" },
			{ IEEE80211_STYPE_DATA_CFACK, "CFACK" },
			{ IEEE80211_STYPE_DATA_CFPOLL, "CFPOLL" },
			{ IEEE80211_STYPE_NULLFUNC, "NULL" },
			{ IEEE80211_STYPE_QOS_DATA, "QOS" }
			):(__entry->fc_type == cpu_to_le16(IEEE80211_FTYPE_MGMT))?get_mgmt_str(__entry->fc_subtype):"???",
		__print_flags(__entry->fc_etc, NULL,
			{ IEEE80211_FCTL_TODS, "ToDS" },
			{ IEEE80211_FCTL_FROMDS, "FromDS" },
			{ IEEE80211_FCTL_MOREFRAGS, "Frag" },
			{ IEEE80211_FCTL_RETRY, "Re" },
			{ IEEE80211_FCTL_PM, "Pm", },
			{ IEEE80211_FCTL_MOREDATA, "More" },
			{ IEEE80211_FCTL_PROTECTED, "Prot" },
			{ IEEE80211_FCTL_ORDER, "Order" } ),
		__entry->addr1,
		__entry->addr2,
		__entry->sn,
		__entry->tid,
		__entry->data1,
		__entry->data2
		//__print_hex(__get_dynamic_array(data), __entry->len)
	)
);

#define MAX_PAYLOAD		0x60

DECLARE_EVENT_CLASS(nrc_payload_event,
		    TP_PROTO(struct nrc *nw, const void *data, size_t len),

	TP_ARGS(nw, data, len),

	TP_STRUCT__entry(
		__string(device, dev_name(nw->dev))
		__string(driver, dev_driver_string(nw->dev))
		__field(size_t, len)
		__dynamic_array(u8, payload, (len -
					      nrc_frm_hdr_len(data, len)))
	),

	TP_fast_assign(
		__assign_str(device, dev_name(nw->dev));
		__assign_str(driver, dev_driver_string(nw->dev));
		__entry->len = len - nrc_frm_hdr_len(data, len);
		memcpy(__get_dynamic_array(payload),
		       data + nrc_frm_hdr_len(data, len), min_t(size_t, __entry->len, MAX_PAYLOAD));
	),

#if defined __print_hex_dump
	TP_printk(
		"len %zu \n%s%s\n",
		__entry->len,
		__print_hex_dump("\t\t\t", DUMP_PREFIX_NONE, 16, 1, __get_dynamic_array(payload), min_t(size_t, __entry->len, MAX_PAYLOAD), false),
		(__entry->len > MAX_PAYLOAD) ? "\t\t\t...":""
	)
#else
	TP_printk(
		"len %zu \n%s%s\n",
		__entry->len,
		__print_array(__get_dynamic_array(payload), min_t(size_t, __entry->len, MAX_PAYLOAD), 1),
		(__entry->len > MAX_PAYLOAD) ? "\t\t\t...":""
	)
#endif
);

DEFINE_EVENT(nrc_hdr_event, nrc_tx_hdr,
         TP_PROTO(struct nrc *nw, const void *data, size_t len, int8_t data1, int8_t data2),
         TP_ARGS(nw, data, len, data1, data2)
);

DEFINE_EVENT(nrc_payload_event, nrc_tx_payload,
         TP_PROTO(struct nrc *nw, const void *data, size_t len),
         TP_ARGS(nw, data, len)
);

DEFINE_EVENT(nrc_hdr_event, nrc_rx_hdr,
         TP_PROTO(struct nrc *nw, const void *data, size_t len, int8_t data1, int8_t data2),
         TP_ARGS(nw, data, len, data1, data2)
);

DEFINE_EVENT(nrc_payload_event, nrc_rx_payload,
         TP_PROTO(struct nrc *nw, const void *data, size_t len),
         TP_ARGS(nw, data, len)
);

DECLARE_EVENT_CLASS(nrc_hif_slot_event,
		    TP_PROTO(struct nrc_spi_priv *priv, int dir, const char *msg),

	TP_ARGS(priv, dir, msg),

	TP_STRUCT__entry(
		__string(msg, msg)
		__field(u8, dir)
		__field(u16, head)
		__field(u16, tail)
		__field(u16, num)
	),

	TP_fast_assign(
		__assign_str(msg, msg);
		__entry->dir = dir;
		__entry->head = priv->slot[dir].head;
		__entry->tail = priv->slot[dir].tail;
		__entry->num = priv->slot[dir].head - priv->slot[dir].tail;
	),

	TP_printk(
		"SLOT[%s] %2hu (%4hu,%4hu) '%s'\n", 
		(__entry->dir == 0)?"TX":"RX", __entry->num, __entry->head, __entry->tail, __get_str(msg)
	)
);

DEFINE_EVENT(nrc_hif_slot_event, nrc_hif_rx_slot,
		 TP_PROTO(struct nrc_spi_priv *priv, int dir, const char *msg),
         TP_ARGS(priv, dir, msg)
);

DEFINE_EVENT(nrc_hif_slot_event, nrc_hif_tx_slot,
		 TP_PROTO(struct nrc_spi_priv *priv, int dir, const char *msg),
         TP_ARGS(priv, dir, msg)
);



#endif /* _NRC_TRACE_H_ */

#if defined(CONFIG_NRC_TRACING)
#undef TRACE_INCLUDE_PATH
#define TRACE_INCLUDE_PATH .
#undef TRACE_INCLUDE_FILE
#define TRACE_INCLUDE_FILE nrc-trace

#include <trace/define_trace.h>
#endif

