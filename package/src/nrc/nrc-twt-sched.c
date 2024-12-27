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


#include <linux/kernel.h>
#include <linux/version.h>
#if KERNEL_VERSION(4, 8, 16) < LINUX_VERSION_CODE
#include <linux/bitfield.h>
#endif
#include <linux/ktime.h>
#include <net/mac80211.h>

#include "compat.h"
#include "nrc.h"
#include "wim.h"
#include "nrc-debug.h"
#include "nrc-mac80211-twt.h"
#include "nrc-twt-sched.h"

static u64 twt_get_tsf (struct nrc *nw, struct ieee80211_vif *vif);

#define DEBUG_TWT_TIME

static int twt_param_check (u64 *sp, u32 *num, u64 *interval, u32 num_in_group)
{
	u64 n;
	u32 rem;
	int ret = 0;

	if ((*sp != 0 && *num != 0 && *interval == 0)) {
		*interval = (*sp) * ((*num) / num_in_group);
	}
	else if (*sp != 0 && *num == 0 && *interval != 0) {
		*num = div64_u64_rem(*interval, *sp, &n) * num_in_group;
	}
	else if (*sp == 0 && *num != 0 && *interval != 0) {
		n = *interval;
		rem = do_div(n, *num / num_in_group);
		*sp = n;
		//printk("rem: %u\n", rem);
	} else {
		ret = -1;
	}

	return ret;
}

static int cal_twt_interval (u64 i, u16 *m, u8 *e) 
{
    // x: 16bit,  y: 5bit
    u16 maxX = (1 << 16) - 1;
    u8 maxY = (1 << 5) - 1;

    u64 bestX = 0, currentX = 0;
    u8  bestY = 0, currentY = 0;
    u64 minDiff = ULLONG_MAX;

	int ret = -1;

	//printk("i: %llu\n", i);
    for (currentY = 0; currentY <= maxY; ++currentY) {
        currentX = (i >> currentY);

        if (currentX >= 0 && currentX <= maxX) {
            u64 diff = abs((u64)(currentX << currentY) - i);
			//printk ("1currentY: %d,currentX: %llu, diff: %llu\n", currentY, currentX, diff);

            if (diff < minDiff) {
                minDiff = diff;
                bestX = currentX;
                bestY = currentY;
				ret = 0;
            }
			currentX += 1;
            diff = abs((u64)(currentX << currentY) - i);
			//printk ("2currentY: %d,currentX: %llu, diff: %llu\n", currentY, currentX, diff);
            if (diff <= minDiff) {
                minDiff = diff;
                bestX = currentX;
                bestY = currentY;
				ret = 0;
            }
        }
    }

    *m = bestX;
    *e = bestY;

	return ret;
}

//#define USE_TSF_FOR_NEXT_TWT
/* can't get tsf , since this is rx routine, can't do wim process */

#define OP_DELAY	1000 * 1000  /* 1 sec  (CPU + TX RX + WIM + etc) */

static inline u64 get_next_twt (struct nrc_twt_sched *s, struct twt_sched_entry *e)
{
	u64 twt;
#ifdef USE_TSF_FOR_NEXT_TWT 
	u64 cur_tsf;
#else
	u64 cur_time, base_time;
#endif
	u64 base_tsf = s->start_tsf + s->interval * s->sched_count;

	u64 offset = e->index * s->sp;
	u64 interval = 0;

#ifdef USE_TSF_FOR_NEXT_TWT
	cur_tsf = twt_get_tsf(s->nw, s->vif);

	if (cur_tsf + OP_DELAY > base_tsf + offset) {
		interval = s->interval;
	}
#else
	cur_time = ktime_to_us(ktime_get_boottime());
	base_time = s->time;

	if (cur_time + OP_DELAY > base_time + offset) {
		interval = s->interval;
	}
#endif

	twt = base_tsf + interval + offset;

#if 0
	//twt = base_tsf + interval + offset + 12000000; 	/* abnormal stat test, 10sec period */
	//twt = base_tsf + interval + offset + 7000000; 	/* later stat test, 10sec period */
	//twt = base_tsf + interval + offset - 7000000; 	/* earlier stat test, 10sec period */
	//twt = base_tsf + interval + offset - 12000000; 	/* abnormal stat test, 10sec period */
#endif

	return twt;
}


