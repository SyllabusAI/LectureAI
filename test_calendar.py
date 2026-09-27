"""Tests for calendars.py and destinations.py: filing to-dos on Apple
Calendar, Apple Reminders and Google Calendar, next to Notion.

Nothing here reaches EventKit, Calendar.app, Google, or Notion. EventKit is
replaced by a fake module, osascript by a fake subprocess.run, Google by a
fake HTTP session, and Notion by stubs on notion_tasks, so no calendar on the
machine running this is ever touched. From the project root:

    .venv/bin/python test_calendar.py
"""
import json
import subprocess
import sys
import types
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import calendars, config, destinations, doctor, notion_tasks  # noqa: E402
from intake import gui  # noqa: E402


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    except Exception as exc:
        import traceback
        print(f"FAIL  {label}\n      unexpected {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False
    print(f"ok    {label}")
    return True


def reset():
    """Every calendar off, no ledger, no Google token, Notion off."""
    for key in calendars.KEYS:
        setattr(config, calendars.SETTING[key], "")
        setattr(config, calendars.NAME_SETTING[key], config.PROFILE.title)
    config.CALENDAR_LEDGER.unlink(missing_ok=True)
    config.CALENDAR_TOKEN_FILE.unlink(missing_ok=True)
    config.NOTION_TOKEN = ""
    config.NOTION_DATABASE = ""


class Swap:
    """Replace attributes for the length of a with-block."""

    def __init__(self, *triples):
        self.triples = triples
        self.saved = []

    def __enter__(self):
        for obj, name, value in self.triples:
            self.saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        return self

    def __exit__(self, *exc):
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)
        return False


class FakeBackend:
    """Stands in for any calendar: records what it was asked to create."""

    via = "fake"

    def __init__(self, state="granted", fail_on=()):
        self.state = state
        self.fail_on = set(fail_on)
        self.created = []

    def access(self):
        return self.state, f"state is {self.state}"

    def create(self, name, entry, uid):
        if any(word in entry.title for word in self.fail_on):
            raise calendars.CalendarError("simulated refusal")
        self.created.append((name, entry, uid))
        return f"id-{len(self.created)}"


ITEMS = [
    {"task": "Read chapter 7", "detail": "The **second** edition.",
     "due_date": "2026-09-22", "kind": "reading", "date_source": "stated"},
    {"task": "Submit the case memo", "detail": "", "due_date": "2026-09-24",
     "kind": "assignment", "date_source": "assumed"},
    {"task": "Think about a project topic", "detail": "", "due_date": "",
     "kind": "project", "date_source": "none"},
]

results = []


# --- Settings and defaults --------------------------------------------------------

def t1():
    reset()
    fresh = config.env_defaults(config.PROFILE)
    for key in calendars.KEYS:
        assert fresh[calendars.SETTING[key]] == "", f"{key} must ship switched off"
        assert not calendars.enabled(key), key
        assert calendars.calendar_name(key) == "Syllabus", calendars.calendar_name(key)
    from intake import profiles
    assert config.env_defaults(profiles.SOUS)["APPLE_CALENDAR_NAME"] == "Sous"
    assert config.env_defaults(profiles.SOUS)["GOOGLE_CALENDAR_NAME"] == "Sous"
    # calendar.app.created and nothing broader, and never on the Drive token.
    assert config.CALENDAR_SCOPES == ["https://www.googleapis.com/auth/calendar.app.created"]
    assert config.CALENDAR_TOKEN_FILE != config.TOKEN_FILE
    assert not set(config.CALENDAR_SCOPES) & set(config.DRIVE_SCOPES)
results.append(run("every calendar ships off, named after the profile, on its own token", t1))


