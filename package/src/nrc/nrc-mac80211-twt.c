/*
 * Copyright (c) 2016-2024 Newracom, Inc.
 *
 * TX/RX routines
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
#include <linux/version.h>
#if KERNEL_VERSION(4, 8, 16) < LINUX_VERSION_CODE
#include <linux/bitfield.h>
#endif
#include <net/mac80211.h>

#include "compat.h"
#include "nrc.h"
#include "wim.h"
#include "nrc-debug.h"
#include "nrc-mac80211.h"
#include "nrc-vendor.h"
#include "nrc-mac80211-twt.h"
#include "nrc-twt-sched.h"

//#define DEBUG_ASSOC


char *setup_cmd_to_str[] = {
	"REQUEST",
	"SUGGEST",
	"DEMAND", 
	"GROUPING",
	"ACCEPT",
	"ALTERNATE",
	"DICTATE",
	"REJECT",
};

static void twt_vendor_ie_fill (struct ieee80211_twt_setup_vendor_ie *twt_vendor_ie, u64 sp)
{
	twt_vendor_ie->v.element_id = WLAN_EID_VENDOR_SPECIFIC;
	twt_vendor_ie->v.len = sizeof(*twt_vendor_ie) - 2;
	twt_vendor_ie->v.oui[0] = 0xFC;
	twt_vendor_ie->v.oui[1] = 0xFF;
	twt_vendor_ie->v.oui[2] = 0xAA;
	twt_vendor_ie->v.oui_type = NRC_SUBCMD_TWT_VENDOR_IE;
	twt_vendor_ie->sp = cpu_to_le64(sp);
}


static bool twt_setup_in_progress (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_twt_setup *twt)
{
	struct nrc_sta *i_sta = to_i_sta(sta);
	s8 flowid;
	bool ret = false;

	flowid = i_sta->twt.assoc_flowid - 1;

	if (flowid >= 0) {
		ret = true;
	}

	return ret;
}

static void twt_setup_assoc_info_save (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_twt_setup *twt)
{
	struct ieee80211_twt_params *twt_agrt = (void *)twt->params;
	u16 req_type = le16_to_cpu(twt_agrt->req_type);
	u8 flowid = FIELD_GET(IEEE80211_TWT_REQTYPE_FLOWID, req_type);

	struct nrc_sta *i_sta = to_i_sta(sta);
	struct nrc_twt_flow *flow;

	i_sta->twt.assoc_flowid = flowid + 1;
	flow = &i_sta->twt.flow[flowid];

	flow->twt_ie.control = twt->control;
	memcpy(&flow->twt_ie.params, twt_agrt, sizeof(struct ieee80211_twt_params));
}


static int twt_setup_assoc_info_restore (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_twt_setup_assoc_ie *twt_ie)
{
	struct nrc_sta *i_sta = to_i_sta(sta);
	struct nrc_twt_flow *flow;
	s8 flowid;
	int ret = -1;

	flowid = i_sta->twt.assoc_flowid - 1;
	if (flowid < 0) {
		dev_err(nw->dev, "No associated TWT\n");
		goto done;
	}

	flow = &i_sta->twt.flow[flowid];
	
	memcpy(twt_ie, &flow->twt_ie, sizeof(struct ieee80211_twt_setup_assoc_ie));

	i_sta->twt.assoc_flowid = 0;
	ret = 0;

done:
	return ret;
}

void static twt_setup_dump (struct nrc *nw, struct ieee80211_twt_setup *twt)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
	struct ieee80211_twt_params *twt_agrt = (struct ieee80211_twt_params *)twt->params;
	enum ieee80211_twt_setup_cmd cmd;
	u8 flowid, exp;
	u64 interval;
	int duration;
	u16 req_type = le16_to_cpu(twt_agrt->req_type);

	if (!twt_sched) {
		return;
	}

	if (!test_bit(TWT_DEBUG_IE_FLAG, &twt_sched->debug_flags)) return;

	dev_info(nw->dev, "\n======================================\n");
	dev_info(nw->dev, "TWT DUMP\n");
	dev_info(nw->dev, "--------------------------------------\n");

	/* Dump Request Type, 2 Octets */
	dev_info(nw->dev, "%-10s : %-10s\n", "TYPE", (req_type & IEEE80211_TWT_REQTYPE_REQUEST)?"REQUEST":"RESPONSE");

	cmd = FIELD_GET(IEEE80211_TWT_REQTYPE_SETUP_CMD, req_type);
	dev_info(nw->dev, "%-10s : %-10s\n", "CMD", setup_cmd_to_str[cmd]);

	dev_info(nw->dev, "%-10s : %-10s\n", "OP", (req_type & IEEE80211_TWT_REQTYPE_IMPLICIT)?"IMPLICIT":"EXPLICIT");
	
	dev_info(nw->dev, "%-10s : %-10s\n", "FLOW TYPE", (req_type & IEEE80211_TWT_REQTYPE_FLOWTYPE)?"UNANNOUNCE":"ANNOUNCE");
	flowid = FIELD_GET(IEEE80211_TWT_REQTYPE_FLOWID, req_type);
	dev_info(nw->dev, "%-10s : %-10d\n", "FLOW ID", flowid);

	exp = FIELD_GET(IEEE80211_TWT_REQTYPE_WAKE_INT_EXP, req_type);
	dev_info(nw->dev, "%-10s : %-10d\n", "EXP", exp);

	dev_info(nw->dev, "%-10s : %-10s\n", "PROTECT", (req_type & IEEE80211_TWT_REQTYPE_PROTECTION)?"PROTECTED":"UNPROTECTED");

	/* Dump Params */
	dev_info(nw->dev, "%-10s : %-10llu\n", "WAKE TIME", le64_to_cpu(twt_agrt->twt));
	dev_info(nw->dev, "%-10s : %-10u\n", "DURATION", twt_agrt->min_twt_dur);
	dev_info(nw->dev, "%-10s : %-10d\n", "MANTISSA", le16_to_cpu(twt_agrt->mantissa));

	dev_info(nw->dev, "--------------------------------------\n");
	interval = (u64)le16_to_cpu(twt_agrt->mantissa) << exp;
	dev_info(nw->dev, "%-10s : %llu usec, %llu TU\n", "INTERVAL", interval, interval >> 10); 
	duration = twt_agrt->min_twt_dur << 8;
	dev_info(nw->dev, "%-10s : %d usec, %d TU\n", "SP", duration, duration >> 10); 
	dev_info(nw->dev, "--------------------------------------\n");
}

