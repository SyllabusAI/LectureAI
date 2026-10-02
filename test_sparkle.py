"""Tests for self-update being off unless a build turns it on, and the appcast.

intake/sparkle.py must do nothing in an unsigned build, a local build, or a
pipx install; packaging/appcast.py must write a feed Sparkle can read. No
Cocoa, no network, no keychain. From the project root:

    .venv/bin/python test_sparkle.py
"""
import base64
import plistlib
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))

from _test_home import fresh_home  # noqa: E402

fresh_home()

import appcast  # noqa: E402  packaging/appcast.py
from intake import app, sparkle  # noqa: E402

NS = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


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


def fake_contents(framework: bool, keys: dict) -> Path:
    contents = Path(tempfile.mkdtemp()) / "Syllabus.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    if framework:
        (contents / "Frameworks" / sparkle.FRAMEWORK).mkdir(parents=True)
    info = {"CFBundleName": "Syllabus", **keys}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    return contents


KEYS = {"SUFeedURL": "https://example.test/appcast.xml", "SUPublicEDKey": "abc="}


def test_configured_needs_everything():
    assert sparkle.configured(fake_contents(True, KEYS))
    assert not sparkle.configured(fake_contents(False, KEYS)), "no framework"
    assert not sparkle.configured(fake_contents(True, {})), "an unsigned build's plist"
    assert not sparkle.configured(fake_contents(True, {"SUFeedURL": "x"})), "no key"
    assert not sparkle.configured(fake_contents(True, {**KEYS, "SUPublicEDKey": " "}))
    assert not sparkle.configured(Path(tempfile.mkdtemp())), "no bundle at all"


def test_start_is_off_without_a_configured_bundle():
    assert not sparkle.start(frozen=False, contents=fake_contents(True, KEYS)), "pipx"
    assert not sparkle.start(frozen=True, contents=fake_contents(False, {})), "unsigned app"
    assert not sparkle.active()
    sparkle.check_now()  # a no-op when off, never an error


def test_contents_dir():
    assert sparkle.contents_dir("/Applications/Syllabus.app/Contents/MacOS/Syllabus") == \
        Path("/Applications/Syllabus.app/Contents").resolve()


def test_menu():
    status = {"recording": {"active": False}, "configured": True,
              "update": {"available": True, "version": "9.9.9", "how": "download",
                         "url": "https://x/Syllabus-9.9.9.dmg"}}
    actions = [i["action"] for i in app.menu_items(status, False, False)]
    assert "update" in actions and "sparkle_check" not in actions, "unsigned: the download link"
    items = app.menu_items(status, False, False, self_update=True)
    actions = [i["action"] for i in items]
    assert "sparkle_check" in actions and "update" not in actions, actions
    assert any(i["title"] == "Check for Updates…" for i in items)


def test_appcast():
    xml = appcast.build("0.6.0", appcast.dmg_url("0.6.0", "Syllabus-0.6.0.dmg"), 1234,
                        "c2lnbmF0dXJl\n", notes_url="https://x/notes",
                        pub_date="Mon, 06 Oct 2026 12:00:00 GMT")
    root = ET.fromstring(xml)
    item = root.find("channel/item")
    assert item.find(f"{NS}version").text == "0.6.0"
    assert item.find(f"{NS}shortVersionString").text == "0.6.0"
    assert item.find(f"{NS}minimumSystemVersion").text == "13.0"
    enc = item.find("enclosure")
    assert enc.get("url") == ("https://github.com/SyllabusAI/LectureAI/releases/download/"
                              "v0.6.0/Syllabus-0.6.0.dmg"), enc.get("url")
    assert enc.get("length") == "1234" and enc.get(f"{NS}edSignature") == "c2lnbmF0dXJl"
    try:
        appcast.build("0.6.0", "u", 1, "  ")
    except ValueError:
        pass
    else:
        raise AssertionError("an empty signature must not make a feed")


def test_minimum_system_matches_the_spec():
    spec = (ROOT / "packaging" / "syllabus.spec").read_text()
    assert f'"LSMinimumSystemVersion": "{appcast.MIN_SYSTEM}"' in spec


