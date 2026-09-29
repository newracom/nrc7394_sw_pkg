#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

"""halow-webd - Unified AP configuration daemon with Web GUI (P2).

Standalone generic HaLow AP management: works with only the base AP stack
(halow-wlan.service). The vendor gateway is an OPTIONAL ADD-ON - its panel
appears in the dashboard only when halow-gateway.service is installed.

Design:
  /etc/halow/schema.json   - developer-defined items (hidden/modifiable/apply)
  /etc/halow/config.json   - current values (this daemon is the writer)
  apply policies:
    none            value only (e.g. admin_password)
    live            applied immediately without restart (e.g. txpower)
    hostapd-restart re-render hostapd conf overrides + restart hostapd only
    full-restart    systemctl restart halow.target

Pure python3 stdlib - no external dependencies.
"""

import bisect
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tarfile
import urllib.parse
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import cookies

ETC_DIR = "/etc/halow"
SCHEMA_FILE = os.path.join(ETC_DIR, "schema.json")
CONFIG_FILE = os.path.join(ETC_DIR, "config.json")
SERVICE_CONF = os.path.join(ETC_DIR, "halow.conf")
NET_STATE_FILE = "/tmp/nrc_net.state"
AP_ENV_FILE = "/run/halow/ap.env"
HOSTAPD_CTRL = "/var/run/hostapd"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SECURITY_ARG = {"open": "0", "wpa2-psk": "1", "wpa3-owe": "2", "wpa3-sae": "3"}
TLS_DIR = os.path.join(ETC_DIR, "tls")
DEFAULT_ADMIN_PW = "newracom"


def _ap_env():
    """ap_env module from the installed nrc_pkg/script - the single source of
    truth for the S1G channel table and the country->plan mapping. Importing
    it needs NRC_SCRIPT_DIR on sys.path, so every user goes through here
    rather than depending on some earlier call having set it up."""
    svc = read_kv_file(SERVICE_CONF)
    sdir = svc.get("NRC_SCRIPT_DIR", "")
    if sdir and sdir not in sys.path:
        sys.path.insert(0, sdir)
    import ap_env
    return ap_env


def _halow_config():
    """halow_config module from nrc_pkg/script: the settings window that
    carries what is saved here into the start.py header and the conf files a
    manual run edits, so both show the same configuration."""
    svc = read_kv_file(SERVICE_CONF)
    sdir = svc.get("NRC_SCRIPT_DIR", "")
    if sdir and sdir not in sys.path:
        sys.path.insert(0, sdir)
    import halow_config
    return halow_config


def pkg_dir():
    """Installation root (the parent of nrc_pkg/script)."""
    sdir = read_kv_file(SERVICE_CONF).get("NRC_SCRIPT_DIR", "")
    return os.path.dirname(sdir.rstrip("/")) if sdir else ""


def apply_to_scripts(cfg, keep_blank=False):
    """Write the saved configuration into start.py and the conf files.

    A customer can run the radio from the Web GUI or by hand, and both must
    show what was configured last. Failing here never fails a save: the GUI
    path reads config.json directly and works regardless. keep_blank is for
    a reset, where an empty value is the shipped one and has to be written.
    """
    try:
        done = _halow_config().apply_to_scripts(pkg_dir(), cfg,
                                                keep_blank=keep_blank)
        # A manual run can have the radio in a role the saved mode does not
        # name (start.py started a station while the GUI still says AP). Only
        # the saved mode's conf files would be written, so the files the
        # running role actually reads would keep the old SSID: write both.
        # A relay is exempt: its two legs are one radio, so the detected role
        # is always one of its own. With relay_type=0 the station leg is wlan0
        # and the detection reads "sta", which would put the AP leg's SSID in
        # the station conf - hence the saved mode decides, not the detected one.
        live = live_mode(halow_iface_from_net_state())
        if live and live != cfg.get("operation_mode") \
                and cfg.get("operation_mode") != "relay":
            alt = dict(cfg)
            alt["operation_mode"] = live
            # conf files only: halow.conf and the header carry the role the
            # operator saved, and writing the live one there would undo a
            # mode change the same save just made
            done.update(_halow_config().apply_to_scripts(
                pkg_dir(), alt, keep_blank=keep_blank,
                network_only=True) or {})
    except Exception as exc:
        print("[halow-webd] settings not written to the scripts: %s" % exc,
              flush=True)
        return ""
    if done:
        return "; scripts updated (%s)" % ", ".join(sorted(done))
    return ""