static struct twt_sched_entry *find_unused_twt_entry (struct nrc_twt_sched *s, u8 multi)
{
	struct twt_sched_entry *entries = s->entries;
	u32 num = s->num / s->max_num_in_group;
	struct twt_sched_entry *e;
	int i;

	switch (s->algo) {
		case TWT_SCHED_ALGO_BALANCED:
			if (s->min_num_in_group < s->max_num_in_group) {
				for (i = 0; i < num; i++) {
					e = entries + i;
					if (e->sub_num == s->min_num_in_group) {
						goto found;
					}
				}

				if (++s->min_num_in_group < s->max_num_in_group) {
					e = entries;
					i = 0;
					goto found;
				}
			}
			break;
		case TWT_SCHED_ALGO_FCFS:
			for (i = 0; i < num; i++) {
				e = entries + i;
				if (e->sub_num < s->max_num_in_group) {
					goto found;
				}
			}
			break;
		default:
			dev_err(s->nw->dev, "Invalid algo: %d\n", s->algo);
			return NULL;
	}

	dev_err(s->nw->dev, "No more unused entry, Total used: %u\n", s->alloc_num);
	return NULL;

found:
	s->alloc_index = (num * s->min_num_in_group) + i + 1;
	s->alloc_num ++;
	e->index = i;
	e->multi = multi;
	e->sub_num ++;

	dev_info(s->nw->dev, "Alloc TWT entry (index: %u, multi: %u), Total alloced: %u\n",
							i, multi, s->alloc_num);

	return e;
}

//#define TSF_ALIGN_TBTT
static u64 twt_get_tsf (struct nrc *nw, struct ieee80211_vif *vif)
{
	u64 tsf;
#ifdef TSF_ALIGH_TBTT
	u64 remain;
	u64 beacon_int = (u64)nw->beacon_int << 10;
#endif

	tsf = nrc_wim_get_tsf(nw, vif);
	//printk("TSF: %llu\n", tsf);
#ifdef TSF_ALIGH_TBTT 
	beacon_int = (u64)nw->beacon_int << 10;
	tsf = (div64_u64_rem(tsf, beacon_int, &remain) + 1) * beacon_int;
#endif
	//printk("ADJ TSF: %llu, remain: %llu\n", tsf, remain);

	return tsf;
}

static void get_tsf_worker (struct work_struct *work)
{
	struct nrc_twt_sched *twt_sched = container_of(work, struct nrc_twt_sched, get_tsf_work);
	struct nrc *nw = twt_sched->nw;
	u64 tsf;
	u64 time;

	mutex_lock(&twt_sched->mutex);

	tsf = twt_get_tsf(nw, twt_sched->vif);
	if (tsf == 0) { /* wim timeout with massive STAs */
		tsf = twt_sched->tsf + twt_sched->interval;
	}

	time = ktime_to_us(ktime_get_boottime());

	twt_sched->sched_count ++;

	twt_sched->tsf_diff = tsf - twt_sched->start_tsf - (twt_sched->interval * twt_sched->sched_count);
	twt_sched->time_diff = time - twt_sched->start_time - (twt_sched->interval * twt_sched->sched_count);

	if (test_bit(TWT_DEBUG_TIME_FLAG, &twt_sched->debug_flags)) {
		dev_info(twt_sched->nw->dev, "TSF DIFF: %lld, TIME DIFF: %lld DIFF SUM: %lld, CNT: %llu\n",
				twt_sched->tsf_diff,
				twt_sched->time_diff,
				twt_sched->time_diff- twt_sched->tsf_diff,
				twt_sched->sched_count);
	}

	twt_sched->tsf = tsf;
	twt_sched->time = time;

	if (hrtimer_active(&twt_sched->timer)) {
		ktime_t ktime = hrtimer_get_expires(&twt_sched->timer);
		hrtimer_cancel(&twt_sched->timer);
		if (twt_sched->tsf_diff < 0) {
			time = ktime_to_us(ktime_add_us(ktime, abs(twt_sched->tsf_diff)));
		} else {
			time = ktime_to_us(ktime_sub_us(ktime, twt_sched->tsf_diff));
		}
		hrtimer_start(&twt_sched->timer, ktime, HRTIMER_MODE_ABS);
	}
	mutex_unlock(&twt_sched->mutex);
}