def t2():
    reset()
    env = config.ENV_FILE
    env.write_text('OPENAI_API_KEY="sk-keep"\nANTHROPIC_API_KEY="ant-keep"\n'
                   'NOTION_TOKEN="ntn_keep"\nNOTION_DATABASE="https://notion.so/x"\n'
                   'DRIVE_SOURCE="local"\n')
    try:
        calendars.write_settings({"APPLE_CALENDAR": "on", "APPLE_CALENDAR_NAME": "Classes"})
        from intake import setup_wizard
        values = setup_wizard.read_env(env)
        assert values["OPENAI_API_KEY"] == "sk-keep", values
        assert values["NOTION_TOKEN"] == "ntn_keep", values
        assert values["DRIVE_SOURCE"] == "local", values
        assert values["APPLE_CALENDAR"] == "on", values
        assert calendars.enabled(calendars.APPLE), "reload did not pick the setting up"
        assert calendars.calendar_name(calendars.APPLE) == "Classes"
        try:
            calendars.write_settings({"OPENAI_API_KEY": "stolen"})
        except ValueError:
            pass
        else:
            raise AssertionError("write_settings accepted a setting that is not a calendar's")
        for bad in ("", "  ", "x" * 61, "null\x00byte"):
            try:
                calendars.clean_name(bad)
            except ValueError:
                continue
            raise AssertionError(f"clean_name accepted {bad!r}")
    finally:
        env.unlink(missing_ok=True)
        config.reload()
        reset()
results.append(run("switching a calendar on keeps every other setting in .env", t2))


# --- What goes on a calendar ---------------------------------------------------------

def t3():
    reset()
    first = calendars.entry_for(ITEMS[0], "ACCT-4321", "https://docs.test/1")
    assert first.title == "ACCT-4321: Read chapter 7", first.title
    assert first.due == date(2026, 9, 22) and first.all_day, first
    assert "The second edition." in first.notes, first.notes
    assert "*" not in first.notes, "markdown reached the calendar"
    assert "Course: ACCT-4321" in first.notes, first.notes
    assert "https://docs.test/1" in first.notes, first.notes
    assumed = calendars.entry_for(ITEMS[1], "ACCT-4321")
    assert "next scheduled meeting" in assumed.notes, assumed.notes
    assert calendars.entry_for(ITEMS[2], "ACCT-4321") is None, "an undated item got a day"
    unknown = calendars.entry_for(ITEMS[0], config.UNKNOWN_COURSE)
    assert unknown.title == "Read chapter 7", unknown.title
    timed = calendars.entry_for(dict(ITEMS[0], due_date="2026-09-22T23:59"), "ACCT-4321")
    assert timed.at == datetime(2026, 9, 22, 23, 59) and not timed.all_day, timed
    start, end = calendars.span(timed)
    assert end == timed.at and (end - start).seconds == 30 * 60, (start, end)
    start, end = calendars.span(first)
    assert (start, end) == (datetime(2026, 9, 22), datetime(2026, 9, 23)), (start, end)
    assert calendars.parse_due("next week") is None
results.append(run("an entry carries the task, course, notes link, and its day", t3))


# --- Filing, and filing only once -----------------------------------------------------

def t4():
    reset()
    fake = FakeBackend()
    out = calendars.push(calendars.APPLE, ITEMS, "ACCT-4321", "https://docs.test/1",
                         lecture="2026-09-15T14:00", reach=lambda key: fake)
    assert out["added"] == 2 and out["undated"] == 1 and out["failed"] == 0, out
    assert [e.title for _, e, _ in fake.created] == [
        "ACCT-4321: Read chapter 7", "ACCT-4321: Submit the case memo"], fake.created
    assert all(name == "Syllabus" for name, _, _ in fake.created)
    assert calendars.outcome_warning(calendars.APPLE, out, 3) == "", \
        "an undated item was reported as a failure"

    # The same lecture again: a resume, or the watcher picking it up twice.
    again = FakeBackend()
    out = calendars.push(calendars.APPLE, ITEMS, "ACCT-4321", "https://docs.test/1",
                         lecture="2026-09-15T14:00", reach=lambda key: again)
    assert again.created == [], f"a reprocessed lecture filed twice: {again.created}"
    assert out["skipped"] == 2 and out["added"] == 0, out

    # Reworded by a fresh summary, and with the date moved: still the same lecture.
    reworded = [dict(ITEMS[0], task="Read ch. 7", due_date="2026-09-23")]
    out = calendars.push(calendars.APPLE, reworded, "ACCT-4321",
                         lecture="2026-09-15T14:00", reach=lambda key: again)
    assert again.created == [] and out["skipped"] == 1, out

    # Another lecture bringing up the same assignment for the same day.
    out = calendars.push(calendars.APPLE, [ITEMS[0]], "ACCT-4321",
                         lecture="2026-09-17T14:00", reach=lambda key: again)
    assert again.created == [] and out["skipped"] == 1, out

    # Another lecture, a different due date: a new to-do.
    out = calendars.push(calendars.APPLE, [dict(ITEMS[0], due_date="2026-10-06")],
                         "ACCT-4321", lecture="2026-09-29T14:00", reach=lambda key: again)
    assert len(again.created) == 1 and out["added"] == 1, out

    # Each calendar keeps its own record: Reminders still gets everything.
    other = FakeBackend()
    out = calendars.push(calendars.REMINDERS, ITEMS, "ACCT-4321",
                         lecture="2026-09-15T14:00", reach=lambda key: other)
    assert out["added"] == 2, out

    ledger = json.loads(config.CALENDAR_LEDGER.read_text())
    assert len(ledger["items"]) == 5, ledger
    assert oct(config.CALENDAR_LEDGER.stat().st_mode & 0o777) == "0o600"
