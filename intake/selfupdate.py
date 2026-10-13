"""Update now: the app replaces itself with a newer release, in one click.

A stopgap until Sparkle can be switched on. Sparkle (intake/sparkle.py) needs
a Developer ID signed build, and until there is one the panel could only say
that a version is out and link to the disk image, and the person had to
replace the copy in Applications by hand. This does the same steps
install.sh does, from inside the app:

1. Download Syllabus-X.Y.Z.dmg and its .sha256 from the GitHub release
   tagged vX.Y.Z, over HTTPS, from github.com/SyllabusAI/LectureAI only.
2. Check the image against the checksum.
3. Open the image, copy Syllabus.app out of it with ditto into a hidden
   folder next to the running app (same disk, so the swap is a rename), and
   check the copy: its bundle identifier, that CFBundleShortVersionString is
   the version that was asked for, and that its executable runs on this
   Mac's chip.
4. Hand off to a small shell script outside the bundle, then quit. The
   script waits for this process to end, renames the old app aside, renames
   the new one into place, opens it, and only then deletes the old one. Any
   step that fails puts the old app back and opens it.

Nothing is replaced until every check has passed and the app has quit, so a
failure at any point leaves the version that was running.

Quarantine: the image is fetched with Python's own HTTP client, which does
not set com.apple.quarantine, so the new app opens without a Gatekeeper
prompt, the same as an install by install.sh. The new version has a new ad
hoc signature, so macOS asks for the microphone again on the first
recording, the same as after any reinstall.

Not offered when the build carries Sparkle (a signed build updates itself
that way), from a pipx install, from a copy macOS runs from a translocated
or read-only place, or when the folder holding the app is not writable.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from intake import __version__, config, updates

REPO = updates.REPO
DOWNLOAD_HOST = "github.com"
DOWNLOAD_PATH = f"/{REPO}/releases/download/"
BUNDLE_ID = "com.maincoursemedia.syllabus"
APP_NAME = "Syllabus.app"
EXECUTABLE = "Syllabus"

# The image is about 150 MB. Anything far bigger is not ours.
MAX_DMG_BYTES = 1024 * 1024 * 1024
MAX_SHA_BYTES = 4096
TIMEOUT_SECONDS = 30.0
# How long the swap script waits for this process to end before it gives up
# and leaves the old app as it was.
QUIT_WAIT_SECONDS = 60

# Mach-O CPU types, from <mach/machine.h>.
_CPU_TYPES = {0x01000007: "x86_64", 0x0100000C: "arm64"}


class UpdateError(Exception):
    """A step failed. The message is shown to the person, in plain words."""


# --- Where the app is, and whether it can replace itself -----------------------

def bundle_path(executable: str | None = None) -> Path | None:
    """Syllabus.app, from its Contents/MacOS/Syllabus; None outside a bundle."""
    exe = Path(executable or sys.executable).resolve()
    contents = exe.parent.parent
    app = contents.parent
    if exe.parent.name != "MacOS" or contents.name != "Contents" or app.suffix != ".app":
        return None
    return app


def unsupported_reason(frozen: bool | None = None, platform: str | None = None,
                       executable: str | None = None) -> str:
    """Why this copy cannot update itself, or "" when it can."""
    frozen = config.FROZEN if frozen is None else frozen
    platform = platform or sys.platform
    if not frozen:
        return "not the app"
    if platform != "darwin":
        return "not a Mac"
    app = bundle_path(executable)
    if app is None:
        return "not inside an app bundle"
    from intake import sparkle
    if sparkle.configured(app / "Contents"):
        return "Sparkle updates this build"
    if "/AppTranslocation/" in str(app) or str(app).startswith("/Volumes/"):
        return "running from a disk image or a translocated copy"
    if not os.access(app.parent, os.W_OK) or not os.access(app, os.W_OK):
        return f"{app.parent} is not writable"
    return ""


def supported(**kwargs) -> bool:
    return unsupported_reason(**kwargs) == ""


# --- The release's two files ---------------------------------------------------

def check_url(url: str) -> str:
    """The URL back if it is an HTTPS URL for this repository's release files."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != DOWNLOAD_HOST or parts.port not in (None, 443) \
            or parts.username or parts.password or not parts.path.startswith(DOWNLOAD_PATH) \
            or ".." in parts.path or parts.query or parts.fragment:
        raise UpdateError("the download address is not a Syllabus release on GitHub")
    return url