void get_time_str_from_usec (u64 usec, char *buf)
{
	u64 days, hours, minutes, seconds, msecs, remainder;
	int n = 0;

	days = usec;

	//printk("%llu, %lu, %lu, %lu\n", TWT_DAY, TWT_HOUR, TWT_MINUTE, TWT_SECOND);
	days = div64_u64_rem(days, TWT_DAY, &hours);
    minutes = do_div(hours, TWT_HOUR);
    seconds = do_div(minutes, TWT_MINUTE);
    msecs = do_div(seconds, TWT_SECOND);
    remainder = do_div(msecs, TWT_MSEC);

	if (days > 0) {
		n += sprintf(buf + n, "%llddays ", days);
	}
	if (hours > 0) {
		n += sprintf(buf + n, "%lldhours ", hours);
	}
	if (minutes > 0) {
		n += sprintf(buf + n, "%lldminutes ", minutes);
	}
	if (seconds > 0) {
		n += sprintf(buf + n, "%lldseconds ", seconds);
	}
	if (msecs > 0) {
		n += sprintf(buf + n, "%lldmsecs ", msecs);
	}
	if (remainder > 0) {
		n += sprintf(buf + n, "%lldusecs", remainder);
	}
	//sprintf(buf + n, "%lldusecs", remainder);
}

static ktime_t get_ktime_from_interval (u64 interval)
{
	u64 sec;
	long nsec;
	ktime_t t;

	sec = interval;
	nsec = do_div(sec, TWT_SECOND);
	nsec = nsec * 1000;

	t = ktime_set(sec, nsec);

	return t;
}

static enum hrtimer_restart twt_sched_timer_handler(struct hrtimer *hrtimer)
{
	struct nrc_twt_sched *twt_sched = container_of(hrtimer, struct nrc_twt_sched, timer);
	//struct nrc *nw = twt_sched->nw;
	ktime_t t;
	//u64 tsf;

	if (0) return HRTIMER_NORESTART;
	//tsf = nrc_wim_get_tsf(nw, nw->vif[0]);
	queue_work(twt_sched->get_tsf_wq, &twt_sched->get_tsf_work);
									
	t = get_ktime_from_interval(twt_sched->interval);
	hrtimer_forward(hrtimer, hrtimer_get_expires(hrtimer), t);

	return HRTIMER_RESTART;
	
}

int nrc_twt_sched_entry_dump (struct  nrc *nw, char **buf)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
	struct twt_sched_entry *e;
	struct ieee80211_sta *sta;
	struct twt_flow_entry *fentry;
	int text_size;
	int aid;
	int i, m = 0;
	int r, c = 0;

	text_size = twt_sched->num * (4 + 1 + 3) + 10; /* 4digit + 1space + 3multiple and last \0 */
	*buf = kmalloc(text_size, GFP_KERNEL);
    if (!*buf) {
        return -ENOMEM;
    }

	mutex_lock(&twt_sched->mutex);

	for (i = 0; i < twt_sched->num / twt_sched->max_num_in_group; i++) {
		e = twt_sched->entries + i;
		list_for_each_entry(fentry, &e->flow_entry_list, list) {
			if (fentry->sta) {
				sta = to_ieee80211_sta(fentry->sta);
				aid = sta->aid;
			} else {
				aid = 0;
			}

			m += scnprintf(*buf + m, text_size - m, "%4d", aid);
			if (e->multi > 1) {
				m += scnprintf(*buf + m, text_size - m, "(%d)", e->multi);
			}
			else {
				m += scnprintf(*buf + m, text_size - m, "   ");
			}
			c++;
		}

		for (r = twt_sched->max_num_in_group - e->sub_num; r > 0; r--) {
			m += scnprintf(*buf + m, text_size - m, "%4d   ", 0);
			c++;
		}

		if (c >= 10) {
			m += scnprintf(*buf + m, text_size -m, "\n");
			c = 0;
		}
		else {
			m += scnprintf(*buf + m, text_size -m, " ");
		}
	}

	mutex_unlock(&twt_sched->mutex);

	m += scnprintf(*buf + m, text_size -m, "\n");

	return m + 1;
}

