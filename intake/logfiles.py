"""Keep panel.log from growing without end.

panel.log is not written by any logging handler of ours. launchd opens it
(StandardOutPath and StandardErrorPath in service.plist) and hands the panel
the open file as its stdout and stderr, so every print and every werkzeug line
lands there, appended, forever. One read of it was 3.7 MB and 54,000 lines.

A RotatingFileHandler cannot fix that: it would be a second writer on a file
launchd already holds open, and after it renamed the file launchd's
descriptor would keep writing into the renamed copy. So rotation happens
here, by size, the way logrotate's create mode does it: rename the file to
panel.log.1 (shifting older copies down), then, if this process's own stdout
or stderr is that file, point them at a fresh panel.log. Checked once at
startup and then every few minutes from a daemon thread, because the panel
runs for days between logins.

pipeline.log is deliberately not handled here: it is one line per lecture, the
panel's history of every recording, and it is small.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

# A few megabytes is weeks of the panel's log now that the status poll is
# filtered out (gui._QuietPolling), and two old copies keep the last rotation
# readable when somebody goes looking for yesterday's disconnect.
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 2
CHECK_SECONDS = 600

_watching: set[str] = set()
_lock = threading.Lock()


def _backup(path: Path, n: int) -> Path:
    return path.with_name(f"{path.name}.{n}")


def rotate(path: Path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> bool:
    """Move `path` aside if it has grown past `max_bytes`. True if it did.

    panel.log.1 is the newest old copy; anything past `backups` is dropped.
    Never raises: a log that cannot be rotated must not stop the panel.
    """
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    try:
        old_inode = path.stat().st_ino
        if backups < 1:
            path.unlink()
        else:
            _backup(path, backups).unlink(missing_ok=True)
            for n in range(backups - 1, 0, -1):
                older = _backup(path, n)
                if older.exists():
                    older.replace(_backup(path, n + 1))
            path.replace(_backup(path, 1))
        _reattach_stdio(path, old_inode)
        return True
    except OSError:
        return False


def _reattach_stdio(path: Path, old_inode: int) -> None:
    """Point stdout and stderr at a fresh `path` if they were writing the old one.

    Under launchd they are the file that was just renamed, and would carry on
    filling panel.log.1. Run from a terminal they are the terminal, and are
    left alone.
    """
    fds = []
    for fd in (1, 2):
        try:
            if os.fstat(fd).st_ino == old_inode:
                fds.append(fd)
        except OSError:
            continue
    _point_at(path, fds)


def _stdio_on(path: Path) -> list[int]:
    """Which of stdout and stderr are writing `path` right now."""
    try:
        here = path.stat()
    except OSError:
        return []
    fds = []
    for fd in (1, 2):
        try:
            mine = os.fstat(fd)
        except OSError:
            continue
        if (mine.st_dev, mine.st_ino) == (here.st_dev, here.st_ino):
            fds.append(fd)
    return fds


def _reclaim(path: Path, fds: list[int]) -> None:
    """Point `fds` back at `path` if the file they write is no longer it.

    Only this process's own rotation used to move them. Anything else that
    renamed or deleted panel.log (a second `intake panel` from a terminal
    rotates on its way up, before it finds the port taken) left the running
    panel writing into panel.log.1 for good: nothing wrote panel.log, so it
    never grew past the limit again, and panel.log.1 grew without end until
    a later rotation unlinked it and every line after that went nowhere.
    """
    stale = []
    try:
        here = path.stat()
        where = (here.st_dev, here.st_ino)
    except OSError:
        where = None
    for fd in fds:
        try:
            mine = os.fstat(fd)
        except OSError:
            continue
        if (mine.st_dev, mine.st_ino) != where:
            stale.append(fd)
    try:
        _point_at(path, stale)
    except OSError:
        pass


def _point_at(path: Path, fds: list[int]) -> None:
    if not fds:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    # 0600: what the panel prints (file names, errors, whatever a traceback
    # carries) is this user's alone, even if the folder around it is opened up.
    fresh = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        for fd in fds:
            os.dup2(fresh, fd)
    finally:
        os.close(fresh)


def keep_trimmed(path: Path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS,
                 every: float = CHECK_SECONDS) -> None:
    """Rotate `path` now if it is oversized, and keep checking from a thread.

    Safe to call more than once: one thread per file per process.
    """
    rotate(path, max_bytes, backups)
    key = str(path)
    with _lock:
        if key in _watching or every <= 0:
            return
        _watching.add(key)
    # Decided once, while the file is certainly ours: under launchd both are
    # panel.log, from a terminal neither is, and a terminal is never reclaimed.
    owned = _stdio_on(path)

    def loop() -> None:
        stop = threading.Event()
        while not stop.wait(every):
            rotate(path, max_bytes, backups)
            _reclaim(path, owned)

    threading.Thread(target=loop, name="log-rotate", daemon=True).start()