def ap_conf_channel(cfg):
    """The channel the AP's own conf file carries, "" when it has none.

    The shipped conf files keep every channel line commented out, so an AP
    saved without a channel reaches hostapd with none at all - and this
    build has no ACS to fall back on. A customer who wrote a channel into
    the file is the exception this reads for.
    """
    hc = _halow_config()
    try:
        sec = hc.SECURITY_TO_ARG.get(cfg.get("security", "open"), 0)
        path = hc.conf_path(pkg_dir(), cfg.get("country", "US"), "ap", sec)
        with open(path) as f:
            for line in f:
                m = re.match(r"\s*channel\s*=\s*(\S+)", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""


def s1g_table():
    """Driver-derived S1G channel table, keyed by channel plan. {} if absent."""
    try:
        return _ap_env().S1G_CH_TABLE
    except Exception:
        return {}


# ---------------------------------------------------------------- cli_app (P0)
# The Diagnostics tab runs cli_app for the operator. Read-only by design:
# `show ...` and `help` only. Every invocation opens a netlink socket to the
# driver, and a transient socket has been able to hijack the driver's single
# portid before (the listener went deaf until it re-registered),
# so runs are serialised and rate-limited rather than fired per click.
CLI_TIMEOUT = 10
CLI_MIN_INTERVAL = 1.0
# An operator action queues behind a poll for this long before giving up.
CLI_QUEUE_S = 5.0
CLI_MAX_OUTPUT = 64 * 1024
CLI_CATALOG_TTL = 300

# Never runnable from the web, not even in a debug build: memory pokes,
# continuous transmit, calibration and anything that erases or resets.
CLI_BLOCKED = ("write", "cont_tx", "exit", "reset", "factory", "flash",
               "erase", "cal", "sysconfig_write", "dfs_cac")
# Interactive/streaming commands only meaningful inside the cli_app prompt.
# Matched on whole tokens: ("show", "rx") must not also catch `show rx_count`.
CLI_STREAMING = (("show", "signal"), ("show", "rx"), ("show", "tx_time", "start"))

# Write commands exist only in a debug package (make_pkg.sh --debug ->
# BUILD_TYPE=debug in halow.conf) and even there the operator has to unlock
# them per session; the unlock lapses after this much idle time.
CLI_WRITE_UNLOCK_TTL = 300

_cli_lock = threading.Lock()
_cli_prio_lock = threading.Lock()
_cli_prio = {"waiting": 0}
_cli_state = {"last_run": 0.0, "catalog": None, "catalog_ts": 0.0,
              "catalog_mode": None}
_cli_unlocked = {}          # session token -> expiry timestamp

# Which roles a command is meaningful in. cli_app offers the same ~95 commands
# whatever the radio is doing, so an AP operator is offered power-save, scan
# and APF commands that can only ever answer with zeroes. Tag the ones that
# belong to a single role and let the UI grey the rest out.
#
# Roles, not modes: `relay` is an AP and a STA at once, and `mesh` beacons like
# an AP, so both inherit more than one set (see CLI_MODE_ROLES).
#   "sta"     - station-side only (association, power save, scanning, APF)
#   "ap"      - AP-side only (beaconing, associated stations, channel choice)
#   "sniffer" - monitor-mode only
#   "mesh"    - mesh-only
#   "rftest"  - RF test tones: these take the radio off the air, so they are
#               flagged everywhere rather than restricted to one mode.
# Anything absent from this table is treated as valid everywhere, so a missing
# or stale entry can never hide a working command.
CLI_CMD_ROLES = {
    # -- station-side ------------------------------------------------------
    "show apf": "sta", "show apf_filter": "sta",
    "set apf": "sta", "set apf_filter": "sta",
    # The bcmc filter is the same idea as APF: it decides which broadcast /
    # multicast frames are worth waking a sleeping station for (by ethertype,
    # source MAC, all-multicast, or destination IP - see ret_bcmc_filter_info
    # in nrc_ps_api.h). An AP never sleeps, so it has nothing to answer.
    "show bcmc": "sta", "set bcmc": "sta",
    "show ps_all": "sta", "show ps_conf": "sta", "show ps_status": "sta",
    "show idle_mode": "sta", "set idle_mode": "sta",
    # `show 32k_status` looks station-ish (it lives among the deep-sleep
    # commands) but it measures the 32 kHz clock against the reference and
    # returns a real tick count on an AP too, so it is left untagged.
    "show wakeup_pin": "sta", "show wakeup_source": "sta",
    "set wakeup_pin": "sta", "set wakeup_source": "sta",
    # older cli_app builds misspell it in the usage line; keep both keys so
    # the tag survives a package that has not picked the fix up yet
    "set wakeup_soruce": "sta",
    "set deepsleep_gpio": "sta",
    "show scan_status": "sta", "set scan_period": "sta",
    "set bgscan_trx": "sta",
    # -- AP-side -----------------------------------------------------------
    "show sta": "ap",
    "show bcn_mcs": "ap", "set bcn_mcs": "ap", "set bcmc_mcs": "ap",
    "show self_config": "ap", "set self_config": "ap",
    "show optimal_channel": "ap",
    "set color": "ap",
    # `show duty` is deliberately absent: the duty-cycle limiter caps this
    # radio's own transmit time for regulatory reasons and answers the same
    # in AP and STA mode, so it is not an AP-side command.
    # -- single-mode -------------------------------------------------------
    "set prim_loc": "sniffer",
    "set mesh_rssi_threshold": "mesh",
    # -- disruptive --------------------------------------------------------
    "test mcs": "rftest", "test country": "rftest", "test cont_tx": "rftest",
    "test freq_bw": "rftest", "test single_tx": "rftest",
    "test length": "rftest", "test sine_tx": "rftest",
}

# Keys kept for packages that have not picked up a cli_app spelling fix. One
# of the pair never matches, by design, so they are exempt from the drift
# warning below - which otherwise fires on every catalog refresh and teaches
# the reader that the warning means nothing.
CLI_ROLE_ALIASES = {"set wakeup_soruce"}

# Roles each operation mode plays.
CLI_MODE_ROLES = {
    "unified-ap": ("ap",),
    "ap":         ("ap",),
    "sta":        ("sta",),
    "sniffer":    ("sniffer",),
    "relay":      ("ap", "sta"),
    "mesh":       ("ap", "sta", "mesh"),
}


def cli_cmd_role_key(cmd):
    """The role-table key a command matches, or None."""
    words = cmd.split()
    for n in range(len(words), 0, -1):
        key = " ".join(words[:n])
        if key in CLI_CMD_ROLES:
            return key
    return None


def cli_cmd_role(cmd):
    """Role of a command, matching the longest tagged prefix.

    Not every usage string brackets its arguments - "set bcmc [type|...]
    value" and "set wakeup_soruce rtc gpio hspi" leave argument words looking
    like part of the command - so the parsed id can carry a tail the role
    table has no way to predict. Walk the words back until one matches
    instead of demanding the whole id, or a tag silently does nothing.
    """
    key = cli_cmd_role_key(cmd)
    return CLI_CMD_ROLES[key] if key else None


def cli_cmd_applicable(cmd, mode):
    """(applicable, role) for a command in the given operation mode."""
    role = cli_cmd_role(cmd)
    if role is None:
        return True, None
    if role == "rftest":
        return True, role       # allowed, but flagged as disruptive
    return role in CLI_MODE_ROLES.get(mode, ()), role


def is_debug_build():
    return read_kv_file(SERVICE_CONF).get("BUILD_TYPE", "").lower() == "debug"


def cli_write_allowed(token):
    """Write commands need a debug build AND a live per-session unlock."""
    if not is_debug_build() or not token:
        return False
    exp = _cli_unlocked.get(token, 0)
    if exp <= time.time():
        _cli_unlocked.pop(token, None)
        return False
    _cli_unlocked[token] = time.time() + CLI_WRITE_UNLOCK_TTL
    return True


def cli_app_path():
    svc = read_kv_file(SERVICE_CONF)
    return os.path.join(svc.get("NRC_SCRIPT_DIR", ""), "cli_app")


def is_read_only(argv):
    """True for commands that only print, so the GUI can skip the write
    warning and the firmware cache can stay valid.

    `show` and `help` are obvious. `set tpc show` and a bare `set tpc stats`
    are spelled `set` for historical reasons and write nothing; the same
    command with an argument (clear | on | off) does write.
    """
    if not argv:
        return False
    if argv[0] in ("show", "help"):
        return True
    if argv[0] == "set" and argv[1:2] == ["tpc"]:
        if argv[2:3] == ["show"]:
            return True
        if argv[2:3] == ["stats"] and len(argv) == 3:
            return True
    return False


def cli_is_blocked(argv, write_ok=False):
    if not argv:
        return "empty command"
    if argv[0] in CLI_BLOCKED:
        return "%s is not available from the web interface" % argv[0]
    for s in CLI_STREAMING:
        if tuple(argv[:len(s)]) == s:
            return "%s streams and only works in the cli_app prompt" % " ".join(s)
    # `hlog` decides where firmware log lines go and nothing else: it does
    # not touch radio state, and its cost is bounded by the firmware's own
    # free-buffer watermark. It is also the one diagnostic that has to work
    # on a production board -- which is precisely where the read-only rule
    # below would otherwise block it -- so it is allowed on its own terms.
    if argv[0] == "hlog":
        return None
    # `dump run` snapshots state and ships it to the host. It does not crash
    # anything or touch the radio, and being able to take one on demand is
    # how the crash-reporting path gets checked without waiting for a crash.
    if argv[0] == "dump" and argv[1:2] == ["run"]:
        return None
    # `set chmgr` selects the channel-manager mode and profile. It is a write,
    # but it is the same class of operation as the channel setting this GUI
    # already offers, and an operator who cannot turn automatic channel
    # selection off on a production board has no way to pin a channel by hand.
    # The arguments are built here, never taken from the request.
    if argv[0] == "set" and argv[1:2] == ["chmgr"]:
        return None
    # `set tpc show` and a bare `set tpc stats` only print. They are spelled
    # `set` for historical reasons and write nothing - the same command does
    # write once it carries an argument (clear | on | off).
    if is_read_only(argv):
        return None
    if argv[0] not in ("show", "help") and not write_ok:
        if not is_debug_build():
            return "read-only: only `show ...` and `help` are available " \
                   "(write commands need a debug build)"
        return "write commands are locked - enable them first"
    return None


def cli_usage_words(usage):
    """Literal words of a usage string, i.e. the command id without its
    argument slots. Bracket-aware, because a slot may contain spaces:
    "show self_config {Country(KR,US...)} {BW} {dwell time}" is the command
    `show self_config` with three slots - splitting on whitespace and
    dropping tokens that start with a brace leaves the stray "time}" behind
    and invents a command called `show self_config time}`."""
    words, buf, depth = [], "", 0
    for ch in usage:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth = max(0, depth - 1)
        elif depth == 0:
            if ch.isspace():
                if buf:
                    words.append(buf)
                    buf = ""
            else:
                buf += ch
    if buf:
        words.append(buf)
    return words


def cli_parse_catalog(text):
    """`cli_app help` prints one command per line as "<usage>\t+:<desc>"."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.startswith(" "):
            continue
        # Split on the tab run that separates the two columns, NOT on the
        # last colon: descriptions contain colons of their own, and
        # rpartition(":") turned "show cca_thresh ... :show cca_thresh(unit:
        # dBm)" into the command `show cca_thresh :show cca_thresh(unit`.
        m = re.match(r"^\s*(.*?)\t+\s*:\s*(.*)$", line)
        if not m:
            # A usage long enough to fill the column gets no padding tab at
            # all - "set bgscan_trx [..]:set bgscan_trx" - so fall back to
            # the first colon that sits outside any bracket. Requiring the
            # tab dropped those commands from the catalogue entirely.
            depth, cut = 0, -1
            for i, ch in enumerate(line):
                if ch in "{[(":
                    depth += 1
                elif ch in "}])":
                    depth = max(0, depth - 1)
                elif ch == ":" and depth == 0:
                    cut = i
                    break
            if cut < 0:
                continue
            m = None
            usage, desc = line[:cut].strip(), line[cut + 1:].strip()
        else:
            usage, desc = m.group(1).strip(), m.group(2).strip()
        if not usage or not desc:
            continue
        # "show ampdu | show ampdu clear" -> take the first form as the id
        first = usage.split(" | ")[0].strip()
        words = cli_usage_words(first)
        if not words:
            continue
        # "blocked" is permanent (blocklist / prompt-only streaming); "write"
        # merely needs the debug-build unlock, so the UI can grey the two
        # differently without the catalog depending on session state.
        blocked = (words[0] in CLI_BLOCKED or
                   any(tuple(words[:len(s)]) == s for s in CLI_STREAMING))
        cmd = " ".join(words)
        out.append({
            "cmd": cmd,
            "usage": usage,
            "desc": desc,
            "group": words[0],
            "blocked": blocked,
            # Longest-prefix, the same way `applicable` is decided below. An
            # exact lookup disagreed with it for any id carrying an argument
            # tail: the command dimmed, and the badge saying why did not.
            "role": cli_cmd_role(cmd),
            "write": (not blocked) and words[0] not in ("show", "help"),
        })
    return out


def cli_catalog(refresh=False):
    now = time.time()
    mode = get_config().get("operation_mode", "ap")
    if not refresh and _cli_state["catalog"] is not None \
            and _cli_state["catalog_mode"] == mode \
            and now - _cli_state["catalog_ts"] < CLI_CATALOG_TTL:
        return _cli_state["catalog"]
    out = run([cli_app_path(), "help"], timeout=CLI_TIMEOUT)
    cat = cli_parse_catalog(out) if out else []
    for c in cat:
        c["applicable"], _ = cli_cmd_applicable(c["cmd"], mode)
    if cat:
        # The role table is hand-maintained while cli_app's command list comes
        # from the firmware package; say so once per refresh when a tagged
        # command has disappeared, so the two cannot drift apart unnoticed.
        matched = {cli_cmd_role_key(c["cmd"]) for c in cat}
        stale = sorted(set(CLI_CMD_ROLES) - matched - CLI_ROLE_ALIASES)
        if stale:
            print("[cli] role table lists %d command(s) cli_app no longer "
                  "has: %s" % (len(stale), ", ".join(stale)), flush=True)
        _cli_state["catalog"] = cat
        _cli_state["catalog_ts"] = now
        _cli_state["catalog_mode"] = mode
    return cat


def cli_run(argv, write_ok=False, queue_s=0.0):
    """Run one cli_app command. Returns (http_status, payload).

    queue_s lets a caller wait for the lock instead of failing on it. Polls
    pass nothing and are dropped on contention; an operator pressing a button
    passes a few seconds, because CLI_MIN_INTERVAL keeps the lock held for a
    second at a time and a click that lands in that second is not an error."""
    why = cli_is_blocked(argv, write_ok)
    if why:
        return 403, {"error": why}
    path = cli_app_path()
    if not os.path.isfile(path):
        return 503, {"error": "cli_app not found at %s" % path}

    # An operator pressing a button must win against the panel's own refresh:
    # the poll runs two commands every 2 s and CLI_MIN_INTERVAL holds the lock
    # a second at a time, so the click had no gap to land in and came back
    # "another command is still running".
    operator = bool(queue_s)
    if not operator:
        with _cli_prio_lock:
            busy = _cli_prio["waiting"]
        if busy:
            return 409, {"error": "an operator command has priority"}
    else:
        with _cli_prio_lock:
            _cli_prio["waiting"] += 1
    got = _cli_lock.acquire(blocking=operator, timeout=queue_s or -1)
    if operator:
        with _cli_prio_lock:
            _cli_prio["waiting"] -= 1
    if not got:
        return 409, {"error": "another command is still running"}
    try:
        wait = CLI_MIN_INTERVAL - (time.time() - _cli_state["last_run"])
        if wait > 0:
            time.sleep(wait)
        t0 = time.time()
        try:
            # Bytes, then decode leniently: the firmware's shell response is
            # not guaranteed to be UTF-8 (a command that leaves its response
            # buffer partly unset returns whatever was in it), and a stray
            # byte used to raise UnicodeDecodeError out of this call and fail
            # the whole request rather than the one command.
            p = subprocess.run([path] + list(argv), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=CLI_TIMEOUT)
            out = (p.stdout or b"").decode("utf-8", "replace")
            rc = p.returncode
        except subprocess.TimeoutExpired:
            out, rc = "", -1
        except Exception as e:
            out, rc = str(e), -1
        _cli_state["last_run"] = time.time()
        ms = int((_cli_state["last_run"] - t0) * 1000)
        # an operator command may be exactly what changed the settled state.
        # `set tpc show` is spelled `set` but only reads - counting it as a
        # write made it invalidate its own cache and poll forever.
        if not is_read_only(argv):
            _fw_invalidate()
    finally:
        _cli_lock.release()

    truncated = len(out) > CLI_MAX_OUTPUT
    if truncated:
        out = out[:CLI_MAX_OUTPUT] + "\n... [truncated]"
    # audit trail -> journal (halow-webd.service)
    print("[cli] cmd=%r rc=%d ms=%d bytes=%d%s"
          % (" ".join(argv), rc, ms, len(out), " WRITE" if write_ok else ""),
          flush=True)
    return 200, {"rc": rc, "ms": ms, "out": out, "truncated": truncated,
                 "timeout": rc == -1 and not out}


# Units the log viewer may read. Anything else is refused rather than passed
# to journalctl, so the browser cannot read arbitrary system logs.
LOG_UNITS = ("halow-wlan", "halow-hostapd", "halow-webd", "halow-fwlog",
             "halow.target")


def journal_tail(unit, lines):
    if unit not in LOG_UNITS:
        return 403, {"error": "unit not available"}
    lines = max(10, min(int(lines or 200), 1000))
    out = run(["journalctl", "-u", unit + (".service" if unit != "halow.target" else ""),
               "-n", str(lines), "--no-pager", "--output", "short-iso"], timeout=15)
    return 200, {"unit": unit, "lines": lines, "text": out or "(no entries)"}


# ---------------------------------------------------------------- firmware log
# The firmware ships its console over the host interface (HIF_TYPE_LOG); the
# driver republishes it on the "nrc-log" netlink group and halow-fwlog.service
# stores it here. On a production board with no UART header these files are
# the only way to read the firmware log.
#
# fwlog-store.py keeps a bounded set: fwlog.log is the one being written, and
# each fwlog-<date>-<time>.log is a closed-off earlier one. Listing, download
# and delete all work off exactly that naming.
FWLOG_DIR = "/var/log/halow/fwlog"
FWLOG_CURRENT = "fwlog.log"
FWLOG_PATH = os.path.join(FWLOG_DIR, FWLOG_CURRENT)
# Where the previous package wrote its single unrotated file. fwlog-store.py
# adopts it on first start, but a webd that comes up before the follower does
# should still show it rather than an empty panel.
FWLOG_LEGACY = "/var/log/halow/fwlog.log"
FWLOG_PIDFILE = "/run/halow/fwlog-store.pid"
# One reading of the firmware's log settings, taken by fwlog-defaults.sh when
# the firmware loads and rewritten whenever the GUI asks the chip itself. On
# tmpfs on purpose: a reading must not outlive the firmware that gave it.
FWLOG_STATE_CACHE = "/run/halow/fwlog-state.txt"
FWLOG_UNIT = "halow-fwlog.service"
FWLOG_TAIL_MAX = 64 * 1024

# Archive names, as produced above. Matching the exact shape is what keeps a
# traversal attempt from reaching the filesystem, the same discipline the
# core-dump handlers use.
_FWLOG_ARCHIVE = re.compile(r"fwlog-(\d{8}-\d{6})(?:-(\d+))?\.log")


def fwlog_supported():
    """Whether this build has the firmware log channel at all.

    The feature spans the firmware (which publishes the log as HIF frames),
    the driver (which republishes them on a netlink group) and cli_app (whose
    -lf follows that group). A tree that has not picked it up has none of the
    three, and install-halow-services.sh records the verdict here after
    probing cli_app - so the panel can say "this build has no firmware log"
    instead of showing controls that answer nothing.

    Absent key means supported: every package that carries the feature
    predates the marker, and a missing marker must not switch a working panel
    off.
    """
    val = read_kv_file(SERVICE_CONF).get("FWLOG_SUPPORTED", "").strip()
    return val not in ("0", "no", "false")


def _fwlog_age_key(name):
    """(timestamp, collision counter) from an archive name.

    Not a string sort: two rotations inside one second give fwlog-<ts>.log
    and fwlog-<ts>-1.log, and '-' sorts before '.', so a string sort would
    present the older of the two as the newer.
    """
    m = _FWLOG_ARCHIVE.fullmatch(name)
    return (m.group(1), int(m.group(2) or 0)) if m else ("", 0)


def fwlog_current_path():
    """The log being written now, falling back to the pre-rotation path so a
    part-upgraded box still shows something."""
    if os.path.isfile(FWLOG_PATH) or not os.path.isfile(FWLOG_LEGACY):
        return FWLOG_PATH
    return FWLOG_LEGACY


def _fwlog_locate(name):
    """Full path of a log file by name, or None if the name is not one the
    store produces."""
    if name == FWLOG_CURRENT:
        return fwlog_current_path()
    if _FWLOG_ARCHIVE.fullmatch(name or ""):
        return os.path.join(FWLOG_DIR, name)
    return None


def fwlog_files():
    """Current log first, then archives newest-first."""
    items = []
    cur = fwlog_current_path()
    try:
        st = os.stat(cur)
        items.append({"name": FWLOG_CURRENT, "size": st.st_size,
                      "mtime": int(st.st_mtime), "current": True})
    except OSError:
        pass
    arch = []
    try:
        names = os.listdir(FWLOG_DIR)
    except OSError:
        names = []
    for n in names:
        if not _FWLOG_ARCHIVE.fullmatch(n):
            continue
        try:
            st = os.stat(os.path.join(FWLOG_DIR, n))
        except OSError:
            continue
        arch.append({"name": n, "size": st.st_size,
                     "mtime": int(st.st_mtime), "current": False})
    arch.sort(key=lambda x: _fwlog_age_key(x["name"]), reverse=True)
    items += arch
    return {"dir": FWLOG_DIR, "files": items,
            "total_bytes": sum(f["size"] for f in items)}


def fwlog_delete(names):
    """Remove saved logs by name. The log being written is refused: deleting
    it under the writer would silently send the stream to an unlinked inode
    until the next rotation."""
    removed, failed = [], []
    for name in names or []:
        if name == FWLOG_CURRENT:
            failed.append({"name": name, "error": "the current log cannot be "
                                                  "deleted; save it first"})
            continue
        path = _fwlog_locate(name)
        if path is None:
            failed.append({"name": name, "error": "not a firmware log file"})
            continue
        try:
            os.remove(path)
            removed.append(name)
        except OSError as e:
            failed.append({"name": name, "error": str(e)})
    if removed:
        print("[fwlog] deleted: %s" % ", ".join(removed), flush=True)
    return {"ok": not failed, "removed": removed, "failed": failed}


def fwlog_rotate():
    """Close the current log off under its own name, so it can be downloaded
    as a fixed file. SIGHUP to the store is the whole mechanism."""
    # The store skips archiving an empty file (correctly - an empty archive
    # is clutter), so saying "saved" for one would be a lie the file list
    # immediately exposes. Say what actually happened instead.
    try:
        if os.path.getsize(fwlog_current_path()) == 0:
            return {"ok": False, "error": "the current log is empty - "
                                          "there is nothing to save yet"}
    except OSError:
        return {"ok": False, "error": "there is no current log yet "
                                      "(is halow-fwlog active?)"}
    try:
        pid = int(open(FWLOG_PIDFILE).read().strip())
    except (OSError, ValueError):
        return {"ok": False, "error": "the log store is not running "
                                      "(is halow-fwlog active?)"}
    # A pidfile can outlive its process and the number can have been reused,
    # so check what that pid actually is before signalling it.
    try:
        cmdline = open("/proc/%d/cmdline" % pid, "rb").read().decode(
            "utf-8", "replace")
    except OSError:
        return {"ok": False, "error": "the log store is not running"}
    if "fwlog-store" not in cmdline:
        return {"ok": False, "error": "stale pidfile"}
    try:
        os.kill(pid, signal.SIGHUP)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    time.sleep(0.3)     # let the rename land before the caller re-lists
    return {"ok": True, "files": fwlog_files()}


def fwlog_state(probe=True):
    """Firmware-side settings plus the host-side plumbing around them.

    `probe` False sends nothing to the firmware: the follower unit and the
    file on disk are the host's own, and the firmware's settings come from the
    reading cached when it loaded. Asking the chip costs a shell command it
    echoes into the very log being read, so that happens only when somebody
    asks for it. `at` dates whichever reading came back; `unknown` says there
    is no reading at all, which the caller must show as absent rather than
    dressing up a default to look like one.
    """
    st = {"mode": None, "level": None, "maxlevel": None, "cli": None,
          "running": None, "ring_used": None, "ring_size": None, "stats": {},
          "service": unit_active(FWLOG_UNIT), "bytes": 0, "error": None,
          "unknown": False, "at": None, "supported": fwlog_supported()}
    if not st["supported"]:
        # No firmware-log channel in this build. Say so plainly: the panel
        # then explains itself instead of showing dead controls over a tail
        # that will never fill, and probing would only add a failed command.
        return st
    try:
        st["bytes"] = os.path.getsize(fwlog_current_path())
    except OSError:
        pass

    if not probe:
        # The stack takes one reading when the firmware loads and leaves it
        # here, so the panel can open with live controls without anybody
        # sending a command. Counters in it are as old as the file, which is
        # why the reading is dated for the reader.
        try:
            out = open(FWLOG_STATE_CACHE).read()
            at = os.path.getmtime(FWLOG_STATE_CACHE)
        except OSError:
            st["unknown"] = True
            return st
        if "mode(" not in out:
            st["unknown"] = True
            return st
        st["at"] = at
        _fwlog_parse_show(out, st)
        return st

    code, res = cli_run(["hlog", "show"])
    if code != 200:
        st["error"] = res.get("error", "cli_app failed")
        return st
    out = res.get("out", "")
    if "mode(" not in out:
        # cli_app exits 0 whether or not the firmware answered, so `rc` says
        # nothing; the reply does. Without this the panel got mode=None and
        # error=None together, which an older GUI drew as "off" - a setting
        # nobody made, on a firmware that had simply not been heard from.
        st["error"] = ("the firmware did not answer `hlog show`"
                       + (" (%s)" % out.strip()[:40] if out.strip() else ""))
        return st

    st["at"] = time.time()
    _fwlog_parse_show(out, st)
    # Keep the cache current, so the next page load - or a second browser -
    # starts from this reading instead of an older one or none at all.
    _fwlog_cache_write(out)
    return st


def _fwlog_parse_show(out, st):
    """Fill `st` from the `name(value)` pairs `hlog show` prints."""
    # the same shape the Diagnostics tab already renders
    pairs = dict(re.findall(r"(\w+)\(([^)]*)\)", out))
    st["mode"] = pairs.get("mode")
    st["level"] = pairs.get("level")
    # Trace macros are compiled out per build flavour, so the firmware also
    # reports the most verbose level this image can emit at all. Older
    # firmware does not send it; treat that as "no ceiling known".
    st["maxlevel"] = pairs.get("maxlevel")
    st["cli"] = pairs.get("cli") == "1"
    st["running"] = pairs.get("run") == "1"
    ring = pairs.get("ring", "")
    if "/" in ring:
        used, _, size = ring.partition("/")
        try:
            st["ring_used"], st["ring_size"] = int(used), int(size)
        except ValueError:
            pass
    # `lost` is bytes the ring overwrote to make room; firmware older than
    # today reports `drop`, a count of refused lines, which the GUI still
    # understands so a mixed package does not lose the number.
    for k in ("line", "lost", "drop", "frame", "sent", "skip", "fail", "rxbuf"):
        if k in pairs:
            try:
                st["stats"][k] = int(pairs[k])
            except ValueError:
                pass


def _fwlog_cache_write(out):
    """Replace the cached reading. Best effort - a failure here must not turn
    a working query into an error, it only costs the next reader a Refresh."""
    try:
        os.makedirs(os.path.dirname(FWLOG_STATE_CACHE), exist_ok=True)
        tmp = FWLOG_STATE_CACHE + ".tmp"
        with open(tmp, "w") as f:
            f.write(out)
        os.rename(tmp, FWLOG_STATE_CACHE)    # readers never see it half-written
    except OSError:
        pass


def fwlog_tail(offset, gen=None):
    """Incremental read of the log file.

    `offset` is a byte position, -1 to attach at the end. Truncation (a
    rotation, or the file being recreated) shows up as the file being
    shorter than the caller's offset; restart from 0 rather than seeking
    past the end and reporting silence forever.

    Both repositionings mean the viewer is not seeing everything, so say
    which one happened and how much was passed over. A log with a hole in it
    that looks continuous is worse than no log: it invites conclusions drawn
    from evidence that was never there.
    """
    path = fwlog_current_path()
    try:
        st = os.stat(path)
    except OSError:
        return {"offset": 0, "size": 0, "gen": None, "text": "",
                "note": "no log yet (is halow-fwlog running?)"}
    size = st.st_size

    jumped, skipped = None, 0
    if offset is None or offset < 0:
        # first attach: start near the end rather than shipping the whole file
        offset = max(0, size - FWLOG_TAIL_MAX)
        if offset > 0:
            jumped, skipped = "tail", offset
    elif offset > size or (gen is not None and gen != st.st_ino):
        # The file shrank under us, or it is a different file altogether:
        # the store rotated it. Size alone misses a rotation followed by a
        # burst of new output larger than the caller's offset - a chatty
        # stream with the tab in the background and the poll throttled -
        # which is why the caller also hands back the inode it last read.
        jumped, skipped = "rotated", 0
        offset = 0

    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(FWLOG_TAIL_MAX)
    except OSError as e:
        return {"offset": offset, "size": size, "text": "", "note": str(e)}

    return {"offset": offset + len(data), "size": size, "gen": st.st_ino,
            "jumped": jumped, "skipped_bytes": skipped,
            "text": data.decode("utf-8", "replace")}


# ------------------------------------------------------------- symbolisation
# Resolving a firmware address to a function is what turns a core dump or a
# stack dump into something you can act on. Doing it with addr2line would
# mean shipping an ARM toolchain and the ELF to every board, so instead we
# read the symbol table the build emits next to the firmware
# (arm-none-eabi-nm -S -n, i.e. "<addr> <size> <type> <name>") and do the
# lookup here. That gives function+offset without any external tool; file
# and line still need addr2line on a workstation, and the CLI decoder does
# that when it is available.
#
# The table only exists in a debug package: it is a complete map of the
# firmware's internals, so make_pkg.sh deliberately leaves it out of a
# release. On a release box these lookups return nothing and the addresses
# stay as hex, which is the intended behaviour rather than a fault.
_sym_cache = {"path": None, "mtime": 0, "table": None}


def fw_dir():
    script = read_kv_file(SERVICE_CONF).get("NRC_SCRIPT_DIR", "")
    return os.path.join(os.path.dirname(script), "sw", "firmware") if script else ""


def symbol_file():
    """The symbol table shipped alongside the running firmware, if any."""
    d = fw_dir()
    if not d:
        return None
    for name in ("uni_s1g.sym", "uni_s1g.nm",
                 "nrc7394_cspi_mcp.sym", "nrc7394_cspi.sym"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def symbol_table():
    """[(start, end, name)] sorted by address, or None when no map is present."""
    path = symbol_file()
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _sym_cache["path"] == path and _sym_cache["mtime"] == mtime:
        return _sym_cache["table"]

    table = []
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                parts = line.split()
                # "<addr> <size> <type> <name>"; only code symbols, and only
                # with a size, so an address can be checked against the
                # function's extent instead of being attached to whichever
                # symbol happens to precede it.
                if len(parts) != 4 or parts[2] not in ("T", "t", "W", "w"):
                    continue
                try:
                    addr, size = int(parts[0], 16), int(parts[1], 16)
                except ValueError:
                    continue
                table.append((addr, addr + max(size, 1), parts[3]))
    except OSError:
        return None

    table.sort()
    _sym_cache.update(path=path, mtime=mtime, table=table)
    return table


def symbolize(addr):
    """'function+0x12' for a code address, or None."""
    table = symbol_table()
    if not table or not addr:
        return None
    a = addr & ~1                       # Thumb addresses carry bit 0 set
    i = bisect.bisect_right(table, (a, float("inf"), "")) - 1
    if i < 0:
        return None
    start, end, name = table[i]
    if not (start <= a < end):
        return None
    off = a - start
    return name if off == 0 else "%s+0x%x" % (name, off)


def code_range():
    """Address span covered by the symbol table, used to tell a return
    address on the stack from an ordinary data word."""
    table = symbol_table()
    if not table:
        return None
    return (table[0][0], table[-1][1])


# ---------------------------------------------------------------- core dumps
# The driver writes firmware core dumps here; the directory is a module
# parameter, so read the value the running driver actually has rather than
# assuming the default.
def coredump_dir():
    for mod in ("nrc_core", "nrc"):
        try:
            with open("/sys/module/%s/parameters/dump_dir" % mod) as f:
                d = f.read().strip()
                if d:
                    return d
        except OSError:
            continue
    return "/var/log"


# Two name patterns, in two places. The current driver writes
# nrc_core_dump_<epoch>.bin into its dump_dir; the older one wrote
# host_core_dump_<n>.bin relative to the kernel's cwd, which is "/". A board
# still running the shipped driver produces the latter, so look for both
# rather than show an empty panel next to a filesystem full of dumps.
_CD_PATTERNS = ((r"nrc_core_dump_\d+\.bin", None),
                (r"host_core_dump_\d+\.bin", "/"))


def _coredump_locate(name):
    """Directory a dump of this name lives in, or None if the name is not one
    the driver produces. Matching the exact pattern is also what keeps a
    traversal attempt from ever reaching the filesystem."""
    for pattern, fixed_dir in _CD_PATTERNS:
        if re.fullmatch(pattern, name or ""):
            return fixed_dir or coredump_dir()
    return None


def coredump_list():
    d = coredump_dir()
    items, seen = [], set()
    for _pattern, fixed_dir in _CD_PATTERNS:
        where = fixed_dir or d
        if where in seen:
            continue
        seen.add(where)
        try:
            names = os.listdir(where)
        except OSError:
            continue
        for name in names:
            if not any(re.fullmatch(p, name) for p, _f in _CD_PATTERNS):
                continue
            try:
                stat = os.stat(os.path.join(where, name))
            except OSError:
                continue
            items.append({"name": name, "size": stat.st_size,
                          "mtime": int(stat.st_mtime)})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"dir": d, "dumps": items[:50]}


# ARMv7-M Configurable Fault Status Register, split into its three bytes.
# Turning 0x00008200 into "precise bus fault, BFAR valid" is the difference
# between a dump you can act on and a hex blob you forward to somebody else.
_CFSR_BITS = (
    (0,  "IACCVIOL",   "instruction fetch from a forbidden address"),
    (1,  "DACCVIOL",   "data access to a forbidden address"),
    (3,  "MUNSTKERR",  "MPU fault unstacking on exception return"),
    (4,  "MSTKERR",    "MPU fault stacking on exception entry"),
    (5,  "MLSPERR",    "MPU fault during lazy FP state save"),
    (7,  "MMARVALID",  "MMFAR holds the faulting address"),
    (8,  "IBUSERR",    "bus fault on an instruction fetch"),
    (9,  "PRECISERR",  "precise bus fault - BFAR holds the address"),
    (10, "IMPRECISERR", "imprecise bus fault - a late write went bad"),
    (11, "UNSTKERR",   "bus fault unstacking on exception return"),
    (12, "STKERR",     "bus fault stacking on exception entry "
                       "(classically a stack overflow)"),
    (13, "LSPERR",     "bus fault during lazy FP state save"),
    (15, "BFARVALID",  "BFAR holds the faulting address"),
    (16, "UNDEFINSTR", "undefined instruction"),
    (17, "INVSTATE",   "invalid state - Thumb bit lost, or a bad function "
                       "pointer"),
    (18, "INVPC",      "invalid PC on exception return"),
    (19, "NOCP",       "coprocessor access with the coprocessor disabled"),
    (24, "UNALIGNED",  "unaligned access"),
    (25, "DIVBYZERO",  "divide by zero"),
)

_HFSR_BITS = (
    (1,  "VECTTBL",  "vector table read fault"),
    (30, "FORCED",   "escalated from a fault that was disabled or masked"),
    (31, "DEBUGEVT", "debug event"),
)

# The exception number in ICSR's low bits. 0-15 are architectural; the rest
# are this SoC's interrupt lines, carried over from the original
# decode_core_dump.py so a dump still says "WDT" rather than "exception 19".
_EXCEPTIONS = {
    0: "thread mode", 1: "reset", 2: "NMI", 3: "hard fault",
    4: "memory management fault", 5: "bus fault", 6: "usage fault",
    11: "SVC", 12: "debug monitor", 14: "PendSV", 15: "SysTick",
    16: "PMU", 17: "TIMER4", 18: "RTC", 19: "WDT", 20: "TIMER0",
    21: "HMAC0", 22: "DMACINTTC", 23: "TIMER5", 24: "TIMER1",
    25: "HSUART0", 26: "SSP0", 27: "PWR_FAIL", 28: "LPO",
    29: "DMACINTERR", 30: "HIF", 31: "HSUART3", 32: "TIMER2",
    33: "HSUART1", 34: "SSP1", 35: "I2C", 36: "HMAC1", 37: "HMAC2",
    38: "HIF_TXDONE", 39: "HIF_RXDONE", 40: "TIMER3", 41: "HSUART2",
    42: "MBXTX", 43: "MBXRX", 44: "EXT0", 45: "EXT1",
    46: "TXQUE", 47: "RXQUE",
}

# The 15 words the firmware writes are the exception stack frame followed by
# the fault status registers -- not eight anonymous "stack" words.
#
# The fifth word is R12, not R13: an ARMv7-M exception frame is
# R0,R1,R2,R3,R12,LR,PC,xPSR. The firmware's own console report and the
# original decode_core_dump.py both called it R13, which invites reading a
# scratch register's leftover value as a corrupted stack pointer.
_CPU_REGS = ("R0", "R1", "R2", "R3", "R12", "LR", "PC", "PSR",
             "CFSR", "HFSR", "DFSR", "AFSR", "BFAR", "MMAR", "ICSR")

# Container framing written by util_core_dump.c
COREDUMP_MAGIC = 0x4443524E     # 'NRCD'
CD_SEC = {0x0000: "end", 0x0001: "version", 0x0002: "meta",
          0x0003: "reason", 0x0004: "cpu", 0x0005: "lmac",
          0x0006: "qm", 0x0007: "dl", 0x0008: "phy",
          0x0009: "hostif", 0x000A: "buftrack"}


def _cd_sections(blob):
    """Walk the type/length framing.

    Returns (sections, terminated) where sections is a list of
    (type, name, payload) and payload is None for a section whose declared
    length runs past the end of the file. `terminated` says whether the
    CD_SEC_END marker was reached, which is how a cut-short transfer is
    told apart from a dump that simply carried fewer sections.
    """
    out, off = [], 8                            # magic + format + reserved
    while off + 4 <= len(blob):
        # A section header is a pair of 16-bit fields, and the byte stream
        # aligns before writing one, so a section whose payload ended on an
        # odd byte is followed by one pad byte. Skip to the next even offset
        # rather than reading the header off by one.
        off += off & 1
        if off + 4 > len(blob):
            break
        stype, slen = struct.unpack_from("<HH", blob, off)
        off += 4
        if stype == 0x0000:                     # CD_SEC_END
            return out, True
        name = CD_SEC.get(stype, "0x%04X" % stype)
        if off + slen > len(blob):
            out.append((stype, name, None))
            return out, False
        out.append((stype, name, blob[off:off + slen]))
        off += slen
    return out, False


def _cd_lead_pad(payload, expected):
    """Bytes of alignment padding the byte stream inserted before a section's
    first word. It pads only ahead of the first aligned put -- consecutive
    words stay aligned, and a buffer boundary realigns without padding -- so
    any excess over the expected size sits at the front."""
    pad = len(payload) - expected
    return pad if 0 <= pad <= 3 else None


def coredump_parse(blob):
    """Decode a firmware core dump.

    The dump is a framed container: an 'NRCD' header followed by
    type/length-tagged sections (see util/util_core_dump.c). Framing means a
    reader can decode the sections it knows, skip the ones it does not, and
    notice when one is a different size than expected -- rather than the old
    arrangement, where a bare concatenation left every reader hardcoding the
    firmware's field layout and silently mis-decoding after any change.
    """
    out = {"size": len(blob), "ok": False, "error": None, "format": None,
           "version": None, "meta": None, "reason": None, "regs": {},
           "fault": {}, "sections": [], "notes": []}

    if len(blob) < 8:
        out["error"] = "too short to be a dump (%d bytes)" % len(blob)
        return out

    magic, fmt, _rsvd = struct.unpack_from("<IHH", blob, 0)
    if magic != COREDUMP_MAGIC:
        out["error"] = ("not a framed core dump (magic 0x%08X). Dumps from "
                        "firmware older than the framed format need the "
                        "legacy decode_core_dump.py." % magic)
        return out
    out["format"] = fmt

    sections, terminated = _cd_sections(blob)
    for stype, name, payload in sections:
        if payload is None:
            out["notes"].append("Section '%s' is truncated - the transfer was "
                                "cut short." % name)
            break
        out["sections"].append({"type": stype, "name": name,
                                "len": len(payload)})

        if name == "version" and len(payload) >= 4:
            slen = payload[3]
            out["version"] = {
                "fw": "%02d.%02d.%02d" % (payload[0], payload[1], payload[2]),
                "build": payload[4:4 + slen].decode("ascii", "replace"),
            }
        elif name == "reason":
            out["reason"] = payload.split(b"\0")[0].decode("ascii", "replace")
        elif name == "meta":
            pad = _cd_lead_pad(payload, 20)
            if pad is not None:
                (tlo, thi, heap, rx, tx, rxn, txn) = struct.unpack_from(
                    "<IIIHHHH", payload, pad)
                tsf = (thi << 32) | tlo
                out["meta"] = {
                    "uptime_ms": tsf // 1000,
                    "free_heap": heap,
                    "pool_rx": rx, "pool_rx_total": rxn,
                    "pool_tx": tx, "pool_tx_total": txn,
                }
                # Pool exhaustion is a recurring failure mode here, and it is
                # invisible in the registers; call it out where it shows.
                if rxn and rx * 10 < rxn:
                    out["notes"].append("The RX buffer pool was nearly empty "
                                        "(%d of %d free) when the dump was "
                                        "taken." % (rx, rxn))
                if txn and tx * 10 < txn:
                    out["notes"].append("The TX buffer pool was nearly empty "
                                        "(%d of %d free) when the dump was "
                                        "taken." % (tx, txn))
        elif name == "cpu":
            pad = _cd_lead_pad(payload, 60)
            if pad is None:
                out["notes"].append("CPU section is %d bytes, expected 60 - "
                                    "skipping it." % len(payload))
                continue
            words = struct.unpack_from("<15I", payload, pad)
            out["regs"] = dict(zip(_CPU_REGS, words))
            cfsr, hfsr, icsr = words[8], words[9], words[14]
            out["fault"] = {
                "cfsr": cfsr, "hfsr": hfsr,
                "bfar": words[12], "mmar": words[13],
                "bfar_valid": bool(cfsr & (1 << 15)),
                "mmar_valid": bool(cfsr & (1 << 7)),
                "exception": icsr & 0x1FF,
                "exception_name": _EXCEPTIONS.get(icsr & 0x1FF,
                                                  "IRQ %d" % (icsr & 0x1FF)),
                "cfsr_flags": [{"bit": b, "name": n, "meaning": d}
                               for b, n, d in _CFSR_BITS if cfsr & (1 << b)],
                "hfsr_flags": [{"bit": b, "name": n, "meaning": d}
                               for b, n, d in _HFSR_BITS if hfsr & (1 << b)],
            }
        elif name == "qm":
            out["qm"] = _cd_parse_qm(payload, out["notes"])

    out["complete"] = terminated
    if not terminated:
        out["notes"].append("No end-of-dump marker - the transfer was "
                            "probably cut short.")

    # Resolve the addresses that say where it happened.
    out["symbols"] = {k: symbolize(v) for k, v in (out["regs"] or {}).items()
                      if k in ("PC", "LR")}
    out["has_symbols"] = symbol_table() is not None
    if not out["has_symbols"]:
        out["notes"].append(
            "Addresses are not resolved to function names: the symbol table "
            "ships only with a debug package, since it also maps the whole "
            "firmware. Re-analyse this dump on a box running a debug "
            "package, or decode it on a workstation with "
            "decode_core_dump.py --elf <firmware>.elf.")

    out["diagnosis"] = _cd_diagnose(out)
    out["ok"] = True
    return out


def _cd_diagnose(d):
    """Plain-language reading of the dump: what happened, and where.

    Everything here is already in the fields above; the point is to save the
    reader from having to know that "an assert with no fault bits" and "a
    usage fault with INVSTATE" mean quite different things, and to put the
    likely culprit in one sentence.
    """
    f = d.get("fault") or {}
    regs = d.get("regs") or {}
    syms = d.get("symbols") or {}
    reason = d.get("reason") or ""
    meta = d.get("meta") or {}
    lines, where = [], []

    pc, lr = syms.get("PC"), syms.get("LR")
    if pc:
        where.append("PC in %s" % pc)
    if lr and lr != pc:
        where.append("called from %s" % lr)

    if reason.startswith("ASSERT"):
        headline = "The firmware stopped itself: an assertion failed."
        m = re.search(r"ASSERT at 0x([0-9A-Fa-f]{8})", reason)
        if m:
            # release image: the site is an address, not a string
            site = symbolize(int(m.group(1), 16))
            lines.append(
                "This image records the assert's address rather than its "
                "text%s. Run addr2line against the firmware .elf of this "
                "exact build for the file and line; the source line is the "
                "condition that failed." % (" (%s)" % site if site else ""))
        else:
            lines.append("The assertion text below names the file and line. ")
        lines.append("The registers are zero because nothing faulted - the "
                     "code decided the state was impossible and halted.")
        lines.append("What led up to it is in the firmware log, not here.")
    elif reason.startswith("requested"):
        headline = "Not a crash - this dump was taken on request."
        lines.append("It is a snapshot of a running system, so the fault "
                     "registers are empty by design.")
    elif f.get("cfsr") or f.get("hfsr"):
        names = [x["name"] for x in f.get("cfsr_flags", [])]
        headline = "The CPU faulted (%s)." % (", ".join(names) or "hard fault")
        # The specific bits are what point at a cause.
        if "STKERR" in names or "MSTKERR" in names:
            lines.append("The fault happened while the CPU was pushing "
                         "registers onto the stack for an exception, which "
                         "usually means the stack had run past its limit. "
                         "Look at the task that was running and its stack "
                         "size.")
        if "INVSTATE" in names:
            lines.append("The CPU was asked to branch to an address with the "
                         "Thumb bit clear - a corrupted or uninitialised "
                         "function pointer, or a jump into data.")
        if "UNDEFINSTR" in names:
            lines.append("The CPU reached bytes that are not an instruction: "
                         "execution ran off into data, or the image is "
                         "damaged.")
        if "PRECISERR" in names and f.get("bfar_valid"):
            lines.append("A data access to 0x%08X was rejected by the bus. "
                         "That address is the thing to explain."
                         % f.get("bfar", 0))
        if "IMPRECISERR" in names:
            lines.append("The bus error surfaced late, so PC points after the "
                         "access that caused it rather than at it.")
        if "IBUSERR" in names:
            lines.append("The failure was fetching an instruction, so the "
                         "branch target itself is wrong.")
        if not lines:
            lines.append("See the fault bits below for what the CPU objected "
                         "to.")
    else:
        headline = "The dump carries no fault and no reason."
        lines.append("It was most likely taken by firmware older than the "
                     "framed format, or by a path that does not set one.")

    if where:
        lines.insert(0, "It happened with " + ", ".join(where) + ".")
    elif regs.get("PC") and not d.get("has_symbols"):
        lines.append("PC was 0x%08X; with a symbol table it would be named."
                     % regs["PC"])

    # Pool exhaustion is a recurring cause here and is invisible in the
    # registers, so say it plainly when the numbers show it.
    for tag, free, total in (("RX", meta.get("pool_rx"), meta.get("pool_rx_total")),
                             ("TX", meta.get("pool_tx"), meta.get("pool_tx_total"))):
        if free is not None and total and free * 10 < total:
            lines.append("The %s buffer pool was down to %d of %d when this "
                         "was taken - worth treating as a cause, not a "
                         "symptom." % (tag, free, total))

    return {"headline": headline, "detail": lines}


def _cd_parse_qm(payload, notes):
    """Queue-manager counters.

    The section now leads with its own dimensions, so the table is laid out
    from the dump rather than from constants copied into the reader. That is
    the fix for the failure the old decoder shipped with: MAX_AC grew from 6
    to 11 and it kept reading six columns.
    """
    off = 0
    if len(payload) < 10:
        return None
    n_ac, n_stats, n_credit, n_state, n_extra = struct.unpack_from(
        "<HHHHH", payload, off)
    off += 10
    if not (0 < n_ac <= 32 and 0 < n_stats <= 64):
        notes.append("Queue-manager section header looks wrong "
                     "(%d ACs, %d stats) - not decoding it." % (n_ac, n_stats))
        return None

    need = n_stats * n_ac * 2 + n_credit * n_ac * 4 + n_ac + \
        n_state * n_ac * 2 + n_extra * 2
    if off + need > len(payload):
        notes.append("Queue-manager section is shorter than its own header "
                     "says it should be.")
        return None

    stats = []
    for _ in range(n_stats):
        stats.append(list(struct.unpack_from("<%dH" % n_ac, payload, off)))
        off += 2 * n_ac
    credit = []
    for _ in range(n_credit):
        off += (-off) % 4
        credit.append(list(struct.unpack_from("<%di" % n_ac, payload, off)))
        off += 4 * n_ac

    return {"n_ac": n_ac, "stats": stats, "credit": credit}

def coredump_delete(names):
    """Remove dumps by name. Only names matching a driver-produced pattern
    are touched, so this cannot be pointed at anything else."""
    removed, failed = [], []
    for name in names or []:
        where = _coredump_locate(name)
        if where is None:
            failed.append({"name": name, "error": "not a dump file"})
            continue
        try:
            os.remove(os.path.join(where, os.path.basename(name)))
            removed.append(name)
        except OSError as e:
            failed.append({"name": name, "error": str(e)})
    if removed:
        print("[coredump] deleted: %s" % ", ".join(removed), flush=True)
    return {"ok": not failed, "removed": removed, "failed": failed}


# --------------------------------------------------------- stack dump reader
# util_trace_stack.c prints one word per line as "\t<addr>: 0x<value>", with
# " <-- SP" on the line holding the stack pointer, under a task name and an
# optional "EXCEPTION[n]" header. Reading a call stack out of that means
# picking the values that are code addresses -- return addresses left behind
# by the calls that are still on the stack -- which is exactly what
# tool/DumpParser/stack_addr2line_batch.py does by hand.
_STACK_WORD = re.compile(r"([0-9A-Fa-f]{8})\s*:\s*0x([0-9A-Fa-f]{8})(\s*<-- SP)?")


def stack_dump_scan(text):
    """Stack-dump blocks in a chunk of log. Returns a list of
    {start, end, task, words} where words are (slot, value, is_sp)."""
    lines = text.splitlines()
    blocks, cur = [], None

    for i, line in enumerate(lines):
        m = _STACK_WORD.search(line)
        if m:
            if cur is None:
                # Look back a few lines for the task name / exception header
                # the dumper prints immediately before the words.
                head = ""
                for j in range(max(0, i - 4), i):
                    t = re.sub(r"^\[FW/\w+\]\s*", "", lines[j])
                    t = re.sub(r"^\[[^\]]*\]\[[^\]]*\]\s*", "", t).strip()
                    if t and not _STACK_WORD.search(t):
                        head = t
                cur = {"start": i, "task": head, "words": []}
            cur["words"].append((int(m.group(1), 16), int(m.group(2), 16),
                                 bool(m.group(3))))
        elif cur is not None:
            cur["end"] = i
            blocks.append(cur)
            cur = None

    if cur is not None:
        cur["end"] = len(lines)
        blocks.append(cur)

    # A couple of stray "xxxxxxxx: 0x…" lines are not a stack dump.
    return [b for b in blocks if len(b["words"]) >= 8]


_FAULT_LINE = re.compile(
    r"\[FAULT\][^\n]*?CFSR=([0-9A-Fa-f]+)[^\n]*?HFSR=([0-9A-Fa-f]+)"
    r"[^\n]*?PC=([0-9A-Fa-f]+)")
# Two shapes, because a release image no longer carries the assert text: the
# debug form names the expression, the release form gives the address of the
# assert site instead (see ASSERT() in system/system_macro.h). The address is
# the more useful of the two here, since it can be resolved to a function.
_ASSERT_LINE = re.compile(r"ASSERT\([^)]*\)[^\n]*")
_ASSERT_PC_LINE = re.compile(r"ASSERT at 0x([0-9A-Fa-f]{8})[^\n]*")


def fault_sites(text):
    """The crash sites the log itself names.

    A stack dump is walked from the current stack pointer, so in a firmware
    that prints while it walks, the words nearest the top belong to the
    printing. The [FAULT] line, by contrast, carries the PC the CPU actually
    faulted at -- resolve that and the answer is one line rather than a hunt
    through the frames.
    """
    sites = []
    for m in _FAULT_LINE.finditer(text):
        pc = int(m.group(3), 16)
        sites.append({"kind": "fault", "cfsr": int(m.group(1), 16),
                      "hfsr": int(m.group(2), 16), "pc": pc,
                      "sym": symbolize(pc), "text": m.group(0)})
    for m in _ASSERT_LINE.finditer(text):
        sites.append({"kind": "assert", "text": m.group(0).strip()})
    for m in _ASSERT_PC_LINE.finditer(text):
        pc = int(m.group(1), 16)
        sites.append({"kind": "assert", "pc": pc, "sym": symbolize(pc),
                      "text": m.group(0).strip()})
    # Newest first - after a crash it is the last one that matters - and one
    # entry per distinct site: the log holds every crash the board has had,
    # and repeating an identical line reads as if it happened repeatedly.
    uniq, seen = [], set()
    for s in reversed(sites):
        key = s.get("text") if s["kind"] == "assert" else (s["pc"], s["cfsr"])
        if key in seen:
            for prev in uniq:
                if prev.get("_key") == key:
                    prev["seen"] += 1
                    break
            continue
        seen.add(key)
        s["_key"], s["seen"] = key, 1
        uniq.append(s)
    for s in uniq:
        s.pop("_key", None)
    return uniq[:10]


def stack_dump_resolve(block):
    """Turn the words of one block into a call stack.

    Only values inside the code range are kept: the rest of a stack frame is
    saved registers and locals, and reporting those as callers would be
    noise dressed up as evidence.

    The stack is walked from the deepest address upwards, so the frames come
    out innermost-first. A loop leaves the same return address in several
    adjacent slots, so runs of one address collapse to a single entry with a
    count - repeating it verbatim reads like recursion that is not there.
    """
    rng = code_range()
    frames = []
    for slot, value, _is_sp in block.get("words", []):
        if not rng or not (rng[0] <= (value & ~1) < rng[1]):
            continue
        name = symbolize(value)
        if not name:
            continue
        if frames and frames[-1]["addr"] == value:
            frames[-1]["repeat"] += 1
            continue
        frames.append({"slot": slot, "addr": value, "sym": name, "repeat": 1,
                       "noise": name.startswith(_STACK_NOISE)})
    return frames


# The dumper prints while it walks, so the deepest frames are always the
# reporting machinery itself rather than anything to do with the failure.
# Flagging them lets the reader's eye land on the first frame that is
# actually about the crash. If one of these names ever changes the frame
# simply stops being flagged, which is the harmless direction to fail in.
_STACK_NOISE = ("_ntoa_", "_out_buffer", "_vsnprintf", "_snprintf", "_printf",
                "log_dma_", "stack_print_cb", "vIterateTaskStackHistory",
                "util_trace_stack_dump", "util_hif_logger", "hal_uart_",
                "nrc_hsuart_",
                # newlib's own formatting internals, which sit between the
                # dumper and anything of interest. Without these the "crash
                # context from here" marker lands on __ssputs_r, which is as
                # unhelpful as not marking anything.
                "__ssputs_r", "_svfprintf_r", "_svfiprintf_r", "_vfprintf_r",
                "_vfiprintf_r", "_printf_i", "_printf_common", "_dtoa_r",
                "__sprint_r", "_fflush_r", "__sfvwrite_r")


def fwlog_analyse(text):
    """Everything the log itself can be made to say about a crash: the sites
    it names, and a call stack per distinct stack dump.

    Shared by the GUI's "Resolve call stack" and by the support bundle, so a
    bundle carries the same reading an operator would have got on screen.
    """
    blocks = stack_dump_scan(text)
    out, seen = [], set()
    # A log file accumulates every crash the board has had, and a reboot
    # replays the same boot-time dump, so the same block arrives several
    # times. Show each distinct one once, newest first, with a count.
    for b in reversed(blocks):
        frames = stack_dump_resolve(b)
        key = (b["task"], tuple(f["addr"] for f in frames), len(b["words"]))
        if key in seen:
            for prev in out:
                if prev["_key"] == key:
                    prev["seen"] += 1
                    break
            continue
        seen.add(key)
        out.append({"_key": key, "task": b["task"], "seen": 1,
                    "words": len(b["words"]), "frames": frames})
    for b in out:
        b.pop("_key", None)
    return {"has_symbols": symbol_table() is not None,
            "sym_file": symbol_file(),
            "sites": fault_sites(text),
            "blocks": out}


def coredump_read(name):
    where = _coredump_locate(name)
    if where is None:
        return None
    try:
        with open(os.path.join(where, os.path.basename(name)), "rb") as f:
            return f.read(4 * 1024 * 1024)
    except OSError:
        return None


def _redact(txt):
    """Secrets never leave the box: passphrases, PSKs and hashes are blanked
    wherever they appear in confs or logs."""
    return re.sub(r"(?im)^(\s*#?\s*[^=:\n]*(passphrase|password|psk|_hash|sae_pass)[^=:\n]*[:=]).*$",
                  r"\1 <redacted>", txt or "")


def _sanitize(obj):
    """Recursive copy of a dict/list with secret-looking values blanked."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(w in lk for w in ("password", "passphrase", "psk")) or \
               lk.endswith("_hash"):
                out[k] = "<redacted>"
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _run_nice(cmd, timeout=15):
    """External commands in the bundle run niced (CPU + idle-IO) so an
    operator pulling a bundle from a struggling box does not add to the
    struggle."""
    pre = []
    if shutil.which("nice"):
        pre = ["nice", "-n", "10"]
    if shutil.which("ionice"):
        pre += ["ionice", "-c", "3"]
    return run(pre + cmd, timeout=timeout)


def _proc_conf_paths(name):
    """Conf files of a running daemon, read from its own command line - the
    start scripts write these under changing temp names, so the process is
    the one reliable source. hostapd takes the conf as a positional arg,
    wpa_supplicant as -c <path> / -c<path>."""
    paths = []
    out = run(["pgrep", "-a", "-x", name], timeout=5) or ""
    for line in out.splitlines():
        toks = line.split()[1:]
        for i, t in enumerate(toks):
            if t == "-c" and i + 1 < len(toks):
                paths.append(toks[i + 1])
            elif t.startswith("-c") and len(t) > 2:
                paths.append(t[2:])
            elif t.endswith(".conf") and name == "hostapd":
                paths.append(t)
    return [q for q in dict.fromkeys(paths) if os.path.isfile(q)]


# The firmware state a bug report needs, straight from cli_app. Order matters
# only for the reader; every command is independent and a failing one is
# recorded as such rather than aborting the bundle.
BUNDLE_CLI = [
    ["show", "version"], ["show", "config"], ["show", "duty"],
    ["show", "recovery", "stats"], ["show", "detection", "stats"],
    ["show", "temp"], ["show", "sta", "0", "all"], ["show", "ap", "0"],
    ["show", "tx_time"], ["show", "cca_thresh"], ["show", "sysconfig"],
    ["show", "mac", "tx", "stats"], ["show", "mac", "rx", "stats"],
]

REPORT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "report_template.html")

# How much of the firmware log a bundle carries. The current log goes in
# whole up to its cap; older ones are the archives, and taking every one of
# them would put ~18 MB of mostly-idle log into a file somebody has to
# e-mail. Three is a couple of hours of streaming, and anything dropped is
# listed in the report rather than quietly left out.
FWLOG_BUNDLE_ARCHIVES = 3
# Core dumps are ~2 KB each, so the only reason to bound this is a board
# that has been crash-looping for a week.
CD_BUNDLE_MAX = 20

_bundle_lock = threading.Lock()


