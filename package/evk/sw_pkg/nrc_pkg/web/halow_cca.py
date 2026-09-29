# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

"""CCA channel scanner bridge for the Web GUI.

An NRC5294R1 running the vendor SDK *test* firmware (`./make-test.sh test
nrc5294r1`) sweeps the country's channel table and reports the CCA busy ratio
and noise floor per channel over its USB UART.  This module watches the box's
USB serial ports, recognises such a board when it is plugged in, keeps its
measurements in a ring buffer the GUI polls, and forwards the GUI's control
requests.  Unplugging the board makes the tab disappear again.

The UART protocol is the one the vendor SDK's PC tool (tools/cca_scanner)
speaks - reproduced here rather than imported, since that tool is a
Tk/matplotlib/pyserial program and the box has none of those:

    board -> host   CCA,<seq>,<idx>,<freq_100khz>,<bw_mhz>,<busy_1m%>,
                        <busy_2m%>,<busy_2m_sec%>,<noise_dbm>
                    CCASCAN,READY,<nch>            (re)boot
                    CCAINFO,LIST,<c1>;<c2>;...     country list
                    CCAINFO,COUNTRY,<name>         current country
                    CCAINFO,NCH,<n> / CCAINFO,<idx>,<freq>,<bw> / CCAINFO,END
                    CCASTATUS,<run|stop>,<dwell>,...
                    CCAACK,<name>,<value>          command reply (-1 = rejected)
    host -> board   cca info|status|start|stop|reset
                    cca dwell <ms> | country <name> | focus <idx|-1>
                    cca custom <start_100khz> <stop_100khz> <bw>

Two firmware quirks the PC tool documents and this module honours: the CLI
executes on CR and treats LF as a line-buffer reset, and it drains its RX
queue one character per main-loop pass - so commands go out CR-terminated at
one character per millisecond.  Opening the port asserts DTR, which resets
the board (it answers with CCASCAN,READY about a second later); that is the
sync point the probe waits for.

Identification is active: the CP210x bridge on the EVK (10c4:ea60) is the
same part other boards use (the NRC7394 EVK console among them), so a port is
only claimed once it answers `cca info` with CCAINFO lines.  A port that does
not answer is left alone until it is unplugged, so nothing else on the box is
reset over and over.

stdlib only (os/termios/select) - the Rockchip host has no pyserial.
"""

import fcntl
import glob
import os
import re
import select
import struct
import sys
import termios
import threading
import time
from collections import deque

BAUD = termios.B921600
TX_CHAR_DELAY_S = 0.001          # firmware CLI drains 1 char per main loop
NOISE_INVALID = -128
RING_MAX = 20000                 # ~66 min at the default 200 ms dwell
SCAN_PERIOD_S = 2.0              # port watcher cadence
PROBE_READY_S = 3.5              # wait for CCASCAN,READY after the DTR reset
PROBE_INFO_S = 2.5               # wait for CCAINFO after `cca info`
ACK_TIMEOUT_S = 1.5
ACK_TRIES = 3
DEFAULT_DWELL_MS = 200
# USB vendor ids worth probing: Silicon Labs (EVK's CP210x), FTDI
PROBE_VENDORS = ("10c4", "0403")
BY_ID_DIR = "/dev/serial/by-id"

RE_CCA = re.compile(r"CCA,(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(-?\d+)")
RE_INFO = re.compile(r"CCAINFO,(\d+),(\d+),(\d+)")
RE_INFO_LIST = re.compile(r"CCAINFO,LIST,([\w;]+)")
RE_INFO_COUNTRY = re.compile(r"CCAINFO,COUNTRY,(\w+)")
RE_INFO_NCH = re.compile(r"CCAINFO,NCH,(\d+)")
RE_READY = re.compile(r"CCASCAN,READY,(\d+)")
RE_ACK = re.compile(r"CCAACK,(\w+),(-?\d+)")
RE_STATUS = re.compile(r"CCASTATUS,(run|stop),(\d+)")
RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _log(msg):
    sys.stderr.write("[cca] %s\n" % msg)
    sys.stderr.flush()


# ------------------------------------------------------------------ settings
_get_config = None


def configure(get_config=None):
    """Hook the daemon's config reader; keys (all optional, in config.json):
        cca_scanner_port   "off" (default) | "auto" | "/dev/serial/by-id/..."
                           Probing is opt-in: a scanner board cannot be told
                           from an EVK console by its USB id, so "auto" opens
                           every CP210x/FTDI port and asserts DTR on it. On a
                           board whose console is one of them, that resets the
                           terminal an operator is watching - for a board that
                           is usually not even plugged in.
        cca_autostart      true (default): start sweeping as soon as attached
        cca_dwell_ms       initial dwell pushed to the board (default 200)
    """
    global _get_config
    _get_config = get_config