def release_urls(version: str) -> tuple[str, str]:
    """The disk image and its checksum for version X.Y.Z, as install.sh builds them."""
    if updates.parse_version(version) is None:
        raise UpdateError(f"{version!r} is not a version number")
    version = version.lstrip("v")
    base = f"https://{DOWNLOAD_HOST}{DOWNLOAD_PATH}v{version}/Syllabus-{version}.dmg"
    return check_url(base), check_url(base + ".sha256")


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """GitHub answers a release file with a redirect to its storage host.
    Follow it only to another HTTPS address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise UpdateError("the download was redirected away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    opener = urllib.request.build_opener(_HttpsOnlyRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": f"syllabus/{__version__}"})
    return opener.open(request, timeout=TIMEOUT_SECONDS)


def download(url: str, dest: Path, limit: int = MAX_DMG_BYTES,
             progress: Callable[[int, int], None] | None = None,
             opener: Callable = _open) -> Path:
    """Save `url` to `dest`, refusing anything over `limit` bytes."""
    check_url(url)
    with opener(url) as response:
        total = int(response.headers.get("Content-Length") or 0)
        if total > limit:
            raise UpdateError("the download is far larger than a Syllabus release")
        done = 0
        with open(dest, "wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise UpdateError("the download is far larger than a Syllabus release")
                out.write(chunk)
                if progress:
                    progress(done, total)
    return dest


def parse_checksum(text: str, name: str) -> str:
    """The hex digest from a `shasum -a 256` line naming `name`."""
    match = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s+\*?(\S+)\s*", text)
    if not match or match.group(2) != name:
        raise UpdateError("the checksum file of the release is not in the expected form")
    return match.group(1).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(dmg: Path, checksum_text: str) -> None:
    if sha256_file(dmg) != parse_checksum(checksum_text, dmg.name):
        raise UpdateError("the download does not match its checksum")


# --- The app inside the image ----------------------------------------------------

def macho_archs(path: Path) -> set[str]:
    """The CPU architectures a Mach-O file (thin or universal) carries."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
            if len(head) < 8:
                return set()
            magic = struct.unpack(">I", head[:4])[0]
            if magic in (0xCAFEBABE, 0xCAFEBABF):  # universal, big-endian header
                count = struct.unpack(">I", head[4:8])[0]
                size = 20 if magic == 0xCAFEBABE else 32
                if count > 32:
                    return set()
                archs = set()
                for _ in range(count):
                    entry = fh.read(size)
                    if len(entry) < size:
                        break
                    cpu = struct.unpack(">I", entry[:4])[0]
                    if cpu in _CPU_TYPES:
                        archs.add(_CPU_TYPES[cpu])
                return archs
            if struct.unpack("<I", head[:4])[0] in (0xFEEDFACF, 0xFEEDFACE):  # thin, little-endian
                cpu = struct.unpack("<I", head[4:8])[0]
                return {_CPU_TYPES[cpu]} if cpu in _CPU_TYPES else set()
    except OSError:
        pass
    return set()


def this_mac_arch() -> str:
    """The chip, not the process: under Rosetta platform.machine() says x86_64
    on Apple silicon. Same test as install.sh."""
    try:
        out = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    return "arm64" if out == "1" else "x86_64"


def check_app(app: Path, version: str, arch: str, bundle_id: str = BUNDLE_ID) -> None:
    """Refuse a copied app that is not the Syllabus asked for, or that this Mac cannot run."""
    try:
        with open(app / "Contents" / "Info.plist", "rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        raise UpdateError("the new version has no readable Info.plist") from None
    if info.get("CFBundleIdentifier") != bundle_id:
        raise UpdateError("the disk image holds a different app")
    found = str(info.get("CFBundleShortVersionString") or "")
    if found != version.lstrip("v"):
        raise UpdateError(f"the disk image holds version {found or 'unknown'}, "
                          f"not {version.lstrip('v')}")
    exe = app / "Contents" / "MacOS" / EXECUTABLE
    if arch not in macho_archs(exe):
        chip = "Apple silicon" if arch == "arm64" else "Intel"
        raise UpdateError(f"Syllabus {found} does not run on Macs with {chip}")


def stage_dir(target: Path, version: str) -> Path:
    """The hidden folder beside the app that holds the new copy until the swap."""
    return target.parent / f".Syllabus-update-{version.lstrip('v')}"


def _mount_and_copy(dmg: Path, mount: Path, dest: Path) -> None:
    """install.sh's hdiutil and ditto steps."""
    mount.mkdir(parents=True, exist_ok=True)
    run = subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-readonly",
                          "-mountpoint", str(mount), str(dmg)], capture_output=True, text=True)
    if run.returncode != 0:
        raise UpdateError("the disk image could not be opened")
    try:
        source = mount / APP_NAME
        if not source.is_dir():
            raise UpdateError("the disk image does not contain Syllabus.app")
        # ditto keeps the symlinks and the code signature of the bundle.
        run = subprocess.run(["ditto", str(source), str(dest)], capture_output=True, text=True)
        if run.returncode != 0:
            raise UpdateError("the new version could not be copied out of the disk image")
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], capture_output=True)


