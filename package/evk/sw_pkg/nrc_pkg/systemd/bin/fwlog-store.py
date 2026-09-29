#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2023-2026 Newracom, Inc.
#
# SPDX-License-Identifier: ISC

"""Rotating writer for the firmware log.

`cli_app -lf` streams the firmware console on stdout for as long as the
driver is loaded. Redirecting that straight into one file -- which is what
systemd's `append:` did -- has two problems on a production board. The file
is unbounded, on a filesystem that is an SD card; and there is only ever
*one* of it, so an operator who wants to hand somebody "the log from when it
broke" has nothing to point at.

This sits at the end of the pipe and keeps a bounded set instead. The log
being written is always fwlog.log; when it reaches the size limit it is
closed off under a timestamped name and a fresh one is started, and the
oldest archives are dropped once there are more than the keep count. The Web
GUI lists exactly these files.

SIGHUP closes the current file off immediately -- that is the GUI's "save
current log", for the case where somebody has just reproduced a fault and
wants it frozen under its own name rather than waiting for 2 MB to accrue.

Limits come from the environment (halow.conf is the unit's EnvironmentFile,
so they can be set there): FWLOG_MAX_BYTES, FWLOG_KEEP, FWLOG_DIR.
"""

import os
import re
import signal
import sys
import time

DIR = os.environ.get("FWLOG_DIR", "/var/log/halow/fwlog")
CURRENT = "fwlog.log"
PIDFILE = os.environ.get("FWLOG_PIDFILE", "/run/halow/fwlog-store.pid")

def _env_int(name, default):
    """A malformed value in halow.conf must not take the follower down: this
    unit restarts on exit, so a typo here would otherwise be a restart loop
    that also stops the assert stream being captured."""
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        sys.stderr.write("fwlog-store: ignoring %s=%r (not a number)\n"
                         % (name, os.environ.get(name)))
        return default


# 2 MB x 8 keeps roughly a working day of a chatty debug stream in ~18 MB,
# which is affordable on the smallest card these boards ship with.
MAX_BYTES = max(64 << 10, _env_int("FWLOG_MAX_BYTES", 2 << 20))
KEEP = max(1, _env_int("FWLOG_KEEP", 8))

# One level up from DIR is where the previous package wrote its single
# unrotated file; see _adopt_legacy().
LEGACY = os.path.join(os.path.dirname(DIR.rstrip("/")), "fwlog.log")


_ARCHIVE_RE = re.compile(r"fwlog-(\d{8}-\d{6})(?:-(\d+))?\.log$")


def _age_key(name):
    """Sort key for archive names: (timestamp, collision counter).

    Not a plain string sort. Rotating twice inside one second produces
    fwlog-<ts>.log and fwlog-<ts>-1.log, and '-' sorts before '.', so a
    string sort calls the first one the newest -- which would make the
    pruning below delete the wrong file.
    """
    m = _ARCHIVE_RE.match(name)
    if not m:
        return ("", 0)
    return (m.group(1), int(m.group(2) or 0))


def _stamp_name(n=0):
    base = "fwlog-" + time.strftime("%Y%m%d-%H%M%S")
    return base + (".log" if not n else "-%d.log" % n)


def _archive_path():
    """A free timestamped name. Rotating twice inside one second (a size
    rotation and a SIGHUP, say) must not overwrite the first one."""
    n = 0
    while True:
        p = os.path.join(DIR, _stamp_name(n))
        if not os.path.exists(p):
            return p
        n += 1


def _prune():
    """Drop the oldest archives beyond the keep count."""
    try:
        names = [n for n in os.listdir(DIR) if _ARCHIVE_RE.match(n)]
    except OSError:
        return
    # By the name's own timestamp: unlike mtime it does not reorder if
    # something touches a file.
    for n in sorted(names, key=_age_key)[:-KEEP] if len(names) > KEEP else []:
        try:
            os.remove(os.path.join(DIR, n))
        except OSError:
            pass


def _open_current():
    os.makedirs(DIR, exist_ok=True)
    # Unbuffered: the Web GUI tails this file, and a log you have to wait for
    # is not much use while you are watching a board misbehave.
    return open(os.path.join(DIR, CURRENT), "ab", buffering=0)


def _adopt_legacy():
    """Take over from the previous package's single file.

    Upgrading otherwise leaves the old log stranded at a path nothing reads
    any more, which reads as "the log was lost". Move it in as the first
    archive instead.
    """
    if not os.path.isfile(LEGACY) or os.path.getsize(LEGACY) == 0:
        return
    try:
        os.makedirs(DIR, exist_ok=True)
        os.rename(LEGACY, _archive_path())
    except OSError:
        pass


class Store(object):
    def __init__(self):
        _adopt_legacy()
        self.fh = _open_current()
        self.size = self._size()

    def _size(self):
        try:
            return os.path.getsize(os.path.join(DIR, CURRENT))
        except OSError:
            return 0

    def rotate(self):
        """Close the current log off under its own name and start a fresh
        one. An empty current file is not worth archiving."""
        try:
            self.fh.close()
        except OSError:
            pass
        src = os.path.join(DIR, CURRENT)
        try:
            if os.path.isfile(src) and os.path.getsize(src) > 0:
                os.rename(src, _archive_path())
                _prune()
        except OSError:
            pass
        self.fh = _open_current()
        self.size = self._size()

    def write(self, data):
        try:
            self.fh.write(data)
        except OSError:
            # A write can fail because the disk filled or because something
            # removed the file underneath us (the GUI's delete refuses the
            # current log, but /var/log is not ours alone). Reopen once; if
            # that fails too, drop the line rather than dying -- exiting
            # would take cli_app down with us and lose the netlink stream,
            # which is a far worse outcome than a gap in a log file.
            try:
                self.fh = _open_current()
                self.fh.write(data)
            except OSError:
                return
        self.size += len(data)
        if self.size >= MAX_BYTES:
            self.rotate()


def main():
    store = Store()

    def on_hup(_sig, _frm):
        # Done here rather than by setting a flag: the flag would not be
        # looked at until the next line arrives, and an idle stream is
        # exactly when somebody asks for the log to be saved.
        store.rotate()

    signal.signal(signal.SIGHUP, on_hup)

    try:
        os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write("%d\n" % os.getpid())
    except OSError:
        pass        # the pidfile only enables "save current log"; not fatal

    try:
        while True:
            # Bounded read: a stream that never sends a newline would
            # otherwise buffer without limit.
            line = sys.stdin.buffer.readline(1 << 16)
            if not line:
                break
            store.write(line)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            store.fh.close()
        except OSError:
            pass
        try:
            if open(PIDFILE).read().strip() == str(os.getpid()):
                os.remove(PIDFILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
