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

#include "nrc.h"
#include "nrc-mac80211.h"
#include "nrc-hif.h"
#include "wim.h"
#include "nrc-ps.h"

static char *ps_mode_str[] = {
    "ACTIVE",
    "MODEMSLEEP",
    "DEEPSLEEP_TIM",
    "DEEPSLEEP_NONTIM",
};

static char *nrc_ps_mode_str (enum NRC_PS_MODE mode)
{
    BUG_ON(mode >= NRC_PS_MAX);

    return ps_mode_str[mode];
}

int nrc_ps_set_mode (struct nrc *nw, enum NRC_PS_MODE mode, int timeout)
{
	struct ieee80211_hw *hw = nw->hw;
	int ret = 0;

	dev_info(nw->dev, "Entering PS mode:%s\n", 
						nrc_ps_mode_str(mode));

	if (mode == NRC_PS_NONE) {
		ret = nrc_hif_wake_target(nw->hif, timeout);
		goto done;
	}

	if (nw->drv_state == NRC_DRV_CLOSING)
		goto done;

	if (!disable_cqm) {
		try_to_del_timer_sync(&nw->bcn_mon_timer);
	}

	ieee80211_stop_queues(hw);

#ifdef CONFIG_USE_TXQ
	nrc_cleanup_txq_all(nw);
#endif

	nrc_hif_sleep_target_start(nw->hif, mode);

	ret = nrc_wim_set_ps(nw, mode, timeout);
	
	nrc_hif_suspend(nw->hif);

	ieee80211_wake_queues(hw);

	nrc_hif_sleep_target_end(nw->hif, mode);

done:
	return ret;
}
