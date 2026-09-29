# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

# -*- coding: utf-8 -*-
#
# halow_config.py - the settings window both start paths share.
#
# The same settings live in two places: /etc/halow/config.json, which the Web
# GUI shows and saves, and the start.py header plus conf/<country>/*.conf,
# which a manual run edits. This module carries values in both directions so
# whichever a person changed last is what both of them show.
#
#   manual ./start.py  --(run)-->  config.json      (sync_out)
#   Web GUI save       --(save)->  start.py header + conf files (sync_in)
#
# A board without the Web GUI has no config.json. Every entry point then does
# nothing at all and a manual run behaves exactly as it always did.
#
# Python 2 and 3 both import this: a manual ./start.py takes the
# "#!/usr/bin/env python" shebang, which is python2 on Raspberry Pi OS buster.

import json
import os
import re

CONFIG_FILE = "/etc/halow/config.json"
SERVICE_CONF = "/etc/halow/halow.conf"

# conf_key (schema.json / halow.conf) -> start.py header variable.
# This is the mapping start-wlan.sh used to spell out as key=value arguments.
CONF_KEY_TO_VAR = {
    'MAX_CPUCLOCK': 'max_cpuclock', 'GUARD_INT': 'guard_int',
    'SHORT_BCN_ENABLE': 'short_bcn_enable', 'LEGACY_ACK_ENABLE': 'legacy_ack_enable',
    'NDP_ACK_1M': 'ndp_ack_1m', 'AMPDU_ENABLE': 'ampdu_enable',
    'BSS_MAX_IDLE_ENABLE': 'bss_max_idle_enable', 'BSS_MAX_IDLE': 'bss_max_idle',
    'CCA_THRESHOLD': 'cca_threshold', 'POWER_SAVE_PRETEND': 'power_save_pretend',
    'CHANNEL_MANAGER': 'channel_manager',
    'CHANNEL_MANAGER_PROFILE': 'channel_manager_profile',
    'CHANNEL_MANAGER_PREFER_BW': 'channel_manager_prefer_bw',
    'CHANNEL_MANAGER_DWELL': 'channel_manager_dwell',
    'CHANNEL_MANAGER_EXCLUDE': 'channel_manager_exclude',
    'TPC': 'tpc', 'EU_EN_300_220': 'eu_en_300_220',
    'MAX_TXPWR': 'max_txpwr', 'MAX_TXPWR_STA': 'max_txpwr_sta',
    'BW_4M_2M_PRIM_LOC': 'bw_4m_2m_prim_loc', 'HOSTAPD_DEBUG': 'hostapd_debug',
    'USE_EEPROM_CONFIG': 'use_eeprom_config', 'FW_NAME': 'fw_name',
    'BD_NAME': 'bd_name', 'BRIDGE_IP_MODE': 'bridge_ip_mode',
    'RELAY_TYPE': 'relay_type', 'RELAY_NAT': 'relay_nat',
    'AP_IP': 'ap_ip', 'DHCP_START': 'dhcp_start', 'DHCP_END': 'dhcp_end',
    'POWER_SAVE': 'power_save', 'PS_TIMEOUT': 'ps_timeout',
    'SLEEP_DURATION': 'sleep_duration', 'LISTEN_INTERVAL': 'listen_interval',
    'IDLE_MODE': 'idle_mode', 'CQM_ENABLE': 'cqm_enable',
    'BEACON_BYPASS_ENABLE': 'beacon_bypass_enable',
    'SCHED_SCAN_ENABLE': 'sched_scan_enable',
    'SUPPORT_CH_WIDTH': 'support_ch_width', 'NDP_PREQ': 'ndp_preq',
    'MESH_PEER': 'peer', 'MESH_STATIC_IP': 'static_ip', 'MESH_BATMAN': 'batman',
    'MACADDR': 'macaddr', 'TWT_INT': 'twt_int', 'TWT_NUM': 'twt_num',
    'TWT_SP': 'twt_sp', 'TWT_FORCE_SLEEP': 'twt_force_sleep',
    'TWT_NUM_IN_GROUP': 'twt_num_in_group', 'TWT_ALGO': 'twt_algo',
    'STA_STATIC_IP': 'sta_static_ip', 'STA_GATEWAY': 'sta_gateway',
    'BRIDGE_ETH_IFACE': 'eth_iface',
    'ETH_IP_MODE': 'eth_ip_mode', 'ETH_STATIC_IP': 'eth_static_ip',
    'ETH_GATEWAY': 'eth_gateway', 'ETH_DNS': 'eth_dns',
}

