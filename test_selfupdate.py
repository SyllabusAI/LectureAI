"""Tests for intake/selfupdate.py: Update now, the one-click update until Sparkle is on.

No network and no hdiutil: the download and the disk image are handed in, and
the swap script runs for real against fake bundles in a temporary folder
(bash only, so this runs on the Linux CI too). From the project root:

    .venv/bin/python test_selfupdate.py
"""
import hashlib
import os
import plistlib
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()

from intake import __version__, selfupdate, updates  # noqa: E402
from intake.selfupdate import UpdateError  # noqa: E402


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    except Exception as exc:
        print(f"FAIL  {label}\n      unexpected {type(exc).__name__}: {exc}")
        return False
    print(f"ok    {label}")
    return True


def raises(fn, *needles):
    try:
        fn()
    except UpdateError as exc:
        for needle in needles:
            assert needle in str(exc), f"{needle!r} not in {exc!r}"
        return str(exc)
    raise AssertionError("no UpdateError")


def bump(version: str, by: int = 1) -> str:
    major, minor, patch = updates.parse_version(version)
    return f"{major}.{minor}.{patch + by}"


NEWER = bump(__version__)

THIN_ARM = struct.pack("<II", 0xFEEDFACF, 0x0100000C) + b"\0" * 24
THIN_X86 = struct.pack("<II", 0xFEEDFACF, 0x01000007) + b"\0" * 24
FAT_BOTH = (struct.pack(">II", 0xCAFEBABE, 2)
            + struct.pack(">IIIII", 0x01000007, 3, 4096, 100, 12)
            + struct.pack(">IIIII", 0x0100000C, 0, 8192, 100, 14))


def make_app(app: Path, version: str, binary: bytes = FAT_BOTH,
             bundle_id: str = selfupdate.BUNDLE_ID, marker: str = "") -> Path:
    """A bundle with just enough in it: Info.plist and the executable."""
    (app / "Contents" / "MacOS").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleIdentifier": bundle_id,
                       "CFBundleShortVersionString": version}, fh)
    exe = app / "Contents" / "MacOS" / "Syllabus"
    exe.write_bytes(binary)
    exe.chmod(0o755)
    (app / "Contents" / "marker").write_text(marker or version)
    return app


def fake_release(dmg_bytes: bytes, checksum: str | None = None, name: str | None = None):
    """A fetch that serves the release's two files from memory."""
    def fetch(url, dest):
        selfupdate.check_url(url)
        if url.endswith(".sha256"):
            digest = checksum or hashlib.sha256(dmg_bytes).hexdigest()
            dest.write_text(f"{digest}  {name or url.rsplit('/', 1)[1][:-7]}\n")
        else:
            dest.write_bytes(dmg_bytes)
        return dest
    return fetch


def fake_unpack(version, binary=FAT_BOTH, bundle_id=selfupdate.BUNDLE_ID):
    """Stands in for hdiutil and ditto: puts a made-up app where ditto would."""
    def unpack(dmg, mount, dest):
        make_app(dest, version, binary=binary, bundle_id=bundle_id)
    return unpack


# --- Versions and addresses -------------------------------------------------------

def test_versions():
    assert updates.is_newer("0.6.10", "0.6.9") and updates.is_newer("v1.0.0", "0.99.99")
    assert not updates.is_newer("0.6.2", "0.6.2") and not updates.is_newer("0.6.1", "0.6.2")
    dmg, sha = selfupdate.release_urls(NEWER)
    assert dmg == (f"https://github.com/SyllabusAI/LectureAI/releases/download/"
                   f"v{NEWER}/Syllabus-{NEWER}.dmg"), dmg
    assert sha == dmg + ".sha256"
    assert selfupdate.release_urls("v" + NEWER) == (dmg, sha), "a leading v is the same version"
    raises(lambda: selfupdate.release_urls("0.7"), "not a version")
    raises(lambda: selfupdate.release_urls("../../x"), "not a version")
    # Not newer than what is running: refused before anything is fetched.
    with tempfile.TemporaryDirectory() as tmp:
        fetched = []
        raises(lambda: selfupdate.prepare(__version__, Path(tmp) / "Syllabus.app", Path(tmp),
                                          arch="arm64", fetch=lambda u, d: fetched.append(u)),
               "not newer")
        raises(lambda: selfupdate.prepare(bump(__version__, -1) if __version__ != "0.0.0" else "0.0.0",
                                          Path(tmp) / "Syllabus.app", Path(tmp), arch="arm64",
                                          fetch=lambda u, d: fetched.append(u)), "not newer")
        assert fetched == []