def test_public_key():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization as s
    except ImportError:
        print("      (cryptography not installed; skipped)")
        return
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())
    pub = key.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)
    assert appcast.public_key(base64.b64encode(seed).decode() + "\n") == \
        base64.b64encode(pub).decode()
    try:
        appcast.public_key(base64.b64encode(b"x" * 64).decode())
    except ValueError:
        pass
    else:
        raise AssertionError("a key that is not the 32-byte seed is refused")


def test_release_notes_line_the_signed_build_drops():
    # release.yml drops the one line carrying this phrase from a signed build's
    # notes. If the wording changes, change the workflow with it.
    notes = (ROOT / "packaging" / "release-notes.md").read_text().splitlines()
    hits = [line for line in notes if "not yet signed with an Apple Developer ID" in line]
    assert len(hits) == 1, hits
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "not yet signed with an Apple Developer ID" in workflow


class FakeClock:
    """schedule() and on_main() for RelaunchHold, run by hand."""

    def __init__(self):
        self.pending = []
        self.main = []

    def schedule(self, seconds, fn):
        self.pending.append(fn)

    def on_main(self, fn):
        self.main.append(fn)

    def tick(self):
        due, self.pending = self.pending, []
        for fn in due:
            fn()


def test_relaunch_goes_now_without_a_recording():
    clock = FakeClock()
    hold = sparkle.RelaunchHold(lambda: False, clock.schedule, clock.on_main)
    assert hold.postpone(lambda: None) is False
    assert not hold.waiting and not clock.pending


def test_relaunch_waits_for_the_recording():
    recording = [True]
    calls = []
    clock = FakeClock()
    hold = sparkle.RelaunchHold(lambda: recording[0], clock.schedule, clock.on_main)
    assert hold.postpone(lambda: calls.append("first")) is True
    assert hold.waiting and len(clock.pending) == 1
    clock.tick()
    clock.tick()
    assert not clock.main and len(clock.pending) == 1, "still recording: checks again"
    # Asked again while waiting: the newest handler wins, and no second timer.
    assert hold.postpone(lambda: calls.append("second")) is True
    assert len(clock.pending) == 1
    recording[0] = False
    clock.tick()
    assert not clock.pending and len(clock.main) == 1 and not hold.waiting
    clock.main[0]()
    assert calls == ["second"], calls


def test_a_failed_check_does_not_block_updates():
    def broken():
        raise OSError("ps went away")
    clock = FakeClock()
    hold = sparkle.RelaunchHold(broken, clock.schedule, clock.on_main)
    assert hold.postpone(lambda: None) is False


def test_recording_in_progress_reads_the_state_file():
    from intake import record
    saved = record._read_state, record._process_is_recording
    try:
        record._read_state = lambda: None
        assert sparkle.recording_in_progress() is False
        record._read_state = lambda: {"pid": 1, "staging": Path("/x/rec.m4a")}
        record._process_is_recording = lambda pid, staging: pid == 1 and staging.name == "rec.m4a"
        assert sparkle.recording_in_progress() is True
        record._process_is_recording = lambda pid, staging: False
        assert sparkle.recording_in_progress() is False, "a dead ffmpeg is not a recording"
    finally:
        record._read_state, record._process_is_recording = saved


if __name__ == "__main__":
    results = [
        run("Sparkle is configured only with the framework and both keys",
            test_configured_needs_everything),
        run("an unsigned or pipx install never starts it", test_start_is_off_without_a_configured_bundle),
        run("the bundle is found from its executable", test_contents_dir),
        run("the menu offers Check for Updates only when it runs", test_menu),
        run("the appcast carries the version, the image, and its signature", test_appcast),
        run("the appcast's minimum macOS is the app's", test_minimum_system_matches_the_spec),
        run("the public key comes from the exported private key", test_public_key),
        run("the Gatekeeper paragraph the signed notes drop is still there",
            test_release_notes_line_the_signed_build_drops),
        run("with no recording the update relaunches at once",
            test_relaunch_goes_now_without_a_recording),
        run("during a recording the relaunch waits until it stops",
            test_relaunch_waits_for_the_recording),
        run("a recording check that fails lets the update go",
            test_a_failed_check_does_not_block_updates),
        run("a recording is a live ffmpeg named in the state file",
            test_recording_in_progress_reads_the_state_file),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