results.append(run("a lecture filed again never files a to-do twice", t4))


def t5():
    reset()
    fake = FakeBackend(fail_on={"chapter"})
    out = calendars.push(calendars.APPLE, ITEMS, "ACCT-4321",
                         lecture="L1", reach=lambda key: fake)
    assert out["added"] == 1 and out["failed"] == 1, out
    line = calendars.outcome_warning(calendars.APPLE, out, 3)
    assert line == "1 of 3 to-dos did not reach Apple Calendar: simulated refusal", line
    # The failed one is not recorded, so the next run tries it again.
    retry = FakeBackend()
    out = calendars.push(calendars.APPLE, ITEMS, "ACCT-4321",
                         lecture="L1", reach=lambda key: retry)
    assert [e.title for _, e, _ in retry.created] == ["ACCT-4321: Read chapter 7"], retry.created
results.append(run("one item failing costs only that item, and it is retried next time", t5))


def t6():
    reset()
    for state in ("denied", "restricted", "write_only", "not_determined"):
        fake = FakeBackend(state=state)
        try:
            calendars.push(calendars.APPLE, ITEMS, "ACCT-4321", reach=lambda key: fake)
        except calendars.CalendarError:
            assert fake.created == [], state
            continue
        raise AssertionError(f"{state} access filed anyway")
    try:
        calendars.push(calendars.APPLE, ITEMS, "ACCT-4321", reach=lambda key: None)
    except calendars.CalendarError as exc:
        assert "Apple Calendar" in str(exc), exc
    else:
        raise AssertionError("no backend did not raise")
    assert calendars.push(calendars.APPLE, [], "X", reach=lambda key: None)["added"] == 0, \
        "a lecture with no to-dos should not even look for a calendar"
results.append(run("a calendar without permission is refused before anything is written", t6))


# --- Every destination together -----------------------------------------------------

class Calls:
    def __init__(self):
        self.notion = 0
        self.calendars = []
        self.stages = []
        self.lines = []


def _file(calls, notion_on=True, notion_error=None, calendar_errors=(), on=(), items=ITEMS):
    def fake_notion_push(items, course, source_url="", dry_run=False):
        calls.notion += 1
        if notion_error:
            raise notion_error
        return {"added": len(items), "skipped": 0, "failed": 0, "notes": [],
                "reasons": [], "urls": []}

    def fake_calendar_push(key, items, course, source_url="", lecture="", reach=None):
        calls.calendars.append(key)
        if key in calendar_errors:
            raise calendars.CalendarError(f"{key} is broken")
        return {"added": 2, "skipped": 0, "failed": 0, "undated": 1,
                "notes": [], "reasons": [], "ids": []}

    for key in calendars.KEYS:
        setattr(config, calendars.SETTING[key], "on" if key in on else "")
    with Swap((notion_tasks, "enabled", lambda: notion_on),
              (notion_tasks, "push", fake_notion_push),
              (calendars, "push", fake_calendar_push)):
        return destinations.file_all(
            items, "ACCT-4321", "https://docs.test/1", "L1",
            status=lambda stage, detail="": calls.stages.append(stage),
            log=calls.lines.append)


def t7():
    reset()
    calls = Calls()
    out = _file(calls, notion_on=True, on=())
    assert calls.notion == 1 and calls.calendars == [], "a switched-off calendar was called"
    assert out["warning"] == "" and calls.stages == ["notion"], (out, calls.stages)
    assert "  notion: 3 added, 0 already there, 0 failed" in calls.lines, calls.lines

    calls = Calls()
    out = _file(calls, notion_on=True, on=calendars.KEYS)
    assert calls.calendars == list(calendars.KEYS), calls.calendars
    assert calls.stages == ["notion", "calendar", "calendar", "calendar"], calls.stages
    assert any("left off because it has no due date" in l for l in calls.lines), calls.lines
    assert out["warning"] == "", out