def test_only_release_files_from_github_over_https():
    ok = f"https://github.com/SyllabusAI/LectureAI/releases/download/v{NEWER}/Syllabus-{NEWER}.dmg"
    assert selfupdate.check_url(ok) == ok
    for bad in [ok.replace("https:", "http:"),
                ok.replace("github.com", "github.com.evil.example"),
                ok.replace("github.com", "objects.githubusercontent.com"),
                ok.replace("SyllabusAI/LectureAI", "someone/LectureAI"),
                ok.replace("/releases/download/", "/raw/main/"),
                ok + "?x=1",
                ok.replace("github.com", "user@github.com"),
                ok.replace("github.com", "github.com:8443"),
                f"https://github.com/SyllabusAI/LectureAI/releases/download/../../x/y.dmg"]:
        raises(lambda: selfupdate.check_url(bad), "not a Syllabus release")
    # A redirect to plain HTTP is refused, an HTTPS one followed.
    handler = selfupdate._HttpsOnlyRedirects()
    raises(lambda: handler.redirect_request(None, None, 302, "", {}, "http://x/y"), "HTTPS")


def test_download_refuses_oversize():
    class Response:
        def __init__(self, body, length):
            self.body, self.headers = body, {"Content-Length": str(length)}

        def read(self, n):
            chunk, self.body = self.body[:n], self.body[n:]
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    url, _ = selfupdate.release_urls(NEWER)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "x.dmg"
        selfupdate.download(url, dest, limit=10, opener=lambda u: Response(b"12345", 5))
        assert dest.read_bytes() == b"12345"
        raises(lambda: selfupdate.download(url, dest, limit=10,
                                           opener=lambda u: Response(b"x" * 50, 50)), "larger")
        # A server that lies about the length is caught while reading.
        raises(lambda: selfupdate.download(url, dest, limit=10,
                                           opener=lambda u: Response(b"x" * 50, 0)), "larger")
        raises(lambda: selfupdate.download("https://example.com/x.dmg", dest), "not a Syllabus")


# --- Checks on what was downloaded ---------------------------------------------------

def test_checksum_mismatch_aborts():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = make_app(root / "Applications" / "Syllabus.app", __version__)
        work = root / "work"
        work.mkdir()
        unpacked = []
        raises(lambda: selfupdate.prepare(
            NEWER, target, work, arch="arm64",
            fetch=fake_release(b"the real image", checksum="0" * 64),
            unpack=lambda *a: unpacked.append(a)), "does not match")
        assert unpacked == [], "the image was opened after its checksum failed"
        assert not selfupdate.stage_dir(target, NEWER).exists()
        assert (target / "Contents" / "marker").read_text() == __version__, "the app was touched"
        # A checksum file for some other file, or in some other form, is refused too.
        raises(lambda: selfupdate.prepare(NEWER, target, work, arch="arm64",
                                          fetch=fake_release(b"img", name="Other.dmg")),
               "expected form")
        assert selfupdate.parse_checksum("A" * 64 + "  Syllabus-1.0.0.dmg\n",
                                         "Syllabus-1.0.0.dmg") == "a" * 64
        raises(lambda: selfupdate.parse_checksum("<html>Not Found</html>", "x.dmg"), "expected form")


def test_macho_archs():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "bin"
        for data, want in [(FAT_BOTH, {"x86_64", "arm64"}), (THIN_ARM, {"arm64"}),
                           (THIN_X86, {"x86_64"}), (b"#!/bin/sh\n", set()), (b"", set())]:
            p.write_bytes(data)
            assert selfupdate.macho_archs(p) == want, (data[:8], selfupdate.macho_archs(p))
        assert selfupdate.macho_archs(Path(tmp) / "missing") == set()


