/*
 * MIT License
 *
 * Copyright (c) 2020 Newracom, Inc.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 *
 */

#ifndef _CLI_KEY_LIST_H_
#define _CLI_KEY_LIST_H_

/* cli list version */
#define CLI_APP_LIST_VERSION			3

/* show version */
#define SHOW_VERSION_DISP		"%02u.%02u.%02u,%s,%s,%s"
#define SHOW_VERSION_KEY_LIST	"Newracom Firmware Version,gerrit/master,Board Revision,Description"

/* show config */
#define SHOW_CONFIG_DISP "%s,%s,%s,\
%s,%s,%s,%d,%d,%s,\
%d (%d),%d (%d),\
%d,%s,%s,%s,%s,%s,\
%s,%s,%s,%s,\
%s,%s,%s,%s,%s,%s,%s,\
0x%x,%s,%s,%d,%s,%s,%s,\
%s,%s,%s,%s,%s,\
%s,%s,%s,%s,%s,%d,%s"


#define SHOW_CONFIG_KEY_LIST "[MAC Configuration]\nBoot Mode,Device Mode,MAC Address,\
Country,Bandwidth, - PRI CH BW, - PRI CH LO, - PRI CH NUM, - Center LO,\
Frequency,MAC80211_freq,\
Default MCS,Rate Control, - Mode, - Info, - MCS10(MGMT),Guard Interval,\
Security,  - AKM,  - Cipher,  - Type,\
RTS, - threshold, - rid,CTS,Format,Preamble type,Promiscuous Mode,\
Color,RF,Auto CFO Cal,TX Retry Limit,Fragment,Defragment,PS pretend,\
Bitmap Encoding,Reverse Scrambler,Power Save, - type, - duration,\
BSSID,AID,Scan Type,Scan Mode,\n[PHY Configuration]\nTx Gain,Base Rx Gain,Tx Power Type"

/* show edca */
#define SHOW_EDCA_DISP		"%d,%d,%d,%d,\
%d,%d,%d,%d,%d"
#define SHOW_EDCA_KEY_LIST	"[AC], - priority, - aggregation, - max agg num,\
 - aifsn, - cw min, - cw max, - txop limit, - txop max"

/* show stats simple_rx */
#define SHOW_STATS_SIMPLE_RX_DISP		"%d,%lu,%lu,%lu,%lu,%lu"
#define SHOW_STATS_SIMPLE_RX_KEY_LIST	"RSSI,CS_Cnt,PSDU_Succ,MPDU_Rcv,MPDU_Succ,SNR"

/* show signal */
#define SHOW_SIGNAL_DISP		"%d,%d"
#define SHOW_SIGNAL_KEY_LIST	"MAC addr,rssi,snr"

/* show temp */
#define SHOW_TEMPERATURE_DISP		"%s"
#define SHOW_TEMPERATURE_KEY_LIST	"Temperature"

/* set maxagg */
#define SET_MAXAGG_DISP		"%s,%s,%d,%d"
#define SET_MAXAGG_KEY_LIST	"AC,State,Value,Size"

/* show mac [tx|rx] */
// show mac [tx|rx] stats st
#define SHOW_MAC_TRX_STATS_ST_DISP	"%lu,%lu,%lu,%lu"

// show mac [tx|rx] stats ac
#define SHOW_MAC_TRX_STATS_AC_DISP	"%s,%lu,%lu,%lu,%lu"

// show mac [tx|rx] stats type
#define SHOW_MAC_TRX_STATS_TYPE_DISP	"%s,%lu,%lu,%lu,%lu"

// show mac [tx|rx] stats mcs
#define SHOW_MAC_TRX_STATS_MCS_DISP	"%d,%lu,%lu,%lu,%lu,%lu"


/* show uinfo {vif_id} */
// show uinfo {vif_id} ap
#define SHOW_UINFO_AP_DISP_1	"%02x:%02x:%02x:%02x:%02x:%02x,%s,%d,%d,%d"
#define SHOW_UINFO_AP_DISP_2	",%d,%d,%lx,%lu,%d,%d,%d,%d,\
%d,%d,%d,%d,%d,%d,\
%d,%d,%d,%x,%d"

#define SHOW_UINFO_AP_KEY_LIST	"bssid,ssid,ssid_len,security,beacon_interval,\
short_bi,assoc_s1g_channel,cssid,change_seq_num,Support\n s1g_long, pv1, nontim, twt,\
 ampdu, ndp_pspoll, traveling pilot, short gi\n  -1mhz,  -2mhz,  -4mhz,\
max mpdu_len,ampdu_len_exp,min mpdu_start_spacing,rx_s1gmcs_map,color"

// show uinfo {vif_id} sta
#define SHOW_UINFO_STA_DISP_1	"%02x:%02x:%02x:%02x:%02x:%02x,%d,%lu"
#define SHOW_UINFO_STA_DISP_2	",%d,%d,%d,%d,%d,\
%d,%d,%d,%d,%d,%d,\
%d,%d,%x"

#define SHOW_UINFO_STA_KEY_LIST	"mac_addr,aid,listen_interval\
,Support\n s1g_long, pv1, nontim, twt, ampdu,\
 ndp_pspoll, traveling pilot, short gi\n  -1mhz,  -2mhz,  -4mhz,max mpdu_len,\
ampdu_len_exp,min mpdu_start_spacing,rx_s1gmcs_map"