def _cfg(key, default):
    if _get_config is None:
        return default
    try:
        v = _get_config().get(key, default)
        return default if v in ("", None) else v
    except Exception:
        return default


# ------------------------------------------------------------------- serial
def _open_port(path):
    """Raw 921600 8N1, exclusive, non-blocking.  Raises OSError."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
        except OSError:
            pass                            # a pty (tests) has no TIOCEXCL
        attrs = termios.tcgetattr(fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        iflag = 0
        oflag = 0
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB
                   | termios.CRTSCTS)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL | termios.HUPCL
        lflag = 0
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, BAUD, BAUD, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)
        try:                                # DTR/RTS high = board runs
            fcntl.ioctl(fd, termios.TIOCMBIS,
                        struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))
        except OSError:
            pass
        return fd
    except Exception:
        os.close(fd)
        raise


def _usb_vendor(dev):
    """USB idVendor (lowercase hex) of the device behind a tty, or ''."""
    try:
        name = os.path.basename(os.path.realpath(dev))
        node = os.path.realpath("/sys/class/tty/%s/device" % name)
        for _ in range(5):
            p = os.path.join(node, "idVendor")
            if os.path.exists(p):
                with open(p) as f:
                    return f.read().strip().lower()
            node = os.path.dirname(node)
    except OSError:
        pass
    return ""


def _candidates():
    """Serial ports worth probing, stable names first."""
    pin = str(_cfg("cca_scanner_port", "off")).strip()
    if pin and pin not in ("auto", "off"):
        return [pin] if os.path.exists(pin) else []
    devs = sorted(glob.glob(os.path.join(BY_ID_DIR, "*")))
    if not devs:
        devs = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    out = []
    for d in devs:
        vid = _usb_vendor(d)
        low = d.lower()
        if vid in PROBE_VENDORS or "silicon_labs" in low or "cp21" in low:
            out.append(d)
    return out


# ------------------------------------------------------------------ session
class _Session:
    """One serial link: probe, then the long-lived protocol loop."""

    def __init__(self, bridge, dev):
        self.bridge = bridge
        self.dev = dev
        self.fd = None
        self.buf = b""
        self.tx_lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True,
                                       name="cca-" + os.path.basename(dev))

    # --- io
    def _send(self, command):
        """CR-terminated, 1 char/ms (see module doc)."""
        data = command.encode("ascii") + b"\r"
        with self.tx_lock:
            for i in range(len(data)):
                os.write(self.fd, data[i:i + 1])
                try:
                    termios.tcdrain(self.fd)
                except OSError:
                    pass
                time.sleep(TX_CHAR_DELAY_S)

    def send(self, command):
        try:
            self._send(command)
            return True
        except OSError:
            self.stop_ev.set()
            return False

    def _read_lines(self, timeout):
        """Block up to `timeout` s; yield complete lines (ANSI stripped)."""
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            raise OSError("port gone")
        if not r:
            return []
        try:
            chunk = os.read(self.fd, 4096)
        except BlockingIOError:
            return []
        if chunk == b"":
            raise OSError("port closed")
        self.buf += chunk
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            text = RE_ANSI.sub("", line.decode("utf-8", "replace"))
            text = text.strip("\r\x00 ")
            if text:
                out.append(text)
        if len(self.buf) > 8192:            # garbage without newlines
            self.buf = self.buf[-1024:]
        return out

    # --- lifecycle
    def run(self):
        b = self.bridge
        try:
            self.fd = _open_port(self.dev)
        except OSError as exc:
            b._probe_failed(self.dev, "open: %s" % exc)
            return
        try:
            if not self._probe():
                b._probe_failed(self.dev, "no CCA firmware answer")
                return
            b._attached(self)
            self._loop()
        except OSError as exc:
            b._detached(self, str(exc))
        except Exception as exc:            # never take the daemon down
            _log("session error on %s: %r" % (self.dev, exc))
            b._detached(self, "error: %r" % exc)
        finally:
            self._close()

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _probe(self):
        """The open reset the board: wait for READY (or anything CCA-shaped),
        ask for the table, accept the port only if CCAINFO comes back."""
        b = self.bridge
        deadline = time.monotonic() + PROBE_READY_S
        seen_ready = False
        while time.monotonic() < deadline and not self.stop_ev.is_set():
            for line in self._read_lines(0.2):
                if RE_READY.search(line):
                    seen_ready = True
                    b._feed(self, line)
                    break
                if RE_INFO.search(line) or RE_INFO_LIST.search(line) \
                        or RE_CCA.search(line):
                    seen_ready = True      # already talking: no reset happened
                    break
            if seen_ready:
                break
        # ask regardless - an older bridge may not pulse DTR at all
        if not self.send("cca info"):
            return False
        deadline = time.monotonic() + PROBE_INFO_S
        got = False
        while time.monotonic() < deadline and not self.stop_ev.is_set():
            for line in self._read_lines(0.2):
                if RE_INFO.search(line) or RE_INFO_LIST.search(line) \
                        or RE_INFO_COUNTRY.search(line) \
                        or RE_INFO_NCH.search(line):
                    got = True
                b._feed(self, line)
        return got

    def _loop(self):
        b = self.bridge
        while not self.stop_ev.is_set():
            for line in self._read_lines(0.2):
                b._feed(self, line)
            if not os.path.exists(self.dev):
                raise OSError("device node removed")
            b._tick(self)


class Bridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.ring = deque(maxlen=RING_MAX)
        self.row_seq = 0
        self.epoch = 0               # bumps when the table/stats reset
        self.state = "absent"        # absent | probing | attached
        self.session = None
        self.port = ""
        self.attached_at = None
        self.skipped = {}            # dev -> reason (until unplugged)
        self.note = ""
        self.enabled = True
        self._reset_board_view()
        self.want_dwell = None
        self.want_country = None
        self.acks = {}               # name -> threading.Event (waiters)
        self.ack_values = {}

    # --- view of the board
    def _reset_board_view(self):
        self.channels = {}           # idx -> (freq_100khz, bw)
        self.countries = []
        self.country = ""
        self.running = False
        self.fw_dwell = None
        self.focus = None
        self.sweep_seq = 0
        self.t_first = None
        self.t_last = None
        self.nch = None
        self.pending = {}            # ack name -> {cmd, left, due}

    def _bump(self):
        self.epoch += 1

    # --- session callbacks (called from the session thread)
    def _probe_failed(self, dev, why):
        with self.lock:
            self.skipped[dev] = why
            if self.session and self.session.dev == dev:
                self.session = None
            self.state = "absent"
            self.port = ""
        _log("%s: not a CCA scanner board (%s) - ignored until replugged"
             % (dev, why))

    def _attached(self, sess):
        with self.lock:
            self.state = "attached"
            self.port = sess.dev
            self.attached_at = time.time()
            self.note = "board attached"
        _log("CCA scanner board attached on %s" % sess.dev)
        # link-up sequence, as the PC tool does after connect
        sess.send("cca status")
        dwell = self.want_dwell or int(_cfg("cca_dwell_ms", DEFAULT_DWELL_MS))
        self._send_acked(sess, "dwell", "cca dwell %d" % dwell)
        if self._autostart():
            self._send_acked(sess, "start", "cca start")

    def _detached(self, sess, why):
        with self.lock:
            if self.session is sess:
                self.session = None
            self.state = "absent"
            self.port = ""
            self.note = "board removed"
            self.pending.clear()
        _log("CCA scanner board on %s detached (%s)" % (sess.dev, why))

    def _autostart(self):
        v = _cfg("cca_autostart", True)
        if isinstance(v, str):
            return v.strip().lower() not in ("0", "false", "no", "off")
        return bool(v)

    def _send_acked(self, sess, name, cmd):
        with self.lock:
            self.pending[name] = {"cmd": cmd, "left": ACK_TRIES,
                                  "due": time.monotonic() + ACK_TIMEOUT_S}
        sess.send(cmd)

    def _tick(self, sess):
        """Ack retry chain (runs in the session thread every ~200 ms)."""
        now = time.monotonic()
        resend = []
        with self.lock:
            for name, p in list(self.pending.items()):
                if now < p["due"]:
                    continue
                p["left"] -= 1
                if p["left"] <= 0:
                    del self.pending[name]
                    if name in ("country", "custom"):
                        self.note = ("%s switch got no ack - check the "
                                     "firmware" % name)
                        resend.append("cca info")
                    continue
                p["due"] = now + ACK_TIMEOUT_S
                resend.append(p["cmd"])
        for cmd in resend:
            sess.send(cmd)

    def _feed(self, sess, text):
        """One protocol line from the board - mirrors the PC tool's handler."""
        m = RE_CCA.search(text)
        if m:
            seq, idx, freq, bw, b1m, b2m, b2ms, noise = map(int, m.groups())
            busy = b1m if bw == 1 else b2m
            now = time.time()
            with self.lock:
                if idx not in self.channels:
                    self.channels[idx] = (freq, bw)
                if self.t_first is None:
                    self.t_first = now
                self.t_last = now
                self.sweep_seq = seq
                self.row_seq += 1
                self.ring.append({
                    "n": self.row_seq, "t": round(now, 3), "seq": seq,
                    "idx": idx, "f": freq, "bw": bw, "busy": busy,
                    "b1": b1m, "b2": b2m, "b2s": b2ms, "noise": noise,
                    "e": self.epoch})
            return

        m = RE_INFO_LIST.search(text)
        if m:
            with self.lock:
                self.countries = [c for c in m.group(1).split(";") if c]
            return

        m = RE_INFO_COUNTRY.search(text)
        if m:
            name = m.group(1)
            push = None
            with self.lock:
                self.country = name
                busy = "country" in self.pending or "custom" in self.pending
                # one-shot: re-apply the country chosen before a reboot
                if self.want_country and not busy:
                    want, self.want_country = self.want_country, None
                    if want != name and want in self.countries \
                            and want != "CUSTOM":
                        push = want
            if push:
                self._send_acked(sess, "country", "cca country %s"
                                 % push.lower())
            return

        m = RE_INFO_NCH.search(text)
        if m:
            with self.lock:
                self.nch = int(m.group(1))
            return

        m = RE_INFO.search(text)
        if m:
            idx, freq, bw = map(int, m.groups())
            with self.lock:
                self.channels.setdefault(idx, (freq, bw))
            return

        m = RE_STATUS.search(text)
        if m:
            with self.lock:
                self.running = (m.group(1) == "run")
                self.fw_dwell = int(m.group(2))
            return

        m = RE_ACK.search(text)
        if m:
            name, value = m.group(1), int(m.group(2))
            refetch = False
            with self.lock:
                self.pending.pop(name, None)
                self.ack_values[name] = value
                ev = self.acks.get(name)
                if name == "dwell":
                    self.fw_dwell = value
                elif name in ("start", "stop"):
                    self.running = (name == "start")
                elif name in ("country", "custom"):
                    if value < 0:
                        self.note = "board rejected the %s switch" % name
                    else:
                        self.channels = {}
                        self.sweep_seq = 0
                        self.t_first = self.t_last = None
                        self.focus = None
                        self._bump()
                        refetch = True
                elif name == "focus":
                    self.focus = value if value >= 0 else None
                elif name == "reset":
                    self.sweep_seq = 0
                    self.t_first = self.t_last = None
                    self.focus = None
                    self._bump()
                if value >= 0 or name not in ("country", "custom"):
                    self.note = "board ack: %s = %d" % (
                        "interval" if name == "dwell" else name, value)
            if ev:
                ev.set()
            if refetch:
                sess.send("cca info")
            return

        if RE_READY.search(text):
            with self.lock:
                was_running = self.running
                self.channels = {}
                self.countries = []
                self.sweep_seq = 0
                self.t_first = self.t_last = None
                self.focus = None
                self.running = False
                self._bump()
                self.note = "board rebooted - resynced"
                if self.country:
                    self.want_country = self.country
                self.pending.pop("country", None)
                dwell = self.want_dwell or self.fw_dwell \
                    or int(_cfg("cca_dwell_ms", DEFAULT_DWELL_MS))
            if self.state != "attached":
                return                      # still probing: run() continues
            sess.send("cca info")
            self._send_acked(sess, "dwell", "cca dwell %d" % dwell)
            if was_running or self._autostart():
                self._send_acked(sess, "start", "cca start")

    # --- port watcher
    def _watch(self):
        while True:
            try:
                self._scan_once()
            except Exception as exc:
                _log("watcher error: %r" % exc)
            time.sleep(SCAN_PERIOD_S)

    def _scan_once(self):
        pin = str(_cfg("cca_scanner_port", "off")).strip().lower()
        self.enabled = pin != "off"
        if not self.enabled:
            with self.lock:
                sess = self.session
            if sess:
                sess.stop_ev.set()
            return
        present = set(_candidates())
        with self.lock:
            for dev in [d for d in self.skipped if d not in present]:
                del self.skipped[dev]        # unplugged: may probe again later
            if self.session is not None:
                return
            todo = [d for d in sorted(present) if d not in self.skipped]
            if not todo:
                return
            dev = todo[0]
            self._reset_board_view()
            self.state = "probing"
            self.port = dev
            self.session = _Session(self, dev)
            sess = self.session
        _log("probing %s" % dev)
        sess.thread.start()

    # --- public API (HTTP handlers)
    def snapshot(self):
        with self.lock:
            chans = [{"idx": i, "f": fb[0], "bw": fb[1]}
                     for i, fb in sorted(self.channels.items())]
            return {
                "supported": self.enabled,
                "state": self.state,
                "port": self.port,
                "attached_at": self.attached_at,
                "country": self.country,
                "countries": list(self.countries),
                "channels": chans,
                "nch": self.nch,
                "running": self.running,
                "dwell": self.fw_dwell,
                "focus": self.focus,
                "sweep": self.sweep_seq,
                "t_first": self.t_first,
                "t_last": self.t_last,
                "epoch": self.epoch,
                "seq": self.row_seq,
                "note": self.note,
                "autostart": self._autostart(),
                "skipped": dict(self.skipped),
            }

    def channel_for(self, freq_100khz, bw_mhz):
        """Scan-table index covering a (centre, BW) - exact match first, else
        the channel whose span contains the centre (a 1 MHz AP channel sits
        inside a 2 MHz scan channel).  None when the table has no such one."""
        try:
            f = int(freq_100khz)
            bw = int(bw_mhz) if bw_mhz else 1
        except (TypeError, ValueError):
            return None
        with self.lock:
            best = None
            for idx, (cf, cbw) in self.channels.items():
                if cf == f and cbw == bw:
                    return idx
                if abs(cf - f) < (cbw + bw) * 5 - 1:      # spans overlap
                    if best is None or abs(cbw - bw) < abs(
                            self.channels[best][1] - bw):
                        best = idx
            return best

    def latest_for(self, freq_100khz, bw_mhz, max_age_s=3.0):
        """Most recent (busy, noise, t) on the channel covering freq/bw, or
        None when nothing that fresh exists (or no board is attached)."""
        idx = self.channel_for(freq_100khz, bw_mhz)
        if idx is None:
            return None
        cut = time.time() - max_age_s
        with self.lock:
            if self.state != "attached":
                return None
            for r in reversed(self.ring):
                if r["t"] < cut:
                    return None
                if r["idx"] == idx and r["e"] == self.epoch:
                    return {"busy": r["busy"], "noise": r["noise"],
                            "t": r["t"], "idx": idx}
        return None

    def window_stats(self, window_s=300):
        """Per-channel avg/max busy and mean noise over the last window_s
        seconds (current table only): {idx: {f, bw, avg, max, noise, n}}."""
        cut = time.time() - window_s
        out = {}
        with self.lock:
            for idx, (f, bw) in self.channels.items():
                out[idx] = {"f": f, "bw": bw, "n": 0, "sum": 0, "max": None,
                            "nsum": 0, "nn": 0}
            for r in self.ring:
                if r["t"] < cut or r["e"] != self.epoch:
                    continue
                c = out.get(r["idx"])
                if c is None:
                    c = out[r["idx"]] = {"f": r["f"], "bw": r["bw"], "n": 0,
                                         "sum": 0, "max": None, "nsum": 0,
                                         "nn": 0}
                c["n"] += 1
                c["sum"] += r["busy"]
                if c["max"] is None or r["busy"] > c["max"]:
                    c["max"] = r["busy"]
                if r["noise"] != NOISE_INVALID:
                    c["nsum"] += r["noise"]
                    c["nn"] += 1
        res = {}
        for idx, c in out.items():
            res[idx] = {"f": c["f"], "bw": c["bw"], "n": c["n"],
                        "avg": round(c["sum"] / c["n"], 1) if c["n"] else None,
                        "max": c["max"],
                        "noise": round(c["nsum"] / c["nn"], 1)
                        if c["nn"] else None}
        return res

    def rows(self, since, limit=6000):
        with self.lock:
            if since <= 0:
                out = list(self.ring)
            else:
                # the ring is ordered by n; skip the prefix cheaply
                out = [r for r in self.ring if r["n"] > since]
            return {"seq": self.row_seq, "epoch": self.epoch,
                    "rows": out[-limit:]}

    def command(self, op, value=None):
        """Forward a control request; waits briefly for the board's ack."""
        with self.lock:
            sess = self.session if self.state == "attached" else None
        if sess is None:
            return {"ok": False, "error": "no scanner board attached"}
        ack = None
        if op == "start":
            cmd, ack = "cca start", "start"
        elif op == "stop":
            cmd, ack = "cca stop", "stop"
        elif op == "reset":
            cmd, ack = "cca reset", "reset"
        elif op == "dwell":
            try:
                ms = int(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": "dwell must be an integer"}
            if not 20 <= ms <= 5000:
                return {"ok": False, "error": "dwell must be 20..5000 ms"}
            with self.lock:
                self.want_dwell = ms
            cmd, ack = "cca dwell %d" % ms, "dwell"
        elif op == "country":
            name = str(value or "").strip().upper()
            with self.lock:
                known = list(self.countries)
            if not re.fullmatch(r"[A-Z0-9_]{1,16}", name):
                return {"ok": False, "error": "bad country name"}
            if known and name not in known:
                return {"ok": False, "error": "unknown country %s" % name}
            if name == "CUSTOM":
                return {"ok": False, "error": "use op=custom with a range"}
            cmd, ack = "cca country %s" % name.lower(), "country"
        elif op == "custom":
            try:
                start = int(value["start_100khz"])
                stop = int(value["stop_100khz"])
                bw = int(value["bw"])
            except (TypeError, KeyError, ValueError):
                return {"ok": False, "error": "custom needs start_100khz, "
                                              "stop_100khz, bw"}
            if bw not in (1, 2, 4) or not 7500 <= start < stop <= 9500 \
                    or stop - start < bw * 10:
                return {"ok": False, "error": "range must lie in 750..950 MHz "
                                              "and span at least one channel"}
            cmd, ack = "cca custom %d %d %d" % (start, stop, bw), "custom"
        elif op == "focus":
            try:
                idx = int(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": "focus needs a channel index"}
            with self.lock:
                if idx >= 0 and self.channels and idx not in self.channels:
                    return {"ok": False, "error": "no such channel"}
            cmd, ack = "cca focus %d" % idx, "focus"
        else:
            return {"ok": False, "error": "unknown op"}

        ev = threading.Event()
        with self.lock:
            self.acks[ack] = ev
            self.ack_values.pop(ack, None)
        self._send_acked(sess, ack, cmd)
        got = ev.wait(ACK_TIMEOUT_S)
        with self.lock:
            if self.acks.get(ack) is ev:
                del self.acks[ack]
            val = self.ack_values.get(ack)
        if not got:
            return {"ok": True, "acked": False,
                    "note": "sent; no ack yet (retrying)"}
        if val is not None and val < 0:
            return {"ok": False, "acked": True, "value": val,
                    "error": "board rejected %s" % op}
        return {"ok": True, "acked": True, "value": val}

    def csv(self, window_s=0):
        """The PC tool's log format, so its Play CSV can replay this."""
        with self.lock:
            rows = list(self.ring)
            country = self.country
        if window_s and rows:
            cut = rows[-1]["t"] - window_s
            rows = [r for r in rows if r["t"] >= cut]
        out = ["# country,%s" % (country or ""),
               "time,t_rel_s,seq,idx,freq_100khz,bw_mhz,"
               "busy_pct,busy_1m,busy_2m,busy_2m_sec,noise_dbm"]
        t0 = rows[0]["t"] if rows else 0
        for r in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["t"]))
            stamp += ".%03d" % (int(r["t"] * 1000) % 1000)
            out.append("%s,%.3f,%d,%d,%d,%d,%d,%d,%d,%d,%d" % (
                stamp, r["t"] - t0, r["seq"], r["idx"], r["f"], r["bw"],
                r["busy"], r["b1"], r["b2"], r["b2s"], r["noise"]))
        return "\n".join(out) + "\n"


_bridge = Bridge()


def start():
    threading.Thread(target=_bridge._watch, daemon=True,
                     name="cca-watch").start()


def snapshot():
    return _bridge.snapshot()


def rows(since, limit=6000):
    return _bridge.rows(since, limit)


def command(op, value=None):
    return _bridge.command(op, value)


def csv(window_s=0):
    return _bridge.csv(window_s)


def attached():
    return _bridge.state == "attached"


def channel_for(freq_100khz, bw_mhz):
    return _bridge.channel_for(freq_100khz, bw_mhz)


def latest_for(freq_100khz, bw_mhz, max_age_s=3.0):
    return _bridge.latest_for(freq_100khz, bw_mhz, max_age_s)


def window_stats(window_s=300):
    return _bridge.window_stats(window_s)
