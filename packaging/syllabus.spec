# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for Syllabus.app. Run it through packaging/build.sh.

One directory bundle: the Python runtime, every dependency, and the intake
package with its templates, images, and bundled Google client. The entry
point is packaging/entry.py, so the same binary is the app and the command.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))
from intake import __version__  # noqa: E402

BUILD = ROOT / "packaging" / "build"
ICON = BUILD / "Syllabus.icns"

datas = [
    (str(ROOT / "intake" / "templates"), "intake/templates"),
    (str(ROOT / "intake" / "static"), "intake/static"),
    (str(ROOT / "intake" / "credentials.json"), "intake"),
]
# The Drive client loads its API description from files inside the package.
datas += collect_data_files("googleapiclient")

# ffmpeg and ffprobe, static LGPL builds (packaging/ffmpeg/), with their
# license texts beside them. build.sh sets the folder; intake/tools.py finds
# them under sys._MEIPASS/ffmpeg at run time.
FFMPEG_DIR = Path(os.environ["SYLLABUS_FFMPEG_DIR"])
binaries = [(str(FFMPEG_DIR / name), "ffmpeg") for name in ("ffmpeg", "ffprobe")]
datas += [(str(FFMPEG_DIR / name), "ffmpeg")
          for name in ("LICENSE.md", "COPYING.LGPLv2.1", "BUILD.txt")]

# EventKit, for Apple Calendar and Apple Reminders (intake/calendars.py). It is
# imported inside a function, so it is named here rather than left to the
# import scan; the [app] extra installs it. A build without it still works and
# files Apple Calendar through AppleScript instead.
try:
    import EventKit  # noqa: F401
    calendar_imports = ["EventKit", "Foundation"]
except ImportError:
    calendar_imports = []

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    # cli.py imports each subcommand's module inside a function; listing the
    # package makes sure none is left out of the bundle.
    hiddenimports=collect_submodules("intake") + calendar_imports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here is used at run time; leaving them out keeps the bundle
    # smaller. pkg_resources and unittest stay: the Google client imports both.
    excludes=["tkinter", "pydoc_data", "lib2to3",
              "IPython", "pytest", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Syllabus",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON) if ICON.exists() else None,
    # Unset means ad hoc: runnable here, not on another Mac without the
    # Gatekeeper steps in the README. A Developer ID identity goes here.
    codesign_identity=os.environ.get("SYLLABUS_CODESIGN_IDENTITY") or None,
    entitlements_file=os.environ.get("SYLLABUS_ENTITLEMENTS") or None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Syllabus")

app = BUNDLE(
    coll,
    name="Syllabus.app",
    icon=str(ICON) if ICON.exists() else None,
    bundle_identifier="com.maincoursemedia.syllabus",
    version=__version__,
    info_plist={
        "CFBundleName": "Syllabus",
        "CFBundleDisplayName": "Syllabus",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        # A menu bar app: no Dock tile at launch. app.py gives it one while
        # its window is open.
        "LSUIElement": True,
        "NSHumanReadableCopyright": "Main Course Media",
        # Shown by macOS the first time a recording opens the microphone.
        "NSMicrophoneUsageDescription":
            "Syllabus records your lectures from this microphone.",
        # Shown the first time Apple Calendar or Apple Reminders is switched
        # on in Setup. macOS 14 and later read the FullAccess keys; macOS 13
        # reads the older two. Full access, not write-only: Syllabus has to
        # find its own calendar by name before it can add to it.
        "NSCalendarsFullAccessUsageDescription":
            "Syllabus adds the deadlines from your lectures to a calendar you choose.",
        "NSCalendarsUsageDescription":
            "Syllabus adds the deadlines from your lectures to a calendar you choose.",
        "NSRemindersFullAccessUsageDescription":
            "Syllabus adds the deadlines from your lectures to a reminders list you choose.",
        "NSRemindersUsageDescription":
            "Syllabus adds the deadlines from your lectures to a reminders list you choose.",
        # Only used when EventKit is missing from a build and Apple Calendar
        # falls back to AppleScript.
        "NSAppleEventsUsageDescription":
            "Syllabus adds the deadlines from your lectures to Calendar.",
    },
)