results.append(run("each destination that is on is filed to, and only those", t7))


def t8():
    reset()
    calls = Calls()
    out = _file(calls, notion_on=True, notion_error=RuntimeError("Notion is down"),
                calendar_errors=(calendars.APPLE,), on=calendars.KEYS)
    # Notion and Apple failed; Reminders and Google still ran.
    assert calls.calendars == list(calendars.KEYS), calls.calendars
    assert set(out["outcomes"]) == {calendars.REMINDERS, calendars.GOOGLE}, out["outcomes"]
    assert out["warnings"]["notion"] == "Notion was not reached: Notion is down", out
    assert out["warnings"][calendars.APPLE] == \
        "Apple Calendar was not reached: apple_calendar is broken", out
    assert out["warning"] == ("Notion was not reached: Notion is down | "
                              "Apple Calendar was not reached: apple_calendar is broken"), out
    assert "  notion: skipped (Notion is down)" in calls.lines, calls.lines
    assert "\t" not in out["warning"] and "\n" not in out["warning"]
results.append(run("one destination failing never stops the others, and each says so", t8))


def t9():
    # Notion alone behaves exactly as it did before destinations existed:
    # the same call, the same warning text, the same log line.
    reset()
    outcome = {"added": 0, "skipped": 0, "failed": 2, "notes": [],
               "reasons": ["no week heading covers 2026-09-22"], "urls": []}
    with Swap((notion_tasks, "enabled", lambda: True),
              (notion_tasks, "push", lambda items, course, source_url="": outcome)):
        lines = []
        out = destinations.file_all(ITEMS[:2], "ACCT-4321", "u", "L1", log=lines.append)
    assert out["warning"] == notion_tasks.outcome_warning(outcome, 2), out
    assert out["warning"] == "2 of 2 to-dos did not reach Notion: no week heading covers 2026-09-22"
    assert lines == ["  notion: 0 added, 0 already there, 2 failed"], lines

    long = RuntimeError("x" * 900)
    with Swap((notion_tasks, "enabled", lambda: True),
              (notion_tasks, "push", lambda *a, **k: (_ for _ in ()).throw(long))):
        out = destinations.file_all(ITEMS, "ACCT-4321", log=lambda _l: None)
    assert len(out["warning"]) == 300, len(out["warning"])

    calls = Calls()
    out = _file(calls, notion_on=True, on=calendars.KEYS, items=[])
    assert calls.notion == 1 and calls.calendars == [], \
        "no to-dos: Notion is still called as before, calendars are not"

    calls = Calls()
    out = _file(calls, notion_on=False, on=(), items=ITEMS)
    assert calls.lines == ["  3 action items (Notion not configured)"], calls.lines
results.append(run("with only Notion on, nothing about Notion changed", t9))


def t10():
    # End to end through watch.process: a calendar's warning reaches the log
    # line the panel reads, and the lecture is filed regardless.
    reset()
    from intake import summarize, transcribe, watch
    from intake import upload as drive
    import os
    audio = config.INBOX_DIR / "ACCT-4321_2026-09-15_1400.m4a"
    audio.write_text("AUDIO")
    stamp = datetime(2026, 9, 15, 15, 0).timestamp()
    os.utime(audio, (stamp, stamp))
    config.APPLE_CALENDAR = "on"
    seen = {}

    def fake_calendar_push(key, items, course, source_url="", lecture="", reach=None):
        seen.update(key=key, lecture=lecture, url=source_url, n=len(items))
        return {"added": 1, "skipped": 0, "failed": 1, "undated": 1, "ids": [],
                "notes": [], "reasons": ["Calendar said no"]}

    with Swap((transcribe, "duration_seconds", lambda p: 3600.0),
              (transcribe, "transcribe", lambda p, on_progress=None: "words here"),
              (summarize, "summarize", lambda t, c, d: {
                  "summary_md": "# notes", "topic_slug": "Costing",
                  "key_terms": [], "action_items": list(ITEMS)}),
              (summarize, "render_markdown", lambda r, c, d: r["summary_md"]),
              (drive, "upload", lambda path, course, interactive=True, **kw:
                  drive.Upload(url=f"https://drive.test/{kw.get('name')}", name=kw.get("name"))),
              (notion_tasks, "enabled", lambda: False),
              (calendars, "push", fake_calendar_push)):
        out = watch.process(audio, interactive=False)
    assert seen["key"] == calendars.APPLE and seen["n"] == 3, seen
    assert seen["url"] == out["summary_url"], seen
    assert seen["lecture"] == config.recording_key(stamp, 3600.0), seen
    line = config.LOG_FILE.read_text().splitlines()[-1].split("\t")
    assert line[5] == "1 of 3 to-dos did not reach Apple Calendar: Calendar said no", line
    assert out["notion_warning"] == "", out
    config.LOG_FILE.unlink(missing_ok=True)
    reset()