void nrc_mac_tx_twt_setup (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_twt_setup *twt)
{
	int len;
    struct sk_buff *skb;
	struct ieee80211_vif *vif = nw->vif[0]; /* CKLEE_TODO, any vif */
	struct ieee80211_hw *hw = nw->hw;
    struct ieee80211_mgmt *mgmt;
    struct ieee80211_twt_setup *twt_setup;
    struct ieee80211_tx_info *txi;
#ifdef CONFIG_SUPPORT_TX_CONTROL
    struct ieee80211_tx_control control = { .sta = sta, };
#endif
#ifdef NRC_TWT_VENDOR_IE_ENABLE
	struct ieee80211_twt_setup_vendor_ie *twt_vendor_ie;
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
#endif
    u8 *da, *sa, *bssid;

    len = IEEE80211_MIN_ACTION_SIZE + 4 + 1 + sizeof(struct ieee80211_twt_params); /* setup + control + param */
#ifdef NRC_TWT_VENDOR_IE_ENABLE
	len += sizeof(struct ieee80211_twt_setup_vendor_ie);
#endif
    skb = dev_alloc_skb(hw->extra_tx_headroom + len);
    if (!skb) return;

    skb_reserve(skb, hw->extra_tx_headroom);
    mgmt = skb_put_zero(skb, len);
    mgmt->frame_control = cpu_to_le16(IEEE80211_FTYPE_MGMT |
                  IEEE80211_STYPE_ACTION);

    /* ap send */ /* check if sta is ap or sta */
    da = sta->addr;
    sa = vif->addr;
    bssid = vif->addr;

    memcpy(mgmt->da, da, ETH_ALEN);
    memcpy(mgmt->sa, sa, ETH_ALEN);
    memcpy(mgmt->bssid, bssid, ETH_ALEN);
    mgmt->u.action.category = WLAN_CATEGORY_S1G;
    //mgmt->u.action.u.s1g.action_code = WLAN_S1G_TWT_SETUP;
    mgmt->u.action.u.chan_switch.action_code = WLAN_S1G_TWT_SETUP;

    //twt_setup = (void *)mgmt->u.action.u.s1g.variable;
    twt_setup = (void *)mgmt->u.action.u.chan_switch.variable;
	memcpy(twt_setup, twt, sizeof(*twt_setup) + sizeof(struct ieee80211_twt_params));
#ifdef NRC_TWT_VENDOR_IE_ENABLE
	twt_vendor_ie = (void *)twt_setup + sizeof(*twt_setup) + sizeof(struct ieee80211_twt_params);
	twt_vendor_ie_fill(twt_vendor_ie, twt_sched->sp);
#endif

    skb_set_queue_mapping(skb, IEEE80211_AC_VO);

    txi = IEEE80211_SKB_CB(skb);
    txi->control.vif = vif;

#ifdef CONFIG_SUPPORT_NEW_MAC_TX
    nrc_mac_tx(hw, &control, skb);
#else
    nrc_mac_tx(hw, skb);
#endif

}