void nrc_twt_sched_entry_del_all (struct nrc *nw, struct nrc_sta *sta)
{
	int i;

	for (i = 0; i < NRC_MAX_STA_TWT_AGRT; i++) {
		nrc_twt_sched_entry_del(nw, sta, i);
	}
}

	
void nrc_twt_sched_entry_del (struct nrc *nw, struct nrc_sta *sta, u8 flowid)
{
	struct nrc_twt_sched *s = nw->twt_sched;
	struct nrc_twt_flow *flow;
	struct twt_sched_entry *e;
	struct twt_flow_entry *fentry, *t_fentry;
	struct ieee80211_sta *m_sta;

	if (s == NULL) return;

	mutex_lock(&s->mutex);

	if (!s->started) {
		goto unlock;
	}

	if (flowid >= ARRAY_SIZE(sta->twt.flow))
		goto unlock;

	if (!(sta->twt.flowid_mask & BIT(flowid)))
		goto unlock;                                      

	flow = &sta->twt.flow[flowid];

	e = flow->entry;
	list_for_each_entry_safe(fentry, t_fentry, &e->flow_entry_list, list) {
		if (fentry->sta == sta) {
			s->alloc_num --;
			e->sub_num --;
			if (e->sub_num < s->min_num_in_group) {
				s->min_num_in_group = e->sub_num;
			}
			m_sta = to_ieee80211_sta(sta);
			dev_info(s->nw->dev, "Delete TWT flow (aid: %u flowid: %u), Total used: %u\n",
									m_sta->aid, flowid, e->sub_num);
			list_del(&fentry->list);
			kfree(fentry);
			break;
		}
	}

	sta->twt.flowid_mask &= ~BIT(flowid);

	if (e->sub_num == 0) {
		e->multi = 0;
		dev_info(s->nw->dev, "Delete TWT entry (index: %u), Total used: %u\n",
								e->index, s->alloc_num);
	}

unlock:
	mutex_unlock(&s->mutex);
}

#define MAX_MULTI		8