def support_bundle(note=""):
    """One-click field diagnostics: build support_<host>_<ts>.tar.gz holding
    report.html (self-contained viewer) plus every raw artifact, and return
    (filename, path, tmpdir) - the caller streams the tar and removes tmpdir.

    Load discipline, because this runs on a box that may already be in
    trouble: one bundle at a time (caller holds _bundle_lock), external
    commands niced + idle-IO, per-item timeouts, per-file size caps, gzip at
    a modest level, and any failing item is recorded in the manifest as
    skipped instead of failing the bundle."""
    host = socket.gethostname()
    ts = time.strftime("%Y%m%d-%H%M%S")
    root = "support_%s_%s" % (re.sub(r"[^A-Za-z0-9._-]", "-", host), ts)
    tmpdir = tempfile.mkdtemp(prefix="halow-support-")
    base = os.path.join(tmpdir, root)
    os.makedirs(base)

    data = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host": host, "note": note, "steps": []}

    def guard(name, fn):
        t0 = time.time()
        try:
            fn()
            data["steps"].append({"step": name,
                                  "ms": int((time.time() - t0) * 1000)})
        except Exception as exc:
            data["steps"].append({"step": name,
                                  "ms": int((time.time() - t0) * 1000),
                                  "skipped": "%s: %s" % (type(exc).__name__,
                                                         exc)})

    def add_text(arc, text):
        p = os.path.join(base, arc)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", errors="replace") as f:
            f.write(text or "(no output)\n")

    def add_file(arc, src, cap=4 << 20):
        """Copy src, keeping the TAIL when it exceeds cap - the newest lines
        are the ones an incident report needs."""
        size = os.path.getsize(src)
        p = os.path.join(base, arc)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(src, "rb") as i, open(p, "wb") as o:
            if size > cap:
                o.write(b"[truncated: original %d bytes, last %d kept]\n"
                        % (size, cap))
                i.seek(size - cap)
                i.readline()
            shutil.copyfileobj(i, o, 1 << 16)

    def tail_lines(text, n):
        return "\n".join((text or "").splitlines()[-n:])

    def err_lines(text, n=60):
        pat = re.compile(r"error|fail|warn|panic|oops|wedge|timeout|denied",
                         re.I)
        return "\n".join([l for l in (text or "").splitlines()
                           if pat.search(l)][-n:])

    with _lock:
        mode = get_config().get("operation_mode", "")
    data["mode"] = mode

    # -- identity / status ---------------------------------------------------
    def c_status():
        with _lock:                 # consistent snapshot vs a concurrent apply
            st = _sanitize(full_status())
        data["status"] = st
        add_text("status.json", json.dumps(st, indent=1))
        idn = _sanitize(identity_status())
        # the package markers next to VERSION-SDK: which vendor protocol the
        # image speaks, and the AP<->host ABI stamp install.sh verifies
        for marker in ("MCP_VENDOR", "MCP_ABI"):
            try:
                idn[marker.lower()] = open(os.path.join(
                    idn.get("pkg_dir", ""), marker)).read().strip()
            except OSError:
                pass
        data["identity"] = idn
        try:
            add_text("halow.conf", _redact(open(SERVICE_CONF).read()))
        except OSError:
            pass
        add_text("config.json",
                 json.dumps(_sanitize(dict(get_config())), indent=1))
    guard("status", c_status)

    # -- firmware (cli_app) --------------------------------------------------
    def c_cli():
        cli = cli_app_path()
        rows = []
        # a relay holds its second role on the other vif, and that half of
        # its state is missing from a bundle that only ever asks vif 0
        cmds = list(BUNDLE_CLI)
        for vif in [v for v in sorted(halow_vifs()) if v]:
            cmds += [["show", "config", str(vif)],
                     ["show", "sta", str(vif), "all"], ["show", "ap", str(vif)]]
        if os.path.isfile(cli):
            for args in cmds:
                nm = " ".join(args)
                # not run(): a command that exits non-zero (e.g. a mode where
                # the query does not apply prints FAIL and exits 1) still
                # produced output worth keeping
                try:
                    # Bytes, not universal_newlines: some replies carry raw
                    # flash contents (show sysconfig's user_factory blob is
                    # not text at all), and strict decoding turned a command
                    # that worked perfectly into "no output" in the report.
                    r = subprocess.run([cli] + args, capture_output=True,
                                       timeout=CLI_TIMEOUT)
                    out = (r.stdout or b"").decode("utf-8", "replace") + \
                          (r.stderr or b"").decode("utf-8", "replace")
                    ok = r.returncode == 0 and bool(out.strip())
                except Exception as exc:
                    out, ok = "(%s)" % exc, False
                add_text("cli/%s.txt" % nm.replace(" ", "_"), out)
                rows.append({"cmd": "cli_app " + nm, "ok": ok,
                             "text": (out.strip() or "(no output)")[:6000]})
                time.sleep(0.05)   # a breath between netlink round-trips
        data["cli"] = rows
    guard("cli_app", c_cli)

    # -- host device ----------------------------------------------------------
    def c_host():
        h = {}
        try:
            h["model"] = open("/proc/device-tree/model").read().strip("\x00 ")
        except OSError:
            h["model"] = ""
        h["kernel"] = (run(["uname", "-a"], timeout=5) or "").strip()
        try:
            osr = dict(l.split("=", 1) for l in
                       open("/etc/os-release").read().splitlines() if "=" in l)
            h["os"] = osr.get("PRETTY_NAME", "").strip('"')
        except OSError:
            h["os"] = ""
        try:
            cpu = open("/proc/cpuinfo").read()
            m = re.search(r"(?im)^(model name|Hardware)\s*:\s*(.+)$", cpu)
            h["cpu"] = "%s x%d" % (m.group(2).strip() if m else "?",
                                   cpu.count("processor\t"))
        except OSError:
            pass
        try:
            h["cmdline"] = open("/proc/cmdline").read().strip()
        except OSError:
            pass
        h["sys"] = system_status()      # load/uptime/mem/disk/temp
        for tool, arc in ((["free", "-m"], "host/free.txt"),
                          (["df", "-h"], "host/df.txt"),
                          (["ip", "-s", "link"], "host/ip_link.txt"),
                          (["ip", "addr"], "host/ip_addr.txt")):
            add_text(arc, _run_nice(tool, timeout=10))
        ps = _run_nice(["ps", "aux", "--sort=-%cpu"], timeout=10) or ""
        add_text("host/ps.txt", ps)
        h["top"] = "\n".join(ps.splitlines()[:11])
        if shutil.which("vcgencmd"):    # Pi: undervoltage/throttle history
            h["throttled"] = (run(["vcgencmd", "get_throttled"], timeout=5)
                              or "").strip()
        dmesg = _run_nice(["dmesg", "-T"], timeout=20) or \
            _run_nice(["dmesg"], timeout=20)
        add_text("host/dmesg.txt", dmesg)
        h["dmesg_flags"] = err_lines(dmesg, 80)
        data["hostinfo"] = h
    guard("host", c_host)

    # -- host driver -----------------------------------------------------------
    def c_driver():
        d = {}
        lsmod = run(["lsmod"], timeout=10) or ""
        add_text("driver/lsmod.txt", lsmod)
        mods = [l.split()[0] for l in lsmod.splitlines()
                if l.startswith("nrc")]
        d["modules"] = mods
        for m in mods:
            add_text("driver/modinfo_%s.txt" % m,
                     run(["modinfo", m], timeout=10))
        add_text("driver/iw_dev.txt", run(["iw", "dev"], timeout=10))
        dbg = "/sys/kernel/debug/nrc"
        rows = []
        if os.path.isdir(dbg):
            for n in sorted(os.listdir(dbg)):
                src = os.path.join(dbg, n)
                try:
                    add_file("driver/mcp_%s.txt" % n, src, cap=2 << 20)
                    rows.append(n)
                except OSError:
                    pass
        d["debugfs"] = rows
        data["driver"] = d
    guard("driver", c_driver)

    # -- mode-dependent: hostapd / wpa_supplicant ------------------------------
    # ap + unified-ap run hostapd; sta + mesh run wpa_supplicant; relay runs
    # both halves; sniffer runs neither.
    def c_modeconf():
        want_ap = mode in ("unified-ap", "ap", "relay")
        want_sta = mode in ("sta", "mesh", "relay")
        m = {"hostapd": want_ap, "wpa_supplicant": want_sta}
        iface = halow_iface_from_net_state()
        if want_ap:
            m["hostapd_status"] = run(["hostapd_cli", "-p", HOSTAPD_CTRL,
                                       "-i", iface, "status"], timeout=5)
            for i, cp in enumerate(_proc_conf_paths("hostapd")):
                add_text("modeconf/hostapd_%d.conf" % i,
                         _redact(open(cp).read()))
        if want_sta:
            m["wpa_status"] = run(["wpa_cli", "-i", iface, "status"],
                                  timeout=5)
            for i, cp in enumerate(_proc_conf_paths("wpa_supplicant")):
                add_text("modeconf/wpa_supplicant_%d.conf" % i,
                         _redact(open(cp).read()))
        data["modeconf"] = m
    guard("modeconf", c_modeconf)

    # -- traffic: the sampler's own ring (10 min at 1 s), no extra sampling ----
    def c_traffic():
        data["traffic"] = {"interval": TRAFFIC_INTERVAL,
                           "rows": list(_traffic["buf"])}
    guard("traffic", c_traffic)

    # -- CCA scanner: channel occupancy seen by the USB scanner board, if one
    #    is attached - the last hour as the PC tool's CSV plus a 15-min summary
    def c_cca():
        if _cca is None:
            return
        snap = _cca.snapshot()
        if snap["state"] != "attached" and not snap["seq"]:
            return
        f, bw = _cca_ap_channel()
        summary = {"state": snap["state"], "port": snap["port"],
                   "country": snap["country"], "running": snap["running"],
                   "dwell_ms": snap["dwell"], "rows": snap["seq"],
                   "ap_channel_idx": _cca.channel_for(f, bw) if f else None,
                   "stats_15min": _cca.window_stats(900)}
        data["cca"] = summary
        add_text("cca/summary.json", json.dumps(summary, indent=1))
        add_text("cca/cca_log_1h.csv", _cca.csv(3600))
    guard("cca", c_cca)

    # -- network map snapshot: the DATA, re-rendered by report.html ------------
    def c_map():
        stas = station_list(ap_iface())
        data["map"] = {"stations": stas}
    guard("map", c_map)

    # -- journals + service snapshot -------------------------------------------
    def c_journal():
        j = {}
        for unit in LOG_UNITS:
            if unit == "halow.target":
                continue     # a target has no process, hence no journal
            out = _run_nice(["journalctl", "-u", unit + ".service",
                             "-n", "1000", "--no-pager",
                             "--output", "short-iso"], timeout=20)
            out = _redact(out or "")
            add_text("journal/%s.log" % unit, out)
            j[unit] = {"errors": err_lines(out, 40),
                       "tail": tail_lines(out, 25)}
        sysw = _run_nice(["journalctl", "-b", "-p", "warning", "-n", "300",
                          "--no-pager", "--output", "short-iso"], timeout=20)
        add_text("journal/system_warnings.log", _redact(sysw))
        j["system_warnings"] = {"errors": "", "tail": tail_lines(sysw, 25)}
        units = _run_nice(["systemctl", "--no-pager", "--all",
                           "list-units", "halow*"], timeout=10)
        add_text("journal/systemctl_units.txt", units)
        data["units_snapshot"] = units or ""
        data["journal"] = j
    guard("journal", c_journal)

    # -- firmware log files -----------------------------------------------------
    # On a board with no UART header the firmware console exists only here,
    # so a bug report that leaves it out is missing the one log that says
    # what the firmware was doing. The files go in raw, and so does the
    # reading of them the GUI would have offered: crash sites and call
    # stacks, resolved here while the symbol table is at hand (it ships only
    # with a debug package, so nobody can redo this later on a workstation).
    def c_fwlog():
        info = {"files": [], "omitted": [], "analysis": None, "state": None}
        try:
            info["state"] = _sanitize(fwlog_state())
        except Exception as exc:
            info["state"] = {"error": "%s: %s" % (type(exc).__name__, exc)}

        listing = fwlog_files()
        info["dir"] = listing.get("dir")
        info["total_bytes"] = listing.get("total_bytes")
        kept_archives = 0
        for f in listing.get("files", []):
            src = _fwlog_locate(f["name"])
            if not src or not os.path.isfile(src):
                continue
            if f["current"]:
                cap = 4 << 20
            elif kept_archives < FWLOG_BUNDLE_ARCHIVES:
                cap, kept_archives = 2 << 20, kept_archives + 1
            else:
                # Say what was left behind rather than letting the bundle
                # look complete. The board still has these.
                info["omitted"].append({"file": f["name"], "bytes": f["size"]})
                continue
            try:
                add_file("fwlog/%s" % f["name"], src, cap=cap)
                info["files"].append({"file": f["name"], "bytes": f["size"],
                                      "current": f["current"],
                                      "truncated": f["size"] > cap})
            except OSError as e:
                info["omitted"].append({"file": f["name"], "error": str(e)})

        # Analyse the tail of the current log: that is where a crash that is
        # still being investigated will be.
        cur = fwlog_current_path()
        text, window = "", 512 << 10
        try:
            size = os.path.getsize(cur)
            with open(cur, "rb") as fh:
                fh.seek(max(0, size - window))
                text = fh.read().decode("utf-8", "replace")
            # Say when the reading below covers only part of the file, so a
            # "no crash found" is not mistaken for "no crash happened".
            info["analysed_bytes"] = len(text)
            info["analysed_all"] = size <= window
        except OSError:
            pass
        if text:
            info["analysis"] = fwlog_analyse(text)
            add_text("fwlog/analysis.json",
                     json.dumps(info["analysis"], indent=1))
        data["fwlog"] = info
    guard("fwlog", c_fwlog)

    # -- firmware core dumps ----------------------------------------------------
    # The dumps are small (a couple of KB each) and are the record of a
    # crash, so they go in whole. Each is also decoded here: a .bin is
    # useless to the person receiving the bundle unless they have this
    # firmware's symbol table, and they do not.
    def c_coredump():
        listing = coredump_list()
        info = {"dir": listing.get("dir"), "dumps": [], "omitted": []}
        for d in listing.get("dumps", [])[:CD_BUNDLE_MAX]:
            blob = coredump_read(d["name"])
            if blob is None:
                info["omitted"].append({"file": d["name"],
                                        "error": "could not be read"})
                continue
            try:
                add_file("coredump/%s" % d["name"],
                         os.path.join(_coredump_locate(d["name"]), d["name"]))
            except OSError as e:
                info["omitted"].append({"file": d["name"], "error": str(e)})
            entry = {"file": d["name"], "bytes": d["size"],
                     "mtime": d["mtime"]}
            try:
                parsed = coredump_parse(blob)
                add_text("coredump/%s.json" % d["name"],
                         json.dumps(parsed, indent=1))
                entry.update({
                    "ok": parsed.get("ok"), "error": parsed.get("error"),
                    "reason": parsed.get("reason"),
                    "version": parsed.get("version"),
                    "regs": parsed.get("regs"),
                    "symbols": parsed.get("symbols"),
                    "fault": parsed.get("fault"),
                    "meta": parsed.get("meta"),
                    "notes": parsed.get("notes"),
                    "diagnosis": parsed.get("diagnosis"),
                    "complete": parsed.get("complete"),
                })
            except Exception as exc:
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            info["dumps"].append(entry)
        extra = len(listing.get("dumps", [])) - CD_BUNDLE_MAX
        if extra > 0:
            info["omitted"].append({"file": "(%d older dumps)" % extra,
                                    "error": "not collected"})
        data["coredumps"] = info
    guard("coredump", c_coredump)


    # -- file inventory, report, manifest ---------------------------------------
    files = []
    for dirpath, _dirs, names in os.walk(base):
        for n in names:
            fp = os.path.join(dirpath, n)
            rel = os.path.relpath(fp, base)
            h = hashlib.sha256()
            with open(fp, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
            files.append({"file": rel.replace(os.sep, "/"),
                          "bytes": os.path.getsize(fp),
                          "sha256": h.hexdigest()})
    files.sort(key=lambda e: e["file"])
    data["files"] = files

    def c_report():
        tpl = open(REPORT_TEMPLATE, encoding="utf-8").read()
        blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        add_text("report.html", tpl.replace("__BUNDLE_DATA__", blob))
    guard("report", c_report)
    add_text("manifest.json", json.dumps(
        {"bundle": root, "files": files, "steps": data["steps"]}, indent=1))

    out_path = os.path.join(tmpdir, root + ".tar.gz")
    # modest compression: logs still shrink ~10x, at a fraction of the CPU
    with tarfile.open(out_path, "w:gz", compresslevel=4) as t:
        t.add(base, arcname=root)
    return root + ".tar.gz", out_path, tmpdir


# ---- disk cleanup -----------------------------------------------------------
# Reclaimable space an operator can free from the GUI when an install fails
# on a full disk. Every item is platform-neutral (Pi and Rockchip alike) and
# loses nothing the box needs to run: caches, rotated journals, dangling
# docker layers, install backups, old merged-log archives.
_cleanup_lock = threading.Lock()


def _disk_free_bytes():
    st = os.statvfs("/")
    return st.f_bavail * st.f_frsize


def _du_bytes(paths):
    total = 0
    for q in paths:
        out = run(["du", "-sb", q], timeout=60)
        if out:
            try:
                total += int(out.split()[0])
            except (ValueError, IndexError):
                pass
    return total


def _backup_dirs():
    """install.sh keeps the previous install as ~/nrc_pkg_backup_<ts>."""
    hits = []
    for home in glob.glob("/home/*") + ["/root"]:
        hits += sorted(glob.glob(os.path.join(home, "nrc_pkg_backup_*")))
    return hits




def _docker_df():
    """{'build_cache': text, 'images': text} reclaimable columns, or {}."""
    if not shutil.which("docker"):
        return {}
    out = run(["docker", "system", "df"], timeout=30) or ""
    res = {}
    for line in out.splitlines():
        low = line.lower()
        cols = line.split()
        # RECLAIMABLE prints as "1.2GB (80%)" (two tokens) or a bare "0B"
        if low.startswith("build cache") and len(cols) >= 2:
            res["build_cache"] = cols[-2] if cols[-1].endswith("%)") else cols[-1]
        elif low.startswith("images") and len(cols) >= 2:
            res["images"] = cols[-2] if cols[-1].endswith("%)") else cols[-1]
    return res


def disk_cleanup_catalog():
    """What can be freed, with best-effort size estimates. The POST measures
    the real gain per item from the filesystem, so a rough estimate here is
    fine - it only orders the operator's choices."""
    ddf = _docker_df()
    backups = _backup_dirs()
    items = [
        {"id": "apt_cache", "label": "APT package cache",
         "desc": "downloaded .deb files - re-fetched on demand",
         "est_bytes": _du_bytes(["/var/cache/apt/archives"])},
        {"id": "journal", "label": "systemd journal (keep last 100 MB)",
         "desc": "old boot logs; the last 100 MB stays",
         "est": (lambda o: o.split("take up")[-1].split("in the")[0].strip()
                 if o and "take up" in o else "")(
                     run(["journalctl", "--disk-usage"], timeout=15))},
        {"id": "docker_builder", "label": "Docker build cache",
         "desc": "layers left over from image builds",
         "est": ddf.get("build_cache", ""),
         "unavailable": not ddf},
        {"id": "docker_dangling", "label": "Dangling docker images",
         "desc": "untagged leftovers of replaced images (reclaimable shown "
                 "for ALL unused images; only dangling ones are removed)",
         "est": ddf.get("images", ""),
         "unavailable": not ddf},
        {"id": "backups", "label": "Old install backups",
         "desc": "%d x nrc_pkg_backup_* from previous installs" % len(backups)
                 if backups else "none found",
         "est_bytes": _du_bytes(backups) if backups else 0,
         "unavailable": not backups},
        {"id": "tmp_bundles", "label": "Leftover support-bundle temp files",
         "desc": "aborted bundle collections under /tmp",
         "est_bytes": _du_bytes(glob.glob("/tmp/halow-support-*")),
         "unavailable": not glob.glob("/tmp/halow-support-*")},
    ]
    return {"disk_free_bytes": _disk_free_bytes(), "items": items}


def disk_cleanup_run(ids):
    """Run the selected items sequentially, niced, measuring each item's real
    gain as the filesystem free-space delta."""
    actions = {
        "apt_cache": lambda: _run_nice(["apt-get", "clean"], timeout=60),
        "journal": lambda: _run_nice(["journalctl", "--vacuum-size=100M"],
                                     timeout=120),
        "docker_builder": lambda: _run_nice(
            ["docker", "builder", "prune", "-af"], timeout=300),
        "docker_dangling": lambda: _run_nice(
            ["docker", "image", "prune", "-f"], timeout=300),
        "backups": lambda: [shutil.rmtree(d, ignore_errors=True)
                            for d in _backup_dirs()],
        "tmp_bundles": lambda: [shutil.rmtree(d, ignore_errors=True)
                                for d in glob.glob("/tmp/halow-support-*")],
    }
    results = []
    for i in ids:
        if i not in actions:
            results.append({"id": i, "ok": False, "error": "unknown item"})
            continue
        before = _disk_free_bytes()
        print("[cleanup] running %s" % i, flush=True)   # audit trail
        try:
            actions[i]()
            ok, err = True, ""
        except Exception as exc:
            ok, err = False, str(exc)
        results.append({"id": i, "ok": ok, "error": err,
                        "freed_bytes": max(0, _disk_free_bytes() - before)})
    return {"results": results, "disk_free_bytes": _disk_free_bytes()}


_ping_lock = threading.Lock()


def ping_station(ip):
    """Ping one address this box already knows about: an associated station,
    a device the bridge has seen, the uplink gateway, or (on a station) the
    AP. Restricting to that set keeps the browser from turning the box into
    a general network scanner while still covering every ping button the
    GUI offers."""
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip or ""):
        return 400, {"error": "not an IPv4 address"}
    known = {s.get("ip") for s in station_list(ap_iface())}
    bc = bridge_clients(False)
    for c in bc.get("clients", []):
        known.update(c.get("ips", []))
    known.add(bc.get("gateway_ip"))
    if get_config().get("operation_mode") in ("sta", "relay") \
            or live_mode(halow_iface_from_net_state()) == "sta":
        st = sta_status()
        # both of them: the page pings ap_ip when it has one and the leg's
        # gateway when it does not
        known.add(st.get("ap_ip"))
        known.add(st.get("ap_gw"))
    known.discard(None)
    known.discard("")
    if ip not in known:
        return 403, {"error": "%s is not one of this board's stations, "
                              "bridged devices or its gateway - the button "
                              "only reaches what the bridge knows" % ip}
    if not _ping_lock.acquire(blocking=False):
        return 409, {"error": "another ping is running"}
    try:
        # Every address this endpoint allows lives on the bridge side, so
        # send the echo through br0 explicitly. On a lab board with a second
        # wired NIC into the same subnet the kernel would otherwise route it
        # there - 0.7 ms to the "AP over HaLow" - and the button would prove
        # nothing about the radio link it exists to check.
        cmd = ["ping", "-c", "3", "-W", "1", "-i", "0.3"]
        via = "the routing table"
        if os.path.isdir("/sys/class/net/br0"):
            cmd += ["-I", "br0"]
            via = "br0"
        out = run(cmd + [ip], timeout=8) or ""
    finally:
        _ping_lock.release()
    m = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", out)
    ok = bool(m) and int(m.group(2)) > 0
    print("[ping] %s ok=%s" % (ip, ok), flush=True)
    return 200, {"ip": ip, "ok": ok, "via": via,
                 "sent": int(m.group(1)) if m else 0,
                 "recv": int(m.group(2)) if m else 0,
                 "avg_ms": float(rtt.group(2)) if rtt else None,
                 "out": out[-800:]}


def plan_key(country):
    """Channel plan a country belongs to. The table (like the conf files) is
    keyed by plan, so every EU member country resolves to the EU plan - the
    same mapping start_modular.py applies."""
    try:
        return _ap_env().table_key(country)
    except Exception:
        return country


def s1g_channels(country):
    """Channel table for a country."""
    return s1g_table().get(plan_key(country), {})


def channel_options_for(tbl):
    bw_name = {0: "1", 1: "2", 2: "4"}
    return [{"value": ch,
             "label": "%d  ·  %.1f MHz / %s MHz BW"
                      % (ch, row[0] / 10.0, bw_name.get(row[1], "?"))}
            for ch, row in sorted(tbl.items())]


def channel_options(country):
    return channel_options_for(s1g_channels(country))

# Re-entrant on purpose: a request handler holds this while it builds the
# status, and something it calls - the relay guard writing config.json, say -
# legitimately takes it again. With a plain Lock that thread deadlocked and
# the whole GUI stopped answering, because every other request waits here too.
_lock = threading.RLock()
_sessions = {}          # token -> expiry
# Kept across restarts: every deploy or unit restart used to log everyone out,
# and the operator had to type the password again to look at one page. /run is
# tmpfs, so a reboot still starts with nobody logged in.
SESSION_FILE = "/run/halow/webd-sessions.json"


def _sessions_load():
    try:
        with open(SESSION_FILE) as f:
            now = time.time()
            for tok, exp in (json.load(f) or {}).items():
                if float(exp) > now:
                    _sessions[str(tok)] = float(exp)
    except Exception:
        pass


def _sessions_save():
    now = time.time()
    for tok in [t for t, e in _sessions.items() if e <= now]:
        _sessions.pop(tok, None)
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_sessions, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSION_FILE)
    except OSError:
        pass
_apply_state = {"in_progress": False, "last_action": "", "last_result": "", "ts": 0}
SESSION_TTL = 30 * 60


# ---------------------------------------------------------------- helpers
def run(cmd, timeout=10):
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                      timeout=timeout, universal_newlines=True)
        return out
    except Exception:
        return None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_kv_file(path):
    kv = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
    except OSError:
        pass
    return kv


def write_service_conf(updates):
    """Update keys in /etc/halow/halow.conf preserving comments.

    Returns the keys whose value actually changed."""
    try:
        with open(SERVICE_CONF) as f:
            lines = f.readlines()
    except OSError:
        lines = []
    touched = []
    for key, val in updates.items():
        pat = re.compile(r"^%s=" % re.escape(key))
        new_line = "%s=%s\n" % (key, val)
        for i, line in enumerate(lines):
            if pat.match(line):
                if lines[i] != new_line:
                    lines[i] = new_line
                    touched.append(key)
                break
        else:
            lines.append(new_line)
            touched.append(key)
    if touched:
        with open(SERVICE_CONF, "w") as f:
            f.writelines(lines)
    return touched


def derived_service_conf(cfg):
    """Every halow.conf key that is derived from the GUI config.

    config.json is the source of truth and halow.conf is its projection for
    the services, so this returns the *whole* projection rather than a diff:
    writing only the keys a save touched lets the two drift apart for good
    once anything edits halow.conf out of band (a hand fix, a restored
    backup, an interrupted apply). The GUI then shows one channel while the
    radio runs another. Keys the GUI does not own (NRC_SCRIPT_DIR, HALOW_DIR,
    BUILD_TYPE, GATEWAY_BEACON_INT, ...) are simply absent here and survive
    untouched."""
    out = {
        "COUNTRY": cfg.get("country", "US"),
        "SECURITY": SECURITY_ARG.get(cfg.get("security", "open"), "0"),
        # "auto" is the GUI's way of saying "the channel manager picks", so
        # the scripts must see no channel at all: start.py reads CHANNEL as a
        # number and treats any channel as manual operation.
        "CHANNEL": "" if str(cfg.get("channel") or "").strip().lower() == "auto"
                   else str(cfg.get("channel") or ""),
        "BRIDGE": "1" if cfg.get("bridge") == "on" else "0",
        # The modular stack exists for the Unified AP (on the
        # chip); every other mode - plain ap, sta, sniffer, relay, mesh -
        # runs the stock start.py flow on the monolithic nrc.ko.
        "DRIVER_STACK":
            "modular" if cfg.get("operation_mode") == "unified-ap" else "nrc",
    }
    # schema entries with a conf_key persist into halow.conf (value optionally
    # translated via conf_map) and reach the start scripts as key=value args
    for key, meta in get_schema().items():
        ck = meta.get("conf_key")
        if ck:
            val = str(cfg.get(key, ""))
            out[ck] = str(meta.get("conf_map", {}).get(val, val))
    return out


def reconcile_service_conf():
    """Re-project config.json onto halow.conf and the scripts at startup.

    A board that drifted while the daemon was down heals without waiting for
    a save. The scripts are included because a package update replaces
    start.py with the shipped header: without this the saved settings would
    live on in the GUI while a manual run used the package defaults - and
    start.py, seeing the newer script, would then absorb those defaults back
    into the configuration.
    """
    try:
        cfg = get_config()
        touched = write_service_conf(derived_service_conf(cfg))
        note = apply_to_scripts(cfg)
    except Exception as exc:                       # never block startup
        print("[halow-webd] service conf reconcile skipped: %s" % exc,
              flush=True)
        return
    if touched:
        print("[halow-webd] halow.conf re-synced from config.json: %s"
              % ", ".join(touched), flush=True)
    if note:
        print("[halow-webd] scripts re-synced from config.json%s" % note,
              flush=True)


def pw_hash(pw):
    return hashlib.sha256(("halow-ap:" + pw).encode()).hexdigest()


def needs_pw_change(cfg):
    """Force a password change while the factory default is still in use."""
    # keyed on the stored hash alone: a "changed" flag could be set with the
    # default still in place, and then the gate never came back.
    return cfg.get("admin_password_hash") == pw_hash(DEFAULT_ADMIN_PW)


def unit_exists(unit):
    return os.path.exists("/etc/systemd/system/" + unit)