void nrc_mac_rx_twt_setup (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_mgmt *mgmt)
{
	//struct ieee80211_twt_setup *twt = (void *)mgmt->u.action.u.s1g.variable;
	struct ieee80211_twt_setup *twt = (void *)mgmt->u.action.u.chan_switch.variable;
	struct ieee80211_twt_params *twt_agrt = (void *)twt->params;

	twt_setup_dump(nw, twt);

	if (nw->twt_responder) {
		/* prepare response */
		twt_agrt->req_type &= cpu_to_le16(~IEEE80211_TWT_REQTYPE_REQUEST);

		/* sanity check */

		/* call ops */
		nrc_mac_add_twt_setup(nw->hw, sta, twt);

		twt_setup_dump(nw, twt);

		nrc_mac_tx_twt_setup(nw, sta, twt);
	}
}

void nrc_mac_rx_twt_teardown (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_mgmt *mgmt)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
	//u8 flowid = mgmt->u.action.u.s1g.variable[0];
	u8 flowid = mgmt->u.action.u.chan_switch.variable[0];

	if (!twt_sched) {
		return;
	}

	if (test_bit(TWT_DEBUG_IE_FLAG, &twt_sched->debug_flags))
		dev_info(nw->dev, "TWT teardown Flowid : %u\n", flowid);

	if (nw->twt_responder) {
		nrc_mac_twt_teardown_request(nw->hw, sta, flowid);
	}
}

void nrc_mac_rx_twt_setup_assoc_req (struct nrc *nw, struct ieee80211_sta *sta, struct ieee80211_mgmt *mgmt, size_t len)
{
	u8 *ies = mgmt->u.assoc_req.variable;
	size_t ies_len = len - (ies - (u8 *)mgmt);
#if KERNEL_VERSION(5, 1, 0) > LINUX_VERSION_CODE
	const u8 *ie;
#else
	const struct element *elem;
#endif
	struct ieee80211_twt_setup *twt;
	struct ieee80211_twt_setup_ie *twt_ie;
	struct ieee80211_twt_params *twt_agrt;
	
	bool in_prog;

#ifdef DEBUG_ASSOC 
	print_hex_dump(KERN_DEBUG, "assoc req ie: ", DUMP_PREFIX_NONE,
			16, 1, ies, ies_len, true);
#endif

#if KERNEL_VERSION(5, 1, 0) > LINUX_VERSION_CODE
	ie = cfg80211_find_ie(WLAN_EID_S1G_TWT, ies, ies_len);
	if (ie == NULL) {
		goto done;
	}
	twt_ie = (void *)ie + 2;
#else
	elem = cfg80211_find_elem(WLAN_EID_S1G_TWT, ies, ies_len); 
	if (elem == NULL) {
		goto done;
	}
	twt_ie = (void *)elem->data;
#endif

	dev_info(nw->dev, "TWT IE FOUND in Assoc Req\n");

	twt = (struct ieee80211_twt_setup *)((u8 *)twt_ie - 3);	/* dialog_token, element_id, length... dirty hack */
	twt_agrt = (void *)twt->params;

	twt_setup_dump(nw, twt);

	in_prog = twt_setup_in_progress(nw, sta, twt);
	if (in_prog) {
		dev_err(nw->dev, "TWT setup is in progress\n");
		goto done;
	}

	/* prepare response */
	twt_agrt->req_type &= cpu_to_le16(~IEEE80211_TWT_REQTYPE_REQUEST);

	nrc_mac_add_twt_setup(nw->hw, sta, twt);

	twt_setup_dump(nw, twt);

	twt_setup_assoc_info_save(nw, sta, twt);

done:
	;
}