int nrc_twt_sched_entry_add (struct nrc *nw, struct nrc_sta *sta, struct nrc_twt_flow *flow)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
	struct twt_sched_entry *e;
	struct twt_flow_entry *fentry;

	u16 mantissa = twt_sched->mantissa;
	u8 exp = twt_sched->exponent;
	u64 interval = twt_sched->interval;
	u64 flow_interval;
	u64 multi, remain;
	u64 bss_max_idle;

	int ret = 0;

	if (twt_sched == NULL) {
		return -1;
	}

	mutex_lock(&twt_sched->mutex);

	if (!twt_sched->started) {
		ret = -1;
		goto unlock;
	}

	flow_interval = (u64)flow->mantissa << flow->exp;

	multi = div64_u64_rem(flow_interval, interval, &remain);
	printk("sched: %llu, flow: %llu, multi: %llu\n", twt_sched->interval, flow_interval, multi);

	/* set max multi shorter than bss max idle */
	bss_max_idle = ((u64)sta->max_idle.idle_period << 10) * 1000;
	if (bss_max_idle != 0 && bss_max_idle < flow_interval) {
		dev_err(nw->dev, "TWT Interval must be shorter than BSS Max Idle (bss:%llu, int:%llu)\n",
			bss_max_idle, flow_interval);
		while (multi > 1) {
			multi--;
			flow_interval -= interval;
			if (flow_interval <= bss_max_idle) {
				break;
			}
		}
		ret = 1;
	}

	if (multi > MAX_MULTI) {
		dev_err(nw->dev, "The max supported multiple is over (%llu)\n", multi);
		multi = MAX_MULTI;
		ret = 1;
		goto done;
	}

	/* multi must be power of two */
	while ((multi > 1) && !!(multi & (multi - 1))) {
		multi--;
		ret = 1;
	}

	if (ret != 0) {
		goto done;
	}

	if (remain != 0) {
		ret = 1;
		goto done;
	}

	e = find_unused_twt_entry(twt_sched, (u8)multi);
	if (!e) {
		ret = -1;
		goto done;
	}

	fentry = (struct twt_flow_entry *)kzalloc(sizeof(struct twt_flow_entry), GFP_KERNEL);
	if (!fentry) {
		dev_err(nw->dev, "alloc failed\n");
		ret = -1;
		goto done;
	}
	fentry->id = flow->id;
	fentry->sta = sta;
	list_add_tail(&fentry->list, &e->flow_entry_list);

	/* return flow */
	flow->entry = e;
	flow->twt = get_next_twt(twt_sched, e);
	sta->twt.flowid_mask |= BIT(flow->id);
done:
	flow->mantissa = mantissa;
	for (; multi > 1; multi >>= 1) {
		exp++;
	}
	flow->exp = exp;
unlock:
	mutex_unlock(&twt_sched->mutex);
	return ret;
}

struct nrc_twt_sched *nrc_twt_sched_init (struct nrc *nw, u64 sp, u32 num, u64 interval,
	u32 num_in_group, u8 algo)
{
	struct nrc_twt_sched *twt_sched = NULL;
	struct twt_sched_entry *entries = NULL;
	char buf[128];
	int ret = 0;
	int i = 0;

	//u64 interval = sp * num;
	u16 mantissa = 0;
	u8 exponent = 0;
#if 0
	u32 exp_result;
#endif

	ret = twt_param_check(&sp, &num, &interval, num_in_group);
	if (ret) {
		dev_err(nw->dev, "Invalid TWT Params (Interval: %llu, Service Period: %llu, Service Number: %u Group Number: %u)\n",
							interval, sp, num, num_in_group);
		goto fail;
	}

	dev_info(nw->dev, "Initializing TWT (Interval: %llu, Service Period: %llu, Service Number: %u Group Number: %u)\n",
						interval, sp, num, num_in_group);

	get_time_str_from_usec(interval, buf);
	dev_info(nw->dev, "TWT Interval: %s\n", buf);

	ret = cal_twt_interval(interval, &mantissa, &exponent);
	if (ret) {
		dev_err(nw->dev, "Invalid TWT Params Size (Interval: %llu, Service Period: %llu, Service Number: %u Group Number: %u)\n",
							interval, sp, num, num_in_group);
		goto fail;
	}
	dev_info(nw->dev, "TWT Mantissa: %u, Exponent: %u\n", mantissa, exponent);

#if 0
	exp_result = 1 << exponent;
	interval = (u64)((u64)mantissa * (exp_result));
#else
	interval = (u64)(mantissa) << exponent;
#endif
	get_time_str_from_usec(interval, buf);
	dev_info(nw->dev, "TWT Real Interval: %llu, %s\n", interval, buf);

	entries = (struct twt_sched_entry *)kzalloc(sizeof(*entries) * (num / num_in_group), GFP_KERNEL);
	if (!entries) {
		dev_err(nw->dev, "alloc failed\n");
		goto fail;
	}
	for (i = 0; i < num / num_in_group; i++) {
		struct twt_sched_entry *e = entries + i;
		INIT_LIST_HEAD(&e->flow_entry_list);
	}

	twt_sched = (struct nrc_twt_sched *)kzalloc(sizeof(*twt_sched), GFP_KERNEL);
	if (!twt_sched) {
		dev_err(nw->dev, "alloc failed\n");
		goto fail;
	}

	twt_sched->interval = interval;
	twt_sched->sp = sp;
	twt_sched->num = num;
	twt_sched->mantissa = mantissa;
	twt_sched->exponent = exponent;
	twt_sched->max_num_in_group = num_in_group;
	if (algo >= TWT_SCHED_ALGO_MAX) {
		dev_err(nw->dev, "sched algo %d not supported\n", algo);
		goto fail;
	} else {
		twt_sched->algo = algo;
		dev_info(nw->dev, "TWT Scheduling Algorithm: %s\n",
			algo == TWT_SCHED_ALGO_BALANCED ? "Balanced" : "FCFS");
	}


	twt_sched->entries = entries;

	hrtimer_init(&twt_sched->timer, CLOCK_BOOTTIME, HRTIMER_MODE_REL);
	twt_sched->timer.function = twt_sched_timer_handler;

	twt_sched->get_tsf_wq = alloc_workqueue("get_tsf_wq", WQ_HIGHPRI, 0);
	if (twt_sched->get_tsf_wq == NULL) {
		goto fail;
	}
	INIT_WORK(&twt_sched->get_tsf_work, get_tsf_worker);

	mutex_init(&twt_sched->mutex);

	twt_sched->nw = nw;

	return twt_sched;
fail:
	kfree(entries);
	kfree(twt_sched);
	dev_err(nw->dev, "Failed to initialize TWT, disabled\n");
	return NULL;
}

