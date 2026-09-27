"""Tests for record.py's device selection and naming.

Pure logic only: no microphone is opened and no ffmpeg process is started, so
this is safe to run anywhere. From the project root:

    .venv/bin/python test_record.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config  # noqa: E402
from intake import consent  # noqa: E402
from intake import record  # noqa: E402

# Real output from `ffmpeg -f avfoundation -list_devices true -i ""`, including
# the curly apostrophe macOS puts in device names.
SAMPLE = """[AVFoundation indev @ 0x809018140] AVFoundation video devices:
[AVFoundation indev @ 0x809018140] [0] FaceTime HD Camera
[AVFoundation indev @ 0x809018140] [1] Trace’s iPhone Camera
[AVFoundation indev @ 0x809018140] [2] Capture screen 0
[AVFoundation indev @ 0x809018140] AVFoundation audio devices:
[AVFoundation indev @ 0x809018140] [0] Trace’s iPhone Microphone
[AVFoundation indev @ 0x809018140] [1] MacBook Pro Microphone
[AVFoundation indev @ 0x809018140] [2] Microsoft Teams Audio
[in#0 @ 0x809018000] Error opening input: Input/output error
"""


def fake_devices(sample=SAMPLE):
    """Parse `sample` the way list_devices parses ffmpeg's real output."""
    devices, in_audio = [], False
    for line in sample.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if "AVFoundation video devices:" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = record.DEVICE_LINE.match(line)
        if match:
            devices.append((int(match.group(1)), match.group(2).strip()))
    return devices


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    print(f"ok    {label}")
    return True


results = []

# Only audio devices, never the cameras that share the same numbering.
def t1():
    got = fake_devices()
    assert got == [(0, "Trace’s iPhone Microphone"),
                   (1, "MacBook Pro Microphone"),
                   (2, "Microsoft Teams Audio")], got
results.append(run("parses audio devices, skipping video", t1))


# The whole reason for name matching: index 0 is the iPhone, not this Mac.
def t2():
    record.list_devices = fake_devices
    index, name = record.resolve_device(None)
    assert name == "MacBook Pro Microphone", name
    assert index == 1, index
results.append(run("default picks the built-in mic, not index 0", t2))


def t3():
    record.list_devices = fake_devices
    assert record.resolve_device("teams")[0] == 2
    assert record.resolve_device("MacBook")[0] == 1
results.append(run("name substring match is case-insensitive", t3))


def t4():
    record.list_devices = fake_devices
    assert record.resolve_device("0")[1] == "Trace’s iPhone Microphone"
results.append(run("an explicit index is still honored", t4))


def t5():
    record.list_devices = fake_devices
    for bad in ("nonexistent-mic", "9"):
        try:
            record.resolve_device(bad)
        except RuntimeError:
            continue
        raise AssertionError(f"{bad!r} should have raised")
results.append(run("unknown device or index raises", t5))


# With the preferred mic unplugged, fall through rather than failing.
def t6():
    without = SAMPLE.replace("[1] MacBook Pro Microphone",
                             "[1] Some USB Headset Microphone")
    record.list_devices = lambda: fake_devices(without)
    index, name = record.resolve_device(None)
    assert "Microphone" in name, name
results.append(run("falls back when the preferred mic is absent", t6))


# Naming: the course code has to survive into the filename, because
# infer_course_from_filename is the pipeline's fallback when the schedule misses.
def t7():
    # Tuesday 14:00 is ACCT-4321 in the shipped schedule.
    when = datetime(2026, 9, 8, 14, 0)
    name = record.output_name(when)
    assert name == "ACCT-4321_2026-09-08_1400.m4a", name
    assert config.infer_course_from_filename(name) == "ACCT-4321"
results.append(run("scheduled class is named for its course", t7))


def t8():
    when = datetime(2026, 9, 6, 3, 0)  # a Sunday, no class
    name = record.output_name(when)
    assert name == "lecture_2026-09-06_0300.m4a", name
results.append(run("unscheduled recording gets a neutral name", t8))


def t9():
    when = datetime(2026, 9, 6, 3, 0)
    name = record.output_name(when, course="RELI-3304")
    assert name == "RELI-3304_2026-09-06_0300.m4a", name
    assert config.infer_course_from_filename(name) == "RELI-3304"
results.append(run("an explicit course overrides the schedule", t9))


# The date in the name must not be mistaken for a course code.
def t10():
    name = record.output_name(datetime(2026, 9, 8, 14, 0))
    assert config.infer_course_from_filename(name) == "ACCT-4321"
    neutral = record.output_name(datetime(2026, 9, 6, 3, 0))
    assert config.infer_course_from_filename(neutral) == config.UNKNOWN_COURSE
results.append(run("the date in a name invents no course", t10))


# --- a recording nobody is holding ----------------------------------------
#
# ffmpeg runs in its own session and outlives the panel or terminal that
# started it. The state file is how the next process finds it again.

import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from intake import transcribe  # noqa: E402

STATE = config.RECORDING_STATE_FILE
STAGING = config.WORK_DIR / "recording_20260910-123340.m4a"
STARTED = datetime(2026, 9, 10, 12, 33, 40)  # a Thursday: ENTR-3306 at 12

# Adopting or starting a recording asks caffeinate to hold the Mac awake for
# ffmpeg's lifetime. The tests stand a `sleep` in for ffmpeg, and t22 checks
# the real function on its own.
REAL_KEEP_AWAKE = record._keep_awake
record._keep_awake = lambda pid: None


def reset():
    STATE.unlink(missing_ok=True)
    STAGING.unlink(missing_ok=True)
    STAGING.with_suffix(".log").unlink(missing_ok=True)
    config.LOG_FILE.unlink(missing_ok=True)
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for p in config.INBOX_DIR.iterdir():
        p.unlink()


def log_lines():
    if not config.LOG_FILE.exists():
        return []
    return [line.split("\t") for line in config.LOG_FILE.read_text().splitlines()]


def t11():
    cmd = record._ffmpeg_command(1, STAGING, 240)
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "14400", cmd
    assert cmd[-1] == str(STAGING) and cmd[-2] == "-y", cmd
    assert "-t" not in record._ffmpeg_command(1, STAGING, None)
results.append(run("ffmpeg itself is told when to stop", t11))


def t12():
    reset()
    assert record.Recorder.adopt() is None
    assert record.finish_abandoned() is None
    STATE.write_text("not json")
    assert record.Recorder.adopt() is None
    assert record.finish_abandoned() is None
    reset()
results.append(run("no state file, or a broken one, means nothing to adopt", t12))


def t13():
    reset()
    # A pid that is not running: the ffmpeg was killed outright, so the file
    # has no trailer. It stays where it is, but the state file goes.
    STAGING.write_bytes(b"x" * 1000)
    record._write_state(2 ** 22 - 1, STAGING, STARTED, None, "MacBook Pro Microphone")
    real = transcribe.duration_seconds
    transcribe.duration_seconds = lambda p: None
    try:
        assert record.Recorder.adopt() is None
        assert record.finish_abandoned() is None
    finally:
        transcribe.duration_seconds = real
    assert not STATE.exists(), "stale state must be cleared"
    assert STAGING.exists(), "an unplayable file must not be deleted"
    assert list(config.INBOX_DIR.iterdir()) == []
    reset()
results.append(run("a dead ffmpeg with an unplayable file is cleared, not filed", t13))


def t14():
    reset()
    # ffmpeg reached its time cap, finalized the file, and exited; the panel
    # that started it was already gone. The lecture must still reach inbox/.
    STAGING.write_bytes(b"x" * 1000)
    record._write_state(2 ** 22 - 1, STAGING, STARTED, None, "MacBook Pro Microphone")
    real = transcribe.duration_seconds
    transcribe.duration_seconds = lambda p: 4573.0
    try:
        filed = record.finish_abandoned()
    finally:
        transcribe.duration_seconds = real
    assert filed is not None and filed.parent == config.INBOX_DIR, filed
    assert filed.name == "ENTR-3306_2026-09-10_1233.m4a", filed.name
    assert not STATE.exists() and not STAGING.exists()
    assert record.finish_abandoned() is None
    reset()
results.append(run("a finished recording nobody moved is filed into the inbox", t14))


def t15():
    reset()
    # A live process standing in for ffmpeg. Its command line is not ffmpeg's,
    # so the ownership check is stubbed; t16 covers the check itself.
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    STAGING.write_bytes(b"x" * 1000)
    record._write_state(proc.pid, STAGING, STARTED, "RELI-3304", "Some Mic")
    real = record._process_is_recording
    record._process_is_recording = lambda pid, staging: pid == proc.pid
    try:
        rec = record.Recorder.adopt()
        assert rec is not None and rec.adopted, "a live pid must be adopted"
        assert rec.is_recording
        assert rec.started == STARTED and rec.course == "RELI-3304", (rec.started, rec.course)
        assert rec.device_name == "Some Mic"
        assert rec.staged_bytes == 1000
        assert rec.planned_name == "RELI-3304_2026-09-10_1233.m4a", rec.planned_name
        assert rec.elapsed > 60, rec.elapsed
        # finish_abandoned must leave a live recording alone.
        assert record.finish_abandoned() is None and STATE.exists()
        # A second start must refuse rather than open the mic twice.
        try:
            record.Recorder().start()
        except RuntimeError as exc:
            assert "already running" in str(exc), exc
        else:
            raise AssertionError("starting over a live recording should raise")
        filed = rec.stop()
    finally:
        record._process_is_recording = real
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    assert proc.returncode is not None, "stop must end the process"
    assert filed.name == "RELI-3304_2026-09-10_1233.m4a", filed
    assert filed.exists() and not STAGING.exists()
    assert not STATE.exists(), "stopping must clear the state file"
    assert not rec.is_recording
    reset()
results.append(run("a live recording from an earlier process is adopted and stopped", t15))


def t16():
    reset()
    # Ownership is pid + command line, so a reused pid can't be mistaken for
    # our ffmpeg and killed.
    assert not record._process_is_recording(2 ** 22 - 1, STAGING)
    assert not record._process_is_recording(os.getpid(), STAGING), \
        "this test process is not ffmpeg"
    reset()
results.append(run("a live pid that is not our ffmpeg is not adopted", t16))


def t17():
    reset()
    consent.record("setup")
    # start() records what it launched, without launching anything here.
    launched = []

    class FakePopen:
        pid = 4242
        def __init__(self, cmd, **kw):
            launched.append((cmd, kw))
        def poll(self):
            return None

    real_popen, real_which, real_awake = (record.subprocess.Popen, record.shutil.which,
                                          record._keep_awake)
    real_find = record.tools.find
    record.subprocess.Popen = FakePopen
    record.shutil.which = lambda name: f"/opt/homebrew/bin/{name}"
    # ffmpeg comes through tools.find: the bundled copy, a named folder, or PATH.
    record.tools.find = lambda name, env=None: f"/opt/homebrew/bin/{name}"
    record._keep_awake = REAL_KEEP_AWAKE
    record.list_devices = fake_devices
    try:
        rec = record.Recorder(course="RELI-3304")
        rec.start(max_minutes=90)
    finally:
        record.subprocess.Popen, record.shutil.which = real_popen, real_which
        record.tools.find = real_find
        record._keep_awake = real_awake
    cmds = {Path(cmd[0]).name: (cmd, kw) for cmd, kw in launched}
    assert set(cmds) == {"ffmpeg", "caffeinate"}, [c for c, _ in launched]
    cmd, kw = cmds["ffmpeg"]
    assert cmd[0] == "/opt/homebrew/bin/ffmpeg", "ffmpeg is run by the path tools found"
    assert kw["start_new_session"] is True
    assert cmd[cmd.index("-t") + 1] == "5400", cmd
    # Idle sleep ends the capture, so the Mac is held awake for exactly as
    # long as ffmpeg runs, by pid, with nothing to clean up afterwards.
    awake, _ = cmds["caffeinate"]
    assert awake[awake.index("-w") + 1] == "4242" and "-i" in awake, awake
    state = record._read_state()
    assert state is not None, "start must write the state file"
    assert state["pid"] == 4242 and state["course"] == "RELI-3304", state
    assert state["staging"] == rec._staging and state["started"] == rec.started, state
    assert state["device"] == "MacBook Pro Microphone", state
    assert rec._errors == rec._staging.with_suffix(".log") and rec._errors.exists()
    rec._errors.unlink()
    reset()
results.append(run("start writes the state file the next process will read", t17))


def t17b():
    # Permission to record is checked where every recording begins, so no
    # front end can skip it, and nothing is launched when it is missing.
    reset()
    config.CONSENT_FILE.unlink(missing_ok=True)
    launched = []

    class FakePopen:
        pid = 4243
        def __init__(self, cmd, **kw):
            launched.append(cmd)
        def poll(self):
            return None

    real_popen, real_find = record.subprocess.Popen, record.tools.find
    record.subprocess.Popen = FakePopen
    record.tools.find = lambda name, env=None: f"/opt/homebrew/bin/{name}"
    record.list_devices = fake_devices
    try:
        try:
            record.Recorder(course="RELI-3304").start(max_minutes=90)
        except consent.ConsentRequired as exc:
            assert "intake setup --consent" in str(exc), exc
            assert "permission to record" in str(exc), exc
        else:
            raise AssertionError("a recording started without permission to record")
        assert launched == [], f"something was launched anyway: {launched}"
        assert record._read_state() is None, "a refused start left a state file"

        # `intake record` says so up front, with the exact command, and fails.
        said = []
        real_log = record.log
        record.log = said.append
        try:
            assert record.main([]) == 1
        finally:
            record.log = real_log
        assert launched == [], launched
        assert any("intake setup --consent" in line for line in said), said

        # Once it is on file the same start goes ahead.
        consent.record("setup")
        rec = record.Recorder(course="RELI-3304")
        rec.start(max_minutes=90)
        assert [Path(c[0]).name for c in launched] == ["ffmpeg"], launched
        rec._errors.unlink(missing_ok=True)
    finally:
        record.subprocess.Popen, record.tools.find = real_popen, real_find
        reset()
results.append(run("no recording starts until permission to record is on file", t17b))


def t18():
    reset()
    # Two front ends held the same recording and the other one stopped it:
    # the file is gone and so is the state. That is not a failed recording.
    gone = record.Recorder(course="RELI-3304")
    gone.started = STARTED
    gone._staging = STAGING
    gone._proc = record._ExternalProcess(2 ** 22 - 1)
    assert gone.finish() is None
    assert not gone.is_recording
    # Whereas a recording that ended on its own with its file in place is filed.
    STAGING.write_bytes(b"x" * 1000)
    ended = record.Recorder(course="RELI-3304")
    ended.started = STARTED
    ended._staging = STAGING
    ended._proc = record._ExternalProcess(2 ** 22 - 1)
    filed = ended.finish()
    assert filed is not None and filed.name == "RELI-3304_2026-09-10_1233.m4a", filed
    assert filed.exists() and not STAGING.exists()
    reset()
results.append(run("finish() tells a recording filed elsewhere from one that ended", t18))

# --- a recording that captured nothing ------------------------------------
#
# A lecture once ran 50 minutes with the timer ticking while the microphone
# delivered nothing; the failure flashed for six seconds, went to a terminal
# nobody was watching, and the cleanup deleted the only evidence.

def t19():
    reset()
    errors = STAGING.with_suffix(".log")
    errors.write_text("[avfoundation] Input/output error\n")
    rec = record.Recorder(course="RELI-3304")
    rec.started = STARTED
    rec.device_name = "MacBook Pro Microphone"
    rec._staging, rec._errors = STAGING, errors
    rec._proc = record._ExternalProcess(2 ** 22 - 1)  # ffmpeg already gone
    try:
        rec.stop()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("a recording with no file must raise")
    assert "Input/output error" in message, message
    assert str(errors) in message, "the kept log must be named"
    assert errors.exists(), "ffmpeg's log is the only evidence; it must be kept"
    assert not STATE.exists()
    # pipeline.log carries the failure, named for the lecture that was lost,
    # on one line, so the panel's recent list shows it.
    lines = log_lines()
    assert len(lines) == 1 and lines[0][1] == "ERROR", lines
    assert lines[0][2] == "RELI-3304_2026-09-10_1233.m4a", lines[0]
    assert "no audio" in lines[0][3] and "\n" not in lines[0][3], lines[0]
    assert list(config.INBOX_DIR.iterdir()) == []
    reset()
results.append(run("a recording that produced nothing is logged and its ffmpeg log kept", t19))


def t20():
    reset()
    # The same failure with an empty file and a silent ffmpeg: the empty file
    # and empty log go, the failure still lands in pipeline.log.
    STAGING.write_bytes(b"")
    errors = STAGING.with_suffix(".log")
    errors.write_text("")
    rec = record.Recorder(course="RELI-3304")
    rec.started = STARTED
    rec.device_name = "MacBook Pro Microphone"
    rec._staging, rec._errors = STAGING, errors
    rec._proc = record._ExternalProcess(2 ** 22 - 1)
    try:
        rec.stop()
    except RuntimeError as exc:
        assert "Microphone" in str(exc) and "no error of its own" in str(exc), exc
    else:
        raise AssertionError("an empty file must raise")
    assert not STAGING.exists() and not errors.exists()
    assert len(log_lines()) == 1 and log_lines()[0][1] == "ERROR", log_lines()

    # Found later by another process instead: same outcome.
    reset()
    STAGING.write_bytes(b"")
    record._write_state(2 ** 22 - 1, STAGING, STARTED, "RELI-3304", "Some Mic")
    assert record.finish_abandoned() is None
    assert not STATE.exists() and not STAGING.exists()
    lines = log_lines()
    assert len(lines) == 1 and lines[0][2] == "RELI-3304_2026-09-10_1233.m4a", lines
    assert "Some Mic" in lines[0][3], lines[0]
    reset()
results.append(run("an empty recording, stopped or found abandoned, reaches pipeline.log", t20))


def t21():
    reset()

    class Alive:
        pid = 4242
        def poll(self):
            return None

    from datetime import timedelta
    rec = record.Recorder(course="RELI-3304")
    rec.device_name = "MacBook Pro Microphone"
    rec._staging = STAGING
    rec._proc = Alive()
    STAGING.write_bytes(b"x" * 44)  # the m4a header ffmpeg writes at once

    rec.started = datetime.now() - timedelta(seconds=5)
    assert not rec.stalled, "too early to tell"
    assert rec.warning == ""

    rec.started = datetime.now() - timedelta(seconds=config.RECORD_NO_AUDIO_SECONDS + 1)
    assert rec.stalled, "a header-only file after the grace period is a stall"
    assert "MacBook Pro Microphone" in rec.warning and "Microphone" in rec.warning, rec.warning

    STAGING.write_bytes(b"x" * (config.RECORD_NO_AUDIO_BYTES + 1))
    assert not rec.stalled, "audio on disk means the mic is live"

    rec._proc = record._ExternalProcess(2 ** 22 - 1)  # gone
    STAGING.write_bytes(b"x" * 44)
    assert not rec.stalled, "a finished recording is not stalled, it is over"
    reset()
results.append(run("a mic that delivers nothing is called out after the grace period", t21))


def t22():
    launched = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            launched.append((cmd, kw))

    real_popen, real_which = record.subprocess.Popen, record.shutil.which
    record.subprocess.Popen = FakePopen
    try:
        record.shutil.which = lambda name: None
        REAL_KEEP_AWAKE(1234)
        assert launched == [], "no caffeinate on this system means nothing to run"
        record.shutil.which = lambda name: "/usr/bin/caffeinate"
        REAL_KEEP_AWAKE(1234)
    finally:
        record.subprocess.Popen, record.shutil.which = real_popen, real_which
    assert len(launched) == 1, launched
    cmd, kw = launched[0]
    assert cmd[0] == "caffeinate" and cmd[-2:] == ["-w", "1234"], cmd
    assert kw["start_new_session"] is True and kw["stdin"] is subprocess.DEVNULL
results.append(run("caffeinate follows ffmpeg's pid and is skipped where it does not exist", t22))


def t23():
    # Whether a watcher is running is read from the lock, not from kill(0):
    # a pid in the file with nobody holding the lock is a dead watcher.
    import fcntl
    lock = config.LOCK_FILE
    lock.write_text(str(os.getpid()))
    assert not record._watcher_is_running(), "an unheld lock is a stopped watcher"
    handle = lock.open("r+")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert record._watcher_is_running(), "a held lock is a running watcher"
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
    assert not record._watcher_is_running()
    lock.unlink()
    assert not record._watcher_is_running()
results.append(run("the watcher is running when its lock is held, not when its pid exists", t23))


def t24():
    """tools: the bundled ffmpeg first, then $INTAKE_FFMPEG_DIR, then PATH."""
    import os
    import stat
    import tempfile
    import sys
    from intake import tools
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "bundle"
        (bundle / "ffmpeg").mkdir(parents=True)
        extra = Path(tmp) / "extra"
        extra.mkdir()
        for folder in (bundle / "ffmpeg", extra):
            for name in ("ffmpeg", "ffprobe"):
                exe = folder / name
                exe.write_text("#!/bin/sh\n")
                exe.chmod(exe.stat().st_mode | stat.S_IXUSR)

        # Outside the app: nothing bundled; the named folder wins over PATH.
        assert tools.bundle_dir() is None
        assert tools.search_dirs({}) == []
        assert tools.find("ffmpeg", {tools.FFMPEG_DIR_ENV_VAR: str(extra)}) == str(extra / "ffmpeg")
        assert tools.find("ffmpeg", {}) == record.shutil.which("ffmpeg")
        assert tools.find("ffmpeg", {tools.FFMPEG_DIR_ENV_VAR: str(Path(tmp) / "nowhere")}) \
            == record.shutil.which("ffmpeg"), "an empty folder falls through to PATH"
        assert tools.install_hint() == "brew install ffmpeg"
        assert not tools.bundled()

        # Inside Syllabus.app, PyInstaller names the unpacked folder in sys._MEIPASS.
        sys._MEIPASS = str(bundle)
        try:
            assert tools.bundle_dir() == bundle / "ffmpeg"
            assert tools.find("ffmpeg", {tools.FFMPEG_DIR_ENV_VAR: str(extra)}) \
                == str(bundle / "ffmpeg" / "ffmpeg"), "the bundled copy comes first"
            assert tools.find("ffprobe", {}) == str(bundle / "ffmpeg" / "ffprobe")
            assert tools.bundled()
            assert "Syllabus.app" in tools.install_hint()
            # A bundle missing its copy still falls back to PATH.
            (bundle / "ffmpeg" / "ffmpeg").unlink()
            assert tools.find("ffmpeg", {}) == record.shutil.which("ffmpeg")
            assert not tools.bundled()
        finally:
            del sys._MEIPASS
    assert tools.ffmpeg() == (record.shutil.which("ffmpeg") or "ffmpeg")
results.append(run("tools finds ffmpeg bundled first, then a named folder, then PATH", t24))

# --- the transcriber seam -------------------------------------------------
#
# The chunking rules are the provider's, not the audio's. These drive
# transcribe() with the ffmpeg calls and the API call stubbed out, so what is
# under test is only the decision: compress or not, split or not, into how
# many pieces, and whether a suspiciously long result gets re-split.

from intake import providers  # noqa: E402

MB = 1024 * 1024
# The real benchmark lecture: 39.5 minutes, 19.3MB of aac.
LECTURE_SECONDS = 2369.4
LECTURE_BYTES = 20287896


class FakeProvider:
    """A provider with limits set per test and no API behind it."""

    def __init__(self, name="fake", max_bytes=25 * MB, compress_threshold_bytes=24 * MB,
                 max_chunk_seconds=8 * 60, truncation_word_threshold=1700,
                 diarization=False, words=100):
        self.name = name
        self.max_bytes = max_bytes
        self.compress_threshold_bytes = compress_threshold_bytes
        self.max_chunk_seconds = max_chunk_seconds
        self.truncation_word_threshold = truncation_word_threshold
        self.diarization = diarization
        self.words = words
        self.calls = []

    def transcribe_file(self, path, prompt=""):
        self.calls.append(Path(path).name)
        words = self.words(len(self.calls)) if callable(self.words) else self.words
        return " ".join(["word"] * words)


def stub_of(provider):
    """A FakeProvider wearing a real provider's limits.

    Lets the decision be tested against the registry's actual numbers without
    an API call behind it.
    """
    return FakeProvider(
        name=provider.name,
        max_bytes=provider.max_bytes,
        compress_threshold_bytes=provider.compress_threshold_bytes,
        max_chunk_seconds=provider.max_chunk_seconds,
        truncation_word_threshold=provider.truncation_word_threshold,
        diarization=provider.diarization,
    )


def drive_transcribe(provider, *, size=LECTURE_BYTES, seconds=LECTURE_SECONDS):
    """transcribe() over a fake file, with ffmpeg and the network removed.

    Returns (text, log) where log records what the pipeline decided to do.
    """
    import math

    audio = config.WORK_DIR / "lecture.m4a"
    audio.write_bytes(b"x")
    events = {"compressed": False, "splits": [], "sizes": {}}
    events["sizes"][audio.name] = size

    def fake_size(path):
        return events["sizes"].get(Path(path).name, 1)

    def fake_duration(path):
        return events["sizes"].get(Path(path).name + ":seconds", seconds)

    def fake_compress(src, work_dir):
        events["compressed"] = True
        dest = work_dir / "compressed.m4a"
        events["sizes"][dest.name] = size // 4
        return dest

    def fake_split(src, work_dir, seconds):
        events["splits"].append((Path(src).name, seconds))
        length = fake_duration(src)
        count = max(1, math.ceil(length / seconds))
        out = []
        for i in range(count):
            chunk = work_dir / f"{Path(src).stem}_chunk{len(events['splits'])}_{i:03d}.m4a"
            events["sizes"][chunk.name] = fake_size(src) // count
            events["sizes"][chunk.name + ":seconds"] = length / count
            out.append(chunk)
        return out

    real = (transcribe._size, transcribe.duration_seconds,
            transcribe.compress, transcribe.split, transcribe.log)
    transcribe._size = fake_size
    transcribe.duration_seconds = fake_duration
    transcribe.compress = fake_compress
    transcribe.split = fake_split
    transcribe.log = lambda msg: None
    try:
        text = transcribe.transcribe(audio, provider=provider)
    finally:
        (transcribe._size, transcribe.duration_seconds, transcribe.compress,
         transcribe.split, transcribe.log) = real
        audio.unlink(missing_ok=True)
    return text, events


def t25():
    """The registry hands out each model's own limits, not one global set."""
    default = providers.get()
    assert default.name == config.TRANSCRIBE_MODEL
    # The config constants are still the default provider's values, so a
    # change there still changes what the default does.
    assert default.max_bytes == config.WHISPER_LIMIT_BYTES
    assert default.compress_threshold_bytes == config.COMPRESS_THRESHOLD_BYTES
    assert default.max_chunk_seconds == config.CHUNK_SECONDS
    assert default.truncation_word_threshold == config.TRUNCATION_WORD_THRESHOLD

    whisper = providers.get("whisper-1")
    assert whisper.max_chunk_seconds == 20 * 60, "whisper has no output cap to guard"
    assert whisper.truncation_word_threshold is None

    groq = providers.get("groq/whisper-large-v3")
    assert groq.model == "whisper-large-v3" and "groq.com" in groq.base_url
    assert groq.api_key_setting == "GROQ_API_KEY"
    assert groq.max_bytes == 100 * MB

    # Turbo is the same endpoint and key, a different model and price.
    turbo = providers.get("groq/whisper-large-v3-turbo")
    assert turbo.model == "whisper-large-v3-turbo"
    assert turbo.base_url == groq.base_url and turbo.api_key_setting == "GROQ_API_KEY"
    assert turbo.truncation_word_threshold is None, "no output cap to guard"

    deepgram = providers.get("deepgram/nova-3")
    # watch.preflight asks the provider which key it needs, so a non-OpenAI
    # provider must not be gated on an OpenAI key that is never used.
    assert deepgram.api_key_setting == "DEEPGRAM_API_KEY"
    assert default.api_key_setting == "OPENAI_API_KEY"
    assert config.DEEPGRAM_API_KEY == "" and config.GROQ_API_KEY == ""
    assert deepgram.max_chunk_seconds == providers.NO_DURATION_LIMIT
    assert deepgram.truncation_word_threshold is None
    assert deepgram.diarization and not default.diarization

    # An unregistered name is an OpenAI model on the default limits, which is
    # what TRANSCRIBE_MODEL meant before providers existed.
    other = providers.get("gpt-5-transcribe-imaginary")
    assert isinstance(other, providers.OpenAIProvider)
    assert other.max_chunk_seconds == config.CHUNK_SECONDS
    # Every provider satisfies the protocol it claims to.
    for prov in (default, whisper, groq, turbo, deepgram):
        assert isinstance(prov, providers.TranscriptionProvider), prov.name
results.append(run("each provider carries its own limits, defaulting to config's", t25))


def t26():
    """The same 39.5 minute lecture splits differently per provider."""
    text, events = drive_transcribe(stub_of(providers.get("gpt-4o-mini-transcribe")))
    assert events["splits"] == [("lecture.m4a", 8 * 60)], events["splits"]
    assert not events["compressed"], "19MB is under the 24MB threshold"

    _, whisper = drive_transcribe(FakeProvider(
        max_chunk_seconds=20 * 60, truncation_word_threshold=None))
    assert whisper["splits"] == [("lecture.m4a", 20 * 60)], whisper["splits"]

    _, deep = drive_transcribe(stub_of(providers.get("deepgram/nova-3")))
    assert deep["splits"] == [], "no duration limit means one request"
    assert not deep["compressed"], "19MB is far under Deepgram's threshold"
results.append(run("one lecture, three providers, three chunking decisions", t26))


def t27():
    """The split boundary is the provider's number, exactly."""
    for limit in (8 * 60, 20 * 60, 600):
        prov = FakeProvider(max_chunk_seconds=limit, truncation_word_threshold=None)
        _, at = drive_transcribe(prov, seconds=float(limit))
        assert at["splits"] == [], f"exactly {limit}s must not split"
        assert len(prov.calls) == 1

        over = FakeProvider(max_chunk_seconds=limit, truncation_word_threshold=None)
        _, past = drive_transcribe(over, seconds=limit + 1.0)
        assert past["splits"] == [("lecture.m4a", limit)], past["splits"]
        assert len(over.calls) == 2, over.calls
results.append(run("audio splits one second over the provider's limit, not at it", t27))


def t28():
    """Size splits and compression are the provider's thresholds too."""
    # Short but heavy: duration says no, size says yes.
    prov = FakeProvider(max_chunk_seconds=providers.NO_DURATION_LIMIT, truncation_word_threshold=None)
    _, events = drive_transcribe(prov, size=30 * MB, seconds=60.0)
    assert events["compressed"], "30MB is over the 24MB compress threshold"
    assert events["splits"] == [], "compression got it under 25MB"

    # Deepgram's thresholds are so much higher that the same file is one call.
    deep = stub_of(providers.get("deepgram/nova-3"))
    _, big = drive_transcribe(deep, size=30 * MB, seconds=60.0)
    assert not big["compressed"] and big["splits"] == []

    # Still over the cap after compressing: split on size.
    heavy = FakeProvider(max_chunk_seconds=providers.NO_DURATION_LIMIT, truncation_word_threshold=None)
    _, split_on_size = drive_transcribe(heavy, size=200 * MB, seconds=60.0)
    assert split_on_size["compressed"]
    assert split_on_size["splits"] == [("compressed.m4a", providers.NO_DURATION_LIMIT)]
results.append(run("compression and size splits follow the provider's bytes", t28))


def t29():
    """A chunk over max_bytes is a readable error naming the provider."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".m4a") as fh:
        fh.write(b"x" * 2048)
        fh.flush()
        prov = providers.OpenAIProvider("gpt-4o-mini-transcribe", max_bytes=1024)
        try:
            prov.transcribe_file(Path(fh.name))
        except RuntimeError as exc:
            assert "gpt-4o-mini-transcribe" in str(exc), exc
            assert "over" in str(exc) and "API limit" in str(exc), exc
        else:
            raise AssertionError("a chunk over max_bytes must raise")
results.append(run("an oversized chunk names the provider whose limit it broke", t29))


def t30():
    """Only a provider that can truncate gets the re-split treatment."""
    # Every chunk comes back implausibly long, so each is halved twice (the
    # depth cap) before the pipeline gives up and keeps what it has.
    truncating = FakeProvider(max_chunk_seconds=8 * 60, truncation_word_threshold=1700,
                              words=2000)
    _, events = drive_transcribe(truncating, seconds=16 * 60.0)
    assert events["splits"][0] == ("lecture.m4a", 8 * 60)
    halvings = [s for s in events["splits"][1:]]
    assert halvings, "a long result must be re-split"
    assert all(seconds < 8 * 60 for _, seconds in halvings), halvings

    # Same output, from a model with no output cap: nothing is re-split.
    cannot = FakeProvider(max_chunk_seconds=8 * 60, truncation_word_threshold=None,
                          words=2000)
    _, quiet = drive_transcribe(cannot, seconds=16 * 60.0)
    assert quiet["splits"] == [("lecture.m4a", 8 * 60)], quiet["splits"]
    assert len(cannot.calls) == 2, cannot.calls
results.append(run("re-splitting on suspected truncation is per-provider", t30))


def t31():
    # output_name is where a course stops being text and becomes a path.
    # `../escaped` used to file the recording in the profile directory above
    # inbox/, and an absolute course threw the inbox away altogether.
    when = datetime(2026, 9, 15, 14, 0)
    for course in ("../escaped", "/tmp/absolute", "MATH/101", "..",
                   "A\\B", "X\nY"):
        name = record.output_name(when, course=course)
        assert "/" not in name and "\\" not in name, (course, name)
        assert "\n" not in name and not name.startswith("."), (course, name)
        destination = config.INBOX_DIR / name
        assert destination.resolve().parent == config.INBOX_DIR.resolve(), \
            f"{course!r} escaped the inbox: {destination}"
    # It repairs rather than raises on purpose: this runs after the recording
    # is finalized and its state file cleared, so raising here would strand
    # audio that nothing ever comes back for.
    assert record.output_name(when, course="ACCT-4321") == \
        "ACCT-4321_2026-09-15_1400.m4a"
results.append(run("a course cannot move a finished recording out of the inbox", t31))


def t32():
    """A recording short enough to go up whole is checked for truncation too."""
    # 470 seconds against a 480 second limit: under the duration that forces a
    # split, so this went straight to the provider and skipped the only code
    # that notices a truncated answer. A fast talker in a 40 minute class can
    # pass the output cap without going near the duration limit.
    truncating = FakeProvider(max_chunk_seconds=8 * 60, truncation_word_threshold=1700,
                              words=1800)
    _, events = drive_transcribe(truncating, seconds=470.0)
    assert events["splits"], "a short recording was never re-split"
    assert len(truncating.calls) > 1, \
        f"the single-file path skipped the truncation check: {truncating.calls}"

    # A provider that cannot truncate still sends a short one in one piece.
    cannot = FakeProvider(max_chunk_seconds=8 * 60, truncation_word_threshold=None,
                          words=1800)
    _, quiet = drive_transcribe(cannot, seconds=470.0)
    assert quiet["splits"] == [], quiet["splits"]
    assert len(cannot.calls) == 1, cannot.calls

    # And a short, ordinary-length result is left alone whatever the provider.
    ordinary = FakeProvider(max_chunk_seconds=8 * 60, truncation_word_threshold=1700,
                            words=400)
    _, plain = drive_transcribe(ordinary, seconds=470.0)
    assert plain["splits"] == [], plain["splits"]
    assert len(ordinary.calls) == 1, ordinary.calls
results.append(run("a short recording is checked for truncation as well", t32))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
