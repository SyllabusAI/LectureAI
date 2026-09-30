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
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

from intake import config

FRAMEWORK = "Sparkle.framework"
REQUIRED_KEYS = ("SUFeedURL", "SUPublicEDKey")

_controller = None


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
    global _controller
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
        _controller = controller_class.alloc() \
            .initWithStartingUpdater_updaterDelegate_userDriverDelegate_(True, None, None)
    except Exception as exc:  # never let an updater keep the app from opening
        print(f"sparkle: not started: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        _controller = None
        return False
    return True


def check_now() -> None:
    """The menu's Check for Updates: Sparkle's own window, found or not."""
    if _controller is not None:
        _controller.checkForUpdates_(None)
