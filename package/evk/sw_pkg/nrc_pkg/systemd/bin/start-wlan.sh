#!/bin/sh

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# halow-wlan.service ExecStart: bring up the HaLow stack. Configuration
# comes from /etc/halow/halow.conf (loaded by systemd as EnvironmentFile).
#
# STA_TYPE selects the operating mode (positional #1 of both start scripts):
#   0 STA | 1 AP | 2 SNIFFER | 3 RELAY | 4 MESH
# The modular stack (nrc_spi/core/wlan+mcp via start_modular.py) exists for
# the Unified AP - the mode where the chip also runs the MCP/ESL engine.
# Every other mode runs the stock start.py flow on the monolithic nrc.ko:
#   unified-ap  = STA_TYPE=1 + DRIVER_STACK=modular
#   plain ap    = STA_TYPE=1 + DRIVER_STACK=nrc
#   sta/sniffer/relay/mesh (STA_TYPE!=1) = start.py regardless of
#   DRIVER_STACK, so configs from older packages migrate by themselves.
set -e

[ -n "$NRC_SCRIPT_DIR" ] || { echo "NRC_SCRIPT_DIR not set"; exit 1; }
cd "$NRC_SCRIPT_DIR"

MODE=${STA_TYPE:-1}

# GUI-persisted options -> key=value args. Both start scripts speak this
# form (start.py via its apply_kv_args parity hook); empty values are
# skipped, leaving the script-header defaults in charge.
KVARGS=""
for kv in \
    max_cpuclock="$MAX_CPUCLOCK" \
    guard_int="$GUARD_INT" \
    short_bcn_enable="$SHORT_BCN_ENABLE" \
    legacy_ack_enable="$LEGACY_ACK_ENABLE" \
    ndp_ack_1m="$NDP_ACK_1M" \
    ampdu_enable="$AMPDU_ENABLE" \
    bss_max_idle_enable="$BSS_MAX_IDLE_ENABLE" \
    bss_max_idle="$BSS_MAX_IDLE" \
    cca_threshold="$CCA_THRESHOLD" \
    power_save_pretend="$POWER_SAVE_PRETEND" \
    channel_manager="$CHANNEL_MANAGER" \
    channel_manager_profile="$CHANNEL_MANAGER_PROFILE" \
    channel_manager_prefer_bw="$CHANNEL_MANAGER_PREFER_BW" \
    channel_manager_dwell="$CHANNEL_MANAGER_DWELL" \
    channel_manager_exclude="$CHANNEL_MANAGER_EXCLUDE" \
    tpc="$TPC" \
    eu_en_300_220="$EU_EN_300_220" \
    bw_4m_2m_prim_loc="$BW_4M_2M_PRIM_LOC" \
    hostapd_debug="$HOSTAPD_DEBUG" \
    use_eeprom_config="$USE_EEPROM_CONFIG" \
    fw_name="$FW_NAME" \
    bd_name="$BD_NAME" \
    bridge_ip_mode="$BRIDGE_IP_MODE" \
    relay_type="$RELAY_TYPE" \
    relay_nat="$RELAY_NAT" \
    ap_ip="$AP_IP" \
    dhcp_start="$DHCP_START" \
    dhcp_end="$DHCP_END" \
    power_save="$POWER_SAVE" \
    ps_timeout="$PS_TIMEOUT" \
    sleep_duration="$SLEEP_DURATION" \
    listen_interval="$LISTEN_INTERVAL" \
    idle_mode="$IDLE_MODE" \
    cqm_enable="$CQM_ENABLE" \
    beacon_bypass_enable="$BEACON_BYPASS_ENABLE" \
    sched_scan_enable="$SCHED_SCAN_ENABLE" \
    support_ch_width="$SUPPORT_CH_WIDTH" \
    ndp_preq="$NDP_PREQ" \
    peer="$MESH_PEER" \
    static_ip="$MESH_STATIC_IP" \
    batman="$MESH_BATMAN" \
    macaddr="$MACADDR" \
    twt_int="$TWT_INT" \
    twt_num="$TWT_NUM" \
    twt_sp="$TWT_SP" \
    twt_force_sleep="$TWT_FORCE_SLEEP" \
    twt_num_in_group="$TWT_NUM_IN_GROUP" \
    twt_algo="$TWT_ALGO" \
; do
    case "$kv" in
    *=) ;;                      # unset in halow.conf -> keep script default
    *)  KVARGS="$KVARGS $kv" ;;
    esac
done
# bridge=1 only makes sense where start.py has a bridge flow (STA, AP,
# relay). A leftover BRIDGE=1 from a previous mode used to reach mesh too,
# whose bring-up then polled dhcpcd against a br0 that never exists and the
# unit sat in "activating" forever.
case "${STA_TYPE:-1}" in
0|1|3) [ "$BRIDGE" = "1" ] && KVARGS="$KVARGS bridge=1" ;;
esac