def unit_active(unit):
    # is-active exits non-zero for inactive units; capture stdout regardless
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, universal_newlines=True,
                           timeout=5)
        return (r.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- schema/config
DEFAULT_SCHEMA = {}   # shipped as schema.json; empty fallback keeps daemon alive


def get_schema():
    return load_json(SCHEMA_FILE, DEFAULT_SCHEMA)


def unified_ap_available():
    """Whether this box can run the Unified AP at all.

    That mode needs its own firmware image and the modular driver stack.
    A package built without them carries neither, and copy.sh refuses at
    start time rather than bringing the mode up half-configured - but by
    then the operator has already saved the mode and restarted the stack.
    Leave the choice out of the list instead.

    Everything the mode needs ships together, so the firmware image alone is
    a sufficient test; the modular driver is checked too because a build that
    failed on this kernel leaves the image without a stack to load it.
    """
    svc = read_kv_file(SERVICE_CONF)
    pkg = os.path.dirname(svc.get("NRC_SCRIPT_DIR", "/nonexistent").rstrip("/"))
    return os.path.isfile(os.path.join(pkg, "sw/firmware/nrc7394_cspi_mcp.bin")) \
        and os.path.isdir(os.path.join(pkg, "sw/driver/nrc_modular"))


def get_config():
    schema = get_schema()
    cfg = load_json(CONFIG_FILE, {})
    changed = False
    for key, meta in schema.items():
        if key not in cfg:
            cfg[key] = meta.get("default", "")
            changed = True
    # a blank beacon_int used to mean "leave the conf alone", which hid what
    # the AP really ran at; a board upgraded with one comes onto the default
    if "beacon_int" in schema and str(cfg.get("beacon_int", "")).strip() == "":
        cfg["beacon_int"] = schema["beacon_int"].get("default", 100)
        changed = True
    for key, meta in schema.items():
        if meta.get("values_from") != "eth_ports":
            continue
        ports = wired_ports()
        if ports and cfg.get(key) not in ports:
            cfg[key] = meta.get("default") if meta.get("default") in ports \
                       else ports[0]
            changed = True
    if "admin_password_hash" not in cfg:
        cfg["admin_password_hash"] = pw_hash(DEFAULT_ADMIN_PW)
        changed = True
    if changed:
        save_json(CONFIG_FILE, cfg)
    return cfg


def validate(key, val, meta):
    t = meta.get("type", "string")
    # the wired port list belongs to the board, not to the schema file, and
    # the choice is the operator's: a board with two ports has no right guess
    if meta.get("values_from") == "eth_ports":
        name = str(val).strip()
        if name not in wired_ports():
            return None, ("pick one of this board's wired ports (%s)"
                          % ", ".join(wired_ports() or ["none found"]))
        return name, None
    # "auto" on the channel means "let the channel manager pick", which is a
    # choice, not a number - see the coupling in the settings handler
    if key == "channel" and str(val).strip().lower() == "auto":
        return "auto", None
    # "auto" on the TX power means the board data file decides; anything
    # else is a number of dBm
    if key in ("txpower", "txpower_sta") and str(val).strip().lower() == "auto":
        return "auto", None
    if t in ("int",):
        if val in ("", None):
            # empty int = "use the conf/script default" (e.g. the channel
            # dropdown's "(conf default)" option); persists as an empty
            # conf value which start-wlan.sh skips
            if key == "beacon_int":
                # every shipped conf carries 100, so blank names no other
                # value - it would only hide what the AP really runs at
                return None, "enter a beacon interval in TU"
            return "", None
        try:
            val = int(val)
        except (TypeError, ValueError):
            return None, "not an integer"
        rng = meta.get("range")
        if rng and not (rng[0] <= val <= rng[1]):
            return None, "out of range %s" % rng
    elif t == "enum":
        if val not in meta.get("values", []):
            return None, "invalid value"
    else:  # string / password
        val = str(val)
        v = meta.get("validate", {})
        if "maxlen" in v and len(val) > v["maxlen"]:
            return None, "too long (max %d)" % v["maxlen"]
        if "minlen" in v and len(val) < v["minlen"]:
            return None, "too short (min %d)" % v["minlen"]
        # schema regexes are written anchored; fullmatch keeps a sloppy one
        # from passing on a prefix
        if v.get("regex") and not re.fullmatch(v["regex"], val):
            return None, v.get("regex_hint", "invalid format")
    return val, None


# ---------------------------------------------------------------- apply engine
APPLY_RANK = {"none": 0, "live": 1, "hostapd-restart": 2,
              "csa-or-restart": 3, "full-restart": 3,
              "eth-config": 0,   # handled out-of-band in do_apply
              "autostart": 0}    # same - only flips the boot-time link


def csa_count(cfg):
    """CSA countdown in beacon intervals.

    Plain 11ah AP: 10 beacons (standard quick switch). A longer countdown
    gives sleeping stations more observation windows: count = rounds x
    time-group count (e.g. 5 x 16 = 80, ~25 s; any uint8 count is
    standard-valid for 11ah STAs - they simply follow the advertised
    value). The broadcast channel-switch data wakeup delivers one chance
    per round to every sleeping tag; misses fall back to the scan path."""
    if unit_active("halow-gateway.service") != "active":
        return 10
    rounds = int(cfg.get("mcp_csa_rounds", 5) or 5)
    # The optional extension module can supply the exact group count; the
    # shipped default of 16 stands otherwise. A tuned group count then only
    # makes the countdown conservative or short, and tags that miss the
    # switch recover through their scan path.
    return min(rounds * 16, 255)


def try_channel_switch(cfg, count=None):
    """Live channel change via 802.11ah CSA (ECSA in the S1G beacon).
    Verified HW path: hostapd chan_switch -> FW counts down per beacon ->
    retune (incl. 1/2/4 MHz bandwidth changes). Returns (ok, message)."""
    if count is None:
        count = csa_count(cfg)
    country = cfg.get("country", "US")
    ch = cfg.get("channel")
    row = s1g_channels(country).get(ch)
    if not row:
        return False, "channel %s not in %s table" % (ch, country)
    s1g_freq, _bw, nons1g = row

    env = read_kv_file(AP_ENV_FILE)
    iface = env.get("HALOW_IFACE", "wlan0")
    if hostapd_status(iface).get("state") != "ENABLED":
        return False, "AP not ENABLED"

    cli = None
    for c in ("/usr/sbin/hostapd_cli", "/sbin/hostapd_cli", "hostapd_cli"):
        if not c.startswith("/") or os.path.exists(c):
            cli = c
            break
    out = run([cli, "-p", HOSTAPD_CTRL, "-i", iface, "chan_switch",
               str(count), str(nons1g)], timeout=10)
    if out is None or "OK" not in out:
        return False, "chan_switch rejected"

    # countdown runs one CSA count per beacon (~0.31 s at 300 TU).
    # NOTE: hostapd `status` reports the target freq as soon as the CSA is
    # scheduled, so poll the FW's own view (cli_app) for real completion.
    svc = read_kv_file(SERVICE_CONF)
    cli_app = os.path.join(svc.get("NRC_SCRIPT_DIR", ""), "cli_app")
    deadline = time.time() + count * 0.5 + 15
    switched = False
    ap_vif = str(vif_of(iface))          # the interface doing the switch
    while time.time() < deadline:
        chk = run([cli_app, "show", "config", ap_vif], timeout=10) or ""
        m = re.search(r"Frequency\s*:\s*(\d+)", chk)
        if m and int(m.group(1)) == s1g_freq \
                and hostapd_status(iface).get("state") == "ENABLED":
            switched = True
            break
        time.sleep(1)
    if not switched:
        return False, "switch did not complete (fw freq != %d)" % s1g_freq

    # refresh /run/halow/ap.env with the new channel
    subprocess.run(["sh", "-c",
                    "set -a; . %s; HALOW_GW_RESYNC=0 exec /usr/lib/halow/gen-ap-env.sh"
                    % SERVICE_CONF], timeout=60)

    return True, "CSA complete: ch %s (%.1f MHz, count %d)" \
                 % (ch, s1g_freq / 10.0, count)






def halow_iface_from_net_state():
    """HaLow netdev the last start actually used (STA mode has no ap.env)."""
    kv = read_kv_file(NET_STATE_FILE)
    return kv.get("NRC_HALOW_IFACE", "wlan0")


# The HaLow radio can carry more than one netdev: a relay runs an AP and a
# station side by side. Neither the names nor the roles are fixed - start.py
# falls back to wlan0_1 when another radio already holds wlan1, and RELAY_TYPE
# decides which way round the pair sits - so each interface is identified by
# asking the kernel what it is, never by its number.
HALOW_PHY = "nrc80211"
_iface_roles = {"t": 0.0, "v": [], "sig": []}
IFACE_ROLE_TTL = 5.0


def _ifindex(iface):
    """The kernel's index for a netdev, "" when it is gone."""
    try:
        with open("/sys/class/net/%s/ifindex" % iface) as f:
            return f.read().strip()
    except (OSError, IOError):
        return ""


_netdevs = {"t": 0.0, "v": []}


def halow_netdevs():
    """Every netdev on the HaLow radio, by the phy they sit on.

    Every firmware read asks this first - it is what says whether there is a
    radio to ask at all - so the sysfs walk is memoised for a second.
    """
    now = time.time()
    if now - _netdevs["t"] < 1.0:
        return list(_netdevs["v"])
    out = []
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return out
    for n in names:
        if "." in n:                 # AP-VLAN child of a 4-address station
            continue
        try:
            with open("/sys/class/net/%s/phy80211/name" % n) as f:
                if f.read().strip() == HALOW_PHY:
                    out.append(n)
        except (OSError, IOError):
            continue
    _netdevs.update({"t": now, "v": out})
    return out


def halow_iface_roles(max_age=IFACE_ROLE_TTL):
    """[(iface, role)] for the HaLow netdevs, role as the kernel reports it.

    Cached for a few seconds: this is read on every dashboard poll and each
    entry costs an `iw` call.
    """
    now = time.time()
    if now - _iface_roles["t"] < max_age and _iface_roles["v"]:
        return list(_iface_roles["v"])
    v = [(n, live_mode(n)) for n in halow_netdevs()]
    # names alone miss a driver reload that recreates wlan0/wlan1, so the
    # kernel's interface index goes into the comparison
    sig = [(n, _ifindex(n)) for n, _r in v]
    if sig != _iface_roles["sig"]:
        # The stack changed shape: a relay became a plain AP, a leg went
        # away, or the radio came back after a restart. Everything the
        # firmware told us describes the radio that was there before - the
        # vif roles send per-vif reads to the wrong leg, and a settled
        # reading is kept for 15 minutes, which is how the dashboard came
        # back showing what the previous run had.
        _vif_role.clear()
        _fw_cache.clear()
        _ap_tpc.clear()
        # probe answers name the addresses of the network that just went away
        discovery_forget()
    _iface_roles.update({"t": now, "v": v, "sig": sig})
    return list(v)


def iface_role(iface):
    """ap / sta / sniffer / mesh for one netdev, from the cached list.

    None for anything that is not on the HaLow radio - asking `iw` about it
    on every poll buys nothing.
    """
    for n, r in halow_iface_roles():
        if n == iface:
            return r
    return live_mode(iface) if iface in halow_netdevs() else None


def ap_iface():
    """The netdev serving our own BSS.

    That is the AP interface on a relay and the mesh interface on a mesh
    point. Falls back to the first HaLow netdev while no interface holds the
    role yet, so the page still has a name to show as the stack comes up.
    """
    known = halow_iface_roles()
    for n, r in known:
        if r in ("ap", "mesh"):
            return n
    return known[0][0] if known else halow_iface_from_net_state()


def station_iface():
    """The netdev holding the station link.

    Everywhere but relay that is the HaLow interface itself. A relay joins an
    upstream AP on its second interface, and asking the AP interface about an
    association returns nothing - which is what left the relay's uplink and
    its ping button off the dashboard.
    """
    known = halow_iface_roles()
    for n, r in known:
        if r == "sta":
            return n
    # not associated yet: on a relay the STA side is the netdev that is not
    # the AP's, so it can still be named before the link comes up
    ap = ap_iface()
    for n, _r in known:
        if n != ap:
            return n
    return halow_iface_from_net_state()


def apply_supplicant_restart():
    out = run(["/usr/lib/halow/apply-supplicant.sh"], timeout=60)
    if out is None:
        return "apply-supplicant.sh FAILED"
    return out.strip().splitlines()[-1] if out.strip() else "supplicant reconfigured"


def apply_live(cfg, changed_keys):
    results = []
    if "bridge_flood_guard" in changed_keys:
        v = str(cfg.get("bridge_flood_guard", "off"))
        if v == "on":
            # halow_flood_off.sh re-arms every wlan* bridge port (it reads
            # this same config key, which the caller has already saved, so
            # its kill-switch gate passes). Then re-pin the ARP-proxy
            # neighbours the guard depends on.
            out = run(["/usr/lib/halow/halow_flood_off.sh", "apply"],
                      timeout=15)
            results.append("bridge_flood_guard=on %s"
                           % ("ok" if out is not None else "FAILED"))
        else:
            # Restore plain bridge flooding and drop the ARP-proxy pins -
            # with suppression off they would go stale unnoticed.
            n = 0
            try:
                members = os.listdir("/sys/class/net/br0/brif")
            except OSError:
                members = []
            for m in members:
                if not m.startswith("wlan"):
                    continue
                base = "/sys/class/net/br0/brif/%s" % m
                for f, val in (("unicast_flood", "1"),
                               ("multicast_flood", "1"),
                               ("broadcast_flood", "1"),
                               ("neigh_suppress", "0")):
                    try:
                        with open(os.path.join(base, f), "w") as fh:
                            fh.write(val)
                    except OSError:
                        pass
                n += 1
            out = run(["ip", "-4", "neigh", "show", "nud", "permanent",
                       "dev", "br0"], timeout=5) or ""
            for line in out.splitlines():
                ip = line.split()[0] if line.split() else ""
                if ip:
                    run(["ip", "neigh", "del", ip, "dev", "br0"], timeout=5)
            _neigh_pinned.clear()
            results.append("bridge_flood_guard=off (%d ports)" % n)
    if "guard_int" in changed_keys:
        v = str(cfg.get("guard_int", "auto"))
        svc = read_kv_file(SERVICE_CONF)
        cli = os.path.join(svc.get("NRC_SCRIPT_DIR", ""), "cli_app")
        # `set gi` without a vif reaches vif 0 only, so a relay's STA side
        # kept the default until each interface was named
        done = []
        for vif in sorted(halow_vifs()):
            out = run([cli, "set", "gi", v, str(vif)], timeout=10)
            done.append("vif%d %s" % (vif, "ok" if out is not None else "FAILED"))
        results.append("guard_int=%s %s" % (v, ", ".join(done)))
    if "txpower" in changed_keys or "txpower_sta" in changed_keys:
        # NOTE: `iw phy set txpower` returns 0 but is a silent no-op on the
        # nrc driver (verified on HW via cli_app/FW-console readback), so the
        # cli_app netlink path is the real mechanism; iw is kept best-effort
        # in case the driver implements set_txpower later.
        svc = read_kv_file(SERVICE_CONF)
        cli = os.path.join(svc.get("NRC_SCRIPT_DIR", ""), "cli_app")
        mode = cfg.get("operation_mode", "ap")

        tpc_on = str(cfg.get("tpc", "off")) == "on"

        def _want(role):
            """What this vif should run at, or None while TPC owns it.

            TPC writes a station's power on every decision, so a value set
            here would only be overwritten. The AP side keeps its own number
            either way - TPC there only reports what it measures.
            """
            if tpc_on and role == "sta":
                return None
            v = cfg.get("txpower_sta") if (mode == "relay" and role == "sta") \
                else cfg.get("txpower")
            return str(v).strip().lower() or "auto"

        seen, checked, ok, capped = [], 0, True, None
        for vif, role in sorted(halow_vifs().items()):
            v = _want(role)
            if v is None:
                seen.append("vif%d=TPC" % vif)
                continue
            if v == "auto":
                # TXPWR_AUTO: the board data file's own maximum
                out = run([cli, "set", "txpwr", str(vif), "auto", "30"],
                          timeout=10)
                seen.append("vif%d=auto%s"
                            % (vif, "" if out is not None else " FAILED"))
                continue
            out = run([cli, "set", "txpwr", str(vif), "limit", v], timeout=10)
            if out is None:
                ok = False
            capped = v if capped is None else capped
            chk = run([cli, "show", "config", str(vif)], timeout=10) or ""
            m = re.search(r"Tx Power Type\s*:\s*~?(\d+)", chk)
            val = m.group(1) if m else "?"
            seen.append("vif%d=%s" % (vif, val))
            checked += 1
            if val != v:
                ok = False
        if capped is not None:
            # second limiter, and only where a fixed ceiling was asked for
            run(["iw", "phy", "nrc80211", "set", "txpower", "limit",
                 str(int(capped) * 100)], timeout=10)
        verdict = "not verified (board data or TPC owns the power)" if not checked \
            else ("ok" if ok else "MISMATCH")
        results.append("txpower: %s %s" % (" ".join(seen) or "-", verdict))
    if "channel_manager_profile" in changed_keys:
        # The firmware re-profiles in place: the candidate table, the
        # operating bandwidth and the accumulated scores all survive, and
        # only the streak counter resets. Country and bandwidth are
        # deliberately not resent - naming either makes the firmware sweep
        # again, which takes the AP off channel for the length of the sweep
        # to apply a setting that did not change.
        prof = {"conservative": "0", "normal": "1",
                "aggressive": "2"}.get(str(cfg.get("channel_manager_profile")))
        mode = {"auto": "auto", "monitor": "monitor",
                "off": "off"}.get(str(cfg.get("channel_manager", "off")), "off")
        if prof is None:
            results.append("channel_manager_profile: unknown value")
        elif mode == "off":
            # Nothing to re-profile, and sending it would start the engine.
            results.append("channel_manager_profile stored (manager is off)")
        else:
            svc = read_kv_file(SERVICE_CONF)
            cli = os.path.join(svc.get("NRC_SCRIPT_DIR", ""), "cli_app")
            out = run([cli, "set", "chmgr", mode, "profile=" + prof],
                      timeout=30) or ""
            _chmgr_param["at"] = 0.0
            results.append("channel_manager_profile=%s %s"
                           % (prof, "ok" if "ok" in out.lower() else "FAILED"))
    return "; ".join(results) or "ok"


def apply_hostapd_restart():
    # timeout covers hostapd + the gateway add-on bounce (see apply-hostapd.sh)
    out = run(["/usr/lib/halow/apply-hostapd.sh"], timeout=180)
    if out is None:
        return "apply-hostapd.sh FAILED"
    return out.strip().splitlines()[-1] if out.strip() else "hostapd restarted"


def apply_async(action, fn):
    """Run a bounded restart off the request thread.

    The settings request holds the global lock while it applies, so a
    hostapd bounce that takes tens of seconds stops the dashboard
    answering and the progress bar never appears.
    """
    def worker():
        try:
            _apply_state["last_result"] = fn()
        except Exception as e:
            _apply_state["last_result"] = "%s failed: %s" % (action, e)
        finally:
            _apply_state["in_progress"] = False
            _apply_state["ts"] = int(time.time())
    _apply_state["in_progress"] = True
    _apply_state["last_action"] = action
    threading.Thread(target=worker, daemon=True).start()


def apply_full_restart_async():
    def worker():
        try:
            subprocess.run(["systemctl", "restart", "halow.target"],
                           timeout=900)
            _apply_state["last_result"] = "restart complete"
        except Exception as e:
            _apply_state["last_result"] = "restart failed: %s" % e
        finally:
            _apply_state["in_progress"] = False
            _apply_state["ts"] = int(time.time())
    _apply_state["in_progress"] = True
    _apply_state["last_action"] = "full-restart"
    _fw_invalidate()          # the vif roles change across a restart
    threading.Thread(target=worker, daemon=True).start()


def apply_full_stop_async():
    """Take the whole stack down: stopping halow.target stops hostapd, the
    gateway and the wlan unit, whose ExecStop kills the userland daemons and
    unloads every driver module. halow-webd is not part of the target, so
    the GUI stays up to start the stack again (Restart stack)."""
    def worker():
        try:
            subprocess.run(["systemctl", "stop", "halow.target"],
                           timeout=900)
            # a manual start.py run is not systemd's, so the target was already
            # inactive and that call did nothing - stop.py owns that radio
            if stack_running():
                sdir = read_kv_file(SERVICE_CONF).get("NRC_SCRIPT_DIR", "")
                stop = os.path.join(sdir, "stop.py")
                if sdir and os.path.exists(stop):
                    print("[halow-webd] radio still up after the target stopped"
                          " - running %s" % stop, flush=True)
                    subprocess.run(["python3", stop], cwd=sdir, timeout=900)
            _apply_state["last_result"] = "stack stopped"
        except Exception as e:
            _apply_state["last_result"] = "stop failed: %s" % e
        finally:
            _apply_state["in_progress"] = False
            _apply_state["ts"] = int(time.time())
    _apply_state["in_progress"] = True
    _apply_state["last_action"] = "full-stop"
    _fw_invalidate()          # no vif after this: drop what the roles were
    threading.Thread(target=worker, daemon=True).start()


def do_apply(cfg, changed):
    """changed: {key: meta}. Executes the highest-rank action needed."""
    # Two kinds of setting are handled here rather than by a stack restart:
    # the boot-time link, which a restart would not change, and the wired
    # uplink, which lives outside the HaLow stack. Both drop out of the batch
    # and the rest proceeds normally.
    auto_keys = {k for k in changed if m_apply(changed[k]) == "autostart"}
    auto_note = ""
    if auto_keys:
        auto_note = apply_autostart(cfg)
        changed = {k: m for k, m in changed.items() if k not in auto_keys}
        if not changed:
            return "autostart", auto_note
        auto_note += "; "
    eth_keys = {k for k in changed if m_apply(changed[k]) == "eth-config"}
    eth_note = auto_note
    if eth_keys:
        eth_note = auto_note + apply_eth_config(cfg)
        changed = {k: m for k, m in changed.items() if k not in eth_keys}
        if not changed:
            return "eth-config", eth_note
        eth_note += "; "
    action, result = _do_apply_stack(cfg, changed)
    return action, eth_note + result


def m_apply(meta):
    return meta.get("apply", "none")


def stack_running():
    """True when there is a HaLow radio up to reconfigure.

    A stopped stack has no netdev on the phy, because the driver is gone.
    The systemd unit is not the test: a manual start.py run leaves it
    inactive while the radio is very much up.
    """
    return bool(halow_netdevs())


def _do_apply_stack(cfg, changed):
    # The radio never sees these (the admin password, the GUI's own
    # ports): re-projecting halow.conf and rewriting the scripts for
    # one of them edited conf files the operator did not touch.
    if changed and all(m_apply(m) == "none" for m in changed.values()):
        return "none", "saved - it applies at the next login"
    rank = max([APPLY_RANK.get(m.get("apply", "none"), 0)
                for m in changed.values()] or [0])
    # Re-project the whole config onto halow.conf, not just what this save
    # touched: a diff-only write cannot repair drift it did not cause, and a
    # halow.conf that disagrees with config.json runs the radio on settings
    # the GUI never shows. (Switching operating mode also flips DRIVER_STACK
    # here; one device-tree overlay serves both flavours, so that is a module
    # reload and nothing more. Unloading the monolithic driver can leave the
    # SPI device without a usable IRQ, which only a reboot clears - but it
    # does not always happen, so start-wlan.sh raises the reboot banner when
    # the interface actually fails to appear rather than warning up front.)
    write_service_conf(derived_service_conf(cfg))
    # SSID, credentials and channel go straight into the conf files a manual
    # run edits, so both ways of driving the radio show the same settings
    apply_to_scripts(cfg)

    # Nothing to bounce while the stack is down: the files written above
    # already carry the change, and hostapd or wpa_supplicant is not there to
    # restart. Without this the save failed, and the operator had to press
    # Apply a second time to be offered a stack restart.
    if not stack_running():
        return "settings-only", ("saved - the stack is stopped; start it "
                                 "from the dashboard to run with them")

    kinds = set(m.get("apply", "none") for m in changed.values())
    # live CSA needs a running 11ah AP - other modes take the restart path
    if kinds == {"csa-or-restart"} and str(cfg.get("channel", "")).strip() \
            and cfg.get("operation_mode", "ap") in ("unified-ap", "ap"):
        ok, msg = try_channel_switch(cfg)
        if ok:
            return "csa", msg
        apply_full_restart_async()
        return "full-restart", "CSA not possible (%s); restarting stack" % msg

    if rank >= 3:
        apply_full_restart_async()
        return "full-restart", "restarting halow.target (takes 1-3 min)"
    if rank == 2:
        # "wlan-restart" rank: dispatched by role - AP/RELAY bounce hostapd,
        # STA re-reads the supplicant conf (wpa_cli reconfigure). The role is
        # the one the radio is in, not the saved mode: a manual start.py run
        # can have a station up while the configuration still says AP, and
        # bouncing hostapd there reaches nothing - the station keeps the old
        # SSID. Relay and mesh run both halves, so those keep the saved mode.
        saved = cfg.get("operation_mode", "ap")
        live = live_mode(halow_iface_from_net_state())
        role = saved if saved in ("relay", "mesh") or not live else live
        if role == "sta":
            apply_async("supplicant-restart", apply_supplicant_restart)
            return "supplicant-restart", "restarting wpa_supplicant"
        if role == "mesh":
            # The mesh ID lives in the supplicant conf, and on a portal in
            # the AP leg's hostapd conf as well. Neither is a systemd unit
            # here - mesh bring-up is start.py's own child process tree - so
            # bouncing halow-hostapd would leave the mesh leg on the old ID.
            apply_full_restart_async()
            return "full-restart", "restarting halow.target (takes 1-3 min)"
        apply_async("hostapd-restart", apply_hostapd_restart)
        return "hostapd-restart", "restarting hostapd"
    if rank == 1:
        return "live", apply_live(cfg, changed.keys())
    return "none", "saved"


# ---------------------------------------------------------------- sniffer (M3)
# Web sniffer: tshark streams a wireshark-like packet list into a ring
# buffer for the GUI; dumpcap keeps a pcap ring on disk for download (deep
# analysis stays in desktop Wireshark - by design).
CAP_DIR = "/tmp/halow-capture"
TSHARK_FIELDS = ["frame.number", "frame.time_relative", "_ws.col.protocol",
                 "wlan.sa", "wlan.da", "radiotap.dbm_antsignal",
                 "frame.len", "_ws.col.info",
                 # S1G frames carry the signal here instead of the plain
                 # radiotap field - used as fallback
                 "wlan_radio.signal_dbm",
                 # kernel capture timestamp: identical in the live tshark
                 # stream and the dumpcap pcap ring (same capture source),
                 # so it links a live row to its frame in the ring for the
                 # click-to-inspect protocol tree
                 "frame.time_epoch",
                 # type/subtype as one 6-bit number (type << 4 | subtype).
                 # The info column carries it in prose, but only sometimes
                 # and never in a form you can sort or scan a column of.
                 "wlan.fc.type_subtype"]
_sniffer = {"tshark": None, "dumpcap": None, "buf": deque(maxlen=3000),
            "seq": 0}

# 802.11 type/subtype names, keyed by the combined value tshark reports.
# Only the ones that actually turn up on an S1G link are spelled out; the
# rest fall back to the raw number, which is still better than a blank.
WLAN_SUBTYPE = {
    0x00: "Assoc Req",   0x01: "Assoc Resp",  0x02: "Reassoc Req",
    0x03: "Reassoc Resp", 0x04: "Probe Req",  0x05: "Probe Resp",
    0x08: "Beacon",      0x09: "ATIM",        0x0A: "Disassoc",
    0x0B: "Auth",        0x0C: "Deauth",      0x0D: "Action",
    0x0E: "Action NoAck",
    0x18: "BlockAck Req", 0x19: "BlockAck",   0x1A: "PS-Poll",
    0x1B: "RTS",         0x1C: "CTS",         0x1D: "ACK",
    0x1E: "CF-End",      0x1F: "CF-End+Ack",
    0x20: "Data",        0x24: "Null",
    0x28: "QoS Data",    0x2C: "QoS Null",
    # Extension type (0b11). The S1G beacon lives here and is the frame this
    # radio sends most, but tshark's dissector calls it "Unrecognized
    # (Reserved frame)" and the info column comes back empty - so naming it
    # here is the only place the operator sees what it is.
    0x31: "S1G Beacon",
}


def wlan_subtype_name(raw):
    """Readable name for a tshark wlan.fc.type_subtype value."""
    raw = (raw or "").split(",")[0].strip()
    if not raw:
        return ""
    try:
        n = int(raw, 0)
    except ValueError:
        return raw
    return WLAN_SUBTYPE.get(n, "0x%02X" % n)


def sniffer_running():
    p = _sniffer["tshark"]
    return p is not None and p.poll() is None


def _sniffer_reader(proc):
    for line in proc.stdout:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < len(TSHARK_FIELDS):
            continue
        _sniffer["seq"] += 1
        rssi = parts[5].split(",")[0] or parts[8].split(",")[0]
        _sniffer["buf"].append({
            "seq": _sniffer["seq"], "no": parts[0], "t": parts[1][:9],
            "proto": parts[2], "sa": parts[3], "da": parts[4],
            "rssi": rssi, "len": parts[6],
            "subtype": wlan_subtype_name(parts[10]),
            "info": parts[7][:160], "epoch": parts[9]})


def sniffer_start():
    if sniffer_running():
        return True, "capture already running"
    if not shutil.which("tshark"):
        return False, "tshark not installed (start the sniffer stack once " \
                      "with internet access, or apt-get install tshark)"
    iface = halow_iface_from_net_state()
    os.makedirs(CAP_DIR, exist_ok=True)
    _sniffer["buf"].clear()
    _sniffer["seq"] = 0
    _sniffer["dumpcap"] = subprocess.Popen(
        ["dumpcap", "-i", iface, "-q", "-b", "filesize:10240",
         "-b", "files:5", "-w", os.path.join(CAP_DIR, "cap.pcapng")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = ["tshark", "-i", iface, "-l", "-n", "-T", "fields",
           "-E", "separator=\t"]
    for f in TSHARK_FIELDS:
        cmd += ["-e", f]
    _sniffer["tshark"] = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, bufsize=1)
    threading.Thread(target=_sniffer_reader,
                     args=(_sniffer["tshark"],), daemon=True).start()
    return True, "capture started on %s" % iface


def sniffer_stop():
    n = _sniffer["seq"]
    for key in ("tshark", "dumpcap"):
        p = _sniffer[key]
        if p is not None and p.poll() is None:
            p.terminate()
        _sniffer[key] = None
    return True, "capture stopped (%d packets seen)" % n


def sniffer_pcap_files():
    try:
        return sorted((os.path.join(CAP_DIR, f) for f in os.listdir(CAP_DIR)),
                      key=os.path.getmtime, reverse=True)
    except OSError:
        return []


# ------------------------------------------------------------ channel manager
# The firmware scores the channels and decides; chmgrd only carries the
# decision out, by running a hostapd channel switch. So the status comes from
# the chip, through cli_app, and chmgrd's own liveness is a separate question -
# a firmware that has decided to switch and a daemon that is not running to do
# it is exactly the failure worth seeing.
CHMGR_PROFILES = ("conservative", "normal", "aggressive")
_CHMGR_PARAM_KEYS = {"margin": "margin", "c1": "c1", "holddown": "holddown",
                     "alpha": "alpha", "threshold": "threshold"}
_chmgr_cache = {"at": 0.0, "data": None}
# tuning values, re-read rarely: only an operator command changes them
_chmgr_param = {"at": 0.0, "v": {}}
CHMGR_PARAM_TTL = 30.0
# The engine can be armed from outside this daemon - a manual start.py run -
# and chmgrd outlives the engine it served, so its presence cannot signal that.
# Re-read a cached "off" this often instead of trusting it forever.
CHMGR_OFF_TTL = 30.0
# the dashboard card tolerates a stale reading; the panel does not
CHMGR_SUMMARY_TTL = 15.0
# A build with no engine answers nothing useful; stop asking for a while.
CHMGR_UNSUPPORTED_HOLD_S = 300.0
_chmgr_unsupported_until = 0.0
_chmgr_prev = {}
# Live operator aid, not a record: it starts empty on every restart, and
# twenty entries is more than anybody reads at a glance.
_chmgr_events = deque(maxlen=20)
# The panel polls /api/chmgr on its own timer, outside the global lock, so
# two firmware reads can land here at once.
_chmgr_lock = threading.Lock()


def _chmgr_num(v):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def parse_chmgr_show(text):
    """The firmware answers in position-free k=v tokens plus a score table of
    `(*)<freq>(<busy>)` entries - no line labels, because the whole reply has
    to fit a few hundred bytes."""
    d = {"channels": [], "raw": text}
    for line in text.splitlines():
        s = line.strip()
        m = re.search(r"dcs=(\d+)\((\w+)\)", s)
        if m:
            d["dcs_num"] = int(m.group(1))
            d["dcs_name"] = m.group(2)
        for k, v in re.findall(r"(\w+)=([\w./+-]+)", s):
            if k != "dcs":
                d[k] = _chmgr_num(v)
        for star, freq, busy in re.findall(
                r"(\*?)\s*(\d+\.\d)\(\s*(\d+\.\d)\)", s):
            d["channels"].append({"freq": float(freq), "busy": float(busy),
                                  "current": star == "*"})
    return d


def parse_chmgr_param(text, d):
    for k, v in re.findall(r"(\w+)=([\w.]+)", text):
        if k == "profile":
            d["profile"] = v if v in CHMGR_PROFILES else _chmgr_num(v)
        elif k in _CHMGR_PARAM_KEYS:
            d[_CHMGR_PARAM_KEYS[k]] = _chmgr_num(v)


# A disabled engine has nothing to draw, so it is not asked again at all -
# the configured mode cannot decide that on its own (config.json is this
# daemon's, and a board started by hand disagrees with it in both directions),
# so the firmware's own answer decides. What brings it back is free to check:
# arming the manager starts chmgrd, and a command sent from here invalidates
# the reading anyway. A firmware engine armed by hand with no chmgrd behind it
# is not noticed until this daemon restarts.
def _chmgr_is_on(st):
    return bool(st) and st.get("state") not in (None, "disabled") \
        and str(st.get("mode", "off")) != "off"


def wds_peers(iface):
    """Relays associated to us. A 4-address station is moved onto its own
    AP-VLAN child of the radio (wlan0.staN), so the netdev list names them."""
    try:
        return sorted(x for x in os.listdir("/sys/class/net")
                      if x.startswith(iface + "."))
    except OSError:
        return []


def chmgrd_ensure():
    """Start chmgrd if the engine is armed without it.

    The firmware decides; chmgrd is what carries the decision to hostapd. A
    stack started with the manager off has no daemon, so arming it from the
    dashboard would leave the decision with nothing to act on it - the panel
    says as much, but the button should simply work. Same invocation
    start.py uses at bring-up.
    """
    if run(["pgrep", "-x", "chmgrd"], timeout=5):
        return True
    svc = read_kv_file(SERVICE_CONF)
    exe = os.path.join(svc.get("NRC_SCRIPT_DIR", ""), "chmgrd")
    if not os.path.exists(exe):
        return False
    # a package can ship it without +x, and then the only symptom is that no
    # channel switch ever happens - repair it rather than report nothing
    if not os.access(exe, os.X_OK):
        try:
            os.chmod(exe, 0o755)
        except OSError:
            return False
    try:
        subprocess.Popen([exe, "-i", ap_iface()],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    time.sleep(0.5)
    return bool(run(["pgrep", "-x", "chmgrd"], timeout=5))


_chmgr_refreshing = {"on": False}


def chmgr_status_cached(cfg, max_age):
    """Reading for the dashboard card that never waits on the firmware.

    /api/status runs under the global lock, so a chip round trip here stalls
    every other request - and with a relay attached the guard adds a third
    one. The browser then queues refreshes it cannot finish and the page
    looks dead. Serve the cache and let a worker catch it up.
    """
    got = _chmgr_lock.acquire(False)
    if got:
        try:
            if _chmgr_cache["data"] is not None and \
                    time.time() - _chmgr_cache["at"] < max_age:
                return _chmgr_cache["data"]
        finally:
            _chmgr_lock.release()
    if not _chmgr_refreshing["on"]:
        _chmgr_refreshing["on"] = True

        def worker():
            try:
                chmgr_status(cfg, max_age)
            except Exception:
                pass
            finally:
                _chmgr_refreshing["on"] = False
        threading.Thread(target=worker, daemon=True).start()
    return _chmgr_cache["data"]


def chmgr_status(cfg, max_age=2.0):
    with _chmgr_lock:
        return _chmgr_read(cfg, max_age)


def _chmgr_read(cfg, max_age):
    """Channel-manager status, cached.

    Two firmware round trips, and during the boot sweep the chip does not
    answer until the sweep finishes - so this is deliberately not on the
    one-second dashboard poll's critical path. Returns None only when the
    build has no engine and nobody asked for one, which is what keeps the
    panel out of the way on a board that does not use it.

    config.json is written by this daemon alone, so a manual start.py run
    arms the engine without it: ask the chip and follow that, the way
    full_status() follows the radio for operation_mode.
    """
    global _chmgr_unsupported_until
    now = time.time()
    if not halow_netdevs():
        return _chmgr_cache["data"]        # radio down: nothing to ask
    want = str(cfg.get("channel_manager", "off")) != "off"
    if not want and now < _chmgr_unsupported_until:
        return None
    daemon = bool(run(["pgrep", "-x", "chmgrd"], timeout=5))
    cached = _chmgr_cache["data"]
    if cached is not None:
        if _chmgr_is_on(cached):
            if now - _chmgr_cache["at"] < max_age:
                return cached
        elif daemon == cached.get("daemon") and \
                now - _chmgr_cache["at"] < CHMGR_OFF_TTL:
            # off, and nothing has started or stopped the daemon since.
            # Testing for the daemon appearing rather than for it running
            # matters - it outlives the engine it served, and "running" alone
            # would poll for as long as it does. That same outliving is why
            # the check cannot stand on its own: a manual start.py run arms
            # the engine while chmgrd is already up, and the reading would
            # then never be refreshed. CHMGR_OFF_TTL bounds it.
            return cached

    st = {"supported": True, "error": None, "manual": False,
          "daemon": daemon}
    code, res = cli_run(["show", "chmgr"])
    if code != 200:
        # 409 is another command holding cli_app, which the panel's own poll
        # makes routine: keep showing the last reading instead of an error.
        if code == 409 and _chmgr_cache["data"] is not None:
            return _chmgr_cache["data"]
        st["error"] = res.get("error", "cli_app failed")
    else:
        text = res.get("out", "") or ""
        if "state=" not in text and "mode=" not in text:
            # A firmware with no channel manager answers the passthrough with
            # nothing this can be parsed out of. Test for the fields, not for
            # a "chmgr:" prefix: cli_app looks for that prefix and prints FAIL
            # when it is missing, but this firmware answers with the bare k=v
            # line and no prefix, so the prefix says nothing about whether the
            # engine is there.
            st["supported"] = False
        else:
            st.update(parse_chmgr_show(text))
            # The tuning values change only when the operator sets them, so
            # asking every poll doubled the firmware round trips for nothing
            # - and cli_app is a single channel the operator's own commands
            # have to get into.
            if now - _chmgr_param["at"] > CHMGR_PARAM_TTL:
                code2, res2 = cli_run(["show", "chmgr", "param"])
                if code2 == 200:
                    p2 = {}
                    parse_chmgr_param(res2.get("out", "") or "", p2)
                    _chmgr_param.update({"at": now, "v": p2})
            st.update(_chmgr_param["v"])
            # engine is armed while the GUI's own config says off
            st["manual"] = not want
    if st["supported"]:
        _chmgr_unsupported_until = 0.0
    else:
        _chmgr_unsupported_until = now + CHMGR_UNSUPPORTED_HOLD_S
        if not want:
            return None
    peers = wds_peers(ap_iface())
    st["relay_block"] = peers
    _chmgr_note_events(st)
    st["events"] = list(_chmgr_events)
    _chmgr_cache.update({"at": now, "data": st})
    return st


def _chmgr_note_events(st):
    """Turn counter movement into a short event trail.

    A switch, a veto and a firmware watchdog reset are the three things worth
    knowing after the fact, and the firmware reports them only as running
    totals - so the change has to be noticed by whoever polls, and the browser
    polls far too irregularly to be that.

    Kept in memory here rather than in the health history: that history is a
    record of things that went wrong and raises a banner for each entry, and
    it is only rendered for the Unified AP stack. An automatic channel switch
    is the feature working.
    """
    prev = dict(_chmgr_prev)
    _chmgr_prev.clear()
    for k in ("cur", "sw", "veto", "wdt"):
        if k in st:
            _chmgr_prev[k] = st[k]
    if not prev:
        return                      # first reading is a baseline, not an event
    now = int(time.time())
    if "cur" in prev and st.get("cur") not in (None, prev["cur"]):
        _chmgr_events.appendleft(
            {"at": now, "type": "switch",
             "text": "channel %s -> %s" % (prev["cur"], st["cur"])})
    for key, kind, what in (("veto", "veto", "switch vetoed"),
                            ("wdt", "wdt", "firmware watchdog reset")):
        if key in prev and isinstance(st.get(key), int) \
                and isinstance(prev[key], int) and st[key] > prev[key]:
            _chmgr_events.appendleft(
                {"at": now, "type": kind,
                 "text": "%s (%s -> %s)" % (what, prev[key], st[key])})


def chmgr_only_status(max_age=1.5):
    """Just the channel-manager block, for the panel's own timer.

    The panel refreshes faster than the dashboard does, and the rest of
    full_status() is far more expensive than these two firmware reads. The
    cache is shorter than the fastest poll on offer (2 s) so every tick
    still gets a fresh reading."""
    cfg = get_config()
    mode = cfg.get("operation_mode", "ap")
    live = live_mode(halow_iface_from_net_state())
    if live and mode != "relay" and _MODE_ROLE.get(mode) != live:
        mode = live                     # follow the radio, as full_status does
    if mode not in ("unified-ap", "ap"):
        return None
    return chmgr_status(cfg, max_age)


def chmgr_events_clear():
    """Drop the decision trail. The counters stay: they are the firmware's."""
    with _chmgr_lock:
        _chmgr_events.clear()
        _chmgr_prev.clear()             # next reading is a baseline again
        if _chmgr_cache["data"] is not None:
            _chmgr_cache["data"]["events"] = []


def radio_live(cfg, iface):
    """What the radio is actually tuned to, next to what the GUI asked for.

    The channel the operator sees is the configured one; this is the one on
    the air. They can disagree - a CSA that did not complete, a start script
    that fell back to a conf default, a hand-edited halow.conf - and without
    showing both, the GUI quietly misreports where the radio is."""
    out = {"iface": iface, "channel": None, "configured": None,
           "mismatch": False, "managed": True}
    cc = str(cfg.get("channel") or "").strip()
    out["configured"] = int(cc) if cc.isdigit() else None
    info = run(["iw", "dev", iface, "info"], timeout=5) or ""
    m = re.search(r"channel (\d+)", info)
    if m:
        out["channel"] = int(m.group(1))
    # A radio the stack did not bring up - a start.py run with halow-wlan
    # stopped - is not a setting that failed to take. There is nothing to
    # re-apply, and the banner's advice (save Settings, restart the stack)
    # would tear that AP down. The header already says the mode is manual.
    out["managed"] = unit_active(STACK_ANCHOR_UNIT) == "active"
    # Only a role whose own configuration picks the channel can disagree
    # with it. A station - and a relay, which follows its uplink - takes the
    # channel from the AP it joins, so the saved value says nothing there.
    saved = cfg.get("operation_mode", "ap")
    live = live_mode(halow_iface_from_net_state())
    # a relay reports its AP leg as the live role, but its channel still
    # comes from the uplink, so the saved mode settles that one
    role = saved if saved == "relay" else (live or saved)
    owns_channel = role in ("unified-ap", "ap", "sniffer", "mesh")
    # only a real disagreement counts: no live radio, or "(conf default)"
    # left unset in the GUI, is not a mismatch
    out["mismatch"] = bool(owns_channel and out["managed"] and out["channel"]
                           and out["configured"]
                           and out["channel"] != out["configured"])
    return out


def sniffer_status():
    files = sniffer_pcap_files()
    iface = halow_iface_from_net_state()
    # A sniffer hears one channel. Report the one the radio is actually on,
    # not the configured one: tuned elsewhere, the capture is simply silent
    # and looks indistinguishable from "no traffic".
    info = run(["iw", "dev", iface, "info"], timeout=5) or ""
    m = re.search(r"channel (\d+) \((\d+) MHz\)", info)
    # monitor mode: the firmware refuses every read, so the channel comes
    # from iw plus the driver's mapping table
    cv = channel_view(get_config().get("country", "US"),
                      mac_ch=int(m.group(1)) if m else None)
    mac_ch, s1g_ch, s1g_f = cv["channel"], cv["s1g_channel"], cv["freq_mhz"]
    return {
        "running": sniffer_running(),
        "packets": _sniffer["seq"],
        "iface": iface,
        "channel": mac_ch,
        "s1g_channel": s1g_ch,
        "s1g_freq_mhz": s1g_f,
        "freq_mhz": int(m.group(2)) if m else None,
        "tshark": bool(shutil.which("tshark")),
        "pcap_files": [{"name": os.path.basename(f),
                        "kb": os.path.getsize(f) // 1024} for f in files[:5]],
    }


# ---------------------------------------------------------------- traffic
# Throughput sampler: one thread reads the 11ah netdev counters (always) and
# the interface counters into a ring buffer the Traffic tab
# polls incrementally. Rates are computed here between consecutive samples,
# so the GUI never has to reason about counter resets (driver reload).
TRAFFIC_INTERVAL = 1.0
_traffic = {"buf": deque(maxlen=600), "seq": 0}


def _netdev_bytes(iface):
    try:
        base = "/sys/class/net/%s/statistics/" % iface
        with open(base + "tx_bytes") as f:
            tx = int(f.read())
        with open(base + "rx_bytes") as f:
            rx = int(f.read())
        return tx, rx
    except (OSError, ValueError):
        return None, None


def _iface_group_bytes(iface):
    """Counters for one interface: the netdev plus its AP-VLAN children.

    A station in 4-address (bridge) mode is moved onto its own wlan0.staN
    netdev and its bytes stop passing through wlan0's counters, which is why
    a bridged AP showed no RX at all on the Traffic tab.
    """
    tx = rx = None
    names = [iface]
    try:
        names += sorted(x for x in os.listdir("/sys/class/net")
                        if x.startswith(iface + "."))
    except OSError:
        pass
    for n in names:
        a, b = _netdev_bytes(n)
        if a is None:
            continue
        tx = a if tx is None else tx + a
        rx = b if rx is None else rx + b
    return tx, rx


def traffic_ifaces():
    """(AP-side netdev, station-side netdev) for the Traffic tab.

    The second one is None unless the radio really runs two interfaces. A
    relay does: its AP serves the stations below it and its STA side carries
    everything going upstream, so one counter pair cannot stand for both.
    """
    devs = halow_netdevs()
    if not devs:
        return halow_iface_from_net_state(), None
    ap = ap_iface()
    sta = station_iface()
    return ap, (sta if len(devs) > 1 and sta != ap else None)


def _read_kv_ints(path):
    kv = {}
    with open(path) as f:
        for line in f:
            p = line.split()
            if len(p) == 2 and p[1].isdigit():
                kv[p[0]] = int(p[1])
    return kv


def _traffic_sampler():
    prev = {}          # key -> (value, ts); delta only within the same iface

    def rate(key, val, now):
        if val is None:
            prev.pop(key, None)
            return None
        old = prev.get(key)
        prev[key] = (val, now)
        if not old or now <= old[1]:
            return None
        d = val - old[0]
        # counter reset (driver reload) -> one-sample gap, not a spike
        return None if d < 0 else int(round(d / (now - old[1])))

    while True:
        try:
            now = time.time()
            ap, sta = traffic_ifaces()
            if prev.get("ifaces") != (ap, sta):
                prev.clear()
                prev["ifaces"] = (ap, sta)
            wtx, wrx = _iface_group_bytes(ap)
            row = {"t": round(now, 1),
                   "wtx": rate("wtx", wtx, now), "wrx": rate("wrx", wrx, now)}
            if sta:
                stx, srx = _iface_group_bytes(sta)
                row["stx"] = rate("stx", stx, now)
                row["srx"] = rate("srx", srx, now)
            # CCA busy % of our own channel, when a scanner board is attached
            # and visited it within the last few seconds (a 13-channel sweep
            # at 200 ms comes round every ~2.6 s; a focused board every dwell)
            # The value is held for up to 3 s so the line stays continuous;
            # "ccaf" marks the row where a fresh reading actually arrived.
            row["cca"] = row["ccan"] = None
            if _cca is not None:
                f, bw = _cca_ap_channel()
                m = _cca.latest_for(f, bw, 3.0) if f else None
                if m:
                    row["cca"] = m["busy"]
                    if m["noise"] != -128:
                        row["ccan"] = m["noise"]
                    if m["t"] != prev.get("cca_t"):
                        prev["cca_t"] = m["t"]
                        row["ccaf"] = 1
            _traffic["seq"] += 1
            row["seq"] = _traffic["seq"]
            _traffic["buf"].append(row)
        except Exception:
            pass                       # sampler must survive anything
        time.sleep(TRAFFIC_INTERVAL)


def traffic_start():
    threading.Thread(target=_traffic_sampler, daemon=True).start()


# ------------------------------------------------------- firmware-derived bits
# TPC settles when the link comes up: a station's capability is what it put in
# its association request, and this board's own setting is applied before the
# interface is brought up. So it is read when the association changes, not on a
# timer - polling it put the firmware's state dump on the UART of anyone
# watching, every few seconds, for a value that had not moved.
#
# The exception is the per-station modulation, which is the one firmware
# reading that is live; it answers into the result buffer and prints nothing.
FW_LIVE_TTL = 3.0        # per-station rates
FW_SETTLED_TTL = 900.0   # safety net: a value changed by hand still shows up
FW_FAIL_TTL = 5.0        # "not ready yet" - retry soon, but not every poll
_fw_cache = {}


def _fw_invalidate():
    """An operator command may have changed what settled at association."""
    _fw_cache.pop("tpc", None)
    _sta_tpc.clear()
    _sta_tpc_asked[0] = 0.0
    _chmgr_cache["at"] = 0.0        # `set chmgr` may have just armed it


def _fw_cached(key, argv, ttl=FW_SETTLED_TTL):
    """Run one cli_app command, at most once per `ttl`. None = no answer.

    The lock has to be waited for, not tried: the channel-manager panel polls
    every second and holds it for CLI_MIN_INTERVAL each time, so a call that
    gives up on contention never runs at all. A miss is not cached either -
    caching it would hide the next reading for a whole TTL.
    """
    now = time.time()
    hit = _fw_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    if not halow_netdevs():
        # No HaLow interface: the driver is not loaded, so every cli_app call
        # would hold the lock for a second and come back with FAIL. Nothing
        # to ask until the radio exists.
        return hit[1] if hit else None
    code, res = cli_run(argv, queue_s=3.0)
    if code != 200:
        return hit[1] if hit else None
    out = res.get("out", "")
    _learn_vif_roles(out)
    # A bare FAIL means the firmware is not up, not that this is the answer.
    # Holding it for the settled TTL left the dashboard showing n/a for 15
    # minutes after the radio came up; asking again on every poll costs a
    # second of the cli_app lock each time. Keep it for a few seconds.
    stamp = now - max(0.0, ttl - FW_FAIL_TTL) if _cli_failed(out) else now
    _fw_cache[key] = (stamp, out)
    return out


def _cli_failed(out):
    """cli_app exits 0 and prints a bare "FAIL" when the firmware refuses or
    is not up yet, so the return code says nothing - the text does."""
    s = (out or "").strip()
    return not s or all(l.strip() in ("FAIL", "") for l in s.splitlines())


def _cli_kv(out):
    """cli_app prints structured answers as "Key    : value" lines."""
    kv = {}
    for line in (out or "").splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip():
            kv[k.strip()] = v.strip()
    return kv


def tpc_state():
    """{'on': bool, 'role': str, 'peer': bool} - or {} when unavailable.

    `set tpc show` reports this board's own setting. The peer field is what
    the associated AP advertised, which is only meaningful on a station.
    """
    kv = _cli_kv(_fw_cached("tpc", ["set", "tpc", "show"]))
    if "TPC" not in kv:
        return {}
    return {"on": kv["TPC"] == "on", "role": kv.get("Role", ""),
            "peer": kv.get("Peer TPC") == "on"}


# "0   bc:10:2f:de:4e:7d   1   ASSOC   3.34MBit/s(MCS 7)   3.34MBit/s(MCS 7)"
_STA_RATE_RE = re.compile(
    r"^\s*\d+\s+([0-9a-fA-F:]{17})\s+(\d+)\s+(\S+)\s+"
    r"([\d.]+)MBit/s\(MCS\s*(\d+)\)\s+([\d.]+)MBit/s\(MCS\s*(\d+)\)")


# The channel a HaLow radio runs on has two numbers: the S1G channel, which
# is what the air carries, and the mac80211 channel the driver maps it to
# (what hostapd, iw and the conf files use). The driver table gives the S1G
# frequency for every mac80211 channel, so both numbers come out of what the
# host already knows - no firmware call on the polling path.
FW_CHAN_TTL = FW_SETTLED_TTL


# S1G channel numbering: centre frequency = starting frequency + 0.5 x
# channel number (802.11ah). Only the starting frequency differs per
# regulatory domain. US is confirmed against the firmware (927.0 MHz ->
# channel 50). A domain that is not listed keeps the mac80211 number, and
# the firmware is asked instead where it answers at all.
S1G_START_MHZ = {"US": 902.0, "AU": 915.0, "NZ": 915.0,
                 "EU": 863.0, "JP": 916.5}


def s1g_channel_from_freq(freq_mhz, country):
    """S1G channel number for a centre frequency, None when not derivable."""
    start = S1G_START_MHZ.get(str(country or "").upper())
    if not start or not freq_mhz:
        return None
    n = (float(freq_mhz) - start) / 0.5
    return int(round(n)) if 0 < n < 256 and abs(n - round(n)) < 0.01 else None


def fw_channel_numbers():
    """Last resort: read the pair from `show config` (monitor mode: FAIL)."""
    out = _fw_cached("cfg_channels", ["show", "config"], FW_CHAN_TTL)
    if not out:
        return {}
    res = {}
    m = re.search(r"^Frequency\s*:\s*(\d+)\s*\((\d+)\)", out, re.M)
    if m:
        res["s1g_freq_mhz"] = int(m.group(1)) / 10.0
        res["s1g_channel"] = int(m.group(2))
    m = re.search(r"^MAC80211_freq\s*:\s*\d+\s*\((\d+)\)", out, re.M)
    if m:
        res["mac80211_channel"] = int(m.group(1))
    return res


def channel_view(country, mac_ch=None, s1g_freq=None, nons1g_freq=None):
    """{"channel", "s1g_channel", "freq_mhz"} from whatever number is at hand.

    Accepts the mac80211 channel (ap.env, iw), the S1G centre frequency
    (ap.env, `show chmgr`) or the mac80211 frequency the supplicant reports,
    and fills in the rest from the driver's mapping table.
    """
    tbl = s1g_channels(country) or {}
    try:
        mac_ch = int(mac_ch) if str(mac_ch or "").strip() else None
    except (TypeError, ValueError):
        mac_ch = None
    if mac_ch is None and nons1g_freq:
        for ch, row in tbl.items():
            if len(row) > 2 and int(row[2]) == int(nons1g_freq):
                mac_ch = ch
                break
    if s1g_freq is None and mac_ch is not None and mac_ch in tbl:
        s1g_freq = tbl[mac_ch][0] / 10.0
    if mac_ch is None and s1g_freq:
        for ch, row in tbl.items():
            if abs(row[0] / 10.0 - float(s1g_freq)) < 0.05:
                mac_ch = ch
                break
    row = tbl.get(mac_ch) if mac_ch is not None else None
    out = {"channel": mac_ch, "freq_mhz": s1g_freq,
           "s1g_channel": s1g_channel_from_freq(s1g_freq, country),
           # the table knows the width of each channel; ap.env only holds
           # the one the last start used, and the manager changes it too
           "bw_mhz": ({0: 1, 1: 2, 2: 4}.get(row[1])
                      if row and len(row) > 1 else None)}
    if out["s1g_channel"] is None and out["channel"] is not None:
        # a domain whose S1G numbering is not tabulated here: ask the chip,
        # once every FW_CHAN_TTL rather than on every poll
        fw = fw_channel_numbers()
        if fw:
            out["s1g_channel"] = fw.get("s1g_channel")
            out["freq_mhz"] = fw.get("s1g_freq_mhz") or out["freq_mhz"]
    return out


def fw_sta_rates(vif=0):
    """{mac: {tx_mcs, rx_mcs, tx_rate, rx_rate}} from `show sta <vif> all`.

    The firmware records the modulation of the last frame it exchanged with
    each station. The driver's sta_rate debugfs file cannot be used for this:
    it reads the S1G SIG, which the firmware only copies into the host buffer
    in monitor mode, so on an AP it reports one wrong value forever.
    """
    out = {}
    res = _fw_cached("sta_all_%d" % vif, ["show", "sta", str(vif), "all"],
                     FW_LIVE_TTL)
    if res is None:
        return out          # cli_app busy or absent: no answer, not a hint
    if "Invalid command" in res or _cli_failed(res):
        # asked the STA side for a station table: try the AP side instead
        o = other_vif(vif)
        if o is None:
            return out
        res = _fw_cached("sta_all_%d" % o, ["show", "sta", str(o), "all"],
                         FW_LIVE_TTL) or ""
        vif = o
    for line in res.splitlines():
        m = _STA_RATE_RE.match(line)
        if m:
            _vif_role[vif] = "ap"        # it answered, so it is the AP
            out[m.group(1).lower()] = {
                "tx_rate": float(m.group(4)), "tx_mcs": int(m.group(5)),
                "rx_rate": float(m.group(6)), "rx_mcs": int(m.group(7))}
    return out


_sta_tpc = {}
_sta_conn = {}
_sta_tpc_asked = [0.0]


def _conn_secs(v):
    """Seconds out of iw's "connected time" ("158 seconds")."""
    m = re.match(r"\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def fw_sta_tpc(macs, vif=0):
    """{mac: bool} - which stations asked for TPC in their association.

    Capability, not state: a station sends it once, in its association
    request, and it cannot change while the association lasts. So the
    firmware is asked when a station appears that we have no answer for,
    and not again - a timer here would print the station table to the UART
    for a value that never moves.
    """
    macs = {m.lower() for m in macs}
    for gone in set(_sta_tpc) - macs:
        del _sta_tpc[gone]
    for gone in set(_sta_conn) - macs:
        del _sta_conn[gone]
    if not macs - set(_sta_tpc):
        return dict(_sta_tpc)
    # Asking is not free, and a station the firmware does not list would
    # otherwise be asked about on every poll for as long as it stays.
    now = time.time()
    if now - _sta_tpc_asked[0] < FW_LIVE_TTL * 10:
        return dict(_sta_tpc)
    _sta_tpc_asked[0] = now
    mac = None
    code, res = cli_run(["show", "uinfo", str(vif)], queue_s=3.0)
    if code != 200:
        return dict(_sta_tpc)
    _learn_vif_roles(res.get("out", ""))
    for line in res.get("out", "").splitlines():
        m = re.search(r"mac_addr\(([0-9a-fA-F:]{17})\)", line)
        if m:
            mac = m.group(1).lower()
            continue
        m = re.search(r"\btpc\((\d)\)", line)
        if m and mac:
            if mac in macs:
                _sta_tpc[mac] = m.group(1) == "1"
            mac = None
    return dict(_sta_tpc)


# ---------------------------------------------------------------- status data
def hostapd_cli_path():
    for c in ("/usr/sbin/hostapd_cli", "/sbin/hostapd_cli", "hostapd_cli"):
        if c.startswith("/") and not os.path.exists(c):
            continue
        return c
    return None


def hostapd_status(iface):
    cli = hostapd_cli_path()
    out = run([cli, "-p", HOSTAPD_CTRL, "-i", iface, "status"], timeout=5) if cli else None
    if not out:
        return {}
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


_AP_LINK_RE = re.compile(
    r"^([0-9a-fA-F:]{17})\s+(\S+)\s+([\d.]+)\s*MBit/s\(MCS\s*(\d+)\)"
    r"\s+([\d.]+)\s*MBit/s\(MCS\s*(\d+)\)")
_ap_tpc = {}        # bssid -> (advertised TPC, connected secs at the read)


# Which vif holds which role is not fixed either. The driver hands out vif
# indexes in the order the interfaces are created, so a relay's AP can end up
# on either one, and a read aimed at the wrong vif is refused - and printed on
# the firmware console - every single time it is asked. The firmware names the
# role in that refusal, so its own answers are what the mapping is built from.
HALOW_VIF_MAX = 2
_vif_role = {}                  # vif -> "ap" | "sta"
_VIF_ROLE_RE = re.compile(r"vif(\d+) is (ap|station)")


def _learn_vif_roles(out):
    """Record the role the firmware named while refusing a wrong-vif read."""
    for m in _VIF_ROLE_RE.finditer(out or ""):
        _vif_role[int(m.group(1))] = "ap" if m.group(2) == "ap" else "sta"


def vif_of(iface):
    """The firmware vif behind a netdev.

    The role the kernel reports for the interface picks the vif the firmware
    said holds that role. Until the firmware has said anything, the name is
    the best guess - and a single interface is always vif 0, whatever it is
    called, because it was the first one created.
    """
    role = iface_role(iface)
    if role in ("ap", "sta"):
        same = [v for v, r in _vif_role.items() if r == role]
        if len(same) == 1:
            return same[0]
    if len(halow_netdevs()) <= 1:
        return 0
    m = re.search(r"(\d+)$", str(iface or ""))
    n = int(m.group(1)) if m else 0
    return n if n < HALOW_VIF_MAX else 0


def halow_vifs():
    """{vif: role} for every vif the radio currently runs.

    A vif-scoped cli_app command has to be sent once per vif: a relay's AP
    and its STA side keep their settings apart, so naming only one of them
    leaves the other on whatever it started with. Commands with no vif
    argument are board-wide and are sent once.
    """
    return {vif_of(n): r for n, r in halow_iface_roles()} or {0: None}


def other_vif(vif):
    """The other vif, when the radio runs two of them."""
    if len(halow_netdevs()) < 2 or vif not in (0, 1):
        return None
    return 1 - vif


def fw_ap_link(vif=0):
    """The AP this station is attached to: bandwidth and the modulation of
    the last frame each way, from `show ap <vif>`.

    `iw link` cannot answer this on S1G - it reports the legacy rate - so the
    figure the AP table shows for its stations comes from the same firmware
    table, only read from the other side.
    """
    d = _fw_ap_link(vif)
    if d or _vif_role.get(vif) != "ap":
        return d
    # the vif we asked turned out to be the AP: the station is the other one
    o = other_vif(vif)
    return _fw_ap_link(o) if o is not None else {}


def _fw_ap_link(vif):
    out = _fw_cached("ap_link_%d" % vif, ["show", "ap", str(vif)], FW_LIVE_TTL)
    if out is None:
        return {}           # cli_app busy or absent: no answer, not a hint
    if "Invalid command" in out:
        # wrong vif: the firmware says so on its console every time it is
        # asked, so remember the refusal instead of repeating it
        _fw_cache["ap_link_%d" % vif] = (time.time() + FW_SETTLED_TTL, "")
        return {}
    if _cli_failed(out):
        # cli_app relays a bare FAIL while the link is not up yet. It must not
        # be held: the short live TTL asks again on the next poll.
        return {}
    for line in (out or "").splitlines():
        m = _AP_LINK_RE.match(line.strip())
        if m:
            _vif_role[vif] = "sta"       # it answered, so it is the station
            return {"bssid": m.group(1).lower(), "bw": m.group(2),
                    "tx_rate": float(m.group(3)), "tx_mcs": int(m.group(4)),
                    "rx_rate": float(m.group(5)), "rx_mcs": int(m.group(6))}
    return {}


def fw_ap_tpc(bssid, vif=0, conn=None):
    """Whether the AP advertised transmit power control.

    It is a property of the association, not something that changes while
    connected, so it is read once and kept. Joining the same AP again is a
    new association and the AP may have been reconfigured meanwhile, so a
    connected time that went backwards drops the cached answer. `show uinfo`
    prints tpc() twice - once under AP INFO, once under STA INFO; the first
    one is the AP's.
    """
    if not bssid:
        return None
    key = bssid.lower()
    if key in _ap_tpc:
        val, was = _ap_tpc[key]
        if conn is None or was is None or conn >= was:
            _ap_tpc[key] = (val, conn if conn is not None else was)
            return val
    out = _fw_cached("uinfo_%d" % vif, ["show", "uinfo", str(vif)],
                     FW_LIVE_TTL) or ""
    if "Invalid command" in out:
        _ap_tpc[key] = (None, conn)
        return None
    in_ap = False
    for line in out.splitlines():
        if "AP INFO" in line:
            in_ap = True
            continue
        if not in_ap:
            continue
        if "STA INFO" in line:
            break
        m = re.search(r"\btpc\((\d+)\)", line)
        if m:
            _ap_tpc.clear()          # a station holds one association
            _ap_tpc[key] = (m.group(1) == "1", conn)
            return _ap_tpc[key][0]
    return None


def we_serve_dhcp():
    """True when this run's own DHCP server is the one handing out leases."""
    plan = load_json("/run/halow/net.json", {}) or {}
    srv = plan.get("dhcp_server") or {}
    return bool(srv.get("ranges"))


def station_ips():
    """Best-effort mac -> ip map for connected stations.

    The neighbour table holds what this board has actually resolved, so it
    decides. Our own DHCP leases only fill in a station the board has not
    talked to yet, and only while we are the server: the lease file keeps a
    day of history, so after a switch to the upstream LAN's DHCP it still
    names the address of the run before - which is how a station showed the
    AP's old 192.168.200.x while it was living on the LAN's 192.168.1.x.
    """
    ips = {}
    if we_serve_dhcp():
        now = time.time()
        try:
            with open("/var/lib/misc/dnsmasq.leases") as f:
                for line in f:
                    p = line.split()
                    if len(p) >= 3 and ":" in p[1] and \
                            (not p[0].isdigit() or int(p[0]) > now):
                        ips[p[1].lower()] = p[2]
        except OSError:
            pass
    # The neighbour table keeps a pairing until something proves it wrong, so
    # after a device renews onto another address the old one is still sitting
    # there - and that is the address the GUI then offers its ping button.
    # Our own lease, while we are the server, is the newer fact of the two.
    leased = dict(ips)
    dead = set()
    out = run(["ip", "-4", "neigh"], timeout=5) or ""
    for line in out.splitlines():
        m = re.match(r"([0-9.]+)\s+dev\s+\S+\s+lladdr\s+([0-9a-fA-F:]{17})",
                     line)
        if m and "FAILED" not in line:
            mac = m.group(2).lower()
            if mac not in leased:
                ips[mac] = m.group(1)
            continue
        m = re.match(r"([0-9.]+)\s+dev\s+\S+.*FAILED", line)
        if m:
            dead.add(m.group(1))
    # A lease says what we handed out, not what the device took: one that
    # kept an address from another network ignores it until the lease runs
    # out. The kernel asked and got no answer - that beats the lease, and
    # the ping button must not be offered an address nobody answers for.
    for mac in [m for m, ip in ips.items() if ip in dead]:
        del ips[mac]
    # A neighbour entry outlives the network it was learned on, so drop what
    # this board can no longer reach rather than print a stale address.
    subs = local_subnets()
    return {m: ip for m, ip in ips.items() if _on_subnets(ip, subs)} \
        if subs else ips


# ------------------------------------------------------------ bridged devices
# Who is reachable through br0? The bridge already knows: its forwarding table
# holds every MAC it has seen, per port, so a camera cabled to the STA's eth0
# shows up there the moment it says anything - and on the AP the same MAC
# appears on the HaLow port, because 4-address frames carry it across. IPs
# come from the ARP/neighbour table when this board has talked to the device;
# for the ones it hasn't, a short on-demand ARP sniff picks up the sender
# address of their periodic gateway ARPs (how the camera was found by hand).
_bridge_probe = {"lock": threading.Lock(), "last": 0.0}
# Addresses are never remembered: an address this board saw an hour ago says
# nothing about now, and a table that shows one is worse than a blank column.
# Every reader takes the DHCP lease file (while we are the server) and the
# kernel neighbour table, and shows nothing when neither knows.
# The snooper keeps this only to avoid re-pinning the same pairing.
_arp_seen = {}
# Addresses this daemon pinned as permanent bridge neighbours (_neigh_sync);
# only these may be deleted again when their claim moves off the radio side.
_neigh_pinned = set()


def _iface_subnets(dev):
    """(network, prefix) of every IPv4 on one interface."""
    out = []
    for line in (run(["ip", "-4", "-o", "addr", "show", "dev", dev],
                     timeout=5) or "").splitlines():
        m = re.search(r"inet ([0-9.]+)/(\d+)", line)
        if m:
            out.append((_ip_int(m.group(1)), int(m.group(2))))
    return out


_subnets_cache = {"at": 0.0, "v": []}


def local_subnets():
    """Subnets this board reaches on the HaLow side, read from the kernel.

    One answer for every layout, so no caller has to guess which one it is
    in: a bridged run carries the address on br0, a NAT run on the HaLow
    netdev, a station on its own leg. Every address the GUI shows for a peer
    - a station, a bridged device, the AP we joined - must sit on one of
    these, or it is a leftover from a network this board has left. Memoised
    for a second: the poll asks for it several times per refresh.
    """
    now = time.time()
    if now - _subnets_cache["at"] < 1.0:
        return list(_subnets_cache["v"])
    devs = (["br0"] if os.path.isdir("/sys/class/net/br0") else []) \
        + halow_netdevs()
    out = []
    for d in devs:
        out += _iface_subnets(d)
    _subnets_cache.update({"at": now, "v": out})
    return list(out)


def _on_subnets(ip, subnets):
    """True when ip sits on one of (network, prefix)."""
    v = _ip_int(ip)
    if v is None:
        return False
    for net, pfx in subnets:
        mask = (0xFFFFFFFF << (32 - pfx)) & 0xFFFFFFFF
        if net is not None and (v & mask) == (net & mask):
            return True
    return False


def _ip_int(ip):
    try:
        a, b, c, d = (int(x) for x in ip.split("."))
    except ValueError:
        return None
    return (a << 24) | (b << 16) | (c << 8) | d


def reachable_ips(ips, subnets=None):
    """The addresses of this list this board can still reach.

    A device keeps its entry for half a day, so after the network changed the
    table listed every address it ever held - 192.168.1.67, 192.168.200.17,
    two addresses on one MAC. Only what shares a subnet with us is current.
    """
    subs = local_subnets() if subnets is None else subnets
    if not subs:
        return list(ips)
    return [ip for ip in ips if _on_subnets(ip, subs)]


def _neigh_sync(mac, ip):
    """Mirror a learned pairing into a permanent bridge neighbour when (and
    only when) the device sits on a HaLow port with ARP suppression armed.

    halow_flood_off.sh turns on neigh_suppress and turns off broadcast_flood
    on the wlan* bridge ports: upstream ARP for a pinned device is answered
    by the bridge itself and never burns airtime, and everything else the
    office LAN broadcasts stays off the air. The pins are what makes that
    safe - without one, a HaLow device would become unreachable from the
    LAN. On boards where the guard is not armed (plain STA bridges, old
    kernels without the sysfs knob) this is a no-op, so no pile of
    permanent entries builds up where suppression cannot use them.
    A pairing that moves to a non-HaLow MAC drops its pin again, so the
    bridge never keeps answering for a host that left the radio side."""
    port = None
    out = run(["bridge", "fdb", "show", "br", "br0"], timeout=5) or ""
    for line in out.splitlines():
        p = line.split()
        if p and p[0].lower() == mac and "dev" in p:
            try:
                port = p[p.index("dev") + 1]
            except (ValueError, IndexError):
                pass
            break
    armed = False
    if port and port.startswith("wlan"):
        try:
            with open("/sys/class/net/br0/brif/%s/neigh_suppress" % port) as f:
                armed = f.read().strip() == "1"
        except OSError:
            pass
    if armed:
        run(["ip", "neigh", "replace", ip, "lladdr", mac,
             "dev", "br0", "nud", "permanent"], timeout=5)
        _neigh_pinned.add(ip)
        return
    # Not (or no longer) a suppressed HaLow port: any permanent entry left
    # for this address is a stale pin of ours - nothing else creates
    # permanent bridge neighbours on these boards. Checking the kernel
    # rather than _neigh_pinned also catches pins from a previous webd run.
    if ip in _neigh_pinned or "PERMANENT" in (
            run(["ip", "neigh", "show", ip, "dev", "br0"], timeout=5) or ""):
        run(["ip", "neigh", "del", ip, "dev", "br0"], timeout=5)
        _neigh_pinned.discard(ip)


def bridge_port_cleaner_start():
    """Keep addresses off the radio's bridge ports.

    hostapd moves every 4-address station onto its own AP-VLAN, created long
    after bring-up ran its one-shot cleaner - and the system DHCP client
    takes each new netdev it sees, leaving an IPv4LL address on a port that
    must carry none. Two interfaces answering for one subnet is ARP flux,
    and the address is useless there in any case. Wireless bridge ports
    only: the wired port keeps its own client.
    """
    def loop():
        while True:
            try:
                for br in os.listdir("/sys/class/net"):
                    brif = "/sys/class/net/%s/brif" % br
                    if not os.path.isdir(brif):
                        continue
                    for port in os.listdir(brif):
                        if not os.path.isdir("/sys/class/net/%s/phy80211"
                                             % port):
                            continue
                        if not iface_ipv4(port):
                            continue
                        run(["dhcpcd", "-k", port], timeout=10)
                        run(["ip", "addr", "flush", "dev", port], timeout=10)
                        print("[net] %s: address removed from bridge port %s"
                              % (br, port), flush=True)
            except Exception:
                pass
            time.sleep(15)
    threading.Thread(target=loop, daemon=True).start()


def arp_snooper_start():
    """Passively learn MAC<->IPv4 pairings from every ARP crossing br0.

    Foreign 4addr stations (boards not running this GUI) never announce
    themselves over the UDP discovery, and the bridge forwards their traffic
    without the host stack ever learning an address - so Connected Stations
    showed no IP for them. Every IPv4 host ARPs sooner or later, though, and
    the ARP sender fields carry exactly the pairing we want. A raw AF_PACKET
    socket joined to br0 in promiscuous membership sees each bridged ARP
    (request or reply, broadcast or unicast) - pure python, always on, no
    tcpdump/tshark needed (the Rockchip board ships neither)."""
    def loop():
        SOL_PACKET, PACKET_ADD_MEMBERSHIP, PACKET_MR_PROMISC = 263, 1, 1
        while True:
            try:
                sk = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                   socket.htons(0x0806))
                sk.bind(("br0", 0))
                mreq = struct.pack("iHH8s", socket.if_nametoindex("br0"),
                                   PACKET_MR_PROMISC, 0, b"")
                sk.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
            except OSError:
                time.sleep(30)          # no bridge (yet/anymore) - retry
                continue
            # The bridge is up: restore the ARP-proxy pins the kernel lost
            # across the restart that brought us here (cache survives, pins
            # do not). Ports enslaved later re-pin as their devices ARP.
            last_save = 0.0
            while True:
                try:
                    pkt = sk.recv(2048)
                except OSError:
                    break               # bridge went away - reopen
                if len(pkt) < 42:
                    continue
                # ethernet(14) + arp: sender hw @22..27, sender ip @28..31
                mac = ":".join("%02x" % b for b in pkt[22:28])
                ip = ".".join(str(b) for b in pkt[28:32])
                if ip == "0.0.0.0" or mac == "00:00:00:00:00:00":
                    continue            # DAD probe / garbage
                if _arp_seen.get(mac) != ip:
                    _arp_seen[mac] = ip
                    _neigh_sync(mac, ip)
            try:
                sk.close()
            except OSError:
                pass
    threading.Thread(target=loop, daemon=True, name="arp-snoop").start()


# ------------------------------------------------------ AP discovery (webd)
# A station cannot learn its AP's management address from the air: the AP's
# bridge answers ARP with its wired MAC, not the radio MAC the station knows
# as the BSSID. But both ends run this daemon - so the AP answers a one-line
# UDP question with its address and radio MAC, and the station accepts the
# reply whose radio MAC equals its BSSID. That pins "ping the AP" to the AP
# itself instead of falling back to the uplink gateway.
#
# It is the one thing here that puts a frame on the air, so it never runs on
# a timer: the question is a broadcast, and a broadcast wakes every sleeping
# station. Only the operator's "probe IPs" asks.
DISCOVER_PORT = 8442
DISCOVER_MAGIC = b"HALOW-WEBD-DISCOVER-1"
DISCOVER_TTL = 300.0
_discovered = {}                   # radio MAC -> (ip, seen at); memory only
_discovered_lock = threading.Lock()


def _discover_put(mac, ip):
    if not mac or not ip:
        return
    with _discovered_lock:
        _discovered[str(mac).lower()] = (str(ip), time.time())


def _card_take(d):
    """File a peer's card: its address under every radio MAC it named."""
    ip = d.get("ip")
    macs = d.get("radio_macs") or [d.get("radio_mac")]
    out = []
    for m in macs:
        if m and ip:
            _discover_put(m, ip)
            out.append((str(m).lower(), str(ip)))
    return out


def discovered_ip(mac):
    """What a probe paired with this radio MAC, while the answer still holds.

    A probe answer outlives the network it was taken on: switch the AP from
    serving DHCP to taking a lease and every peer moves subnet, but the TTL
    has not run out. So the address must still sit on a subnet this board is
    on, or it names a network nobody is in any more.
    """
    if not mac:
        return ""
    with _discovered_lock:
        e = _discovered.get(str(mac).lower())
    if not e or time.time() - e[1] > DISCOVER_TTL:
        return ""
    return e[0] if reachable_ips([e[0]], local_subnets()) else ""


def discovery_forget():
    """Drop every probe answer - the network they were taken on is gone."""
    with _discovered_lock:
        n = len(_discovered)
        _discovered.clear()
    if n:
        print("[net] discovery: %d entries dropped (stack changed)" % n,
              flush=True)


def _my_card():
    """This board's address and every radio MAC a peer may know it by.

    A relay is two things at once: its stations know it by the AP vif MAC,
    its own AP knows it by the STA vif MAC. Sending one would answer only
    half the network, so the card carries them all and the asker files the
    address under each.
    """
    env = read_kv_file(AP_ENV_FILE)
    iface = env.get("HALOW_IFACE", "wlan0")
    macs = []
    for dev in [iface] + list(halow_netdevs()):
        try:
            with open("/sys/class/net/%s/address" % dev) as f:
                mac = f.read().strip().lower()
        except OSError:
            continue
        if mac and mac not in macs:
            macs.append(mac)
    return macs, (iface_ipv4("br0") or iface_ipv4(iface) or "")


def discovery_responder_start():
    """Answer discovery datagrams. Never speaks first.

    The reply carries this board's radio MAC and address, which is no more
    than an ARP of the same LAN shows, and the question carries the asker's -
    so one exchange pairs both ends.
    """
    def loop():
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sk.bind(("0.0.0.0", DISCOVER_PORT))
        except OSError:
            return
        while True:
            try:
                data, addr = sk.recvfrom(256)
                if not data.startswith(DISCOVER_MAGIC):
                    continue
                tail = data[len(DISCOVER_MAGIC):].strip()
                if tail:
                    try:
                        _card_take(json.loads(tail.decode()))
                    except (ValueError, UnicodeDecodeError):
                        pass
                macs, ip = _my_card()
                if not (macs and ip):
                    continue
                sk.sendto(json.dumps({"v": 1, "ip": ip,
                                      "radio_mac": macs[0],
                                      "radio_macs": macs}).encode(), addr)
            except Exception:
                time.sleep(0.5)
    threading.Thread(target=loop, daemon=True, name="halow-discover").start()


def _halow_side_dev():
    """The netdev that faces the HaLow network - br0 when we bridge onto it."""
    env = read_kv_file(AP_ENV_FILE)
    iface = env.get("HALOW_IFACE", "wlan0")
    if os.path.isdir("/sys/class/net/br0") and iface_ipv4("br0"):
        return "br0"
    return iface


def _halow_probe_from():
    """(device, source address, broadcast) the probe should ask from.

    br0 can hold two addresses: the one the uplink LAN handed this board and
    the one it serves the HaLow side on. Only the second has peers that can
    answer - a station on 192.168.200.x has no route back to a question asked
    from 192.168.1.98 - and it is the one the default gateway does not sit in.
    """
    dev = _halow_side_dev()
    addrs = []
    for line in (run(["ip", "-4", "-o", "addr", "show", "dev", dev],
                     timeout=5) or "").splitlines():
        m = re.search(r"inet ([0-9.]+)/(\d+)", line)
        if m:
            addrs.append((m.group(1), int(m.group(2))))
    if not addrs:
        return dev, None, None
    # dhcpcd leaves a link-local address behind when no lease arrives, and no
    # peer can answer a question asked from it
    real = [a for a in addrs if not a[0].startswith("169.254.")]
    if real:
        addrs = real
    if len(addrs) > 1:
        # the address this board serves the HaLow side on is the one its
        # stations have a route back to
        want = str(get_config().get("ap_ip", "")).strip().split("/")[0]
        served = [a for a in addrs if a[0] == want] if want else []
        if served:
            addrs = served
    if len(addrs) > 1:
        m = re.search(r"default via ([0-9.]+)",
                      run(["ip", "route", "show", "default", "dev", dev],
                          timeout=5) or "")
        if m:
            g = _ip_int(m.group(1))
            rest = [(a, p) for a, p in addrs
                    if (_ip_int(a) >> (32 - p)) != (g >> (32 - p))]
            if rest:
                addrs = rest
    ip, pfx = addrs[0]
    net = _ip_int(ip) & (((1 << pfx) - 1) << (32 - pfx))
    bcast = net | ((1 << (32 - pfx)) - 1)
    return dev, ip, ".".join(str((bcast >> sh) & 0xff)
                             for sh in (24, 16, 8, 0))


def discovery_probe(wait=1.0):
    """Ask once and take what answers. Operator-driven only.

    Bound to the HaLow-facing device: a bench board's default route points at
    its wired management LAN, and an unbound broadcast would go there instead
    - asking the wrong network and telling it who we are.
    """
    macs, ip = _my_card()
    ident = json.dumps({"radio_mac": macs[0], "radio_macs": macs,
                        "ip": ip}).encode() if macs and ip else b""
    found, sk = {}, None
    dev, src, bcast = _halow_probe_from()
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sk.setsockopt(socket.SOL_SOCKET, 25,          # SO_BINDTODEVICE
                          dev.encode() + b"\0")
        except OSError:
            pass                       # not root, or the device went away
        if src:
            sk.bind((src, 0))          # the address peers can answer
        sk.settimeout(0.4)
        sk.sendto(DISCOVER_MAGIC + b" " + ident,
                  (bcast or "255.255.255.255", DISCOVER_PORT))
        deadline = time.time() + max(0.2, wait)
        while time.time() < deadline:
            try:
                data, _ = sk.recvfrom(512)
            except socket.timeout:
                continue
            try:
                d = json.loads(data.decode())
            except (ValueError, UnicodeDecodeError):
                continue
            for rm, rip in _card_take(d):
                found[rm] = rip
    except OSError:
        pass
    finally:
        if sk is not None:
            try:
                sk.close()
            except OSError:
                pass
    return found


def mode_is_ap():
    """True while this board serves its own BSS on the HaLow radio."""
    cfg = get_config()
    live = live_mode(halow_iface_from_net_state())
    return (live or _MODE_ROLE.get(cfg.get("operation_mode", ""), "")) in (
        "ap", "unified-ap")


def bridge_clients(probe=False):
    br = "br0"
    if not os.path.isdir("/sys/class/net/%s" % br):
        return {"bridge": None, "clients": []}

    local_macs = set()
    for iface in os.listdir("/sys/class/net"):
        try:
            with open("/sys/class/net/%s/address" % iface) as f:
                local_macs.add(f.read().strip().lower())
        except OSError:
            pass

    # Associated stations (and, on a STA board, the AP we joined) have their
    # own table - Connected Stations - and would otherwise appear here too,
    # since the bridge learns their radio MAC like any other. This card is
    # for what sits BEHIND the radio peers, so leave the peers themselves out.
    peer_macs = set()
    ap_if = ap_iface()
    for s in station_list(ap_if):
        peer_macs.add(s.get("mac", "").lower())
    sta_if = station_iface()
    if sta_if != ap_if:
        # a relay's upstream AP is a radio peer too, on the station side
        for s in station_list(sta_if):
            peer_macs.add(s.get("mac", "").lower())

    clients = {}          # mac -> entry
    out = run(["bridge", "fdb", "show", "br", br], timeout=10) or ""
    for line in out.splitlines():
        p = line.split()
        if len(p) < 3 or "permanent" in p:
            continue
        mac = p[0].lower()
        if mac in local_macs or mac in peer_macs \
                or mac.startswith(("01:", "33:33", "ff:")):
            continue      # ourselves / radio peers / multicast groups
        try:
            dev = p[p.index("dev") + 1]
        except (ValueError, IndexError):
            continue
        kind = "halow" if dev.startswith("wlan") else "wired"
        clients[mac] = {"mac": mac, "port": dev, "kind": kind, "ips": []}

    def merge_neigh():
        out = run(["ip", "-4", "neigh", "show", "dev", br], timeout=10) or ""
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 4 and p[1] == "lladdr" and p[-1] != "FAILED":
                mac = p[2].lower()
                if mac in clients and p[0] not in clients[mac]["ips"]:
                    clients[mac]["ips"].append(p[0])

    merge_neigh()

    probed = False
    if probe:
        # the sniffer below only overhears; this asks the boards that run
        # this GUI, which is the only way a bridged AP names itself
        for dmac, dip in discovery_probe().items():
            c = clients.get(dmac)
            if c is not None and dip not in c["ips"]:
                c["ips"].append(dip)
    if probe and any(not c["ips"] for c in clients.values()):
        # Serialise and rate-limit: tcpdump is cheap but not free, and two
        # browser tabs must not stack sniffers on the bridge.
        if _bridge_probe["lock"].acquire(blocking=False):
            try:
                if time.time() - _bridge_probe["last"] > 3:
                    sniffer = shutil.which("tcpdump")
                    tshark = None if sniffer else shutil.which("tshark")
                    if tshark:
                        # Rockchip boards ship tshark but not tcpdump (the
                        # web sniffer installs it); field extraction gives
                        # the same MAC<->IP pairs without regexing text.
                        try:
                            p = subprocess.run(
                                ["timeout", "-s", "INT", "9", tshark, "-i",
                                 br, "-l", "-c", "300", "-a", "duration:8",
                                 "-T", "fields", "-e", "eth.src",
                                 "-e", "arp.src.proto_ipv4", "-e", "ip.src",
                                 "arp or ip"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=12,
                                universal_newlines=True)
                            for line in (p.stdout or "").splitlines():
                                f = (line.split("\t") + ["", ""])[:3]
                                mac, ip = f[0].lower(), f[1] or f[2]
                                c = clients.get(mac)
                                if c is not None and ip \
                                        and ip not in c["ips"]:
                                    c["ips"].append(ip)
                            probed = True
                        except Exception:
                            pass
                        _bridge_probe["last"] = time.time()
                    if sniffer:
                        # -e prints the source MAC, so one overheard ARP maps
                        # a device to its address directly - no ping needed.
                        # The external `timeout` (not check_output's) matters:
                        # tcpdump rarely fills its packet count in 5 s, and a
                        # check_output timeout would discard the partial
                        # output we are actually after.
                        try:
                            p = subprocess.run(
                                ["timeout", "-s", "INT", "8", sniffer, "-i",
                                 br, "-nn", "-e", "-l", "-c", "300",
                                 "arp", "or", "ip"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=11,
                                universal_newlines=True)
                            out = p.stdout or ""
                        except Exception:
                            out = ""
                        # An ARP names its sender ("tell A" / "Reply A
                        # is-at"), and any IPv4 packet names its source; both
                        # lines start with the sender's MAC thanks to -e, so
                        # each one is a finished MAC<->IP pairing.
                        pat = (r"([0-9a-f]{2}(?::[0-9a-f]{2}){5}) > \S+ "
                               r".*?(?:tell ([0-9.]+)|Reply ([0-9.]+) is-at"
                               r"|ethertype IPv4[^:]*: "
                               r"(\d+\.\d+\.\d+\.\d+))")
                        for m in re.finditer(pat, out, re.I):
                            mac = m.group(1).lower()
                            ip = m.group(2) or m.group(3) or m.group(4)
                            c = clients.get(mac)
                            if c is not None and ip and ip not in c["ips"]:
                                c["ips"].append(ip)
                        probed = True
                    _bridge_probe["last"] = time.time()
            finally:
                _bridge_probe["lock"].release()

    # Only what is true now: our own lease while we are the server, else what
    # the kernel resolved. Nothing else - a blank column beats a wrong address.
    subnets = local_subnets()
    # Our own lease file is the one source that cannot be out of date while we
    # are the server: an address it has since handed to another device must
    # not keep showing here from what this board saw earlier.
    leased = station_ips() if we_serve_dhcp() else {}
    for mac, c in clients.items():
        if mac in leased:
            c["ips"] = [leased[mac]]
        c["ips"] = reachable_ips(c["ips"], subnets)

    # Which port faces the LAN at large? The one the default gateway was
    # learned on. Everything there is just "the office network"; the far
    # side - a camera behind the STA, or the STA's devices seen from the
    # AP - is what this table is for, so the GUI collapses the uplink side.
    # The gateway IP is taken from ANY default route: on a lab board a
    # second NIC may hold the route, but the gateway itself is still
    # reachable (and learned) through the bridge.
    uplink = None
    gw_ip = None
    out = run(["ip", "route", "show", "default"], timeout=5) or ""
    # A relay reaches the world through the bridge, while a wired NIC on the
    # bench holds a second default route that is listed first - take the one
    # the bridge itself uses, or the AP side never collapses.
    cands = re.findall(r"default via ([0-9.]+) dev (\S+)", out)
    for gw, dev in cands:
        if dev == br:
            gw_ip = gw
            break
    if gw_ip is None and cands:
        gw_ip = cands[0][0]
    if gw_ip:
        for c in clients.values():
            if gw_ip in c["ips"]:
                uplink = c["port"]
                break
    if uplink is None and get_config().get("operation_mode") in ("sta",
                                                                 "relay"):
        # A station's way out is its own uplink leg, whatever the neighbour
        # table happens to hold: without this the AP side was listed as if it
        # sat behind this board (the gateway's MAC is not resolved until this
        # board sends something).
        leg = sta_status().get("iface", "")
        if leg and os.path.isdir("/sys/class/net/%s/brif/%s" % (br, leg)):
            uplink = leg
    if uplink is None and mode_is_ap() and re.search(r"dev\s+%s\b" % br, out):
        # The wired member is the way out only when the bridge itself carries
        # the default route - the office LAN case, where the gateway's MAC has
        # simply not reached the fdb yet. An AP that keeps its uplink on a
        # port OUTSIDE the bridge (eth0 to the internet, eth1 and the radio
        # serving its own LAN) must not have that LAN collapsed: those are
        # its own devices, which is what this table is for.
        wired = eth_uplink_iface(get_config())
        if wired and os.path.isdir("/sys/class/net/%s/brif/%s" % (br, wired)):
            uplink = wired
    if uplink is None and len(clients) >= 10:
        # No usable route/neigh hint (fresh boot, cold caches): the port
        # carrying the overwhelming majority of MACs is the LAN at large.
        by_port = {}
        for c in clients.values():
            by_port[c["port"]] = by_port.get(c["port"], 0) + 1
        port, n = max(by_port.items(), key=lambda kv: kv[1])
        if n >= 0.8 * len(clients):
            uplink = port

    # A 4addr peer gets its own AP/VLAN netdev, and that netdev is the port a
    # device behind it is learned on - so the port names the station it sits
    # behind. Without this the operator reads "wlan0.sta2" and has to work out
    # which relay that is.
    by_port = {}
    for st in station_list(ap_if) if ap_if else []:
        if st.get("iface") and st["iface"] != ap_if:
            by_port[st["iface"]] = {"mac": st["mac"], "ip": st.get("ip") or ""}
    for c in clients.values():
        v = by_port.get(c["port"])
        if v:
            c["via_mac"], c["via_ip"] = v["mac"], v["ip"]

    rows = sorted(clients.values(),
                  key=lambda c: (c["port"] == uplink, c["kind"], c["mac"]))
    return {"bridge": br, "clients": rows[:100], "total": len(rows),
            "uplink_port": uplink, "gateway_ip": gw_ip, "probed": probed}


def station_list(iface):
    stations = {}
    # A station in 4-address (bridge) mode is moved onto its own AP-VLAN
    # interface, wlan0.sta1, and stops appearing in wlan0's dump - so a
    # bridged STA was invisible to Connected Stations. Dump the children too.
    ifaces = [iface]
    try:
        ifaces += sorted(x for x in os.listdir("/sys/class/net")
                         if x.startswith(iface + "."))
    except OSError:
        pass
    out = "\n".join(run(["iw", "dev", i, "station", "dump"], timeout=10) or ""
                    for i in ifaces)
    mac = None
    for line in out.splitlines():
        # "Station <mac> (on wlan0.sta1)" - keep the interface, it is the
        # key that ties a bridged client (fdb port) to its parent station
        m = re.match(r"Station ([0-9a-f:]{17})(?: \(on (\S+)\))?", line)
        if m:
            mac = m.group(1)
            stations[mac] = {"mac": mac, "iface": m.group(2) or iface}
            continue
        if not mac:
            continue
        for field, key in (("signal:", "rssi"), ("tx bitrate:", "tx_rate"),
                           ("rx bitrate:", "rx_rate"), ("inactive time:", "idle"),
                           ("connected time:", "connected"),
                           # cumulative counters for the Traffic tab's
                           # per-station rates (client computes the deltas)
                           ("tx bytes:", "tx_bytes"), ("rx bytes:", "rx_bytes"),
                           ("tx packets:", "tx_packets"),
                           ("rx packets:", "rx_packets")):
            if line.strip().startswith(field):
                stations[mac][key] = line.split(":", 1)[1].strip()
    ipmap = station_ips()
    # bytes stay strings for the Traffic tab, which has always read them that
    # way; the station table wants numbers
    for st in stations.values():
        for k, n in (("tx_packets", "tx_packets"), ("rx_packets", "rx_packets"),
                     ("tx_bytes", "tx_bytes_n"), ("rx_bytes", "rx_bytes_n")):
            v = st.get(k)
            if v is not None and str(v).isdigit():
                st[n] = int(v)
    # `iw station dump` lists the AP as a station on a station, so the row
    # count says nothing about our role. The firmware's station table exists
    # only on an AP - asking for it elsewhere makes the firmware answer
    # "vif0 is station" on the console, once per poll.
    is_ap = stations and live_mode(iface) == "ap"
    if is_ap:
        # A station that reconnects keeps its MAC, so "new MAC" is not enough
        # to notice a new association - and the capability we hold is the one
        # the previous association asked for. hostapd restarts the connected
        # time, so a drop means re-associated: forget what we knew about it.
        for mac, st in stations.items():
            n = _conn_secs(st.get("connected"))
            was = _sta_conn.get(mac.lower())
            if n is not None and (was is None or n < was):
                _sta_tpc.pop(mac.lower(), None)
            if n is not None:
                _sta_conn[mac.lower()] = n
    ap_vif = vif_of(iface)
    fw_rates = fw_sta_rates(ap_vif) if is_ap else {}
    fw_tpc = fw_sta_tpc(stations, ap_vif) if is_ap else {}

    for mac, st in stations.items():
        st["ip"] = ipmap.get(mac.lower(), "") or discovered_ip(mac)
        r = fw_rates.get(mac.lower())
        if r:
            # the direction we hear
            # too, so the map colours both halves by the same measure
            st["mcs"] = r["rx_mcs"]
            st["tx_mcs"], st["rx_mcs"] = r["tx_mcs"], r["rx_mcs"]
            st["tx_phy_mbps"], st["rx_phy_mbps"] = r["tx_rate"], r["rx_rate"]
        if mac.lower() in fw_tpc:
            st["tpc"] = fw_tpc[mac.lower()]
    return list(stations.values())


def sta_status():
    """Station link status: wpa_supplicant state + RSSI from iw.

    Also serves relay, where the station leg is the second netdev rather
    than the HaLow interface the AP side reports.
    """
    iface = station_iface()
    st = {"iface": iface, "state": "UNKNOWN", "mac": read_mac(iface)}
    cli = None
    for c in ("/usr/sbin/wpa_cli", "/sbin/wpa_cli", "wpa_cli"):
        if c.startswith("/") and not os.path.exists(c):
            continue
        cli = c
        break
    out = run([cli, "-i", iface, "status"], timeout=5) if cli else None
    if out:
        kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        st.update({
            "state": kv.get("wpa_state", "UNKNOWN"),
            "ssid": kv.get("ssid", ""),
            "bssid": kv.get("bssid", ""),
            "freq": kv.get("freq", ""),
            "key_mgmt": kv.get("key_mgmt", ""),
            # in bridge mode the address moves from wlan0 to br0
            "ip": kv.get("ip_address", "") or iface_ipv4(iface)
                  or iface_ipv4("br0"),
        })
    cv = channel_view(get_config().get("country", "US"),
                      nons1g_freq=st.get("freq") or None)
    st["channel"] = cv["channel"]
    st["s1g_channel"] = cv["s1g_channel"]
    st["freq_mhz"] = cv["freq_mhz"]
    link = run(["iw", "dev", iface, "link"], timeout=5) or ""
    m = re.search(r"signal:\s*(-?\d+)\s*dBm", link)
    if m:
        st["rssi"] = m.group(1) + " dBm"
    m = re.search(r"[rt]x bitrate:\s*([\d.]+ [\w/]+)", link)
    if m:
        st["bitrate"] = m.group(1)
    # Read on this board only - the daemon never puts a frame on the air to
    # find the AP, because that would wake every sleeping station.
    devs = [iface]
    if os.path.isdir("/sys/class/net/br0/brif/%s" % iface):
        devs.append("br0")
    # The gateway is the way out, not the AP: a bridged AP passes on whatever
    # router its own LAN has.
    for dev in devs:
        m = re.search(r"default via ([0-9.]+)",
                      run(["ip", "route", "show", "default", "dev", dev],
                          timeout=5) or "")
        if m:
            st["ap_gw"] = m.group(1)
            break
    # Only the BSSID identifies the AP, so take the address the ARP table ties
    # to it. A bridged AP answers from br0 under another MAC and stays blank.
    if st.get("bssid"):
        for dev in devs:
            for line in (run(["ip", "-4", "neigh", "show", "dev", dev],
                             timeout=5) or "").splitlines():
                f = line.split()
                if len(f) >= 4 and f[1] == "lladdr" \
                        and f[2].lower() == st["bssid"].lower() \
                        and f[-1] != "FAILED":
                    st["ap_ip"] = f[0]
                    break
            if st.get("ap_ip"):
                break
    # a bridged AP answers ARP under its bridge MAC, so the pairing can only
    # come from the AP naming its own radio MAC - which a probe asks for
    if st.get("bssid") and not st.get("ap_ip"):
        st["ap_ip"] = discovered_ip(st["bssid"])
    st["tpc"] = tpc_state()

    # The AP row wants what the AP's own station table shows: counters and
    # times from `iw station dump` (which lists the AP when we are the
    # station) and the modulation from the firmware.
    for line in (run(["iw", "dev", iface, "station", "dump"], timeout=5)
                 or "").splitlines():
        s = line.strip()
        for field, key in (("inactive time:", "idle"),
                           ("connected time:", "connected"),
                           ("tx bytes:", "tx_bytes"), ("rx bytes:", "rx_bytes"),
                           ("tx packets:", "tx_packets"),
                           ("rx packets:", "rx_packets")):
            if s.startswith(field):
                st[key] = s.split(":", 1)[1].strip()
    for k in ("tx_bytes", "rx_bytes", "tx_packets", "rx_packets"):
        v = st.get(k)
        if v is not None and str(v).isdigit():
            st[k + "_n"] = int(v)
    # Only ask the firmware once the link is up. `show ap <vif>` fails while
    # the station has no association, and the firmware prints "ERROR: ap" on
    # its console for every poll - which is what filled the log of a relay
    # waiting for its AP to come back.
    bssid = str(st.get("bssid") or "")
    linked = st.get("state") == "COMPLETED" and bssid.strip("0:") != ""
    if live_mode(iface) == "sta" and linked:
        vif = vif_of(iface)
        link = fw_ap_link(vif)
        if link:
            st.update({"ap_bw": link["bw"],
                       "tx_mcs": link["tx_mcs"], "rx_mcs": link["rx_mcs"],
                       "tx_mbps": link["tx_rate"], "rx_mbps": link["rx_rate"]})
        # fw_ap_link corrects the role map when it lands on the wrong vif.
        # `show uinfo` does not refuse by role - it would answer with this
        # board's own BSS - so re-derive the vif after that correction.
        ap_tpc = fw_ap_tpc(st.get("bssid"), vif_of(iface),
                           _conn_secs(st.get("connected")))
        if ap_tpc is not None:
            st["ap_tpc"] = ap_tpc
    return st


def mesh_status():
    """11s mesh view for the network map, from userspace only.

    `station dump` gives the direct peers (the ones we actually hear); `mpath
    dump` adds destinations reachable through them. A node beyond the first
    hop is only ever a MAC in the path table - there is no way to learn its
    address from here, so the map draws it without one. batman-adv meshes
    would need batctl and are not covered.
    """
    iface = halow_iface_from_net_state()
    ips = station_ips()
    peers = []
    for st in station_list(iface):
        peers.append({"mac": st["mac"], "ip": st.get("ip", ""),
                      "rssi": st.get("rssi", "")})
    direct = {p["mac"].lower() for p in peers}
    paths = []
    out = run(["iw", "dev", iface, "mpath", "dump"], timeout=10) or ""
    for line in out.splitlines():
        m = re.match(r"\s*([0-9a-fA-F:]{17})\s+([0-9a-fA-F:]{17})", line)
        if not m:
            continue
        dest, nexthop = m.group(1).lower(), m.group(2).lower()
        if dest in direct:
            continue                      # already drawn as a direct peer
        paths.append({"dest": dest, "next_hop": nexthop,
                      "ip": ips.get(dest, "")})
    return {"iface": iface, "mac": read_mac(iface), "ip": iface_ipv4(iface),
            "peers": peers, "paths": paths}


def read_mac(iface):
    try:
        with open("/sys/class/net/%s/address" % iface) as f:
            return f.read().strip()
    except OSError:
        return ""


def iface_ipv4(iface):
    out = run(["ip", "-4", "-o", "addr", "show", "dev", iface], timeout=5)
    if out:
        m = re.search(r"inet ([0-9.]+)/", out)
        if m:
            return m.group(1)
    return ""


def dhcp_role():
    """Who addresses this segment, read from what is running.

    The setting says what was asked for; this says what the board is doing,
    which is what an operator switching between server and client wants to
    see. Read live: a bridged run can take its lease through
    bridge_ip_move.sh, which leaves no entry in the network plan.
    """
    dev = "br0" if os.path.isdir("/sys/class/net/br0") else halow_iface_from_net_state()
    # our own dnsmasq, and the pool it serves
    if run(["pgrep", "-f", "dnsmasq.*/run/halow/dnsmasq.conf"], timeout=5):
        rng = iface = ""
        try:
            for line in open("/run/halow/dnsmasq.conf"):
                if line.startswith("dhcp-range="):
                    p = line.split("=", 1)[1].split(",")
                    rng = "%s-%s" % (p[0].strip(), p[1].strip())
                elif line.startswith("interface="):
                    iface = line.split("=", 1)[1].strip()
        except OSError:
            pass
        return {"role": "server", "iface": iface or dev, "range": rng}
    addr = run(["ip", "-4", "-o", "addr", "show", "dev", dev], timeout=5) or ""
    if "dynamic" in addr:
        return {"role": "client", "iface": dev, "range": ""}
    m = re.search(r"inet ([0-9.]+/\d+)", addr)
    if m:
        return {"role": "static", "iface": dev, "range": m.group(1)}
    return {"role": "", "iface": dev, "range": ""}


def bridge_status(cfg):
    st = {"mode": cfg.get("bridge", "off"), "dhcp": dhcp_role(),
          "ip_mode": cfg.get("bridge_ip_mode", "")}
    br = "br0"
    br_sys = "/sys/class/net/%s" % br
    st["br0_present"] = os.path.isdir(br_sys)
    if st["br0_present"]:
        st["br0_ip"] = iface_ipv4(br)
        try:
            st["members"] = sorted(os.listdir(os.path.join(br_sys, "brif")))
        except OSError:
            st["members"] = []
        # Air guard: without these, upstream-LAN chatter floods the HaLow
        # air (~100 frames/s observed on a corporate LAN). flood_off = the
        # bridge-core unknown-unicast/multicast flag on every wlan* port.
        flags = []
        for m in st["members"]:
            if not m.startswith("wlan"):
                continue
            try:
                with open(os.path.join(br_sys, "brif", m,
                                       "unicast_flood")) as f:
                    flags.append(f.read().strip())
            except OSError:
                pass
        st["flood_off"] = bool(flags) and all(v == "0" for v in flags)
        # Lets the GUI tell an intentional off (settings) from a guard that
        # failed to arm - only the latter deserves a warning.
        st["guard_cfg"] = str(cfg.get("bridge_flood_guard", "off"))
    return st


# ------------------------------------------------------------ ethernet uplink
# The wired management port (eth0 on RPi, end0 on Rockchip) is owned by the
# host OS, not by the HaLow stack; its IPv4 comes from NetworkManager on both
# reference boards (dhcpcd fallback for images without NM). The GUI only
# rewrites that owner's configuration - it never ip-addr-flushes by hand -
# so a later `nmcli`/`dhcpcd` inspection shows exactly what the GUI set.

def wired_ports():
    """Wired netdevs this board has. A two-port board is why the setting
    exists: only the operator knows which one the HaLow network belongs to."""
    try:
        return [d for d in sorted(os.listdir("/sys/class/net"))
                if d.startswith(("eth", "end", "enp", "eno", "enx"))
                and not os.path.isdir("/sys/class/net/%s/phy80211" % d)]
    except OSError:
        return []


def eth_uplink_iface(cfg):
    """The wired management interface. bridge_eth_iface set to an explicit
    name wins; 'auto' = the default-route device, else the first eth-like."""
    name = str(cfg.get("bridge_eth_iface", "auto")).strip()
    if name and name != "auto":
        return name
    out = run(["ip", "-4", "route", "show", "default"], timeout=5) or ""
    for line in out.splitlines():
        m = re.search(r"\bdev\s+(\S+)", line)
        if m and not m.group(1).startswith(("wlan", "br", "docker", "veth")):
            return m.group(1)
    try:
        for dev in sorted(os.listdir("/sys/class/net")):
            if dev.startswith(("eth", "end", "enp", "eno")):
                return dev
    except OSError:
        pass
    return ""


def eth_nm_connection(iface):
    """Name of the NetworkManager connection active on iface, or None."""
    if unit_active("NetworkManager") != "active":
        return None
    out = run(["nmcli", "-t", "-f", "GENERAL.CONNECTION", "dev", "show",
               iface], timeout=10)
    if not out:
        return None
    line = out.strip().splitlines()[0]
    name = line.split(":", 1)[1] if ":" in line else ""
    return name or None


def eth_status(cfg):
    iface = eth_uplink_iface(cfg)
    return {"iface": iface,
            "ip": iface_ipv4(iface) if iface else "",
            "mode": cfg.get("eth_ip_mode", "dhcp")}


DHCPCD_CONF = "/etc/dhcpcd.conf"
ETH_MARK_BEGIN = "# NRC eth_config begin"
ETH_MARK_END = "# NRC eth_config end"
# An earlier build wrote the markers with a space ("NRC eth config"). Match
# both when removing, or a board that once had a static wired address keeps
# it after switching back to DHCP - and a second block is appended on top.
_ETH_MARK_RE = re.compile(r"^#\s*NRC eth[_ ]config (begin|end)\s*$", re.I)


def _static_halow_ifaces():
    """HaLow netdevs the stack addressed itself (AP, relay AP leg, mesh).

    Read from the network plan start.py writes; a station leg is absent from
    it because it runs a DHCP client, and that lease must not be released.
    """
    try:
        plan = load_json("/run/halow/net.json", {})
        return [e.get("iface") for e in plan.get("static", [])
                if e.get("iface") in halow_netdevs()]
    except Exception:
        return []


def _dhcpcd_write_eth_block(iface, cfg):
    """Replace our marker block in dhcpcd.conf (fallback path, no NM)."""
    try:
        with open(DHCPCD_CONF) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    out, skip = [], False
    for ln in lines:
        m = _ETH_MARK_RE.match(ln.strip())
        if m:
            skip = m.group(1).lower() == "begin"
            continue
        if not skip:
            out.append(ln)
    if cfg.get("eth_ip_mode") == "static":
        blk = [ETH_MARK_BEGIN, "interface " + iface,
               "static ip_address=" + cfg["eth_static_ip"]]
        if str(cfg.get("eth_gateway", "")).strip():
            blk.append("static routers=" + cfg["eth_gateway"].strip())
        if str(cfg.get("eth_dns", "")).strip():
            blk.append("static domain_name_servers="
                       + re.sub(r"[,\s]+", " ", cfg["eth_dns"].strip()))
        blk.append(ETH_MARK_END)
        out += blk
    tmp = DHCPCD_CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, DHCPCD_CONF)


def apply_autostart(cfg):
    """Enable or disable halow.target, which is what starts the radio at boot.

    The members keep their WantedBy=halow.target links either way, so the
    stack can still be started and stopped by hand or from the dashboard -
    this only decides whether systemd pulls it in at power-on. halow-webd is
    WantedBy=multi-user.target and is untouched, or turning the radio off
    would take away the page that turns it back on.
    """
    want = str(cfg.get("radio_autostart", "off")).lower() == "on"
    verb = "enable" if want else "disable"
    r = subprocess.run(["systemctl", verb, "halow.target"],
                       capture_output=True, universal_newlines=True,
                       timeout=15)
    if r.returncode != 0:
        return "autostart: systemctl %s halow.target failed (%s)" % (
            verb, (r.stderr or "").strip().splitlines()[-1:] or "no detail")
    return "autostart: radio %s at boot" % ("starts" if want else "stays down")


def reconcile_autostart():
    """Make the boot-time link agree with config.json at daemon start.

    A board upgraded from a package that always enabled halow.target would
    otherwise keep starting the radio while the page reads off.
    """
    try:
        cfg = get_config()
    except Exception:
        return
    want = str(cfg.get("radio_autostart", "off")).lower() == "on"
    r = subprocess.run(["systemctl", "is-enabled", "halow.target"],
                       capture_output=True, universal_newlines=True,
                       timeout=5)
    now = (r.stdout or "").strip()
    if (now == "enabled") == want:
        return
    print("[halow-webd] %s halow.target to match radio_autostart=%s"
          % ("enabling" if want else "disabling", "on" if want else "off"),
          flush=True)
    apply_autostart(cfg)


def apply_eth_config(cfg):
    """Reconfigure the wired uplink's IPv4. Deferred to a thread: the change
    can drop the very TCP connection this HTTP response rides on, so the
    reply must leave first. NetworkManager path (both reference boards) or
    dhcpcd marker block as fallback."""
    iface = eth_uplink_iface(cfg)
    if not iface:
        return "eth: no wired interface found - nothing changed"
    if cfg.get("bridge") == "on":
        return ("eth: bridge mode owns the wired port (br0) - use "
                "'Bridge IP mode' instead; nothing changed")
    mode = cfg.get("eth_ip_mode", "dhcp")
    if mode == "static" and not str(cfg.get("eth_static_ip", "")).strip():
        return "eth: static mode needs an address - nothing changed"
    con = eth_nm_connection(iface)
    if con is None and not os.path.exists(DHCPCD_CONF):
        return "eth: neither NetworkManager nor dhcpcd manages %s" % iface

    def worker():
        time.sleep(1.5)  # let the HTTP response drain
        try:
            if con:
                if mode == "static":
                    dns = re.sub(r"[,\s]+", " ",
                                 str(cfg.get("eth_dns", "")).strip())
                    subprocess.run(
                        ["nmcli", "con", "mod", con,
                         "ipv4.method", "manual",
                         "ipv4.addresses", cfg["eth_static_ip"].strip(),
                         "ipv4.gateway",
                         str(cfg.get("eth_gateway", "")).strip(),
                         "ipv4.dns", dns],
                        timeout=30, check=True, capture_output=True)
                else:
                    subprocess.run(
                        ["nmcli", "con", "mod", con,
                         "ipv4.method", "auto", "ipv4.addresses", "",
                         "ipv4.gateway", "", "ipv4.dns", ""],
                        timeout=30, check=True, capture_output=True)
                subprocess.run(["nmcli", "con", "up", con, "ifname", iface],
                               timeout=90, capture_output=True)
            else:
                _dhcpcd_write_eth_block(iface, cfg)
                subprocess.run(["systemctl", "restart", "dhcpcd"],
                               timeout=60, capture_output=True)
                # dhcpcd claims every interface it is not told to leave
                # alone. On a restart it would take the HaLow netdev the
                # stack gave a fixed address - a link-local address and a
                # default route through the radio - so hand those back.
                for hw in _static_halow_ifaces():
                    subprocess.run(["dhcpcd", "-k", hw],
                                   timeout=20, capture_output=True)
            _apply_state["last_result"] = "eth %s applied on %s" % (mode, iface)
        except Exception as e:
            _apply_state["last_result"] = "eth reconfigure failed: %s" % e
        finally:
            _apply_state["ts"] = int(time.time())

    threading.Thread(target=worker, daemon=True).start()
    tgt = cfg.get("eth_static_ip", "").strip() if mode == "static" else "DHCP"
    return ("eth: switching %s to %s in ~2 s - if the address changes, "
            "reconnect the browser/ssh to the new one" % (iface, tgt))



# Keys shown even when the installed variant's template omits them, because
# the app supports them regardless (the generic config.json ships without a
# `security` block, but app-test.cpp still parses and applies it). A bool
# always-field defaults to off when absent.
ALWAYS_FIELDS = {"security", "tack_sync"}


def _nested_get(d, dotted):
    """Follow a dotted key into nested dicts. Returns (found, value)."""
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return (False, None)
        cur = cur[part]
    return (True, cur)


def _nested_set(d, dotted, value):
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _nested_pop(d, dotted):
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        cur = nxt
    cur.pop(parts[-1], None)




















_STA_GROUP_RE = re.compile(
    r"^Group\[(\d+)\]\s*(<-- Current)?\s*\(Stations:\s*(\d+)\)")
# ID: 0x00000001, 12:34:56:78:00:00, SG: 0, Ver: 0x02, cnt: 1, rc_mcs: 7,
#   rc_success: 10, rc_failure: 1, nb: 0, OK, 2026-08-11 12:00:00
_STA_ID_RE = re.compile(
    r"^\s*ID:\s*0x([0-9A-Fa-f]+),\s*"
    r"([0-9A-Fa-f:]{17}),\s*"
    r"SG:\s*(\d+),\s*Ver:\s*0x([0-9A-Fa-f]+),\s*cnt:\s*(\d+),\s*"
    r"rc_mcs:\s*(\d+),\s*rc_success:\s*(\d+),\s*rc_failure:\s*(\d+),\s*"
    r"nb:\s*(-?\d+),\s*([A-Z_]+),\s*(.+?)\s*$")


def parse_sta_list(text):
    """Parse the last `sta list` block into groups. print_stations wraps its
    footer in a banner:
        ========================================
        Total Stations: N
        ========================================
    The app has a concurrent event thread that logs to the same stdout, so
    the block's lines can be INTERLEAVED with async output (NEXT_GROUP, poll
    timeouts) line by line. So we cannot assume contiguity: we anchor on the
    banner-wrapped footer (not a bare 'Total Stations:' — the app also logs
    that periodically), find the Group[0] that starts this print, then scan
    forward to the footer picking out Group[..] and ID lines and ignoring
    everything interleaved between them."""
    lines = (text or "").splitlines()

    def prev_nonblank(i):
        j = i - 1
        while j >= 0 and lines[j].strip() == "":
            j -= 1
        return lines[j] if j >= 0 else ""

    # last banner-wrapped footer
    end = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("Total Stations:") and prev_nonblank(i).startswith("===="):
            end = i
            break
    if end is None:
        for i in range(len(lines) - 1, -1, -1):  # fall back to a bare footer
            if lines[i].startswith("Total Stations:"):
                end = i
                break
    if end is None:
        return None  # no sta-list output found
    total = 0
    m = re.search(r"Total Stations:\s*(\d+)", lines[end])
    if m:
        total = int(m.group(1))
    # start = the Group[0] that begins this print (last one before the footer);
    # sta list always prints groups from 0, so that marks the block's head.
    start = 0
    for i in range(end - 1, -1, -1):
        gm = _STA_GROUP_RE.match(lines[i])
        if gm and int(gm.group(1)) == 0:
            start = i
            break
    groups, cur = [], None
    for line in lines[start:end]:
        gm = _STA_GROUP_RE.match(line)
        if gm:
            cur = {"id": int(gm.group(1)), "current": bool(gm.group(2)),
                   "count": int(gm.group(3)), "stations": []}
            groups.append(cur)
            continue
        im = _STA_ID_RE.match(line)
        if im and cur is not None:
            cur["stations"].append({
                "id": int(im.group(1), 16),
                "id_hex": "0x%08X" % int(im.group(1), 16),
                "mac": im.group(2).upper(),
                "sg": int(im.group(3)),
                "ver": int(im.group(4), 16),
                "cnt": int(im.group(5)),
                "rc_mcs": int(im.group(6)),
                "rc_success": int(im.group(7)),
                "rc_failure": int(im.group(8)),
                "nb": int(im.group(9)),
                "state": im.group(10),
                "time": im.group(11),
            })
    return {"groups": groups, "total": total}












def _safe_name(name):
    base = os.path.basename(name or "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base or "upload.bin"










def _parse_bool(raw):
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "on", "yes")



















# ------------------------------------------------------- CCA channel scanner: an NRC5294R1 test-firmware board on a USB serial port
# adds a scanner tab while it is plugged in. The module watches the ports
# itself; the GUI polls /api/cca and shows the tab only while a board is
# attached. Absent module -> {supported:false} and no tab.
try:
    import halow_cca as _cca
    _cca.configure(get_config=get_config)
except ImportError:
    _cca = None


def _cca_ap_channel():
    """(centre_100khz, bw_mhz) of this AP's own channel from ap.env, or
    (None, None) - what the scanner overlays key on."""
    env = read_kv_file(AP_ENV_FILE)
    f = env.get("HALOW_CHBEACON", "")
    if not f.isdigit():
        return None, None
    b = env.get("HALOW_BANDWIDTH", "")
    bw = {0: 1, 1: 2, 2: 4}.get(int(b) if b.isdigit() else -1, 1)
    return int(f), bw


def fmt_uptime(sec):
    return "%dd %02d:%02d" % (sec // 86400, sec % 86400 // 3600,
                              sec % 3600 // 60)


STACK_ANCHOR_UNIT = "halow-wlan.service"


def stack_uptime(dev_up):
    """Uptime of the HaLow stack, distinct from the device's.

    Both systemd's ActiveEnterTimestampMonotonic and /proc/uptime count from
    boot, so subtracting them needs no clock or timezone reasoning and is
    immune to NTP steps. Returns {} when the unit never came up, so callers
    fall back to showing the device uptime alone rather than a wrong zero.
    """
    out = {}
    props = run(["systemctl", "show", STACK_ANCHOR_UNIT,
                 "-p", "ActiveState",
                 "-p", "ActiveEnterTimestampMonotonic",
                 "-p", "ActiveEnterTimestamp"])
    if not props:
        return out
    mono, human, state = 0, "", ""
    for line in props.splitlines():
        k, _, v = line.partition("=")
        if k == "ActiveEnterTimestampMonotonic":
            try:
                mono = int(v)
            except ValueError:
                mono = 0
        elif k == "ActiveEnterTimestamp":
            human = v.strip()
        elif k == "ActiveState":
            state = v.strip()
    if state:
        out["stack_state"] = state
    # systemd keeps the last activation timestamp on a unit that has since
    # stopped (and while it is restarting), so the timestamp alone would
    # report an uptime for a stack that is not up. Only claim one while the
    # unit is actually active.
    if state != "active":
        return out
    if mono <= 0:
        return out          # never activated in this boot
    up = dev_up - mono / 1e6
    if up < 0:
        return out          # clock/unit mismatch: say nothing rather than lie
    out["stack_uptime"] = fmt_uptime(up)
    out["stack_since"] = human
    return out


def system_status():
    st = {}
    try:
        st["load"] = open("/proc/loadavg").read().split()[0]
        up = float(open("/proc/uptime").read().split()[0])
        st["uptime"] = fmt_uptime(up)
        # How long the HaLow stack itself has been up, which is the number an
        # AP operator actually wants: after a watchdog recovery the box keeps
        # counting for days while the radio restarted minutes ago. Anchored on
        # halow-wlan - it loads the driver and downloads firmware, so it is
        # the first thing to come back in every mode.
        st.update(stack_uptime(up))
        mem = read_kv_file_proc("/proc/meminfo")
        st["mem_free_mb"] = int(mem.get("MemAvailable", 0)) // 1024
    except Exception:
        pass
    for tz in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            st["temp_c"] = round(int(open(tz).read().strip()) / 1000.0, 1)
        except Exception:
            pass
    # A full rootfs breaks the next install (driver builds need a few hundred
    # MB) long before it breaks anything else, so surface it early.
    try:
        s = os.statvfs("/")
        total = s.f_blocks * s.f_frsize
        free = s.f_bavail * s.f_frsize
        st["disk_free_mb"] = free // (1024 * 1024)
        st["disk_used_pct"] = round(100.0 * (total - free) / total, 1) if total else None
    except Exception:
        pass
    return st


# ------------------------------------------------- what is actually running
_ident_cache = {"ts": 0.0, "val": None}


def _md5(path):
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def identity_status():
    """Package / firmware / driver identity: does what is loaded match what
    this package installed? Both mismatches bit us in the field - a stale
    firmware image kept running after an install, and an upgrade left the
    previous package's modules loaded because the units were never restarted.
    """
    now = time.time()
    if _ident_cache["val"] is not None and now - _ident_cache["ts"] < 30:
        return _ident_cache["val"]

    svc = read_kv_file(SERVICE_CONF)
    sdir = svc.get("NRC_SCRIPT_DIR", "")
    pkg = os.path.dirname(sdir.rstrip("/")) if sdir else ""
    out = {"pkg_dir": pkg, "build_type": svc.get("BUILD_TYPE", "release"),
           "driver_stack": svc.get("DRIVER_STACK", "modular")}

    ver = {}
    try:
        with open(os.path.join(pkg, "VERSION-SDK")) as f:
            for line in f:
                p = line.split()
                if len(p) >= 2:
                    ver[p[0]] = p[1]
    except Exception:
        pass
    if ver:
        out["version"] = "%s.%s.%s" % (ver.get("VERSION_MAJOR", "?"),
                                       ver.get("VERSION_MINOR", "?"),
                                       ver.get("VERSION_REVISION", "?"))

    # firmware: which package image does /lib/firmware/uni_s1g.bin come from?
    fwdir = os.path.join(pkg, "sw", "firmware")
    live = _md5("/lib/firmware/uni_s1g.bin")
    out["fw"] = {"live_md5": live[:12], "image": "", "matches": None}
    if live:
        for name in ("nrc7394_cspi_mcp.bin", "nrc7394_cspi.bin"):
            if _md5(os.path.join(fwdir, name)) == live:
                out["fw"]["image"] = name
                break
        out["fw"]["matches"] = bool(out["fw"]["image"])
        out["fw"]["variant"] = ("mcp" if out["fw"]["image"].endswith("_mcp.bin")
                                else ("plain" if out["fw"]["image"] else ""))

    # drivers: loaded modules vs the .ko files this package installed
    mods, mismatch = [], False
    drvdir = os.path.join(pkg, "sw", "driver",
                          "nrc_modular" if out["driver_stack"] == "modular" else "nrc")
    names = (["nrc_spi", "nrc_core", "nrc_wlan", "nrc-mcp"]
             if out["driver_stack"] == "modular" else ["nrc"])
    for n in names:
        sysname = n.replace("-", "_")
        # loaded = the module directory exists. srcversion is only there for
        # modules carrying MODULE_VERSION() unless the kernel sets
        # CONFIG_MODULE_SRCVERSION_ALL (Raspberry Pi OS does, the Rockchip
        # 6.1 kernel does not), so its absence must not read as "not loaded".
        loaded = os.path.isdir("/sys/module/%s" % sysname)
        live_sv = ""
        try:
            with open("/sys/module/%s/srcversion" % sysname) as f:
                live_sv = f.read().strip()
        except Exception:
            pass
        pkg_sv = (run(["modinfo", "-F", "srcversion",
                       os.path.join(drvdir, n + ".ko")], timeout=5) or "").strip()
        # verify only when both sides actually have a srcversion
        verifiable = bool(live_sv) and bool(pkg_sv)
        ok = loaded and (not verifiable or live_sv == pkg_sv)
        if verifiable and live_sv != pkg_sv:
            mismatch = True
        mods.append({"name": n, "loaded": loaded, "matches": ok})
    out["driver"] = {"dir": drvdir, "modules": mods, "mismatch": mismatch,
                     "loaded": sum(1 for m in mods if m["loaded"]), "total": len(mods)}

    _ident_cache.update(ts=now, val=out)
    return out


def reboot_required():
    """select-dtoverlay.sh drops this marker when it repoints config.txt; /run
    means it clears itself on the reboot it is asking for."""
    return os.path.exists("/run/halow/reboot-required")


def read_kv_file_proc(path):
    kv = {}
    try:
        with open(path) as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    kv[k.strip()] = int(v.strip().split()[0])
    except Exception:
        pass
    return kv


# operation_mode values that put the radio in one role. relay runs two at
# once, so iw cannot tell its legs apart and it is left out.
_MODE_ROLE = {"unified-ap": "ap", "ap": "ap", "sta": "sta",
              "sniffer": "sniffer"}


def live_mode(iface):
    """The role the radio is actually in, or None when it cannot be told.

    The GUI's mode comes from config.json, which only this daemon writes. A
    manual start.py run changes the radio without touching that file, and the
    page then describes a stack that is not running."""
    info = run(["iw", "dev", iface, "info"], timeout=5) or ""
    m = re.search(r"^\s*type\s+(\S+)", info, re.M)
    if not m:
        return None
    return {"AP": "ap", "managed": "sta", "mesh": "mesh",
            "monitor": "sniffer"}.get(m.group(1))


_apenv_refresh = {"ts": 0, "busy": False}


def refresh_ap_env_async(cooldown=30):
    """Rewrite /run/halow/ap.env from the live radio, in the background.

    ap.env is published once at start-up. A switch the GUI did not make -
    the channel manager's, or a hand-made hostapd_cli chan_switch - leaves
    every reader of it on the old channel: the dashboard card, the CCA
    overlay.
    """
    now = time.time()
    if _apenv_refresh["busy"] or now - _apenv_refresh["ts"] < cooldown:
        return
    svc = read_kv_file(SERVICE_CONF)
    sdir = svc.get("NRC_SCRIPT_DIR", "")
    gen = os.path.join(sdir, "ap_env.py")
    if not sdir or not os.path.exists(gen):
        return
    _apenv_refresh.update({"busy": True, "ts": now})

    def work():
        try:
            subprocess.run(["python3", gen, "--country",
                            svc.get("COUNTRY", "US"), "--iface", ap_iface()],
                           timeout=60, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            pass
        finally:
            _apenv_refresh["busy"] = False

    threading.Thread(target=work, daemon=True).start()


def ui_version():
    """Build stamp of the page this daemon serves.

    The dashboard is a single page that never re-fetches itself, so a
    deployed UI only reaches an open tab when the tab reloads - it compares
    this against the one it started with.
    """
    try:
        st = os.stat(os.path.join(STATIC_DIR, "index.html"))
        return "%d-%d" % (int(st.st_mtime), st.st_size)
    except OSError:
        return ""


def full_status():
    ap_env = read_kv_file(AP_ENV_FILE)
    # the AP leg as it is now, not the name the last AP start recorded: a
    # relay serves its BSS on one netdev and joins upstream on the other
    iface = ap_iface()
    cfg = get_config()
    mode = cfg.get("operation_mode", "ap")
    # a manual run leaves config.json behind: follow the radio, not the file
    live = live_mode(halow_iface_from_net_state())
    manual = bool(live and mode != "relay" and _MODE_ROLE.get(mode) != live)
    if manual:
        mode = live
    # asked after the mode is known: a station and a sniffer run no hostapd,
    # and the call only failed against a socket that is not there
    # everything live below is read only while the radio is up: a stopped
    # stack must not keep showing the stations and peers of the last run
    up = stack_running()
    hs = hostapd_status(iface) if up and mode not in ("sta", "sniffer") else {}
    # One source decides both numbers, or the card pairs a fresh S1G channel
    # with the mac80211 channel of the old one. hostapd's own STATUS is live
    # after any switch - the channel manager's, the GUI's or a hand-made
    # chan_switch; ap.env only holds what the last start wrote.
    cm = _chmgr_cache["data"] or {}
    cm_live = str(cm.get("state", "")) == "running"
    cm_freq = cm.get("cur") or cm.get("s1g")
    country = cfg.get("country", "US")
    if str(hs.get("channel", "")).isdigit():
        fw_ch = channel_view(country, mac_ch=hs["channel"])
        if hs["channel"] != ap_env.get("HALOW_CHANNEL"):
            refresh_ap_env_async()      # the radio moved without the GUI
    elif cm_live and isinstance(cm_freq, int):
        fw_ch = channel_view(country, s1g_freq=cm_freq / 10.0)
    else:
        fw_ch = channel_view(
            country, mac_ch=ap_env.get("HALOW_CHANNEL"),
            s1g_freq=(int(ap_env["HALOW_CHBEACON"]) / 10.0
                      if ap_env.get("HALOW_CHBEACON", "").isdigit()
                      else None))
    return {
        "mode": mode,
        "mode_saved": cfg.get("operation_mode", "ap"),
        "mode_manual": manual,
        # a restored session never passes through the login reply, so the page
        # learns here that every setting but the password would be refused
        "must_change": needs_pw_change(cfg),
        # relays associated to us, so the settings page can grey out what the
        # daemon would refuse anyway (the channel manager)
        "relay_peers": wds_peers(iface) if up else [],
        # relay runs a STA link upstream as well as its own AP, and the map
        # needs both ends
        "sta": sta_status() if up and mode in ("sta", "relay") else None,
        "sniffer": sniffer_status() if mode == "sniffer" else None,
        "bridge": bridge_status(cfg),
        "eth": eth_status(cfg),
        "ap": {
            # a manual STA run leaves the last AP's ap.env in place; stop.py
            # does not clear it, so ready must follow the live role
            "ready": up and ap_env.get("HALOW_READY") == "1" and mode != "sta",
            "state": hs.get("state") or ("UNKNOWN" if up else "STOPPED"),
            "ssid": hs.get("ssid[0]", ""),
            "iface": iface,
            "mac": ap_env.get("HALOW_MAC", ""),
            # the S1G channel is what the card shows; the mac80211 number
            # stays beside it, since hostapd, iw and the conf files use it
            "channel": fw_ch.get("channel") or ap_env.get("HALOW_CHANNEL", ""),
            "s1g_channel": fw_ch.get("s1g_channel"),
            "freq_mhz": fw_ch.get("freq_mhz"),
            "bandwidth_mhz": fw_ch.get("bw_mhz") or {0: 1, 1: 2, 2: 4}.get(
                int(ap_env["HALOW_BANDWIDTH"])
                if ap_env.get("HALOW_BANDWIDTH", "").isdigit() else -1),
            "country": ap_env.get("HALOW_COUNTRY", ""),
            "num_sta": int(hs.get("num_sta[0]", 0) or 0),
            # what the radio side is actually addressed as, read live: br0
            # when this leg is bridged into it, the HaLow netdev otherwise.
            # ap.env only holds what the run that wrote it saw, so it is the
            # last resort - a leftover br0 must not speak for an unbridged run
            "ip": (iface_ipv4("br0")
                   if os.path.isdir("/sys/class/net/br0/brif/%s" % iface)
                   else iface_ipv4(iface)) or ap_env.get("HALOW_AP_IP", ""),
            "mgmt_ip": ap_env.get("HALOW_IP", ""),
            "tpc": tpc_state() if mode != "sta" else {},
        },
        # hostapd: the modular stack (unified-ap) supervises it as a systemd
        # unit; the plain stack's start.py runs it as its own child, where
        # halow-hostapd is rightly inactive - report the process there so a
        # healthy plain AP does not read as a fault. Modes without an AP side
        # (sta/sniffer/mesh) get no hostapd row at all.
        "services": dict(
            [("halow-wlan", unit_active("halow-wlan.service"))] +
            (([("halow-hostapd", unit_active("halow-hostapd.service"))]
              if unit_exists("halow-hostapd.service") else [])
             if mode == "unified-ap" else
             ([("hostapd", "active"
                if run(["pgrep", "-x", "hostapd"], timeout=5) else "inactive")]
              if mode in ("ap", "relay") else []))),
        "system": system_status(),
        "identity": identity_status(),
        "reboot_required": reboot_required(),
        "radio": radio_live(cfg, iface),
        # None whenever the manager is configured off, which is what keeps the
        # panel off a board that picks its channel by hand
        # The panel keeps this fresh on its own timer; here it is only a
        # summary card. Reading it every poll costs a full second - two
        # firmware round trips, and cli_run holds the lock for
        # CLI_MIN_INTERVAL between them - which is what kept the 1 s
        # dashboard interval from ever being 1 s.
        "chmgr": chmgr_status_cached(cfg, CHMGR_SUMMARY_TTL)
                 if mode in ("unified-ap", "ap") else None,
        "apply": dict(_apply_state),
        "ui_version": ui_version(),
        # "auto" means the channel manager picks; with the manager off nobody
        # does, hostapd is handed a conf with no channel and exits at once
        "channel_unset": (mode in ("unified-ap", "ap")
                          and str(cfg.get("channel", "")).strip().lower()
                          == "auto"
                          and str(cfg.get("channel_manager", "off"))
                          in ("off", "monitor")),
    }


# ---------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    server_version = "halow-webd/1.0"

    # --- plumbing
    def _send(self, code, obj, ctype="application/json", raw=None,
              set_cookie=None, headers=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            # largest legitimate body: an 8 MB FOTA bin as base64 (~11 MB)
            if n > 16 * 1024 * 1024:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _token(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return c["halow_session"].value if "halow_session" in c else None

    def _authed(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["halow_session"].value if "halow_session" in c else None
        exp = _sessions.get(tok)
        if exp and exp > time.time():
            _sessions[tok] = time.time() + SESSION_TTL
            return True
        return False

    def log_message(self, fmt, *args):
        sys.stderr.write("[apd] %s - %s\n" % (self.address_string(),
                                              fmt % args))

    # --- CCA scanner (USB NRC5294R1 test board)
    def _cca_get(self, path):
        if _cca is None:
            return self._send(200, {"supported": False})
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        q = {}
        for kv in qs.split("&"):
            k, _, v = kv.partition("=")
            if k:
                q[k] = v
        if path == "/api/cca":
            return self._send(200, _cca.snapshot())
        if path == "/api/cca/rows":
            try:
                since = int(q.get("since", 0))
            except ValueError:
                since = 0
            return self._send(200, _cca.rows(since))
        if path == "/api/cca/stats":
            try:
                window = max(10, min(3600, int(q.get("window", 300))))
            except ValueError:
                window = 300
            f, bw = _cca_ap_channel()
            return self._send(200, {
                "attached": _cca.attached(), "window": window,
                "ap_idx": _cca.channel_for(f, bw) if f else None,
                "channels": _cca.window_stats(window)})
        if path == "/api/cca/csv":
            try:
                window = max(0, int(q.get("window", 0)))
            except ValueError:
                window = 0
            body = _cca.csv(window).encode()
            fname = time.strftime("cca_log_%Y%m%d_%H%M%S.csv")
            return self._send(200, None, "text/csv; charset=utf-8", raw=body,
                              headers={"Content-Disposition":
                                       'attachment; filename="%s"' % fname})
        return self._send(404, {"error": "not found"})

    # --- routes
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            try:
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, None, "text/html; charset=utf-8",
                                      raw=f.read())
            except OSError:
                return self._send(500, {"error": "ui missing"})
        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._authed():
            # A board still on the factory default has no password worth
            # asking for - it ships in the README. Say that much without a
            # session so the first screen is the set-password form.
            if path == "/api/status" and needs_pw_change(get_config()):
                return self._send(200, {"setup": True, "must_change": True})
            return self._send(401, {"error": "auth required"})
        if path == "/api/support-bundle":
            # Deliberately OUTSIDE the global _lock: every other GET holds it
            # for the whole handler, and a multi-second collection under it
            # froze the entire GUI (and made the 429 below unreachable). The
            # collector takes _lock itself, briefly, for the config reads
            # that need it; single-flight is its own lock.
            if not _bundle_lock.acquire(blocking=False):
                return self._send(429, {"error": "a bundle is already "
                                                 "being generated"})
            try:
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                note = ""
                for kv in qs.split("&"):
                    k, _, v = kv.partition("=")
                    if k == "note":
                        note = urllib.parse.unquote_plus(v)[:500]
                fname, out_path, tmpdir = support_bundle(note)
                with open(out_path, "rb") as f:
                    body = f.read()
                shutil.rmtree(tmpdir, ignore_errors=True)
            finally:
                _bundle_lock.release()
            return self._send(200, None, ctype="application/gzip",
                              raw=body,
                              headers={"Content-Disposition":
                                       'attachment; filename="%s"' % fname})
        if path == "/api/chmgr":
            # Outside the global lock, like the two below: the panel polls it
            # every few seconds and it only reads config and the chip. Its own
            # lock serialises the firmware round trips.
            return self._send(200, {"chmgr": chmgr_only_status()})
        if path.startswith("/api/cca"):
            # Outside the global lock as well: the scanner tab polls this
            # every second and only reads the bridge's ring buffer.
            return self._cca_get(path)
        if path == "/api/fwlog/tail":
            # Outside the global lock, like the bundle above: the log viewer
            # polls this every couple of seconds and every other GET holds
            # _lock for its whole handler, so serialising it here would make
            # the rest of the GUI stutter for as long as the panel is open.
            # It only reads a file, so it needs no shared state.
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            off, gen = None, None
            for kv in qs.split("&"):
                k, _, v = kv.partition("=")
                try:
                    if k == "offset":
                        off = int(v)
                    elif k == "gen":
                        gen = int(v)
                except ValueError:
                    pass
            return self._send(200, fwlog_tail(off, gen))
        if path == "/api/fwlog/files":
            # Outside the lock for the same reason as the tail above: it is
            # part of the log panel and only reads a directory.
            return self._send(200, fwlog_files())
        if path == "/api/fwlog/file":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            name = ""
            for kv in qs.split("&"):
                k, _, v = kv.partition("=")
                if k == "name":
                    name = urllib.parse.unquote(v)
            fp = _fwlog_locate(name)
            if fp is None or not os.path.isfile(fp):
                return self._send(404, {"error": "no such log file"})
            try:
                with open(fp, "rb") as f:
                    blob = f.read(64 << 20)
            except OSError as e:
                return self._send(500, {"error": str(e)})
            # text/plain, not octet-stream: these are logs, and a reader who
            # wants to look before saving should be able to.
            return self._send(200, None, ctype="text/plain; charset=utf-8",
                              raw=blob,
                              headers={"Content-Disposition":
                                       'attachment; filename="%s"'
                                       % os.path.basename(name)})
        if path == "/api/coredump/file":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            name = ""
            for kv in qs.split("&"):
                k, _, v = kv.partition("=")
                if k == "name":
                    name = urllib.parse.unquote(v)
            blob = coredump_read(name)
            if blob is None:
                return self._send(404, {"error": "no such dump"})
            return self._send(200, None, ctype="application/octet-stream",
                              raw=blob,
                              headers={"Content-Disposition":
                                       'attachment; filename="%s"'
                                       % os.path.basename(name)})
        with _lock:
            if path == "/api/schema":
                cfg = get_config()
                schema = {}
                for k, v in get_schema().items():
                    if v.get("hidden"):
                        continue
                    v = dict(v)
                    if v.get("values_from") == "eth_ports":
                        v["values"] = wired_ports() or [v.get("default", "eth0")]
                    if v.get("values_from") == "s1g_channels":
                        v["options"] = channel_options(cfg.get("country", "US"))
                        # Every plan's channels plus the country->plan map, so
                        # the browser can refresh the list the moment another
                        # country is picked - without saving first.
                        v["options_by_plan"] = {
                            plan: channel_options_for(tbl)
                            for plan, tbl in s1g_table().items()}
                        v["country_plan"] = {
                            c: plan_key(c)
                            for c in get_schema().get("country", {}).get("values", [])}
                    if k == "operation_mode":
                        v["values"] = [m for m in v.get("values", [])
                                       if m != "unified-ap" or unified_ap_available()]
                        if v.get("default") not in v["values"] and v["values"]:
                            v["default"] = v["values"][0]
                    schema[k] = v
                return self._send(200, schema)
            if path == "/api/config":
                schema = get_schema()
                cfg = get_config()
                out = {}
                for k, meta in schema.items():
                    if meta.get("hidden"):
                        continue
                    if meta.get("type") == "password":
                        # the value never leaves the board, and the admin one
                        # is only ever stored as a hash - look there too
                        out[k] = {"set": bool(cfg.get(k)
                                              or cfg.get(k + "_hash"))}
                    else:
                        out[k] = cfg.get(k, "")
                return self._send(200, out)
            if path == "/api/cli/catalog":
                gw = unit_exists("halow-gateway.service") and \
                    unit_active("halow-gateway.service") == "active"
                return self._send(200, {
                    "commands": cli_catalog(),
                    "mode": get_config().get("operation_mode", "ap"),
                    "gateway_active": gw,
                    "debug_build": is_debug_build(),
                    "write_unlocked": cli_write_allowed(self._token()),
                    "unlock_ttl": CLI_WRITE_UNLOCK_TTL})
            if path == "/api/status":
                return self._send(200, full_status())
            if path == "/api/disk-cleanup":
                return self._send(200, disk_cleanup_catalog())
            if path == "/api/logs":
                qs = dict(kv.split("=", 1) for kv in
                          (self.path.split("?", 1)[1].split("&")
                           if "?" in self.path else []) if "=" in kv)
                code, payload = journal_tail(qs.get("unit", "halow-wlan"),
                                             qs.get("lines", 200))
                payload["units"] = list(LOG_UNITS)
                return self._send(code, payload)
            if path == "/api/fwlog":
                # probe=0 is the passive form: host-side facts only, no
                # command sent to the firmware. The tab uses it on entry.
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                return self._send(200, fwlog_state("probe=0" not in qs))
            if path == "/api/coredump":
                return self._send(200, coredump_list())
            if path == "/api/coredump/parse":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                name = ""
                for kv in qs.split("&"):
                    k, _, v = kv.partition("=")
                    if k == "name":
                        name = urllib.parse.unquote(v)
                blob = coredump_read(name)
                if blob is None:
                    return self._send(404, {"error": "no such dump"})
                res = coredump_parse(blob)
                res["name"] = os.path.basename(name)
                return self._send(200, res)
            if path == "/api/config/export":
                cfg = dict(get_config())
                cfg.pop("admin_password_hash", None)
                cfg.pop("admin_password_changed", None)
                out = {"exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "version": identity_status().get("version", ""),
                       "config": cfg}
                return self._send(200, None, ctype="application/json",
                                  raw=json.dumps(out, indent=1).encode(),
                                  headers={"Content-Disposition":
                                           'attachment; filename="halow-config.json"'})
            if path == "/api/mesh":
                if get_config().get("operation_mode") != "mesh":
                    return self._send(400, {"error": "not in mesh mode"})
                return self._send(200, mesh_status())
            if path == "/api/probe":
                # the operator asked: one broadcast, then the tables read
                # what answered. Never called on a timer.
                if not stack_running():
                    return self._send(200, {"found": 0, "peers": {}})
                found = discovery_probe()
                return self._send(200, {"found": len(found), "peers": found})
            if path == "/api/stations":
                # a stopped stack has no stations; the last ones read as live
                if not stack_running():
                    return self._send(200, [])
                return self._send(200, station_list(ap_iface()))
            if path == "/api/bridge/clients":
                if not stack_running():
                    return self._send(200, {"bridge": None, "clients": []})
                probe = "probe=1" in (self.path.split("?", 1) + [""])[1]
                return self._send(200, bridge_clients(probe))
            if path == "/api/traffic":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                since = 0
                for kv in qs.split("&"):
                    if kv.startswith("since="):
                        try:
                            since = int(kv[6:])
                        except ValueError:
                            pass
                rows = [r for r in list(_traffic["buf"]) if r["seq"] > since]
                cca = None
                if _cca is not None and _cca.attached():
                    f, bw = _cca_ap_channel()
                    snap = _cca.snapshot()
                    cca = {"idx": _cca.channel_for(f, bw) if f else None,
                           "focus": snap.get("focus"),
                           "running": snap.get("running")}
                ap_if, sta_if = traffic_ifaces()
                return self._send(200, {
                    "seq": _traffic["seq"],
                    "interval": TRAFFIC_INTERVAL,
                    "iface": ap_if,
                    "iface_role": iface_role(ap_if),
                    "iface2": sta_if,
                    "iface2_role": iface_role(sta_if) if sta_if else None,
                    "mcp": False,
                    "cca": cca,
                    "rows": rows})
            if path == "/api/sniffer":
                return self._send(200, sniffer_status())
            if path == "/api/sniffer/packets":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                since = 0
                for kv in qs.split("&"):
                    if kv.startswith("since="):
                        try:
                            since = int(kv[6:])
                        except ValueError:
                            pass
                rows = [r for r in list(_sniffer["buf"]) if r["seq"] > since]
                return self._send(200, {"seq": _sniffer["seq"],
                                        "rows": rows[-500:]})
            if path == "/api/sniffer/detail":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                epoch = ""
                for kv in qs.split("&"):
                    if kv.startswith("epoch="):
                        epoch = kv[6:]
                try:
                    e = float(epoch)
                except ValueError:
                    return self._send(400, {"error": "bad epoch"})
                # the live row and the pcap ring share kernel timestamps,
                # so a tight time filter finds the very same frame
                flt = "frame.time_epoch >= %.6f && frame.time_epoch <= %.6f" \
                      % (e - 0.0005, e + 0.0005)
                for f in sniffer_pcap_files():
                    out = run(["tshark", "-r", f, "-Y", flt, "-V"],
                              timeout=30)
                    if out and out.strip():
                        # keep only the first matched frame's tree
                        m = re.search(r"\nFrame \d+:", out[1:])
                        if m:
                            out = out[:m.start() + 1]
                        return self._send(200, {"detail": out[:65536]})
                return self._send(404, {"error": "packet no longer in the "
                                                 "pcap ring (rotated out)"})
            if path == "/api/sniffer/pcap":
                files = sniffer_pcap_files()
                if not files:
                    return self._send(404, {"error": "no capture files yet"})
                with open(files[0], "rb") as f:
                    data = f.read()
                return self._send(200, None, "application/octet-stream",
                                  raw=data, headers={
                                      "Content-Disposition":
                                      'attachment; filename="%s"'
                                      % os.path.basename(files[0])})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._json_body()
        if path == "/api/login":
            cfg = get_config()
            if pw_hash(str(body.get("password", ""))) == cfg.get(
                    "admin_password_hash"):
                tok = secrets.token_hex(16)
                _sessions[tok] = time.time() + SESSION_TTL
                _sessions_save()
                return self._send(200, {"ok": True,
                                        "must_change": needs_pw_change(cfg)},
                                  set_cookie="halow_session=%s; HttpOnly; Secure; "
                                             "Path=/; SameSite=Strict" % tok)
            time.sleep(1)
            return self._send(403, {"error": "wrong password"})
        if not self._authed():
            return self._send(401, {"error": "auth required"})
        if path == "/api/logout":
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            if "halow_session" in c:
                _sessions.pop(c["halow_session"].value, None)
                _sessions_save()
            return self._send(200, {"ok": True},
                              set_cookie="halow_session=; Max-Age=0; Path=/")
        if path == "/api/cca/cmd":
            # scanner board control; the module validates and allowlists
            # the verbs, and waits briefly for the board's ack
            if _cca is None:
                return self._send(404, {"error": "no scanner support"})
            res = _cca.command(str(body.get("op", "")), body.get("value"))
            return self._send(200 if res.get("ok") else 400, res)
        if path == "/api/chmgr/events/clear":
            # No print like the health trail above: this is a live aid the
            # daemon rebuilds from the firmware's counters, not a fault record.
            chmgr_events_clear()
            return self._send(200, {"ok": True})
        if path == "/api/chmgr/control":
            # Mode and profile only, and both are looked up in a fixed table
            # rather than passed through: the request never reaches the
            # firmware as text. Country, bandwidth, dwell and the exclusion
            # list are deliberately absent - naming any of them makes the
            # firmware sweep again, taking the AP off channel to apply a
            # setting that did not change. Those live in Settings, where the
            # restart they need is expected.
            cfg = get_config()
            mode = body.get("mode")
            prof = body.get("profile")
            if mode is not None and mode not in ("off", "auto", "monitor"):
                return self._send(400, {"error": "unknown mode"})
            if prof is not None and prof not in CHMGR_PROFILES:
                return self._send(400, {"error": "unknown profile"})
            if mode is None and prof is None:
                return self._send(400, {"error": "nothing to set"})
            eff_mode = mode or str(cfg.get("channel_manager", "off"))
            eff_prof = prof or str(cfg.get("channel_manager_profile", "normal"))
            # A relay below us cannot be quieted for the home-channel read and
            # cannot follow a switch, so the manager would leave a clean
            # channel and drop the chain. Refuse here
            # rather than start it and kill it a poll later.
            peers = wds_peers(ap_iface()) if eff_mode != "off" else []
            if peers:
                return self._send(409, {
                    "error": "a relay is associated (%s): the channel manager "
                             "cannot run with relays below this AP"
                             % ", ".join(peers)})
            # Setting a profile on a stopped manager would start it, which is
            # not what a profile button means. Promote the mode explicitly,
            # the way the operator would.
            if eff_mode == "off" and prof is not None and mode is None:
                eff_mode = "auto"
            argv = ["set", "chmgr", eff_mode]
            if eff_mode != "off":
                argv.append("profile=%d" % CHMGR_PROFILES.index(eff_prof))
            # the daemon first: armed firmware with no chmgrd decides into
            # the void, and this button is meant to be enough on its own
            daemon = True
            if eff_mode != "off":
                daemon = chmgrd_ensure()
            code, res = cli_run(argv, queue_s=CLI_QUEUE_S)
            _chmgr_param["at"] = 0.0          # this command changed them
            if code != 200:
                return self._send(code, res)
            out = res.get("out", "") or ""
            if "ok" not in out.lower():
                return self._send(502, {"error": "firmware refused",
                                        "out": out[:400]})
            if not daemon:
                print("[chmgr] armed, but chmgrd could not be started",
                      flush=True)
            # Persist, so a restart comes back the way the operator left it.
            with _lock:
                cfg = get_config()
                cfg["channel_manager"] = eff_mode
                cfg["channel_manager_profile"] = eff_prof
                # "auto" means the manager picks; with the manager off nobody
                # does, and hostapd starts on a conf with no channel at all
                # and dies. Pin what the radio is on.
                if eff_mode == "off" and \
                        str(cfg.get("channel", "")).strip().lower() == "auto":
                    ch = hostapd_status(ap_iface()).get("channel", "")
                    if ch.isdigit():
                        cfg["channel"] = int(ch)
                save_json(CONFIG_FILE, cfg)
                write_service_conf(derived_service_conf(cfg))
            _chmgr_cache["data"] = None      # next poll re-reads the chip
            return self._send(200, {"ok": True, "mode": eff_mode,
                                    "profile": eff_prof, "out": out[:400]})
        if path == "/api/fwlog/config":
            # Firmware-side log delivery. Each verb is a separate cli_app
            # round trip (cli_run serialises and rate-limits them), so apply
            # only the keys the caller actually sent.
            wanted = []
            if body.get("mode") in ("off", "flight", "stream"):
                wanted.append(["hlog", "mode", body["mode"]])
            if body.get("level") in ("none", "error", "warn", "info", "verbose"):
                wanted.append(["hlog", "level", body["level"]])
            if "cli" in body:
                wanted.append(["hlog", "cli", "on" if body["cli"] else "off"])
            if not wanted:
                return self._send(400, {"error": "nothing to set"})
            for argv in wanted:
                code, res = cli_run(argv, queue_s=CLI_QUEUE_S)
                # cli_app exits 0 whether or not the firmware accepted the
                # verb, so `rc` cannot tell. The firmware's reply can: it
                # echoes the setting it applied as "<verb>(<value>)".
                if code != 200 or (argv[1] + "(") not in res.get("out", ""):
                    return self._send(200, {"ok": False,
                                            "error": res.get("error")
                                            or "%s: firmware did not confirm"
                                               % " ".join(argv)})
            return self._send(200, {"ok": True, "state": fwlog_state()})
        if path == "/api/fwlog/dump":
            # Flight-recorder upload: push whatever the firmware has held
            # back to the host now. This is the field operation - it is what
            # makes "no UART" survivable.
            code, res = cli_run(["hlog", "dump"], queue_s=CLI_QUEUE_S)
            if code != 200:
                return self._send(code, res)
            return self._send(200, {"ok": res.get("rc") == 0,
                                    "out": res.get("out", "")})
        if path == "/api/fwlog/save":
            # Close the current log off under its own name, so what is on
            # screen right now can be handed over as a fixed file.
            return self._send(200, fwlog_rotate())
        if path == "/api/fwlog/delete":
            return self._send(200, fwlog_delete(body.get("names")))
        if path == "/api/coredump/delete":
            return self._send(200, coredump_delete(body.get("names")))
        if path == "/api/fwlog/stack":
            # Resolve a call stack out of a stack dump the operator picked
            # in the log view. The text comes from the browser rather than
            # being re-read here, so what is analysed is exactly what they
            # are looking at.
            text = body.get("text") or ""
            if len(text) > 256 * 1024:
                return self._send(400, {"error": "too much text"})
            return self._send(200, fwlog_analyse(text))
        if path == "/api/coredump/run":
            # Take a dump on a running system: proves the whole reporting
            # path (firmware -> SPI -> driver -> file -> parser) without
            # having to crash anything first.
            code, res = cli_run(["dump", "run"], queue_s=CLI_QUEUE_S)
            if code != 200:
                return self._send(code, res)
            return self._send(200, {"ok": res.get("rc") == 0,
                                    "out": res.get("out", "")})
        if path == "/api/disk-cleanup":
            ids = body.get("items")
            if not isinstance(ids, list) or not ids:
                return self._send(400, {"error": "no items selected"})
            if not _cleanup_lock.acquire(blocking=False):
                return self._send(429, {"error": "a cleanup is already "
                                                 "running"})
            try:
                return self._send(200, disk_cleanup_run(
                    [str(i)[:40] for i in ids[:20]]))
            finally:
                _cleanup_lock.release()
        if path == "/api/cli/unlock":
            # write commands: debug packages only, and only while the operator
            # keeps the session warm
            if not is_debug_build():
                return self._send(403, {"error": "not a debug build"})
            tok = self._token()
            if body.get("enable"):
                _cli_unlocked[tok] = time.time() + CLI_WRITE_UNLOCK_TTL
                print("[cli] write commands unlocked for %ds" % CLI_WRITE_UNLOCK_TTL,
                      flush=True)
            else:
                _cli_unlocked.pop(tok, None)
                print("[cli] write commands locked", flush=True)
            return self._send(200, {"write_unlocked": cli_write_allowed(tok)})
        if path == "/api/config/import":
            # restore a backup: same validation as the settings form, then the
            # normal apply path, so an imported file cannot bypass the schema
            incoming = body.get("config")
            if not isinstance(incoming, dict):
                return self._send(400, {"error": "no config object"})
            with _lock:
                if _apply_state["in_progress"]:
                    return self._send(409, {"error": "apply in progress"})
                schema, cfg = get_schema(), get_config()
                changed, skipped = {}, []
                for k, v in incoming.items():
                    meta = schema.get(k)
                    if not meta or meta.get("hidden") or not meta.get("modifiable") \
                            or k in ("admin_password", "admin_password_hash"):
                        skipped.append(k)
                        continue
                    # the management IP is per-board, not part of a HaLow
                    # profile: restoring board A's backup on board B must
                    # not steal A's address (or cut B's own uplink)
                    if meta.get("apply") == "eth-config":
                        skipped.append("%s (per-board setting)" % k)
                        continue
                    val, err = validate(k, v, meta)
                    if err:
                        skipped.append("%s (%s)" % (k, err))
                        continue
                    if cfg.get(k) != val:
                        cfg[k] = val
                        changed[k] = meta
                if not changed:
                    return self._send(200, {"action": "none", "skipped": skipped,
                                            "result": "nothing to change"})
                save_json(CONFIG_FILE, cfg)
                action, result = do_apply(cfg, changed)
                return self._send(200, {"action": action, "result": result,
                                        "changed": sorted(changed), "skipped": skipped})
        if path == "/api/config/reset":
            # Back to the shipped defaults. Two things stay: the admin
            # credential, so a reset is not a way past the login, and the
            # wired management address, which is how this page is reached
            # and is per-board rather than part of a HaLow profile - the
            # same exclusion the restore path above makes.
            with _lock:
                if _apply_state["in_progress"]:
                    return self._send(409, {"error": "apply in progress"})
                schema, cfg = get_schema(), get_config()
                backup = CONFIG_FILE + ".pre-reset"
                save_json(backup, cfg)
                new, changed, kept = dict(cfg), {}, []
                for k, meta in schema.items():
                    if meta.get("hidden") or not meta.get("modifiable") \
                            or k in ("admin_password", "admin_password_hash"):
                        continue
                    if m_apply(meta) == "eth-config":
                        kept.append(k)
                        continue
                    dflt = meta.get("default", "")
                    if new.get(k) != dflt:
                        new[k] = dflt
                        changed[k] = meta
                save_json(CONFIG_FILE, new)
                print("[cfg] reset to defaults: %d item(s), backup %s"
                      % (len(changed), backup), flush=True)  # audit trail
                # The scripts are part of what "default" means here: a header
                # or conf file edited by hand goes back too, even when
                # config.json already reads the shipped values.
                write_service_conf(derived_service_conf(new))
                note = apply_to_scripts(new, keep_blank=True)
                # that call reaches the country in use; the shipped beacon
                # interval belongs in every country's beaconing conf
                try:
                    n = len(_halow_config().reset_conf_beacon_int(
                        pkg_dir(), new.get("beacon_int")))
                    if n:
                        note += ("; beacon interval restored in %d conf "
                                 "file(s)" % n)
                except Exception as exc:
                    print("[halow-webd] beacon interval not reset in the "
                          "country confs: %s" % exc, flush=True)
                # A reset restores settings; it does not decide whether the
                # radio runs. Restarting here would bring a board back as the
                # default AP - on a remote station that is the link the
                # operator is using. The boot-time link is a setting, so that
                # one is applied; everything else waits for a restart.
                auto = ""
                if any(m_apply(m) == "autostart" for m in changed.values()):
                    auto = "; " + apply_autostart(new)
                msg = ("already at the defaults" if not changed
                       else "settings restored")
                if changed or note:
                    msg += " - restart the stack to run with them"
                if new.get("operation_mode") in ("unified-ap", "ap") \
                        and not str(new.get("channel", "")).strip() \
                        and not ap_conf_channel(new):
                    msg += "; the defaults leave the channel unset, so pick " \
                           "a channel (or auto) before starting the radio"
                return self._send(200, {
                    "action": "settings-only", "changed": sorted(changed),
                    "kept": kept, "backup": backup,
                    "result": msg + (note or "") + auto})
        if path == "/api/ping":
            code, payload = ping_station(str(body.get("ip", "")))
            return self._send(code, payload)
        if path == "/api/cli/run":
            argv = body.get("argv")
            if isinstance(argv, str):
                argv = argv.split()
            if not isinstance(argv, list) or not all(
                    isinstance(a, str) for a in argv):
                return self._send(400,
                                  {"error": "argv must be a list of strings"})
            argv = [a for a in (s.strip() for s in argv) if a]
            code, payload = cli_run(argv, cli_write_allowed(self._token()),
                                      queue_s=CLI_QUEUE_S)
            return self._send(code, payload)
        if path == "/api/sniffer":
            cfg = get_config()
            if cfg.get("operation_mode") != "sniffer":
                return self._send(400, {"error": "not in sniffer mode"})
            action = body.get("action", "")
            with _lock:
                if action == "start":
                    ok, msg = sniffer_start()
                elif action == "stop":
                    ok, msg = sniffer_stop()
                else:
                    return self._send(400, {"error": "bad action"})
            return self._send(200 if ok else 500, {"result": msg})
        if path == "/api/actions/restart":
            scope = body.get("scope", "ap")
            with _lock:
                if _apply_state["in_progress"]:
                    return self._send(409, {"error": "apply in progress"})
                if scope == "ap":
                    apply_full_restart_async()
                    return self._send(200, {"result": "restarting"})
                if scope == "stop":
                    apply_full_stop_async()
                    return self._send(200, {"result": "stopping the stack"})
            return self._send(400, {"error": "bad scope"})
        return self._send(404, {"error": "not found"})

    def do_PUT(self):
        path = self.path.split("?")[0]
        if path != "/api/config":
            if not self._authed():
                return self._send(401, {"error": "auth required"})
            return self._send(404, {"error": "not found"})
        body = self._json_body()
        changes = body.get("changes", {})
        # the set-password form is the one thing the factory default lets
        # through; every other key still needs a session
        if not self._authed() and not (list(changes) == ["admin_password"]
                                       and needs_pw_change(get_config())):
            return self._send(401, {"error": "auth required"})
        with _lock:
            if _apply_state["in_progress"]:
                return self._send(409, {"error": "apply in progress"})
            schema = get_schema()
            cfg = get_config()
            changed = {}
            before = {}
            errors = {}
            must_change = needs_pw_change(cfg)
            # items are validated against the mode the batch will end up in
            eff_mode = changes.get("operation_mode",
                                   cfg.get("operation_mode", "ap"))
            for key, val in changes.items():
                meta = schema.get(key)
                if meta is None or meta.get("hidden"):
                    errors[key] = "unknown item"
                    continue
                if not meta.get("modifiable", False):
                    errors[key] = "not modifiable"
                    continue
                if meta.get("modes") and eff_mode not in meta["modes"]:
                    errors[key] = "not applicable in %s mode" % eff_mode
                    continue
                if must_change and key != "admin_password":
                    errors[key] = "change the admin password first"
                    continue
                if key == "admin_password":
                    if len(str(val)) < 5:
                        errors[key] = "too short (min 5)"
                        continue
                    # one rule for both cases: at the first login the
                    # current password is the factory default, later it is
                    # whatever the operator set.
                    if pw_hash(str(val)) == cfg.get("admin_password_hash"):
                        errors[key] = "must differ from the current password"
                        continue
                    cfg["admin_password_hash"] = pw_hash(str(val))
                    changed[key] = meta
                    continue
                val, err = validate(key, val, meta)
                if err:
                    errors[key] = err
                    continue
                # some enum choices only exist in some modes (values_modes) -
                # e.g. run_mp() knows OPEN and WPA3-SAE, so wpa2-psk in mesh
                # would start no supplicant at all
                vmodes = meta.get("values_modes", {}).get(val)
                if vmodes and eff_mode not in vmodes:
                    errors[key] = "%s is not supported in %s mode" % (val, eff_mode)
                    continue
                # fake (non-S1G) channel numbers equal the S1G channel numbers
                # by design (CHANNEL_MAPPING_TABLE), so the same validation
                # holds for the sniffer's `iw set channel` path too
                if key == "channel" and val not in ("", "auto") \
                        and eff_mode in ("unified-ap", "ap", "sniffer", "mesh") \
                        and val not in s1g_channels(cfg.get("country", "US")):
                    errors[key] = "not a valid S1G channel for %s" \
                                  % cfg.get("country", "US")
                    continue
                if key in ("mesh_peer", "macaddr") and str(val).strip() and \
                        not re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$",
                                     str(val)):
                    errors[key] = "not a MAC address"
                    continue
                # The channel manager and a fixed channel both decide the
                # operating channel, and start.py resolves the clash by
                # silently disabling the manager - so a board saved with both
                # would run with automatic selection quietly off. Say it here
                # instead. The exclusions are the same rule seen from the
                # firmware's side: it owns the choice in relay mode and in
                # country K2, so the manager never starts there.
                if key == "channel_manager" and val != "off":
                    _ch = str(changes.get("channel", "")).strip()
                    if _ch and _ch.lower() != "auto":
                        errors[key] = "clear the fixed Channel first - the " \
                                      "manager picks the channel itself"
                        continue
                    if eff_mode == "relay":
                        errors[key] = "not available in relay mode"
                        continue
                    _rel = wds_peers(ap_iface())
                    if _rel:
                        errors[key] = ("a relay is associated (%s): the "
                                       "channel manager cannot run with "
                                       "relays below this AP" % ", ".join(_rel))
                        continue
                    if str(changes.get("country",
                                       cfg.get("country", "US"))) == "K2":
                        errors[key] = "not available in country K2 - the " \
                                      "firmware selects the channel there"
                        continue
                # Naming a channel is what turns the manager off, so only a
                # save that asks for both at once is a contradiction.
                if key == "channel" and str(val).strip().lower() not in ("", "auto") \
                        and str(changes.get("channel_manager", "off")) \
                        not in ("off", "monitor"):
                    errors[key] = "this save asks for a fixed channel and the " \
                                  "channel manager at once - pick one"
                    continue
                if key == "channel" and str(val).strip().lower() == "auto":
                    if eff_mode == "relay":
                        errors[key] = "auto is not available in relay mode - " \
                                      "the uplink decides the channel"
                        continue
                    _rel = wds_peers(ap_iface())
                    if _rel:
                        errors[key] = ("a relay is associated (%s): the "
                                       "channel manager cannot run with "
                                       "relays below this AP" % ", ".join(_rel))
                        continue
                    if str(changes.get("country",
                                       cfg.get("country", "US"))) == "K2":
                        errors[key] = "auto is not available in country K2 - " \
                                      "the firmware selects the channel there"
                        continue
                    # auto is the switch: the coupling below turns the manager
                    # on for it, and Settings has no item to do that by hand.
                    # Only a save that asks for both at once, or a board left
                    # in monitor (it measures but never picks), is refused.
                    _cm_req = str(changes.get("channel_manager", "")).strip()
                    if _cm_req in ("off", "monitor") or \
                            (not _cm_req and
                             str(cfg.get("channel_manager", "off")) == "monitor"):
                        errors[key] = "auto needs the channel manager - " \
                                      "turn it on, or name a channel"
                        continue
                if key == "mesh_static_ip" and str(val).strip() and \
                        not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", str(val)):
                    errors[key] = "not an IPv4 address"
                    continue
                if key == "eth_static_ip" and str(val).strip() and \
                        not re.match(r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$",
                                     str(val).strip()):
                    errors[key] = "use CIDR form, e.g. 192.168.1.91/24"
                    continue
                if key == "eth_gateway" and str(val).strip() and \
                        not re.match(r"^(\d{1,3}\.){3}\d{1,3}$",
                                     str(val).strip()):
                    errors[key] = "not an IPv4 address"
                    continue
                if key == "eth_dns" and str(val).strip() and \
                        not all(re.match(r"^(\d{1,3}\.){3}\d{1,3}$", d)
                                for d in re.split(r"[,\s]+",
                                                  str(val).strip())):
                    errors[key] = "space-separated IPv4 addresses"
                    continue
                if key in ("fw_name", "bd_name") and str(val).strip():
                    svc = read_kv_file(SERVICE_CONF)
                    fwdir = os.path.join(os.path.dirname(
                        svc.get("NRC_SCRIPT_DIR", "/x").rstrip("/")),
                        "sw", "firmware")
                    if not os.path.isfile(os.path.join(fwdir, str(val))):
                        errors[key] = "no such file in %s" % fwdir
                        continue
                if cfg.get(key) != val:
                    before[key] = cfg.get(key)
                    cfg[key] = val
                    changed[key] = meta
            # One decision, not two: "auto" on the channel is what turns the
            # manager on, and naming a channel turns it off. A board that
            # measures without switching keeps its monitor setting either way.
            if not errors and "channel" in changed:
                _ch = str(cfg.get("channel", "")).strip().lower()
                _cm = str(cfg.get("channel_manager", "off"))
                _want = "auto" if _ch == "auto" else "off"
                if _cm != "monitor" and _cm != _want:
                    cfg["channel_manager"] = _want
                    changed["channel_manager"] = schema["channel_manager"]

            # The DHCP pool has to sit in the AP's own /24 or dnsmasq refuses
            # to start - and the AP then beacons normally while handing out
            # no leases at all. Checked across the batch, since the address
            # and the bounds are three separate items and any one of them
            # can be the one that moved.
            if not errors and any(k in changes for k in
                                  ("ap_ip", "dhcp_start", "dhcp_end")):
                dflt = "192.168.150.1" if eff_mode == "relay" else "192.168.200.1"
                base = str(cfg.get("ap_ip", "")).strip() or dflt
                pfx = base.rsplit(".", 1)[0] + "."
                for k in ("dhcp_start", "dhcp_end"):
                    v = str(cfg.get(k, "")).strip()
                    if v and not v.startswith(pfx):
                        errors[k] = "must be in the AP's %s0/24" % pfx
                lo = str(cfg.get("dhcp_start", "")).strip()
                hi = str(cfg.get("dhcp_end", "")).strip()
                if not errors and lo and hi and \
                        int(lo.rsplit(".", 1)[1]) > int(hi.rsplit(".", 1)[1]):
                    errors["dhcp_end"] = "pool ends before it starts"
            # a country change can strand a channel override the new domain
            # does not have; fall back to the country conf file's channel
            # rather than handing hostapd an out-of-domain channel
            if not errors and "country" in changed and "channel" not in changes \
                    and str(cfg.get("channel", "")).strip().lower() != "auto" \
                    and str(cfg.get("channel", "")).strip() and \
                    cfg.get("channel") not in s1g_channels(cfg["country"]):
                cfg["channel"] = ""
                changed["channel"] = schema["channel"]
            if not errors and eff_mode == "sniffer" and \
                    not str(cfg.get("channel", "")).strip():
                errors["channel"] = "sniffer mode needs a channel"
            # Same rule for an AP, for the same reason: hostapd gets no
            # channel, this build has no ACS, and the stack comes up with
            # the interface still in managed mode - no beacon, no error.
            # "auto" passes: the manager picks one before hostapd starts.
            if not errors and eff_mode in ("unified-ap", "ap") \
                    and not str(cfg.get("channel", "")).strip() \
                    and not ap_conf_channel(cfg):
                errors["channel"] = ("AP mode needs a channel - pick one, or "
                                     "auto to let the channel manager choose")
            # static without an address would fall through to "nothing
            # changed" at apply time; reject up front instead
            if not errors and ("eth_ip_mode" in changed
                               or "eth_static_ip" in changed) \
                    and cfg.get("eth_ip_mode") == "static" \
                    and not str(cfg.get("eth_static_ip", "")).strip():
                errors["eth_static_ip"] = "static mode needs an address"
            if errors:
                # say it in the log too: the page shows one line and the
                # operator is usually looking at the board, not the browser
                print("[apd] settings refused: %s"
                      % "; ".join("%s: %s" % kv for kv in errors.items()),
                      flush=True)
                return self._send(400, {"errors": errors})
            save_json(CONFIG_FILE, cfg)
            if not changed:
                return self._send(200, {"action": "none",
                                        "result": "no changes"})
            action, result = do_apply(cfg, changed)
            return self._send(200, {"action": action, "result": result,
                                    "changed": sorted(changed.keys())})


class RedirectHandler(BaseHTTPRequestHandler):
    """Plain-HTTP listener that redirects everything to HTTPS."""
    https_port = 8443

    def _redirect(self):
        host = (self.headers.get("Host") or "").split(":")[0] or \
               self.server.server_address[0]
        self.send_response(301)
        self.send_header("Location", "https://%s:%d%s"
                         % (host, self.https_port, self.path))
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = _redirect

    def log_message(self, fmt, *args):
        pass


def ensure_tls():
    """Generate a self-signed certificate on first run (openssl CLI)."""
    cert = os.path.join(TLS_DIR, "cert.pem")
    key = os.path.join(TLS_DIR, "key.pem")
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        os.makedirs(TLS_DIR, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-nodes", "-days", "3650", "-subj", "/CN=halow-ap",
                        "-keyout", key, "-out", cert],
                       check=True, capture_output=True, timeout=60)
        os.chmod(key, 0o600)
        print("[halow-webd] generated self-signed TLS cert in %s" % TLS_DIR)
    return cert, key


def main():
    cfg = get_config()
    reconcile_service_conf()
    reconcile_autostart()
    traffic_start()
    if _cca is not None:
        _cca.start()
    _sessions_load()
    arp_snooper_start()
    discovery_responder_start()
    bridge_port_cleaner_start()
    http_port = int(cfg.get("http_port", 8080) or 8080)
    https_port = int(cfg.get("https_port", 8443) or 8443)

    cert, key = ensure_tls()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = ThreadingHTTPServer(("0.0.0.0", https_port), Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)

    RedirectHandler.https_port = https_port
    redirect_srv = ThreadingHTTPServer(("0.0.0.0", http_port), RedirectHandler)
    threading.Thread(target=redirect_srv.serve_forever, daemon=True).start()

    print("[halow-webd] https on :%d, http redirect on :%d (static: %s)"
          % (https_port, http_port, STATIC_DIR))
    srv.serve_forever()


if __name__ == "__main__":
    main()