void nrc_mac_rx_twt_setup_assoc_resp (struct nrc *nw, struct ieee80211_sta *sta, struct sk_buff *skb)
{
	int ret;
	u8 *assoc_ie;
	struct ieee80211_twt_setup_assoc_ie twt_ie;
#ifdef NRC_TWT_VENDOR_IE_ENABLE
	struct ieee80211_twt_setup_vendor_ie *twt_vendor_ie;
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
#endif

#ifdef DEBUG_ASSOC  /* dump assoc resp */
	struct ieee80211_mgmt *mgmt = (void *)skb->data;
	size_t len = skb->len;
	u8 *ies = mgmt->u.assoc_resp.variable;
	size_t ies_len = len - (ies - (u8 *)mgmt);
	u16 aid = le16_to_cpu(mgmt->u.assoc_resp.aid);
	u16 status_code = le16_to_cpu(mgmt->u.assoc_resp.status_code);

	printk("ASSOC RESP AID: %d, status: %d\n", (u16)(aid & ~(BIT(15)|BIT(14))), status_code);
	print_hex_dump(KERN_DEBUG, "assoc resp ie: ", DUMP_PREFIX_NONE,
			16, 1, ies, ies_len, true);
#endif

	ret = twt_setup_assoc_info_restore(nw, sta, &twt_ie);
	if (ret == 0) {
		dev_info(nw->dev, "TWT IE Set in Assoc Resp\n");
		assoc_ie = ieee80211_append_ie(skb, WLAN_EID_S1G_TWT, sizeof(struct ieee80211_twt_setup_assoc_ie));
		memcpy(assoc_ie, &twt_ie, sizeof(struct ieee80211_twt_setup_assoc_ie));
#ifdef NRC_TWT_VENDOR_IE_ENABLE
		skb_put(skb, sizeof(struct ieee80211_twt_setup_vendor_ie));
		twt_vendor_ie = (void *)assoc_ie + sizeof(struct ieee80211_twt_setup_assoc_ie);
		twt_vendor_ie_fill(twt_vendor_ie, twt_sched->sp);
#if 0
		print_hex_dump(KERN_DEBUG, "assoc resp ie: ", DUMP_PREFIX_NONE,
				16, 1, skb->data, skb->len, true);
#endif
#endif
	}
}

static int nrc_mac_check_twt_req(struct ieee80211_twt_setup *twt)
{
	struct ieee80211_twt_params *twt_agrt;
	u16 req_type;
	u64 interval, duration;
	u16 mantissa;
	u8 exp;

	twt_agrt = (struct ieee80211_twt_params *)twt->params;
	req_type = le16_to_cpu(twt_agrt->req_type);

	/* only implicit agreement supported*/
	if (!(req_type & IEEE80211_TWT_REQTYPE_IMPLICIT)){
		nrc_mac_dbg("Explicit TWT REQ is not supported\n");
		return -EOPNOTSUPP;
	}

	exp = FIELD_GET(IEEE80211_TWT_REQTYPE_WAKE_INT_EXP, req_type);
	mantissa = le16_to_cpu(twt_agrt->mantissa);
	duration = twt_agrt->min_twt_dur << 8;  /* 256 us */

	interval = (u64)mantissa << exp;
	if (interval < duration){
		nrc_mac_dbg("Sleep interval must be longer than service duration(int:%llu, dur:%llu)\n", interval, duration);
		return -EOPNOTSUPP;
	}

	return 0;
}

static int
nrc_mac_twt_flow_get (struct nrc *nw, struct nrc_sta *i_sta,
        struct ieee80211_twt_params *twt_agrt, struct nrc_twt_flow **flow)
{
    u16 type = le16_to_cpu(twt_agrt->req_type);
    u8 exp = FIELD_GET(IEEE80211_TWT_REQTYPE_WAKE_INT_EXP, type);
	u8 duration = twt_agrt->min_twt_dur;
	u16 mantissa = le16_to_cpu(twt_agrt->mantissa);
	u8 flowid = FIELD_GET(IEEE80211_TWT_REQTYPE_FLOWID, type);
	u64 interval;

