"""Is there a newer Syllabus? Asked of GitHub Releases once a day, answered from a file.

Releases of the app are tags like v0.2.0 on SyllabusAI/LectureAI, cut by the
release workflow (.github/workflows/release.yml). The panel asks GitHub for
the list from a thread at startup, no more than once a day, and remembers
the answer in the home directory. The status the page polls, and the menu
bar's menu, read that file and never the network, so a Mac that is offline
or a GitHub that is slow costs nothing.

What a person is told depends on how Syllabus was installed. In the app,
the newer version is a download away and the notice links to it. From a
pipx install the update is `pipx upgrade intake`, so the notice says that.
Nothing here replaces itself: an unsigned app that swapped its own files
would send everyone back through Gatekeeper on each version, and its
microphone permission with them. A Developer ID signed build that carries
Sparkle updates itself through intake/sparkle.py; this check keeps running
beside it for the panel's notice and the pipx path.

Other releases on the same repository, the ffmpeg builds the app bundles,
are tagged ffmpeg-... and are skipped here.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from intake import __version__, config

REPO = "SyllabusAI/LectureAI"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

# Between checks. A person who opens the panel ten times a day asks once.
INTERVAL_SECONDS = 24 * 60 * 60
TIMEOUT_SECONDS = 8.0

VERSION_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def cache_path() -> Path:
    return config.WORK_DIR / "update.json"


def parse_version(text: str) -> tuple[int, int, int] | None:
    """(0, 2, 0) from "v0.2.0" or "0.2.0"; None for anything else."""
    match = VERSION_TAG.match(text if text.startswith("v") else "v" + text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, now = parse_version(candidate), parse_version(current)
    return bool(new and now and new > now)


def pick_release(releases: list[dict]) -> dict | None:
    """The newest published app release in GitHub's list, or None.

    App releases are tagged vX.Y.Z; drafts, prereleases, and the ffmpeg
    builds are passed over. GitHub lists newest first, but the version
    decides, not the order.
    """
    best: dict | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name") or "")
        version = parse_version(tag)
        if version is None:
            continue
        if best is None or version > parse_version(best["tag_name"]):  # type: ignore[operator]
            best = release
    return best


def _download_url(release: dict) -> str:
    """The DMG attached to the release, or the release page when none is."""
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.endswith(".dmg") and asset.get("browser_download_url"):
            return str(asset["browser_download_url"])
    return str(release.get("html_url") or RELEASES_PAGE)


def _fetch_releases() -> list[dict]:
    request = urllib.request.Request(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"syllabus/{__version__}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        data = json.load(response)
    return data if isinstance(data, list) else []


def read_cache(path: Path | None = None) -> dict:
    path = path or cache_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def check(now: float | None = None, fetch: Callable[[], list[dict]] = _fetch_releases,
          force: bool = False, path: Path | None = None) -> dict:
    """Refresh the remembered answer if it is a day old. Returns the record.

    The record: checked_at (epoch seconds), version (the newest release's,
    "" when none), url (where to get it), error ("" or what went wrong the
    last time; the previous answer is kept then).
    """
    path = path or cache_path()
    now = time.time() if now is None else now
    record = read_cache(path)
    if not force and record.get("checked_at") and \
            now - float(record["checked_at"]) < INTERVAL_SECONDS:
        return record
    try:
        release = pick_release(fetch())
        record = {
            "checked_at": now,
            "version": str(release["tag_name"]).lstrip("v") if release else "",
            "url": _download_url(release) if release else RELEASES_PAGE,
            "error": "",
        }
    except Exception as exc:  # offline, rate limited, GitHub down: keep what we had
        record = {**record, "checked_at": now, "error": f"{type(exc).__name__}: {exc}"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    except OSError:
        pass
    return record


def status(record: dict | None = None, current: str = __version__,
           frozen: bool | None = None) -> dict:
    """What the page and the menu show. File only; never the network."""
    record = read_cache() if record is None else record
    frozen = config.FROZEN if frozen is None else frozen
    latest = str(record.get("version") or "")
    available = bool(latest) and is_newer(latest, current)
    checked = record.get("checked_at")
    return {
        "current": current,
        "available": available,
        "version": latest if available else "",
        "url": str(record.get("url") or RELEASES_PAGE) if available else "",
        # How this install updates: the app is a download, pipx is a command.
        "how": "download" if frozen else "pipx",
        "checked_at": (datetime.fromtimestamp(float(checked), tz=timezone.utc).isoformat()
                       if checked else ""),
        "error": str(record.get("error") or ""),
    }


def check_later(delay: float = 3.0) -> threading.Thread:
    """Refresh from a thread, after the panel is up, without holding it up."""
    def run():
        time.sleep(delay)
        try:
            check()
        except Exception as exc:  # never let this reach the panel
            print(f"update check: {exc}", file=sys.stderr, flush=True)
    thread = threading.Thread(target=run, name="update-check", daemon=True)
    thread.start()
    return thread
