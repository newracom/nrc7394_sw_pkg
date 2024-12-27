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

#ifndef _NRC_TWT_SCHED_H_
#define _NRC_TWT_SCHED_H_

//#define TWT_SECOND 1000000UL  
#define TWT_MSEC	1000UL			/* usec */
#define TWT_SECOND (1000UL * TWT_MSEC)
#define TWT_MINUTE (60UL * TWT_SECOND)
#define TWT_HOUR (60UL * TWT_MINUTE)
#define TWT_DAY (24ULL * TWT_HOUR)

#define TIME_INTERVAL TWT_SECOND

struct nrc_twt_sched {
	bool started;
	unsigned long debug_flags;

	struct nrc *nw;
	struct ieee80211_vif *vif;

	u64 interval;
	u64 sp;
	u32 num;

	u16 mantissa;
	u8 exponent;

	u32 alloc_num;
	u32 alloc_index;
	struct twt_sched_entry *entries;
	u32 max_num_in_group;
	u32 min_num_in_group;
	u8 algo;

	u64 start_tsf;
	s64 start_time;
	u64 sched_count;
	
	u64 tsf;
	s64 time;
#if 1
	s64 tsf_diff;
	s64 time_diff;
#endif
	struct hrtimer timer;
	struct work_struct get_tsf_work;
	struct workqueue_struct *get_tsf_wq;
	struct mutex mutex;
};

struct twt_sched_stat {
	bool service;
	u64 start_time;

	u32 wake_nok;
	u32 wake_earlier;
	u32 wake_ok;
	u32 wake_later;
	s64 wake_nok_diff;

	u32 sleep_nok;
	u32 sleep_earlier;
	u32 sleep_ok;
	u32 sleep_later;
	s64 sleep_nok_diff;

	u64 avg_sp;
	u64 sp_count;
};

struct twt_flow_entry {
	u8 id;
	struct nrc_sta *sta;
	struct twt_sched_stat stat;
	struct list_head list;
};

struct twt_sched_entry {
	u32 index;
	u8 multi;

	u32 sub_num;
	struct list_head flow_entry_list;
};

enum twt_sched_algo {
	TWT_SCHED_ALGO_BALANCED    = 0,
	TWT_SCHED_ALGO_FCFS        = 1,
	TWT_SCHED_ALGO_MAX
};

struct nrc_twt_sched *nrc_twt_sched_init (struct nrc *nw, u64 sp, u32 num, u64 interval, u32 num_in_group, u8 algo);
void nrc_twt_sched_deinit (struct nrc *nw);
int nrc_twt_sched_start (struct nrc *nw, struct ieee80211_vif *vif);
void nrc_twt_sched_stop (struct nrc *nw, struct ieee80211_vif *vif);

int nrc_twt_sched_entry_add (struct nrc *nw, struct nrc_sta *sta, struct nrc_twt_flow *flow);
void nrc_twt_sched_entry_del (struct nrc *nw, struct nrc_sta *sta, u8 flowid);
void nrc_twt_sched_entry_del_all (struct nrc *nw, struct nrc_sta *sta);
int nrc_twt_sched_entry_dump (struct  nrc *nw, char **buf);
void get_time_str_from_usec (u64 usec, char *buf);

void nrc_twt_sched_start_update (struct nrc_twt_sched *sched, struct nrc_sta *sta, struct nrc_twt_flow *flow);
void nrc_twt_sched_end_update (struct nrc_twt_sched *sched, struct nrc_sta *sta, struct nrc_twt_flow *flow);
int nrc_twt_sched_entry_monitor (struct  nrc *nw, char **buf);

#endif