	struct nrc_twt_flow *f = NULL;
	int ret = -1;

	if (flowid > NRC_MAX_STA_TWT_AGRT -1) {
		dev_err(nw->dev, "Exceed TWT flow id (%u), max num: %u\n", flowid, NRC_MAX_STA_TWT_AGRT);
		goto done; /* reject */
	}

	f = &i_sta->twt.flow[flowid];

	if ((i_sta->twt.flowid_mask & BIT(flowid))) {
		dev_info(nw->dev, "TWT flow id (%u) exist\n", flowid);

        if (f->duration == duration &&
                f->mantissa == mantissa &&
                f->exp == exp) { 
			dev_info(nw->dev, "Same TWT Param\n");
			goto done; /* same accept */
		}

		f = NULL;
		goto done; /* reject */
	}

	memset(f, 0, sizeof(*f));
	f->id = flowid;
	f->duration = duration;
	f->mantissa = mantissa;
	f->exp = exp;

	interval = (u64)(mantissa) << exp;

	dev_info(nw->dev, "Flowid:%u Duration:%u, Interval:(man:%u, exp:%u = %llu)", 
						flowid, duration, mantissa, exp, interval);

	ret = 0; /* accept */
done:
	*flow = f;
	return ret;
}


#if 0
static bool
nrc_mac_twt_param_equal(struct nrc *nw, struct nrc_sta *i_sta,
        struct ieee80211_twt_params *twt_agrt)
{
    u16 type = le16_to_cpu(twt_agrt->req_type);
    u8 exp = FIELD_GET(IEEE80211_TWT_REQTYPE_WAKE_INT_EXP, type);
	u8 duration = twt_agrt->min_twt_dur;
	u16 mantissa = le16_to_cpu(twt_agrt->mantissa);
	u8 flowid = FIELD_GET(IEEE80211_TWT_REQTYPE_FLOWID, type);
    int i;

    exp = FIELD_GET(IEEE80211_TWT_REQTYPE_WAKE_INT_EXP, type);

	if (flowid > NRC_MAX_STA_TWT_AGRT -1) {
		dev_err(nw->dev, "Exceed TWT flow id (%u), max num: %u\n", flowid, NRC_MAX_STA_TWT_AGRT);
		return true;
	}

	if ((i_sta->twt.flowid_mask & BIT(flowid))) {
		dev_err(nw->dev, "TWT flow id (%u) exist\n", flowid);
		return true;
	}

    for (i = 0; i < NRC_MAX_STA_TWT_AGRT; i++) {
        struct nrc_twt_flow *f;

		if (!(i_sta->twt.flowid_mask & BIT(i)))
			continue;

        f = &i_sta->twt.flow[i];
        if (f->duration == duration &&
                f->mantissa == mantissa &&
                f->exp == exp) { 
			dev_err(nw->dev, "Same TWT Param\n");
            return true;
		}
    }

    return false;
}
#endif

void nrc_mac_add_twt_setup (struct ieee80211_hw *hw,
			      struct ieee80211_sta *sta,
			      struct ieee80211_twt_setup *twt)
{
	struct ieee80211_twt_params *twt_agrt = (void *)twt->params;
	enum ieee80211_twt_setup_cmd ret_setup_cmd = TWT_SETUP_CMD_REJECT;
	//enum ieee80211_twt_setup_cmd sta_setup_cmd = FIELD_GET(IEEE80211_TWT_REQTYPE_SETUP_CMD, req_type);

	struct nrc *nw = hw->priv;
	struct nrc_sta *i_sta = to_i_sta(sta);
	struct nrc_twt_flow *flow = NULL;

	int ret = 0;

	dev_info(nw->dev, "TWT Setup from %pM (%d)\n", sta->addr, sta->aid);

	if (nrc_mac_check_twt_req(twt))
		goto out;

#if 0
	if (nrc_mac_twt_param_equal(nw, i_sta, twt_agrt)){
		goto out;
	}
#endif

	ret = nrc_mac_twt_flow_get(nw, i_sta, twt_agrt, &flow);

	if (ret == 0) {
		ret = nrc_twt_sched_entry_add(nw, i_sta, flow);
	}
	else {
		if (flow) {
			ret = 0;
			/* CKLEE_TODO, need update Wake Time? */
		}
		else {
			goto out;
		}
	}