results.append(run("the watcher records a calendar's warning with the lecture", t10))


# --- Google Calendar ------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeGoogle:
    """Answers the Calendar API calls calendars.py makes, and records them."""

    def __init__(self, event_status=None):
        self.calls = []
        self.calendars = 0
        self.event_status = list(event_status or [])

    def request(self, method, url, json=None, timeout=None):
        path = url.removeprefix(calendars.GOOGLE_API)
        self.calls.append((method, path, json))
        if method == "POST" and path == "/calendars":
            self.calendars += 1
            return FakeResponse(200, {"id": f"cal{self.calendars}@group.calendar.google.com"})
        if method == "POST" and path.endswith("/events"):
            status = self.event_status.pop(0) if self.event_status else 200
            if status == 200:
                return FakeResponse(200, {"id": json["id"]})
            return FakeResponse(status, {"error": {"message": f"status {status}"}})
        if method == "GET":
            return FakeResponse(200, {"id": path})
        return FakeResponse(400, {"error": {"message": "unexpected"}})


def google_with(fake):
    backend = calendars.GoogleBackend()
    backend.access = lambda: ("granted", "signed in")
    backend._http = fake
    return backend


def t11():
    reset()
    fake = FakeGoogle()
    backend = google_with(fake)
    out = calendars.push(calendars.GOOGLE, ITEMS, "ACCT-4321", "https://docs.test/1",
                         lecture="L1", reach=lambda key: backend)
    assert out["added"] == 2 and out["undated"] == 1, out
    assert fake.calendars == 1, "the app's calendar was created more than once"
    create = fake.calls[0]
    assert create[:2] == ("POST", "/calendars") and create[2]["summary"] == "Syllabus", create
    event = fake.calls[1][2]
    assert fake.calls[1][1] == "/calendars/cal1@group.calendar.google.com/events", fake.calls[1]
    assert event["start"] == {"date": "2026-09-22"} and event["end"] == {"date": "2026-09-23"}, event
    assert event["summary"] == "ACCT-4321: Read chapter 7", event
    assert event["source"] == {"title": "Notes", "url": "https://docs.test/1"}, event
    # Google's rule for a client-chosen id: base32hex, 5 to 1024 characters.
    assert set(event["id"]) <= set("0123456789abcdefghijklmnopqrstuv"), event["id"]
    assert 5 <= len(event["id"]) <= 1024, event["id"]
    assert event["id"] == calendars.item_uid(calendars.GOOGLE, "L1", "Read chapter 7")

    # A second lecture reuses the calendar it remembered.
    fake2 = FakeGoogle()
    backend2 = google_with(fake2)
    calendars.push(calendars.GOOGLE, [dict(ITEMS[0], due_date="2026-10-01")], "ACCT-4321",
                   lecture="L2", reach=lambda key: backend2)
    assert fake2.calendars == 0, "a remembered calendar was created again"
results.append(run("Google Calendar gets one calendar of its own and all-day events", t11))