// set tx_time
#define SET_TXTIME_KEY_DISP	"%ldus,%ldus,%ldus"
#define SET_TXTIME_KEY_LIST	"CS time,Pause time,Resume time"

/* set config <ack[0,1]> <agg[0,1]> <mcs> */
#define SET_CONFIG_KEY_DISP	"%s,%s,%d"
#define SET_CONFIG_KEY_LIST	"Ack,Aggregation,Mcs"

/* set rc <on|off> [vif_id] [mode] */
#define SET_RC_KEY_DISP	"%s,%d,%s"
#define SET_RC_KEY_LIST	"rc,vif_id,mode"

/* set rc_param <EWMA value> <Update interval value> */
#define SET_RC_PARAM_KEY_DISP	"%d,%d,%d"
#define SET_RC_PARAM_KEY_LIST	"EWMA value,Update interval value,Probe interval value"

/* set duty <on|off> {duty window} {tx duration in duty window} {duty margin}*/
#define SET_DUTY_KEY_DISP	"%s,%lu,%lu,%lu"
#define SET_DUTY_KEY_LIST	"Duty cycle,Duty window,Tx duration,Duty margin"

/* show duty */
#define SHOW_DUTY_KEY_DISP	"%s,%lu,%lu,%lu,%lu"
#define SHOW_DUTY_KEY_LIST	"Duty cycle,Duty window,Tx duration,Remain tx duration,Duty error"

/* show autotxgain */
#define SHOW_AUTOTXGAIN_KEY_DISP	"%s,%d,%d,%d,%d,%d,%d,%d,%d,%d"
#define SHOW_AUTOTXGAIN_KEY_LIST	"Auto txgain,Tx power index for MCS 0,Tx power index for MCS 1,\
Tx power index for MCS 2,Tx power index for MCS 3,Tx power index for MCS 4,Tx power index for MCS 5,\
Tx power index for MCS 6,Tx power index for MCS 7,Tx power index for MCS 10"

/* show cal_use */
#define SHOW_CAL_USE_KEY_DISP	"%s,%s"
#define SHOW_CAL_USE_KEY_LIST	"Calibration_use,Country"

// show sta {vif_id} all
#define SHOW_STA_AID_DISP	"%02x:%02x:%02x:%02x:%02x:%02x,%d,%s"
#define SHOW_STA_AID_KEY_LIST	"mac_addr,aid,state"

#define SHOW_STA_ALL_DISP	"%d,%02x:%02x:%02x:%02x:%02x:%02x,%d,%s"
#define SHOW_STA_ALL_KEY_LIST	"num,mac_addr,aid,state"

/* show recovery stats */
#define SHOW_RECOVERY_STATS_KEY_DISP1	"%lu"
#define SHOW_RECOVERY_STATS_KEY_DISP2	"%d,%lu,%d"

/* show detection stats */
#define SHOW_DETECTION_STATS_KEY_DISP1	"%d,%d"
#define SHOW_DETECTION_STATS_KEY_DISP2	"%d,%d(%d),%d,%d,%d"

/* show tx_time */
#define SHOW_TX_TIME_KEY_DISP	"%ld,%ld,%ld"
#define SHOW_TX_TIME_KEY_LIST	"CS time,Pause time,Resume time"

/* set txpwr */
#define SET_TXPWR_KEY_DISP	"%s,%d"
#define SET_TXPWR_KEY_LIST	"Type,Tx power"

/* show wakeup_pin */
#define SHOW_WAKEUP_PIN_DISP		"%s,%d"
#define SHOW_WAKEUP_PIN_KEY_LIST	"Debounce,Pin number"

/* set wakeup_pin */
#define SET_WAKEUP_PIN_DISP		"%s,%d"
#define SET_WAKEUP_PIN_KEY_LIST	"Debounce,Pin number"

/* show wakeup_source */
#define SHOW_WAKEUP_SOURCE_DISP		"%s"
#define SHOW_WAKEUP_SOURCE_KEY_LIST	"Wakeup source"

/* set wakeup_source */
#define SET_WAKEUP_SOURCE_DISP		"%s"
#define SET_WAKEUP_SOURCE_KEY_LIST	"Wakeup source"

/* set drop */
#define SET_DROP_DISP		"%d,%s,%s"
#define SET_DROP_KEY_LIST	"vif_id,mac_addr,on"

/* set ack_mode */
#define SET_ACK_MODE_DISP	"%s"
#define SET_ACK_MODE_LIST	"ACK_MODE"

/* show xtal status */
#define SHOW_XTAL_STATUS_DISP "%d"
#define SHOW_XTAL_STATUS_LIST "XTAL status"

/* show rc */
#define SHOW_RC_KEY_DISP	"%2d %2d,%2d %2d,%2d %2d,%2d %2d"
#define SHOW_RC_KEY_LIST	" maxtp, tp2, maxp, lowest"

/* show rc_param */
#define SHOW_RC_PARAM_KEY_DISP	"%ld,%d,%d"
#define SHOW_RC_PARAM_KEY_LIST	"EWMA(%),Update interval(ms),Probe interval(ms)"

/* show rxgain_table */
#define SHOW_RXGAIN_TABLE_KEY_DISP	"%x"
#define SHOW_RXGAIN_TABLE_KEY_LIST	"idx,"

#endif /* _CLI_KEY_LIST_H_ */