	switch (ret) {
		case 0:
			dev_info(nw->dev, "TWT ACCEPT\n");
			ret_setup_cmd = TWT_SETUP_CMD_ACCEPT;

#if 0
			i_sta->twt.flowid_mask |= BIT(flowid);
			twt_agrt->req_type &= ~cpu_to_le16(IEEE80211_TWT_REQTYPE_FLOWID);
			twt_agrt->req_type |= le16_encode_bits(flowid,
					IEEE80211_TWT_REQTYPE_FLOWID);
#endif
			twt_agrt->twt = cpu_to_le64(flow->twt);
			break;
		case 1:
			dev_info(nw->dev, "TWT DICTATE\n");
			ret_setup_cmd = TWT_SETUP_CMD_DICTATE;

			twt_agrt->mantissa = cpu_to_le16(flow->mantissa);
			twt_agrt->req_type &= ~cpu_to_le16(IEEE80211_TWT_REQTYPE_WAKE_INT_EXP);
			twt_agrt->req_type |= le16_encode_bits(flow->exp, IEEE80211_TWT_REQTYPE_WAKE_INT_EXP);

#if 0
			twt_agrt->req_type &= ~cpu_to_le16(IEEE80211_TWT_REQTYPE_FLOWID);
			twt_agrt->req_type |= le16_encode_bits(flowid,
					IEEE80211_TWT_REQTYPE_FLOWID);
#endif
			break;
		case -1:
			dev_info(nw->dev, "TWT REJECT\n");
			ret_setup_cmd = TWT_SETUP_CMD_REJECT;
			break;
		default:
			dev_info(nw->dev, "TWT REJECT\n");
			ret_setup_cmd = TWT_SETUP_CMD_REJECT;
			break;
	}

out:
	twt_agrt->req_type &= ~cpu_to_le16(IEEE80211_TWT_REQTYPE_SETUP_CMD);
	twt_agrt->req_type |=
		le16_encode_bits(ret_setup_cmd, IEEE80211_TWT_REQTYPE_SETUP_CMD);
}

void nrc_mac_twt_teardown_request (struct ieee80211_hw *hw,
			      struct ieee80211_sta *sta,
			      u8 flowid)
{
	struct nrc *nw = hw->priv;
	struct nrc_sta *i_sta = to_i_sta(sta);

	dev_info(nw->dev, "TWT Teardown from %pM (%d)\n", sta->addr, sta->aid);
	nrc_twt_sched_entry_del(nw, i_sta, flowid);
}

void nrc_mac_twt_sp_update (struct nrc *nw, struct ieee80211_sta *sta, int type)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
	struct nrc_sta *i_sta = to_i_sta(sta);
	struct nrc_twt_flow *flow;
	u8 flowid = 0;

	/* only one flow supported, I can't distinguish flow id with QoS NULL */
	if (NRC_MAX_STA_TWT_AGRT != 1) return;

	if (twt_sched  == NULL) return;

	mutex_lock(&twt_sched->mutex);

	if (!twt_sched->started) {
		goto unlock;
	}

	if (!(i_sta->twt.flowid_mask & BIT(flowid))) {
		goto unlock;
	}

	flow = &i_sta->twt.flow[flowid];

	if (type == 0) {
		nrc_twt_sched_start_update(twt_sched, i_sta, flow);
	}
	else {
		nrc_twt_sched_end_update(twt_sched, i_sta, flow);
	}

unlock:
	mutex_unlock(&twt_sched->mutex);
}

static ssize_t nrc_mac_twt_info_read (struct file *file, char __user *user_buf,
                 size_t count, loff_t *ppos)
{
	struct nrc *nw = file->private_data;
	struct nrc_twt_sched *twt_sched = nw->twt_sched;

	char buf[128];
    char *text;
    int m, ret, text_size = 256;


	if (*ppos != 0) { return 0; }
    text = kmalloc(text_size, GFP_KERNEL);
    if (!text) {
        return -ENOMEM;
    }

	m = 0;
	if (!twt_sched) {
		m += scnprintf(text + m, text_size - m, "TWT is disabled\n");
		goto done;
	}

	m += scnprintf(text + m, text_size -m,
		"Num:%u\n",
		twt_sched->num);
	get_time_str_from_usec(twt_sched->sp, buf);
	m += scnprintf(text + m, text_size -m,
		"Period:%llu usec (%s)\n",
		twt_sched->sp,
		buf);
	get_time_str_from_usec(twt_sched->interval, buf);
	m += scnprintf(text + m, text_size -m,
		"Interval:%llu usec(%s)\n",
		twt_sched->interval,
		buf);
	m += scnprintf(text + m, text_size -m,
		"Mantissa: %u\n",
		twt_sched->mantissa);
	m += scnprintf(text + m, text_size -m,
		"Exponent: %u\n",
		twt_sched->exponent);


done:
	m = min_t(int, m, text_size);

	ret = simple_read_from_buffer(user_buf, count, ppos, text, m);
	kfree(text);

	return ret;
}