if [ "$MODE" != "1" ] || [ "${DRIVER_STACK:-modular}" = "nrc" ]; then
    # ---- stock start.py flow: monolithic nrc.ko ----------------------------
    KO="$NRC_SCRIPT_DIR/../sw/driver/nrc/nrc.ko"
    [ -f "$KO" ] || KO="$NRC_SCRIPT_DIR/../sw/driver/nrc.ko"
    [ -f "$KO" ] || {
        echo "this mode needs the monolithic driver (sw/driver/nrc/nrc.ko) - not found"
        exit 1
    }

    case "$MODE" in
    0)  POS="" ;;                                # STA
    1)  POS="$CHANNEL" ;;                        # plain AP
    2)  # sniffer: channel mandatory. start.py knows local(0)/remote(1);
        # the GUI's "web" mode maps to remote (headless monitor iface) and
        # halow-webd drives the tshark/dumpcap capture itself.
        [ -n "$CHANNEL" ] || { echo "sniffer mode needs CHANNEL"; exit 1; }
        SM=${SNIFFER_MODE:-2}
        [ "$SM" = "2" ] && {
            SM=1
            if ! command -v tshark >/dev/null 2>&1; then
                echo "[start-wlan] installing tshark for the web sniffer..."
                DEBIAN_FRONTEND=noninteractive apt-get install -y tshark || \
                    echo "[start-wlan] WARNING: tshark install failed - web capture unavailable"
            fi
        }
        POS="$CHANNEL $SM"
        ;;
    3)  # relay: the single radio serves both legs, so the AP leg's hostapd
        # must beacon on the same channel the STA leg associates on - pass
        # CHANNEL through (run_ap seds it into the hostapd conf, exactly as
        # in plain AP mode). Empty keeps the conf's default, as before.
        POS="$CHANNEL" ;;
    4)  # mesh: mesh mode is positional; peer/static_ip ride KVARGS. The
        # channel lives inside the country's mesh conf files - write the
        # configured one in before start.py reads them.
        if [ -n "$CHANNEL" ]; then
            python3 mesh_chan.py "${COUNTRY:-US}" "$CHANNEL" || \
                echo "[start-wlan] WARNING: mesh channel not applied - conf default stands"
        fi
        POS="${MESH_MODE:-2}"
        ;;
    esac

    # Bring-up must not block on association: start.py waits for a DHCP lease
    # forever by default, which is right when a person runs it and watches,
    # but under systemd it keeps the unit in "activating" until the start
    # timeout fires - so halow.target never comes up and the Web GUI cannot
    # apply another config. Bound the wait; wpa_supplicant carries on
    # scanning and joins whenever the AP appears.
    case "$MODE" in
    0|3) KVARGS="$KVARGS ip_wait=${IP_WAIT:-30}" ;;   # STA, relay
    esac

    # which wired port the bridge/NAT use (start_modular.py has its own key
    # for this, so it is added here and not to the shared KVARGS list)
    if [ -n "$BRIDGE_ETH_IFACE" ]; then
        KVARGS="$KVARGS eth_iface=$BRIDGE_ETH_IFACE"
    fi

    echo "[start-wlan] mode $MODE on the monolithic driver via start.py ($KO)"
    python3 start.py "$MODE" "${SECURITY:-0}" "${COUNTRY:-US}" $POS $KVARGS
else
    # ---- modular stack: Unified AP (MCP/ESL) --------------------------------
    ARGS="$CHANNEL $EXTRA_ARGS $KVARGS"
    # modular-only options (no equivalent in the stock flow)
    for kv in \
        mcp_priority="$MCP_PRIORITY" \
        bridge_eth_iface="$BRIDGE_ETH_IFACE" \
        halow_iface="$HALOW_IFACE" \
    ; do
        case "$kv" in
        *=) ;;
        *)  ARGS="$ARGS $kv" ;;
        esac
    done
    [ "$HOSTAPD_MODE" = "external" ] && ARGS="$ARGS hostapd=external"

    python3 start_modular.py "$MODE" "${SECURITY:-0}" "${COUNTRY:-US}" $ARGS
fi

# The radio interface is the proof that the driver bound. When it is missing
# because request_irq() found no usable IRQ, the SPI device is stuck that way
# until the board reboots - seen after the monolithic driver is unloaded - and
# no amount of restarting the services helps. Say so instead of leaving the
# operator to guess; /run means the marker clears itself on that reboot.
if ! /sbin/iw dev 2>/dev/null | grep -q "Interface ${HALOW_IFACE:-wlan0}"; then
    if dmesg 2>/dev/null | tail -80 | grep -q 'request_irq()'; then
        mkdir -p /run/halow 2>/dev/null || true
        echo "the radio has no usable interrupt - reboot to recover" \
            > /run/halow/reboot-required 2>/dev/null || true
        echo "[start-wlan] driver could not claim its IRQ - a reboot is required"
    fi
fi

# TX power is start.py's now: the setting reaches its header (MAX_TXPWR), and
# only start.py knows what to do with the values that are not a number -
# "auto" leaves the board data file's own maximum in place, and a station
# whose power TPC owns is not written at all. Re-applying it from here sent
# cli_app the word "auto", which the firmware read as 0 and rejected.
