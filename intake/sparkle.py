"""Sparkle self-update, for a signed Syllabus.app that carries it.

Off unless the build turned it on. packaging/build.sh embeds
Contents/Frameworks/Sparkle.framework and syllabus.spec writes SUFeedURL and
SUPublicEDKey into Info.plist only when the release workflow hands them the
EdDSA public key, and the workflow does that only for a Developer ID signed,
notarized build (docs/signing.md). Every other build, the unsigned releases,
a local build.sh, a pipx install, has neither, so `start` returns False and
nothing here touches Cocoa.

When it is on, Sparkle reads the appcast attached to the latest GitHub
release (release.yml writes it and signs the disk image with the EdDSA key),
checks once a day, and asks before installing. The GitHub check in
intake/updates.py keeps running beside it: the panel's notice and the pipx
path depend on it, and it costs one request a day.

Sparkle is an Objective-C framework; PyObjC loads it into the running
NSApplication that pywebview already drives, so no native wrapper is needed.

Install and Relaunch waits for a recording to finish. The recorder is an
ffmpeg the app started from inside its own bundle, and Sparkle replaces that
bundle and quits the app; a lecture would be cut off or left for the next
launch to rescue. Sparkle's delegate lets the relaunch be postponed until a
handler is called, so `RelaunchHold` holds it while a recording is running,
checks again every POLL_SECONDS, and lets the update go once it has stopped.
"""

from __future__ import annotations

import plistlib
import sys
import threading
from pathlib import Path
from typing import Callable

from intake import config, record

FRAMEWORK = "Sparkle.framework"
REQUIRED_KEYS = ("SUFeedURL", "SUPublicEDKey")

# Between checks while an update waits on a recording. The relaunch comes at
# most this long after the recording stops, which also gives the panel time
# to file it.
POLL_SECONDS = 15.0

POSTPONE_SELECTOR = b"updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:"

_controller = None
_delegate = None  # Sparkle holds its delegate weakly; this keeps it alive.


def recording_in_progress() -> bool:
    """Whether the ffmpeg of a recording is running. Same test as the watcher's."""
    state = record._read_state()
    return state is not None and record._process_is_recording(
        state["pid"], state["staging"])


class RelaunchHold:
    """Keeps Sparkle's install-and-relaunch waiting while a recording runs.

    `postpone(handler)` answers Sparkle's question: False lets the relaunch go
    now; True means `handler` will be called later, once `recording()` says
    the recording is over. `schedule(seconds, fn)` runs fn later off the main
    thread, and `on_main(fn)` runs fn on the main thread, where Sparkle wants
    its handler called. All three are passed in so the tests need no Cocoa.
    """

    def __init__(self, recording: Callable[[], bool],
                 schedule: Callable[[float, Callable[[], None]], None],
                 on_main: Callable[[Callable[[], None]], None],
                 poll_seconds: float = POLL_SECONDS):
        self._recording = recording
        self._schedule = schedule
        self._on_main = on_main
        self._poll_seconds = poll_seconds
        self._handler: Callable[[], None] | None = None
        self._lock = threading.Lock()

    @property
    def waiting(self) -> bool:
        return self._handler is not None

    def postpone(self, handler: Callable[[], None]) -> bool:
        try:
            busy = self._recording()
        except Exception as exc:  # a failed check must not block updates forever
            print(f"sparkle: recording check failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            busy = False
        if not busy:
            return False
        with self._lock:
            first = self._handler is None
            self._handler = handler  # Sparkle's newest handler is the one to call
        if first:
            print("sparkle: update waits for the recording to stop", file=sys.stderr, flush=True)
            self._schedule(self._poll_seconds, self._poll)
        return True

    def _poll(self) -> None:
        try:
            busy = self._recording()
        except Exception:
            busy = False
        if busy:
            self._schedule(self._poll_seconds, self._poll)
            return
        with self._lock:
            handler, self._handler = self._handler, None
        if handler is not None:
            print("sparkle: recording stopped, installing the update", file=sys.stderr, flush=True)
            self._on_main(handler)


def _later(seconds: float, fn: Callable[[], None]) -> None:
    timer = threading.Timer(seconds, fn)
    timer.daemon = True
    timer.start()


def _on_main(fn: Callable[[], None]) -> None:
    from PyObjCTools import AppHelper
    AppHelper.callAfter(fn)


def _make_delegate(hold: RelaunchHold):
    """An SPUUpdaterDelegate whose only answer is the relaunch hold."""
    import objc
    from Foundation import NSObject

    # PyObjC has no metadata for Sparkle. Conforming to the protocol gives the
    # method its real encoding (BOOL, and a block as the last argument), and
    # the registration says what the block takes (index 4: self, _cmd,
    # updater, item, block), so the handler arrives as a callable rather
    # than an opaque object.
    objc.registerMetaDataForSelector(b"NSObject", POSTPONE_SELECTOR, {
        "retval": {"type": objc._C_NSBOOL},
        "arguments": {4: {"callable": {"retval": {"type": b"v"},
                                       "arguments": {0: {"type": b"^v"}}}}},
    })
    protocol = objc.protocolNamed("SPUUpdaterDelegate")

    class SyllabusUpdaterDelegate(NSObject, protocols=[protocol]):
        def updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(
                self, updater, item, handler):
            return hold.postpone(handler)

    return SyllabusUpdaterDelegate.alloc().init()


def contents_dir(executable: str | None = None) -> Path:
    """Syllabus.app/Contents, from Contents/MacOS/Syllabus."""
    return Path(executable or sys.executable).resolve().parent.parent


def configured(contents: Path) -> bool:
    """Whether this bundle was built with Sparkle: the framework and both keys."""
    if not (contents / "Frameworks" / FRAMEWORK).is_dir():
        return False
    try:
        with open(contents / "Info.plist", "rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    return all(str(info.get(key) or "").strip() for key in REQUIRED_KEYS)


def active() -> bool:
    return _controller is not None


def start(frozen: bool | None = None, contents: Path | None = None) -> bool:
    """Start Sparkle's updater if this build carries it. Main thread, once.

    Returns whether it is running. Any failure leaves the app as it would be
    without Sparkle: the GitHub notice still says when a version is out.
    """
    global _controller, _delegate
    if _controller is not None:
        return True
    frozen = config.FROZEN if frozen is None else frozen
    if not frozen or sys.platform != "darwin":
        return False
    contents = contents or contents_dir()
    if not configured(contents):
        return False
    try:
        import objc
        objc.loadBundle("Sparkle", {}, bundle_path=str(contents / "Frameworks" / FRAMEWORK))
        controller_class = objc.lookUpClass("SPUStandardUpdaterController")
        _delegate = _make_delegate(RelaunchHold(recording_in_progress, _later, _on_main))
        _controller = controller_class.alloc() \
            .initWithStartingUpdater_updaterDelegate_userDriverDelegate_(True, _delegate, None)
    except Exception as exc:  # never let an updater keep the app from opening
        print(f"sparkle: not started: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        _controller = _delegate = None
        return False
    return True


def check_now() -> None:
    """The menu's Check for Updates: Sparkle's own window, found or not."""
    if _controller is not None:
        _controller.checkForUpdates_(None)