def prepare(version: str, target: Path, work: Path, arch: str | None = None,
            fetch: Callable[[str, Path], Path] | None = None,
            unpack: Callable[[Path, Path, Path], None] = _mount_and_copy,
            say: Callable[[str], None] = lambda stage: None) -> Path:
    """Download, check, and copy out version `version`. Returns the stage folder,
    which holds the checked Syllabus.app. Touches nothing outside `work` and
    the stage folder; on any failure the stage folder is gone again."""
    if not updates.is_newer(version, __version__):
        raise UpdateError(f"{version} is not newer than {__version__}")
    arch = arch or this_mac_arch()
    fetch = fetch or download
    dmg_url, sha_url = release_urls(version)
    name = dmg_url.rsplit("/", 1)[1]
    say("downloading")
    dmg = fetch(dmg_url, work / name)
    sha = fetch(sha_url, work / (name + ".sha256"))
    say("checking")
    if sha.stat().st_size > MAX_SHA_BYTES:
        raise UpdateError("the checksum file of the release is not in the expected form")
    verify_checksum(dmg, sha.read_text(errors="replace"))
    stage = stage_dir(target, version)
    shutil.rmtree(stage, ignore_errors=True)  # a leftover from an earlier try
    stage.mkdir(parents=True)
    try:
        unpack(dmg, work / "mnt", stage / APP_NAME)
        check_app(stage / APP_NAME, version, arch)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage


# --- The swap, after the app has quit ---------------------------------------------

# Runs from a temporary folder, never from inside the bundle it replaces.
# Arguments: the pid to wait for, the app to replace, the stage folder that
# holds the new Syllabus.app, then the command that opens an app (`open`).
# SYLLABUS_WATCH_PROFILE, when set, starts the watcher again from the new app.
SWAP_SCRIPT = r"""#!/bin/bash
set -u
pid="$1"; target="$2"; stage="$3"; shift 3
# This script's own folder holds the downloaded image; it goes on every exit.
trap 'rm -rf "$(dirname "$0")"' EXIT
new="$stage/Syllabus.app"
old="$stage/previous.app"
say() { printf '%s update: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
reopen() { "$@" "$target" || say "could not open $target"; }
# The old copy goes back where it was. Its folder is deleted only once it is
# back in place, so the old app is never lost.
restore() {
    rm -rf "$target"
    if mv "$old" "$target"; then
        rm -rf "$stage"
    else
        say "could not put the old copy back; it is at $old"
    fi
}

for _ in $(seq 1 "${SYLLABUS_QUIT_WAIT:-__WAIT__}"); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
    say "Syllabus did not quit; nothing was replaced"
    rm -rf "$stage"
    exit 1
fi
if [[ ! -x "$new/Contents/MacOS/Syllabus" ]]; then
    say "the new copy is missing; nothing was replaced"
    rm -rf "$stage"
    reopen "$@"
    exit 1
fi
if ! mv "$target" "$old"; then
    say "could not move the old copy aside; nothing was replaced"
    rm -rf "$stage"
    reopen "$@"
    exit 1
fi
if ! mv "$new" "$target"; then
    say "could not move the new copy into place; putting the old one back"
    restore
    reopen "$@"
    exit 1
fi
if ! "$@" "$target"; then
    say "the new copy did not open; putting the old one back"
    restore
    reopen "$@"
    exit 1
fi
rm -rf "$stage"
say "installed $target"
if [[ -n "${SYLLABUS_WATCH_PROFILE:-}" ]]; then
    "$target/Contents/MacOS/Syllabus" --profile "$SYLLABUS_WATCH_PROFILE" watch \
        </dev/null >>"${SYLLABUS_WATCH_LOG:-/dev/null}" 2>&1 &
    say "started the watcher again"
fi
exit 0
""".replace("__WAIT__", str(QUIT_WAIT_SECONDS))


def write_swap_script(folder: Path) -> Path:
    script = folder / "swap.sh"
    script.write_text(SWAP_SCRIPT)
    script.chmod(0o700)
    return script


def launch_swap(script: Path, pid: int, target: Path, stage: Path, log: Path,
                opener: list[str] | None = None, env: dict | None = None) -> subprocess.Popen:
    """Start the swap script in its own session, so it outlives this process."""
    handle = open(log, "a")
    try:
        return subprocess.Popen(
            ["/bin/bash", str(script), str(pid), str(target), str(stage), *(opener or ["open"])],
            stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            start_new_session=True, env={**os.environ, **(env or {})})
    finally:
        handle.close()


