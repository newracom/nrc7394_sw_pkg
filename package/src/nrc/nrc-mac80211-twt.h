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

#ifndef _NRC_MAC80211_TWT_H_
#define _NRC_MAC80211_TWT_H_

#include "nrc.h"

/* This is from ieee80211.h */
#if !defined(IEEE80211_TWT_REQTYPE_REQUEST) /* this is added in kernel 5.14.0 */
#define IEEE80211_TWT_CONTROL_NDP           BIT(0)
#define IEEE80211_TWT_CONTROL_RESP_MODE         BIT(1)
#define IEEE80211_TWT_CONTROL_NEG_TYPE_BROADCAST    BIT(3)
#define IEEE80211_TWT_CONTROL_RX_DISABLED       BIT(4)
#define IEEE80211_TWT_CONTROL_WAKE_DUR_UNIT     BIT(5)

#define IEEE80211_TWT_REQTYPE_REQUEST           BIT(0)
#define IEEE80211_TWT_REQTYPE_SETUP_CMD         GENMASK(3, 1)
#define IEEE80211_TWT_REQTYPE_TRIGGER           BIT(4)
#define IEEE80211_TWT_REQTYPE_IMPLICIT          BIT(5)
#define IEEE80211_TWT_REQTYPE_FLOWTYPE          BIT(6)
#define IEEE80211_TWT_REQTYPE_FLOWID            GENMASK(9, 7)
#define IEEE80211_TWT_REQTYPE_WAKE_INT_EXP      GENMASK(14, 10)
#define IEEE80211_TWT_REQTYPE_PROTECTION        BIT(15)

enum ieee80211_twt_setup_cmd {
    TWT_SETUP_CMD_REQUEST,
    TWT_SETUP_CMD_SUGGEST,
    TWT_SETUP_CMD_DEMAND,
    TWT_SETUP_CMD_GROUPING,
    TWT_SETUP_CMD_ACCEPT,
    TWT_SETUP_CMD_ALTERNATE,
    TWT_SETUP_CMD_DICTATE,
    TWT_SETUP_CMD_REJECT,
};

struct ieee80211_twt_params {
    __le16 req_type;
    __le64 twt;
    u8 min_twt_dur;
    __le16 mantissa;
    u8 channel;
} __packed;

struct ieee80211_twt_setup {
    u8 dialog_token;
    u8 element_id;
    u8 length;
    u8 control;
    u8 params[];
} __packed;

#define S1G_CAP8_TWT_GROUPING   BIT(0)
#define S1G_CAP8_BDT        BIT(1)
#define S1G_CAP8_COLOR      GENMASK(4, 2)
#define S1G_CAP8_TWT_REQUEST    BIT(5)      
#define S1G_CAP8_TWT_RESPOND    BIT(6)
#define S1G_CAP8_PV1_FRAME  BIT(7)

enum ieee80211_s1g_actioncode {
    WLAN_S1G_AID_SWITCH_REQUEST,
    WLAN_S1G_AID_SWITCH_RESPONSE,
    WLAN_S1G_SYNC_CONTROL,
    WLAN_S1G_STA_INFO_ANNOUNCE,
    WLAN_S1G_EDCA_PARAM_SET,
    WLAN_S1G_EL_OPERATION,
    WLAN_S1G_TWT_SETUP,
    WLAN_S1G_TWT_TEARDOWN,
    WLAN_S1G_SECT_GROUP_ID_LIST,
    WLAN_S1G_SECT_ID_FEEDBACK,
    WLAN_S1G_TWT_INFORMATION = 11,
};




enum nrc_ieee80211_eid {
	WLAN_EID_S1G_TWT = 216,
};

enum nrc_ieee80211_category {
	WLAN_CATEGORY_S1G = 22,
};
#endif /* #if !defined(CONFIG_USE_KERNEL_S1G_TWT) */

struct ieee80211_twt_setup_assoc_ie {
    u8 control;
	struct ieee80211_twt_params params;
} __packed;

struct ieee80211_twt_setup_vendor_ie {
	struct ieee80211_vendor_ie v;
	u64 sp;
} __packed;

#define NRC_MAX_STA_TWT_AGRT        1
#define NRC_TWT_VENDOR_IE_ENABLE

#define TWT_DEBUG_IE_FLAG   	(0)
#define TWT_DEBUG_TIME_FLAG   	(1)

#if 1
struct nrc_twt_flow {
	u8 id;

	u8 duration;
	u16 mantissa;
	u8 exp;
	u64 twt;

	struct ieee80211_twt_setup_assoc_ie twt_ie;

	//struct list_head sched_entries;
	// currently only one supported
	struct twt_sched_entry *entry;

};


struct nrc_twt {
	u8 flowid_mask;
	struct nrc_twt_flow flow[NRC_MAX_STA_TWT_AGRT];

	s8 assoc_flowid;	/* 1 based */
};
#endif


/* upper */
struct nrc;

void nrc_mac_rx_twt_setup (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_mgmt *mgmt);

void nrc_mac_tx_twt_setup (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_twt_setup *twt);

void nrc_mac_rx_twt_teardown (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_mgmt *mgmt);

/* ops */
void nrc_mac_add_twt_setup (struct ieee80211_hw *hw,
			      struct ieee80211_sta *sta,
			      struct ieee80211_twt_setup *twt);

void nrc_mac_twt_teardown_request (struct ieee80211_hw *hw,
			      struct ieee80211_sta *sta,
				  u8 flowid);
/* assoc */
void nrc_mac_rx_twt_setup_assoc_req (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_mgmt *mgmt, size_t len);
void nrc_mac_rx_twt_setup_assoc_resp (struct nrc *nw, struct ieee80211_sta *sta, struct sk_buff *skb);

void nrc_mac_twt_debugfs_init (struct dentry *root, void *nw);

void nrc_mac_twt_sp_update (struct nrc *nw, struct ieee80211_sta *sta, int type);
#endif