void nrc_twt_sched_deinit (struct nrc *nw)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;

	if (twt_sched == NULL) return;

	if (twt_sched->started) {
		nrc_twt_sched_stop(nw, NULL); 
	}

	if (twt_sched->get_tsf_wq != NULL) {
		flush_workqueue(twt_sched->get_tsf_wq);
		destroy_workqueue(twt_sched->get_tsf_wq);
	}

	if (twt_sched->entries) {
		kfree(twt_sched->entries);
	}

	nw->twt_sched = NULL;

	kfree(twt_sched);
}

int nrc_twt_sched_start (struct nrc *nw, struct ieee80211_vif *vif)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
	struct nrc_vif *i_vif = to_i_vif(vif);
	ktime_t t;
	u64 bss_max_idle = ((u64)i_vif->max_idle_period << 10) * 1000;

	if (twt_sched == NULL) return 0;

	mutex_lock(&twt_sched->mutex);

	if (twt_sched->started) {
		goto unlock;
	}

	if (bss_max_idle != 0 && bss_max_idle < twt_sched->interval) {
		dev_err(nw->dev, "Failed to start TWT. BSS Max Idle (%llu) < TWT Interval (%llu)\n",
				bss_max_idle, twt_sched->interval);
		goto unlock;
	}

	twt_sched->start_tsf = twt_sched->tsf = twt_get_tsf(nw, vif);
	if (twt_sched->start_tsf == 0) {
		dev_err(nw->dev, "Failed to get start TSF\n");
		goto unlock;
	}

	dev_info(nw->dev, "TWT Start\n");

	twt_sched->start_time = twt_sched->time = ktime_to_us(ktime_get_boottime());
	twt_sched->sched_count = 0;

	twt_sched->tsf_diff = 0; 
	twt_sched->time_diff = 0;

	twt_sched->vif = vif;

	t = get_ktime_from_interval(twt_sched->interval);
	hrtimer_start(&twt_sched->timer, t, HRTIMER_MODE_REL);

	dev_info(nw->dev, "TSF:%llu, KTIME:%lld, DIFF:%llu\n", 
						twt_sched->tsf,
						twt_sched->time,
						twt_sched->time - twt_sched->tsf);

	twt_sched->started = true;

	nw->twt_requester = false;
	nw->twt_responder = true;