def test_wrong_arch_aborts():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = make_app(root / "Applications" / "Syllabus.app", __version__)
        work = root / "work"
        work.mkdir()
        image = b"image"
        # An Apple silicon only build on an Intel Mac.
        raises(lambda: selfupdate.prepare(NEWER, target, work, arch="x86_64",
                                          fetch=fake_release(image),
                                          unpack=fake_unpack(NEWER, binary=THIN_ARM)),
               "does not run on Macs with Intel")
        assert not selfupdate.stage_dir(target, NEWER).exists(), "the rejected copy was left behind"
        raises(lambda: selfupdate.prepare(NEWER, target, work, arch="arm64",
                                          fetch=fake_release(image),
                                          unpack=fake_unpack(NEWER, binary=THIN_X86)),
               "Apple silicon")
        assert (target / "Contents" / "marker").read_text() == __version__


def test_wrong_version_or_app_aborts():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = make_app(root / "Applications" / "Syllabus.app", __version__)
        work = root / "work"
        work.mkdir()
        raises(lambda: selfupdate.prepare(NEWER, target, work, arch="arm64",
                                          fetch=fake_release(b"i"),
                                          unpack=fake_unpack(__version__)),
               f"holds version {__version__}, not {NEWER}")
        raises(lambda: selfupdate.prepare(NEWER, target, work, arch="arm64",
                                          fetch=fake_release(b"i"),
                                          unpack=fake_unpack(NEWER, bundle_id="com.example.other")),
               "different app")
        assert not selfupdate.stage_dir(target, NEWER).exists()


def test_prepare_stages_a_checked_copy():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = make_app(root / "Applications" / "Syllabus.app", __version__)
        work = root / "work"
        work.mkdir()
        stages = []
        stage = selfupdate.prepare(NEWER, target, work, arch="x86_64",
                                   fetch=fake_release(b"image"), unpack=fake_unpack(NEWER),
                                   say=stages.append)
        assert stage == root / "Applications" / f".Syllabus-update-{NEWER}", stage
        assert (stage / "Syllabus.app" / "Contents" / "marker").read_text() == NEWER
        assert stages == ["downloading", "checking"], stages
        assert (target / "Contents" / "marker").read_text() == __version__, \
            "prepare replaced the app; only the swap after quitting may"


# --- The swap -------------------------------------------------------------------------

