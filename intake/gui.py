"""A small local control panel for the pipeline.

    intake panel          # opens http://127.0.0.1:5173 in your browser

The port is the profile's: 5173 for Syllabus, 5174 for Sous, so both panels
can run at once. --port overrides it.

Start and stop a recording, see whether the watcher is running, and check on
recent lectures, without remembering any commands. It drives the same modules
the CLI does, so anything started here behaves identically to the CLI.

The Setup page (/setup) is the browser version of `intake setup`: keys,
microphone, class schedule, Notion, and the Google Drive login, writing the
same .env and schedule.toml into the home directory. The panel opens it
first when nothing is configured yet.

Bound to localhost on purpose. It can start and stop processes and read your
pipeline log, none of which should be reachable from the network.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

from intake import account, assistant, cancellations, config, doctor, insights, relay, service, setup_wizard
from intake import logfiles, signin, sync
from intake import updates
from intake import notion_tasks
from intake import record as recording
from intake import transcribe

app = Flask(__name__)

# One recorder for the process, and a lock to make that true.
#
# "Single-user by construction" was the old claim here and it has not held for
# a while: app.run serves the local browser with threaded=True, and the relay
# runs relayed requests against this same app from a pool of four workers, so
# a phone on the web and the browser on this Mac are two genuine clients.
#
# There were already two guards on starting a second recording, the 409 in
# record_start and Recorder.adopt()'s refusal. Neither is atomic, so two
# starts that arrive together both pass both of them: observed, with real
# subprocesses, as two live captures on one microphone, only one of which the
# panel could then stop. The missing piece is mutual exclusion, not another
# check. Re-entrant because the routes call _current_recorder() while holding
# it, and that takes the lock too.
_recording_lock = threading.RLock()
_recorder: recording.Recorder | None = None

# What the desktop app (app.py) registers when this panel runs inside it:
# "show" brings its window forward. Empty under `intake panel`, and the
# status answer says so, which is how a second copy of the app knows whether
# to ask for the window or to be one.
window_hooks: dict[str, object] = {}

WATCHER_LOG = config.WORK_DIR / "watcher-gui.log"

# Watchers this panel started. They run detached and are meant to outlive
# us, but while we are alive we are still their parent, and a child nobody
# waits on stays a zombie: kill(pid, 0) keeps saying it is alive, and the
# panel keeps reporting a watcher that died hours ago. Polling them here is
# what reaps them.
_children: list[subprocess.Popen] = []


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _current_recorder() -> recording.Recorder | None:
    """The recording in progress, whoever started it.

    ffmpeg runs in its own session and outlives this panel, so a recording
    the previous panel started is still going when this one opens. Showing
    "Not recording" then is a lie with a cost: the person starts a second
    recording, or assumes the lecture is lost. The state file record.py keeps
    is how we find and take over that ffmpeg. This is also where a recording
    that ended on its own (ffmpeg's time cap) gets filed into inbox/.

    Under the lock because this reads and then writes the shared recorder,
    and the dashboard polls it through /api/status once a second: two polls
    landing together could both see a finished recording and both try to file
    it.
    """
    global _recorder
    with _recording_lock:
        if _recorder is not None and not _recorder.is_recording:
            finished, _recorder = _recorder, None
            try:
                filed = finished.finish()
                if filed is None:
                    _say("recording was stopped and filed from elsewhere")
                else:
                    _say(f"recording ended on its own; filed {filed.name}")
            except RuntimeError as exc:
                _say(f"recording ended on its own: {exc}")
        if _recorder is None:
            filed = recording.finish_abandoned()
            if filed is not None:
                _say(f"filed an earlier recording nobody stopped: {filed.name}")
            _recorder = recording.Recorder.adopt()
            if _recorder is not None:
                _say(f"picking up a recording already running since "
                     f"{_recorder.started:%H:%M} (pid {_recorder._proc.pid})")
        return _recorder


def _reap_children() -> None:
    """Collect exit statuses of watchers we started that have since ended."""
    for child in list(_children):
        if child.poll() is not None:
            _children.remove(child)


def _watcher_pid() -> int | None:
    """PID of the running watcher, or None.

    Decided by whether the lock is held, not by whether the pid answers: a
    watcher that was killed answers kill(0) as a zombie until its parent
    (this panel, when it started the watcher) reaps it, and a stale pid can
    be reused by an unrelated process. The flock goes away the instant the
    watcher does, whatever state its process record is in.
    """
    _reap_children()
    return config.watcher_pid()


def _courses() -> list[str]:
    """Every course in the schedule, or none while there is no schedule yet."""
    try:
        return config.courses()
    except config.ScheduleError:
        return []


class _BadRequest(ValueError):
    """A request whose shape is wrong, carrying what the page should say.

    Raised by the readers below and turned into a JSON 400 by one error
    handler, so a route reads the fields it wants and never has to check the
    body it was handed.
    """

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.extra = extra


@app.errorhandler(_BadRequest)
def _bad_request(exc: _BadRequest):
    return jsonify({"ok": False, "error": str(exc), **exc.extra}), 400


def _body() -> dict:
    """The request's JSON object, or a 400 rather than a 500.

    get_json returns whatever the body parsed to, and for `[1]`, `"x"` or
    `42` that is not a dict. Every route then called .get() on it, Flask
    turned the AttributeError into a 500 page of HTML, and the panel could
    not show any of it because it is waiting for JSON.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise _BadRequest("the request body must be a JSON object")
    return payload


def _known_device(name: str) -> bool:
    """Whether `name` is a microphone this Mac actually reports.

    A device already saved stays acceptable even when it is unplugged, so a
    Mac whose headset is in another room can still save its schedule.
    """
    if name == config.RECORD_DEVICE:
        return True
    try:
        return any(name == reported for _, reported in recording.list_devices())
    except OSError:
        # ffmpeg could not be asked. Refusing every save because of that would
        # be worse than accepting a name that is checked again at record time.
        return True


def _text(payload: dict, field: str) -> str:
    """One text field, stripped. Absent and null both read as empty."""
    value = payload.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _BadRequest(f"{field} must be text, not {type(value).__name__}")
    return value.strip()


def _mapping(payload: dict, field: str) -> dict:
    value = payload.get(field)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _BadRequest(f"{field} must be an object, not {type(value).__name__}")
    return value


def _rows(payload: dict, field: str) -> list[dict]:
    value = payload.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _BadRequest(f"{field} must be a list of class meetings")
    for n, row in enumerate(value, start=1):
        if not isinstance(row, dict):
            raise _BadRequest(
                f"class row {n} should have a day, a start and a course", row=n)
    return value


def _minutes(payload: dict, field: str, default: int) -> int:
    """A whole number of minutes, checked the way the schedule file checks it.

    int() took 1.8 and True and stored 1, which contradicted both the message
    it would have shown on a refusal and the stricter parser that reads the
    value back off disk. Same rule in both places now.
    """
    value = payload.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _BadRequest(f"{field} must be a whole number of minutes")
    return value


def _current_class() -> str | None:
    """The course scheduled right now, so the button can name it."""
    try:
        course = config.infer_course(datetime.now())
    except config.ScheduleError:
        return None
    return None if course == config.UNKNOWN_COURSE else course


def _join(parts: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    if len(parts) < 3:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _setup_needs() -> list[str]:
    """What is actually missing before a lecture can be processed.

    The dashboard used to say "Nothing is set up yet ... to add your keys and
    class schedule" whenever this was non-empty, which was wrong twice over
    for a signed-in Mac with Drive connected and no schedule: nothing was the
    wrong count, and keys were the wrong thing. Worse, it sent that user to a
    page that tells them "Nothing to do here" about keys and hides the fields,
    so following the instruction led nowhere.

    Returns the missing pieces in the order the Setup page presents them.
    """
    needs = []
    if not account.managed() and not (config.OPENAI_API_KEY and config.ANTHROPIC_API_KEY):
        needs.append("API keys")
    try:
        if not config.courses():
            needs.append("a class schedule")
    except config.ScheduleError:
        # Not the same as having none, and the old rollup could not tell them
        # apart: the file is there and unreadable, and saving rewrites it.
        needs.append("a readable class schedule")
    return needs


def _configured() -> bool:
    """Whether the pipeline can run at all: somewhere to spend, and classes.

    A signed-in Mac spends the service's keys, so it needs no keys of its own
    and the Setup page must not demand any. One that is not signed in is on
    its own two keys, as every Mac was before managed keys existed.
    """
    if not _courses():
        return False
    if account.managed():
        return True
    return bool(config.OPENAI_API_KEY and config.ANTHROPIC_API_KEY)


def _inbox() -> list[dict]:
    """Recordings waiting to be processed."""
    items = []
    if not config.INBOX_DIR.is_dir():
        return items
    for path in sorted(config.INBOX_DIR.iterdir()):
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and not path.name.startswith("."):
            items.append({"name": path.name, "bytes": path.stat().st_size})
    return items


def _log_rows() -> list[dict]:
    """Every lecture in pipeline.log, oldest first. See insights.parse_log."""
    if not config.LOG_FILE.exists():
        return []
    return insights.parse_log(config.LOG_FILE.read_text())


def _recent(limit: int = 12) -> list[dict]:
    """Recent lectures, newest first, parsed out of pipeline.log."""
    return list(reversed(_log_rows()))[:limit]


def _insights() -> dict:
    """The dashboard's tiles and charts: the log counted against the schedule."""
    try:
        schedule = config.schedule()
    except config.ScheduleError:
        schedule = None
    # Classes that were called off are not misses, so the streak and the
    # coverage rate are counted without them (see cancellations.py).
    off = {(r["course"], r["date"]): r["note"] for r in cancellations.load()}
    return insights.compute(_log_rows(), schedule, canceled=off)


def _processing(watcher_pid: int | None) -> dict | None:
    """What the watcher is working on right now, or None if it is idle.

    Tied to the live watcher's pid. A watcher killed mid-lecture leaves its
    status file behind, and reporting that stale stage forever would be worse
    than saying nothing.
    """
    if watcher_pid is None or not config.STATUS_FILE.exists():
        return None
    try:
        data = json.loads(config.STATUS_FILE.read_text())
    except (OSError, ValueError):
        return None
    if data.get("pid") != watcher_pid:
        return None

    started = data.get("started", "")
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
    except (TypeError, ValueError):
        # TypeError too: the status file is JSON on disk, so `started` can be
        # a number or null as easily as a bad string, and this runs on every
        # poll of /api/status.
        elapsed = 0
    return {
        "stage": data.get("stage", ""),
        "file": data.get("file", ""),
        "course": data.get("course", ""),
        "detail": data.get("detail", ""),
        "elapsed": round(max(0.0, elapsed)),
    }


def _drive_via_account_cached() -> bool:
    """Whether the account's Drive grant was connected the last time anyone
    asked. File only: the status poll never talks to the service."""
    return bool(account.enabled() and account.load() is not None
                and account.drive_cached().get("connected"))


def _integrations() -> dict:
    """Which pieces are configured. Presence only; no secrets leave here."""
    managed = account.managed()
    return {
        # On managed keys both are the service's, and the page says so rather
        # than showing two empty key fields nobody has to fill in.
        "managed": managed,
        "openai": managed or bool(config.OPENAI_API_KEY),
        "anthropic": managed or bool(config.ANTHROPIC_API_KEY),
        "drive": config.TOKEN_FILE.exists() or _drive_via_account_cached(),
        "notion": notion_tasks.enabled(),
    }


# --- The gate, when the panel is reached from elsewhere ------------------------
#
# The panel has no accounts of its own. Through the account service's relay
# (relay.py) the service is the sign-in and names the viewer; through a
# Cloudflare Tunnel, signin.py sends the browser to the service to sign in.
# Its gate runs before every request and sets g.viewer to who is looking.
# Requests straight from this Mac carry neither mark and are not gated, so
# the panel keeps working locally regardless.
signin.install(app)


def _base() -> str:
    """The path this panel is published under: "" here, "/p/<device>" through
    the relay, where the pages write every link and request. request.script_root
    is the WSGI name for it, set by relay.serve."""
    return request.script_root or ""


@app.get("/")
def index():
    return render_template("index.html", profile=config.PROFILE, base=_base())


@app.get("/setup")
def setup_page():
    return render_template("setup.html", profile=config.PROFILE, base=_base())


@app.get("/api/status")
def status():
    pid = _watcher_pid()
    rec = _current_recorder()
    active = rec is not None and rec.is_recording
    return jsonify({
        "recording": {
            "active": active,
            "elapsed": round(rec.elapsed, 1) if active else 0,
            "device": rec.device_name if active else "",
            "planned": rec.planned_name if active else "",
            "bytes": rec.staged_bytes if active else 0,
            "course": (rec.course or "") if active else "",
            # Started by an earlier panel and picked up by this one.
            "resumed": rec.adopted if active else False,
            # Open for a while with nothing on disk: the timer is ticking but
            # the mic is delivering nothing, and the page must say so.
            "stalled": rec.stalled if active else False,
            "warning": rec.warning if active else "",
        },
        "watcher": {"running": pid is not None, "pid": pid},
        "processing": _processing(pid),
        "inbox": _inbox(),
        "recent": _recent(),
        # What the tiles, the week grid, and the charts are drawn from.
        "insights": _insights(),
        "integrations": _integrations(),
        "courses": _courses(),
        "now_class": _current_class(),
        "configured": _configured(),
        # Named rather than rolled up, so the banner can say what is actually
        # missing instead of asking for everything.
        "setup_needs": _setup_needs(),
        # Who is looking, when the request came through the relay. Nobody,
        # always, on this Mac: a local request is never gated.
        "signed_in_as": g.get("viewer", ""),
        # The sign-in is the account service's, so the sign-out is there too.
        # Empty when nobody is signed in, which is when the page hides it.
        "signout_url": (account.url() + "/logout") if g.get("viewer") else "",
        # The panel's place on the web, and whether the socket to it is up.
        "relay": relay.status(),
        # The Syllabus account this Mac is claimed into, if any. File only;
        # the status poll never talks to the account service.
        "account": account.summary(),
        # Whether this panel runs inside the desktop app, which has a window
        # to show (see window_hooks).
        "window": "show" in window_hooks,
        # Whether a newer Syllabus has been released, from the file the daily
        # check writes (updates.py). Never the network.
        "update": updates.status(),
    })


@app.get("/api/allowance")
def allowance():
    """What is left on this account this month, for the page to say so.

    Its own route rather than a field on /api/status. The status poll runs
    every couple of seconds and never talks to the account service; this is
    asked once when the page loads and again when a recording ends, which is
    the only moment the numbers can have moved because of this Mac.

    A Mac with no account has no meters, and says so rather than failing.
    """
    if not account.managed():
        return jsonify({"ok": False, "error": "this Mac uses its own keys"})
    return jsonify(account.allowance())


@app.post("/api/window/show")
def window_show():
    """Bring the desktop app's window forward. A second copy of the app asks
    this instead of opening a window of its own."""
    hook = window_hooks.get("show")
    if hook is None:
        return jsonify({"ok": False, "error": "this panel has no window"}), 409
    hook()
    return jsonify({"ok": True})


# --- Start at login ------------------------------------------------------------

@app.get("/api/login-item")
def login_item_state():
    """Whether a launch agent starts this program at login (service.py)."""
    return jsonify(_login_item())


def _login_item() -> dict:
    return {
        "installed": service.installed(),
        # An agent from a different install: the Terminal install's panel
        # while the app is running, or the other way round.
        "other": service.installed() and not service.runs_this_program(),
        "label": (f"Start {config.PROFILE.title} when I log in" if "show" in window_hooks
                  else f"Start the {config.PROFILE.title} panel when I log in"),
    }


@app.post("/api/login-item")
def login_item_set():
    payload = _body()
    said: list[str] = []
    if payload.get("enabled"):
        rc = service.install(say=said.append, wait=0)
    else:
        rc = service.uninstall(say=said.append)
    if rc != 0:
        return jsonify({"ok": False, "error": " ".join(said) or "launchctl refused",
                        **_login_item()}), 500
    return jsonify({"ok": True, **_login_item()})


@app.post("/api/record/start")
def record_start():
    global _recorder
    # Read the body before taking the lock: it can refuse the request, and
    # there is no reason to hold up a poll while deciding that.
    payload = _body()
    course = _text(payload, "course") or None
    if course and course not in set(_courses()):
        return jsonify({"ok": False, "error": f"unknown course {course}"}), 400

    # The check and the start are one step. Apart, two clients pressing
    # Record at the same moment both passed the check and both opened the
    # microphone.
    with _recording_lock:
        if _current_recorder() is not None:
            return jsonify({"ok": False, "error": "already recording"}), 409
        try:
            _recorder = recording.Recorder(course=course)
            _recorder.start()
        except Exception as exc:
            _recorder = None
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify({"ok": True, "device": _recorder.device_name,
                        "planned": _recorder.planned_name})


@app.post("/api/record/stop")
def record_stop():
    global _recorder
    # Same reason as starting: two stops arriving together both took the same
    # recorder and both called stop() on it, and the second found the staging
    # file already moved into the inbox.
    with _recording_lock:
        rec = _current_recorder()
        if rec is None:
            return jsonify({"ok": False, "error": "not recording"}), 409
        try:
            destination = rec.stop()
        except Exception as exc:
            _recorder = None
            return jsonify({"ok": False, "error": str(exc)}), 500
        wall = rec.elapsed
        _recorder = None

    # Report the audio the file actually holds, not how long the button was
    # held down. Opening the capture device costs a second or more, and on a
    # cold mic considerably more, so the wall clock overstates short takes.
    captured = transcribe.duration_seconds(destination) or 0
    return jsonify({
        "ok": True,
        "name": destination.name,
        "bytes": destination.stat().st_size,
        "seconds": round(captured, 1),
        "lost": round(max(0.0, wall - captured), 1),
        "watching": _watcher_pid() is not None,
    })


@app.post("/api/watcher/start")
def watcher_start():
    if _watcher_pid() is not None:
        return jsonify({"ok": False, "error": "already running"}), 409
    WATCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    handle = WATCHER_LOG.open("a")
    # start_new_session so the watcher outlives this GUI: closing the control
    # panel should not abandon a lecture that is midway through transcription.
    #
    # config.program spells the command the way this process was started:
    # this interpreter and the package from a checkout or a pipx install, the
    # app's own binary inside Syllabus.app.
    child = subprocess.Popen(
        watcher_command(),
        cwd=str(config.program_cwd()), stdin=subprocess.DEVNULL,
        stdout=handle, stderr=handle, start_new_session=True,
    )
    _children.append(child)
    # A watcher that exits within its first moments never got going: it
    # could not take the lock, or preflight refused it. Saying "started"
    # anyway left the button looking dead, with the reason only in a log
    # nobody was reading.
    if _exited_early(child):
        _children.remove(child)
        return jsonify({"ok": False, "error": _last_error_line(WATCHER_LOG)}), 500
    return jsonify({"ok": True})


def watcher_command() -> list[str]:
    """The command that starts the watcher for this profile."""
    return config.program("watch")


def _exited_early(child: subprocess.Popen, seconds: float = 1.5) -> bool:
    """Whether the child ended within `seconds` of being started."""
    try:
        child.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _last_error_line(log: Path) -> str:
    """The most recent error the watcher logged, or a generic message."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines[-20:]):
        if "error:" in line:
            return line.split("error:", 1)[1].strip()
    return "the watcher exited right after starting; see watcher-gui.log"


@app.post("/api/watcher/stop")
def watcher_stop():
    pid = _watcher_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "not running"}), 409
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


# --- Setup page -------------------------------------------------------------

_drive_login = {"running": False, "error": ""}


def _schedule_rows() -> tuple[list[dict], int, str]:
    """The schedule as the page edits it, plus any problem reading the file."""
    if not config.SCHEDULE_FILE.exists():
        return [], config.DEFAULT_TOLERANCE_MINUTES, ""
    try:
        loaded = config.load_schedule()
    except config.ScheduleError as exc:
        return [], config.DEFAULT_TOLERANCE_MINUTES, str(exc)
    rows = [{"day": m.day, "start": m.hour, "course": m.course}
            for m in sorted(loaded.meetings,
                            key=lambda m: (config.DAYS.index(m.day), m.hour))]
    return rows, loaded.tolerance_minutes, ""


def _meeting_on(course: str, day: str) -> bool:
    """Whether the schedule has `course` meeting on that date at all.

    A cancellation has to name a real meeting. Without this, a typo writes
    a row that excuses nothing and sits in the file forever, and the page
    has no way to show that it did not take.
    """
    try:
        meetings = config.schedule().meetings
    except config.ScheduleError:
        return False
    weekday = date.fromisoformat(day).strftime("%a")
    return any(m.course == course and m.day == weekday for m in meetings)


def _filed_on(course: str, day: str) -> bool:
    """Whether a lecture is already filed for that class meeting."""
    return any(r["course"] == course and r["date"] == day for r in _log_rows())


@app.post("/api/class/cancel")
def class_cancel():
    """Mark a class as one that did not meet, so it is not counted as missed.

    The streak exists to show a habit, and a class the professor called off
    is not a lapse in it. Everything about the meeting is still on the
    schedule; this only says that this one date was not held, and
    /api/class/restore takes it back.
    """
    payload = _body()
    course = config.safe_course(_text(payload, "course"))
    note = _text(payload, "note")
    try:
        day = cancellations.clean_date(payload.get("date"))
    except cancellations.CancelError as exc:
        raise _BadRequest(str(exc)) from None
    if course == config.UNKNOWN_COURSE:
        raise _BadRequest("say which course did not meet")
    if not _meeting_on(course, day):
        raise _BadRequest(f"{course} does not meet on {day}")
    # A lecture that was filed is the answer to whether the class met, and
    # it outranks anything typed afterward.
    if _filed_on(course, day):
        raise _BadRequest(f"a lecture is already filed for {course} on {day}")
    cancellations.cancel(course, day, note, when=datetime.now().isoformat(timespec="seconds"))
    return jsonify({"ok": True, "course": course, "date": day, "note": note})


@app.post("/api/class/restore")
def class_restore():
    """Put a canceled meeting back on the books: it counts again."""
    payload = _body()
    course = config.safe_course(_text(payload, "course"))
    try:
        day = cancellations.clean_date(payload.get("date"))
    except cancellations.CancelError as exc:
        raise _BadRequest(str(exc)) from None
    cancellations.restore(course, day)
    return jsonify({"ok": True, "course": course, "date": day})


@app.get("/api/setup")
def setup_state():
    """Everything the Setup page shows. Keys are masked; they never leave here."""
    values = setup_wizard.read_env(config.ENV_FILE)
    try:
        devices = [{"index": i, "name": n} for i, n in recording.list_devices()]
    except OSError:
        devices = []
    rows, tolerance, schedule_error = _schedule_rows()
    notion_on = bool(values.get("NOTION_TOKEN") and values.get("NOTION_DATABASE"))
    acct = account.load()
    return jsonify({
        "home": str(config.HOME_DIR),
        # Signed in, the keys are the service's and step 1 is not this
        # person's job. The page swaps the two fields for a line saying so.
        "managed": account.managed(),
        "managed_email": acct.email if acct else "",
        "openai": setup_wizard.mask(values.get("OPENAI_API_KEY", "")),
        "openai_set": bool(values.get("OPENAI_API_KEY")),
        "anthropic": setup_wizard.mask(values.get("ANTHROPIC_API_KEY", "")),
        "anthropic_set": bool(values.get("ANTHROPIC_API_KEY")),
        "device": values.get("RECORD_DEVICE", "") or config.RECORD_DEVICE,
        "devices": devices,
        "schedule": rows,
        "tolerance": tolerance,
        "schedule_error": schedule_error,
        "days": list(config.DAYS),
        "notion": {
            "enabled": notion_on,
            "token": setup_wizard.mask(values.get("NOTION_TOKEN", "")),
            "token_set": bool(values.get("NOTION_TOKEN")),
            "database": values.get("NOTION_DATABASE", "") if notion_on else "",
        },
        "drive": {
            "connected": config.TOKEN_FILE.exists(),
            "running": _drive_login["running"],
            "error": _drive_login["error"],
            "account": _drive_account_state(),
        },
        "configured": _configured(),
    })


def _drive_account_state() -> dict:
    """The account's Drive grant, for the Setup page. Asks the service."""
    if not (account.enabled() and account.load() is not None):
        return {"available": False}
    out = {"available": True, "connect_url": account.url() + "/drive/connect",
           "connected": False, "google_email": "", "error": ""}
    try:
        status = account.drive_status()
        out["connected"] = bool(status.get("connected"))
        out["google_email"] = str(status.get("google_email") or "")
        out["revoked_reason"] = str(status.get("revoked_reason") or "")
    except Exception as exc:
        cached = account.drive_cached()
        out["connected"] = bool(cached.get("connected"))
        out["google_email"] = str(cached.get("google_email") or "")
        out["error"] = f"could not reach the account service: {exc}"
    return out


@app.post("/api/setup")
def setup_save():
    """Write .env and schedule.toml from the form, then reload settings.

    A blank key means "keep the one already on file", so the page never has
    to show a real key to let the user leave it alone.
    """
    payload = _body()
    # Every field is read and shape-checked before any rule is applied to it.
    # The other way round, a request with `schedule: 42` in it was told it was
    # missing API keys, which is true of the body but not what is wrong with
    # it, and the page showed that to the user.
    rows = _rows(payload, "schedule")
    tolerance = _minutes(payload, "tolerance", config.DEFAULT_TOLERANCE_MINUTES)
    notion = _mapping(payload, "notion")
    device = _text(payload, "device")

    values = setup_wizard.read_env(config.ENV_FILE)
    for field, key in (("openai_key", "OPENAI_API_KEY"),
                       ("anthropic_key", "ANTHROPIC_API_KEY")):
        given = _text(payload, field)
        if given:
            values[key] = given
    # A signed-in Mac spends the service's keys, so it has none of its own to
    # offer and the page deliberately hides the fields that would collect
    # them. Demanding them here is the same mistake _configured() and the
    # processing preflight already avoid, and it fires before anything is
    # written: without this branch a managed Mac cannot save a schedule, so it
    # never becomes configured and the panel reopens this page forever.
    if not account.managed() and (not values.get("OPENAI_API_KEY")
                                  or not values.get("ANTHROPIC_API_KEY")):
        return jsonify({"ok": False, "error": "both API keys are needed"}), 400

    # A microphone is chosen from a list this Mac produced, so a value that is
    # not on that list is not a microphone. Checked rather than trusted because
    # this field is written into the settings file: it was the way in for both
    # the secret-reference and the newline-injection findings, and neither
    # needed the value to be plausible.
    if device and not _known_device(device):
        return jsonify({"ok": False, "error": "that is not one of this Mac's "
                        "microphones; press Refresh and choose one"}), 400
    values["RECORD_DEVICE"] = device or values.get("RECORD_DEVICE") or config.RECORD_DEVICE

    meetings = []
    for n, row in enumerate(rows, start=1):
        try:
            meetings.append(config.Meeting(
                config.normalize_day(row.get("day", "")),
                config.normalize_hour(row.get("start", "")),
                config.normalize_course(row.get("course", "")),
            ))
        except (ValueError, AttributeError) as exc:
            return jsonify({"ok": False, "error": f"class row {n}: {exc}", "row": n}), 400
    if not meetings:
        return jsonify({"ok": False, "error": "add at least one class meeting"}), 400

    skipped = not notion.get("enabled")
    if skipped:
        values["NOTION_TOKEN"] = ""
        values["NOTION_DATABASE"] = ""
    else:
        token = _text(notion, "token")
        database = _text(notion, "database")
        if token:
            values["NOTION_TOKEN"] = token
        if database:
            values["NOTION_DATABASE"] = database
        if not (values.get("NOTION_TOKEN") and values.get("NOTION_DATABASE")):
            return jsonify({"ok": False, "error": "Notion needs both the integration "
                            "secret and the database URL, or untick it to skip"}), 400

    config.ensure_home()
    try:
        setup_wizard.write_env(config.ENV_FILE, values, notion_skipped=skipped)
    except setup_wizard.UnsafeSetting as exc:
        # The file is written whole or not at all, so nothing was saved.
        return jsonify({"ok": False, "error": str(exc)}), 400
    config.write_schedule(meetings, tolerance)
    config.reload()
    # The account's copy follows the file, when this Mac is signed in.
    sync.sync_later("save")
    return jsonify({"ok": True, "configured": _configured(),
                    "classes": len(meetings)})


@app.get("/api/doctor")
def doctor_report():
    checks = doctor.run_checks()
    return jsonify({
        # fix_web when the check has one: the page cannot work out from a CLI
        # instruction whether its own form can carry it out.
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                    "fix": c.fix_web or c.fix, "required": c.required,
                    "fix_is_web": bool(c.fix_web)} for c in checks],
        # Not called "ok": the page's fetch helper reads ok=false as a failed
        # request, and a doctor report with a failing check is a good report.
        "healthy": all(c.ok or not c.required for c in checks),
    })