def t12():
    reset()
    # 409: an event with this id exists already, so it was filed before the
    # ledger was lost. Counted as filed, not as a failure.
    backend = google_with(FakeGoogle(event_status=[409]))
    out = calendars.push(calendars.GOOGLE, [ITEMS[0]], "ACCT-4321", lecture="L1",
                         reach=lambda key: backend)
    assert out["added"] == 1 and out["failed"] == 0, out

    # 404: the calendar was deleted in Google Calendar. Made again, once.
    reset()
    fake = FakeGoogle(event_status=[404, 200])
    backend = google_with(fake)
    out = calendars.push(calendars.GOOGLE, [ITEMS[0]], "ACCT-4321", lecture="L1",
                         reach=lambda key: backend)
    assert out["added"] == 1 and fake.calendars == 2, (out, fake.calls)

    # 403: a refusal is reported, with Google's own words.
    reset()
    backend = google_with(FakeGoogle(event_status=[403, 403]))
    out = calendars.push(calendars.GOOGLE, ITEMS[:2], "ACCT-4321", lecture="L1",
                         reach=lambda key: backend)
    assert out["failed"] == 2, out
    assert out["reasons"] == ["Google Calendar returned 403: status 403"], out
results.append(run("Google Calendar's duplicate, deleted-calendar and refusal answers", t12))


def t13():
    reset()
    g = calendars.GoogleBackend()
    assert g.access()[0] == "not_determined"
    token = config.CALENDAR_TOKEN_FILE
    token.write_text("[]")
    assert g.access()[0] == "denied"
    token.write_text(json.dumps({"refresh_token": "r", "scopes": config.DRIVE_SCOPES}))
    assert g.access()[0] == "denied", "a Drive-scoped token passed for a calendar one"
    token.write_text(json.dumps({"scopes": config.CALENDAR_SCOPES}))
    assert g.access()[0] == "denied"
    token.write_text(json.dumps({"refresh_token": "r", "scopes": config.CALENDAR_SCOPES}))
    assert g.access()[0] == "granted"
    # The Drive token is not the calendar token and is never read for it.
    assert not config.TOKEN_FILE.exists()
    token.unlink()
results.append(run("Google Calendar's own token is judged on its own scope", t13))


# --- Apple, through AppleScript --------------------------------------------------------

def t14():
    reset()
    ran = []

    def fake_run(cmd, **kwargs):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="UID-1\n", stderr="")

    hostile = dict(ITEMS[0], task='Read "chapter 7" end tell do shell script "rm -rf ~"')
    with Swap((subprocess, "run", fake_run)):
        out = calendars.push(calendars.APPLE, [hostile], "ACCT-4321", "https://docs.test/1",
                             lecture="L1", reach=lambda key: calendars.AppleScriptBackend())
    assert out["added"] == 1 and out["ids"] == ["UID-1"], out
    cmd = ran[0]
    assert cmd[:3] == ["osascript", "-e", calendars.APPLESCRIPT_CREATE], cmd[:3]
    assert "rm -rf" not in cmd[2], "task text was pasted into the script"
    assert cmd[3] == "Syllabus" and "rm -rf" in cmd[4], cmd
    assert cmd[7:] == ["2026", "9", "22", "1", "0", str(24 * 3600)], cmd[7:]

    def refused(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="",
                                           stderr="execution error: Not authorized (-1743)")
    with Swap((subprocess, "run", refused)):
        state, detail = calendars.AppleScriptBackend().request_access()
    assert state == "denied" and "Automation" in detail, (state, detail)
results.append(run("AppleScript gets every value as an argument, never as script", t14))


# --- Apple, through EventKit (a fake one) ------------------------------------------------

class FakeCal:
    def __init__(self, title, writable=True):
        self._title = title
        self._writable = writable
        self._source = None

    def title(self):
        return self._title

    def setTitle_(self, t):
        self._title = t

    def allowsContentModifications(self):
        return self._writable

    def setSource_(self, s):
        self._source = s

    def source(self):
        return "iCloud"


class FakeItem:
    count = 0

    def __init__(self):
        FakeItem.count += 1
        self.ident = f"EK-{FakeItem.count}"
        self.props = {}

    def __getattr__(self, name):
        if name.startswith("set") and name.endswith("_"):
            return lambda value: self.props.__setitem__(name[3:-1], value)
        raise AttributeError(name)

    def eventIdentifier(self):
        return self.ident

    def calendarItemIdentifier(self):
        return self.ident