unlock:
	mutex_unlock(&twt_sched->mutex);

	return 0;
}

void nrc_twt_sched_stop (struct nrc *nw, struct ieee80211_vif *vif)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;

	if (twt_sched == NULL) return;

	mutex_lock(&twt_sched->mutex);

	if (!twt_sched->started) {
		goto unlock;
	}

	if (twt_sched->vif != vif) {
		goto unlock;
	}

	dev_info(nw->dev, "TWT Stop\n");

	hrtimer_cancel(&twt_sched->timer);
	cancel_work_sync(&twt_sched->get_tsf_work);

	twt_sched->vif = NULL;
	twt_sched->started = false;

	nw->twt_responder = false;
unlock:
	mutex_unlock(&twt_sched->mutex);
}

#define SP_MARGIN		(1000 * 1000)		/* 1sec */

void nrc_twt_sched_start_update (struct nrc_twt_sched *sched, struct nrc_sta *sta, struct nrc_twt_flow *flow)
{
	struct twt_sched_stat *stat;
	struct twt_flow_entry *fentry;

	u64 cur_time, sched_time, sp;
	s64 diff_time, abs_diff_time;

	list_for_each_entry(fentry, &flow->entry->flow_entry_list, list) {
		if (fentry->sta == sta && fentry->id == flow->id) {
			stat = &fentry->stat;

			if (stat->service) {
				return;
			}
			stat->service = true;

			cur_time = ktime_to_us(ktime_get_boottime());
			sched_time = sched->time + flow->entry->index * sched->sp;
			if (flow->entry->index == 0 && (cur_time - sched_time > sched->interval - sched->sp)) { /* before update, first entry */
				sched_time += sched->interval;
			}
			diff_time = sched_time - cur_time;
			abs_diff_time = abs(diff_time);
			sp = sched->sp;

			stat->start_time = cur_time;

			if (abs_diff_time > sp ) {
				stat->wake_nok ++;
				stat->wake_nok_diff = diff_time;
			}
			else if (abs_diff_time <= SP_MARGIN) {
				stat->wake_ok ++;
			}
			else if (diff_time > 0) {
				stat->wake_earlier ++;
			}
			else {
				stat->wake_later ++;
			}
			break;
		}
	}
}

void nrc_twt_sched_end_update (struct nrc_twt_sched *sched, struct nrc_sta *sta, struct nrc_twt_flow *flow)
{
	struct twt_sched_stat *stat;
	struct twt_flow_entry *fentry;

	u64 cur_time, sched_time, sp;
	s64 diff_time, abs_diff_time;
	u64 remain;
	u64 count;

	list_for_each_entry(fentry, &flow->entry->flow_entry_list, list) {
		if (fentry->sta == sta && fentry->id == flow->id) {
			stat = &fentry->stat;

			if (!stat->service) {
				return;
			}
			stat->service = false;

			cur_time = ktime_to_us(ktime_get_boottime());
			sched_time = sched->time + flow->entry->index * sched->sp + sched->sp;
			if ((flow->entry->index == (sched->num / sched->max_num_in_group) - 1) && (sched_time - cur_time > sched->interval - sched->sp)) { /* after update, last entry */
				sched_time -= sched->interval;
			}
			diff_time = sched_time - cur_time;
			abs_diff_time = abs(diff_time);
			sp = sched->sp;

			count = stat->sp_count ++;

			stat->avg_sp = div64_u64_rem(stat->avg_sp * count + (cur_time - stat->start_time),
									count + 1, &remain);
			stat->start_time = 0;

			if (abs_diff_time > sp ) {
				stat->sleep_nok ++;
				stat->sleep_nok_diff = diff_time;
			}
			else if (abs_diff_time <= SP_MARGIN) {
				stat->sleep_ok ++;
			}
			else if (diff_time > 0) {
				stat->sleep_earlier ++;
			}
			else {
				stat->sleep_later ++;
			}
			break;
		}
	}
}