# GUI keys that are positional arguments of start.py, not header variables
MODE_TO_STA_TYPE = {'unified-ap': 1, 'ap': 1, 'sta': 0,
                    'sniffer': 2, 'relay': 3, 'mesh': 4}
STA_TYPE_TO_MODE = {0: 'sta', 1: 'ap', 2: 'sniffer', 3: 'relay', 4: 'mesh'}
SECURITY_TO_ARG = {'open': 0, 'wpa2-psk': 1, 'wpa3-owe': 2, 'wpa3-sae': 3}
ARG_TO_SECURITY = dict((v, k) for k, v in SECURITY_TO_ARG.items())

# conf file basenames per (role, security argument)
CONF_BASENAME = {
    ('ap', 0): 'ap_halow_open.conf', ('ap', 1): 'ap_halow_wpa2.conf',
    ('ap', 2): 'ap_halow_owe.conf', ('ap', 3): 'ap_halow_sae.conf',
    ('ap', 4): 'ap_halow_pbc.conf',
    ('sta', 0): 'sta_halow_open.conf', ('sta', 1): 'sta_halow_wpa2.conf',
    ('sta', 2): 'sta_halow_owe.conf', ('sta', 3): 'sta_halow_sae.conf',
    ('sta', 4): 'sta_halow_pbc.conf',
    ('mesh', 0): 'mp_halow_open.conf', ('mesh', 3): 'mp_halow_sae.conf',
    ('mesh-ap', 0): 'map_halow_open.conf', ('mesh-ap', 1): 'map_halow_wpa2.conf',
    ('mesh-ap', 2): 'map_halow_owe.conf', ('mesh-ap', 3): 'map_halow_sae.conf',
}

EU_COUNTRIES = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
                "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
                "NL", "PL", "PT", "RO", "SE", "SI", "SK", "GB", "SA"]


def conf_country(country):
    """Country directory under script/conf: EU members share one."""
    c = str(country or "US").upper()
    return "EU" if c in EU_COUNTRIES else c


def have_config():
    return os.path.isfile(CONFIG_FILE)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _read_text(path):
    """File contents, through sudo when the file is root-only.

    config.json holds the GUI password hash, so it is mode 0600 root - and a
    manual start.py runs as the login user. Without the sudo fallback the
    settings window read an empty configuration and did nothing, silently.
    """
    try:
        with open(path) as f:
            return f.read()
    except (IOError, OSError):
        pass
    try:
        import subprocess
        p = subprocess.Popen(["sudo", "cat", path], stdout=subprocess.PIPE)
        out = p.communicate()[0]
        if p.returncode == 0:
            return out.decode() if hasattr(out, 'decode') else out
    except (IOError, OSError):
        pass
    return ""


def load_config():
    text = _read_text(CONFIG_FILE)
    if not text:
        return {}
    try:
        cfg = json.loads(text)
        return cfg if isinstance(cfg, dict) else {}
    except ValueError:
        return {}