def gone_pid() -> int:
    """The pid of a process that has already ended."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def swap(root: Path, opener: list[str], pid: int | None = None, new: bool = True,
         env: dict | None = None):
    target = make_app(root / "Applications" / "Syllabus.app", "0.6.2", marker="old")
    stage = root / "Applications" / ".Syllabus-update-0.7.0"
    stage.mkdir()
    if new:
        make_app(stage / "Syllabus.app", "0.7.0", marker="new")
    work = root / "work"
    work.mkdir()
    (work / "Syllabus-0.7.0.dmg").write_bytes(b"image")
    script = selfupdate.write_swap_script(work)
    log = root / "update.log"
    proc = selfupdate.launch_swap(script, pid or gone_pid(), target, stage, log,
                                  opener=opener, env={"SYLLABUS_QUIT_WAIT": "2", **(env or {})})
    proc.wait(timeout=30)
    return proc.returncode, target, stage, work, log.read_text()


def test_swap_replaces_and_relaunches():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        opened = root / "opened"
        code, target, stage, work, log = swap(root, ["bash", "-c", f'echo "$1" >> {opened}', "open"])
        assert code == 0, log
        assert (target / "Contents" / "marker").read_text() == "new", log
        assert not stage.exists(), "the old copy and the stage folder stay behind"
        assert not work.exists(), "the downloaded image stays behind"
        assert opened.read_text().strip() == str(target), "the new app was not opened"
        assert "installed" in log


def test_swap_keeps_old_app_when_new_one_will_not_open():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tries = root / "tries"
        # Fails the first time (the new app), works the second (the old one, put back).
        opener = ["bash", "-c", f'echo "$1" >> {tries}; [[ $(wc -l < {tries}) -gt 1 ]]', "open"]
        code, target, stage, work, log = swap(root, opener)
        assert code == 1, log
        assert (target / "Contents" / "marker").read_text() == "old", "the old app was not put back"
        assert not stage.exists()
        assert len(tries.read_text().splitlines()) == 2, "the old app was not opened again"


def test_swap_keeps_old_app_when_new_copy_is_missing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        code, target, stage, work, log = swap(root, ["true"], new=False)
        assert code == 1 and "missing" in log, log
        assert (target / "Contents" / "marker").read_text() == "old"


def test_swap_waits_and_gives_up_if_app_does_not_quit():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        opened = root / "opened"
        code, target, stage, work, log = swap(root, ["bash", "-c", f"touch {opened}", "open"],
                                              pid=os.getpid())
        assert code == 1 and "did not quit" in log, log
        assert (target / "Contents" / "marker").read_text() == "old"
        assert not stage.exists() and not opened.exists(), "a running app was opened again"


def test_swap_keeps_old_app_when_it_cannot_be_moved():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        apps = root / "Applications"
        apps.mkdir()
        target = make_app(apps / "Syllabus.app", "0.6.2", marker="old")
        stage = root / "elsewhere"  # a stage folder that cannot take the old copy
        make_app(stage / "Syllabus.app", "0.7.0", marker="new")
        stage.chmod(0o500)
        work = root / "work"
        work.mkdir()
        try:
            proc = selfupdate.launch_swap(selfupdate.write_swap_script(work), gone_pid(),
                                          target, stage, root / "log", opener=["true"])
            proc.wait(timeout=30)
        finally:
            stage.chmod(0o700)
        log = (root / "log").read_text()
        if os.geteuid() == 0:  # root ignores the permission; nothing to prove here
            return
        assert proc.returncode == 1 and "nothing was replaced" in log, log
        assert (target / "Contents" / "marker").read_text() == "old"


def test_swap_starts_the_watcher_again():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        watch_log = root / "watcher.log"
        target = make_app(root / "Applications" / "Syllabus.app", "0.6.2", marker="old")
        stage = root / "Applications" / ".Syllabus-update-0.7.0"
        make_app(stage / "Syllabus.app", "0.7.0", binary=b'#!/bin/bash\necho "watching $*"\n')
        work = root / "work"
        work.mkdir()
        proc = selfupdate.launch_swap(
            selfupdate.write_swap_script(work), gone_pid(), target, stage, root / "log",
            opener=["true"], env={"SYLLABUS_WATCH_PROFILE": "syllabus",
                                  "SYLLABUS_WATCH_LOG": str(watch_log)})
        proc.wait(timeout=30)
        for _ in range(50):
            if watch_log.exists() and watch_log.read_text():
                break
            import time
            time.sleep(0.1)
        assert watch_log.read_text().strip() == "watching --profile syllabus watch", \
            watch_log.read_text()


def test_leftovers_cleaned_only_with_the_app_in_place():
    with tempfile.TemporaryDirectory() as tmp:
        apps = Path(tmp)
        target = apps / "Syllabus.app"
        left = apps / ".Syllabus-update-0.7.0"
        make_app(left / "previous.app", "0.6.2")
        # No app in place: the leftover may be the only copy. Keep it.
        assert selfupdate.cleanup_leftovers(target) == [] and left.exists()
        make_app(target, "0.7.0")
        assert selfupdate.cleanup_leftovers(target) == [left] and not left.exists()


# --- When it is offered ----------------------------------------------------------------

def test_offered_only_in_an_unsigned_app():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(Path(tmp) / "Syllabus.app", __version__)
        exe = str(app / "Contents" / "MacOS" / "Syllabus")
        assert selfupdate.bundle_path(exe) == app.resolve()
        assert selfupdate.bundle_path("/usr/bin/python3") is None
        reason = lambda **kw: selfupdate.unsupported_reason(  # noqa: E731
            **{"frozen": True, "platform": "darwin", "executable": exe, **kw})
        assert reason() == "", reason()
        assert reason(frozen=False) == "not the app", "a pipx install keeps its pipx line"
        assert reason(platform="linux") == "not a Mac"
        assert reason(executable="/usr/bin/python3") == "not inside an app bundle"
        # A signed build that carries Sparkle keeps Sparkle's path and hides this one.
        (app / "Contents" / "Frameworks" / "Sparkle.framework").mkdir(parents=True)
        with open(app / "Contents" / "Info.plist", "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": selfupdate.BUNDLE_ID,
                           "CFBundleShortVersionString": __version__,
                           "SUFeedURL": "https://x/appcast.xml", "SUPublicEDKey": "k"}, fh)
        assert reason() == "Sparkle updates this build", reason()
        # A folder this user cannot write to.
        if os.geteuid() != 0:
            (app / "Contents" / "Frameworks" / "Sparkle.framework").rmdir()
            Path(tmp).chmod(0o500)
            try:
                assert "not writable" in reason(), reason()
            finally:
                Path(tmp).chmod(0o700)


def test_installer_failure_keeps_the_app_and_says_why():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = make_app(root / "Applications" / "Syllabus.app", __version__)
        quits = []
        real = selfupdate.download
        selfupdate.download = lambda url, dest, **kw: fake_release(b"img", checksum="1" * 64)(url, dest)
        try:
            inst = selfupdate.Installer()
            # Busy: refused before anything starts, and the page is told.
            assert inst.start(NEWER, busy=lambda: "a recording is under way",
                              quit_app=lambda: quits.append(1), target=target,
                              run_async=False) == "a recording is under way"
            assert inst.snapshot()["state"] == "failed"
            # A bad checksum: failed, with the reason, and the app never asked to quit.
            assert inst.start(NEWER, busy=lambda: "", quit_app=lambda: quits.append(1),
                              target=target, run_async=False) == ""
            snap = inst.snapshot()
            assert snap["state"] == "failed" and "does not match" in snap["error"], snap
            assert quits == [], "the app quit after a failed check"
            assert (target / "Contents" / "marker").read_text() == __version__
            assert not selfupdate.stage_dir(target, NEWER).exists()
        finally:
            selfupdate.download = real


def test_panel_route_and_menu():
    from intake import app as desktop
    from intake import gui
    client = gui.app.test_client()
    d = client.get("/api/status").get_json()["update"]
    assert d["oneclick"] is False and d["install"]["state"] == "", d
    r = client.post("/api/update/install", json={})
    assert r.status_code == 409 and "newest" in r.get_json()["error"], r.get_json()

    idle = {"recording": {"active": False}, "configured": True, "now_class": ""}
    newer = {**idle, "update": {"available": True, "version": "0.9.0", "how": "download",
                                "url": "u", "oneclick": True}}
    titles = [i["title"] for i in desktop.menu_items(newer, attached=False, login_installed=False)]
    assert "Update to Syllabus 0.9.0…" in titles and "Download Syllabus 0.9.0…" not in titles, titles
    titles = [i["title"] for i in desktop.menu_items(newer, attached=False, login_installed=False,
                                                     self_update=True)]
    assert "Check for Updates…" in titles and not any(t.startswith("Update to") for t in titles), \
        "with Sparkle running, Sparkle's item alone"


if __name__ == "__main__":
    results = [
        run("versions compare and name the release files", test_versions),
        run("only release files from GitHub, over HTTPS", test_only_release_files_from_github_over_https),
        run("a download far over size is refused", test_download_refuses_oversize),
        run("a checksum mismatch stops before anything is opened", test_checksum_mismatch_aborts),
        run("Mach-O architectures are read", test_macho_archs),
        run("a build for the other chip is refused", test_wrong_arch_aborts),
        run("the wrong version or another app is refused", test_wrong_version_or_app_aborts),
        run("a checked copy is staged beside the app", test_prepare_stages_a_checked_copy),
        run("the swap replaces the app and opens it", test_swap_replaces_and_relaunches),
        run("the old app comes back when the new one will not open",
            test_swap_keeps_old_app_when_new_one_will_not_open),
        run("the old app stays when the new copy is missing",
            test_swap_keeps_old_app_when_new_copy_is_missing),
        run("the swap gives up if the app does not quit",
            test_swap_waits_and_gives_up_if_app_does_not_quit),
        run("the old app stays when it cannot be moved aside",
            test_swap_keeps_old_app_when_it_cannot_be_moved),
        run("the watcher starts again from the new app", test_swap_starts_the_watcher_again),
        run("leftovers go only while the app is in place",
            test_leftovers_cleaned_only_with_the_app_in_place),
        run("offered only in an unsigned app that can write its folder",
            test_offered_only_in_an_unsigned_app),
        run("a failed update keeps the app and says why",
            test_installer_failure_keeps_the_app_and_says_why),
        run("the panel route and the menu", test_panel_route_and_menu),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