def _run_drive_login() -> None:
    from intake import upload
    try:
        upload.get_credentials(interactive=True)
        _drive_login["error"] = ""
    except Exception as exc:  # reported to the page, never raised into Flask
        _drive_login["error"] = str(exc)
    finally:
        _drive_login["running"] = False


@app.post("/api/drive/login")
def drive_login():
    """Connect Drive: through the account when this Mac is signed in to one
    (the page opens the account's connect page), else the Desktop OAuth flow
    on this Mac, which opens the user's browser on its own. {"local": true}
    asks for the latter regardless."""
    payload = _body()
    if account.enabled() and account.load() is not None and not payload.get("local"):
        account.forget_drive_token()
        return jsonify({"ok": True, "open": account.url() + "/drive/connect"})
    if config.TOKEN_FILE.exists():
        return jsonify({"ok": False, "error": "Drive is already connected"}), 409
    if _drive_login["running"]:
        return jsonify({"ok": False, "error": "a login is already in progress"}), 409
    _drive_login.update(running=True, error="")
    threading.Thread(target=_run_drive_login, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/drive/disconnect")
def drive_disconnect():
    try:
        config.TOKEN_FILE.unlink(missing_ok=True)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


# --- The Syllabus account this Mac belongs to --------------------------------

@app.get("/api/account")
def account_state():
    """Everything the Setup page's account card shows.

    Asks the service to confirm the token when there is one, so a Mac that
    was removed from the account page shows as signed out here within one
    load rather than looking signed in forever.
    """
    out = account.summary()
    if not out["enabled"]:
        return jsonify(out)
    out["claim"] = account.claim_status()
    if out["signed_in"]:
        state, detail = account.whoami()
        out["check"] = {"state": state, "detail": detail}
        if state == "revoked":
            out = {**account.summary(), "claim": out["claim"],
                   "check": {"state": state, "detail": detail}}
        else:
            # Opening the Setup page is a good moment to pick up a schedule
            # another Mac saved; throttled so a page left open is quiet.
            sync.sync_later("setup", throttle=True)
            out["sync"] = sync.status()
            # This Mac's address on the web, and whether the socket is up.
            out["relay"] = relay.status()
    return jsonify(out)


@app.post("/api/account/claim")
def account_claim():
    payload = _body()
    try:
        started = account.start_claim(_text(payload, "name"))
    except RuntimeError as exc:
        code = 503 if "turned off" in str(exc) else 409
        return jsonify({"ok": False, "error": str(exc)}), code
    except Exception as exc:  # the service is unreachable
        return jsonify({"ok": False, "error": f"could not reach the account "
                        f"service: {exc}"}), 502
    started.pop("device_code", None)  # the poller's secret, not the page's
    return jsonify({"ok": True, **started})


@app.post("/api/account/cancel")
def account_cancel():
    account.cancel_claim()
    return jsonify({"ok": True})


@app.post("/api/account/signout")
def account_signout():
    account.sign_out()
    # The socket belonged to the device that just signed out.
    relay.reconnect()
    return jsonify({"ok": True})


# --- Study assistant ----------------------------------------------------------

@app.get("/api/assistant")
def assistant_state():
    """Whether the assistant can run here, and what it can be asked about.

    The panel asks this once on load to decide whether to enable the box. It
    needs a key of its own: this path talks to Anthropic directly rather than
    through the account service, so a managed Mac still has to have one set.
    """
    return jsonify({
        "ok": True,
        "ready": bool(config.ANTHROPIC_API_KEY),
        "reason": "" if config.ANTHROPIC_API_KEY else
                  "Add an Anthropic API key in Setup to use the study assistant.",
        "model": config.ASSISTANT_MODEL,
        "courses": assistant.courses(),
        "escalation": assistant.escalation_rate(),
    })


@app.post("/api/assistant/ask")
def assistant_ask():
    """Ask a question about one course. Streams the answer back as SSE.

    Streamed rather than returned whole because a study guide across a term
    takes the better part of a minute to write, and a minute of nothing is
    indistinguishable from a hang. Each event is one JSON object on a data:
    line, in the shape assistant.ask() yields.
    """
    # No cross-site check here: signin.install(app) gates every request
    # already, and this route is not special enough to second-guess it.
    if not config.ANTHROPIC_API_KEY:
        return jsonify({"ok": False, "error": "no Anthropic API key is set"}), 409

    body = request.get_json(silent=True) or {}
    question = str(body.get("question", ""))
    course = str(body.get("course", ""))
    if not course:
        return jsonify({"ok": False, "error": "no course chosen"}), 400

    def events():
        # Generated outside the request context, so anything raised here has
        # to arrive as an error event; the status line has already been sent.
        try:
            for event in assistant.ask(question, course):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:              # noqa: BLE001 - shown, not swallowed
            app.logger.warning("assistant failed: %s", exc)
            yield "data: " + json.dumps({"type": "error", "text": str(exc)}) + "\n\n"

    return app.response_class(events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def _is_loopback(host: str) -> bool:
    """Whether a --host value stays on this machine.

    The panel has no login, by design: it is a local control surface, and
    anything running as this user could do all of it directly anyway. So the
    only hosts it binds to unasked are loopback ones. What it does have is
    signin.cross_site(), which is a different question: not who is asking,
    but whether a web page somewhere else is the one doing the asking.
    """
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _open_browser_later(url: str, delay: float = 0.8) -> None:
    """Open the panel once the server has had a moment to bind."""
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake panel",
        description=f"Local control panel for {config.PROFILE.title}.")
    parser.add_argument("--port", type=int, default=config.PROFILE.panel_port,
                        help=f"{config.PROFILE.panel_port} for this profile")
    parser.add_argument("--host", default="127.0.0.1",
                        help="localhost by default; this can start processes")
    parser.add_argument("--expose", action="store_true",
                        help="allow --host to reach beyond this Mac. There is no "
                             "login on the panel: anyone who can reach the port "
                             "can start processes and rewrite your keys")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the page in a browser")
    args = parser.parse_args(argv)

    if not _is_loopback(args.host) and not args.expose:
        print(f"refusing to bind the panel to {args.host}: it has no login, and "
              f"anyone who can reach it can start and stop processes, read "
              f"pipeline.log, and rewrite the keys in .env. Keep it on this Mac, "
              f"or put something that authenticates in front of it (an SSH "
              f"tunnel, say) and pass --expose to say you have. To reach the "
              f"panel from elsewhere, sign this Mac in to a Syllabus account "
              f"instead: the account service relays it, with its own login in "
              f"front.", file=sys.stderr, flush=True)
        return 2

    # The panel refuses a request addressed to a hostname that is not this
    # Mac's, which is what stops a page elsewhere resolving its own name to
    # 127.0.0.1 and talking to the panel as though it were the same origin.
    # Behind a
    # proxy the name is whatever the operator chose, so there is nothing to
    # compare against and that one check stands down. The checks that do not
    # depend on the name keep running.
    signin.EXPOSED = bool(args.expose)

    url = prepare(args.host, args.port)
    if not args.no_browser:
        _open_browser_later(url)
    serve(args.host, args.port)
    return 0


def start_url(host: str, port: int) -> str:
    """Where the panel opens: the dashboard, or Setup while nothing is configured."""
    page = "" if _configured() else "setup"
    return f"http://{host}:{port}/{page}"


def prepare(host: str, port: int) -> str:
    """Everything the panel does before it listens. Returns the page to open.

    Shared by `intake panel` and the desktop app (app.py), which runs the
    server from a thread so its window can have the main one.
    """
    url = start_url(host, port)
    # Before the first line of this run is written, so it starts the fresh
    # file rather than ending the old one. launchd appends this process's
    # stdout and stderr to panel.log and nothing else ever trims it.
    logfiles.keep_trimmed(config.HOME_DIR / "panel.log")
    print(f"{config.PROFILE.title} control panel:  {url}", file=sys.stderr, flush=True)
    needs = _setup_needs()
    if needs:
        print(f"  the Setup page opens first: this Mac still needs "
              f"{_join(needs)}", file=sys.stderr, flush=True)
    # Say so now, in the terminal, if a lecture is already being recorded;
    # the page will show it too once it loads.
    _current_recorder()
    # A schedule saved on another Mac arrives now; the network never holds
    # the panel up, and a Mac with no account does nothing here.
    sync.sync_later("start")
    # The panel's place on the web: a socket to the account service, held
    # open from a thread, over which browsers at this Mac's address reach
    # it. Nothing without an account; it waits for one.
    relay.start(app)
    # Once a day, from a thread: is there a newer Syllabus to point at.
    updates.check_later()
    return url


class _QuietPolling(logging.Filter):
    """Keeps the once-a-second status poll out of the log.

    The dashboard asks for /api/status every second for as long as it is
    open, and each one was a line. A panel.log read for this was 3.7 MB and
    54,000 lines, of which fewer than fifty were about anything: the relay
    connecting and disconnecting, the events somebody would actually open
    this file to find, were a rounding error in the noise.

    Only the successful poll is dropped. A status request that failed still
    says so, because that is the interesting one.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ("GET /api/status" in message and " 200 " in message)


def serve(host: str, port: int) -> None:
    """Listen until the process ends."""
    logging.getLogger("werkzeug").addFilter(_QuietPolling())
    # load_dotenv=False: Flask would otherwise read a .env from the current
    # directory into the environment, so running the panel from a checkout
    # (or any folder with a stray .env) silently overrode the home directory's
    # settings. The only .env that counts is the one config already loaded.
    app.run(host=host, port=port, debug=False, threaded=True, load_dotenv=False)


if __name__ == "__main__":
    raise SystemExit(main())