class FakeStore:
    status = {0: 3, 1: 3}

    def __init__(self):
        self.calendars = {0: [FakeCal("Work"), FakeCal("Syllabus", writable=False)],
                          1: [FakeCal("Reminders")]}
        self.saved_calendars = []
        self.saved = []

    @classmethod
    def alloc(cls):
        return cls

    @classmethod
    def init(cls):
        return STORE

    @classmethod
    def authorizationStatusForEntityType_(cls, entity):
        return cls.status[entity]

    def calendarsForEntityType_(self, entity):
        return self.calendars[entity]

    def defaultCalendarForNewEvents(self):
        return self.calendars[0][0]

    def defaultCalendarForNewReminders(self):
        return self.calendars[1][0]

    def saveCalendar_commit_error_(self, cal, commit, error):
        self.saved_calendars.append(cal)
        self.calendars[0 if cal.kind == 0 else 1].append(cal)
        return True, None

    def saveEvent_span_commit_error_(self, event, span, commit, error):
        self.saved.append(event)
        return True, None

    def saveReminder_commit_error_(self, reminder, commit, error):
        self.saved.append(reminder)
        return True, None


STORE = FakeStore()


def fake_eventkit():
    ek = types.SimpleNamespace()
    ek.EKEventStore = FakeStore

    class EKCalendar:
        @staticmethod
        def calendarForEntityType_eventStore_(entity, store):
            cal = FakeCal("")
            cal.kind = entity
            return cal
    ek.EKCalendar = EKCalendar
    ek.EKEvent = types.SimpleNamespace(eventWithEventStore_=lambda store: FakeItem())
    ek.EKReminder = types.SimpleNamespace(reminderWithEventStore_=lambda store: FakeItem())
    ek.EKSpanThisEvent = 0
    ek.EKSourceTypeCalDAV = 2
    ek.EKSourceTypeLocal = 0
    return ek


class FakeComponents:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        self.parts = {}
        return self

    def __getattr__(self, name):
        if name.startswith("set") and name.endswith("_"):
            return lambda value: self.parts.__setitem__(name[3:-1], value)
        raise AttributeError(name)


def fake_foundation():
    return types.SimpleNamespace(
        NSDate=types.SimpleNamespace(dateWithTimeIntervalSince1970_=lambda t: ("date", t)),
        NSURL=types.SimpleNamespace(URLWithString_=lambda u: ("url", u)),
        NSDateComponents=FakeComponents,
    )


def t15():
    global STORE
    reset()
    STORE = FakeStore()
    ek = fake_eventkit()
    saved_foundation = sys.modules.get("Foundation")
    sys.modules["Foundation"] = fake_foundation()
    try:
        with Swap((calendars.EventKitBackend, "module", staticmethod(lambda: ek))):
            events = calendars.EventKitBackend(calendars.APPLE)
            out = calendars.push(calendars.APPLE, ITEMS, "ACCT-4321", "https://docs.test/1",
                                 lecture="L1", reach=lambda key: events)
            assert out["added"] == 2 and out["ids"][0].startswith("EK-"), out
            # The read-only "Syllabus" calendar is not written to; a new one is made.
            assert len(STORE.saved_calendars) == 1, STORE.saved_calendars
            made = STORE.saved_calendars[0]
            assert made.title() == "Syllabus" and made._source == "iCloud", made.__dict__
            event = STORE.saved[0].props
            assert event["Title"] == "ACCT-4321: Read chapter 7" and event["AllDay"] is True, event
            midnight = datetime(2026, 9, 22).timestamp()
            assert event["StartDate"] == ("date", midnight), event
            assert event["EndDate"] == ("date", midnight), "EventKit's all-day end is the same day"
            assert event["Calendar"] is made and event["URL"] == ("url", "https://docs.test/1")

            STORE.saved.clear()
            reminders = calendars.EventKitBackend(calendars.REMINDERS)
            out = calendars.push(calendars.REMINDERS, ITEMS[:1], "ACCT-4321",
                                 lecture="L1", reach=lambda key: reminders)
            due = STORE.saved[0].props["DueDateComponents"].parts
            assert due == {"Year": 2026, "Month": 9, "Day": 22}, due

            for code, word in ((0, "not_determined"), (1, "restricted"), (2, "denied"),
                               (3, "granted"), (4, "write_only")):
                FakeStore.status = {0: code, 1: code}
                state, detail = calendars.EventKitBackend(calendars.APPLE).access()
                assert state == word, (code, state)
                if word == "denied":
                    assert "Privacy & Security > Calendars" in detail, detail
            FakeStore.status = {0: 3, 1: 3}
    finally:
        if saved_foundation is None:
            sys.modules.pop("Foundation", None)
        else:
            sys.modules["Foundation"] = saved_foundation
results.append(run("EventKit files into its own calendar and reads permission without asking", t15))