static ssize_t nrc_mac_twt_info_write(struct file *file, const char __user *buf,
                                size_t len, loff_t *ppos)
{
	printk("%s:%d\n", __FUNCTION__, __LINE__);
    return len;
}

static ssize_t nrc_mac_twt_schedule_read (struct file *file, char __user *user_buf,
                 size_t count, loff_t *ppos)
{
	struct nrc *nw = file->private_data;
	struct nrc_twt_sched *twt_sched = nw->twt_sched;

    char *text;
    int m, ret, text_size = 256;


	if (*ppos != 0) { return 0; }
    text = kmalloc(text_size, GFP_KERNEL);
    if (!text) {
        return -ENOMEM;
    }

	m = 0;
	if (!twt_sched) {
		m += scnprintf(text + m, text_size - m, "TWT is disabled\n");
		goto done;
	}

	mutex_lock(&twt_sched->mutex);

	if (twt_sched->started == false) {
		m += scnprintf(text + m, text_size - m, "TWT is not started\n");
		goto unlock;
	}

	m += scnprintf(text + m, text_size - m,
        "Current TSF: %llu\n", nrc_wim_get_tsf(nw, nw->vif[0]));

	m += scnprintf(text + m, text_size - m,
        "Count : %llu\n", twt_sched->sched_count);

	m += scnprintf(text + m, text_size - m, "Start TSF:%llu, Start Time:%lld\n", 
					twt_sched->start_tsf, twt_sched->start_time);

	m += scnprintf(text + m, text_size - m, "Base TSF:%llu, Base Time:%lld\n", 
					twt_sched->tsf, twt_sched->time);

	m += scnprintf(text + m, text_size - m, "Diff TSF:%lld, Diff Time:%lld\n", 
					twt_sched->tsf_diff, twt_sched->time_diff);

	m += scnprintf(text + m, text_size - m, "Diff Diff:%lld\n", 
					twt_sched->time_diff - twt_sched->tsf_diff);

unlock:
	mutex_unlock(&twt_sched->mutex);

done:
	m = min_t(int, m, text_size);

	ret = simple_read_from_buffer(user_buf, count, ppos, text, m);
	kfree(text);

	return ret;
}

static ssize_t nrc_mac_twt_schedule_write(struct file *file, const char __user *buf,
                                size_t len, loff_t *ppos)
{
	printk("%s:%d\n", __FUNCTION__, __LINE__);
    return len;
}

static ssize_t nrc_mac_twt_dump_read (struct file *file, char __user *user_buf,
                 size_t count, loff_t *ppos)
{
	struct nrc *nw = file->private_data;
	struct nrc_twt_sched *twt_sched = nw->twt_sched;

    char *text = NULL;
    int  m, ret = 0;


	if (*ppos != 0) { return 0; }

	m = 0;
	if (!twt_sched) {
		goto done;
	}
	if (twt_sched->started == false) {
		goto done;
	}

	m = nrc_twt_sched_entry_dump(nw, &text);
	if (m < 0) {
		return 0;
	}

done:
	ret = simple_read_from_buffer(user_buf, count, ppos, text, m);
	kfree(text);

	return ret;
}

static ssize_t nrc_mac_twt_dump_write(struct file *file, const char __user *buf,
                                size_t len, loff_t *ppos)
{
	printk("%s:%d\n", __FUNCTION__, __LINE__);
    return len;
}