int nrc_twt_sched_entry_monitor (struct  nrc *nw, char **buf)
{
	struct nrc_twt_sched *twt_sched = nw->twt_sched;
	struct twt_sched_entry *e;
	struct twt_sched_stat *s;
	struct ieee80211_sta *sta;
	int text_size;
	int aid;
	int i, m = 0;

	text_size = (twt_sched->num + 1) * (17 + 4 + 4 + 2 + (10 * 12) + 10); /* 17mac + 4aid + 4idx + 2space + (9digit + 1space) * 12  and last \0 */
	*buf = kmalloc(text_size, GFP_KERNEL);
    if (!*buf) {
        return -ENOMEM;
    }

	m += scnprintf(*buf + m, text_size -m, "%17s", "MAC address");
	m += scnprintf(*buf + m, text_size -m, " %4s", "AID");
	m += scnprintf(*buf + m, text_size -m, " %4s", "SLOT");
	m += scnprintf(*buf + m, text_size -m, " %9s", "Avg-SP");
	m += scnprintf(*buf + m, text_size -m, " %9s", "Cnt-SP");
	m += scnprintf(*buf + m, text_size -m, " %9s", "W-OK");
	m += scnprintf(*buf + m, text_size -m, " %9s", "S-OK");
	m += scnprintf(*buf + m, text_size -m, " %9s", "W-Earlier");
	m += scnprintf(*buf + m, text_size -m, " %9s", "S-Earlier");
	m += scnprintf(*buf + m, text_size -m, " %9s", "W-Later");
	m += scnprintf(*buf + m, text_size -m, " %9s", "S-Later");
	m += scnprintf(*buf + m, text_size -m, " %9s", "W-NOK");
	m += scnprintf(*buf + m, text_size -m, " %9s", "S-NOK");
	m += scnprintf(*buf + m, text_size -m, " %9s", "W-Diff");
	m += scnprintf(*buf + m, text_size -m, " %9s", "S-Diff");
	m += scnprintf(*buf + m, text_size - m, "\n");

	mutex_lock(&twt_sched->mutex);

	for (i = 0; i < twt_sched->num / twt_sched->max_num_in_group; i++) {
		e = twt_sched->entries + i;
		if (e->sub_num) {
			struct twt_flow_entry *fentry;
			list_for_each_entry(fentry, &e->flow_entry_list, list) {
				s = &fentry->stat;
				sta = to_ieee80211_sta(fentry->sta);
				aid = sta->aid;
				m += scnprintf(*buf + m, text_size -m, "%pM", sta->addr);
				m += scnprintf(*buf + m, text_size - m, " %4d", aid);
				m += scnprintf(*buf + m, text_size - m, " %4d", i + 1);
				m += scnprintf(*buf + m, text_size - m, " %9llu", s->avg_sp);
				m += scnprintf(*buf + m, text_size - m, " %9llu", s->sp_count);
				m += scnprintf(*buf + m, text_size - m, " %9u", s->wake_ok);
				m += scnprintf(*buf + m, text_size - m, " %9u", s->sleep_ok);
				m += scnprintf(*buf + m, text_size - m, " %9u", s->wake_earlier);
				m += scnprintf(*buf + m, text_size - m, " %9u", s->sleep_earlier);
				m += scnprintf(*buf + m, text_size - m, " %9u", s->wake_later);
				m += scnprintf(*buf + m, text_size - m, " %9u", s->sleep_later);
				m += scnprintf(*buf + m, text_size - m, " %9u", s->wake_nok);
				m += scnprintf(*buf + m, text_size - m, " %9u", s->sleep_nok);
				m += scnprintf(*buf + m, text_size - m, " %9lld", s->wake_nok_diff);
				m += scnprintf(*buf + m, text_size - m, " %9lld", s->sleep_nok_diff);
				m += scnprintf(*buf + m, text_size - m, "\n");
			}
		} else {
			continue;
		}
	}

	mutex_unlock(&twt_sched->mutex);

	return m + 1;
}