# --- The panel and the doctor ----------------------------------------------------------

def t16():
    reset()
    client = gui.app.test_client()
    data = client.get("/api/calendar").get_json()
    keys = [d["key"] for d in data["destinations"]]
    assert keys == list(calendars.KEYS), keys
    google = data["destinations"][2]
    assert "preview" not in google and not google["enabled"], google

    asked = []

    def connect(key):
        asked.append(key)
        return ("denied", "Syllabus is not allowed to use Calendars.")

    fake_describe = calendars.describe
    with Swap((calendars, "connect", connect)):
        res = client.post("/api/calendar", json={"destination": "apple_calendar", "enabled": True})
        assert res.status_code == 400 and "not allowed" in res.get_json()["error"], res.get_json()
        assert not calendars.enabled(calendars.APPLE), "switched on without permission"
    with Swap((calendars, "connect", lambda key: ("granted", "ok"))):
        res = client.post("/api/calendar", json={"destination": "apple_calendar", "enabled": True,
                                                 "name": "  My  classes "})
        assert res.status_code == 200, res.get_json()
        assert calendars.enabled(calendars.APPLE)
        assert calendars.calendar_name(calendars.APPLE) == "My classes"
    res = client.post("/api/calendar", json={"destination": "apple_calendar", "enabled": False})
    assert res.status_code == 200 and not calendars.enabled(calendars.APPLE)
    assert asked == ["apple_calendar"]

    for bad in ({"destination": "outlook", "enabled": True},
                {"destination": "apple_calendar", "enabled": "yes"},
                {"destination": "apple_calendar", "name": "a\x00b"}):
        res = client.post("/api/calendar", json=bad)
        assert res.status_code == 400, (bad, res.get_json())

    # Google signs in in the browser, on a thread; the page polls.
    started = []

    class NoThread:
        def __init__(self, target, args=(), daemon=None):
            started.append((target, args))

        def start(self):
            target, args = started[-1]
            target(*args)

    with Swap((calendars, "connect", lambda key: ("granted", "signed in")),
              (gui.threading, "Thread", NoThread)):
        res = client.post("/api/calendar", json={"destination": "google_calendar", "enabled": True})
    assert res.get_json().get("connecting"), res.get_json()
    assert calendars.enabled(calendars.GOOGLE), "a finished Google sign-in did not switch it on"
    assert calendars.describe is fake_describe

    with Swap((calendars, "test", lambda key: ("granted", "allowed; it is there"))):
        res = client.post("/api/calendar/test", json={"destination": "apple_reminders"})
    assert res.get_json()["works"] and res.get_json()["detail"] == "allowed; it is there"
    config.ENV_FILE.unlink(missing_ok=True)
    config.reload()
    reset()
results.append(run("the Setup routes switch a calendar on only once it is allowed", t16))


def t17():
    reset()
    names = [c.name for c in doctor.run_checks()]
    assert not any(n in names for n in calendars.LABELS.values()), \
        f"a calendar nobody switched on appeared in the doctor: {names}"
    config.GOOGLE_CALENDAR = "on"
    check = [c for c in doctor.run_checks() if c.name == "Google Calendar"][0]
    assert not check.ok and not check.required, check
    assert check.fix == "intake calendar --connect google", check
    config.CALENDAR_TOKEN_FILE.write_text(json.dumps(
        {"refresh_token": "r", "scopes": config.CALENDAR_SCOPES}))
    check = [c for c in doctor.run_checks() if c.name == "Google Calendar"][0]
    assert check.ok, check
    reset()
results.append(run("the doctor has a line for each calendar that is on, and only those", t17))


def t18():
    # The Setup page's calendar card, as served: one card, its own script.
    html = gui.app.test_client().get("/setup").get_data(as_text=True)
    assert 'id="calendarCard"' in html and "loadCalendars();" in html
    assert "\u2014" not in html, "an em dash reached the Setup page"
    spec = (Path(__file__).resolve().parent / "packaging" / "syllabus.spec").read_text()
    for key in ("NSCalendarsFullAccessUsageDescription", "NSCalendarsUsageDescription",
                "NSRemindersFullAccessUsageDescription", "NSRemindersUsageDescription"):
        assert key in spec, f"{key} is missing from Info.plist, and macOS refuses without it"
results.append(run("the page and the app bundle carry what macOS needs", t18))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