static ssize_t nrc_mac_twt_mon_read (struct file *file, char __user *user_buf,
                 size_t count, loff_t *ppos)
{
	struct nrc *nw = file->private_data;
	struct nrc_twt_sched *twt_sched = nw->twt_sched;

    char *text = NULL;
    int  m, ret = 0;


	if (*ppos != 0) { return 0; }

	m = 0;
	if (!twt_sched) {
		goto done;
	}
	if (twt_sched->started == false) {
		goto done;
	}

	m = nrc_twt_sched_entry_monitor(nw, &text);
	if (m < 0) {
		return 0;
	}

done:
	ret = simple_read_from_buffer(user_buf, count, ppos, text, m);
	kfree(text);

	return ret;
}

static ssize_t nrc_mac_twt_mon_write(struct file *file, const char __user *buf,
                                size_t len, loff_t *ppos)
{
	printk("%s:%d\n", __FUNCTION__, __LINE__);
    return len;
}

static ssize_t nrc_mac_twt_debug_read (struct file *file, char __user *user_buf,
                 size_t count, loff_t *ppos)
{
	struct nrc *nw = file->private_data;
	struct nrc_twt_sched *twt_sched = nw->twt_sched;

    char *text = NULL;
    int  m, ret = 0, text_size = 128;


	if (*ppos != 0) { return 0; }
    text = kmalloc(text_size, GFP_KERNEL);
    if (!text) {
        return -ENOMEM;
    }

	m = 0;
	if (!twt_sched) {
		m += scnprintf(text + m, text_size - m, "TWT is disabled\n");
		goto done;
	}

	m += scnprintf(text + m, text_size - m,
        "Bit0: Dump TWT IE (%d)\n"  \
		"Bit1: Dump TWT Time (%d)\n",
		 test_bit(TWT_DEBUG_IE_FLAG, &twt_sched->debug_flags),
		 test_bit(TWT_DEBUG_TIME_FLAG, &twt_sched->debug_flags));

done:
	m = min_t(int, m, text_size);
	ret = simple_read_from_buffer(user_buf, count, ppos, text, m);
	kfree(text);

	return ret;
}

static ssize_t nrc_mac_twt_debug_write(struct file *file, const char __user *user_buf,
                                size_t count, loff_t *ppos)
{
	struct nrc *nw = file->private_data;
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
	char buf[16];

	if (!twt_sched) {
		return -EINVAL;
	}

	if (count >= sizeof(buf))
		return -EINVAL;

	if (copy_from_user(buf, user_buf, count))
		return -EFAULT;

	if (count && buf[count - 1] == '\n')
		buf[count - 1] = '\0';
	else
		buf[count] = '\0';

	if (kstrtoul(buf, 0, &twt_sched->debug_flags))
		return -EINVAL;

	return count;
}

static const struct file_operations nrc_mac_twt_info_ops = {
    .read = nrc_mac_twt_info_read,
    .write = nrc_mac_twt_info_write,
    .open = simple_open,
	.llseek = default_llseek,
};


static const struct file_operations nrc_mac_twt_schedule_ops = {
    .read = nrc_mac_twt_schedule_read,
    .write = nrc_mac_twt_schedule_write,
    .open = simple_open,
	.llseek = default_llseek,
};

static const struct file_operations nrc_mac_twt_dump_ops = {
    .read = nrc_mac_twt_dump_read,
    .write = nrc_mac_twt_dump_write,
    .open = simple_open,
	.llseek = default_llseek,
};

static const struct file_operations nrc_mac_twt_mon_ops = {
    .read = nrc_mac_twt_mon_read,
    .write = nrc_mac_twt_mon_write,
    .open = simple_open,
	.llseek = default_llseek,
};

static const struct file_operations nrc_mac_twt_debug_ops = {
    .read = nrc_mac_twt_debug_read,
    .write = nrc_mac_twt_debug_write,
    .open = simple_open,
	.llseek = default_llseek,
};

void nrc_mac_twt_debugfs_init (struct dentry *root, void *nw)  /* (struct nrc *nw) */
{
    debugfs_create_file("info", 0600, root, nw, &nrc_mac_twt_info_ops);
    debugfs_create_file("schedule", 0600, root, nw, &nrc_mac_twt_schedule_ops);
    debugfs_create_file("dump", 0600, root, nw, &nrc_mac_twt_dump_ops);
    debugfs_create_file("monitor", 0600, root, nw, &nrc_mac_twt_mon_ops);
    debugfs_create_file("debug", 0600, root, nw, &nrc_mac_twt_debug_ops);
}