def save_config(cfg):
    """Replace config.json atomically, keeping its owner and mode.

    halow-webd is the other writer and reads the file back on every request,
    so a half-written file would be served to the GUI.
    """
    if not have_config():
        return False
    tmp = CONFIG_FILE + ".start"
    try:
        text = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
        if os.access(os.path.dirname(CONFIG_FILE), os.W_OK):
            with open(tmp, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.rename(tmp, CONFIG_FILE)
        else:
            import subprocess
            p = subprocess.Popen(
                ["sudo", "sh", "-c",
                 "cat > '%s' && chmod 600 '%s' && mv '%s' '%s'"
                 % (tmp, tmp, tmp, CONFIG_FILE)], stdin=subprocess.PIPE)
            p.communicate(text.encode())
            if p.returncode != 0:
                return False
        return True
    except (IOError, OSError, ValueError):
        return False


def load_schema(pkg_dir):
    """schema.json describes the GUI values; it ships inside the package."""
    for p in (os.path.join(pkg_dir, "web", "schema.json"),
              "/etc/halow/schema.json"):
        text = _read_text(p)
        if not text:
            continue
        try:
            s = json.loads(text)
        except ValueError:
            continue
        if isinstance(s, dict) and s:
            return s
    return {}


def _to_script(meta, gui_value):
    """GUI value -> the form the start.py header uses."""
    # "a number, or one word": the word has a sentinel in the header, the
    # way loc_1m_primary_ch uses -1. TX power spells that word "auto".
    aval = meta.get("auto_value")
    if aval is not None and str(gui_value).strip().lower() == "auto":
        return aval
    cmap = meta.get("conf_map") or {}
    v = cmap.get(str(gui_value), gui_value)
    return "" if v is None else v


# header variables whose "disabled" value is the integer 0, which the GUI
# spells as an empty field
_ZERO_IS_EMPTY = ('peer', 'static_ip', 'macaddr', 'bd_name',
                  'channel_manager_exclude')


def _to_gui(meta, script_value, var=None):
    """start.py header value -> the GUI's own spelling."""
    if var in _ZERO_IS_EMPTY and str(script_value) in ('0', ''):
        return ""
    aval = meta.get("auto_value")
    if aval is not None:
        v = str(script_value).strip().strip("'\"").lower()
        return "auto" if v in (str(aval), "auto", "") else str(script_value)
    cmap = meta.get("conf_map") or {}
    if cmap:
        for gui, conf in cmap.items():
            if str(conf) == str(script_value):
                return gui
        return None                      # no GUI spelling for this value
    return str(script_value)


# Names a blank value must never reach: both are install-time file names,
# and an empty fw_name would leave start.py with no firmware to load.
_KEEP_ON_BLANK = ("fw_name", "bd_name")


def config_to_vars(cfg, schema, keep_blank=False):
    """Header variables the saved configuration asks for: {name: value}.

    keep_blank carries empty values through as well, for a reset: there the
    empty value is the shipped one and the header has to go back to it.
    """
    out = {}
    for key, meta in schema.items():
        var = CONF_KEY_TO_VAR.get(meta.get("conf_key") or "")
        if not var or key not in cfg:
            continue
        val = _to_script(meta, cfg.get(key, ""))
        if str(val).strip() == "" and not meta.get("conf_map") \
                and (not keep_blank or key in _KEEP_ON_BLANK):
            continue                     # empty = keep the script default
        out[var] = val
    if "bridge" in cfg:
        out['use_bridge_setup'] = 1 if str(cfg.get("bridge")) == "on" else 0
    return out


def vars_to_config(cfg, schema, values):
    """Fold header values into the saved configuration. Returns changed keys."""
    changed = []
    for key, meta in schema.items():
        var = CONF_KEY_TO_VAR.get(meta.get("conf_key") or "")
        if not var or var not in values:
            continue
        gui = _to_gui(meta, values[var], var)
        if gui is None:                  # e.g. power_save=1, no GUI equivalent
            continue
        if str(cfg.get(key, "")) != str(gui):
            cfg[key] = gui
            changed.append(key)
    if 'use_bridge_setup' in values:
        want = "on" if int(values['use_bridge_setup'] or 0) > 0 else "off"
        # the GUI has no eth index, so only the on/off state travels
        if str(cfg.get("bridge", "off")) != want:
            cfg["bridge"] = want
            changed.append("bridge")
    return changed


def positional_to_config(cfg, sta_type, security, country, channel=None,
                         mesh_mode=None, sniffer_mode=None):
    """Fold the command line's positional arguments into the configuration."""
    changed = []

    def put(key, val):
        if val is None:
            return
        if str(cfg.get(key, "")) != str(val):
            cfg[key] = val
            changed.append(key)

    mode = STA_TYPE_TO_MODE.get(int(sta_type))
    # unified-ap is an AP too: keep the GUI's own flavour rather than
    # downgrading the mode every time someone runs a plain AP by hand
    if mode == 'ap' and cfg.get("operation_mode") == "unified-ap":
        mode = "unified-ap"
    put("operation_mode", mode)
    put("security", ARG_TO_SECURITY.get(int(security)))
    put("country", str(country).upper())
    if channel is not None:
        put("channel", str(channel))
    if mesh_mode is not None:
        put("mesh_mode", {0: 'portal', 1: 'point', 2: 'mesh-ap'}.get(int(mesh_mode)))
    if sniffer_mode is not None:
        put("sniffer_mode", {0: 'local', 1: 'remote'}.get(int(sniffer_mode)))
    return changed


def update_service_conf(cfg, schema):
    """Project the configuration onto halow.conf, the services' copy.

    systemd reads halow.conf, not config.json, so a manual run that only
    updated config.json would leave the next Web UI start on the previous
    values. Only keys this configuration owns are touched; NRC_SCRIPT_DIR,
    DRIVER_STACK and the rest keep whatever the installer put there.
    """
    if not os.path.isfile(SERVICE_CONF):
        return []
    updates = {}
    for key, meta in schema.items():
        ck = meta.get("conf_key")
        if ck and key in cfg:
            updates[ck] = _to_script(meta, cfg.get(key, ""))
    if "security" in cfg:
        updates["SECURITY"] = SECURITY_TO_ARG.get(cfg.get("security"), 0)
    for gui_key, conf_key in (("country", "COUNTRY"), ("channel", "CHANNEL")):
        if gui_key in cfg:
            v = cfg.get(gui_key, "")
            # "auto" is the GUI's way of saying "the channel manager picks":
            # the scripts must see no channel at all
            updates[conf_key] = "" if str(v).strip().lower() == "auto" else v
    if "bridge" in cfg:
        updates["BRIDGE"] = "1" if str(cfg.get("bridge")) == "on" else "0"
    text = _read_text(SERVICE_CONF)
    if not text:
        return []
    lines = text.splitlines(True)
    touched = []
    for key, val in sorted(updates.items()):
        new = "%s=%s\n" % (key, val)
        pat = re.compile(r"^%s=" % re.escape(key))
        for i, line in enumerate(lines):
            if pat.match(line):
                if lines[i] != new:
                    lines[i] = new
                    touched.append(key)
                break
        else:
            lines.append(new)
            touched.append(key)
    if touched:
        _replace_file(SERVICE_CONF, "".join(lines))
    return touched


# --------------------------------------------------------------- start.py
_ASSIGN = r"^(?P<name>%s)(?P<pad>\s*)=(?P<gap>\s*)(?P<value>.*?)(?P<tail>\s*(#.*)?)$"


def _format_value(old, new):
    """Keep the header's own quoting: 'auto' stays quoted, 0 stays bare.

    A number is always written bare, even where the line used to hold a
    quoted string: the header is executable code and int() is applied to
    some of these values, so '21' would fail where 21 works.
    """
    s = str(new)
    if isinstance(new, int) or s.lstrip('-').isdigit():
        return s
    quoted = len(old) >= 2 and old[0] in "'\"" and old[-1] == old[0]
    if quoted or not s.lstrip('-').isdigit():
        return "'%s'" % s.replace("'", "")
    return s


def apply_to_start_py(path, values):
    """Write header variables into a start script, in place.

    Only the value of an existing "name = value" line is replaced; comments
    and every other line stay as they are. Returns the names actually
    written. A syntax check runs before the file is replaced - this is
    executable code, and a broken start.py leaves the board with no radio.
    """
    if not values or not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            lines = f.read().splitlines(True)
    except (IOError, OSError):
        return []
    written = []
    for name, val in sorted(values.items()):
        pat = re.compile(_ASSIGN % re.escape(name))
        for i, line in enumerate(lines):
            m = pat.match(line.rstrip("\n"))
            if not m:
                continue
            new_val = _format_value(m.group("value"), val)
            if new_val == m.group("value"):
                break                        # already correct
            if str(val) == "" and m.group("value")[:1] not in "'\"":
                break                        # bare value (peer = 0): not blankable
            lines[i] = "%s%s=%s%s%s\n" % (m.group("name"), m.group("pad"),
                                          m.group("gap"), new_val, m.group("tail"))
            written.append(name)
            break
    if not written:
        return []
    text = "".join(lines)
    try:
        compile(text, path, "exec")
    except SyntaxError as e:
        print("[!] %s: refusing to write settings (%s)" % (path, e))
        return []
    return written if _replace_file(path, text) else []


def _replace_file(path, text):
    """Replace a file's contents, keeping its owner, mode and a .bak copy.

    The temporary file is a copy of the original, so writing through it
    preserves ownership: a root-only "cat > new && mv" would hand a
    customer's start.py to root and lock them out of editing it.
    """
    tmp = path + ".new"
    try:
        if os.access(os.path.dirname(path) or ".", os.W_OK) and os.access(path, os.W_OK):
            import shutil
            st = os.stat(path)
            shutil.copy2(path, path + ".bak")
            shutil.copy2(path, tmp)
            with open(tmp, "w") as f:
                f.write(text)
            if hasattr(os, "chown") and os.geteuid() == 0:
                # halow-webd runs as root and these files belong to the user
                # who installed the package - hand them back
                os.chown(tmp, st.st_uid, st.st_gid)
                os.chown(path + ".bak", st.st_uid, st.st_gid)
            os.rename(tmp, path)
            return True
        import subprocess
        p = subprocess.Popen(
            ["sudo", "sh", "-c",
             "cp -p '%s' '%s.bak' && cp -p '%s' '%s' && cat > '%s' && mv '%s' '%s'"
             % (path, path, path, tmp, tmp, tmp, path)],
            stdin=subprocess.PIPE)
        p.communicate(text.encode())
        return p.returncode == 0
    except (IOError, OSError):
        return False


# ------------------------------------------------------------- conf files
def conf_path(pkg_dir, country, role, security):
    base = CONF_BASENAME.get((role, int(security)))
    if not base:
        return ""
    return os.path.join(pkg_dir, "script", "conf", conf_country(country), base)


_QUOTED_SUPPLICANT = ("ssid", "psk", "sae_password")


def read_network(path):
    """(ssid, passphrase, beacon_int) a conf file carries, '' when it has none."""
    ssid = pw = bi = ""
    try:
        with open(path) as f:
            for line in f:
                s = line.strip()
                if s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k, v = k.strip(), v.strip().split("#")[0].strip()
                if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
                    v = v[1:-1]
                if k == "ssid" and not ssid:
                    ssid = v
                elif k in ("wpa_passphrase", "psk", "sae_password") and not pw:
                    pw = v
                elif k == "beacon_int" and not bi:
                    bi = v
    except (IOError, OSError):
        pass
    return ssid, pw, bi


def _comment_out(lines, key):
    """Comment out every active "key=" line. Used when the manager owns the
    channel: a fixed one left behind would be what hostapd starts on."""
    pat = re.compile(r"^(\s*)%s\s*=" % re.escape(key))
    return [("#" + ln if pat.match(ln) else ln) for ln in lines]


def set_scan_ssid(path, on):
    """Turn the supplicant's directed scan on or off in a network block.

    NDP probe request carries a compressed SSID, so the station has to scan
    for a named SSID to use it: ndp_preq alone changes nothing while
    scan_ssid is off. The two belong together, and the GUI has one item.
    """
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            lines = f.read().splitlines(True)
    except (IOError, OSError):
        return []
    live = re.compile(r"^(\s*)scan_ssid\s*=")
    dead = re.compile(r"^(\s*)#\s*scan_ssid\s*=")
    for i, ln in enumerate(lines):
        m = live.match(ln) or dead.match(ln)
        if not m:
            continue
        new = "%s%sscan_ssid=1\n" % (m.group(1), "" if on else "#")
        if lines[i] == new:
            return []
        lines[i] = new
        return ["scan_ssid"] if _replace_file(path, "".join(lines)) else []
    return []


def write_network(path, ssid=None, passphrase=None, channel=None,
                  fold_auto=False, beacon_int=None):
    """Put the GUI's network settings into a conf file, in place.

    Only existing assignments are rewritten, so a file keeps its comments,
    its country block and everything the GUI does not own. Supplicant string
    fields are re-quoted; hostapd's are not.
    """
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            lines = f.read().splitlines(True)
    except (IOError, OSError):
        return []
    # A supplicant file quotes its string values; hostapd does not. The live
    # copies are named temp_*, so the name alone missed them and an SSID
    # written into a running station's conf came out unquoted - wpa_supplicant
    # then refused the whole file. The network block settles it either way.
    supplicant = os.path.basename(path).startswith(
        ("sta_", "mp_", "temp_supplicant", "temp_mp_")) \
        or any(ln.lstrip().startswith("network=") for ln in lines)
    wanted = []
    if ssid is not None and str(ssid) != "":
        wanted.append(("ssid", ssid))
    if passphrase is not None and str(passphrase) != "":
        for k in (("psk", "sae_password") if supplicant else ("wpa_passphrase",)):
            wanted.append((k, passphrase))
    # "auto" is not a channel: the manager picks one and start.py writes it
    # into the live conf. Naming it here would hand hostapd the word itself.
    auto_ch = str(channel).strip().lower() == "auto"
    if channel is not None and str(channel) != "" and not auto_ch and not supplicant:
        wanted.append(("channel", channel))
    # hostapd's own key; a station has no beacon of its own to time. Empty
    # leaves the country profile's value where it is.
    if beacon_int is not None and str(beacon_int).strip() != "" and not supplicant:
        wanted.append(("beacon_int", beacon_int))
    written = []
    # the manager owns the channel while "auto" is set: a fixed one left in
    # the country conf is what hostapd would start on, so it is commented
    # out there. The live conf is left alone - it carries the manager's pick.
    if auto_ch and fold_auto and not supplicant:
        folded = _comment_out(lines, "channel")
        if folded != lines:
            lines = folded
            written.append("channel")
    for key, val in wanted:
        # an active assignment first; a commented one only when there is no
        # active line at all. These files carry several commented variants of
        # the same key (channel=, #channel=), and reviving the first one
        # leaves two assignments behind - hostapd then takes the later one.
        live = re.compile(r"^(\s*)%s\s*=.*$" % re.escape(key))
        dead = re.compile(r"^(\s*)#\s*%s\s*=.*$" % re.escape(key))
        idx = None
        for i, line in enumerate(lines):
            if live.match(line.rstrip("\n")):
                idx = i
                break
        if idx is None:
            for i, line in enumerate(lines):
                if dead.match(line.rstrip("\n")):
                    idx = i
                    break
        if idx is None:
            continue
        indent = (live.match(lines[idx].rstrip("\n")) or
                  dead.match(lines[idx].rstrip("\n"))).group(1)
        if supplicant and key in _QUOTED_SUPPLICANT:
            new = '%s%s="%s"\n' % (indent, key, str(val).replace('"', ''))
        else:
            new = "%s%s=%s\n" % (indent, key, val)
        if lines[idx] != new:
            lines[idx] = new
            written.append(key)
    if written:
        _replace_file(path, "".join(lines))
    return written


# ------------------------------------------------------------- directions
def sync_out(pkg_dir, values, positional=None, network=None):
    """Manual run -> config.json: record what this run actually used."""
    if not have_config():
        return []
    schema = load_schema(pkg_dir)
    cfg = load_config()
    if not schema or not cfg:
        print("[!] %s not updated: could not read %s"
              % (CONFIG_FILE, "schema.json" if not schema else CONFIG_FILE))
        return []
    changed = vars_to_config(cfg, schema, values or {})
    if positional:
        changed += positional_to_config(cfg, **positional)
    for key in ("ssid", "password", "beacon_int",
                "uplink_ssid", "uplink_password"):
        val = (network or {}).get(key)
        if val and str(cfg.get(key, "")) != str(val):
            cfg[key] = val
            changed.append(key)
    if changed and save_config(cfg):
        update_service_conf(cfg, schema)     # keep the services' copy in step
        return sorted(set(changed))
    return []


def sync_in(pkg_dir, start_script):
    """config.json -> start.py header, when the saved configuration is newer.

    The Web GUI writes the header itself on every save, so this only has
    work to do after something replaced the script - a package update, a
    restored backup - and it is what keeps a GUI-configured board from
    coming back up on the package defaults.
    """
    if not have_config() or _mtime(CONFIG_FILE) <= _mtime(start_script):
        return []
    schema = load_schema(pkg_dir)
    cfg = load_config()
    if not schema or not cfg:
        return []
    return apply_to_start_py(start_script, config_to_vars(cfg, schema))


def apply_to_scripts(pkg_dir, cfg, schema=None, keep_blank=False,
                     network_only=False):
    """Web GUI save -> start.py header, the conf files and halow.conf.

    network_only writes the hostapd/supplicant conf files and nothing else.
    It is for the second pass over a role the radio runs but the saved mode
    does not name: that pass must not touch halow.conf or the header, whose
    role fields belong to the saved mode alone.
    """
    schema = schema or load_schema(pkg_dir)
    if not schema or not cfg:
        return {}
    done = {}
    if not network_only:
        svc = update_service_conf(cfg, schema)
        if svc:
            done['halow.conf'] = svc
        values = config_to_vars(cfg, schema, keep_blank)
        for script in ("start.py", "start_modular.py"):
            path = os.path.join(pkg_dir, "script", script)
            w = apply_to_start_py(path, values)
            if w:
                done[script] = w
    mode = cfg.get("operation_mode", "ap")
    country = cfg.get("country", "US")
    sec = SECURITY_TO_ARG.get(cfg.get("security", "open"), 0)
    ssid, pw = cfg.get("ssid"), cfg.get("password")
    channel = cfg.get("channel")
    # every security variant of the mode's files, not just the one in use:
    # switching security in the GUI must not fall back to the SSID the
    # package happens to ship in the other file
    roles = []
    if mode in ("unified-ap", "ap"):
        roles = [('ap', ssid, pw, channel)]
    elif mode == "sta":
        roles = [('sta', ssid, pw, None)]
    elif mode == "relay":
        # the AP leg serves ssid; the station leg joins the uplink network
        roles = [('ap', ssid, pw, channel),
                 ('sta', cfg.get("uplink_ssid") or ssid,
                  cfg.get("uplink_password") or pw, None)]
    elif mode == "mesh":
        roles = [('mesh', ssid, pw, None)]
        if cfg.get("mesh_mode") == "mesh-ap":
            roles.append(('mesh-ap', ssid, pw, channel))
    for role, sid, passwd, ch in roles:
        for s in sorted(set(k[1] for k in CONF_BASENAME if k[0] == role)):
            path = conf_path(pkg_dir, country, role, s)
            if not path:
                continue
            w = write_network(path, sid, passwd, ch, fold_auto=True,
                              beacon_int=cfg.get("beacon_int"))
            # ndp_preq and the supplicant's directed scan belong together
            if role in ("sta", "mesh"):
                w = (w or []) + set_scan_ssid(
                    path, str(cfg.get("ndp_preq", "off")) == "on")
            if w:
                done[os.path.basename(path)] = w
    return done


def reset_conf_beacon_int(pkg_dir, beacon_int):
    """Put one beacon interval in every country's beaconing conf. Reset only.

    A normal save reaches the country in use and no other. A value left in
    another country's file comes back the moment the operator selects that
    country, and a manual start.py run there reads it and syncs it out into
    config.json - so a reset has to reach all of them. AP and mesh-ap are
    the roles with a beacon of their own; the supplicant files carry no key.
    """
    if beacon_int is None or str(beacon_int).strip() == "":
        return []
    root = os.path.join(pkg_dir, "script", "conf")
    bases = sorted(v for k, v in CONF_BASENAME.items()
                   if k[0] in ('ap', 'mesh-ap'))
    done = []
    try:
        countries = sorted(os.listdir(root))
    except OSError:
        return []
    for country in countries:
        for base in bases:
            path = os.path.join(root, country, base)
            if not os.path.isfile(path):
                continue
            if write_network(path, None, None, None, beacon_int=beacon_int):
                done.append(os.path.join(country, base))
    return done


def apply_live(conf_path):
    """Put the saved network settings into a conf file the stack is running.

    The live files (conf/temp_*.conf) are copies taken at start, so a GUI
    save that only reaches conf/<country>/*.conf would not show up until the
    next restart. hostapd and wpa_supplicant re-read their own file, which is
    what apply-hostapd.sh / apply-supplicant.sh bounce them for.
    """
    cfg = load_config()
    if not cfg:
        return []
    ssid, pw = cfg.get("ssid"), cfg.get("password")
    if cfg.get("operation_mode") == "relay" and \
            os.path.basename(conf_path).startswith(("sta_", "temp_supplicant")):
        ssid = cfg.get("uplink_ssid") or ssid
        pw = cfg.get("uplink_password") or pw
    ch = cfg.get("channel")
    if str(ch).strip().lower() == "auto":
        ch = None            # the live conf holds the manager's pick
    done = write_network(conf_path, ssid, pw, ch,
                         beacon_int=cfg.get("beacon_int"))
    if os.path.basename(conf_path).startswith(("sta_", "mp_", "temp_supplicant",
                                               "temp_mp_")):
        done += set_scan_ssid(conf_path,
                              str(cfg.get("ndp_preq", "off")) == "on")
    return done


def main(argv):
    """CLI: halow_config.py to-scripts|apply-live <conf>|show [pkg_dir]"""
    action = argv[1] if len(argv) > 1 else "show"
    pkg = argv[2] if len(argv) > 2 else \
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if action == "to-scripts":
        if not have_config():
            print("no %s - nothing to do" % CONFIG_FILE)
            return 0
        done = apply_to_scripts(pkg, load_config())
        for f, keys in sorted(done.items()):
            print("%s: %s" % (f, ", ".join(keys)))
        if not done:
            print("scripts already match %s" % CONFIG_FILE)
        return 0
    if action == "apply-live":
        if len(argv) < 3:
            print("usage: halow_config.py apply-live <conf file>")
            return 1
        done = apply_live(argv[2])
        print("%s: %s" % (argv[2], ", ".join(done) if done else "already current"))
        return 0
    if action == "show":
        schema = load_schema(pkg)
        cfg = load_config()
        print("config: %s (%s)" % (CONFIG_FILE, "present" if have_config() else "absent"))
        print("schema: %d keys" % len(schema))
        for name, val in sorted(config_to_vars(cfg, schema).items()):
            print("  %-26s %s" % (name, val))
        return 0
    print(main.__doc__)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