# --- What the panel drives -----------------------------------------------------------

class Installer:
    """One update at a time, from a thread, with a state the page polls.

    state: "" (nothing going on), "downloading", "checking", "installing"
    (the swap script is waiting for the app to quit), or "failed" with error.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.state = ""
        self.version = ""
        self.error = ""
        self.done = 0
        self.total = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "version": self.version, "error": self.error,
                    "done": self.done, "total": self.total}

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def start(self, version: str, busy: Callable[[], str], quit_app: Callable[[], None],
              watcher: Callable[[], tuple[int | None, bool]] = lambda: (None, False),
              target: Path | None = None, run_async: bool = True) -> str:
        """Begin the update. Returns "" or why it cannot begin.

        busy() says what must finish first ("" when nothing): a recording or a
        lecture being processed. watcher() gives the watcher's pid and whether
        it runs from this app, so it can be stopped and started again.
        """
        with self._lock:
            if self.state in ("downloading", "checking", "installing"):
                return "an update is already under way"
            reason = busy()
            if reason:
                # Shown on the page too, for a start that came from the menu.
                self.state, self.version, self.error = "failed", version, reason
                return reason
            self.state, self.version, self.error, self.done, self.total = \
                "downloading", version, "", 0, 0
        target = target or bundle_path()
        args = (version, target, busy, quit_app, watcher)
        if run_async:
            threading.Thread(target=self._run, args=args, name="update-install",
                             daemon=True).start()
        else:
            self._run(*args)
        return ""

    def _run(self, version, target, busy, quit_app, watcher) -> None:
        work = Path(tempfile.mkdtemp(prefix="syllabus-update-"))
        stage = None
        try:
            def fetch(url: str, dest: Path) -> Path:
                if url.endswith(".dmg"):
                    return download(url, dest,
                                    progress=lambda d, t: self._set(done=d, total=t))
                return download(url, dest, limit=MAX_SHA_BYTES)

            stage = prepare(version, target, work, fetch=fetch,
                            say=lambda state: self._set(state=state))
            # A recording could have started during the download.
            reason = busy()
            if reason:
                raise UpdateError(reason)
            self._set(state="installing")
            env = {}
            pid, ours = watcher()
            if pid is not None and ours:
                _stop(pid)
                env = {"SYLLABUS_WATCH_PROFILE": config.PROFILE.name,
                       "SYLLABUS_WATCH_LOG": str(config.WORK_DIR / "watcher-gui.log")}
            log = config.HOME_DIR / "update.log"
            launch_swap(write_swap_script(work), os.getpid(), target, stage, log, env=env)
            print(f"update: {version} is ready; quitting so it can be put in place",
                  file=sys.stderr, flush=True)
            quit_app()
        except Exception as exc:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
            message = str(exc) if isinstance(exc, UpdateError) else _plain(exc)
            print(f"update: {version} failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            self._set(state="failed", error=message)


def _plain(exc: Exception) -> str:
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "the download did not complete; check the internet connection"
    if isinstance(exc, OSError):
        return f"a file could not be written ({exc.strerror or exc})"
    return "something unexpected went wrong"


def _stop(pid: int, wait: float = 10.0) -> None:
    """Ask the watcher to stop and give it a moment, as the panel's Stop does."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + wait
    while time.time() < deadline and config.watcher_pid() == pid:
        time.sleep(0.2)


def cleanup_leftovers(target: Path | None = None) -> list[Path]:
    """Delete stage folders an interrupted update left beside the app.

    Only while the app itself is in place, so a folder that holds the only
    copy (previous.app, if a swap died halfway) is never the one deleted.
    """
    target = target or bundle_path()
    if target is None or not (target / "Contents" / "MacOS" / EXECUTABLE).exists():
        return []
    gone = []
    for leftover in target.parent.glob(".Syllabus-update-*"):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
            gone.append(leftover)
    return gone


def cleanup_later(delay: float = 120.0) -> threading.Thread | None:
    """At startup, once any swap that opened this copy has finished."""
    if not supported():
        return None

    def run():
        time.sleep(delay)
        if installer.snapshot()["state"] in ("downloading", "checking", "installing"):
            return
        try:
            for leftover in cleanup_leftovers():
                print(f"update: removed {leftover}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"update: cleanup: {exc}", file=sys.stderr, flush=True)
    thread = threading.Thread(target=run, name="update-cleanup", daemon=True)
    thread.start()
    return thread


def runs_from(pid: int, app: Path | None) -> bool:
    """Whether process `pid` runs the executable inside `app`."""
    if app is None:
        return False
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out.startswith(str(app / "Contents" / "MacOS") + "/")


installer = Installer()
